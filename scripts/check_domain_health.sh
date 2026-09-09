#!/usr/bin/env bash
# Health check for the samsquaredsoftwares.com edge architecture:
#
#   browser --> Cloudflare (wildcard cert) --> GitHub Pages (origin)
#                                               \- 301 www -> apex
#
# Exits non-zero if any check fails, so it can run as a daily/CI gate.
#
# Usage:
#   ./scripts/check_domain_health.sh                      # defaults below
#   APEX=example.com PAGES_REPO=org/repo ./scripts/check_domain_health.sh
#   ./scripts/check_domain_health.sh --quiet              # only failures + summary

set -uo pipefail

APEX="${APEX:-samsquaredsoftwares.com}"
WWW="${WWW:-www.$APEX}"
# The GitHub Pages repo that is *supposed* to serve the custom domain.
PAGES_REPO="${PAGES_REPO:-SamSquaredSoftwares/SamSquaredSoftwares.github.io}"
# Paths that must resolve on the live custom domain.
read -r -a MUST_SERVE <<<"${MUST_SERVE:-/ /about.html /contact.html /how-it-works.html /pricing.html /product.html /styles.css /script.js}"
# Cloudflare's resolver: avoids a stale local DNS cache reporting the wrong origin.
RESOLVER="${RESOLVER:-1.1.1.1}"
CURL_TIMEOUT="${CURL_TIMEOUT:-20}"
# Warn when the edge certificate is closer than this to expiry.
CERT_MIN_DAYS="${CERT_MIN_DAYS:-14}"

QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1

PASS=0 FAIL=0 WARN=0
FAILURES=()

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
[[ -t 1 ]] || { c_red=""; c_grn=""; c_yel=""; c_dim=""; c_off=""; }

ok()   { PASS=$((PASS+1)); (( QUIET )) || printf '  %sPASS%s %s\n' "$c_grn" "$c_off" "$1"; }
bad()  { FAIL=$((FAIL+1)); FAILURES+=("$1${2:+ -- $2}"); printf '  %sFAIL%s %s\n' "$c_red" "$c_off" "$1"; [[ -n "${2:-}" ]] && printf '       %s%s%s\n' "$c_dim" "$2" "$c_off"; }
warn() { WARN=$((WARN+1)); printf '  %sWARN%s %s\n' "$c_yel" "$c_off" "$1"; [[ -n "${2:-}" ]] && printf '       %s%s%s\n' "$c_dim" "$2" "$c_off"; }
head_() { (( QUIET )) || printf '\n%s\n' "$1"; }

need() { command -v "$1" >/dev/null 2>&1; }

PS=""
for cand in powershell.exe pwsh.exe pwsh; do need "$cand" && PS="$cand" && break; done

# Query DNS through $RESOLVER rather than the system cache: a stale local cache can
# report a proxied record as pointing straight at the origin, which inverts the
# diagnosis entirely. dig is preferred; PowerShell's resolver is the Windows
# fallback (the nslookup binary there frequently times out against 1.1.1.1).
resolve_a() {
  local host="$1"
  if need dig; then
    dig +short +time=3 +tries=2 @"$RESOLVER" "$host" A 2>/dev/null | grep -E '^[0-9]+\.' || true
  elif [[ -n "$PS" ]]; then
    "$PS" -NoProfile -Command \
      "(Resolve-DnsName -Name '$host' -Type A -Server '$RESOLVER' -ErrorAction SilentlyContinue | Where-Object {\$_.Type -eq 'A'}).IPAddress" \
      2>/dev/null | tr -d '\r' | grep -E '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || true
  elif need nslookup; then
    nslookup -type=A "$host" "$RESOLVER" 2>/dev/null | tr -d '\r' \
      | sed -n '/^Name:/,$p' | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' | grep -v "^${RESOLVER}$" || true
  fi
}

# TXT strings, one record per line.
resolve_txt() {
  local host="$1"
  if need dig; then
    dig +short +time=3 +tries=2 @"$RESOLVER" "$host" TXT 2>/dev/null | tr -d '"' || true
  elif [[ -n "$PS" ]]; then
    "$PS" -NoProfile -Command \
      "Resolve-DnsName -Name '$host' -Type TXT -Server '$RESOLVER' -ErrorAction SilentlyContinue | Where-Object {\$_.Type -eq 'TXT'} | ForEach-Object { \$_.Strings -join '' }" \
      2>/dev/null | tr -d '\r' | grep -v '^$' || true
  fi
}

# Cloudflare anycast space used for proxied records (the ranges these zones land in).
is_cloudflare_ip() { [[ "$1" =~ ^(104\.(1[6-9]|2[0-9]|3[01])\.|172\.6[4-9]\.|172\.7[0-1]\.|188\.114\.|162\.15[89]\.|198\.41\.) ]]; }
is_github_pages_ip() { [[ "$1" =~ ^185\.199\.(108|109|110|111)\.153$ ]]; }

hdrs() { curl -sSI --max-time "$CURL_TIMEOUT" "$@" 2>/dev/null; }
code() { curl -sS -o /dev/null --max-time "$CURL_TIMEOUT" -w '%{http_code}' "$@" 2>/dev/null; }

printf '%sDomain health: %s%s\n' "$c_dim" "$APEX" "$c_off"

# ---------------------------------------------------------------- DNS + proxy
head_ "DNS / proxy status"
apex_ips="$(resolve_a "$APEX")"
www_ips="$(resolve_a "$WWW")"

if [[ -z "$apex_ips" ]]; then
  bad "$APEX resolves" "no A records returned via $RESOLVER"
else
  proxied=1
  while read -r ip; do [[ -n "$ip" ]] && ! is_cloudflare_ip "$ip" && proxied=0; done <<<"$apex_ips"
  if (( proxied )); then
    ok "$APEX is proxied through Cloudflare ($(echo "$apex_ips" | tr '\n' ' ' | sed 's/ $//'))"
  else
    bad "$APEX is NOT proxied through Cloudflare" "resolves to $(echo "$apex_ips" | tr '\n' ' ')- origin is exposed and the Cloudflare cert is bypassed"
  fi
fi

if [[ -z "$www_ips" ]]; then
  bad "$WWW resolves" "no A records returned via $RESOLVER"
else
  wproxied=1 wgithub=0
  while read -r ip; do
    [[ -z "$ip" ]] && continue
    is_cloudflare_ip "$ip" || wproxied=0
    is_github_pages_ip "$ip" && wgithub=1
  done <<<"$www_ips"
  if (( wproxied )); then
    ok "$WWW is proxied through Cloudflare"
  elif (( wgithub )); then
    bad "$WWW points straight at GitHub Pages (DNS-only)" "GitHub serves only *.github.io, so HTTPS to $WWW fails the name check"
  else
    bad "$WWW is NOT proxied through Cloudflare" "resolves to $(echo "$www_ips" | tr '\n' ' ')"
  fi
fi

# ------------------------------------------------------------------- TLS cert
head_ "Edge certificate"
cert_host="$(echo "$apex_ips" | head -1)"
if [[ -n "$cert_host" ]] && need openssl; then
  for name in "$APEX" "$WWW"; do
    cert="$(echo | openssl s_client -connect "$cert_host:443" -servername "$name" 2>/dev/null)"
    subject="$(printf '%s' "$cert" | openssl x509 -noout -subject 2>/dev/null | sed 's/^subject=//')"
    san="$(printf '%s' "$cert" | openssl x509 -noout -ext subjectAltName 2>/dev/null | tr -d ' ' | grep -o 'DNS:[^,]*' | sed 's/DNS://' | tr '\n' ' ')"
    notafter="$(printf '%s' "$cert" | openssl x509 -noout -enddate 2>/dev/null | sed 's/notAfter=//')"

    if [[ -z "$subject" ]]; then
      bad "cert presented for $name" "TLS handshake produced no certificate"
      continue
    fi

    # Does any SAN entry actually cover this hostname (exact or single-label wildcard)?
    covered=0
    for entry in $san; do
      [[ "$entry" == "$name" ]] && covered=1 && break
      if [[ "$entry" == \*.* ]]; then
        suffix="${entry#\*}"                 # ".example.com"
        if [[ "$name" == *"$suffix" ]]; then
          label="${name%"$suffix"}"
          [[ "$label" == *.* ]] || { covered=1; break; }   # wildcard spans one label only
        fi
      fi
    done
    if (( covered )); then
      ok "cert covers $name  ${c_dim}(${subject}; SAN: ${san% })${c_off}"
    else
      bad "cert does NOT cover $name" "presented ${subject}; SAN: ${san% }"
    fi

    # Expiry headroom.
    if [[ -n "$notafter" ]]; then
      if exp_epoch="$(date -d "$notafter" +%s 2>/dev/null)"; then
        days=$(( (exp_epoch - $(date +%s)) / 86400 ))
        if (( days < 0 )); then
          bad "cert for $name expired" "$notafter"
        elif (( days < CERT_MIN_DAYS )); then
          warn "cert for $name expires in ${days}d" "$notafter"
        else
          ok "cert for $name valid ${days}d ${c_dim}(until $notafter)${c_off}"
        fi
      fi
    fi
  done
else
  warn "certificate checks skipped" "openssl unavailable or apex did not resolve"
fi

# ------------------------------------------------------------ redirect chain
head_ "Redirects"
loc="$(hdrs "https://$WWW/" | grep -i '^location:' | tr -d '\r' | awk '{print $2}' | head -1)"
if [[ "$loc" == "https://$APEX/" ]]; then
  st="$(hdrs "https://$WWW/" | head -1 | tr -d '\r')"
  if grep -q '301' <<<"$st"; then
    ok "https://$WWW -> 301 -> https://$APEX/"
  else
    warn "www redirects to apex but not with 301" "$st"
  fi
else
  bad "https://$WWW does not 301 to https://$APEX/" "Location: ${loc:-<none>}"
fi

# A www hit that reaches GitHub costs a full origin round-trip; an edge rule avoids it.
if hdrs "https://$WWW/" | grep -qi '^x-github-request-id:'; then
  warn "the www->apex 301 is generated at the GitHub origin, not the Cloudflare edge" \
       "each www hit pays an origin round-trip; a Cloudflare Redirect Rule would serve it at the edge and survive an origin outage"
fi

http_loc="$(hdrs "http://$APEX/" | grep -i '^location:' | tr -d '\r' | awk '{print $2}' | head -1)"
if [[ "$http_loc" == https://* ]]; then
  ok "http://$APEX -> $http_loc"
else
  bad "http://$APEX does not redirect to HTTPS" "Location: ${http_loc:-<none>}"
fi

final="$(curl -sS -o /dev/null --max-time "$CURL_TIMEOUT" -L -w '%{url_effective}|%{http_code}|%{num_redirects}' "https://$WWW/" 2>/dev/null)"
IFS='|' read -r f_url f_code f_hops <<<"$final"
if [[ "$f_url" == "https://$APEX/" && "$f_code" == "200" ]]; then
  ok "www chain settles at $f_url (200, ${f_hops} hop(s))"
else
  bad "www chain does not settle cleanly" "ended at ${f_url} with ${f_code} after ${f_hops} hop(s)"
fi

# ------------------------------------------------------- origin / Pages owner
head_ "GitHub Pages origin"
if need gh && gh auth status >/dev/null 2>&1; then
  # One API call for both fields, via gh's built-in jq (no external jq dependency).
  pages_info="$(gh api "repos/$PAGES_REPO/pages" \
    --jq '[(.cname // ""), (.https_enforced|tostring)] | join("|")' 2>/dev/null | tr -d '\r')"
  expected_cname="${pages_info%%|*}"
  enforced="${pages_info##*|}"

  if [[ "$expected_cname" == "$APEX" ]]; then
    ok "$PAGES_REPO owns the custom domain $APEX"
  else
    # GitHub binds a custom domain to exactly one Pages site, and the site holding it
    # is often under a different owner than the intended repo. Scan a bounded set of
    # owners to name the holder, since that is the actionable part of the finding.
    claimed=""
    owners=("${PAGES_REPO%%/*}")
    me="$(gh api user --jq '.login' 2>/dev/null | tr -d '\r')"
    # A login is [A-Za-z0-9-] only; anything else means the API returned an error body,
    # which must never be word-split into bogus owner names.
    [[ "$me" =~ ^[A-Za-z0-9-]+$ ]] && owners+=("$me")
    [[ -n "${SCAN_OWNERS:-}" ]] && read -r -a extra <<<"$SCAN_OWNERS" && owners+=("${extra[@]}")

    declare -a cand=()
    for owner in "${owners[@]}"; do
      [[ -z "$owner" ]] && continue
      while read -r r; do
        [[ "$r" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] && cand+=("$r")
      done < <(gh api "users/$owner/repos?per_page=100" --jq '.[]|select(.has_pages)|.full_name' 2>/dev/null | tr -d '\r')
    done

    checked=0
    for repo in $(printf '%s\n' "${cand[@]}" | sort -u); do
      (( checked++ > 40 )) && break          # hard cap: never fan out unbounded
      cn="$(gh api "repos/$repo/pages" --jq '.cname // empty' 2>/dev/null | tr -d '\r')"
      if [[ "$cn" == "$APEX" ]]; then claimed="$repo"; break; fi
    done

    bad "$PAGES_REPO does NOT own the custom domain" \
        "its Pages cname is '${expected_cname:-null}'${claimed:+; the domain is held by $claimed}. GitHub binds a custom domain to one Pages site, so the CNAME file in $PAGES_REPO stays inert until that claim is released."
  fi

  if [[ "$enforced" == "true" ]]; then
    ok "$PAGES_REPO has Enforce HTTPS on"
  elif (( ${proxied:-0} )); then
    ok "origin Enforce HTTPS off -- expected behind the Cloudflare proxy ${c_dim}(GitHub's DNS pre-check sees Cloudflare's IPs, so it cannot issue an apex cert; HTTPS is enforced at the edge instead)${c_off}"
  else
    warn "$PAGES_REPO does not enforce HTTPS" "domain is not proxied, so GitHub should be able to issue a cert and enforce HTTPS"
  fi
else
  warn "Pages ownership checks skipped" "gh CLI not installed or not authenticated"
fi

# Origin TLS: decides whether Cloudflare can run Full vs Full (strict).
if need openssl; then
  osub="$(echo | openssl s_client -connect 185.199.108.153:443 -servername "$APEX" 2>/dev/null | openssl x509 -noout -subject 2>/dev/null | sed 's/^subject=//')"
  if [[ "$osub" == *"$APEX"* ]]; then
    ok "origin serves a cert for $APEX -- Cloudflare Full (strict) is viable ${c_dim}($osub)${c_off}"
  elif [[ -n "$osub" ]]; then
    warn "origin serves $osub, not $APEX" "Cloudflare 'Full' works (encrypted, name not validated). While the domain is proxied GitHub cannot issue an apex cert, so 'Full (strict)' is permanently incompatible with this architecture -- keep the zone on Full"
  fi
fi

# ------------------------------------------------------------- content checks
head_ "Content served on the custom domain"
for p in "${MUST_SERVE[@]}"; do
  c="$(code "https://$APEX$p")"
  if [[ "$c" == "200" ]]; then
    ok "$p -> 200"
  else
    bad "$p -> $c" "expected 200 on the live domain"
  fi
done

# The custom domain and the canonical *.github.io host should serve the same build.
if need md5sum; then
  gh_host="${PAGES_REPO%%/*}.github.io"; gh_host="$(tr '[:upper:]' '[:lower:]' <<<"$gh_host")"
  # -L on both: once the custom domain is bound, the *.github.io host 301s to
  # the apex, so comparing raw bodies would always differ.
  a="$(curl -sSL --max-time "$CURL_TIMEOUT" "https://$APEX/" 2>/dev/null | md5sum | cut -d' ' -f1)"
  b="$(curl -sSL --max-time "$CURL_TIMEOUT" "https://$gh_host/" 2>/dev/null | md5sum | cut -d' ' -f1)"
  if [[ -n "$a" && -n "$b" ]]; then
    [[ "$a" == "$b" ]] && ok "$APEX and $gh_host serve identical content" \
      || bad "$APEX and $gh_host serve DIFFERENT content" "two separate Pages deployments are live; the custom domain is not serving $PAGES_REPO"
  fi
fi

# ------------------------------------------------------------------ DNS hygiene
head_ "DNS hygiene"
txt="$(resolve_txt "$APEX")"
if [[ -n "$txt" ]]; then
  spf_count="$(grep -c 'v=spf1' <<<"$txt" || true)"
  case "$spf_count" in
    0) warn "no SPF record on $APEX" "mail claiming to be from this domain cannot be validated" ;;
    1) ok "exactly one SPF record" ;;
    *) bad "$spf_count SPF records on $APEX" \
            "RFC 7208 allows exactly one; multiple yield a permerror, so SPF fails outright: $(grep 'v=spf1' <<<"$txt" | paste -sd' | ' -)" ;;
  esac
else
  warn "could not read TXT records for $APEX" "install dig, or run where PowerShell/Resolve-DnsName is available"
fi

# CAA: dig if available, else DNS-over-HTTPS JSON (Resolve-DnsName has no CAA type).
caa=""
if need dig; then
  caa="$(dig +short +time=3 @"$RESOLVER" "$APEX" CAA 2>/dev/null)"
elif need python; then
  caa="$(curl -sS --max-time 10 "https://cloudflare-dns.com/dns-query?name=$APEX&type=CAA" \
          -H 'accept: application/dns-json' 2>/dev/null \
        | python -c "import json,sys
for a in json.load(sys.stdin).get('Answer',[]): print(a['data'])" 2>/dev/null)"
fi
if [[ -z "$caa" ]]; then
  warn "no CAA record on $APEX" "any public CA may issue for this domain; Cloudflare's managed CAA (or a manual set covering its Universal SSL CAs) restricts issuance"
else
  # The zone relies on Cloudflare Universal SSL, which currently issues via
  # these CAs; a CAA set missing one of them can silently break cert renewal.
  missing=""
  for ca in pki.goog letsencrypt.org ssl.com; do
    grep -qF "\"$ca" <<<"$caa" || missing="$missing $ca"
  done
  if [[ -z "$missing" ]]; then
    ok "CAA present and covers Cloudflare's issuing CAs ${c_dim}($(grep -c 'issue' <<<"$caa") records)${c_off}"
  else
    warn "CAA present but missing:${missing}" "Cloudflare Universal SSL rotates across these CAs; a renewal routed to a missing one fails"
  fi
fi

hsts="$(hdrs "https://$APEX/" | grep -i '^strict-transport-security:' | tr -d '\r' | cut -d' ' -f2-)"
if [[ -n "$hsts" ]]; then
  ok "HSTS: $hsts"
  if grep -qi 'preload' <<<"$hsts"; then
    maxage="$(grep -oiE 'max-age=[0-9]+' <<<"$hsts" | grep -oE '[0-9]+' | head -1)"
    if [[ -n "$maxage" ]] && (( maxage < 31536000 )); then
      warn "HSTS sends 'preload' with max-age=$maxage" "hstspreload.org requires max-age >= 31536000 (1 year); submission is rejected below that"
    fi
    st="$(curl -sS --max-time 15 "https://hstspreload.org/api/v2/status?domain=$APEX" 2>/dev/null | grep -o '"status":"[^"]*"' | cut -d'"' -f4)"
    case "$st" in
      preloaded) ok "$APEX is on the HSTS preload list" ;;
      pending)   ok "$APEX preload submission is pending ${c_dim}(ships in a future Chromium release; other browsers follow its list)${c_off}" ;;
      *)         warn "HSTS sends 'preload' but $APEX is not on the preload list (status: ${st:-unknown})" \
                      "the directive is inert until the domain is submitted at hstspreload.org" ;;
    esac
  fi
else
  warn "no HSTS header on $APEX"
fi

# ------------------------------------------------------------------- summary
printf '\n%s\n' "----------------------------------------"
printf 'passed %s%d%s   failed %s%d%s   warnings %s%d%s\n' \
  "$c_grn" "$PASS" "$c_off" "$c_red" "$FAIL" "$c_off" "$c_yel" "$WARN" "$c_off"
if (( FAIL )); then
  printf '\n%sFailures:%s\n' "$c_red" "$c_off"
  for f in "${FAILURES[@]}"; do printf '  - %s\n' "$f"; done
  exit 1
fi
exit 0
