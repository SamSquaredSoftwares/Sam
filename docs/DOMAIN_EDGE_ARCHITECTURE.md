# samsquaredsoftwares.com — edge architecture

Target architecture:

```
browser ──► Cloudflare (valid wildcard cert) ──► GitHub Pages (origin)
                                                  └─ 301 www → apex
```

Verified live on 2026-08-17. Re-check any time with:

```bash
./scripts/check_domain_health.sh          # exits non-zero on any failure
./scripts/check_domain_health.sh --quiet  # failures + summary only
```

## Status: the transport layer matches the diagram; the origin serves the wrong site

Everything from the browser to Cloudflare is correct. The break is one layer deeper —
Cloudflare is faithfully proxying to a GitHub Pages site that is **not** the real website.

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

### Broken — the custom domain is bound to the wrong Pages site

GitHub binds a custom domain to **exactly one** Pages site. That binding currently
belongs to a one-page placeholder repo, not the real website:

| | Holds `samsquaredsoftwares.com` | Contents | Last commit |
| --- | --- | --- | --- |
| `samgarib-debug/samsquaredsoftwares-site` | **yes** (`cname: samsquaredsoftwares.com`) | `CNAME`, `index.html` only | 2026-08-09 |
| `SamSquaredSoftwares/SamSquaredSoftwares.github.io` | **no** (`cname: null`) | full 8-page site + `styles.css`, `script.js`, `logo.png` | 2026-08-12 |

Consequences, all reproduced against the live domain:

- `https://samsquaredsoftwares.com/` serves the **2026-08-09 placeholder**
  (`<title>Sam Squared Softwares — SAMePOS</title>`), not the current site
  (`<title>SAM² — Smarter Softwares. Squared.</title>`).
- Every deep link 404s: `/about.html`, `/contact.html`, `/how-it-works.html`,
  `/pricing.html`, `/product.html`, `/styles.css`, `/script.js`. Those files exist only
  in the org repo, which the domain is not bound to.
- The identical 404s occur when querying the GitHub origin IPs directly, so **Cloudflare
  is not the cause** and no amount of cache purging will fix it.
- The `CNAME` file committed to the org repo on 2026-08-12 ("Add CNAME for custom
  domain") is **inert** — GitHub silently refuses a domain already claimed elsewhere.

## Remediation

Order matters: the domain must be released before it can be re-bound.

1. **Release the claim.** In `samgarib-debug/samsquaredsoftwares-site` →
   Settings → Pages → clear the custom domain. (Deleting the repo also works, but
   clearing the field is reversible.)
2. **Bind the domain to the real site.** In
   `SamSquaredSoftwares/SamSquaredSoftwares.github.io` → Settings → Pages → set the
   custom domain to `samsquaredsoftwares.com`. The `CNAME` file already holds the right
   value, so this should bind immediately once step 1 has propagated.
3. **Purge the Cloudflare cache** for the zone, to evict the cached Aug-9 `index.html`.
4. **Wait for GitHub to issue the origin certificate** for the apex, then enable
   *Enforce HTTPS* on the org repo.
5. **Set Cloudflare SSL/TLS mode.** Until step 4 completes, GitHub presents only
   `*.github.io` at the origin, so use **Full** — encrypted, name not validated.
   **Do not use Full (strict)** before step 4: it will fail the origin name check and
   take the site down. After step 4, move to Full (strict).
6. **Re-run the health check.** All content checks should pass and the apex should
   serve byte-identical content to `samsquaredsoftwares.github.io`.

### Verifying the fix

```bash
./scripts/check_domain_health.sh
```

Expected after remediation: `SamSquaredSoftwares/SamSquaredSoftwares.github.io owns the
custom domain`, all `MUST_SERVE` paths `200`, and identical content on both hosts.

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
  restricts issuance, but must include every CA in use — currently Google Trust Services
  (Cloudflare Universal SSL) and Let's Encrypt (GitHub Pages, after step 4). A wrong CAA
  record breaks renewal, so add it only after step 4 and verify with the health check.

- **Domain not verified for the GitHub org.** `protected_domain_state` is `null`. GitHub's
  verified-domains feature prevents another account from claiming the domain — which is
  exactly the failure mode that produced the primary bug above. Worth enabling once the
  domain is bound to the org repo.
