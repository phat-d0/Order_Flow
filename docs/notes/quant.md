# Quant agent notes

Append as you work. Entry format: `### YYYY-MM-DD — <decision|finding|problem|dead-end|done>: <title>` then bullets.
Newest entries at the bottom. The Lead merges summaries into `CLAUDE.md`.


### 2026-11-26 — decision: simulator model (Phase 3)
- Log mid x(t) = W(t) + P(t) + T(t) on a clock of step `dt_s`. W: Brownian, N(0, noise_sigma² dt) per grid step.
  Each trade (sign ε, size v) jumps x by κ·ε·v (κ = `impact_coef`, **log-return per share**); share φ =
  `permanent_fraction` goes to P (never decays), 1−φ to T. T is multiplied by a = exp(−ρ dt) at every grid time
  (exact discretisation of exp(−ρt)); trades land at continuous times between grid points.
  → propagator G(t) = κ(φ + (1−φ)e^{−ρt}) (Obizhaeva-Wang type, exponential resilience).
- Displayed book: latent bid edge B = mid − s/2, ask edge A = mid + s/2, s = `spread_ticks`·tick (integer).
  The book holds `depth` shares per tick beyond each edge; the best bid is the tick level just below B with
  size depth·fill (fill = part of the level inside the edge, in (0,1]); same for the ask. With integer
  spread_ticks bid fill + ask fill = 1, so (bid_sz+ask_sz)/2 = depth/2 exactly (except on the zero-duration
  boundary rows, where both levels are full; time-weighted depth ignores them); displayed spread = s + 1 tick.
- Quote rows: at grid times where a Poisson(`quote_rate`) clock fires **or** a trade follows in that step (book
  changes before a trade are published, like a real L1 feed), after every trade (same ts as the trade), and one
  zero-duration row at every tick boundary crossed between consecutive rows (same ts as the later row). The
  boundary rows make the CKS OFI telescope exactly: OFI = (2·depth/tick)·Δmid(latent).
- Noise trades: Poisson(`trade_rate`), Pareto sizes (tail `size_tail`, mean `size_mean`), random sign, executed
  at the displayed ask (buy) / bid (sell) of the pre-trade state.
- Metaorders: Poisson(`metaorder_rate`) starts; duration τ log-uniform on [`metaorder_min_duration_s`,
  `metaorder_max_duration_s`]; n = round(τ/Δc) children of constant size `size_mean`, every
  Δc = dt·round((1−p)/(p·trade_rate·dt)) s (p = participation; 4.5 s by default, a multiple of dt so decay
  between children is exactly a^{Δc/dt}). Start uniform so the metaorder ends inside the run. Signs random.
  The true metaorder table is returned as `SimResult.metaorders` (same columns as `build_metaorders`;
  p_start = latent mid before the first child, p_end = latent mid after the last).
- Why κ in log units and 1e-5: with the old 1e-4 *price* units, impact explained ~0.3% of 1-min variance and λ
  could not be recovered (SE ≈ 100% per 6h). With κ = 1e-5 log/share, 1-min Kyle regressions have R² ≈ 0.15–0.25
  (realistic) and SE ≈ 12% per 6h path.

### 2026-11-26 — decision: how each `truth` value is derived (closed form, never fitted)
- `rho`, `permanent_fraction`, `kappa`: the inputs.
- `kyle_lambda` (log-return per share, bar Δ = `impact.kyle_bar`): bar return regressed on bar signed volume.
  With i.i.d. signs, earlier bars' transient decay is uncorrelated with this bar's flow, so the slope is
  κ·(φ + (1−φ)·f), f = average fraction of a trade's transient left at bar end = (1−a^N)/(N(1−a)), N = Δ/dt.
  **Exact only when metaorder_rate = 0** (`truth['kyle_lambda_exact']`). With metaorders the sign-persistent
  flow makes the previous bar's decaying transient anti-correlated with this bar's flow: λ̂ ≈ 0.6–0.75× truth
  (seeds 1–3, default config). This is a real effect to remember for Phase 4 (λ under persistent flow is
  biased low at short bars).
- `ofi_beta` (price units per OFI/mean-depth): Δmid = tick/(2·depth)·OFI exactly; regressor = OFI / mean
  depth with mean depth = depth/2 → β = tick/4. Also `ofi_beta_per_share` = tick/(2·depth).
- `sqrt_Y`: the model is not a square-root law (OW impact of a metaorder ≈ linear in Q plus a constant), so Y is
  defined as the best √-law approximation under the estimator's weighting: expected impact of an n-child
  metaorder I(n) = κ·v·(φn + (1−φ)(1−a_c^n)/(1−a_c)), a_c = e^{−ρΔc}; x(n) = σ_d·√(n·v/V);
  Y = Σ_n P(n)·I(n)/x(n) with P(n) the exact pmf of n under log-uniform τ (matches `fit_sqrt_law`'s 1/x²
  weighting). σ_d = noise_sigma·√duration, V = expected total volume over the run (both in `truth`; pass these
  same values to `fit_sqrt_law`). The OLS-weighted version is `sqrt_Y_ols`.

### 2026-11-26 — finding: estimator choices
- `fit_sqrt_law`: OLS through the origin had SE understated ~2× (noise var ∝ duration ∝ Q). Switched to WLS
  with weights 1/x² ⇒ Y = mean(impact/x); OLS kept in `extra['Y_ols']`. Per 60 h path (≈430 metaorders) Y
  dispersion is ~11%; the gate pools 4 seeds.
- `fit_ofi`: regressor = OFI / (mean depth over the fit sample), CKS-style; per-window depth is an option.
  R² ≈ 0.98 on the sim (it is a pure book-shift world; real data will be far lower).
- `event_study` measures ε·log(mid(t0+h)/mid(t0−)) from the last quote strictly before the trade.
  ρ from one 24 h path has ~20% SE (price noise dominates at 30–300 s lags) → the gate pools 6 seeds by
  averaging event-study tables (n-weighted means, pooled SEs).
- Lee-Ready uses the last quote strictly before ts − lag (feeds stamp the post-trade quote with the trade ts).

### 2026-11-26 — finding: performance
- `.to_numpy()` on tz-aware datetime Series builds Python Timestamp objects (20M iterations on a 3-day sim).
  All core code now uses int64 ns via `metrics.ts_ns` / `floor_ns`: the 3-day demo (0.6M trades, 3.9M quote rows)
  dropped from 58 s to 10 s. Rule: never call `.to_numpy()` / `.dt.strftime` on timestamp columns in core.

### 2026-11-26 — done: Phase 3 core + quality gate
- Implemented every stub in `core/` + `core/demo.py` (end-to-end synthetic §3 output). Contract additions listed in
  `docs/CONTRACTS.md` §2 "Phase 3 additions"; config additions in `simulator:` (`start_ts`, `spread_ticks`,
  `metaorder_min_duration_s`, `metaorder_max_duration_s`) and `impact_coef` changed to 1e-5 **log-return per share**.
- Gate (`flow_model/tests/test_core_quality_gate.py`, tolerances from `quality_gate`):
  1. Kyle λ: metaorder_rate=0, 24 h, seed 11 (rel_tol_impact 0.2). sqrt-law Y: seeds 1–4 × 60 h pooled (≈1,750
     metaorders) (rel_tol_impact 0.2).
  2. ρ: seeds 1–6 × 24 h, event-study tables pooled (rel_tol_rho 0.25); AIC must prefer exponential.
  3. OFI β at 1s/10s/1min on the default path (seed 7) (rel_tol_ofi_beta 0.2).
  4. exchange flag = 100%, Lee-Ready ≥ 0.9 on the default path (quotes current, prints at the touch); tick rule
     ≥ 0.9 at noise_sigma 2e-5 (diffusion between trades ≪ spread), and lower at default noise (0.79).
     BVC has no accuracy threshold: it only sees flow that moves prices, so corr(BVC, true flow) ≈ √R²(Kyle)
     ≈ 0.45 on the sim — that is the theoretical ceiling, not a bug.
  5. OW < TWAP with ρ = 1/T; OW = TWAP (rel 1e-9) with ρ = None and ρ = 1e6; OW = TWAP if eta_temp = 0.
  6. `run_demo` (3 days → 2 train + 2 test dates) → `write_results` → `validate_results == []`.
- Robustness: re-ran gate estimators with seed offsets 0/100/200/300: worst |error| Kyle 11.5%, Y 5.7%, ρ 12.3%.
- `pytest`: 65 passed in ~35 s.

### 2026-11-26 — problem: known limitations (for the Lead / Phase 4)
- Kyle λ is biased low (~25%) when order flow is sign-persistent (metaorders): previous bars' decaying transient
  is anti-correlated with this bar's flow. Expect the same on real data; report λ at several bar sizes.
- The simulator is a pure book-shift world: OFI R² ≈ 0.98, spread constant, depth does not deplete after large
  trades (event-study spread/depth columns are flat on the sim). No hidden liquidity, no cancellations unrelated
  to price, no tick-size constraint on impact. Real-data OFI R² will be far lower.
- The simulator's impact is OW (linear + exponential decay), so the √-law Y is a projection and the free δ comes
  out ≈ 0.8–1.0, not 0.5. The gate checks the estimator, not that markets follow a √ law.
- `build_metaorders` (same-sign runs) cannot recover true metaorders when they are interleaved with noise
  trades; on the sim the gate uses the true labels. Real-data √-law fits on proxy runs measure something else
  (short-run impact) — flag it as a caveat.
- `fit_propagator` β is descriptive (log-log fit of the cumulative kernel); on the sim the kernel is exponential
  with a floor, so β has no "true" value.
- Tier C `ImpactModel.from_fits` assumes eta_temp = λ·price with no memory beyond one step (assumption, caveat).
