# Discovery → Promotion gap — the "users escape us" plan (drafted 2026-09-17)

**Status 2026-09-24 19:0x BST — item 1 SHIPPED by bot-gate, the rest partly in motion (read before acting):**

| item | state on disk | evidence |
|---|---|---|
| 1 repoint builder | **SHIPPED** — #552 (a), architect 2026-09-17 10:59; kill switch `HARVEST_USERS_CONSUMER=0` | `bot-gate/intel-fe/backend/build_intel_db.py:2104-2134`; `data/fast-refresh-stdout.log` 44 union lines, last build `meta.generated_at` 09-24 18:03 BST: harvest users 49,895 · ADDED 47,119 (labelled PROFILE_UNTESTED via #511) · 46 withheld (handle worn by another row) |
| 2 leaderboard source | started — 16 of 49,895 `discovered_users` rows have `first_seen_kind='leaderboard'` (was 0 on 09-17) | `fomo_harvest.db` |
| 3 handle drift | detector exists (`bot-gate/scripts/detect_handle_drift.py`); cards #551 completed, #552 pending | board 66f77a54 |
| 4 resolve-handle task | 1 row `first_seen_kind='handle_resolve'`; #638 / #668 pending | board 66f77a54 |
| ⚠ store freshness | `max(discovered_users.last_seen_at)` = **2026-09-22 11:00 BST** — no new sighting for ~56 h at this read. Measured: since then the queue completed only `swaps` 23 · `lifetime_pnl` 7 · `trades` 1 — **zero `followers`/`following` pages**, and the discovery writer only runs on those. Last page of any kind 09-23 23:44 BST. WHY (measured 19:12): the author (`fomo_harvest_author.py`) has NO crontab line and its last `author:*` task was authored 2026-09-18 10:06; the extension polls get `queue_empty`. Dry-run: 34 followers + 160 following seeds ready. Operator 19:1x: he did NOT pause it. ⚠ Do NOT just cron it: since #552 the 47k union rows sit in `wallets` as PROFILE_UNTESTED and author lane (b) (`:617-656`, no min-trades/batch/403 gate) now counts **47,048** `/trades` candidates. Letter to bot-gate `20260924-191304` asks them to ground this before any `--apply` | `fomo_harvest.db::tasks`, `raw_pages` |

**Original status (2026-09-17): PLAN, not built.** Operator (2026-09-17) named this scraperx's MAIN job once the
demix / EVM+SOL address work is done: find new users (goal b) + detect handle changes
(goal c) so users stop escaping. Trigger: @wizardofsoho — a 17,311-follower / 616-trade
account the operator wanted on copy, which I first reported as "not on disk". He WAS on
disk — the gap below is why it looked otherwise.

## The escape, measured (fomo_harvest.db, 2026-09-17)

| fact | value |
|---|---|
| discovered_users total | 6,030 |
| …never promoted to `wallets` (`in_wallets_at_discovery=0`) | 2,677 (44%) |
| discovery via `following` / `followers` | 1,461 / 4,569 |
| discovery via **leaderboard** | **0** |
| wizardofsoho | discovered 09-16 13:15 via `following`; /trades fetched (task #226) → 78 positions in staging; NEVER promoted to intel.db |

**The escape is not "we never saw them" — it is "we saw them, sometimes even fetched them,
and never promoted them to the live tracked set."** We only *discover* through the social
graph of users we already track; we *promote* a subset; the rest sit in staging invisible to
anyone reading `wallets`/intel.db.

## Why wizardofsoho looked new (the trap to kill)

A duplicate/inventory check that reads only `wallets`/`trades`/`positions`/corpus MISSES the
discovery layer (`discovered_users`, `social_edges`, staging `positions`, done harvest tasks).
**Any "is this user new?" check must read the discovery/staging layers first**, or it reports
our own promotion gap as "new user" and reaches for a needless new fetch/permission.

## THE VERIFIED BREAK (measured 2026-09-17 — supersedes the earlier "promotion gates" guess)

**Two discovered-user stores; the intel.db builder reads the DEAD one.** This, not gate
tuning, is why wizardofsoho never reached `wallets`:

| store | who writes | rows | freshness | read by build_intel_db? |
|---|---|---|---|---|
| `data/discovered-users.jsonl` | `leaderboard_discover.py` | 1,602 | **STALE — last write Sep-12 19:02** | YES — this is its `DISCOVERED` source (`build_intel_db.py:34`) |
| `fomo_harvest.db::discovered_users` | harvest author (#517+ queue→extension) | 6,315 | LIVE (last_seen Sep-17 09:45) | only `profile_*` cols (`:2475`), NOT the user LIST |

So the new harvest-lane discovery fills a DB table the builder reads only for a few profile
columns; the builder's actual LIST of users comes from a JSONL that stopped updating Sep-12.
wizardofsoho is in the DB (6,315), absent from the JSONL (1,602) → invisible to the builder →
never promoted. Same failure shape as the two-switch pause / two intel.db files / two extension
loops: a new writer added, the old reader never repointed, divergence invisible until one case
(wizard) surfaces it.

## The plan (deep-dive each before building — post-demix; bot-gate owns build_intel_db)

1. **Repoint the builder to the live discovery store (bot-gate's build code — ground with them
   first).** `build_intel_db.py` should take its discovered-user LIST from
   `fomo_harvest.db::discovered_users` (6,315 live), not `discovered-users.jsonl` (dead Sep-12) —
   OR a step must sync the DB → the JSONL. THEN apply promotion criteria (signal: followers/
   trades/swaps + demix-confirmed pair) to graduate qualifying users into `wallets`. The
   `fomo_harvest_author.py` gates are a SECONDARY concern — moot while the builder can't see the
   live store at all.
2. **Leaderboard as a discovery source.** 0 of 6,030 discovered came from the leaderboard,
   yet the queue already has a `leaderboard` task-kind and it is a ranked list of *active*
   traders — the highest-yield discovery source we are not mining. Feed leaderboard rows into
   `discovered_users`.
3. **Handle-change HISTORY, keyed on the VERIFIED WALLET (goal c — operator 2026-09-17).**
   The operator's refinement: the durable anchor is the on-chain WALLET (EVM/SOL), NOT `user_id`
   and NOT the handle — both drift (bible: 40 "vanished" users trade under new ids). "Once we
   verify everyone's EVM+SOL, we can then ask: for THIS wallet, how did the nick change."
   MEASURED 2026-09-17: the spine ALREADY EXISTS — `intel.db::wallet_identity_history`, PRIMARY
   KEY `(address_key, chain, user_id)` (wallet-anchored), 50,378 rows, rebuilt daily, carrying
   `handle` (current) + `handle_aliases` (other handles seen for that user_id) + first/last_seen,
   addresses stored FULL. What is MISSING, not the anchor:
     - `handle_aliases` is a FLAT set, not a DATED timeline (handle A over [t0..t1], then B) — a
       true change-history needs (handle, seen_from, seen_to) rows, not a comma-joined field.
     - it is only as good as ATTRIBUTION COMPLETENESS: a PROFILE_UNTESTED/UNATTRIBUTED user has no
       verified wallet to anchor the timeline to yet. So the "lots of work ahead" is finishing
       attribution first (every user → a verified EVM+SOL), THEN the nick-timeline hangs off it.
     - `wallets.handle_prev/handle_seen_at` (bot-gate #552, build 14:00) is a FIRST step (one prior
       handle + when), a subset of the full timeline.
   **DETECTION SOURCE — CORRECTED 2026-09-17 13:0x, the first version of this line was wrong.**
   It said the goal-(c) drift probe (71× FomoScan-404 `/trades` tasks) feeds current-handle-vs-stored.
   A `/trades` response carries NO handle: `userHandle`/`displayName` appear in 0 of 83 `trades`
   pages and 0 of 28 `swaps` pages on our own disk, versus 48/49 `followers` and 49/50 `following`.
   The whole batch was un-runnable for its purpose; 12 open tasks were cancelled, 11 had completed.
   **The real detector costs nothing and is already fed:** `fomo_harvest_consume.py:104` writes
   `userHandle → discovered_users.handle` on EVERY followers/following page, COALESCE-upserted so the
   newest sighting wins. So drift = one join, `discovered_users.handle` vs `wallets.handle`, run by
   `~/ai/bot-gate/scripts/detect_handle_drift.py` (read-only, zero FOMO calls).
   Measured the same hour: 7,775 users carry a live handle on disk · 801 of them are in `wallets` ·
   **667 agree (positive control) · 134 drifted** · 87 with evidence newer than the last sweep ·
   101 attributed CONFIRMED/DEMIX_CONFIRMED · **0 of the 134 already known to
   `wallet_identity_history`** (control: it knew the handle for 660 of the 667 agreeing users).
   Report artifact: `~/ai/bot-gate/data/handle_drift_20260917.json`. The DELTA still lands in
   wallet_identity_history / #552 — bot-gate owns that table's build.
   Consequence for the plan: goal (c) does not need a FOMO spend at all. It needs the social-graph
   harvest to keep running (which it does) and a scheduled run of the join. The coverage limit below
   is therefore about DISCOVERY breadth (whose followers we have fetched), not about the scope gate.

   **COVERAGE LIMIT — measured the hard way 2026-09-17, do not re-plan around it.** A FOMO
   `/trades` drift probe CANNOT reach every tracked user: the server's own scope gate
   (`intel-fe/backend/fomo_harvest_store.py:752`, rule at `:611`, tier ladder `:636-648`)
   cancels a `/trades` task AT ISSUE with `scope_note = "| out_of_scope_at_issue"` unless the
   user is outside `wallets` or has `attribution_status` NULL/≠CONFIRMED, with custodial users
   further filtered by `custodial_trades_verdict(tier, n)` (operator decision D1). Seeding 71
   drift probes proved it: 43 were killed before ever being issued (n_issued=0), 23 stayed in
   scope, 5 completed. The pattern looked like a human bulk-cancel and was not — check
   `scope_note` before accusing anyone.
   Consequence for goal (c): FOMO-side drift detection covers only the in-scope slice. CONFIRMED
   and custodial C/D users need a different source for their handle (demix / FomoScan), or the
   operator widens D1 — his gate, his call. bot-gate's decision 2026-09-17 12:59 was (c): no gate
   exception; out-of-scope users get a `handle_stale_possible` label in #551/#552 (display-only in
   the FE; the copy basket does not read C/D handles, and S/A/B leaders pass the gate anyway).
4. **Resolve-handle as a queue task-kind, not a direct call.** Adding a user by bare handle
   currently forces a direct authenticated call (trips the ban/permission gate — 2026-09-17).
   Make handle→user_id a divisor-1 queue task the extension drains, so "add user X by handle"
   is permission-free and ban-safe like every other task. (bot-gate owns the extension +
   task schema; this is a request to them, not a scraperx build.)

## Grounding
- Measurements: `fomo_harvest.db` (discovered_users, tasks, positions), 2026-09-17.
- Author promotion logic: `~/ai/bot-gate/scripts/fomo_harvest_author.py` (discovered-user seed gates).
- Emission doctrine (who may fetch, three goals): `~/.claude/rules/bot-gate-fomo-authority.md`.
- Address classes (profile embed = class B, NOT trading): `~/.claude/rules/fomo-address-classes.md`.
