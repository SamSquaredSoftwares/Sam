"""Actions served by this Sema4.ai Action Package.

Every function decorated with `@action` is discovered by the Action Server and
exposed both as an OpenAPI endpoint and as an MCP tool.

Outbound HTTPS honours a custom CA bundle via `SAM_CA_BUNDLE`,
`REQUESTS_CA_BUNDLE` or `SSL_CERT_FILE` - see `tls_trust` for why that is
needed behind a TLS-inspecting proxy.
"""

import os
import platform
import sys

from sema4ai.actions import ActionError, Secret, action

from tls_trust import CaBundleError, anthropic_http_client, describe_trust

DEFAULT_MODEL = "claude-sonnet-5"


@action
def health_check() -> str:
    """Report the runtime this action package is running on.

    Useful as a smoke test: it needs no credentials and no network access, so a
    successful call proves the Action Server found the package and can execute
    its actions.

    Returns:
        A human readable summary of the Python and library versions in use.
    """
    try:
        from sema4ai.actions import version_info

        actions_version = ".".join(str(part) for part in version_info)
    except Exception:  # pragma: no cover - defensive, version_info is optional
        actions_version = "unknown"

    return (
        f"python={platform.python_version()} "
        f"executable={sys.executable} "
        f"platform={platform.platform()} "
        f"sema4ai-actions={actions_version} "
        f"{describe_trust()}"
    )


@action
def ask_claude(prompt: str, api_key: Secret, model: str = DEFAULT_MODEL) -> str:
    """Send a single prompt to Claude and return the text of the reply.

    Args:
        prompt: The question or instruction to send to the model.
        api_key: Anthropic API key. The Action Server passes it out of band, so
            it never appears in the request body. When it is not supplied the
            `ANTHROPIC_API_KEY` environment variable is used instead.
        model: The model id to call.

    Returns:
        The text content of Claude's reply.
    """
    key = _resolve_api_key(api_key)
    if not key:
        raise ActionError(
            "No Anthropic API key available. Pass one as the `api_key` secret "
            "or set the ANTHROPIC_API_KEY environment variable."
        )

    import anthropic

    # A configured bundle replaces httpx's default trust store. When none is
    # configured `http_client` is None and the SDK builds its own client.
    try:
        http_client = anthropic_http_client()
    except CaBundleError as e:
        raise ActionError(str(e))

    client = anthropic.Anthropic(api_key=key, http_client=http_client)
    try:
        message = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIStatusError as e:
        raise ActionError(f"Anthropic API error ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError as e:
        raise ActionError(f"Could not reach the Anthropic API: {e}")

    texts = [block.text for block in message.content if block.type == "text"]
    if not texts:
        raise ActionError(
            f"Claude returned no text content (stop_reason={message.stop_reason})."
        )

    return "\n".join(texts)


def _resolve_api_key(api_key: Secret) -> str:
    """Return the secret value, falling back to the environment."""
    try:
        value = api_key.value
    except Exception:
        value = ""

    return value or os.environ.get("ANTHROPIC_API_KEY", "")
