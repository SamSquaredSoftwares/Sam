# Runbook: `www` certificate errors on samsquaredsoftwares.com

Written after the **17 Aug 2026** incident, when Chrome showed
`NET::ERR_CERT_COMMON_NAME_INVALID` on `https://www.samsquaredsoftwares.com/`
while the apex `https://samsquaredsoftwares.com/` loaded normally.

## How the domain is wired

```
browser ──► Cloudflare edge (wildcard cert) ──► GitHub Pages origin (apex-only cert)
                                                  └─ 301 www ──► apex
```

Two facts make this shape fragile, and both are confirmed in this repository's
sibling repo `SamSquaredSoftwares/SamSquaredSoftwares.github.io`:

1. Its `CNAME` file contains **`samsquaredsoftwares.com`** — the apex only.
   GitHub Pages therefore issues a certificate for the apex and redirects every
   other hostname to it. It cannot issue one covering `www`, because `www` is
   Cloudflare-proxied and GitHub can never complete domain validation for it.
2. Both hostnames send `Strict-Transport-Security` with `includeSubDomains`.
   The apex's HSTS policy is therefore enforced on `www` too, so any certificate
   problem on `www` is a hard failure with **no click-through option**.

Consequence: `www` is only healthy *because* the request lands on Cloudflare. Any
resolver that sends a visitor straight to the GitHub Pages origin gets the
apex-only certificate and an un-bypassable error page.

## Diagnosis

The certificate and the site were not broken. The affected machine was resolving
`www` to the GitHub Pages origin — a stale DNS entry cached locally, in a router,
or at an ISP resolver, from before `www` was proxied.

Confirmed independently on 17 Aug 2026: both `www` and the apex resolve to the
same Cloudflare addresses (`104.21.61.215`, `172.67.215.104`,
`2606:4700:3034::6815:3dd7`, `2606:4700:3035::ac43:d768`), with no GitHub Pages
records in the answer. The misrouting was client-side, not authoritative.

### Telling the two cases apart

```
nslookup www.samsquaredsoftwares.com
```

| Answer | Meaning | Action |
| --- | --- | --- |
| `104.21.61.215` / `172.67.215.104` | Resolver is correct | Problem is inside the browser — clear its host cache, restart it |
| `185.199.108–111.x` | **Stale GitHub Pages record** — the known cause | Reboot the router, or set DNS to `1.1.1.1` / `8.8.8.8` to bypass the ISP cache |
| Anything else | Local interception | Check the `hosts` file for a `samsquaredsoftwares` line |

## Client-side fix

1. Chrome keeps caches independent of the OS. In `chrome://net-internals/#dns`
   choose **Clear host cache**; in `#sockets` choose **Flush socket pools**; in
   `#hsts` delete the domain security policy for `www.samsquaredsoftwares.com`.
2. Flush the OS resolver — `ipconfig /flushdns` on Windows, or
   `sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder` on macOS.
3. Hard-reload. `www` should 301 to the apex.

## Durable fix (Cloudflare) — removes the dependency entirely

The client-side steps fix one machine. These steps make the failure impossible
for every visitor, by terminating `www` at Cloudflare's edge so GitHub's
apex-only certificate is never part of the `www` path.

1. **Audit DNS records.** Cloudflare → DNS → Records. `www` should have exactly
   one record, **Proxied (orange cloud)**. Delete any leftover record pointing at
   `185.199.108–111.x` or `*.github.io` — a stray DNS-only duplicate is the usual
   cause of the intermittent form of this error.
2. **Move the `www` → apex redirect to Cloudflare.** Rules → Redirect Rules:
   when `Hostname equals www.samsquaredsoftwares.com`, dynamic redirect to
   `concat("https://samsquaredsoftwares.com", http.request.uri.path)`, status
   `301`, preserve query string. Cloudflare then answers `www` itself using its
   valid wildcard certificate.
3. **Leave GitHub Pages on the apex.** Custom domain `samsquaredsoftwares.com`,
   Enforce HTTPS on. GitHub being unable to issue a `www` certificate is expected
   and harmless once step 2 is in place.
4. **Then** set SSL/TLS mode to **Full (strict)**. Do this *after* step 2:
   while `www` still proxies through to GitHub, strict validation of SNI `www`
   against GitHub's apex-only certificate can fail.

## Monitoring

The incident was invisible from the apex, which served a valid certificate and a
200 throughout. `scripts/check_domain_health.py` asserts the `www` path
specifically:

```bash
./scripts/check_domain_health.py --expect-issuer "Google Trust Services"
```

Exit code `0` = healthy, `1` = warnings (e.g. certificate expiring), `2` =
failure. Suitable for cron or CI; add `--json` for a machine-readable record.

It fails loudly on the exact conditions that caused this incident: DNS answers
that point at the origin instead of the edge, a certificate that does not cover
the hostname requested, a redirect chain that does not end at the apex, and a
missing HSTS header.

**Always pass `--expect-issuer` when running it somewhere that might inspect
TLS.** A corporate proxy or sandboxed CI runner re-signs connections with a CA
the machine already trusts, so without it every certificate check passes while
describing the proxy rather than the real origin. This was observed in practice
while developing the script.

## Open items

- **HSTS preload is a long commitment.** `max-age=15552000` (180 days) with
  `includeSubDomains; preload` means every current *and future* subdomain must
  serve valid HTTPS or become completely unreachable in Chrome with no override.
  Confirm whether the domain is actually submitted at `hstspreload.org` —
  removal takes months. Worth verifying before adding `staging`, `api`, or
  `docs` subdomains.
- **Certificate renewal.** Cloudflare Universal SSL auto-renews. The
  `--warn-days` threshold gives advance notice if it ever does not.
- **CAA records** would restrict which CAs may issue for the domain. Verify the
  complete list of CAs in use first — an incomplete CAA record blocks renewals.
