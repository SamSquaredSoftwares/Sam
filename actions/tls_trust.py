"""Shared TLS trust configuration for the Sam actions.

Both outbound clients in this package speak HTTPS: the Anthropic SDK (via
`httpx`) and the Snowflake connector (via `urllib3`). On a network that
re-terminates TLS - a corporate inspecting proxy, or a sandboxed CI runner -
neither of them trusts the interceptor's CA by default, so every call fails
with a certificate verification error until they are pointed at the right
bundle. The two clients disagree about how to be told, which is the whole
reason this module exists:

* `httpx` reads `SSL_CERT_FILE` and `SSL_CERT_DIR`. It ignores
  `REQUESTS_CA_BUNDLE` entirely.
* The Snowflake connector resolves its bundle as
  `ca_certs` kwarg -> `REQUESTS_CA_BUNDLE` -> `SSL_CERT_FILE`, and falls back
  to `certifi`. `ca_certs` is a socket-level argument, not a `connect()`
  parameter, so environment variables are the only supported route.

So `SSL_CERT_FILE` is the one name both honour, and `resolve_ca_bundle` accepts
either spelling plus a package-specific `SAM_CA_BUNDLE` that wins over both.

The second reason this module exists is failure behaviour. The Snowflake
connector loads the bundle inside a `try/except ... pass`, so a mistyped path
is silently ignored: verification quietly falls back to `certifi` and the
handshake then fails with an error that says nothing about the typo. Here a
configured-but-unusable bundle is a loud, specific error instead.

Nothing in this module can disable certificate verification. There is
deliberately no "insecure" or "verify=False" switch: the fix for an
interception proxy is to trust its CA, never to stop checking.

Configuration:

    SAM_CA_BUNDLE        PEM bundle (or directory) to trust, highest priority
    REQUESTS_CA_BUNDLE   honoured for compatibility with the wider ecosystem
    SSL_CERT_FILE        honoured, and the name both clients agree on

When none of these is set the module does nothing at all and each client keeps
its own default trust store.
"""

from __future__ import annotations

import os
import ssl
from collections.abc import Mapping, MutableMapping
from typing import Any

# Highest priority first. `SAM_CA_BUNDLE` lets an operator point this package at
# a bundle without perturbing every other tool that reads the standard names.
CA_BUNDLE_ENV_VARS: tuple[str, ...] = (
    "SAM_CA_BUNDLE",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
)

# The variables the Snowflake connector actually consults, in its own order.
_SNOWFLAKE_CA_ENV_VARS: tuple[str, ...] = ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE")


class CaBundleError(ValueError):
    """A CA bundle was configured but cannot be used.

    Deliberately a plain `ValueError` rather than a `sema4ai` `ActionError`, so
    this module stays importable - and testable - without the Action Server
    runtime. The action layer translates it.
    """


def resolve_ca_bundle(
    env: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Return the `(path, source_variable)` of the configured CA bundle.

    Returns `(None, None)` when no bundle is configured, which means "leave the
    client's default trust store alone". Values that are empty or whitespace
    are treated as unset, since an exported-but-blank variable is a common
    artefact of shell scripts and `.env` files.
    """
    environ = os.environ if env is None else env
    for name in CA_BUNDLE_ENV_VARS:
        value = (environ.get(name) or "").strip()
        if value:
            return value, name
    return None, None


def validate_ca_bundle(path: str, source: str | None = None) -> None:
    """Raise `CaBundleError` unless *path* is a usable trust source.

    Usable means OpenSSL can load it *and* it yields at least one certificate.
    That second condition is what catches the cases a plain existence check
    misses: an empty file, a Git LFS pointer, or a PEM whose base64 is
    truncated all exist and are readable while trusting nothing.
    """
    origin = f"{source}=" if source else ""
    if not os.path.exists(path):
        raise CaBundleError(
            f"CA bundle {origin}{path!r} does not exist. Point it at a PEM file "
            "(or a directory of hashed certificates), or unset the variable to "
            "use the default trust store."
        )

    # An empty context, not `create_default_context()`: the latter preloads the
    # system trust store, whose certificates would then be counted below and
    # mask a bundle that contributes none of its own.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        if os.path.isdir(path):
            context.load_verify_locations(capath=path)
        else:
            context.load_verify_locations(cafile=path)
    except (ssl.SSLError, OSError, ValueError) as exc:
        raise CaBundleError(
            f"CA bundle {origin}{path!r} could not be loaded: {exc}. It must be "
            "PEM encoded (a directory must contain hashed certificates)."
        ) from exc

    # A directory of hashed certs is loaded lazily by OpenSSL, so there is
    # nothing to count; only files can be checked eagerly.
    if not os.path.isdir(path) and not context.get_ca_certs():
        raise CaBundleError(
            f"CA bundle {origin}{path!r} contains no usable certificates. It "
            "loaded without error but yielded nothing to trust (a PEM holding "
            "only a CRL or a private key does this), so every TLS connection "
            "would still fail."
        )


def build_ssl_context(env: Mapping[str, str] | None = None) -> ssl.SSLContext | None:
    """Return a verifying `SSLContext` for the configured bundle, or `None`.

    `None` means no bundle is configured and the caller should keep its own
    default. The returned context always verifies: `check_hostname` and
    `CERT_REQUIRED` come from `create_default_context` and are never relaxed.
    """
    path, source = resolve_ca_bundle(env)
    if not path:
        return None

    validate_ca_bundle(path, source)

    if os.path.isdir(path):
        return ssl.create_default_context(capath=path)
    return ssl.create_default_context(cafile=path)


def anthropic_http_client(env: Mapping[str, str] | None = None) -> Any | None:
    """Return an `httpx.Client` trusting the configured bundle, or `None`.

    `None` means no bundle is configured, in which case the Anthropic SDK
    should build its own client. `trust_env` stays on so proxy variables keep
    working; only the trust store is being overridden here.

    The timeout is deliberately left at httpx's default. The SDK compares a
    supplied client's timeout against that default and, finding it unchanged,
    applies its own (600s read/write) instead - so trust can be configured
    without silently cutting long generations down to httpx's 5s. Setting a
    timeout here would override that, which is why this takes no timeout
    argument.
    """
    context = build_ssl_context(env)
    if context is None:
        return None

    import httpx

    return httpx.Client(verify=context, trust_env=True)


def apply_snowflake_ca_env(
    env: MutableMapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Export the configured bundle under the names the connector reads.

    The connector takes its bundle from the environment, so a bundle supplied
    only as `SAM_CA_BUNDLE` is invisible to it until it is copied across. The
    bundle is validated first, so a bad path fails here with a clear message
    rather than being silently discarded by the connector.

    Returns the names of the variables that were set (empty when no bundle is
    configured, or when they already carry the right value).
    """
    environ = os.environ if env is None else env
    path, source = resolve_ca_bundle(environ)
    if not path:
        return ()

    validate_ca_bundle(path, source)

    applied: list[str] = []
    for name in _SNOWFLAKE_CA_ENV_VARS:
        if (environ.get(name) or "").strip() != path:
            environ[name] = path
            applied.append(name)
    return tuple(applied)


def describe_trust(env: Mapping[str, str] | None = None) -> str:
    """Summarise the trust configuration for diagnostics.

    Never raises: this is meant to be safe to call from a health check, where a
    broken bundle is precisely what the operator is trying to see reported.
    """
    path, source = resolve_ca_bundle(env)
    if not path:
        return "ca_bundle=default(system/certifi)"

    try:
        validate_ca_bundle(path, source)
    except CaBundleError as exc:
        return f"ca_bundle={path} source={source} status=INVALID ({exc})"
    return f"ca_bundle={path} source={source} status=ok"
