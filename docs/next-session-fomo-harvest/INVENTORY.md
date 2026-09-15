# INVENTORY — co dokładnie pobieramy z FOMO i po co

Wyciągnięte **z kodu i ze schematu bazy** 2026-09-15, nie z pamięci. Komendy do powtórzenia
pomiaru są w `EVIDENCE.md`.

---

## 1. Endpointy w użyciu — 5, z 26 miejsc wywołań w `~/ai/bot-gate`

| endpoint | miejsc wywołań | co daje | dokąd trafia |
|---|---|---|---|
| `GET /trades?userId={uuid}` | **19** | pozycje/epizody użytkownika | `intel.db::trades` + `::positions` → **cały tier** |
| `GET /v2/users/{uuid}/balances` | 3 | bieżące worki + ich wycena | `fomo_current_value_usd`, holdings |
| `GET /trades/{tradeId}` | 3 | nogi DCA jednego trejdu | `fetch_dca_trades.py` |
| `GET /v2/users/{uuid}/swaps` | 1 | strumień swapów | sweeper, atrybucja portfeli |
| `GET /v2/userTokens/aggregatedSnapshot/interval` | 1 | portfel w czasie | `fomo_probe.py` (probe zdrowia) |

Baza: `https://prod-api.fomo.family`

**Skatalogowane, ale NIEUŻYWANE** (`scripts/explore_unused_fomo_endpoints.py`):
`/v2/users/{uuid}/followers` · `/v2/users/{uuid}/followingPaginate` ·
`/v2/users/{uuid}/recommendedUsers` · `/v2/users/userHandle/{handle}` ·
`/tokens/{networkId}/{tokenAddress}` · `/feed/token/thesis`

---

## 2. Pola per endpoint

### `GET /trades?userId=` — 21 pól, to jest serce systemu

Odpowiedź: `responseObject.activeTrades[]` + `responseObject.closedTrades[]`, każdy element ma
zagnieżdżony obiekt `trade`:

```
id                     networkId              tokenAddress           tokenMetadata
createdAt              closedAt               updatedAt              userAddress
sumSwapOpen            sumSwapClosed          sumTransferIn          sumTransferOut
humanTokenAmount       avgEntryPrice          avgExitPrice           avgTransferInPrice
avgTransferOutPrice    totalCostBasis         realizedPnlUsd         unrealizedPnlUsd
commentId
```

🔴 **Pułapki, każda kosztowała pomiar:**
- `networkId` Solany to **`1399811149`**. Literał `"solana"` w nagłówku `X-Supported-Chains`
  jest **cicho wycinany** i traci się z nim wszystkie pozycje solanowe.
- Nagłówek `X-Supported-Chains` **to FILTR HISTORII**, nie opis. Poprawna wartość:
  `1399811149,8453,56,143,4663`.
- `sumSwapOpen` to **ilość TOKENÓW**, nie dolary. Tożsamość
  `totalCostBasis == sumSwapOpen * avgEntryPrice` trzyma się w 1% na 142 862/143 548 wierszy base.
- `sumSwapOpen` to noga **SWAP**; `sumTransferIn`/`sumTransferOut` to osobna noga transferowa
  i `positions` jej **nie** utrwala.
- Jeden wiersz = jeden **EPIZOD** pozycji (open→close→reopen), nie cały token.
- `page` i `size` są **ignorowane**, `hasNextPage` zawsze `True` → patrz `README.md` §5.

### `GET /v2/users/{uuid}/balances`

```
tokenAddress           symbol                 humanAmountRemaining   priceUSD
marketCap              valuation              currentCostBasisUsd    currentRealizedPnlUsd
holdingSince           shiftedBalance         wasSwapped             includeInEquity
includeRealizedPnl     includeUnrealizedPnl   useLivePrice           naiveEvmBalances
otherPnl
```

⚠ `otherPnl` ma **klasę piedestału** — wartości absurdalnie duże, których próg
`FOMO_PNL_GARBAGE=1e9` nie łapie. Nie sumować bez filtra.

### `GET /v2/users/{uuid}/swaps`

```
swap_id   humanUsdAmountIn   humanUsdAmountOut   networkId   tokenAddress   createdAt
```

⚠ `/swaps` **podaje swapy Solana→Solana**. Korpus bot-gate ich nie ma z własnej winy
(nagłówek), a nie dlatego, że ich nie ma.

---

## 3. Pola użytkownika, które utrwalamy (`intel.db::wallets`, 72 kolumny)

Pochodzące z FOMO:

```
user_id            handle             display_name       avatar             twitter
rank               fomo_total_volume  lifetime_pnl_api   fomo_num_trades
fomo_swap_count    fomo_avg_hold_s    fomo_followers     fomo_following
fomo_verified      fomo_current_value_usd                fomo_holdings_priced
fomo_holdings_total                   fomo_value_ts
```

⚠ `fomo_swap_count` **NIE jest liczbą dożywotnią i nie jest prawdą** — nie używać jako
mianownika.
⚠ `user_id` FOMO **nie jest kluczem trwałym** — 40 „zniknionych" userów handluje dziś.

---

## 4. Co z tego naprawdę daje TIER — i co ma alternatywę on-chain

Operator wylicza wprost: *„jak nie ma hold time, pnl, ceny wejścia, wyjścia możliwości
policzenia, kelly, roi, wartości dolarowej"* — wallet jest nieprzydatny.

| potrzebne do tieru | pole FOMO | alternatywa z łańcucha |
|---|---|---|
| PnL zrealizowany | `realizedPnlUsd` | **jest** na robinhood/sol; base/bsc **nie** (8% wyceny) |
| cena wejścia | `avgEntryPrice` | **jest** — z receiptu, 144/400 zapisywalnych, mediana błędu 0,0% |
| cena wyjścia | `avgExitPrice` | jest, gdzie sprzedaż wyceniona |
| hold time | `createdAt` → `closedAt` | **jest** — granice epizodów odtworzone 4/4 co do minuty |
| ilość / koszt | `sumSwapOpen`, `totalCostBasis` | jest |
| kelly / ROI / expectancy | liczone z powyższych | jest |
| wartość dolarowa | `priceUSD`, `valuation` | robinhood/sol tak, base/bsc nie |
| **followers / following** | `fomo_followers/following` | **BRAK** |
| **verified / rank / leaderboard** | `fomo_verified`, `rank` | **BRAK** |
| **handle ↔ wallet** | `handle`, `userAddress` | tylko przez demiks, ~44% trafności |

🔑 **Jedyne, co NIE MA odpowiednika on-chain, to graf społeczny, leaderboard i mapowanie
handle→portfel.** Cała arytmetyka handlowa jest odtwarzalna z łańcucha. To ważne dla priorytetów:
jeśli scrape ma pobierać *coś jednego*, to **graf społeczny + leaderboard**, bo reszty można nie
brać od FOMO wcale.

---

## 5. Skala, która decyduje o banie

| | |
|---|---|
| użytkowników w `intel.db::wallets` | **14 131** |
| wierszy `trades` / `positions` | 527 154 / 527 153 |
| tier S/A/B (kohorta streamowana) | 346 |
| bez tieru, ale ≥10 pozycji (kandydaci do awansu) | 364 |
| pełny przebieg per-user na jednym koncie | **~5,4 h ciągłych requestów** = kształt wyzwalacza bana |
| `data/fomo_pnl_cumulative.jsonl` | 377,8 MB |

**Minimalna sensowna kohorta: 346 + 364 = 710 użytkowników.** Pełne 14 131 to inny rząd
wielkości i wymaga rozłożenia na dni.

---

## 6. Gdzie odsyłać zebrane dane

Rozszerzenie POST-uje dziś na `https://intel.lubiszto.win/hook/fomo_key`. Receiver bot-gate to
`~/ai/bot-gate/intel-fe/backend/server.py`. Nowy strumień danych powinien dostać **własny
endpoint** (np. `/hook/fomo_harvest`), a nie doklejać się do klucza — bo bot-gate musi umieć
odróżnić „dostałem token" od „dostałem dane" w logach i w prowenancji.

⚠ **Nie pisać wprost do `intel.db`.** Jest DROP-owana i przebudowywana co 15 minut przez
crona (linia 94), bez człowieka w pętli.
