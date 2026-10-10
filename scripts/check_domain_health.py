#!/usr/bin/env python3
"""Assert that a domain's apex and `www` hostnames are both serving valid HTTPS.

Written after the 17 Aug 2026 `NET::ERR_CERT_COMMON_NAME_INVALID` incident on
`www.samsquaredsoftwares.com`. That outage was invisible from the apex: the apex
served a valid certificate and a 200 the whole time, while `www` handed some
resolvers an apex-only origin certificate. Because the site sends HSTS with
`includeSubDomains`, visitors got an error page they could not click through.

This script encodes the incident's verification checklist so the same failure is
caught by a machine instead of a customer. For every hostname it checks:

* DNS       - which A/AAAA records the resolver returns, and whether they belong
              to the CDN or to the raw origin (a request that reaches the origin
              directly is what produced the bad certificate).
* TLS       - the chain verifies against the system trust store *and* the
              certificate actually covers the hostname requested via SNI, with
              wildcard matching applied the way browsers apply it.
* Expiry    - days remaining, warning before the certificate lapses.
* HTTP      - the redirect chain ends where it should (`www` -> apex, http ->
              https) with the expected status codes.
* HSTS      - `Strict-Transport-Security` is present, and `includeSubDomains` is
              reported because it raises the blast radius of any future
              subdomain certificate mistake.

Stdlib only, so it runs anywhere Python 3.9+ does - a laptop, a cron box, or a
CI job - with no install step.

Caveat worth knowing: on a network that performs TLS inspection (a corporate
proxy, or a sandboxed CI runner) the certificate this script sees is the
*interceptor's*, re-signed by a CA the machine already trusts. Every check then
passes while telling you nothing about the real origin. Pass `--expect-issuer`
to defend against that - the check fails if the issuer is not who you expect,
which turns a silent false pass into a visible error.

Exit codes make it usable as a monitor:

    0   every check passed
    1   warnings only (e.g. certificate expiring soon)
    2   at least one failure (bad certificate, wrong redirect, origin exposure)

Usage:

    ./scripts/check_domain_health.py
    ./scripts/check_domain_health.py --apex example.com
    ./scripts/check_domain_health.py --json
    ./scripts/check_domain_health.py --warn-days 30
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import socket
import ssl
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

DEFAULT_APEX = "samsquaredsoftwares.com"
DEFAULT_WARN_DAYS = 21
DEFAULT_TIMEOUT = 10.0
MAX_REDIRECT_HOPS = 5

OK = "ok"
WARN = "warn"
FAIL = "fail"

_SEVERITY_ORDER = {OK: 0, WARN: 1, FAIL: 2}
_EXIT_CODES = {OK: 0, WARN: 1, FAIL: 2}

# Networks we want to name in the output. `expected` marks the ranges a proxied
# hostname should resolve to; `origin_exposed` marks ranges that mean the
# request skipped the proxy and hit the origin directly, which is the specific
# condition that served the wrong certificate during the incident.
KNOWN_NETWORKS: tuple[tuple[str, str, bool], ...] = (
    # GitHub Pages origin - reaching this directly is the failure mode.
    ("github-pages", "185.199.108.0/22", True),
    ("github-pages", "2606:50c0::/32", True),
    # Cloudflare edge - the expected answer for a proxied record.
    ("cloudflare", "173.245.48.0/20", False),
    ("cloudflare", "103.21.244.0/22", False),
    ("cloudflare", "103.22.200.0/22", False),
    ("cloudflare", "103.31.4.0/22", False),
    ("cloudflare", "141.101.64.0/18", False),
    ("cloudflare", "108.162.192.0/18", False),
    ("cloudflare", "190.93.240.0/20", False),
    ("cloudflare", "188.114.96.0/20", False),
    ("cloudflare", "197.234.240.0/22", False),
    ("cloudflare", "198.41.128.0/17", False),
    ("cloudflare", "162.158.0.0/15", False),
    ("cloudflare", "104.16.0.0/13", False),
    ("cloudflare", "104.24.0.0/14", False),
    ("cloudflare", "172.64.0.0/13", False),
    ("cloudflare", "131.0.72.0/22", False),
    ("cloudflare", "2400:cb00::/32", False),
    ("cloudflare", "2606:4700::/32", False),
    ("cloudflare", "2803:f800::/32", False),
    ("cloudflare", "2405:b500::/32", False),
    ("cloudflare", "2405:8100::/32", False),
    ("cloudflare", "2a06:98c0::/29", False),
    ("cloudflare", "2c0f:f248::/32", False),
)


@dataclass
class Finding:
    """A single assertion result, rendered as one line of output."""

    check: str
    level: str
    message: str
    detail: dict = field(default_factory=dict)


@dataclass
class HostReport:
    """All findings for one hostname."""

    host: str
    findings: list[Finding] = field(default_factory=list)

    def add(self, check: str, level: str, message: str, **detail) -> None:
        self.findings.append(Finding(check, level, message, detail))

    @property
    def level(self) -> str:
        """The worst severity recorded for this host."""
        return max(
            (f.level for f in self.findings),
            key=lambda lvl: _SEVERITY_ORDER[lvl],
            default=OK,
        )


# --------------------------------------------------------------------------- #
# DNS
# --------------------------------------------------------------------------- #


def resolve_host(host: str, port: int = 443) -> list[str]:
    """Return the unique IP addresses the local resolver gives for `host`.

    Deliberately uses the machine's own resolver rather than querying an
    authoritative nameserver: the question this script answers is "what does
    *this* client get", which is exactly where the incident lived.
    """
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    seen: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.append(address)
    return seen


def classify_ip(address: str) -> tuple[str, bool]:
    """Map an IP to a `(provider, origin_exposed)` pair.

    Unrecognised addresses come back as `("unknown", False)` - unknown is not
    treated as a failure on its own, because a perfectly healthy site may sit
    behind a CDN this table does not list.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "invalid", False

    for provider, cidr, origin_exposed in KNOWN_NETWORKS:
        network = ipaddress.ip_network(cidr)
        if ip.version == network.version and ip in network:
            return provider, origin_exposed
    return "unknown", False


def check_dns(report: HostReport, expect_proxied: bool) -> None:
    """Record which providers a hostname resolves to."""
    try:
        addresses = resolve_host(report.host)
    except socket.gaierror as exc:
        report.add("dns", FAIL, f"DNS resolution failed: {exc}")
        return

    if not addresses:
        report.add("dns", FAIL, "DNS returned no addresses")
        return

    classified = {address: classify_ip(address) for address in addresses}
    providers = sorted({provider for provider, _ in classified.values()})
    exposed = [
        address for address, (_, origin_exposed) in classified.items() if origin_exposed
    ]

    report.add(
        "dns",
        OK,
        f"resolves to {len(addresses)} address(es) [{', '.join(providers)}]",
        addresses=addresses,
        providers=providers,
    )

    if exposed and expect_proxied:
        report.add(
            "dns.origin_exposed",
            FAIL,
            "resolver returns origin addresses, so requests bypass the proxy and "
            f"will be served the origin's certificate: {', '.join(sorted(exposed))}",
            addresses=sorted(exposed),
        )
    elif exposed:
        report.add(
            "dns.origin_exposed",
            WARN,
            f"resolves directly to origin addresses: {', '.join(sorted(exposed))}",
            addresses=sorted(exposed),
        )

    # A split answer (some edge, some origin) is the intermittent form of this
    # bug and is easy to miss by eye, so call it out explicitly.
    if len(providers) > 1:
        report.add(
            "dns.mixed",
            WARN,
            f"answer mixes providers ({', '.join(providers)}); a stray DNS-only "
            "record is the usual cause",
            providers=providers,
        )


# --------------------------------------------------------------------------- #
# TLS
# --------------------------------------------------------------------------- #


def host_matches_name(host: str, name: str) -> bool:
    """Return True if `host` matches certificate name `name`.

    Implements the wildcard rule browsers use: a leading `*` matches exactly one
    label, and only the leftmost label. `*.example.com` therefore covers
    `www.example.com` but neither `example.com` nor `a.b.example.com`.
    """
    host = host.lower().rstrip(".")
    name = name.lower().rstrip(".")
    if not host or not name:
        return False
    if not name.startswith("*."):
        return host == name

    suffix = name[2:]
    if not suffix or "*" in suffix:
        return False
    if not host.endswith("." + suffix):
        return False
    label = host[: -(len(suffix) + 1)]
    return bool(label) and "." not in label


def cert_names(cert: dict) -> list[str]:
    """Collect every DNS name a certificate presents, SANs first.

    The subject CN is included only as a fallback for certificates with no SAN
    extension; modern verifiers ignore CN when SANs are present.
    """
    names: list[str] = [
        value for kind, value in cert.get("subjectAltName", ()) if kind.lower() == "dns"
    ]
    if not names:
        for rdn in cert.get("subject", ()):
            for key, value in rdn:
                if key == "commonName":
                    names.append(value)
    return names


def _rdn_value(rdn_sequence: Iterable, key: str) -> str:
    for rdn in rdn_sequence:
        for name, value in rdn:
            if name == key:
                return value
    return ""


def parse_cert_datetime(value: str) -> datetime:
    """Parse an OpenSSL `notBefore`/`notAfter` string into an aware datetime."""
    text = value.strip()
    suffix = ""
    if text.endswith(" GMT"):
        text, suffix = text[:-4], "GMT"
    parsed = datetime.strptime(text, "%b %d %H:%M:%S %Y")
    if suffix == "GMT":
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.replace(tzinfo=timezone.utc)


def fetch_cert(host: str, port: int = 443, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Retrieve the certificate `host` serves for its own SNI name.

    Handshakes are attempted in decreasing strictness so a failure still yields
    a useful diagnosis rather than just "handshake failed":

    1. Verify chain *and* hostname. Success means the host is healthy.
    2. Verify chain only. Success means the chain is trusted but the certificate
       does not cover this hostname - i.e. exactly the browser's
       `ERR_CERT_COMMON_NAME_INVALID`. The certificate is readable here, so we
       can report which names it *does* cover.
    3. Verify nothing. The chain itself is untrusted; report what was served.
    """
    attempts = (
        ("verified", True, ssl.CERT_REQUIRED),
        ("hostname_mismatch", False, ssl.CERT_REQUIRED),
        ("untrusted", False, ssl.CERT_NONE),
    )

    last_error = ""
    for outcome, check_hostname, verify_mode in attempts:
        context = ssl.create_default_context()
        context.check_hostname = check_hostname
        context.verify_mode = verify_mode
        try:
            with socket.create_connection(
                (host, port), timeout=timeout
            ) as raw, context.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                return {
                    "outcome": outcome,
                    "error": last_error,
                    "cert": cert,
                    "names": cert_names(cert),
                    "subject_cn": _rdn_value(cert.get("subject", ()), "commonName"),
                    "issuer": _rdn_value(cert.get("issuer", ()), "organizationName")
                    or _rdn_value(cert.get("issuer", ()), "commonName"),
                    "not_after": cert.get("notAfter", ""),
                    "protocol": tls.version() or "",
                    "cipher": (tls.cipher() or ("",))[0],
                }
        except ssl.SSLError as exc:
            last_error = str(exc)
        except ssl.CertificateError as exc:
            last_error = str(exc)
        except OSError as exc:
            # Connection-level problems will not improve by relaxing verification.
            return {"outcome": "unreachable", "error": str(exc), "cert": {}, "names": []}

    return {"outcome": "handshake_failed", "error": last_error, "cert": {}, "names": []}


def check_issuer(report: HostReport, issuer: str, expect_issuer: Sequence[str]) -> None:
    """Fail if the certificate issuer is not one the caller expects.

    Guards against TLS-inspecting middleboxes: an interceptor presents a
    perfectly valid certificate signed by a CA the machine trusts, so every
    other check passes while describing the proxy rather than the origin.
    """
    if not expect_issuer:
        return
    haystack = issuer.lower()
    if any(expected.lower() in haystack for expected in expect_issuer):
        report.add("tls.issuer", OK, f"issuer {issuer!r} matches expectations")
    else:
        report.add(
            "tls.issuer",
            FAIL,
            f"issuer {issuer!r} does not match any of "
            f"{', '.join(repr(e) for e in expect_issuer)} - the connection may be "
            "intercepted by a TLS-inspecting proxy, so the other TLS results "
            "describe the interceptor and not the real origin",
            issuer=issuer,
            expected=list(expect_issuer),
        )


def check_tls(
    report: HostReport,
    warn_days: int,
    timeout: float,
    expect_issuer: Sequence[str] = (),
) -> None:
    """Verify the certificate served for a hostname covers that hostname."""
    result = fetch_cert(report.host, timeout=timeout)
    outcome = result["outcome"]
    names = result.get("names", [])

    if outcome == "unreachable":
        report.add("tls", FAIL, f"could not connect on 443: {result['error']}")
        return
    if outcome == "handshake_failed":
        report.add("tls", FAIL, f"TLS handshake failed: {result['error']}")
        return

    if outcome == "hostname_mismatch":
        report.add(
            "tls.hostname",
            FAIL,
            f"certificate does not cover {report.host} - browsers will show "
            f"ERR_CERT_COMMON_NAME_INVALID. Certificate covers: "
            f"{', '.join(names) or 'unknown'}",
            covers=names,
            issuer=result.get("issuer", ""),
            error=result["error"],
        )
    elif outcome == "untrusted":
        report.add(
            "tls.chain",
            FAIL,
            f"certificate chain is not trusted: {result['error']}",
        )
        return
    else:
        matched = [name for name in names if host_matches_name(report.host, name)]
        report.add(
            "tls",
            OK,
            f"valid certificate from {result.get('issuer') or 'unknown issuer'} "
            f"({result.get('protocol', '')}), matched {matched[0] if matched else 'host'}",
            covers=names,
            issuer=result.get("issuer", ""),
            protocol=result.get("protocol", ""),
            cipher=result.get("cipher", ""),
        )

    check_issuer(report, result.get("issuer", ""), expect_issuer)

    not_after = result.get("not_after") or ""
    if not not_after:
        return
    try:
        expiry = parse_cert_datetime(not_after)
    except ValueError:
        report.add("tls.expiry", WARN, f"could not parse notAfter {not_after!r}")
        return

    days_left = (expiry - datetime.now(timezone.utc)).days
    if days_left < 0:
        level, note = FAIL, f"certificate expired {abs(days_left)} day(s) ago"
    elif days_left <= warn_days:
        level, note = WARN, f"certificate expires in {days_left} day(s)"
    else:
        level, note = OK, f"certificate valid for {days_left} more day(s)"
    report.add(
        "tls.expiry",
        level,
        f"{note} (notAfter {not_after})",
        days_left=days_left,
        not_after=not_after,
    )


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def http_request(url: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, dict]:
    """Issue a single HEAD request and return `(status, headers)` without following."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    if parsed.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            host, parsed.port or 443, timeout=timeout, context=ssl.create_default_context()
        )
    else:
        conn = http.client.HTTPConnection(host, parsed.port or 80, timeout=timeout)

    try:
        conn.request("HEAD", path, headers={"Host": host, "User-Agent": "domain-health-check/1.0"})
        response = conn.getresponse()
        headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, headers
    finally:
        conn.close()


def follow_redirects(
    url: str,
    request: Callable[[str], tuple[int, dict]],
    max_hops: int = MAX_REDIRECT_HOPS,
) -> list[dict]:
    """Walk a redirect chain, returning one entry per hop.

    `request` is injected so the walk can be exercised without a network.
    Relative `Location` values are resolved against the current URL, matching
    what a browser does.
    """
    chain: list[dict] = []
    current = url
    for _ in range(max_hops + 1):
        status, headers = request(current)
        location = headers.get("location", "")
        chain.append({"url": current, "status": status, "headers": headers})
        if not (300 <= status < 400 and location):
            return chain

        target = urlparse(location)
        if not target.scheme:
            base = urlparse(current)
            target = target._replace(
                scheme=base.scheme, netloc=target.netloc or base.netloc
            )
        current = urlunparse(target)
    chain.append({"url": current, "status": 0, "headers": {}, "error": "too many redirects"})
    return chain


def check_http(
    report: HostReport,
    scheme: str,
    expect_final_host: str,
    expect_final_scheme: str,
    expect_first_status: int | None,
    timeout: float,
) -> None:
    """Verify a hostname's redirect chain lands where it is supposed to."""
    url = f"{scheme}://{report.host}/"
    label = f"http.{scheme}"
    try:
        chain = follow_redirects(url, lambda u: http_request(u, timeout=timeout))
    except (OSError, http.client.HTTPException) as exc:
        report.add(label, FAIL, f"{url} request failed: {exc}")
        return

    hops = " -> ".join(f"{hop['status']} {hop['url']}" for hop in chain)
    final = chain[-1]
    if final.get("error"):
        report.add(label, FAIL, f"{final['error']}: {hops}", chain=hops)
        return

    final_parsed = urlparse(final["url"])
    if final_parsed.hostname != expect_final_host or final_parsed.scheme != expect_final_scheme:
        report.add(
            label,
            FAIL,
            f"chain ends at {final['url']} but expected "
            f"{expect_final_scheme}://{expect_final_host}/ : {hops}",
            chain=hops,
        )
    elif final["status"] >= 400:
        report.add(label, FAIL, f"final response is {final['status']}: {hops}", chain=hops)
    else:
        report.add(label, OK, hops, chain=hops)

    if expect_first_status is not None and chain[0]["status"] != expect_first_status:
        report.add(
            f"{label}.status",
            WARN,
            f"first response was {chain[0]['status']}, expected {expect_first_status}",
            status=chain[0]["status"],
        )

    if scheme == "https":
        _check_hsts(report, chain)


def _check_hsts(report: HostReport, chain: Sequence[dict]) -> None:
    """Report on the HSTS header of the first HTTPS response in a chain."""
    hsts = chain[0]["headers"].get("strict-transport-security", "")
    if not hsts:
        report.add("hsts", WARN, "no Strict-Transport-Security header on the HTTPS response")
        return

    if "includesubdomains" in hsts.lower().replace(" ", ""):
        report.add(
            "hsts",
            OK,
            f"{hsts} - note includeSubDomains makes any future subdomain "
            "certificate error un-bypassable",
            header=hsts,
            include_subdomains=True,
        )
    else:
        report.add("hsts", OK, hsts, header=hsts, include_subdomains=False)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def check_domain(
    apex: str,
    warn_days: int = DEFAULT_WARN_DAYS,
    timeout: float = DEFAULT_TIMEOUT,
    skip_http: bool = False,
    expect_issuer: Sequence[str] = (),
) -> list[HostReport]:
    """Run every check against the apex and its `www` hostname."""
    apex = apex.strip().lower().rstrip(".").removeprefix("www.")
    www = f"www.{apex}"

    reports = []
    for host, expect_first_status in ((apex, 200), (www, 301)):
        report = HostReport(host)
        check_dns(report, expect_proxied=True)
        check_tls(
            report,
            warn_days=warn_days,
            timeout=timeout,
            expect_issuer=expect_issuer,
        )
        if not skip_http:
            check_http(
                report,
                scheme="https",
                expect_final_host=apex,
                expect_final_scheme="https",
                expect_first_status=expect_first_status,
                timeout=timeout,
            )
            check_http(
                report,
                scheme="http",
                expect_final_host=apex,
                expect_final_scheme="https",
                expect_first_status=301,
                timeout=timeout,
            )
        reports.append(report)
    return reports


def overall_level(reports: Iterable[HostReport]) -> str:
    return max(
        (report.level for report in reports),
        key=lambda lvl: _SEVERITY_ORDER[lvl],
        default=OK,
    )


_GLYPHS = {OK: "PASS", WARN: "WARN", FAIL: "FAIL"}


def render_text(reports: Sequence[HostReport]) -> str:
    lines: list[str] = []
    for report in reports:
        lines.append(f"\n{report.host}  [{_GLYPHS[report.level]}]")
        for finding in report.findings:
            lines.append(f"  {_GLYPHS[finding.level]:4}  {finding.check:22}  {finding.message}")
    lines.append(f"\nOverall: {_GLYPHS[overall_level(reports)]}")
    return "\n".join(lines)


def render_json(reports: Sequence[HostReport]) -> str:
    return json.dumps(
        {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "overall": overall_level(reports),
            "hosts": [
                {
                    "host": report.host,
                    "level": report.level,
                    "findings": [
                        {
                            "check": f.check,
                            "level": f.level,
                            "message": f.message,
                            "detail": f.detail,
                        }
                        for f in report.findings
                    ],
                }
                for report in reports
            ],
        },
        indent=2,
        sort_keys=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that a domain's apex and www hostnames serve valid HTTPS.",
    )
    parser.add_argument(
        "--apex",
        default=DEFAULT_APEX,
        help=f"apex domain to check, without www (default: {DEFAULT_APEX})",
    )
    parser.add_argument(
        "--warn-days",
        type=int,
        default=DEFAULT_WARN_DAYS,
        help=f"warn when a certificate expires within this many days (default: {DEFAULT_WARN_DAYS})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"per-connection timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--skip-http",
        action="store_true",
        help="only check DNS and TLS, skipping the redirect and HSTS checks",
    )
    parser.add_argument(
        "--expect-issuer",
        action="append",
        default=[],
        metavar="SUBSTRING",
        help="require the certificate issuer to contain this text (repeatable). "
        "Use it to catch TLS-inspecting proxies, which otherwise make every "
        "check pass against an intercepted certificate. "
        "Example: --expect-issuer 'Google Trust Services'",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    reports = check_domain(
        args.apex,
        warn_days=args.warn_days,
        timeout=args.timeout,
        skip_http=args.skip_http,
        expect_issuer=args.expect_issuer,
    )
    print(render_json(reports) if args.json else render_text(reports))
    return _EXIT_CODES[overall_level(reports)]


if __name__ == "__main__":
    sys.exit(main())
