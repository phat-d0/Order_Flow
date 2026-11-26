# Interface contracts

Single source of truth between agents. The Lead reviews every deliverable against this file.
To change a contract, propose it to the Lead in your report; only the Lead edits §0, §1, §3, §4, §5;
the Quant agent may edit §2 (minimally, listing every change in its report).

Research use only: no broker connections, no orders. Free data only.

---

## 0. Ownership

| Path | Owner | Others may |
|---|---|---|
| `CLAUDE.md`, `docs/CONTRACTS.md`, `flow_model/config/`, `flow_model/settings.py`, `Makefile`, `pyproject.toml`, `run_all.py`, `REPORT.md`, `flow_model/exports/` | Lead | propose changes |
| `flow_model/data/schema.py`, `storage.py`, `loaders/base.py` | Lead | import only |
| `flow_model/core/`, `flow_model/tests/test_core_*.py` | Quant | import only |
| `flow_model/data/loaders/{coinbase,binance}.py`, `recorders/coinbase_ws.py`, `assets/crypto/`, `tests/test_crypto_*.py` | Crypto | — |
| `flow_model/data/loaders/alpaca.py`, `assets/equities/`, `tests/test_equities_*.py`, `tests/fixtures/equities/` | Equities | — |
| `flow_model/data/loaders/yfinance_bars.py`, `recorders/bar_appender.py`, `assets/commodities/`, `tests/test_commodities_*.py` | Commodities | FX imports |
| `flow_model/data/loaders/dukascopy.py`, `assets/fx/`, `tests/test_fx_*.py` | FX | — |
| `flow_model/ui/`, `tests/test_ui_*.py` | UI | — |
| `flow_model/results/<asset_class>/` | that asset agent | UI reads only |
| `docs/notes/<agent>.md` | that agent | Lead reads/merges |

Each agent also owns `flow_model/tests/fixtures/<its-role>/` (small committed test fixtures only).
Asset agents may add keys under their own section of `flow_model/config/instruments.yaml` /
`sources.yaml` and must tell the Lead. Nobody else edits `core.yaml` except via the Lead.

---

## 1. Data layer

### 1.1 Schemas (`flow_model/data/schema.py`)
Timestamps: `timestamp[ns, tz=UTC]`. `recv_ts` = local receive time (null for historical downloads).
`side`: +1 buyer-initiated (aggressor bought), −1 seller-initiated, 0 unknown.

| Kind | Columns (dtype) |
|---|---|
| `trades` | ts, symbol(str), source(str), venue(str), price(f64), size(f64), side(i8), trade_id(str, nullable), conditions(str, nullable: source condition codes comma-joined, e.g. Alpaca `c`; used to filter odd-lots/out-of-sequence), recv_ts(ts, nullable) |
| `quotes` | ts, symbol, source, venue, bid_px(f64), bid_sz(f64, NaN ok), ask_px(f64), ask_sz(f64, NaN ok), recv_ts |
| `l2` | ts, symbol, source, venue, book_side(i8 +1 bid/−1 ask), price(f64), size(f64; new total at level, 0 = removed), is_snapshot(bool), recv_ts |
| `bars` | ts(bar open), symbol, source, venue, interval(str, e.g. "1m"), open, high, low, close, volume (f64) |

API: `SCHEMAS: dict[str, pa.Schema]`, `empty_frame(kind)`, `coerce(df, kind) -> pd.DataFrame`,
`validate(df, kind) -> list[str]` (empty = valid; checks columns, dtypes, UTC tz, sorted `ts`,
side ∈ {−1,0,1}, positive prices, `bid_px <= ask_px` for quotes).

### 1.2 Storage (`flow_model/data/storage.py`)
Raw store: `data/<source>/<symbol>/<kind>/<YYYY-MM-DD>.parquet` (UTC date of `ts`; gitignored).
Symbols are path-sanitised (`=`, `/` → `_`).
- `write_partitioned(df, kind, root=None, mode="append") -> list[Path]` — validates, splits by UTC date,
  appends + de-duplicates (on all columns except `recv_ts`) and sorts by `ts`.
- `read_range(source, symbol, kind, start=None, end=None, root=None) -> pd.DataFrame` — half-open `[start, end)`.
- `list_dates(source, symbol, kind, root=None) -> list[date]`.

### 1.3 Loader interface (`flow_model/data/loaders/base.py`)
Every source implements `Loader`:
```python
class Loader(ABC):
    source: str                 # e.g. "coinbase"
    venue: str                  # e.g. "coinbase" / "iex" / "dukascopy" / "yahoo"
    tier: str                   # "A" | "B" | "C"
    kinds: frozenset[str]       # subset of {"trades","quotes","l2","bars"}
    def fetch(self, kind: str, symbol: str, start: datetime, end: datetime) -> pd.DataFrame: ...
    def check(self) -> SourceCheck: ...   # live reachability probe, never raises
```
`fetch` returns a schema-valid frame or raises `NotSupported` / `SourceUnavailable`.
`load(kind, symbol, start, end)` (concrete, in base) reads the local store first and only fetches
missing dates. Upgrading to a paid feed = new `Loader` subclass + config entry. Nothing in `core/` changes.

---

## 2. `core/` engine API (Quant)

All inputs are schema frames (§1.1) or plain pandas/numpy. All params come from `config/core.yaml`
(passed in as `cfg: dict` or explicit kwargs; no module-level numeric constants).
Every fit returns a `FitResult` (or a dataclass with `to_dict()` that is JSON-safe: NaN/inf → null).

### `metrics.py`
- `@dataclass FitResult`: `name: str, params: dict[str, float], se: dict[str, float], r2: float | None, n: int, extra: dict`, `to_dict()`.
- `ols_fit(y, X, add_constant=True, hac_lags: int | None = None) -> FitResult`
- `train_test_split_by_date(df, train_frac: float, ts_col="ts") -> tuple[pd.DataFrame, pd.DataFrame, dict]` — splits on whole UTC dates; third item = `{"train": [start, end], "test": [start, end]}`.

### `simulator.py`
- `@dataclass SimParams` with `from_config(cfg)`: duration, tick size, depth, impact coefficient, resilience ρ, permanent fraction, noise σ, trade arrival rate, size distribution, metaorder settings, seed.
- `simulate(params: SimParams) -> SimResult` where `SimResult` has `trades` (schema `trades`, true `side`), `quotes` (schema `quotes`), `l2: pd.DataFrame | None`, `truth: dict` with at least keys `kyle_lambda`, `sqrt_Y`, `rho`, `ofi_beta`, `permanent_fraction`.

### `signing.py`
- `exchange_side(trades) -> pd.Series` (int8; passes through `side`)
- `tick_rule(trades) -> pd.Series`
- `lee_ready(trades, quotes, quote_lag: pd.Timedelta) -> pd.Series`
- `bulk_volume_classification(bars, window: int, dist: str = "t", dof: float | None = None) -> pd.DataFrame` → `buy_volume, sell_volume, signed_volume`
- `sign_trades(trades, quotes=None, method="auto", **kw) -> pd.Series` — auto: exchange flag if non-zero, else Lee-Ready if quotes, else tick rule
- `signing_accuracy(true_side, inferred_side) -> dict` → `accuracy, accuracy_volume_weighted, n, confusion`

### `ofi.py` (Cont-Kukanov-Stoikov)
- `event_ofi(quotes) -> pd.Series` — per-quote-update e_n from changes in best bid/ask price & size
- `aggregate_ofi(quotes, window: str) -> pd.DataFrame` → `ofi, dmid, depth, n_events` per window
- `fit_ofi(agg) -> FitResult` — ΔP on OFI/depth; params `beta`, `alpha`
- `ofi_by_window(quotes, windows: list[str]) -> list[FitResult]`

### `impact.py`
- `build_metaorders(trades, max_gap: pd.Timedelta, min_trades: int) -> pd.DataFrame` → `start, end, sign, Q, n_trades, p_start, p_end, impact` (impact = sign·log(p_end/p_start))
- `fit_sqrt_law(metaorders, sigma_daily, volume_daily) -> FitResult` — params `Y` (δ fixed 0.5) and `delta` (free fit) in `extra`
- `kyle_lambda(returns, signed_volume) -> FitResult` — param `lambda`
- `amihud(returns, dollar_volume, freq: str) -> FitResult` — param `illiq`

### `resilience.py`
- `large_trade_events(trades, quantile: float) -> pd.DataFrame`
- `event_study(events, trades, quotes, horizons: list[pd.Timedelta]) -> pd.DataFrame` → `lag_s, price_reversion, spread, depth, n`
- `fit_exponential(decay) -> FitResult` — param `rho` (1/s), `half_life_s` in extra, AIC in extra
- `fit_power_law(decay) -> FitResult` — param `beta`, AIC in extra
- `fit_propagator(trades, max_lag: int) -> FitResult` — G(ℓ) ∝ ℓ^−β fitted on trade-time response
- `compare_resilience(exp_fit, pl_fit) -> dict` → `preferred, delta_aic`

### `execution.py`
- `@dataclass ImpactModel`: `lambda_perm, eta_temp, rho, depth, sigma, spread` + `from_fits(fits: dict, cfg) -> ImpactModel`
- `@dataclass Schedule`: `times, trades, name`
- `twap(Q, T, n)`, `vwap(Q, T, n, volume_profile)`, `almgren_chriss(Q, T, n, model, risk_aversion)`, `obizhaeva_wang(Q, T, n, model)` → `Schedule`
- `expected_cost(schedule, model) -> float` (in price units × shares; also bps via helper)
- `compare_schedules(Q, T, n, model, volume_profile=None) -> pd.DataFrame` → `schedule, cost, cost_bps, savings_vs_twap_bps`

### `seasonality.py`
- `intraday_profile(df, value_col, bucket: str, ts_col="ts", tz=None, session=None) -> pd.DataFrame` → `bucket, mean, median, n`
- `deseasonalize(df, profile, value_col, bucket) -> pd.Series`

### `results.py`
- `write_results(out_dir, fits: dict, tables: dict[str, pd.DataFrame]) -> Path`
- `validate_results(out_dir) -> list[str]` (empty = valid against §3)

---

## 3. Output contract (`flow_model/results/<asset_class>/<symbol>/`)

### `fits.json`
```json
{
  "symbol": "BTC-USD", "asset_class": "crypto", "tier": "A",
  "sources": [{"source": "coinbase", "venue": "coinbase", "kinds": ["trades", "quotes", "l2"], "ts_precision": "us"}],
  "caveats": ["..."],
  "data": {"start": "...", "end": "...", "n_trades": 0, "n_quotes": 0, "n_bars": 0,
           "gaps": [{"start": "...", "end": "...", "kind": "trades", "reason": "..."}]},
  "split": {"train": ["YYYY-MM-DD", "YYYY-MM-DD"], "test": ["YYYY-MM-DD", "YYYY-MM-DD"]},
  "methods": {
    "<method>": {"status": "ok|not_applicable|insufficient_data|failed",
                 "reason": "text or null",
                 "in_sample": {}, "out_of_sample": {}}
  },
  "generated_at": "ISO-8601 UTC", "config": {}
}
```
`<method>` ∈ `signing, ofi, sqrt_law, kyle_lambda, amihud, resilience, execution, seasonality`.
Every method key must be present; methods the tier cannot support have `status: not_applicable` and a reason.
`in_sample`/`out_of_sample` hold `FitResult.to_dict()` (or list of them for OFI windows; `{"exponential", "power_law", "propagator", "comparison"}` for resilience; `{"rows": [...]}` for execution).

### Chart tables (parquet; write only those that apply)
| File | Columns |
|---|---|
| `impact_curve.parquet` | `x_sqrt_q_over_v, impact_norm, n, sample, fitted` (binned) |
| `ofi_bins.parquet` | `window, ofi_norm_bin, dmid_mean, dmid_se, n, sample` |
| `resilience_decay.parquet` | `lag_s, price_reversion, spread, depth, n, exp_fit, pl_fit, sample` |
| `intraday_profile.parquet` | `bucket, volume, spread, volatility, n` |
| `execution.parquet` | `order_size_pct_adv, schedule, step, time_frac, trade_frac, cost_bps, savings_vs_twap_bps` |

`sample` ∈ {"IS", "OOS"}.

---

## 4. Asset module contract

Each `flow_model/assets/<asset_class>/__init__.py` exposes:
```python
def run(symbols: list[str] | None = None, results_root: Path | None = None) -> list[Path]:
    """Load stored data, run every core method the tier allows, write §3 results, return result dirs."""
```
`run_all.py` calls crypto, equities, commodities, fx in turn; a failure in one class is logged and
recorded, not fatal to the others.

## 5. UI contract
`flow_model/ui/app.py` reads only `flow_model/results/**` and `flow_model/exports/**` (+ `config/ui.yaml`).
It never downloads data. It may import `flow_model.core.simulator` and `flow_model.core.execution`
for the simulator tab only.

## 6. Export contract (`flow_model/exports/impact_params.json`, Phase 5)
```json
{"generated_at": "...", "units": {...},
 "instruments": {"<symbol>": {"tier": "A", "Y": 0.0, "rho_per_s": 0.0, "kyle_lambda": 0.0,
                               "typical_spread_bps": 0.0, "source": "...", "status": {...}}}}
```
Missing values are `null` with a reason in `status`.
