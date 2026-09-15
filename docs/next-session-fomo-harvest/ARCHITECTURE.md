# ARCHITECTURE — FOMO harvest, decided by the operator's answers 2026-09-15

**Status:** design locked, build BLOCKED on two operator-only gates (§7). Not yet coded.
**Supersedes the open questions in** `README.md` §7 — they are answered here.
**Author:** session `703f20bb` (scraperx), 2026-09-15 12:0x BST.

---

## 0. What changed vs the brief

The brief offered two roads: ADOPT the browser extension (§3) or BUILD a scraperx
headless session (§6). **The operator's answers pick ADOPT, own-tab-only.** That makes
this a **bot-gate** build (extension + receiver), and demotes the scraperx stealth tools
to the fallback the brief already ranked second. `scrapling_stealth` does not solve the
real constraint (authenticated call volume per account), only the Cloudflare layer — which
the operator's own logged-in tab already passes.

## 1. The three answers (operator, AskUserQuestion 2026-09-15)

| question | answer | consequence |
|---|---|---|
| whose tabs may HARVEST DATA (not just read the key) | **only the operator's tab** | volume divisor = 1; friends' installs keep sending KEYS ONLY, exactly as today. The harvest loop MUST be gated to one Privy DID. |
| safe authenticated calls/min in one tab | **4/min** (~human browsing) | 710-user cohort ≈ 6 h; full 14 131 ≈ 4.9 days of paced fetching. |
| what to collect | **full 14 131 users, spread over days** | needs a DURABLE server-side work queue — the tab will close and reopen across days; progress cannot live in the tab. |

Divisor-1 + 4/min + full-corpus is internally consistent only with a **resumable queue**:
the tab drains a little each time it is open, the server remembers what is done.

## 2. Why in-tab fetch, and why it is safe where a sweep is not

The ban vector (measured, brief §2/§E5) is **authenticated call volume per account**, not
"how the HTML is read". The extension already holds `scripting` + `host_permissions` on
`https://fomo.family/*` and already runs `chrome.scripting.executeScript` in the logged-in
tab to read `localStorage["privy:token"]` (`background.js:20-40`). The same injection can
run `fetch("https://prod-api.fomo.family/…")` **from the page origin**: operator IP, operator
session cookies, real browser fingerprint, correct `Origin`/`Referer`. It is not
"scraping the FE" — it is **being** the FE, which is the one shape FOMO cannot distinguish
from a human using the app. CORS is a non-issue: prod-api already allows
`Origin: https://fomo.family` because the FE calls it on every page.

**The load-bearing UNVERIFIED assumption** is exactly this: that a `fetch` from the
injected context returns 200+JSON, not a 403/opaque-CORS. It is sound by construction but
**not measured** — and I could not measure it (§7.a). It is the first go-live gate.

## 3. The split — minimal new JS, reuse the proven Python

The correctness traps that cost real money (the 5.00× `/trades` page dup → 50× PnL, the
silently-dropped `"solana"` literal, the `X-Supported-Chains` history filter, `otherPnl`
pedestal — brief §5, INVENTORY §2) all live in the **Python** that bot-gate already has and
tests. Re-implementing them in extension JS would re-open every one. So:

```
 ┌─ operator's Chrome tab (fomo.family, logged in) ──────────────────────────┐
 │  extension harvest loop  (NEW JS, ~80 lines, gated to operator DID)        │
 │   1. GET  /hook/fomo_work?agent_sub=<my Privy DID>                         │
 │        → { userIds:[…batch…], pace_ms, chains, run_token } | {} when paused│
 │   2. for each userId, spaced by pace_ms (≥15 000 ms ⇒ ≤4/min):            │
 │        fetch RAW /trades?userId=… and /v2/users/…/balances (+ social)      │
 │        with Authorization: Bearer <privy:token from THIS tab>              │
 │        X-Supported-Chains: 1399811149,8453,56,143,4663                     │
 │        → on HTTP 403: STOP THE WHOLE LOOP, POST an abort report, done.     │
 │   3. POST /hook/fomo_harvest { run_token, userId, raw_trades, raw_bal, … } │
 │        (RAW bytes only — no parsing in the tab)                            │
 └───────────────────────────────────────────────────────────────────────────┘
                     │ raw JSON                         ▲ queue / pause gate
                     ▼                                  │
 ┌─ bot-gate intel-fe backend (server.py, NEW handlers) ─────────────────────┐
 │  /hook/fomo_work    serve next N un-harvested userIds, honor the pause flag│
 │                     + the operator-DID gate; empty ⇒ tab idles, no calls   │
 │  /hook/fomo_harvest ingest raw → run the EXISTING parse/dedup Python →     │
 │                     stage to a NEW table (data/fomo_harvest.db), NOT       │
 │                     intel.db (it is DROP+rebuilt every 15 min, brief §6)   │
 └───────────────────────────────────────────────────────────────────────────┘
```

Both new endpoints sit beside the proven `/hook/fomo_key` (`server.py:8972`), reuse its
shared-token gate (`X-Fomo-Key-Token`, `data/fomo_key_agent_token.txt`), and add the
**operator-DID gate** on top so a friend's install that somehow calls `/hook/fomo_work`
gets `{}` and never harvests.

## 4. The non-negotiables, encoded as code obligations (brief §4/§5)

| rule | where enforced |
|---|---|
| pause flag `~/.claude/state/fomo-operator-pause` respected before the FIRST call | `/hook/fomo_work` returns `{}` while the flag exists; the tab makes zero prod-api calls when the queue is empty |
| 403 = ABORT, never push through | tab loop breaks on first 403 and reports it; server marks the run aborted, does not re-serve for a cooldown |
| harvest only the operator's account | `/hook/fomo_work` compares `agent_sub` to a configured operator DID; all others → `{}` |
| never write intel.db directly | ingest writes `data/fomo_harvest.db` only; merge into intel is a separate, human-gated bot-gate decision |
| dedup `/trades` by `trade.id`, distrust `hasNextPage` | done server-side in the existing Python (`refetch_solana_positions.py:125-144`); the tab sends raw pages, the server dedups |
| friends unaffected | the extension edit is to unpacked dev source; there is NO auto-update channel — each person loaded it by hand, so editing the file changes nobody's install until they reload. The harvest loop is additionally DID-gated. |

## 5. Pacing math (from the answers)

- 4/min ⇒ `pace_ms ≥ 15 000`. Two endpoints/user ⇒ ~30 s/user ⇒ ~120 users/hour.
- Cohort 710 ≈ **6 h**; full 14 131 ≈ **~118 h ≈ 4.9 days** of tab-open paced fetching.
- The queue makes those days non-contiguous: the operator opens the tab when convenient,
  it drains a batch, closes; the cursor persists. No account sees a 5-hour continuous burst
  — which is the exact shape (`~5.4 h`, brief §E5) that triggered the 35-hour ban.

## 6. Success criteria (brief §8, made concrete here)

1. **Zero 403** across a ≥100-user run.
2. **Positive control:** harvested `trade.id`/`sumSwapOpen`/`createdAt` for a user already
   in `intel.db::trades` (527 154 rows) MATCH what is on disk. Mismatch ⇒ reading something
   else ⇒ do not merge.
3. **Negative control:** a fabricated `userId` returns empty, not data.
4. Dedup multiplicity **1.00×**, not 5.00× (brief §E4).
5. Measured throughput (users/h) + cost (calls/user) recorded so full-run time is arithmetic.

## 7. 🛑 BLOCKERS — why no code shipped this turn (both operator-only)

**a. The load-bearing feasibility check is unrun.** §2's assumption (an injected-context
`fetch` to prod-api returns 200+JSON from the operator's tab) decides the entire ADOPT
path, and it cannot be measured without the operator's logged-in session: a headless login
is latched off by breaker `#150` (brief §E5), and the anonymous-probe route was blocked by
this session's own auto-mode classifier. **One paste settles it** — in the fomo.family tab
DevTools console, logged in:

```js
fetch("https://prod-api.fomo.family/trades?userId=2e955ffc-6fac-578a-93d8-3324605f6bed&size=1",
  {headers:{Accept:"application/json","X-Supported-Chains":"1399811149,8453,56,143,4663"}})
  .then(r=>r.json()).then(j=>console.log("OK", JSON.stringify(j).slice(0,200)))
  .catch(e=>console.log("FAIL", e))
```
`OK …activeTrades…` ⇒ build proceeds. `FAIL`/403 ⇒ the extension needs `world:"MAIN"` or
the whole ADOPT path falls back to scraperx headless (breaker #150 must lift first).

**b. Writing two handlers into the LIVE `intel-fe/backend/server.py`** (the receiver the
extension already POSTs to, ~9 000 lines, in production) is a production change on
bot-gate — the operator's call per change-control, and better done in the bot-gate session
where its tests + smoke live, not from the scraperx cwd.

## 8. First implementation steps once unblocked (the wędka, ready to hand off)

1. `bot-gate/scripts/fomo_harvest_ingest.py` — pure function: raw pages → deduped rows,
   wrapping the existing parse/dedup; unit-tested offline against a saved raw fixture
   (`data/fomo-solana-refetch-*.jsonl` already on disk).
2. `bot-gate` `server.py`: `/hook/fomo_work` (+ pause + DID gate) and `/hook/fomo_harvest`
   (calls #1, stages `data/fomo_harvest.db`), beside `hook_fomo_key`, same token gate.
3. `fomo-key-agent` → `v1.2.0`: add the DID-gated harvest loop; bump the manifest;
   operator reloads HIS install only.
4. Run the 100-user acceptance (§6) with the pause flag lifted once, `FOMO_IGNORE_PAUSE`
   never set as a default.

## 9. Related

- `README.md` / `INVENTORY.md` / `EVIDENCE.md` (this dir) — the measured brief.
- `~/ai/bot-gate/tools/fomo-key-agent/background.js` — the extension to extend (v1.1.6).
- `~/ai/bot-gate/intel-fe/backend/server.py:8972` — `hook_fomo_key`, the receiver pattern to copy.
- `~/ai/global-graph/tools/fomoapp-api-bible.md` — field traps.
- `~/ai/global-graph/patterns/fomo-api-403-diagnostic.md` — the CF-vs-account 403 flowchart.
