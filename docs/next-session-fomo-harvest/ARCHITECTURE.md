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

## 9a. ⚠ ZAKRES POD REWIZJĄ OPERATORA 2026-09-15 22:0x — próbka /trades WSTRZYMANA

**Nie budować próbki `/trades` ani grafu społecznego do decyzji operatora.** Klient v1.2.0
nie wymaga zmiany: po poprawce bezpieczeństwa (§9b) zakres wybiera serwer, więc wstrzymanie
jest czysto serwerowe.

**Pytanie operatora, które to wywróciło** (verbatim): *„po co nam trades dla S/A/B i innych
tam, gdzie potwierdziliśmy, że wallety EVM i SOL przypisane użytkownikom są poprawne?"*

Trafne, i pomiar to potwierdza. Nasze 42–43% zgodności tieru mierzyło **brak HISTORII**
(retencja 14 dni; recall feedu vs ledger FOMO 25,7–28,7%), a historię dla **potwierdzonego
portfela backfilluje się ON-CHAIN** (`getAssetTransfers` / `getSignaturesForAddress`), nie z
FOMO. Czyli świadek `/trades` dla S/A/B rozwiązywałby problem, który ma tańsze i trwalsze
rozwiązanie po stronie łańcucha.

**Zmierzone przeze mnie na `intel.db::wallets` (nie relacjonowane):**

| klasa | użytkowników |
|---|---|
| ma **OBA** portfele (EVM+SOL) | **11 344** |
| **BEZ portfela** (custodial) | **2 787** |
| „tylko jeden portfel" | **klasa nie istnieje** — podział jest binarny |

Tiery klasy bez portfela: `-` 2 364 · D 202 · C 196 · **B 21 · A 4**.

**Jedyna klasa, gdzie FOMO API jest STRUKTURALNIE jedynym źródłem trejdów, to custodial** —
oni nie mają portfela do odczytania, więc żaden backfill on-chain ich nie obejmie, nigdy.
Ale realna stawka to **25 użytkowników A/B** (4+21) plus 196 C i 202 D. Czy to warte
powierzchni bana — decyzja operatora, nie moja. *(bot-gate podaje 23 436 wierszy `trades` dla
tej klasy; nie zweryfikowałem tej liczby — join po nazwie kolumny mi nie przeszedł.)*

### Zakres FOMO kurczył się DZIŚ czterokrotnie, za każdym razem przez pomiar

1. „potrzebne do wyceny base/bsc" → **martwe** (base/bsc naprawione 13.09 15:17)
2. „graf społeczny ma wartość tierową" → **martwe** (`_edge_tier` tych kolumn nie czyta)
3. „`userHandle/` da handle→portfel" → **martwe** (zwraca Privy embed, nonce 0)
4. „świadek `/trades` dla S/A/B" → **pod rewizją** (luka to historia, a historia jest on-chain)

**Co przeżyło każdą rundę pomiaru:** (a) odkrywanie NOWYCH użytkowników + mapowanie
handle→portfel (roster raz na miesiąc), (b) `lifetime_pnl_api` jako komponent 40% PnL — do
czasu własnego silnika, (c) klasa custodial. Nic więcej.

---

### (poprzedni zapis, zachowany — na czym stała decyzja przed pytaniem operatora)

## 9a-old. ZAKRES ZDECYDOWANY 2026-09-15 — A + wąska próbka /trades (pomiar bot-gate)

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

## 9b. KONTRAKT KLIENT↔SERWER — przypięty 2026-09-15 (klient v1.2.0 napisany PIERWSZY)

Krok 5 zbudowany (`tools/fomo-key-agent` v1.2.0, +217 linii, **0 usunięć** — tor kluczy
nietknięty, `node --check` OK, **zero nowych uprawnień**, więc nikt nie musi nic re-akceptować).
Serwer (kroki 2–4, bot-gate) musi pasować do tego kontraktu.

### `GET /hook/fomo_work?agent_sub=<privy DID>` · nagłówek `X-Fomo-Key-Token`

```json
200 → { "run_token": "…|null",
        "pacing": { "gapMinMs": 6000, "gapMaxMs": 14000, "burst": 6,
                    "pauseMinMin": 8, "pauseMaxMin": 25 },
        "tasks": [ { "kind": "trades|followers|following", "user_id": "<uuid>" } ] }
```

🔴 **ZMIANA KONTRAKTU 2026-09-15, po przeglądzie bezpieczeństwa — `path` ZNIKNĄŁ.**
Pierwszy szkic v1.2.0 przyjmował `task.path` od serwera i doklejał do niego **Bearer
operatora + cookies sesji**, walidując wyłącznie `startsWith("/")`. Przegląd zgłosił to jako
SSRF / server-controlled credentialed requests **oraz** eksfiltrację — i obie diagnozy mają
**jedną przyczynę**: to **confused deputy**. Przejęty receiver (albo sam wyciekły wspólny
token) mógłby kazać zalogowanej przeglądarce operatora uderzyć **uwierzytelnionym żądaniem w
dowolną ścieżkę prod-api** — także w endpoint ujawniający jego dane prywatne — i odebrać
surową odpowiedź przez `/hook/fomo_harvest`. Klient by posłuchał, bo nie miał zdania o tym,
CO pobiera, tylko o pierwszym znaku łańcucha.

**Naprawa odwraca granicę zaufania zamiast filtrować mocniej:** serwer wybiera **KOGO** i
**JAKIEGO RODZAJU**; URL składa **klient** z zamkniętej listy (`DH_ENDPOINTS`), `user_id`
waliduje jako UUID, a origin sprawdza ponownie przez `new URL()`. **Jakikolwiek `path`
przysłany przez serwer jest IGNOROWANY.** Zakres, kohorta i tempo zostają strojone
serwerowo — nie tracimy nic z projektu harvestu poza możliwością wskazania dowolnego celu
cudzymi poświadczeniami.

Kontrola adwersaryjna klienta (9/9): legalne `trades`/`followers` budują poprawne ścieżki ·
`{kind:"trades", path:"/v2/me/private"}` → **path zignorowany**, wykonana legalna ścieżka ·
nieznany `kind`, sam `path` bez `kind`, `user_id` z traversalem, protocol-relative `//evil.com`,
`x@evil.com`, pusty — **wszystkie ODRZUCONE**.

| odpowiedź | zachowanie klienta |
|---|---|
| `200` + `tasks: []` | pauza albo pusta kolejka → **ZERO wywołań prod-api** |
| `404` | „jeszcze nie wdrożone" → bezczynność 60–90 min, **ZERO wywołań** |
| cokolwiek innego / nieznany kształt | **ZERO wywołań** — klient nigdy nie wymyśla pracy |

`pacing` jest opcjonalny; klient ma własne, **wolniejsze** domyślne. Serwer stroi kształt bez
przeładowywania wtyczki.

### `POST /hook/fomo_harvest` · nagłówek `X-Fomo-Key-Token`

```json
normalnie: { run_token, agent_sub, user_id, kind, path, http_status, raw, ts, agent }
abort:     { run_token, agent_sub, aborted: true, reason: "http_403", path, ts, agent }
```

`raw` to **surowe bajty odpowiedzi** — klient nie parsuje niczego. Pułapka 5,00× stron,
filtr `X-Supported-Chains` i dedup po `trade.id` to wiedza python-side, która ma testy; druga
kopia w JS rozjechałaby się z tą, za której naukę już zapłaciliśmy.

### Obowiązki SERWERA (bot-gate)

1. **Honorować `~/.claude/state/fomo-operator-pause` PRZED wydaniem zadania** — wtyczka nie
   widzi systemu plików operatora, więc pauza musi być egzekwowana po stronie serwera. To
   reguła operatora, nie opcja.
2. **Brama DID** — `agent_sub` musi zgadzać się ze skonfigurowanym DID operatora, inaczej
   `tasks: []`. Instalacje kolegów dalej wysyłają WYŁĄCZNIE klucze.
3. **`path` NIE istnieje w kontrakcie.** Serwer podaje wyłącznie `kind` + `user_id`; URL składa
   klient z zamkniętej listy. Nie wysyłajcie `path` — zostanie zignorowany.
4. Zapis **wyłącznie** do `data/fomo_harvest.db`, nigdy do `intel.db` (DROP+rebuild co 15 min).

### Gwarancje KLIENTA (v1.2.0)

- **Domyślnie WYŁĄCZONY.** Wymaga jawnego włączenia + podania DID operatora.
- **Brama DID także po stronie klienta** — dzielnik wolumenu = 1, konto kolegi nie zostaje
  wydane bez pytania tylko dlatego, że zaktualizował wtyczkę.
- **403 = ABORT całego przebiegu**, POST raportu abort, parking 240 min. Bez przepychania.
- Jitter w serii, **tasowana kolejność**, seria ograniczona budżetem 150 s zegara.
- Pętla rusza z alarmu i z załadowania zalogowanej karty — **dziedziczy godziny operatora**,
  więc nie ma metronomu o 03:00.

### Ograniczenie MV3, które ukształtowało projekt

Service worker MV3 jest eksmitowany w bezczynności i twardo ograniczony ~5 minutami nawet przy
pracy. **Pętla z pauzami 4–20 min NIE MOŻE przeżyć w workerze.** Dlatego jeden tick alarmu =
jedna ograniczona seria, następny tick planowany z jitterem, a **cały stan żyje w
`chrome.storage.local`**. Kto będzie to zmieniał — to jest powód, dla którego nie ma tu pętli.

## 9c. KSZTAŁTY ENDPOINTÓW — uziemione w kodzie bot-gate, nie w prośbie (v1.2.2, 2026-09-16)

bot-gate podał listę kindów i poprosił o konkretne typy parametrów. Trzy z nich nie przeżyły
przeczytania **ich własnych skryptów**. Zapisane tutaj, bo każda z tych pomyłek byłaby CICHA —
nie wywróciłaby niczego, tylko zwróciła wiarygodne zero.

| co proszono | co jest w kodzie | skutek gdyby posłuchać na słowo |
|---|---|---|
| `cursor` typu `uuid` | `quote_plus(cursor)` w `enrich_trading_wallets_v2.py:164` i `v3_async.py:163` → token **nieprzezroczysty** | walidator odrzuca każdą realną stronę 2 → raport „brak dalszych stron", czyli **zero wyprodukowane przez nasz własny typ** |
| `lastId` na followers/followingPaginate | `harvest_social_graph.py:6-7` to **docstring**; kod (:115) wysyła tylko `{"limit": N}`, skrypt `deprecated_2026-07-05`. Żywy `lastId` należy do `/feed/token` (`harvest_feed_token.py:164`). Żywy kształt tutaj to `?cursor=` (`fomo_harvest.py:222`) | paginacja po parametrze, którego ten endpoint może nie znać |
| `leaderboard` z `timeframe` + `page` | `leaderboard_discover.py:150-156`: serwer **ignoruje** limit (50→1000 = zawsze 100 wierszy) i **ignoruje** timeframe (kontrola negatywna `zzz9` → HTTP 200, 100 wierszy) | typowany parametr wyrzucany do kosza = **fałszywa obietnica kontroli**, nie bezpieczeństwo |

**Zasada, która z tego wychodzi:** typ parametru zakotwicz na tym, co token **DZIELI** w całej
klasie (nieprzezroczysty, ograniczony, URL-enkodowany), a nie na kształcie tej jednej wartości,
którą akurat zobaczyłeś. UUID był dokładnie takim tokenem wyróżniającym.

**Allowlista v1.2.2:** `followers` / `following` (odkrywanie; gołe wywołanie to **200 pełnych
rekordów**, `leaderboard_discover.py:181`) · `trades` (`?userId=&size=100&page=1`, zakres
egzekwuje serwer) · `lifetime_pnl` · `leaderboard` (ZERO parametrów). `roster` **nadal nieobecny**
— bo bot-gate potwierdził, że roster **JEST** tym spacerem po followers/following, więc nie ma
czego zgadywać. `identity` **nie istnieje jako kind** — portfel bierze się z dopasowania ledgera
trejdów do łańcucha (#492), nie z osobnego wywołania; kind #2 zapadł się w #1.

**Odrzucenie jest teraz RAPORTOWANE**, nie połykane: `POST /hook/fomo_harvest` z
`{rejected:[{kind,user_id,reason}]}`. Poprzednio `if (!path) continue;` wyglądał po stronie
serwera identycznie jak „tej strony nie ma" — serwer zapisałby opinię naszego walidatora jako
pomiar FOMO.

**Kontrole są ZAKOMITOWANE** (`tools/fomo-key-agent/tests/allowlist_controls.mjs`, 26/26,
`node tests/allowlist_controls.mjs`). Dwie poprzednie rundy dowodziłem w shellu i dowód ginął
z sesją — granica bezpieczeństwa re-argumentowana z pamięci przy każdej edycji nie jest granicą.
Test **parsuje żywe źródło**, nie kopię, bo skopiowana allowlista przechodziłaby własne testy
w nieskończoność, podczas gdy `background.js` dryfuje pod spodem.

⚠ **Kontrola pozytywna złapała martwy inwariant.** Pierwsza wersja sprawdzała
`new URL(API_BASE + path).origin === API_BASE` — 198 zielonych buildów, które **nie mogły
zawieść**: gdy baza niesie origin, wszystko doklejone jest ścieżką, więc nawet
`//evil.example.com/x` zostaje na prod-api. To samo dotyczy **tej samej linijki w
`background.js`** (`target.origin !== API_BASE`) — jest nieszkodliwa i zostaje, ale **to nie ona
nas chroni**; chroni szablon + `encodeURIComponent`. Zastąpione inwariantem szablonu ścieżki
(po normalizacji URL, która zwija `..`) + kluczy query: **0/304** złamanych na prawdziwym
builderze, **7/14** wstrzyknięć złapanych na celowo zepsutym.

## 10. 2026-09-16 — CO TEN TOR NAPRAWDĘ PRZYWRÓCIŁ, i kontrakt `harvest_swaps` (#517)

### 10.1 Odkrywanie użytkowników stało CZTERY MIESIĄCE — to jest właściwa miara tej pracy

Zmierzone 2026-09-16 na pytanie operatora „dlaczego @MomoOnChain nigdy u nas nie był"
(726 trejdów FOMO, $20,06 mln wolumenu, 30 019 obserwujących):

| klucz szukania | wynik |
|---|---|
| `user_id`, `handle` (nocase), SOL, EVM, `demix_wallet`, `denied_*` | **0** |
| `wallet_identity_history` (48 489 wierszy), po adresie i po handle | **0** |
| `handle_aliases` | **0** — kolumna istnieje, wypełniona w **0/48 489** |
| pliki odkrywania (`discovered-users*.jsonl`, `sweep_gate_log.jsonl`) | **0/0/0**, kontrola pozytywna `taxrat` → 1 |

**To nie był dryf nicku.** Oba tory odkrywania spały: BFS społeczny to `SEEDS = ("remusofmars",
"change", "frankdegods")` w `fomo_harvest.py:54` — trzy nazwy, jednorazowo, zamrożone od
2026-05-12; tor leaderboardowy ma `LIMIT = 50` × 4 okna = **sufit 200 tożsamości na przebieg**
i jest poza cronem od 2026-09-12 (decyzja operatora o wstrzymaniu sweepów, nie przeoczenie).

Momo przyszedł **13:48:25 ze strony `following` innego użytkownika** — był **JEDEN SKOK** od
kogoś, kogo już mieliśmy. Stąd wniosek, który zmienia sens całego toru:

> **Wtyczka nie jest trzecim torem odkrywania. Jest ODMROŻONYM BFS-em** — tym samym mechanizmem
> co w maju, chodzącym ciągle, tempowanym tak, by nie złapać bana, i zasilanym z żywej sesji
> zamiast z trzech nazw wpisanych na sztywno. D8 nie jest dodatkiem; przywraca zdolność.

### 10.2 Pierwszy urobek D8 — i pierwszy raz, gdy widzimy Solanę po stronie FOMO

Pięć pierwszych ksiąg z partii 100 (16:05 BST), userzy spoza `wallets`:

| konto | trejdy FOMO | wolumen | pozycji na dysku |
|---|---|---|---|
| @CryptoTalkMan | 860 | $3 539 371 | 96 |
| @cases | 125 | $3 311 091 | 50 |
| @Stark1 | 6 599 | $2 926 444 | 59 |
| @horseimnot | 2 307 | $1 650 224 | **1 034** |
| @neo10 | 492 | $1 157 878 | 66 |

Rozkład sieci w 1 445 pozycjach: **SOLANA 892 (61,7%)** · Robinhood 378 · BNB 157 · Base 17 ·
Monad 1. Czyli **ten tor od pierwszego dnia widzi nogę, której korpus swapów nie ma ani razu**
w 1 472 339 wierszach (biblia §975 — wada nagłówka, nie pól).

### 10.3 Tempo referencyjne klienta — ZMIERZONE, nie założone

| wielkość | wartość |
|---|---|
| wywołania `/hook/fomo_work` | **4,1/h** · odstępy min 5,5 / **mediana 12,1** / max 62,4 min |
| strony | **15,7/h** = **0,26 strony/min** |
| przepustowość | 377 stron/dobę przy pracy ciągłej; realnie ~157 (laptop nie chodzi 24 h) |

Założenie „4 wywołania/min" z 15.09 było **SUFITEM decyzji operatora, nie celem** — klient jest
o rząd wielkości wolniejszy dzięki własnym ogranicznikom (gap ≥6 s, burst ≤6, pauza 8–25 min),
których serwer może tylko ZWOLNIĆ. Model ryzyka bana liczymy po kliencie.

### 10.4 Kontrakt `harvest_swaps` — ZAMKNIĘTY 2026-09-16 (mój projekt + akcept bot-gate + decyzja operatora)

**Korekta założenia, która zmniejszyła zakres pracy:** pola `inNetworkId` / `outNetworkId` /
`recipient` / `isOffPlatform` **nie są nowe** — korpus `fomo_swaps_cumulative.jsonl` ma komplet
30 pól od 114 dni; wadą był wyłącznie nagłówek proszący o literał `solana`. Zweryfikowane przez
bot-gate na 60 000 wierszy: pary wyłącznie cross-chain, **zero SOL→SOL**, `provider` RELAY 59 952.
Nowy tor więc **ODTWARZA** schemat, nie wymyśla go — stary korpus i nowy staging będą unionowalne.

**Rzecz, którą rozstrzygnął jeden prawdziwy rekord:** `inNetworkId=8453, outNetworkId=1399811149,
networkId=8453` — swap Base→Solana. Czyli **`networkId` NIE mówi, na jakim łańcuchu był handel**;
mówi to dopiero para (in, out). Na `networkId` stał fałszywy wniosek kanonu, obalony w biblii.

Trzy decyzje, każda z zapisanym kosztem:

| # | decyzja | dlaczego | koszt, który przyjmujemy |
|---|---|---|---|
| 1 | PK `(user_id, swap_id)` | `user_id` FOMO jest MUTOWALNY (biblia §881); re-rejestracja ma być **widoczna**, nie scalona po cichu | liczba wierszy ≠ liczba swapów → widok `harvest_swaps_distinct` + zdanie w DATA-CATALOGUE |
| 2 | BRAK dedupu przy zapisie | ten sam swap z dwóch nagłówków to **pomiar rozmiaru dziury**, nie śmieć | dedup JAWNY przy odczycie: widok scalający po `swap_id` z kolumną `n_sources` |
| 3 | `address` ZOSTAJE, podpisana | **operator, dosłownie: „musi zostać, podpisany, oznaczony, wiadomy, znany"** — bezużyteczność ma być UDOKUMENTOWANA, nie przemilczana, bo puste pole wyglądające jak adres zaprasza do fałszywego wniosku | komentarz „pasuje do 0/200 000" w DDL + docstringu konsumenta + DATA-CATALOGUE |

Dopiski bot-gate, wszystkie przyjęte: **`chains_header text not null`** (dosłowna wartość
`X-Supported-Chains`, którą pobrano stronę — cała wada była nagłówkiem, więc każdy wiersz niesie
swój), **`fetched_at integer not null`** (epoch serwera, ≠ `consumed_at`), oraz
`create index ix_harvest_swaps_swap on harvest_swaps(swap_id)` pod przyszły union.

**Zasiew partii 1: 681 kont**, nie 963 — `attribution_status IN ('CONFIRMED','DEMIX_CONFIRMED')
AND real_sol_wallet IS NOT NULL AND real_sol_wallet <> '' AND (ledger_sol_verdict IS NULL OR
ledger_sol_verdict = 'UNDETERMINED')`. Pierwsza partia **KALIBRACYJNA: 20 kont × cap 3 strony**,
bo liczby stron/konto NIE ZNAMY — szacunek 1,3 stoi na trejdach EVM jako proxy, a noga SOL jest
u nas niezmierzona z definicji. 60 stron zamienia proxy w pomiar. `fomo_swap_count` jako
mianownik odpada (biblia §57).

**Bramka: GO operatora + D7 (#506).** Tabela nie powstaje wcześniej.

## 9. Related

- `README.md` / `INVENTORY.md` / `EVIDENCE.md` (this dir) — the measured brief.
- `~/ai/bot-gate/tools/fomo-key-agent/background.js` — the extension to extend (v1.1.6).
- `~/ai/bot-gate/intel-fe/backend/server.py:8972` — `hook_fomo_key`, the receiver pattern to copy.
- `~/ai/global-graph/tools/fomoapp-api-bible.md` — field traps.
- `~/ai/global-graph/patterns/fomo-api-403-diagnostic.md` — the CF-vs-account 403 flowchart.
- `~/.claude/skills/fomo-harvest/SKILL.md` — the FOMO procedure skill; carries this architecture's non-negotiables inline and auto-triggers on FOMO work.
