# ARCHITECTURE — FOMO harvest, decided by the operator's answers 2026-09-15

**Status:** design locked. Gate (a) feasibility ✅ RESOLVED 2026-09-15 (backend returns 200 to
the page-origin authenticated call). Gate (b) — live-server handlers — remains operator-gated.
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

## 2. The fetch runs in the BACKGROUND SERVICE WORKER, not the page — measured 2026-09-15

The ban vector (measured, brief §2/§E5) is **authenticated call volume per account**, not
"how the HTML is read". So the harvest is authenticated calls from the operator's real
Chrome (his IP, his session, real fingerprint) — being the FE, not scraping it.

**But WHERE in the extension the fetch runs is not free — measured, corrected from the
first draft:**

- A `fetch` to prod-api from the **page context** (an injected `page.evaluate` / a MAIN-world
  content script) is subject to the page's CORS and **fails** — measured `TypeError: Failed
  to fetch` on 2026-09-15 from a real `https://fomo.family` page (which itself loaded 200,
  no CF challenge). Page JS gets no cross-origin privilege.
- The **background service worker** fetch is NOT subject to CORS **because the manifest's
  `host_permissions` already include `https://*.fomo.family/*`, which covers
  `prod-api.fomo.family`**. MV3 grants the SW cross-origin fetch for hosts it holds
  permission for. This is the world the harvest MUST run in.
- The background SW already reads `localStorage["privy:token"]` from the tab
  (`background.js:20-40`) and already holds the `cookies` permission — so it can assemble the
  exact authenticated call (Bearer + `privy-session`/`privy-token` cookies) and fetch
  prod-api directly. **No page injection is needed at all** — a simplification over the first
  draft's "executeScript in-tab fetch".

**The backend accepts it — MEASURED, this is §7.a resolved.** A plain HTTP client (curl_cffi
firefox144, the SSOT recipe) with a live pooled JWT + session cookies + **`Origin:
https://fomo.family`**, from the operator's real IP (no proxy), returned **HTTP 200 with real
`responseObject.activeTrades`** for a tier-S user. The Origin header is not a blocker and no
proxy is needed on this path. A plain client bypasses CORS the same way the background SW
does, so its 200 is the faithful measure of what the SW will get.

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

## 5. Pacing — HUMAN-SHAPED, not a metronome (operator, 2026-09-15)

> Operator: *"mówimy bardziej jak człowiek — nie bez przerwy, nie w równych odstępach."*

A fixed 15 s cadence is the most bot-like signal there is: a human clicking through profiles
fires in irregular BURSTS with real BREAKS, never a clean tick. So the loop is shaped like a
person browsing, and the "4/min" answer is an **average ceiling**, not an interval.

- **Within a burst** (a browsing cluster): inter-call gap jittered, e.g. `random(4 s … 12 s)`
  — never a constant. A burst covers a random `6–18` users.
- **Between bursts** (person looks away): a real pause, `random(4 min … 20 min)`.
- **Average stays ≤ ~4/min** over any 10-min window — with the pauses it lands well under,
  which is *safer* than the ceiling, not looser.
- **Piggyback the operator's real session**: the loop only runs while his Chrome tab is
  actually open, so it inherits his day/night rhythm for free — no 03:00 metronome, the
  giveaway a cron would produce.
- **Don't sweep in a clean order**: shuffle the work batch and occasionally skip-and-requeue,
  so the request sequence isn't a monotonic userId walk.
- **Implementation:** the jitter/burst/pause parameters live server-side in `/hook/fomo_work`
  (it returns `{userIds (shuffled), gap_ms_range, burst_size, pause_ms_range}`), so the shape
  is tunable without reloading the extension; the background SW just obeys them.

Throughput consequence: bursts + minutes-long pauses mean the full 14 131 spreads across
**many days of ordinary browsing**, not a contiguous run — which is the point. No account
ever sees the ~5.4 h continuous shape (brief §E5) that earned the 35-hour ban; instead it
sees what it sees from any user who opens the app now and then. The 100-user zero-403
acceptance run (§6) is what turns "should look human" into a measured claim.

## 6. Success criteria (brief §8, made concrete here)

1. **Zero 403** across a ≥100-user run.
2. **Positive control:** harvested `trade.id`/`sumSwapOpen`/`createdAt` for a user already
   in `intel.db::trades` (527 154 rows) MATCH what is on disk. Mismatch ⇒ reading something
   else ⇒ do not merge.
3. **Negative control:** a fabricated `userId` returns empty, not data.
4. Dedup multiplicity **1.00×**, not 5.00× (brief §E4).
5. Measured throughput (users/h) + cost (calls/user) recorded so full-run time is arithmetic.

## 7. Gates

**a. ✅ RESOLVED 2026-09-15 — the backend accepts the page-origin authenticated call.**
Measured (out of auto mode, operator-authorized, `FOMO_IGNORE_PAUSE=1` one-shot): a plain
curl_cffi/firefox144 call with a live pooled JWT + session cookies + `Origin:
https://fomo.family`, from the operator's real IP, returned **HTTP 200 with real
`responseObject.activeTrades`** for tier-S user `2e955ffc-…`. The page-context `page.evaluate`
fetch returned `TypeError: Failed to fetch` (CORS) — which is why the harvest runs in the
**background service worker**, not the page (§2). The `host_permissions` for
`https://*.fomo.family/*` already cover `prod-api.fomo.family`, so the SW fetch is CORS-free.
Feasibility of a single call is proven; what remains is purely the VOLUME/pace question the
whole design exists to manage.

**b. Two handlers in the LIVE `intel-fe/backend/server.py`** remain an operator-gated
production change (change-control), and step 2 (`data/fomo_harvest.db` staging) must exist
first — cron :94 DROP+rebuilds `live_positions` every 15 min. Sequence in §8.

**b. Writing two handlers into the LIVE `intel-fe/backend/server.py`** (the receiver the
extension already POSTs to, ~9 000 lines, in production) is a production change on
bot-gate — the operator's call per change-control, and better done in the bot-gate session
where its tests + smoke live, not from the scraperx cwd.

## 8. Implementation steps — step 1 DELIVERED by bot-gate 2026-09-15 12:50

Sequence refined by the bot-gate session (its reply:
`~/ai/bot-gate/docs/outbox-to-scraperx/2026-09-15-1250-*.md`). **Step 1 is
gate-INDEPENDENT** — it makes zero FOMO calls, so it is safe to build before the §7.a
feasibility check; steps 3–5 are the ones the gate blocks.

1. ✅ **DONE** — `bot-gate/scripts/fomo_harvest_ingest.py`: pure function
   (`parse_page`/`dedupe_pages`/`to_position_rows`), zero network/DB/clock. Selftest GREEN
   on the real fixture `fomo-solana-refetch-20260915T061624Z.jsonl`: 303 users, 91 990 raw
   → 18 522 unique, **multiplicity 4.97×** (reproduces the documented 5.00× trap, so the
   parser is not theoretical). Dedup by `trade.id` (fallback `networkId:mint:createdAt`, NOT
   a whole-row hash — FOMO mutates `updatedAt` between fetches); first page adding nothing
   ends the walk; `has_next_claimed` reported back so the caller sees the hard-coded lie;
   non-200/bad-JSON → `UNDETERMINED`, never `EMPTY` (honest nulls).
2. `data/fomo_harvest.db` + staging schema (separate file, NEVER `intel.db`). **This must
   exist before step 3** — cron line 94 DROP+rebuilds `live_positions` every 15 min, so any
   write path touching `live_events.usd` moves tiers within the quarter-hour; the endpoint
   has nowhere safe to write until (2) exists.
3. `server.py` `/hook/fomo_harvest` — beside `hook_fomo_key`, same token gate, writes ONLY
   to (2).
4. `server.py` `/hook/fomo_work` — + DID gate + pause-flag respect.
5. `fomo-key-agent` → `v1.2.0`: DID-gated harvest loop **in the background service worker**
   (NOT a page-injected fetch — that hits CORS; the SW's `host_permissions` for
   `*.fomo.family` make its fetch to prod-api CORS-free). The SW already reads the JWT and
   holds the `cookies` permission, so it assembles Bearer + session cookies + `Origin` and
   fetches prod-api directly. Operator reloads HIS install only.

Steps 2–5 wait on the operator's go (production change, change-control) AND blocker (a).
Then the 100-user acceptance (§6), `FOMO_IGNORE_PAUSE` never a default.

## 9a. ZAKRES ZDECYDOWANY 2026-09-15 — A + wąska próbka /trades (pomiar bot-gate)

**Buduj:** graf społeczny dla userów z adresem **+ próbka `/trades` ~320 S/A/B, raz na miesiąc**.
**Nie buduj:** `/balances` (wycena jest nasza), `userHandle/` (zwraca Privy embed, nonce 0 —
bezużyteczny dla handle→portfel), pełnego przebiegu `/trades` po 14 131 userach.

### Dlaczego świadek `/trades` ZOSTAJE — i dlaczego moja hipoteza padła

Zaproponowałem przemierzenie `can_we_tier_from_our_own_feed.py` na oczyszczonym wejściu,
licząc, że dzisiejsze sprzątanie mianownika ($40 432 173 611 → $179 370 678) zamknie lukę i
uczyni ledger FOMO zbędnym. **Nie zamknęło.** Pomiar bot-gate (ich lane, ich liczby, cztery
przebiegi, kontrola pozytywna scorera 6 380/7 491 = 85,2% w każdym):

| wariant | zgodność tieru |
|---|---|
| produkcja (fizyka OFF), wszystkie łańcuchy | 180/417 = **43,2%** |
| PHYSICS (`LIVE_POS_HONOR_PHYSICS=1`), wszystkie łańcuchy | 181/417 = **43,4%** |
| produkcja, bez Solany | 157/372 = **42,2%** |
| PHYSICS, bez Solany | 157/372 = **42,2%** — identycznie |

**Sprzątanie przesunęło zgodność o JEDNEGO użytkownika.** Luka nie jest napędzana trucizną.

**Co ją napędza — kierunek macierzy jest systematyczny:** nasz feed **AWANSUJE** (B→A 57–58,
C→A 20, D→A 9, FOMO S→A 3). To brak **HISTORII** (retencja 14 dni, base/bsc dopiero od
2026-09-13, wobec dożywotniego ledgera FOMO), nie brak adresów i nie trucizna. Dokładnie to
rozgałęzienie, które docstring skryptu przewidywał.

**Wartość tego pomiaru leżała w ROZDZIELENIU przyczyn, nie w potwierdzeniu mojej nadziei.**
Przed sprzątaniem nie dało się odróżnić „rekonstrukcja jest gorsza przez truciznę" od „przez
pokrycie" — mianownik był w 99,2% fantomem. Teraz wiadomo: **pokrycie**, a tego sprzątanie nie
naprawia ani o wiersz (`measure_feed_recall_vs_fomo_ledger.py`: 25,7% trejdów EVM FOMO).

### Zweryfikowane przeze mnie (nie relacjonowane na słowo)

- `LIVE_POS_DB` override wszedł do skryptu (`can_we_tier_from_our_own_feed.py:54-56`) — lustro
  naszego `LIVE_POS_OUT`, więc pomiar biegnie na wariancie i produkcja stoi nietknięta.
- `--chains` bez Solany jest uzasadnione: `intel.db::trades` ma `networkId` 4663 (213 197),
  8453 (160 190), 56 (146 753), 143 (7 014) i **ZERO wierszy 1399811149** — FOMO `/trades` nie
  zwraca Solany w ogóle.
- Obie strony wypchnięte (bot-gate `3b0276c4`, global-graph `012b4fc4`).

### Konsekwencja dla wartości opcji A

Kolumny społeczne (`fomo_followers`/`following`/`verified`) **nie są wejściami tieru** —
`_edge_tier` czyta wyłącznie exp_R/wr/kelly/n_decisive/lifetime_pnl, a te kolumny są tylko
COALESCE-upsertowane i eksportowane do xlsx. Więc A ma wartość **operatorską** (kogo warto
śledzić — rola FOMO jako „źródła portfeli"), a **nie tierową**. Zbierać wolno i tylko dla
userów z adresem (11 344 z 14 131 = 80,3%).

### Budżet powierzchni bana

~23 000 requestów na miesiąc przy ludzkim tempie z §5 ≈ **4 doby rozłożone na 30**, nie 4,9
doby ciągiem. Próbka `/trades` to ~320 wywołań raz na miesiąc, nie 14 131.

## 9. Related

- `README.md` / `INVENTORY.md` / `EVIDENCE.md` (this dir) — the measured brief.
- `~/ai/bot-gate/tools/fomo-key-agent/background.js` — the extension to extend (v1.1.6).
- `~/ai/bot-gate/intel-fe/backend/server.py:8972` — `hook_fomo_key`, the receiver pattern to copy.
- `~/ai/global-graph/tools/fomoapp-api-bible.md` — field traps.
- `~/ai/global-graph/patterns/fomo-api-403-diagnostic.md` — the CF-vs-account 403 flowchart.
- `~/.claude/skills/fomo-harvest/SKILL.md` — the FOMO procedure skill; carries this architecture's non-negotiables inline and auto-triggers on FOMO work.
