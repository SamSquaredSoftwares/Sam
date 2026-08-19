# samsquaredsoftwares.com — edge architecture

Target architecture:

```
browser ──► Cloudflare (valid wildcard cert) ──► GitHub Pages (origin)
                                                  └─ 301 www → apex
```

Audited 2026-08-17; **primary defect fixed and verified 2026-08-19**. Re-check any
time with:

```bash
./scripts/check_domain_health.sh          # exits non-zero on any failure
./scripts/check_domain_health.sh --quiet  # failures + summary only
```

## Status: the architecture matches the diagram end to end

Everything from the browser to Cloudflare was correct from the start. The break was one
layer deeper — the custom domain was bound to the wrong GitHub Pages site — and was
fixed on 2026-08-19 (see *Remediation — executed* below).

### Working

| Layer | Verified state |
| --- | --- |
| Authoritative DNS | Cloudflare (`harlan.ns`, `nelci.ns`) |
| Apex proxying | `samsquaredsoftwares.com` → `172.67.215.104`, `104.21.61.215` (Cloudflare anycast) |
| `www` proxying | `www.samsquaredsoftwares.com` → same Cloudflare anycast IPs |
| Wildcard certificate | `CN=samsquaredsoftwares.com`, SAN `samsquaredsoftwares.com` + `*.samsquaredsoftwares.com`, issued by Google Trust Services (`WE1`) — Cloudflare Universal SSL, valid to 2026-11-04 |
| `301 www → apex` | `https://www…` → `301` → `https://samsquaredsoftwares.com/`, single hop, settles `200` |
| HTTP → HTTPS | `http://samsquaredsoftwares.com` → `301` → HTTPS |
| Origin type | GitHub Pages (Fastly `via`/`x-github-request-id` headers present) |

The wildcard cert covers `www`, so there is no certificate problem on either hostname.

> A stale local DNS cache will resolve `www` straight to GitHub's `185.199.108–111.153`
> and present GitHub's `*.github.io` certificate, which looks exactly like a
> mis-issued-certificate outage. It is an artifact of the resolver, not the zone. Always
> confirm against a public resolver — `check_domain_health.sh` queries `1.1.1.1` for
> precisely this reason.

### Fixed 2026-08-19 — the custom domain was bound to the wrong Pages site

GitHub binds a custom domain to **exactly one** Pages site. Until 2026-08-19 that
binding belonged to a one-page placeholder repo, not the real website:

| | Holds `samsquaredsoftwares.com` | Contents | Last commit |
| --- | --- | --- | --- |
| `samgarib-debug/samsquaredsoftwares-site` | **yes** (`cname: samsquaredsoftwares.com`) | `CNAME`, `index.html` only | 2026-08-09 |
| `SamSquaredSoftwares/SamSquaredSoftwares.github.io` | **no** (`cname: null`) | full 8-page site + `styles.css`, `script.js`, `logo.png` | 2026-08-12 |

Consequences while the defect was live, all reproduced against the domain at the time:
the apex served the 2026-08-09 placeholder, every deep link 404'd (the same 404s
reproduced against the GitHub origin IPs directly, so Cloudflare was never the cause),
and the `CNAME` file committed to the org repo on 2026-08-12 was inert — GitHub
silently refuses a domain already claimed elsewhere.

## Remediation — executed 2026-08-19

Order mattered: the domain had to be released before it could be re-bound, and the
release was deferred until org-repo settings access was confirmed, because releasing
without the ability to re-bind would have turned a stale-content bug into a full outage.

1. **Released the claim** on `samgarib-debug/samsquaredsoftwares-site` via
   `PUT /repos/{owner}/{repo}/pages` with `cname: null` (that account's token has admin
   there).
2. **Bound the domain to the real site** in
   `SamSquaredSoftwares/SamSquaredSoftwares.github.io` → Settings → Pages (browser
   session with org access; the API token had no write access to the org repo). GitHub
   reported **"DNS check successful"** and the site URL flipped to
   `https://samsquaredsoftwares.com/`. Outage window: under four minutes.
3. **No cache purge was needed** — the zone serves HTML with `cf-cache-status: DYNAMIC`,
   and content flipped immediately.

Verified after the fix: all eight `MUST_SERVE` paths return `200`, the apex serves the
real site (`<title>SAM² — Smarter Softwares. Squared.</title>`), and following
redirects, the apex and `samsquaredsoftwares.github.io` serve byte-identical content
(the `github.io` host now 301s to the apex, as expected once a custom domain binds).

### TLS posture — permanent, by design of this architecture

GitHub reports *Enforce HTTPS unavailable* for the custom domain. This is **expected
and permanent** while Cloudflare proxies the domain: GitHub's pre-issuance DNS check
sees Cloudflare's anycast IPs rather than GitHub's own, so it will never provision an
apex certificate. The consequences:

- Client-facing HTTPS is enforced at the **Cloudflare edge** (wildcard cert +
  HTTP→HTTPS redirect), not at GitHub. This matches the diagram.
- Cloudflare SSL/TLS mode must stay on **Full** (encrypted, origin name not
  validated). **Full (strict) is permanently incompatible** with this architecture —
  the origin can only ever present `*.github.io` — and enabling it would take the site
  down. Do not "upgrade" it during Cloudflare housekeeping.
- If the domain is ever un-proxied (grey-clouded) to plain GitHub Pages, revisit:
  GitHub could then issue its own certificate and *Enforce HTTPS* should be enabled.

```bash
./scripts/check_domain_health.sh   # all content and ownership checks now pass
```

## Secondary findings

- **Duplicate SPF record — observed during the audit, since resolved.** At the start of
  this audit the apex carried two SPF records:

  ```
  v=spf1 ip4:197.185.157.101 include:samsquaredsoftwares.co.za ~all
  v=spf1 ~all
  ```

  RFC 7208 §4.5 permits exactly one; two yield a `permerror`, so SPF evaluation fails
  outright and receivers may reject or quarantine mail from the domain. Re-checking
  against both `1.1.1.1` and `8.8.8.8` later in the same session showed only the specific
  record remaining, so this is no longer live — the stray `v=spf1 ~all` was either removed
  mid-audit or was a propagating intermediate state. Recorded here because the health
  check retains an SPF-count assertion to catch the regression if it recurs.

- **The `www → apex` 301 is generated at the GitHub origin, not the Cloudflare edge.**
  The redirect response carries `x-github-request-id` and `cf-cache-status: DYNAMIC`, so
  every `www` hit pays a full origin round-trip just to be redirected. A Cloudflare
  Redirect Rule (`www.samsquaredsoftwares.com/*` → `https://samsquaredsoftwares.com/$1`,
  301) serves it at the edge and keeps working if the origin is down. This is also what
  the diagram implies by placing the redirect at the Cloudflare layer.

- **HSTS advertises `preload` but the domain is not on the preload list.** The header is
  `max-age=15552000; includeSubDomains; preload`; hstspreload.org reports no entry, so
  the directive is inert. Either submit the domain or drop the token to avoid implying a
  guarantee that is not in place. Note that `includeSubDomains` means any future
  subdomain must be HTTPS-capable — the wildcard cert covers proxied subdomains, but a
  DNS-only subdomain would hard-fail with a non-bypassable error.

- **No CAA record.** Any public CA may currently issue for the domain. A CAA record
  restricts issuance, but must include every CA Cloudflare Universal SSL may use for the
  edge certificate (Google Trust Services today; Cloudflare also issues via Let's
  Encrypt and SSL.com depending on rotation — include all three, or manage CAA through
  Cloudflare's own dashboard, which maintains the correct set automatically). A wrong
  CAA record silently breaks certificate renewal.

- **Domain not verified for the GitHub org.** `protected_domain_state` is `null`. GitHub's
  verified-domains feature prevents another account from claiming the domain — which is
  exactly the failure mode that produced the primary bug above, and nothing stops it
  recurring. Now that the domain is bound to the org repo, verify it under
  Organization Settings → Verified and approved domains (requires adding a DNS TXT
  record in Cloudflare).
