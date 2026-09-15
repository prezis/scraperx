# EVIDENCE — każdy fakt z komendą, którą sprawdzisz go sam

Nic tutaj nie jest „wiadomo". Każdy wiersz ma sposób powtórzenia. Jeśli jakiś pomiar dziś wyjdzie
inaczej — **twój pomiar wygrywa**, a ten plik jest do poprawienia.

---

## E1. `fomo.family/` to statyczna strona marketingowa

Zmierzone 2026-09-15: 48 448 B, **jeden** `<script src>` i to Google Tag Manager, zero markerów
frameworka, zero danych użytkowników.

```bash
cd ~/ai/bot-gate && .venv/bin/python - <<'PY'
import sys, re; sys.path.insert(0,"scripts")
from fomo_cookies import IMPERSONATE
from curl_cffi import requests as cr
t = cr.get("https://fomo.family/", impersonate=IMPERSONATE, timeout=25).text
print("dlugosc:", len(t))
print("skrypty:", re.findall(r'<script[^>]+src="([^"]+)"', t))
marks = ["__NEXT_DATA__","__NUXT__","__remixContext","data-reactroot"]
print("markery:", [k for k in marks if k in t] or "BRAK")
print("dane userow:", [k for k in ("userHandle","realizedPnl","totalVolume","followers") if k in t] or "BRAK")
PY
```

Oczekiwane: `skrypty: ['https://www.googletagmanager.com/gtag/js?id=G-7NZ4HJXCKG']`,
`markery: BRAK`, `dane userow: BRAK`.

---

## E2. Anonimowo API zwraca `unauthorized` na wszystkim

Cztery różne endpointy, bez tokenu → `{"error":"unauthorized"}`. Kod HTTP który wrócił: **431**
(nietypowy, ale ciało jest jednoznaczne).

```bash
cd ~/ai/bot-gate && .venv/bin/python - <<'PY'
import sys; sys.path.insert(0,"scripts")
from fomo_cookies import IMPERSONATE
from curl_cffi import requests as cr
UID = "2e955ffc-6fac-578a-93d8-3324605f6bed"   # @change, tier S
H = {"Accept":"application/json","Origin":"https://fomo.family",
     "Referer":"https://fomo.family/","X-Supported-Chains":"1399811149,8453,56,143,4663"}
for e in (f"/trades?userId={UID}&size=1", f"/v2/users/{UID}/balances",
          f"/v2/users/{UID}/followers", "/v2/users/userHandle/change"):
    r = cr.get("https://prod-api.fomo.family"+e, headers=H, impersonate=IMPERSONATE, timeout=20)
    print(r.status_code, (r.text or "")[:60], e)
PY
```

**To jest kontrola, nie założenie.** Jeśli którykolwiek zwróci 200 z danymi — masz publiczny
kanał i cała reszta tego briefu jest do przemyślenia od nowa.

---

## E3. Cloudflare blokuje wszystkie fingerprinty Chrome

`chrome120`–`chrome146` → HTTP 403 z ciałem HTML (strona WAF). `firefox144` → 200,
`safari180` → 200. Dlatego `scripts/fomo_cookies.py` ma `IMPERSONATE='firefox144'` jako
jedyne źródło prawdy dla 12 skryptów.

Rozróżnienie, które trzeba zrobić **przed** wnioskiem „mamy bana":
- ciało **HTML** (CAPTCHA/WAF) → Cloudflare, czyli fingerprint albo IP
- ciało **JSON** `Forbidden` → backend FOMO, czyli konto

Pełny 5-krokowy flowchart: `~/ai/global-graph/patterns/fomo-api-403-diagnostic.md`.

---

## E4. `/trades` ignoruje `page` i `size` — krotność dokładnie 5,00×

Kanon zapisał to 2026-05-21 (kosztowało 12 h sweepa i **50× zawyżony PnL**), a 2026-09-15
powtórzyło się przy `--max-pages 5`:

| zbiór | wiersze surowe | unikalne po `trade.id` | mediana krotności |
|---|---|---|---|
| 22 userów bez adresu | 1 075 | **219** | **5,00×** |
| 280 userów kontrolnych | 19 736 | **3 972** | **5,00×** |

```bash
# kontrola na artefakcie, ktory juz jest na dysku:
ls -la ~/ai/bot-gate/data/fomo-solana-refetch-*.jsonl
# i policz krotnosc po trade.id — patrz scripts/refetch_solana_positions.py :125-144
```

**Każdy nowy kolektor MUSI deduplikować po `trade.id` i przerywać, gdy strona nie wnosi nic
nowego.** `hasNextPage` jest zakodowane na twardo `True` — nie wierzyć mu.

---

## E5. Historia banów: per-konto, wyzwalane wolumenem

- Konto banowane **godziny po ciężkim sweepie na jednym koncie**.
- Ostatni powrót: **2026-09-14 21:21 BST**, po ~**35 h** HTTP 403.
- Pełny przebieg po 11 342 przypisanych użytkownikach = **~5,4 h ciągłych requestów**.
- Breaker `#150` jest zatrzaśnięty od 2026-08-08 dla headless-agenta na naszym profilu
  (5 kolejnych porażek, live probe 403 dla `sub …w50djuiq5u9qnq`) — **celowo**, żeby nie
  odtwarzać skazanego logowania przeciw odmawiającemu klastrowi.
- **Jedyny żywy tor dostawy klucza dziś: rozszerzenie w Chrome operatora i kolegów.**
  Mierzone 2026-09-11: 2 konta, ~32 klucze na konto na 3 dni, `agent=1.0.0`.

```bash
# stan NA TERAZ, czytany z dysku (nie z pamieci):
cat ~/.claude/state/fomo-fetch-status.json
ls -la /tmp/fomo/jwt.txt                       # wiek tokenu
ls ~/.claude/state/fomo-operator-pause         # pauza zalozona?
tail -3 ~/ai/bot-gate/data/fomo_key_pool.jsonl # rozszerzenie karmi?
ps -eo pid,cmd | grep [f]omo_jwt_agent         # headless agent zyje?
```

---

## E6. Rozszerzenie ma już uprawnienia, których potrzeba

```bash
cat ~/ai/bot-gate/tools/fomo-key-agent/manifest.json
grep -nE "executeScript|localStorage|RECEIVER|tabs.query" \
     ~/ai/bot-gate/tools/fomo-key-agent/background.js
```

Zmierzone: `v1.1.6`, `permissions: [cookies, alarms, scripting, tabs, storage]`,
`host_permissions` obejmuje `https://fomo.family/*`. `background.js` (323 linie) robi
`chrome.tabs.query({url:"*://fomo.family/*"})` → `chrome.scripting.executeScript` →
`localStorage.getItem("privy:token")` → POST na `https://intel.lubiszto.win/hook/fomo_key`.

**Wniosek: `scripting` + host permission na `fomo.family/*` wystarczają, żeby wykonać `fetch`
w origin zalogowanej zakładki.** Zmiana jest mała; ryzyko leży w tempie, nie w mechanizmie.

---

## E7. Ile danych bot-gate już ma (kontrola pozytywna dla kolektora)

```bash
cd ~/ai/bot-gate && sqlite3 "file:intel-fe/backend/intel.db?mode=ro" \
  "select 'wallets', count(*) from wallets
   union all select 'trades', count(*) from trades
   union all select 'positions', count(*) from positions;"
```

Zmierzone 2026-09-15: `wallets 14 131` · `trades 527 154` · `positions 527 153`.

**Użyj tego jako kontroli pozytywnej:** kolektor pobierający dane użytkownika, którego
`trades` już zna, musi dać **te same** `trade.id`, `sumSwapOpen` i `createdAt`. Jeśli nie daje —
czyta co innego i nie wolno tego scalać.

---

## E8. Rozkład wyceny per łańcuch — dlaczego base/bsc są osobnym problemem

Zmierzone 2026-09-15 na `live_events` (tor łańcuchowy, bez wierszy vendora):

| chain | BUY wycenione | SELL wycenione |
|---|---|---|
| robinhood | 410 770 / 410 824 = **100,0%** | **100,0%** |
| sol | 123 815 / 123 996 = **99,9%** | **97,4%** |
| bsc | 749 / 9 327 = **8,0%** | **7,0%** |
| base | 441 / 5 141 = **8,6%** | **2,6%** |
| monad | zero zdarzeń | — |

Istotne dla priorytetów: **na robinhood i sol dane dolarowe mamy z łańcucha.** Braki FOMO bolą
najbardziej tam, gdzie nasza wycena i tak nie działa (base/bsc) — więc scrape FOMO na base/bsc
ma **wyższą** wartość niż na robinhood, gdzie i tak jesteśmy samowystarczalni.
