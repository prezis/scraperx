# API endpoint discovery when the vendor won't give you the contract

> Canonical version in `~/ai/global-graph/patterns/api-endpoint-discovery-without-docs.md`. This is the scraperx-flavored runbook — same ladder, but framed around building a new scraper/client.

## When you need this

You're about to write a new scraper or API client for a vendor. You:
- Don't have access to an OpenAPI spec / Postman collection
- Got stonewalled by vendor support
- Have a valid B2B account but no welcome pack
- Need to move now

**DO NOT:** invent endpoint paths based on intuition, run your first iteration as a support ticket, or ship production code with paths you haven't verified return 200.

**DO:** work the ladder below, stop as soon as you have a verified endpoint list, document your source.

## The ladder — cheapest to most invasive

### 1. OpenAPI self-discovery (30 seconds)

```bash
BASE="https://api.vendor.com/v1"
TOKEN="..."
for path in /openapi.json /openapi.yaml /swagger.json /swagger.yaml \
            /v3/api-docs /api-docs /.well-known/openapi-configuration \
            /docs /redoc /services; do
  curl -s -o /dev/null -w "%{http_code} $path\n" \
    -H "Authorization: Bearer $TOKEN" "$BASE$path"
done
curl -X OPTIONS -i -H "Authorization: Bearer $TOKEN" "$BASE/" | head -20
```

**Hit rate:** ~40% on enterprise B2B. Blocked by WSO2 (IC), Apigee, AWS API Gateway by default.

### 2. DevPortal UI (5 minutes)

Every major API gateway has one. If you have B2B credentials, log in:

| Gateway | URL pattern | What you get |
|---|---|---|
| WSO2 API Manager | `cp.<host>/devportal/apis` | Subscribed APIs + per-API downloadable Swagger |
| Kong | Kong Manager / Konga | API definitions |
| Apigee | `<org>.apigee.com/apis` | API products you're subscribed to |
| Azure APIM | `<org>.developer.azure-api.net` | Developer portal with full contract |
| Tyk | Tyk Dashboard `/apps` | Your API keys + assigned APIs |

### 3. Public-source mining (5-30 minutes)

Real working code leaks real endpoints.

```bash
# GitHub code search
gh search code '"api.vendor.com"'
gh search code '"authorization-host-here"' --language python

# GitLab — check the vendor's public org
curl -s "https://gitlab.com/api/v4/groups/<vendor-org>/projects" | jq '.[].path_with_namespace'

# Postman public workspace
open "https://www.postman.com/search?q=<vendor>+api"

# Wayback Machine (for rendered docs behind Cloudflare)
curl "https://web.archive.org/cdx/search/cdx?url=docs.vendor.com/*&output=json&limit=50"

# Error-code leak grep
gh search code '"VNDR-ERR-211"'  # vendor-specific codes are unique to their API
```

**How we found Inter Cars' contract:** Step 3, 2 minutes. `local_web_search "api.webapi.intercars.eu"` → `gitlab.com/intercars/ic-api` → swagger.yml + Postman collection with all 19 real endpoints.

### 4. Frontend reconnaissance (15 minutes — high-value)

The vendor's customer web app hits the same backend. Every XHR reveals a real endpoint.

**Manual:**
1. Log in to vendor's portal
2. F12 → Network → filter XHR/Fetch
3. Click through every major flow
4. Save HAR file (File menu → Save all as HAR)
5. `jq '.log.entries[].request.url' session.har | sort -u` to extract all URLs

**Automated (headless):**

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    urls = []
    page.on("request", lambda r: urls.append(r.url) if "api.vendor.com" in r.url else None)
    page.goto("https://portal.vendor.com/login")
    # ... login + click through
    print("\n".join(sorted(set(urls))))
```

**Bundle grep (no login needed):**

```bash
# Most SPAs hardcode their base URL + all endpoint paths in the minified JS
curl -s "https://portal.vendor.com" | grep -oE 'src="[^"]+\.js"' \
  | xargs -I{} curl -s "https://portal.vendor.com{}" \
  | grep -oE '"/api[a-zA-Z0-9/_{}-]+"' | sort -u
```

### 5. Error-leakage probing

Sometimes the gateway leaks sibling paths or allowed methods in error responses:

```bash
# Wrong method → 405 with Allow: header
curl -i -X DELETE "$BASE/known/path" -H "Authorization: Bearer $TOKEN"
# Look for: Allow: GET, HEAD, POST

# Malformed param → strict APIs list valid param names
curl "$BASE/known/path?bogus=1" -H "Authorization: Bearer $TOKEN"

# 403 vs 404 is an oracle — 404 = path doesn't exist, 403 = path exists but your scope is insufficient
```

### 6. Wordlist fuzzing (last resort — ETHICS WARNING)

**Only against:** your own API, bug-bounty scope, or explicit written permission. Against a B2B vendor's API without permission = WAF ban + ToS violation + dead token.

```bash
# Use a real API wordlist, not a webroot one
ffuf -w ~/SecLists/Discovery/Web-Content/api/api-endpoints.txt \
     -u "$BASE/FUZZ" \
     -H "Authorization: Bearer $TOKEN" \
     -mc 200,201,204,400,405  # filter out 404/403

# Kiterunner is purpose-built for API discovery with Swagger/Postman wordlists
kr scan https://api.vendor.com -w routes-large.kite
```

### 7. GraphQL introspection (if applicable)

```bash
curl -X POST "$BASE/graphql" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"{__schema{types{name fields{name}}}}"}'
```

### 8. Subdomain enumeration (vendor surface mapping)

```bash
curl -s "https://crt.sh/?q=vendor.com&output=json" | jq -r '.[].name_value' | sort -u
subfinder -d vendor.com -silent
```

Reveals `data.vendor.com` (CSV feeds), `cp.vendor.com` (DevPortal), `docs.vendor.com`, `is.vendor.com` (identity server). Not endpoint-level but shapes your reconnaissance.

## Scraperx new-client checklist

Before opening a PR that adds a new scraper/client to scraperx:

- [ ] Walked steps 1→4 of the ladder
- [ ] Documented source of truth in the package's `README.md`: DevPortal / GitLab repo / HAR file / grepped bundle
- [ ] If HAR/bundle-grep was the source: committed a redacted snapshot to `tests/fixtures/<vendor>/`
- [ ] If DevPortal: noted URL + required subscription tier
- [ ] Every endpoint path in code has a comment linking to its source
- [ ] No invented path names — if unsure, mark `TODO` and block the PR
- [ ] First CI run hits a real sandbox / dev endpoint and captures response for fixture

## Anti-pattern to recognize

"I'll just guess the path based on REST conventions and see what happens" — this is the Inter Cars 2026-04 incident. Three days of wasted support round-trips + embarrassed integrator + broken production paths that would have shipped. Cost: 3 days + credibility damage.

Alternative: 2 minutes of GitLab search + 30 seconds of `/openapi.json` probing + 5 minutes of DevPortal browsing. Cost: 8 minutes.

## Related

- Canonical pattern doc: `~/ai/global-graph/patterns/api-endpoint-discovery-without-docs.md`
- Example — Inter Cars WebAPI: `~/ai/global-graph/tools/intercars-webapi.md`
- Full incident trace: `~/Documents/future-gear/docs/wiki/ic-api-trace-v2-2026-04-24.md`
