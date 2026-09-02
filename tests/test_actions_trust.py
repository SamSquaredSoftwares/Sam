"""Tests for how the actions consume `tls_trust`.

These cover the wiring rather than the trust logic itself (see
`test_tls_trust.py` for that): a `CaBundleError` must surface as the
`ActionError` the Action Server knows how to report, and a bad bundle must not
be handed on to a client. They need `sema4ai-actions`, so they skip on an
install that has only the standard library.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ACTIONS_DIR = Path(__file__).resolve().parents[1] / "actions"
sys.path.insert(0, str(ACTIONS_DIR))

try:
    from sema4ai.actions import ActionError

    import actions as actions_module
    import snowflake_actions

    HAVE_SEMA4AI = True
except ImportError:  # pragma: no cover - minimal installs only
    HAVE_SEMA4AI = False

BAD_BUNDLE = "/nonexistent/definitely-not-a-bundle.pem"

# Every CA variable is cleared so an ambient one cannot mask the bad value.
CLEAN_ENV = {
    "SAM_CA_BUNDLE": "",
    "REQUESTS_CA_BUNDLE": "",
    "SSL_CERT_FILE": "",
}


@unittest.skipUnless(HAVE_SEMA4AI, "sema4ai-actions is required")
class TestActionWiring(unittest.TestCase):
    def test_health_check_reports_the_trust_configuration(self) -> None:
        with mock.patch.dict("os.environ", CLEAN_ENV, clear=False):
            self.assertIn("ca_bundle=", actions_module.health_check())

    def test_health_check_flags_a_bad_bundle_without_raising(self) -> None:
        """The smoke test must be able to *report* misconfiguration."""
        env = {**CLEAN_ENV, "SAM_CA_BUNDLE": BAD_BUNDLE}
        with mock.patch.dict("os.environ", env, clear=False):
            self.assertIn("INVALID", actions_module.health_check())

    def test_ask_claude_raises_action_error_for_a_bad_bundle(self) -> None:
        """A CaBundleError must be translated, not leaked to the server."""
        env = {**CLEAN_ENV, "SAM_CA_BUNDLE": BAD_BUNDLE, "ANTHROPIC_API_KEY": "dummy"}
        with mock.patch.dict("os.environ", env, clear=False):
            with self.assertRaises(ActionError) as caught:
                actions_module.ask_claude("hello", None)
        self.assertIn("SAM_CA_BUNDLE", str(caught.exception))

    def test_snowflake_connect_raises_action_error_for_a_bad_bundle(self) -> None:
        env = {
            **CLEAN_ENV,
            "SAM_CA_BUNDLE": BAD_BUNDLE,
            "SNOWFLAKE_ACCOUNT": "acct",
            "SNOWFLAKE_USER": "usr",
            "SNOWFLAKE_PASSWORD": "pw",
        }
        with mock.patch.dict("os.environ", env, clear=False):
            with self.assertRaises(ActionError) as caught:
                snowflake_actions._connect("acct", "usr", "pw")
            message = str(caught.exception)
            # It must fail on the bundle, before any connection is attempted.
            self.assertIn("SAM_CA_BUNDLE", message)
            self.assertNotIn("Could not connect", message)

    def test_a_bad_bundle_is_never_exported_to_the_connector(self) -> None:
        """A rejected path must not be left behind for the connector to read."""
        env = {
            **CLEAN_ENV,
            "SAM_CA_BUNDLE": BAD_BUNDLE,
            "SNOWFLAKE_ACCOUNT": "acct",
            "SNOWFLAKE_USER": "usr",
            "SNOWFLAKE_PASSWORD": "pw",
        }
        with mock.patch.dict("os.environ", env, clear=False):
            with self.assertRaises(ActionError):
                snowflake_actions._connect("acct", "usr", "pw")
            import os

            self.assertNotEqual(os.environ.get("REQUESTS_CA_BUNDLE"), BAD_BUNDLE)
            self.assertNotEqual(os.environ.get("SSL_CERT_FILE"), BAD_BUNDLE)


@unittest.skipUnless(HAVE_SEMA4AI, "sema4ai-actions is required")
class TestAnthropicTimeoutPreserved(unittest.TestCase):
    """The trust override must not shorten the SDK's request timeout.

    A bare `httpx.Client` carries a 5s timeout, against the SDK's 600s. The SDK
    only substitutes its own when the supplied client's timeout is still httpx's
    default, so setting one here would silently truncate long generations.
    """

    def test_effective_timeout_matches_the_sdk_default(self) -> None:
        try:
            import anthropic
            from anthropic._constants import DEFAULT_TIMEOUT
        except ImportError:
            self.skipTest("anthropic is not installed")

        import tls_trust

        pem = "/etc/ssl/certs/ca-certificates.crt"
        if not Path(pem).is_file():
            self.skipTest("no system CA bundle to point at")

        client = tls_trust.anthropic_http_client({"SAM_CA_BUNDLE": pem})
        self.assertIsNotNone(client)
        try:
            configured = anthropic.Anthropic(api_key="k", http_client=client)
            self.assertEqual(configured.timeout, DEFAULT_TIMEOUT)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
