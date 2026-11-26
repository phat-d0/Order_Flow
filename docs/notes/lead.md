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
