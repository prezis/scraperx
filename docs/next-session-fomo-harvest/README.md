# PROMPT — nauczyć scraperx zbierać dane FOMO bez palenia konta

**Dla:** sesji pracującej w `~/ai/scraperx`.
**Od:** sesji bot-gate, 2026-09-15 11:5x BST.
**Operator:** *„chciałbym scraperx nauczyć scrapowania FE fomo, żebyśmy nie wyłapali więcej banów za JWT"*.

Ten katalog ma trzy pliki i **wszystkie trzy są potrzebne**:

| plik | co zawiera |
|---|---|
| `README.md` (ten) | misja, ograniczenia, czego NIE robić, kryteria sukcesu |
| `INVENTORY.md` | pełna lista endpointów, pól i tego, co każde pole karmi |
| `EVIDENCE.md` | zmierzone fakty + komenda, którą każdy z nich sprawdzisz sam |

---

## 1. Misja, wąsko

bot-gate potrzebuje danych FOMO do **tierowania portfeli** (kto handluje dobrze). Dziś bierze je
przez `prod-api.fomo.family` z tokenem Privy (JWT). Pełny przebieg po wszystkich użytkownikach
to **~5,4 h ciągłych requestów na jednym koncie** — i to jest udokumentowany kształt wyzwalacza
bana. Konto wróciło po **~35 h** HTTP 403 (2026-09-14 21:21 BST).

**Zadanie: zaprojektować i zbudować zbieranie tych samych danych w sposób, który nie wygląda
dla FOMO jak zautomatyzowany sweep.**

---

## 2. 🔴 TRZY FAKTY, KTÓRE ZABIJAJĄ NAIWNY PLAN — sprawdź je PIERWSZE

Zmierzone 2026-09-15. Każdy ma komendę weryfikującą w `EVIDENCE.md`. **Nie zaczynaj od kodu.**

1. **`https://fomo.family/` to statyczna strona marketingowa.** 48 448 B, **jeden** tag
   `<script src=...>` i to Google Tag Manager. Zero `__NEXT_DATA__`, zero `__NUXT__`, zero
   `data-reactroot`. **Zero danych użytkowników w HTML.** Nie ma tam czego scrapować.
2. **Aplikacja jest klientem API.** Renderuje się po stronie klienta i każdą liczbę dociąga
   z `prod-api.fomo.family` z nagłówkiem `Authorization: Bearer <JWT>`. Czyli „scrapowanie FE"
   = prowadzenie przeglądarki, która robi **dokładnie te same wywołania**. Wektor bana
   (wolumen uwierzytelnionych wywołań na konto) **nie zmienia się**.
3. **Anonimowo nie ma NIC.** Kontrola na czterech endpointach bez tokenu → `{"error":
   "unauthorized"}` × 4. Żaden fragment nie jest publiczny.

**Wniosek: problem nie jest „jak odczytać HTML", tylko „z jakiego miejsca i w jakim tempie
wykonać uwierzytelnione wywołania, żeby wyglądały jak człowiek".**

---

## 3. ADOPT PRZED BUILD — 90% tego już działa

W Chrome operatora **działa rozszerzenie `FOMO Key Agent v1.1.6**`, źródło:
`~/ai/bot-gate/tools/fomo-key-agent/`.

```
manifest.json → permissions:      cookies, alarms, scripting, tabs, storage
                host_permissions: https://fomo.family/*, https://*.fomo.family/*,
                                  https://intel.lubiszto.win/*
background.js  (323 linie)  RECEIVER = https://intel.lubiszto.win/hook/fomo_key
```

Dziś używa `chrome.tabs.query({url: "*://fomo.family/*"})` +
`chrome.scripting.executeScript` **wyłącznie po to, by odczytać
`localStorage.getItem("privy:token")`** i wysłać go do receivera.

🔑 **Ten sam mechanizm może wykonać `fetch` WEWNĄTRZ origin zalogowanej zakładki.** Wtedy
request idzie z IP operatora, z jego sesją, z prawdziwym fingerprintem przeglądarki i z
poprawnym `Origin`/`Referer` — jest **nieodróżnialny od ruchu samego FE**. To nie scrapowanie
HTML-a; to **bycie** tym FE.

**To jest najmocniejszy kandydat na architekturę i trzeba go rozważyć PRZED pisaniem
czegokolwiek w scraperx.** Mierzone 2026-09-11: 2 konta, ~32 klucze na konto na 3 dni,
`agent=1.0.0`. Czyli kanał żyje i nie dostaje banów.

Dopiero jeśli to odpadnie, wchodzą narzędzia scraperx (patrz §6).

---

## 4. ⛔ OGRANICZENIA, KTÓRE SĄ NIENEGOCJOWALNE

1. **Istnieje BLOKADA sweepów i trzeba ją respektować.** Flaga:
   `~/.claude/state/fomo-operator-pause`. Każdy skrypt bot-gate sprawdza ją **przed pierwszym
   requestem** i przerywa. Operator, 2026-09-13: *„nie robimy codziennych sweepów, tylko na
   rozkaz"*. Świadome jednorazowe obejście: `FOMO_IGNORE_PAUSE=1 <komenda>`.
   **NIE przełączać `fomo_switch.sh on` na stałe.**
2. **403 = ABORT, nigdy „przepychaj dalej".** Przepchnięcie 403 jest tym, co zamienia
   rate-limit w bana. Każdy nowy kolektor musi przerywać na pierwszym 403 i to zgłaszać.
3. **Uwierzytelnienie ma dokładny kształt i jest w kodzie, nie do odgadywania:**
   `scripts/fomo_cookies.py` w bot-gate — `IMPERSONATE='firefox144'` (fallback `safari180`),
   Bearer **ORAZ** ciasteczka sesji `privy-session` + `privy-token`, plus proxy PL socks5h.
   🔴 **Cloudflare blokuje WSZYSTKIE fingerprinty Chrome** (`chrome120`–`chrome146` → 403);
   firefox144 i safari180 → 200. Zweryfikowane na żywo.
4. **Nie dotykać `intel.db` ani innych baz bot-gate.** Zebrane dane jadą do receivera
   (`/hook/fomo_key` albo nowy endpoint), a decyzję o scaleniu podejmuje bot-gate.
5. **Konta znajomych są w grze.** Rozszerzenie chodzi też u kolegów operatora. Ban dotyczy
   **konta**, więc każdy pomysł zwiększający wolumen na konto ryzykuje ich dostępem.

---

## 5. ❌ CZEGO NIE PRÓBOWAĆ — udokumentowane martwe drogi

Każda z tych rzeczy była testowana i **nie działa**. Nie powtarzaj.

| droga | dlaczego martwa |
|---|---|
| scrapowanie HTML `fomo.family` | statyczna strona marketingowa, zero danych (§2.1) |
| anonimowe wywołania API | `{"error":"unauthorized"}` na wszystkim (§2.3) |
| `curl_cffi` z fingerprintem **Chrome** | Cloudflare 403 na `chrome120`–`chrome146` |
| świeże konta jednorazowe | banowane od startu (403) |
| `page` / `size` w `/trades` | **ignorowane**; `hasNextPage` zakodowane na twardo `True`. Pętla po stronach zwraca TĘ SAMĄ stronę — zmierzone: krotność **dokładnie 5,00×** przy `--max-pages 5`. Dedup po `trade.id`. |
| bootstrap JWT w bot-gate venv | brak `playwright`; działa z `~/ai/scraperx/.venv` lub `~/ai/ca-gate/.venv` |
| Relay `referrer=fomo` jako źródło | HTTP 403 od 2026-08-31, kanał martwy |

---

## 6. Narzędzia scraperx, które mogą być istotne

Z `~/ai/global-graph/tools/scraperx.md` — **przeczytaj go**, nie zgaduj możliwości:

- `scrapling_stealth.fetch_stealth(url, solve_cloudflare=True)` — przebija Cloudflare/Turnstile
  (dowód: DexScreener 403→200, arkm.com ~65 s). **Ale** nie rozwiązuje problemu
  uwierzytelnienia — token nadal musi skądś być.
- Stack stealth: `patchright` (jest w `~/ai/scraperx/.venv`).
- Źródło jest **lokalne i edytowalne** — dodaj scraper/skill tutaj, potem udokumentuj
  w `scraperx.md`.

**Pytanie projektowe, na które trzeba odpowiedzieć pomiarem, nie intuicją:** czy headless
przeglądarka z `patchright`, zalogowana raz i utrzymująca sesję, jest dla FOMO
nieodróżnialna od zakładki operatora? Jeśli tak — scraperx może prowadzić własną sesję
i rozszerzenie nie jest potrzebne. Jeśli nie — rozszerzenie jest jedyną drogą.

---

## 7. Pytania do operatora, których NIE zgaduj

Sesja bot-gate zadała je i **nie ma jeszcze odpowiedzi**:

1. **Ilu kolegów ma zainstalowane rozszerzenie?** (kanon mierzył 2 konta) — to dzielnik wolumenu.
2. **Ile wywołań na minutę** operator uznaje za bezpieczne w swojej zakładce?
3. **Pełny sweep rozłożony w czasie** (14 131 użytkowników = dni, zero bana) czy **tylko
   S/A/B + kandydaci do awansu** (346 + 364 = 710, godziny)?

---

## 8. Kryteria sukcesu, mierzalne

Nie „działa", tylko:

1. **Zero 403** w przebiegu na ≥100 użytkownikach.
2. Pobrane pola **zgadzają się** z tym, co bot-gate ma już na dysku dla tych samych
   użytkowników — kontrola pozytywna na `intel.db::trades` (527 154 wierszy istniejących).
   Bez tej kontroli nie wiadomo, czy kolektor czyta to samo.
3. **Kontrola negatywna:** nieistniejący `userId` musi zwrócić pustkę, nie dane. Kolektor,
   który „zawsze coś zwraca", jest zepsuty.
4. Zmierzona **przepustowość** (użytkowników/godzinę) i **koszt** (requestów/użytkownika), żeby
   dało się policzyć czas pełnego przebiegu.
5. Dedup po `trade.id` **udowodniony** — krotność 1,00, nie 5,00 (patrz §5).

---

## 9. Gdzie jest reszta wiedzy

| co | gdzie |
|---|---|
| pełna lista endpointów i pól | `INVENTORY.md` (obok) |
| zmierzone fakty + jak je sprawdzić | `EVIDENCE.md` (obok) |
| architektura FOMO, 2 560 linii | `~/ai/global-graph/projects/fomoapp-wallet-architecture.md` |
| API FOMO, pułapki pól | `~/ai/global-graph/tools/fomoapp-api-bible.md` |
| diagnostyka 403, 5 kroków | `~/ai/global-graph/patterns/fomo-api-403-diagnostic.md` |
| możliwości scraperx | `~/ai/global-graph/tools/scraperx.md` |
| uwierzytelnienie w kodzie | `~/ai/bot-gate/scripts/fomo_cookies.py` |
| rozszerzenie | `~/ai/bot-gate/tools/fomo-key-agent/` |
