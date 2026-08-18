"""Tests for `scripts/check_domain_health.py`.

Everything here runs offline. The certificate checks are genuine TLS handshakes
against a local server holding purpose-built certificates - a private CA is
generated, trusted via `SSL_CERT_FILE`, and used to issue leaves that are valid,
hostname-mismatched, expiring, expired, or untrusted. That exercises the code
path the 17 Aug 2026 incident actually hit instead of mocking it away.

Run with:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_domain_health as cdh  # noqa: E402

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    HAVE_CRYPTOGRAPHY = True
except BaseException:  # pragma: no cover - exercised only on minimal installs
    # Deliberately broad: a `cryptography` wheel whose native bindings are
    # missing or mismatched does not raise ImportError. It can raise a Rust
    # `pyo3_runtime.PanicException`, which inherits from BaseException, and that
    # must degrade to a skip rather than break collection of the whole suite.
    HAVE_CRYPTOGRAPHY = False


# --------------------------------------------------------------------------- #
# Pure logic
# --------------------------------------------------------------------------- #


class TestHostMatchesName(unittest.TestCase):
    def test_exact_match_is_case_and_dot_insensitive(self):
        self.assertTrue(cdh.host_matches_name("Example.COM", "example.com"))
        self.assertTrue(cdh.host_matches_name("example.com.", "example.com"))
        self.assertTrue(cdh.host_matches_name("www.example.com", "WWW.example.com"))

    def test_wildcard_matches_exactly_one_label(self):
        self.assertTrue(cdh.host_matches_name("www.example.com", "*.example.com"))
        self.assertTrue(cdh.host_matches_name("staging.example.com", "*.example.com"))

    def test_wildcard_does_not_match_apex(self):
        """The incident in one line: an apex-only cert must not satisfy www, and
        a wildcard must not silently satisfy the apex."""
        self.assertFalse(cdh.host_matches_name("example.com", "*.example.com"))

    def test_wildcard_does_not_span_dots(self):
        self.assertFalse(cdh.host_matches_name("a.b.example.com", "*.example.com"))

    def test_apex_cert_does_not_cover_www(self):
        self.assertFalse(cdh.host_matches_name("www.example.com", "example.com"))

    def test_rejects_malformed_input(self):
        self.assertFalse(cdh.host_matches_name("", "example.com"))
        self.assertFalse(cdh.host_matches_name("www.example.com", ""))
        self.assertFalse(cdh.host_matches_name("www.example.com", "*."))
        self.assertFalse(cdh.host_matches_name("www.example.com", "*.*.com"))

    def test_bare_wildcard_label_is_not_a_match(self):
        self.assertFalse(cdh.host_matches_name(".example.com", "*.example.com"))


class TestClassifyIP(unittest.TestCase):
    def test_github_pages_addresses_are_flagged_as_origin(self):
        for address in ("185.199.108.153", "185.199.109.153", "185.199.111.153"):
            provider, exposed = cdh.classify_ip(address)
            self.assertEqual(provider, "github-pages", address)
            self.assertTrue(exposed, address)

    def test_github_pages_ipv6_is_flagged(self):
        provider, exposed = cdh.classify_ip("2606:50c0:8000::153")
        self.assertEqual(provider, "github-pages")
        self.assertTrue(exposed)

    def test_cloudflare_addresses_from_the_incident_are_recognised(self):
        for address in (
            "104.21.61.215",
            "172.67.215.104",
            "2606:4700:3034::6815:3dd7",
            "2606:4700:3035::ac43:d768",
        ):
            provider, exposed = cdh.classify_ip(address)
            self.assertEqual(provider, "cloudflare", address)
            self.assertFalse(exposed, address)

    def test_unknown_and_invalid_addresses(self):
        self.assertEqual(cdh.classify_ip("203.0.113.7"), ("unknown", False))
        self.assertEqual(cdh.classify_ip("not-an-ip"), ("invalid", False))

    def test_ipv4_is_not_matched_against_ipv6_networks(self):
        # A v4 address must never be tested for membership in a v6 network.
        provider, _ = cdh.classify_ip("10.0.0.1")
        self.assertEqual(provider, "unknown")


class TestParseCertDatetime(unittest.TestCase):
    def test_parses_openssl_format(self):
        parsed = cdh.parse_cert_datetime("Nov  4 23:59:59 2026 GMT")
        self.assertEqual(
            parsed, datetime(2026, 11, 4, 23, 59, 59, tzinfo=timezone.utc)
        )

    def test_parses_double_digit_day(self):
        parsed = cdh.parse_cert_datetime("Aug 17 00:00:00 2026 GMT")
        self.assertEqual(parsed, datetime(2026, 8, 17, tzinfo=timezone.utc))

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            cdh.parse_cert_datetime("whenever")


class TestCertNames(unittest.TestCase):
    def test_prefers_subject_alt_names(self):
        cert = {
            "subject": ((("commonName", "example.com"),),),
            "subjectAltName": (("DNS", "example.com"), ("DNS", "*.example.com")),
        }
        self.assertEqual(cdh.cert_names(cert), ["example.com", "*.example.com"])

    def test_ignores_non_dns_sans(self):
        cert = {"subjectAltName": (("DNS", "example.com"), ("IP Address", "1.2.3.4"))}
        self.assertEqual(cdh.cert_names(cert), ["example.com"])

    def test_falls_back_to_common_name_without_sans(self):
        cert = {"subject": ((("commonName", "legacy.example.com"),),)}
        self.assertEqual(cdh.cert_names(cert), ["legacy.example.com"])

    def test_empty_cert_yields_no_names(self):
        self.assertEqual(cdh.cert_names({}), [])


# --------------------------------------------------------------------------- #
# DNS
# --------------------------------------------------------------------------- #


class TestCheckDNS(unittest.TestCase):
    def _run(self, addresses, expect_proxied=True):
        report = cdh.HostReport("www.example.com")
        with mock.patch.object(cdh, "resolve_host", return_value=addresses):
            cdh.check_dns(report, expect_proxied=expect_proxied)
        return report

    def _finding(self, report, check):
        return next((f for f in report.findings if f.check == check), None)

    def test_cloudflare_only_answer_passes(self):
        report = self._run(["104.21.61.215", "172.67.215.104"])
        self.assertEqual(report.level, cdh.OK)
        self.assertIsNone(self._finding(report, "dns.origin_exposed"))

    def test_stale_github_pages_answer_fails(self):
        report = self._run(["185.199.108.153"])
        self.assertEqual(report.level, cdh.FAIL)
        finding = self._finding(report, "dns.origin_exposed")
        self.assertIsNotNone(finding)
        self.assertIn("bypass the proxy", finding.message)

    def test_mixed_answer_is_reported_as_failure_and_mix(self):
        report = self._run(["104.21.61.215", "185.199.110.153"])
        self.assertEqual(report.level, cdh.FAIL)
        self.assertIsNotNone(self._finding(report, "dns.mixed"))
        self.assertIsNotNone(self._finding(report, "dns.origin_exposed"))

    def test_origin_addresses_only_warn_when_proxying_not_expected(self):
        report = self._run(["185.199.108.153"], expect_proxied=False)
        self.assertEqual(report.level, cdh.WARN)

    def test_resolution_failure_is_a_failure(self):
        report = cdh.HostReport("www.example.com")
        with mock.patch.object(
            cdh, "resolve_host", side_effect=socket.gaierror("NXDOMAIN")
        ):
            cdh.check_dns(report, expect_proxied=True)
        self.assertEqual(report.level, cdh.FAIL)

    def test_empty_answer_is_a_failure(self):
        self.assertEqual(self._run([]).level, cdh.FAIL)


# --------------------------------------------------------------------------- #
# Redirects and HSTS
# --------------------------------------------------------------------------- #


class TestFollowRedirects(unittest.TestCase):
    def test_walks_www_to_apex(self):
        responses = {
            "https://www.example.com/": (301, {"location": "https://example.com/"}),
            "https://example.com/": (200, {}),
        }
        chain = cdh.follow_redirects(
            "https://www.example.com/", lambda url: responses[url]
        )
        self.assertEqual([hop["status"] for hop in chain], [301, 200])
        self.assertEqual(chain[-1]["url"], "https://example.com/")

    def test_resolves_relative_location_against_current_url(self):
        responses = {
            "http://example.com/": (301, {"location": "/here"}),
            "http://example.com/here": (200, {}),
        }
        chain = cdh.follow_redirects("http://example.com/", lambda url: responses[url])
        self.assertEqual(chain[-1]["url"], "http://example.com/here")

    def test_scheme_relative_location_keeps_scheme(self):
        responses = {
            "https://www.example.com/": (301, {"location": "//example.com/"}),
            "https://example.com/": (200, {}),
        }
        chain = cdh.follow_redirects(
            "https://www.example.com/", lambda url: responses[url]
        )
        self.assertEqual(chain[-1]["url"], "https://example.com/")

    def test_redirect_without_location_terminates(self):
        chain = cdh.follow_redirects("https://example.com/", lambda url: (301, {}))
        self.assertEqual(len(chain), 1)

    def test_redirect_loop_is_reported(self):
        chain = cdh.follow_redirects(
            "https://example.com/",
            lambda url: (301, {"location": "https://example.com/"}),
            max_hops=3,
        )
        self.assertEqual(chain[-1].get("error"), "too many redirects")


class TestCheckHTTP(unittest.TestCase):
    def _run(self, host, responses, scheme="https", expect_first_status=None):
        report = cdh.HostReport(host)
        with mock.patch.object(
            cdh, "http_request", side_effect=lambda url, timeout=None: responses[url]
        ):
            cdh.check_http(
                report,
                scheme=scheme,
                expect_final_host="example.com",
                expect_final_scheme="https",
                expect_first_status=expect_first_status,
                timeout=1.0,
            )
        return report

    def test_healthy_www_redirect_passes(self):
        report = self._run(
            "www.example.com",
            {
                "https://www.example.com/": (
                    301,
                    {
                        "location": "https://example.com/",
                        "strict-transport-security": "max-age=15552000; includeSubDomains; preload",
                    },
                ),
                "https://example.com/": (200, {}),
            },
            expect_first_status=301,
        )
        self.assertEqual(report.level, cdh.OK)

    def test_chain_ending_on_wrong_host_fails(self):
        report = self._run(
            "www.example.com",
            {
                "https://www.example.com/": (
                    301,
                    {
                        "location": "https://elsewhere.test/",
                        "strict-transport-security": "max-age=1",
                    },
                ),
                "https://elsewhere.test/": (200, {}),
            },
        )
        self.assertEqual(report.level, cdh.FAIL)

    def test_http_scheme_must_upgrade_to_https(self):
        report = self._run(
            "example.com",
            {"http://example.com/": (200, {})},
            scheme="http",
        )
        self.assertEqual(report.level, cdh.FAIL)

    def test_server_error_on_final_hop_fails(self):
        report = self._run(
            "example.com",
            {"https://example.com/": (503, {"strict-transport-security": "max-age=1"})},
        )
        self.assertEqual(report.level, cdh.FAIL)

    def test_unexpected_first_status_warns(self):
        report = self._run(
            "example.com",
            {"https://example.com/": (200, {"strict-transport-security": "max-age=1"})},
            expect_first_status=301,
        )
        self.assertEqual(report.level, cdh.WARN)

    def test_missing_hsts_warns(self):
        report = self._run("example.com", {"https://example.com/": (200, {})})
        self.assertEqual(report.level, cdh.WARN)
        self.assertTrue(any(f.check == "hsts" for f in report.findings))

    def test_include_subdomains_is_surfaced(self):
        report = self._run(
            "example.com",
            {
                "https://example.com/": (
                    200,
                    {
                        "strict-transport-security": "max-age=15552000; includeSubDomains; preload"
                    },
                )
            },
        )
        hsts = next(f for f in report.findings if f.check == "hsts")
        self.assertTrue(hsts.detail["include_subdomains"])
        self.assertIn("un-bypassable", hsts.message)

    def test_connection_error_is_a_failure(self):
        report = cdh.HostReport("example.com")
        with mock.patch.object(cdh, "http_request", side_effect=OSError("refused")):
            cdh.check_http(
                report,
                scheme="https",
                expect_final_host="example.com",
                expect_final_scheme="https",
                expect_first_status=200,
                timeout=1.0,
            )
        self.assertEqual(report.level, cdh.FAIL)


# --------------------------------------------------------------------------- #
# Real TLS handshakes against a local server
# --------------------------------------------------------------------------- #


class _TLSServer:
    """Minimal TLS listener that completes handshakes and closes.

    `fetch_cert` may reconnect up to three times as it relaxes verification, so
    the listener stays up serving connections until stopped.
    """

    def __init__(self, certfile: str, keyfile: str) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile, keyfile)
        self._context = context
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        # Wake out of accept() periodically so stop() does not have to wait for a
        # client: closing the listening socket cannot interrupt a thread that is
        # parked in recv() on an already-accepted connection.
        self._sock.settimeout(0.5)
        self.port = self._sock.getsockname()[1]
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:  # noqa: UP041
                # Not TimeoutError: the two were only unified in Python 3.10,
                # and this suite supports 3.9, where socket.timeout is a
                # distinct OSError subclass that TimeoutError would not catch.
                continue  # Idle tick; re-check _running.
            except OSError:
                return  # Listening socket closed by stop().
            # Bound every client interaction so a half-open connection cannot
            # keep this thread alive past stop().
            conn.settimeout(1.0)
            try:
                with self._context.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(1)
            except OSError:
                pass  # Client rejected the certificate; that is the test's point.
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def stop(self) -> None:
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=5)


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography is required to mint test certs")
class TestTLSAgainstLocalServer(unittest.TestCase):
    """End-to-end certificate checks using a private CA trusted for the test."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)

        cls.ca_key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "Domain Health Test CA")]
        )
        now = datetime.now(timezone.utc)
        cls.ca_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(cls.ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(cls.ca_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=False,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(cls.ca_key, hashes.SHA256())
        )

        cls.ca_bundle = cls.tmp / "ca.pem"
        cls.ca_bundle.write_bytes(
            cls.ca_cert.public_bytes(serialization.Encoding.PEM)
        )

        # Make the private CA the trust store for `create_default_context()`.
        cls._prev_cert_file = os.environ.get("SSL_CERT_FILE")
        os.environ["SSL_CERT_FILE"] = str(cls.ca_bundle)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._prev_cert_file is None:
            os.environ.pop("SSL_CERT_FILE", None)
        else:
            os.environ["SSL_CERT_FILE"] = cls._prev_cert_file
        cls._tmp.cleanup()

    def _leaf(
        self,
        name: str,
        sans: list[str],
        not_after_days: int = 90,
        self_signed: bool = False,
    ) -> tuple[str, str]:
        """Mint a leaf certificate and return its `(certfile, keyfile)` paths.

        The extension set is deliberately complete - key identifiers, basic
        constraints, key usage. Python 3.13 enables `ssl.VERIFY_X509_STRICT` in
        `create_default_context()`, which rejects a leaf with no Authority Key
        Identifier. Without these, every certificate here fails chain
        verification on 3.13 and the tests silently measure the wrong branch.
        Real CA-issued certificates carry all of this.
        """
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, sans[0])])
        now = datetime.now(timezone.utc)
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject if self_signed else self.ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=2))
            .not_valid_after(now + timedelta(days=not_after_days))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(san) for san in sans]),
                critical=False,
            )
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    key.public_key() if self_signed else self.ca_key.public_key()
                ),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
        )
        signer = key if self_signed else self.ca_key
        cert = builder.sign(signer, hashes.SHA256())

        certfile = self.tmp / f"{name}.crt"
        keyfile = self.tmp / f"{name}.key"
        certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        keyfile.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        return str(certfile), str(keyfile)

    def _serve(self, *args, **kwargs) -> _TLSServer:
        server = _TLSServer(*self._leaf(*args, **kwargs))
        self.addCleanup(server.stop)
        return server

    def test_matching_certificate_verifies(self):
        server = self._serve("valid", ["localhost"])
        result = cdh.fetch_cert("localhost", server.port, timeout=5)
        self.assertEqual(result["outcome"], "verified")
        self.assertIn("localhost", result["names"])

    def test_wildcard_certificate_verifies_for_subdomain(self):
        # `*.localtest.me` style coverage, checked through the matcher rather
        # than DNS so the test stays offline.
        server = self._serve("wildcard", ["*.example.com", "example.com"])
        result = cdh.fetch_cert("localhost", server.port, timeout=5)
        self.assertEqual(result["outcome"], "hostname_mismatch")
        self.assertTrue(cdh.host_matches_name("www.example.com", "*.example.com"))

    def test_apex_only_certificate_reports_hostname_mismatch(self):
        """The incident, reproduced: an apex-only cert served for a www request."""
        server = self._serve("apex-only", ["example.com"])
        result = cdh.fetch_cert("localhost", server.port, timeout=5)
        self.assertEqual(result["outcome"], "hostname_mismatch")
        self.assertEqual(result["names"], ["example.com"])

        report = cdh.HostReport("localhost")
        with mock.patch.object(cdh, "fetch_cert", return_value=result):
            cdh.check_tls(report, warn_days=21, timeout=5)
        self.assertEqual(report.level, cdh.FAIL)
        finding = next(f for f in report.findings if f.check == "tls.hostname")
        self.assertIn("ERR_CERT_COMMON_NAME_INVALID", finding.message)
        self.assertIn("example.com", finding.detail["covers"])

    def test_expired_certificate_is_rejected_at_the_chain(self):
        """An expired leaf fails chain verification, so it never reaches the
        expiry arithmetic - it surfaces as an untrusted chain. Either way the
        host is reported as a failure."""
        server = self._serve("expired", ["localhost"], not_after_days=-1)
        result = cdh.fetch_cert("localhost", server.port, timeout=5)
        self.assertEqual(result["outcome"], "untrusted")
        self.assertIn("expired", result["error"].lower())

        report = cdh.HostReport("localhost")
        with mock.patch.object(cdh, "fetch_cert", return_value=result):
            cdh.check_tls(report, warn_days=21, timeout=5)
        self.assertEqual(report.level, cdh.FAIL)

    def test_untrusted_chain_is_detected(self):
        server = self._serve("selfsigned", ["localhost"], self_signed=True)
        result = cdh.fetch_cert("localhost", server.port, timeout=5)
        self.assertEqual(result["outcome"], "untrusted")

        report = cdh.HostReport("localhost")
        with mock.patch.object(cdh, "fetch_cert", return_value=result):
            cdh.check_tls(report, warn_days=21, timeout=5)
        self.assertEqual(report.level, cdh.FAIL)

    def test_healthy_certificate_passes_check_tls(self):
        server = self._serve("healthy", ["localhost"], not_after_days=90)
        result = cdh.fetch_cert("localhost", server.port, timeout=5)
        self.assertEqual(result["outcome"], "verified")

        report = cdh.HostReport("localhost")
        with mock.patch.object(cdh, "fetch_cert", return_value=result):
            cdh.check_tls(report, warn_days=21, timeout=5)
        self.assertEqual(report.level, cdh.OK)

    def test_certificate_expiring_soon_warns(self):
        server = self._serve("expiring", ["localhost"], not_after_days=5)
        result = cdh.fetch_cert("localhost", server.port, timeout=5)
        self.assertEqual(result["outcome"], "verified")

        report = cdh.HostReport("localhost")
        with mock.patch.object(cdh, "fetch_cert", return_value=result):
            cdh.check_tls(report, warn_days=21, timeout=5)
        self.assertEqual(report.level, cdh.WARN)
        expiry = next(f for f in report.findings if f.check == "tls.expiry")
        self.assertLessEqual(expiry.detail["days_left"], 21)

    def test_unreachable_port_is_a_failure(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed_port = probe.getsockname()[1]
        result = cdh.fetch_cert("localhost", closed_port, timeout=3)
        self.assertEqual(result["outcome"], "unreachable")

        report = cdh.HostReport("localhost")
        with mock.patch.object(cdh, "fetch_cert", return_value=result):
            cdh.check_tls(report, warn_days=21, timeout=3)
        self.assertEqual(report.level, cdh.FAIL)


class TestExpiryReporting(unittest.TestCase):
    """Covers the expiry arithmetic directly.

    A verified chain cannot carry an expired leaf, so the `days_left < 0` branch
    is not reachable through a real handshake - it is defensive, and tested here
    against a synthesised result.
    """

    def _run(self, not_after, warn_days=21):
        result = {
            "outcome": "verified",
            "error": "",
            "cert": {},
            "names": ["localhost"],
            "issuer": "Test CA",
            "not_after": not_after,
            "protocol": "TLSv1.3",
            "cipher": "",
        }
        report = cdh.HostReport("localhost")
        with mock.patch.object(cdh, "fetch_cert", return_value=result):
            cdh.check_tls(report, warn_days=warn_days, timeout=1)
        return report

    def test_expired_certificate_reports_days_since_expiry(self):
        report = self._run("Jan  1 00:00:00 2020 GMT")
        self.assertEqual(report.level, cdh.FAIL)
        expiry = next(f for f in report.findings if f.check == "tls.expiry")
        self.assertIn("expired", expiry.message)
        self.assertLess(expiry.detail["days_left"], 0)

    def test_unparseable_not_after_warns_rather_than_crashing(self):
        report = self._run("not a date")
        self.assertEqual(report.level, cdh.WARN)

    def test_missing_not_after_is_not_reported(self):
        report = self._run("")
        self.assertFalse(any(f.check == "tls.expiry" for f in report.findings))


class TestCheckIssuer(unittest.TestCase):
    """The issuer guard exists because a TLS-inspecting proxy otherwise makes
    every certificate check pass against an intercepted certificate."""

    def _run(self, issuer, expected):
        report = cdh.HostReport("www.example.com")
        cdh.check_issuer(report, issuer, expected)
        return report

    def test_no_expectation_records_nothing(self):
        report = self._run("Google Trust Services", ())
        self.assertEqual(report.findings, [])

    def test_matching_issuer_passes(self):
        report = self._run("Google Trust Services", ["Google Trust Services"])
        self.assertEqual(report.level, cdh.OK)

    def test_match_is_case_insensitive_and_partial(self):
        report = self._run("Google Trust Services LLC", ["google trust"])
        self.assertEqual(report.level, cdh.OK)

    def test_any_of_several_expectations_is_enough(self):
        report = self._run("Let's Encrypt", ["Google Trust Services", "Let's Encrypt"])
        self.assertEqual(report.level, cdh.OK)

    def test_intercepting_proxy_issuer_fails(self):
        # The exact false pass observed while developing this script: a sandbox
        # proxy re-signed the connection with its own trusted CA.
        report = self._run("Anthropic", ["Google Trust Services"])
        self.assertEqual(report.level, cdh.FAIL)
        finding = report.findings[0]
        self.assertIn("intercepted", finding.message)
        self.assertEqual(finding.detail["issuer"], "Anthropic")

    def test_empty_issuer_fails_when_expectation_set(self):
        self.assertEqual(self._run("", ["Google Trust Services"]).level, cdh.FAIL)

    def test_check_tls_surfaces_issuer_mismatch(self):
        result = {
            "outcome": "verified",
            "error": "",
            "cert": {},
            "names": ["www.example.com"],
            "issuer": "Anthropic",
            "not_after": "",
            "protocol": "TLSv1.3",
            "cipher": "",
        }
        report = cdh.HostReport("www.example.com")
        with mock.patch.object(cdh, "fetch_cert", return_value=result):
            cdh.check_tls(
                report,
                warn_days=21,
                timeout=1,
                expect_issuer=["Google Trust Services"],
            )
        self.assertEqual(report.level, cdh.FAIL)
        self.assertTrue(any(f.check == "tls.issuer" for f in report.findings))


# --------------------------------------------------------------------------- #
# Reporting and CLI
# --------------------------------------------------------------------------- #


class TestReporting(unittest.TestCase):
    def _report(self, host, levels):
        report = cdh.HostReport(host)
        for index, level in enumerate(levels):
            report.add(f"check{index}", level, "message")
        return report

    def test_host_level_is_the_worst_finding(self):
        self.assertEqual(self._report("h", [cdh.OK, cdh.OK]).level, cdh.OK)
        self.assertEqual(self._report("h", [cdh.OK, cdh.WARN]).level, cdh.WARN)
        self.assertEqual(self._report("h", [cdh.WARN, cdh.FAIL, cdh.OK]).level, cdh.FAIL)

    def test_empty_report_is_ok(self):
        self.assertEqual(cdh.HostReport("h").level, cdh.OK)

    def test_overall_level_spans_hosts(self):
        reports = [self._report("apex", [cdh.OK]), self._report("www", [cdh.FAIL])]
        self.assertEqual(cdh.overall_level(reports), cdh.FAIL)

    def test_exit_codes_map_to_severity(self):
        self.assertEqual(cdh._EXIT_CODES[cdh.OK], 0)
        self.assertEqual(cdh._EXIT_CODES[cdh.WARN], 1)
        self.assertEqual(cdh._EXIT_CODES[cdh.FAIL], 2)

    def test_json_output_is_valid_and_complete(self):
        import json

        reports = [self._report("apex", [cdh.OK]), self._report("www", [cdh.FAIL])]
        payload = json.loads(cdh.render_json(reports))
        self.assertEqual(payload["overall"], cdh.FAIL)
        self.assertEqual([h["host"] for h in payload["hosts"]], ["apex", "www"])
        self.assertEqual(payload["hosts"][1]["findings"][0]["level"], cdh.FAIL)

    def test_text_output_mentions_each_host_and_verdict(self):
        text = cdh.render_text(
            [self._report("apex", [cdh.OK]), self._report("www", [cdh.FAIL])]
        )
        self.assertIn("apex", text)
        self.assertIn("www", text)
        self.assertIn("Overall: FAIL", text)


class TestCLI(unittest.TestCase):
    def test_www_prefix_is_stripped_from_apex_argument(self):
        captured = {}

        def fake_check(report, **kwargs):
            captured.setdefault("hosts", []).append(report.host)

        with mock.patch.object(cdh, "check_dns", side_effect=fake_check), mock.patch.object(
            cdh, "check_tls", lambda *a, **k: None
        ):
            cdh.check_domain("WWW.Example.com.", skip_http=True)

        self.assertEqual(captured["hosts"], ["example.com", "www.example.com"])

    def test_exit_code_reflects_failure(self):
        def fail_dns(report, **kwargs):
            report.add("dns", cdh.FAIL, "boom")

        with mock.patch.object(cdh, "check_dns", side_effect=fail_dns), mock.patch.object(
            cdh, "check_tls", lambda *a, **k: None
        ), mock.patch("builtins.print"):
            self.assertEqual(cdh.main(["--apex", "example.com", "--skip-http"]), 2)

    def test_exit_code_zero_when_healthy(self):
        with mock.patch.object(cdh, "check_dns", lambda *a, **k: None), mock.patch.object(
            cdh, "check_tls", lambda *a, **k: None
        ), mock.patch("builtins.print"):
            self.assertEqual(cdh.main(["--apex", "example.com", "--skip-http"]), 0)

    def test_json_flag_emits_json(self):
        with mock.patch.object(cdh, "check_dns", lambda *a, **k: None), mock.patch.object(
            cdh, "check_tls", lambda *a, **k: None
        ), mock.patch("builtins.print") as printer:
            cdh.main(["--apex", "example.com", "--skip-http", "--json"])
        import json

        json.loads(printer.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
