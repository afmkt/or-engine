# or-engine

Single-day VRPTW dispatch service. Ingest an Excel install-list, geocode via
AMap, build a travel-time matrix, solve one day per worker with OR-Tools
(optional VRP + pickup/drop-off), export an Excel result. Served over REST,
MCP, and a standalone CLI.

## Install

```
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"      # fastapi, httpx, or-tools, openpyxl, pydantic, pytest
```

Set `AMAP_API_KEY` (in `.env` or the env) for live geocoding / travel times;
without it the standalone CLI falls back to deterministic pseudo-geocode +
a Euclidean matrix (clearly labelled in its output).

## Standalone dispatch CLI (one script)

> **Full CLI how-to: see [`docs/CLI.md`](docs/CLI.md)** (input schemas, one-shot
> and step-by-step usage, every option with its default, output sheets, modelling
> decisions, and troubleshooting).

`examples/dispatch_cli.py` is a single, self-contained entry point for the
whole dispatch. It runs the 7 decoupled stages in order, printing each stage's
intermediate data and writing a JSON checkpoint per stage (default `./_run`):

```
python examples/dispatch_cli.py run \
     -w docs/workers.xlsx -t docs/tasks.xlsx -H docs/working_hours.xlsx \
     --city 上海 --out result.xlsx --workdir ./_run
```

Run a single stage (loads the prior stage's checkpoint from `--workdir`):

```
python examples/dispatch_cli.py hours   -H docs/working_hours.xlsx --workdir ./_run
python examples/dispatch_cli.py workers -w docs/workers.xlsx        --workdir ./_run
python examples/dispatch_cli.py tasks   -t docs/tasks.xlsx -H docs/working_hours.xlsx --workdir ./_run
python examples/dispatch_cli.py geocode --city 上海 --workdir ./_run        # AMap, or --coords auto (offline)
python examples/dispatch_cli.py matrix  --workdir ./_run                    # AMap, or --matrix euclidean (offline)
python examples/dispatch_cli.py solve   --workdir ./_run --timeout-s 10
python examples/dispatch_cli.py export  --out result.xlsx --workdir ./_run
```

| # | stage | reads | writes | notes |
|---|-------|-------|--------|-------|
| 1 | parse-hours | `working_hours.xlsx` | `lookup.json`, `unknown.json` | product head → service hours |
| 2 | parse-workers | `workers.xlsx` | `workers_raw.json` | one start depot per worker |
| 3 | parse-tasks | `tasks.xlsx` + lookup | `tasks_raw.json`, `removed.json`, `qty_warnings.json` | product → hours; drop unmatched |
| 4 | geocode | tasks + workers | `tasks_geo.json`, `workers_geo.json` | AMap (`AMAP_API_KEY`; `--coords auto` fallback) |
| 5 | matrix | geocoded points | `matrix.json` | AMap directions (`--matrix euclidean` fallback) |
| 6 | solve | matrix + orders | `result.json` | best-fit; drop penalty ∝ service time |
| 7 | export | result | `result.xlsx` | 结果 / 汇总 / 未分配 / 移除 / 数量警告 |

Key decisions encoded (see `model.md` for the full problem statement):
- **Time**: no per-order start times; each order has only a service duration
  (from the 商品 → working-hours lookup). The optimiser picks the start times
  inside a fixed 08:00–18:00 window (`--window-start/--window-end`).
- **Best-fit subset**: only orders that fit within the window are scheduled;
  the rest are reported in 未分配 with drop penalty ∝ service time
  (`--drop-policy time` is the default).
- **Open route**: the 18:00 cutoff is the *last task finish*; the return-home
  leg is excluded from the objective and the reported travel totals.
- **Dropped tasks** (商品 unresolved, or an address that leaked into 商品) and
  **quantity warnings** (`--warn-qty`, default 5, flag-only) get their own sheets.

Output `result.xlsx` sheets: `结果` (route / stop / worker / arrival / finish),
`汇总` (per-worker summary), `未分配` (dropped), `移除` (removed tasks),
`数量警告` (quantity warnings).

## REST / MCP

```
python -m or_engine serve --port 8000    # REST + MCP on one port
python -m or_engine solve --db sqlite:///dispatch.db --out out.xlsx
```
`openapi.json` and `mcp.json` document every route.

## Examples

- `examples/dispatch_cli.py` — the standalone 7-stage CLI (above).
- `examples/smoke_test.py` — synthetic input → solve → print routes (OR-Tools).
- `examples/demo_routing.py` — real AMap geocode + matrix (needs `AMAP_API_KEY`).
- `docs/tasks.xlsx`, `docs/workers.xlsx`, `docs/working_hours.xlsx` — inputs.
- `docs/results.xlsx` — sample solve output.

## Tests

```
python -m pytest tests/ -q
```

* `tests/test_solver.py` — regression tests for the headline optimiser fixes
  (open route, time windows, optional/drop ∝ time, capacity, 60-min travel).
* `tests/test_tasks.py` — 商品-line parsing, address-leak / missing-product
  detection, blank-hours handling, quantity flagging, real-file import counts.

## Layout

```
src/or_engine/
  models.py          data model (Worker / Order / Route / SolveInput / DispatchResult)
  config.py          Settings (env / .env, incl. amap_city)
  tasks.py           商品-line parsing + 商品→working-hours resolution
  excel.py           workbook importers + result exporter (result + removed + qty sheets)
  engine.py          the orchestration (geocode → matrix → solve → dispatch)
  solver.py          OR-Tools VRPTW + capacity + optional/drop (best-fit) + greedy fallback
  spatial/           city bbox, distance, AMap client (directions matrix + caching)
  cli.py             the 7-stage pipeline + sub-commands (backing the standalone script)
  api/               FastAPI app and MCP server
main.py              top-level entry points (solve / serve)
examples/            dispatch_cli.py (standalone) + demo / smoke
tests/               pytest regressions
```
