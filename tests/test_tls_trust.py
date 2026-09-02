"""Tests for `actions.tls_trust`.

Run with either runner:

    python -m unittest discover -s tests
    pytest tests/test_tls_trust.py

The TLS tests need `cryptography` to mint certificates. They skip when it is
absent so a minimal install can still run the suite - but set
`SAM_TESTS_REQUIRE_TLS=1` and the skips become failures instead. That switch
exists because a silently skipped certificate test is indistinguishable from a
passing one on a CI dashboard, and these are the tests that matter most.
"""

from __future__ import annotations

import functools
import os
import socket
import ssl
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions"))

import tls_trust  # noqa: E402
from tls_trust import (  # noqa: E402
    CaBundleError,
    anthropic_http_client,
    apply_snowflake_ca_env,
    build_ssl_context,
    describe_trust,
    resolve_ca_bundle,
    validate_ca_bundle,
)

REQUIRE_TLS = os.environ.get("SAM_TESTS_REQUIRE_TLS") == "1"

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    HAVE_CRYPTOGRAPHY = True
except BaseException:  # pragma: no cover - minimal installs only
    # Broad on purpose: a `cryptography` wheel with broken native bindings
    # raises a Rust `pyo3_runtime.PanicException`, which derives from
    # BaseException and would otherwise abort collection of the whole module.
    HAVE_CRYPTOGRAPHY = False


def needs_crypto(func):
    """Skip unless certs can be minted - unless the suite demands them."""
    if HAVE_CRYPTOGRAPHY:
        return func

    if REQUIRE_TLS:

        @functools.wraps(func)
        def demand(self, *args, **kwargs):
            self.fail(
                "cryptography is unavailable, so this certificate test cannot "
                "run, and SAM_TESTS_REQUIRE_TLS=1 forbids skipping it. Install "
                "cryptography (a wheel with working native bindings) or unset "
                "SAM_TESTS_REQUIRE_TLS."
            )

        return demand

    return unittest.skip("cryptography is required to mint test certs")(func)


# --------------------------------------------------------------------------- #
# Certificate helpers
# --------------------------------------------------------------------------- #
def _mint_ca(tmp: Path, name: str) -> tuple[Path, x509.Certificate, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path = tmp / f"{name}.pem"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path, cert, key


def _mint_leaf(
    tmp: Path, hostname: str, ca_cert: x509.Certificate, ca_key: ec.EllipticCurvePrivateKey
) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    cert_path = tmp / f"{hostname}-cert.pem"
    key_path = tmp / f"{hostname}-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _real_pem(tmp: Path) -> Path | None:
    """A PEM file containing at least one certificate, or None."""
    if HAVE_CRYPTOGRAPHY:
        return _mint_ca(tmp, "probe-ca")[0]
    for candidate in (
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
    ):
        if os.path.isfile(candidate):
            return Path(candidate)
    return None


class TrustTestCase(unittest.TestCase):
    """Base class providing a temp dir and a guaranteed-clean environment."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def env(self, **overrides: str) -> dict[str, str]:
        """An environment mapping with every CA variable cleared by default."""
        base = {name: "" for name in tls_trust.CA_BUNDLE_ENV_VARS}
        base.update(overrides)
        return base

    def a_pem(self) -> Path:
        pem = _real_pem(self.tmp)
        if pem is None:
            self.skipTest("no certificate source available to build a valid bundle")
        return pem


# --------------------------------------------------------------------------- #
# resolve_ca_bundle
# --------------------------------------------------------------------------- #
class TestResolve(TrustTestCase):
    def test_returns_none_when_nothing_configured(self) -> None:
        self.assertEqual(resolve_ca_bundle(self.env()), (None, None))

    def test_sam_ca_bundle_wins(self) -> None:
        env = self.env(
            SAM_CA_BUNDLE="/sam.pem",
            REQUESTS_CA_BUNDLE="/requests.pem",
            SSL_CERT_FILE="/ssl.pem",
        )
        self.assertEqual(resolve_ca_bundle(env), ("/sam.pem", "SAM_CA_BUNDLE"))

    def test_requests_bundle_beats_ssl_cert_file(self) -> None:
        env = self.env(REQUESTS_CA_BUNDLE="/requests.pem", SSL_CERT_FILE="/ssl.pem")
        self.assertEqual(resolve_ca_bundle(env), ("/requests.pem", "REQUESTS_CA_BUNDLE"))

    def test_ssl_cert_file_is_the_last_resort(self) -> None:
        env = self.env(SSL_CERT_FILE="/ssl.pem")
        self.assertEqual(resolve_ca_bundle(env), ("/ssl.pem", "SSL_CERT_FILE"))

    def test_blank_and_whitespace_values_are_unset(self) -> None:
        """An exported-but-empty variable is a shell/.env artefact, not config."""
        env = self.env(SAM_CA_BUNDLE="   ", REQUESTS_CA_BUNDLE="", SSL_CERT_FILE="/ssl.pem")
        self.assertEqual(resolve_ca_bundle(env), ("/ssl.pem", "SSL_CERT_FILE"))

    def test_surrounding_whitespace_is_stripped(self) -> None:
        env = self.env(SAM_CA_BUNDLE="  /sam.pem  ")
        self.assertEqual(resolve_ca_bundle(env), ("/sam.pem", "SAM_CA_BUNDLE"))


# --------------------------------------------------------------------------- #
# validate_ca_bundle
# --------------------------------------------------------------------------- #
class TestValidate(TrustTestCase):
    def test_missing_path_names_the_source_variable(self) -> None:
        missing = self.tmp / "nope.pem"
        with self.assertRaises(CaBundleError) as caught:
            validate_ca_bundle(str(missing), "SAM_CA_BUNDLE")
        message = str(caught.exception)
        self.assertIn("SAM_CA_BUNDLE", message)
        self.assertIn("does not exist", message)

    def test_empty_file_is_rejected(self) -> None:
        """The case a plain existence check misses: readable but trusts nothing."""
        empty = self.tmp / "empty.pem"
        empty.write_text("")
        with self.assertRaises(CaBundleError) as caught:
            validate_ca_bundle(str(empty), "SSL_CERT_FILE")
        # OpenSSL rejects a certificate-free file at load time; the branch that
        # counts certificates catches the subtler cases below. Either way the
        # operator needs the variable and the path, so assert on those.
        message = str(caught.exception)
        self.assertIn("SSL_CERT_FILE", message)
        self.assertIn("empty.pem", message)

    @needs_crypto
    def test_crl_only_bundle_is_rejected(self) -> None:
        """Loads cleanly, trusts nothing - the cert-count branch's whole point."""
        key = ec.generate_private_key(ec.SECP256R1())
        issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "crl-issuer")])
        now = datetime.now(timezone.utc)
        crl = (
            x509.CertificateRevocationListBuilder()
            .issuer_name(issuer)
            .last_update(now - timedelta(days=1))
            .next_update(now + timedelta(days=30))
            .sign(key, hashes.SHA256())
        )
        path = self.tmp / "only-crl.pem"
        path.write_bytes(crl.public_bytes(serialization.Encoding.PEM))

        with self.assertRaises(CaBundleError) as caught:
            validate_ca_bundle(str(path), "SAM_CA_BUNDLE")
        self.assertIn("no usable certificates", str(caught.exception))

    @needs_crypto
    def test_system_store_cannot_mask_an_empty_bundle(self) -> None:
        """Validation must judge the bundle alone, not the system trust store.

        Regression guard: validating through `create_default_context()` preloads
        the system CAs, so the certificate count never reaches zero and a bundle
        contributing nothing is waved through.
        """
        key = ec.generate_private_key(ec.SECP256R1())
        issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "masking-crl")])
        now = datetime.now(timezone.utc)
        crl = (
            x509.CertificateRevocationListBuilder()
            .issuer_name(issuer)
            .last_update(now - timedelta(days=1))
            .next_update(now + timedelta(days=30))
            .sign(key, hashes.SHA256())
        )
        path = self.tmp / "masking.pem"
        path.write_bytes(crl.public_bytes(serialization.Encoding.PEM))

        # The system store is populated here (that is what makes this a valid
        # regression test), yet validation must still reject the bundle.
        self.assertTrue(ssl.create_default_context().get_ca_certs())
        with self.assertRaises(CaBundleError):
            validate_ca_bundle(str(path), "SAM_CA_BUNDLE")

    def test_non_certificate_content_is_rejected(self) -> None:
        junk = self.tmp / "junk.pem"
        junk.write_text("this is not a certificate\n")
        with self.assertRaises(CaBundleError):
            validate_ca_bundle(str(junk), "SSL_CERT_FILE")

    def test_truncated_pem_is_rejected(self) -> None:
        broken = self.tmp / "broken.pem"
        broken.write_text("-----BEGIN CERTIFICATE-----\nZm9vYmFy\n-----END CERTIFICATE-----\n")
        with self.assertRaises(CaBundleError):
            validate_ca_bundle(str(broken), "SAM_CA_BUNDLE")

    def test_valid_bundle_passes(self) -> None:
        validate_ca_bundle(str(self.a_pem()), "SAM_CA_BUNDLE")

    def test_directory_is_accepted_as_capath(self) -> None:
        directory = self.tmp / "certs"
        directory.mkdir()
        validate_ca_bundle(str(directory), "SSL_CERT_DIR")


# --------------------------------------------------------------------------- #
# build_ssl_context
# --------------------------------------------------------------------------- #
class TestBuildContext(TrustTestCase):
    def test_none_when_unconfigured(self) -> None:
        self.assertIsNone(build_ssl_context(self.env()))

    def test_context_always_verifies(self) -> None:
        """The module must never hand back a permissive context."""
        context = build_ssl_context(self.env(SAM_CA_BUNDLE=str(self.a_pem())))
        assert context is not None
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_bad_bundle_raises_rather_than_falling_back(self) -> None:
        """Silently reverting to the default store is the bug being prevented."""
        with self.assertRaises(CaBundleError):
            build_ssl_context(self.env(SAM_CA_BUNDLE=str(self.tmp / "absent.pem")))

    @needs_crypto
    def test_context_trusts_only_the_configured_ca(self) -> None:
        ours, _, _ = _mint_ca(self.tmp, "ours")
        context = build_ssl_context(self.env(SAM_CA_BUNDLE=str(ours)))
        assert context is not None
        # Exactly the configured CA - not the system store, and nothing else.
        loaded = {c["subject"][0][0][1] for c in context.get_ca_certs()}
        self.assertEqual(loaded, {"ours"})


# --------------------------------------------------------------------------- #
# apply_snowflake_ca_env
# --------------------------------------------------------------------------- #
class TestSnowflakeEnv(TrustTestCase):
    def test_noop_when_unconfigured(self) -> None:
        env = self.env()
        self.assertEqual(apply_snowflake_ca_env(env), ())

    def test_copies_sam_bundle_to_the_names_the_connector_reads(self) -> None:
        """SAM_CA_BUNDLE alone is invisible to the connector until copied."""
        pem = str(self.a_pem())
        env = self.env(SAM_CA_BUNDLE=pem)
        applied = apply_snowflake_ca_env(env)
        self.assertEqual(set(applied), {"REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"})
        self.assertEqual(env["REQUESTS_CA_BUNDLE"], pem)
        self.assertEqual(env["SSL_CERT_FILE"], pem)

    def test_is_idempotent(self) -> None:
        pem = str(self.a_pem())
        env = self.env(SAM_CA_BUNDLE=pem)
        apply_snowflake_ca_env(env)
        self.assertEqual(apply_snowflake_ca_env(env), ())

    def test_only_sets_what_is_missing(self) -> None:
        pem = str(self.a_pem())
        env = self.env(SAM_CA_BUNDLE=pem, REQUESTS_CA_BUNDLE=pem)
        self.assertEqual(apply_snowflake_ca_env(env), ("SSL_CERT_FILE",))

    def test_bad_bundle_raises_before_connecting(self) -> None:
        env = self.env(SAM_CA_BUNDLE=str(self.tmp / "absent.pem"))
        with self.assertRaises(CaBundleError):
            apply_snowflake_ca_env(env)
        # The bad value must not be propagated to the connector's variables.
        self.assertEqual(env["REQUESTS_CA_BUNDLE"], "")


# --------------------------------------------------------------------------- #
# describe_trust
# --------------------------------------------------------------------------- #
class TestDescribeTrust(TrustTestCase):
    def test_reports_the_default_store(self) -> None:
        self.assertIn("default", describe_trust(self.env()))

    def test_reports_a_valid_bundle(self) -> None:
        pem = str(self.a_pem())
        summary = describe_trust(self.env(SAM_CA_BUNDLE=pem))
        self.assertIn("status=ok", summary)
        self.assertIn("SAM_CA_BUNDLE", summary)

    def test_reports_an_invalid_bundle_without_raising(self) -> None:
        """A health check must be able to report breakage, not crash on it."""
        summary = describe_trust(self.env(SAM_CA_BUNDLE=str(self.tmp / "absent.pem")))
        self.assertIn("INVALID", summary)


# --------------------------------------------------------------------------- #
# anthropic_http_client
# --------------------------------------------------------------------------- #
class TestAnthropicClient(TrustTestCase):
    def test_none_when_unconfigured(self) -> None:
        self.assertIsNone(anthropic_http_client(self.env()))

    def test_builds_a_client_for_a_valid_bundle(self) -> None:
        try:
            import httpx  # noqa: F401
        except ImportError:
            self.skipTest("httpx is not installed")
        client = anthropic_http_client(self.env(SAM_CA_BUNDLE=str(self.a_pem())))
        self.assertIsNotNone(client)
        client.close()

    def test_bad_bundle_raises(self) -> None:
        with self.assertRaises(CaBundleError):
            anthropic_http_client(self.env(SAM_CA_BUNDLE=str(self.tmp / "absent.pem")))


# --------------------------------------------------------------------------- #
# End-to-end handshake
# --------------------------------------------------------------------------- #
class TestHandshake(TrustTestCase):
    """Prove the context built from a bundle actually validates a live peer."""

    def _serve(self, cert: Path, key: Path) -> int:
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)

        def accept() -> None:
            try:
                raw, _ = listener.accept()
            except OSError:
                return
            try:
                with server_ctx.wrap_socket(raw, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass  # The client rejecting us is the expected path in one test.
            finally:
                raw.close()

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        # Joining with a timeout keeps a failed handshake from hanging the suite.
        self.addCleanup(thread.join, 5.0)
        return listener.getsockname()[1]

    @needs_crypto
    def test_matching_ca_verifies(self) -> None:
        ca_pem, ca_cert, ca_key = _mint_ca(self.tmp, "handshake-ca")
        cert, key = _mint_leaf(self.tmp, "localhost", ca_cert, ca_key)
        port = self._serve(cert, key)

        context = build_ssl_context(self.env(SAM_CA_BUNDLE=str(ca_pem)))
        assert context is not None
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as tls:
                self.assertEqual(tls.getpeercert()["subject"][0][0][1], "localhost")

    @needs_crypto
    def test_unrelated_ca_is_rejected(self) -> None:
        """The interception case: a cert from a CA we were not told to trust."""
        _, ca_cert, ca_key = _mint_ca(self.tmp, "server-ca")
        other_pem, _, _ = _mint_ca(self.tmp, "unrelated-ca")
        cert, key = _mint_leaf(self.tmp, "localhost", ca_cert, ca_key)
        port = self._serve(cert, key)

        context = build_ssl_context(self.env(SAM_CA_BUNDLE=str(other_pem)))
        assert context is not None
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with self.assertRaises(ssl.SSLCertVerificationError):
                context.wrap_socket(raw, server_hostname="localhost")

    @needs_crypto
    def test_hostname_mismatch_is_rejected(self) -> None:
        """A trusted CA is not enough; the name must match too."""
        ca_pem, ca_cert, ca_key = _mint_ca(self.tmp, "mismatch-ca")
        cert, key = _mint_leaf(self.tmp, "localhost", ca_cert, ca_key)
        port = self._serve(cert, key)

        context = build_ssl_context(self.env(SAM_CA_BUNDLE=str(ca_pem)))
        assert context is not None
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with self.assertRaises(ssl.SSLCertVerificationError):
                context.wrap_socket(raw, server_hostname="other.example")


# --------------------------------------------------------------------------- #
# Invariant
# --------------------------------------------------------------------------- #
class TestNoInsecureEscapeHatch(unittest.TestCase):
    def test_module_cannot_disable_verification(self) -> None:
        """Guards the module's central promise against a future 'quick fix'."""
        import ast

        tree = ast.parse(Path(tls_trust.__file__).read_text())
        # Strip the module docstring: it *describes* the absent escape hatches,
        # so scanning it would match the very strings being prohibited.
        body = tree.body[1:] if ast.get_docstring(tree) else tree.body
        code = "\n".join(ast.unparse(node) for node in body)
        for forbidden in ("CERT_NONE", "verify=False", "check_hostname = False"):
            self.assertNotIn(
                forbidden, code, f"tls_trust must never contain {forbidden!r}"
            )


if __name__ == "__main__":
    unittest.main()
