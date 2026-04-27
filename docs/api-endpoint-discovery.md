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

## Post-discovery: when the endpoint exists but returns 403/401

You found the path. You're authenticated. It still rejects you. **Don't email the vendor yet.** Most enterprise B2B APIs split auth into TWO gateways:

```
┌──────────────────────────────────────────────────────────────────┐
│  IDENTITY GATEWAY (is.vendor.com / auth.vendor.com)              │
│    Issues OAuth tokens. Stamps scope strings. Generous, free.    │
│                                                                  │
│  API MANAGER GATEWAY (api.vendor.com / gateway.vendor.com)       │
│    Validates per-API-product subscription. Strict, plan-based.   │
│                                                                  │
│  DEVPORTAL (cp.vendor.com/devportal/apis / developer.vendor.com) │
│    Customer self-service. Subscribe consumer key to API products.│
└──────────────────────────────────────────────────────────────────┘
```

**Quick triage of any 403:** look at the body. If it carries a vendor-specific code that mentions "subscription", "plan", "tier", or "product" — go to DevPortal, click Subscribe, done. WSO2's flagship code for this is **`900908 "API Subscription validation failed"`**. Apigee uses `subscription_required`. AWS uses `usage_plan_violation`. All mean the SAME thing: your consumer key isn't subscribed to this API product. Self-service fix.

If the body is plain "Forbidden" with no code, common culprits are: passing `receiverId=` / `tenantId=` query params when the token already encodes them, sending the wrong `Accept-Language` format, or using a wrong-tier API key.

**Full pattern doc:** `~/ai/global-graph/patterns/two-gateway-api-platforms.md` — covers WSO2, Apigee, Kong, AWS API Gateway, Azure APIM with vendor-specific error codes and a 4-cell probe to distinguish scope from subscription problems in 30 seconds.

### Drop-in scaffold: probe-before-email

Add this to every new scraper module under `scripts/<vendor>_subscription_probe.py`:

```python
# scripts/<vendor>_subscription_probe.py
import os, asyncio, httpx

AUTH = "https://is.vendor.com/oauth2/token"
API  = "https://api.vendor.com"

async def main():
    creds = {
        "grant_type": "client_credentials",
        "client_id": os.environ["VENDOR_CLIENT_ID"],
        "client_secret": os.environ["VENDOR_CLIENT_SECRET"],
    }
    for label, scope in [("default", None), ("elevated", "allinone")]:
        body = {**creds, **({"scope": scope} if scope else {})}
        async with httpx.AsyncClient(timeout=10) as c:
            tr = await c.post(AUTH, data=body)
            tok = tr.json().get("access_token", "")
            er = await c.get(
                f"{API}/the/path/in/question",
                headers={"Authorization": f"Bearer {tok}"},
            )
            print(
                f"{label:>8} → token-status={tr.status_code} "
                f"endpoint-status={er.status_code} body={er.text[:200]}"
            )

asyncio.run(main())
```

Run it BEFORE opening any vendor support ticket. The output tells you in seconds whether you have a scope problem (fixable on your side) or a subscription gap (DevPortal click) — vs a real path/credential issue worth escalating.

## Scraperx new-client checklist (REVISED)

Before opening a PR that adds a new scraper/client to scraperx:

- [ ] Walked steps 1→4 of the discovery ladder
- [ ] Documented source of truth in the package's `README.md`: DevPortal / GitLab repo / HAR file / grepped bundle
- [ ] If HAR/bundle-grep was the source: committed a redacted snapshot to `tests/fixtures/<vendor>/`
- [ ] If DevPortal: noted URL + required subscription tier
- [ ] Every endpoint path in code has a comment linking to its source
- [ ] No invented path names — if unsure, mark `TODO` and block the PR
- [ ] First CI run hits a real sandbox / dev endpoint and captures response for fixture
- [ ] **Two-gateway check:** if vendor uses WSO2 / Apigee / Kong / AWS APIM / Azure APIM:
  - [ ] Added `scripts/<vendor>_subscription_probe.py` (template above)
  - [ ] README has a "DevPortal subscription state" section with the URL + currently-subscribed API products
  - [ ] First 403 hit was probed BEFORE any vendor ticket (link the probe output in the PR description)

## Related

- Canonical discovery pattern: `~/ai/global-graph/patterns/api-endpoint-discovery-without-docs.md`
- **Two-gateway pattern (post-discovery 403/401 triage):** `~/ai/global-graph/patterns/two-gateway-api-platforms.md`
- Example — Inter Cars WebAPI: `~/ai/global-graph/tools/intercars-webapi.md`
- IC-specific 403 flowchart: `~/ai/global-graph/patterns/ic-api-403-diagnostic.md`
- Full incident trace: `~/Documents/future-gear/docs/wiki/ic-api-trace-v2-2026-04-24.md`
