# Lead agent notes

Append as you work. Entry format: `### YYYY-MM-DD — <decision|finding|problem|dead-end|done>: <title>` then bullets.
Newest entries at the bottom. The Lead merges summaries into `CLAUDE.md`.

### 2026-11-26 — done: Phase 1 scaffold
- Studied the user's existing team layout (soccer / OU teams): a lead session spawns one cloud session per
  role, titled `<Team> team: <Role>`, tagged `<team>` + `<team>:<role>`, each on its own branch, reporting
  back to the lead via messages; the lead reviews and merges PRs. Mirrored it: `Order Flow team: <Role>`,
  tags `order-flow-team(:<role>)`, branches `agent/<role>`.
- Created CLAUDE.md (first), docs/CONTRACTS.md, configs, settings/schema/storage/loader base (+14 tests),
  core API stubs, asset/ui package stubs, agent definitions, notes files, Makefile, pyproject, .gitignore.

### 2026-11-26 — decision: layout details
- Package dirs follow the brief (`flow_model/{results,exports,tests,config}`), raw store at repo-root `data/`.
- `.gitignore` uses `/data/` (root-anchored) so `flow_model/data/` code is not ignored.
- Coinbase `level2` likely requires auth since 2023; crypto agent must test `level2`/`level2_batch` and fall
  back to `ticker` BBO+sizes if unauthenticated L2 is unavailable — no credentials.
- Coinbase `matches.side` is the maker side → aggressor = opposite (flagged in crypto brief).
- Equities default names AAPL, MSFT, NVDA beside SPY.
- Alpaca keys: user didn't choose local vs cloud secrets yet; loader supports both and skips cleanly if absent.

### 2026-11-26 — problem: GitHub default branch
- Pushing to the empty repo made `agent/quant` the default branch; no tool here can change it. Asked the user to set `main`.

### 2026-11-26 — finding: network policy
- All data hosts were proxy-blocked (CONNECT 403). User approved adding 8 exact hosts (see CLAUDE.md §4).
- Re-probe: Coinbase REST 200, Binance archive 200, Alpaca 401 (no keys), Dukascopy 429, Yahoo chart 429.

### 2026-11-26 — decision: Alpaca auth
- User declined the credential form; will add keys themselves. Loader must accept env-var secret OR
  proxy-injected `APCA-API-SECRET-KEY` (send placeholder). Key ID from env var `ALPACA_API_KEY_ID`.

### 2026-11-26 — done: Phase 2+3 sessions spawned
- Quant, Crypto, Equities, Commodities, FX sessions created on agent/<role>, each briefed to PR into main and message the Lead. IDs in CLAUDE.md §3.

### 2026-11-26 — done: reviewed + merged PR #1 (Equities, Alpaca loader)
- Checked: secrets never in repr/logs/errors, placeholder header for proxy, bounded retries + rate limiter,
  exclusive `end`, one-sided/crossed quotes counted not silent, 24 mocked tests; suite 38 passed.
- Follow-ups done by Lead: `conditions` column on trades schema, fixtures ownership rule, `make pull-equities`.
- Equities' decision request answered: keep Alpaca `c` in `conditions` (Phase 4 filtering).

### 2026-11-26 — done: merged PR #2 (Equities: conditions column populated)
- 40 tests pass. Equities on standby for Phase 4.

### 2026-11-26 — done: reviewed + merged PR #3 (Commodities: yfinance loader, appender, rolls)
- Mocked tests only (no live network), config-driven, idempotent appender; 64 tests pass on main.
- Findings folded into CLAUDE.md §4/§5/§7 (30-day 1m limit, crumb 429 harmless, zeroed open-bar volumes, roll offsets).
- Config additions accepted: sources.yaml yfinance.*, instruments.yaml roll_rule for CL=F/GC=F.

### 2026-11-26 — done: reviewed + merged PR #4 (Quant: core/ + quality gate)
- Re-ran suite on the branch (65 passed) and after merge (115 passed). Read execution.py (exact KKT/G⁻¹1 OW
  optimum, consistent cost model for all schedules) and ofi.py (correct CKS e_n, time-weighted depth).
- Accepted: core.yaml simulator keys + impact_coef unit change; CONTRACTS §2 "Phase 3 additions".
- Pytest note: `-q` in addopts + `-q` on CLI hides the summary line; use `-o addopts=""` for counts.
