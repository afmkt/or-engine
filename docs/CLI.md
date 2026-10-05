# Dispatch CLI — how to use the standalone multi-day dispatcher

`examples/dispatch_cli.py` is a **single, self-contained script** that turns an
Excel install-list into a one-day routing plan. It runs the whole pipeline
**in memory — no database, no cache**: a few file transforms, one AMap
geocode + driving-distance call, and the OR-Tools optimiser.

```
working_hours.xlsx ─┐
workers.xlsx    ─────┼─►  [1..7]  ─►  result.xlsx  (+ 移除 / 数量警告 sheets)
tasks.xlsx      ─────┘
```

> It is a thin wrapper: it adds `src/` to the path and calls
> `or_engine.cli.main()`. You can also run the same commands as a module:
> `python -m or_engine.cli run …` (from the repo root, with the package
> installed).

---

## 1. Prerequisites

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # fastapi, httpx, or-tools, openpyxl, pydantic, pytest
```

Coordinates and travel times come from **AMap (高德)**. Either:

* **Online (recommended):** set your key and the pipeline uses live AMap geocode
  + AMap driving (with a 60-min per-leg cap):
```bash
echo 'AMAP_API_KEY=your_key' >> .env           # or: export AMAP_API_KEY=your_key
```
* **Offline (no key):** every step has a clearly-labelled fallback —
  a deterministic *synthetic* geocode into a 上海 bounding box and a Euclidean
  distance matrix. The demo still runs end-to-end; the console says so.

The three input workbooks ship in `docs/`:

| file | what it is |
|------|------------|
| `docs/working_hours.xlsx` | 商品 (product) → 工时（小时） service-time table |
| `docs/workers.xlsx` | the installers (workers) for the day |
| `docs/tasks.xlsx` | the install orders to route |

---

## 2. Input files (column layouts)

These are auto-detected by header name (English or Chinese, aliases allowed),
so extra / reordered columns are fine.

### `working_hours.xlsx`  — product → hours
| 商品 | 工时（小时） |
| ---- | ------------ |
| 平开门 | 1.25 |
| 三折叠门 | 3.00 |
| … | … |
| 垭口套（1.5米以上） | `-`  *(blank / `-` → that product is **dropped**, never 0)* |

A 商品 cell may list variants separated by `/`（, `、`）— each becomes its own
row. A blank or `-` hours value is reported and the task using it is *dropped*
(never silently 0).

### `workers.xlsx`  — the installers
| 师傅 | 常驻地址 | 出行工具 |
| ---- | -------- | -------- |
| 何玉利 | 浦东新区周浦镇沪南路3700号 | 开车 |

`出行工具` `开车` → **car** (`car_sh`), `电瓶车` → **ebike** (affects travel
time; a car is faster but may hit 上海 plate/time restrictions). Each worker is
their own route across as many 08:00–18:00 days as needed (each worker resets to their home depot every day).

### `tasks.xlsx`  — the install orders
| 序号 | 商家 | 地址 | 商品 | 联系人 | 订单总额 |
| ---- | ---- | ---- | ---- | ------ | -------- |
| 1 | 上海雅美乐 | 浦东新区创新中路593弄18栋1502室 | `平开门 x1（715x2110,双包套,内左开）` | 姚彬彬/18918558707 | 838 |

The **`商品` cell is the source of the order's service time**. It can hold
**multiple product lines (blank-line separated)**; the order's total service
time is the *sum* of its resolved lines. The `商品` line looks like
`平开门 x1（715x2110,双包套,内左开）`: a **product head** + an optional
**`xN` quantity** + a parenthesised **spec** (dimensions, opening side, …).
The parser strips the parens and the `xN`, then matches the *head* to
`working_hours`. The quantity is the `xN` **outside** the parens (a dimension
like `715x2110` is **not** mistaken for a quantity).

---

## 3. One-shot run

The simplest use — run all seven steps end-to-end, print a report, write the
output workbook:

```bash
python examples/dispatch_cli.py run \
      -w docs/workers.xlsx \
      -t docs/tasks.xlsx \
      -H docs/working_hours.xlsx \
      --city 上海 \
      --out result.xlsx \
      --workdir ./_run
```

*Steps 1–3 are pure file transforms (no key needed). Steps 4–5 (geocode,
matrix) use AMap when `AMAP_API_KEY` is set, else the offline fallback.*

A representative console report (offline sample, 144 orders, 6 workers):

```
[1/7] parse-hours    docs/working_hours.xlsx
       21 product heads -> hours     | 1 row(s) with unknown hours:
          - 垭口套（1.5米以上）
[2/7] parse-workers   docs/workers.xlsx
        6 workers     | by transport: {'car_sh': 2, 'ebike': 4}
[3/7] parse-tasks     docs/tasks.xlsx     (+ product->hours lookup)
       KEPT 144 tasks    |  REMOVED 22   {'missing-product': 20, 'address-leak': 2}
       每日服务总 614h  vs 单日能力 60h (10.2×)      | travel_obj=…s
       quantity warnings (N>=5, matched, flag-only): 2
[4/7] geocode        source=synthetic  city=上海   (no AMAP_API_KEY -> pseudo-geo)
[5/7] matrix         source=euclidean  N=150  11175 pairwise legs
[6/7] solve          engine=greedy-multi-day  status=optimal
       10 天排满   144/144 已排 · 0 未排          (value 优先级, 每天每工超窗 ≤1)
       已排 ¥60,462 / 全部 ¥60,462    | 每日服务 614h vs 60h 能力 (10.2×)    | travel_obj=…s
[7/7] export         -> result.xlsx
       sheets: 配置  结果  汇总  未分配(0)  移除(22)  数量警告(2)
```

---

## 4. Run the steps one by one (inspect / re-run any single step)

Each step is its own sub-command. After any step, its output is written as a
**JSON checkpoint into `--workdir`** (default `./_run`), so you can inspect the
intermediate data and re-run just one step later:

```bash
python examples/dispatch_cli.py hours   -H docs/working_hours.xlsx --workdir ./_run
python examples/dispatch_cli.py workers -w docs/workers.xlsx        --workdir ./_run
python examples/dispatch_cli.py tasks   -t docs/tasks.xlsx -H docs/working_hours.xlsx --workdir ./_run
python examples/dispatch_cli.py geocode --city 上海  --workdir ./_run     # AMap, or --coords auto
python examples/dispatch_cli.py matrix                       --workdir ./_run     # AMap, or --matrix euclidean
python examples/dispatch_cli.py solve                         --workdir ./_run     # --timeout-s N optional
python examples/dispatch_cli.py export   --out result.xlsx     --workdir ./_run
```

Each step **re-loads the prior step's checkpoint from `--workdir`**, threads the
in-memory state forward, and prints a human-readable summary of that stage's
output. The checkpoint files in `./_run`:

| step | console summary prints | checkpoint file(s) |
| ---- | ---------------------- | ------------------ |
| 1 `hours`   | product heads, `unknown` hours rows | `lookup.json` |
| 2 `workers` | worker count, by-transport | `workers_raw.json` |
| 3 `tasks`   | kept / removed (with reasons), qty warnings | `tasks_raw.json`, `removed.json`, `qty_warnings.json` |
| 4 `geocode` | points resolved / failed | `workers_geo.json`, `tasks_geo.json`, `meta.json` |
| 5 `matrix`  | N nodes, pairwise leg count | `matrix.json` |
| 6 `solve`   | scheduled / dropped, capacity ratio, per-worker routes | `result.json` |
| 7 `export`  | output sheet list | (writes `result.xlsx`) |

> `tasks` / `geocode` will auto-run an earlier missing step from its inputs;
> `matrix` and `solve` require the prior checkpoint (run the earlier steps or
> `run` first) and error clearly otherwise.

---

## 5. Options

The same options apply to every sub-command (`-h` shows them; defaults in
brackets).

| option | short | default | meaning |
| ------ | ----- | ------- | ------- |
| `--workers` | `-w` | `docs/workers.xlsx` | workers workbook |
| `--tasks`   | `-t` | `docs/tasks.xlsx`   | tasks workbook |
| `--hours`   | `-H` | `docs/working_hours.xlsx` | product→hours workbook |
| `--city` | — | `上海` | AMap city for geocode / driving |
| `--out` | — | `result.xlsx` | output workbook |
| `--workdir` | — | `./_run` | per-step JSON checkpoints dir |
| `--day-start` | — | `08:00` | per-day window start (first-class daily time bucket; same window + same home depot every day) |
| `--day-end`   | — | `18:00` | worker day end (**open route**: the last task must finish by this time; the return leg is not counted) |
| `--max-orders` | — | *unbounded* | per-day per-worker cap on number of stops (rarely needed; the day window usually binds first). It bounds **how far each day** fills, hence how many days the loop needs |
| `--timeout-s` | — | *run till solved* | OR-Tools wall-clock budget (seconds); reported in the output |
| `--warn-qty` | — | `5` | flag `商品` lines whose parsed quantity ≥ this (flag-only, not capped) |
| `--drop-by` | — | `value` | fill/drop priority: **`value` (default)** → keep highest-value orders first (penalty ∝ 金额), `time` → keep high-time jobs, `count` → flat penalty. When a day is tight, **low-value / short jobs drop first** |
| `--max-days` | — | `3650` | multi-day loop safety cap (default large ⇒ "as many days as needed" until all are scheduled or no progress is possible) |
| `--depot-fallback` | — | `121.4737,31.2304` | central depot (`lng,lat`) used as a worker's home when they have no coordinate; default = Shanghai centre. Each worker still keeps *its own* depot across every day |
| `--penalty-base` | — | `1e7` | base drop penalty (large ⇒ "schedule as many as fit", then trim) |
| `--per-hour-k` | — | `1.0` | additive penalty per service-second (drop-penalty ∝ time) |
| `--coords` | — | `auto` | coord source: `amap` (require key) / `auto` (synthetic if no key) |
| `--matrix` | — | `auto` | travel-matrix source: `amap` / `euclidean` / `auto` (amap if key else euclidean) |

---

## 6. Output

### `result.xlsx` — a config sheet first, then full per-stop / per-report rows
A dispatcher should be able to open a sheet and act on it. **`配置`** is written
first so the whole run is visible at a glance; the rest follow.

| sheet | contents |
| ----- | -------- |
| **配置** | **the run + daily time bucket + per-worker depots.** Sections: 运行 (city / 坐标来源 / 矩阵来源 / 引擎 / 模式), **每日时间窗** (`--day-start`~`--day-end`, 实际使用天数, 终止原因, 每天每工超窗限额, 中心回退 depot), **丢弃/排入优先级** (`--drop-by`, base, k, 阈值, 最大天数), **统计** (订单总数 / 已排 / 未排 / 移除 + 已排¥ / 未排¥ / 总¥ / 利用率, and unassigned reasons when <100%), **师傅 × depot** (每工 坐标 + 来源〔已提供坐标 / 回退中心depot〕 + 可用时窗 + 日上限 + 总派单数), **每日概览** (天 / 派单数 / 服务工时 / 金额 / 超窗次数) |
| **结果** | every assigned stop, **full order info** (14 cols): **日期 / 师傅 / 师傅ID / 顺序 / 订单编号 / 订单类型 / 商家 / 联系电话 / 安装地址 / 金额(¥) / 做单时长(h) / 预计上门 / 预计完工 / 是否超窗** — 联系电话 keeps the sheet's single “name/phone” value as-is |
| **汇总** | per **(天, 师傅)** totals: **日期 / 师傅 / 师傅ID / 运输工具 / 派单数 / 总服务工时(h) / 总路程时长(min) / 总路程(km)** |
| **未分配** | every dropped order, **full info**: 订单编号 / 订单类型 / 商家 / 联系电话 / 安装地址 / 金额(¥) / 做单时长(h) / 备注 (the 未排原因 is also summarised on 配置) |
| **移除** | tasks removed during parse, **full info**: 订单编号 / 商家 / 联系电话 / 安装地址 / 金额(¥) / 做单时长(h) / 未匹配·非法商品行 / 原因 (`missing-product` / `address-leak` / `unknown-hours`) |
| **数量警告** | `商品` lines whose parsed quantity ≥ `--warn-qty` (flag-only, matched products) |

The console also prints a **per-day** breakdown (天 / 派单数 / 服务工时 / 金额),
an **end reason** (`all-scheduled` / `cap … days` / `stalled` / `no-progress`),
and the capacity ratio (total on-site service vs. **daily** worker-hours).

---

## 7. Modelling decisions baked into the CLI

These are the confirmed behavioural choices — override via the options above.

* **No per-order start time.** `tasks.xlsx` has no scheduled-start column; the
  optimiser *chooses* each order's start. An order contributes only its
  **service duration** (from 商品 → `working_hours`), inside a fixed
  08:00–18:00 window.
* **As many days as needed, fill each day.** Each day the fleet's clock
restarts at `--day-start`; the engine fills day 1, then **overflows the
rest to day 2, 3, …** until every order is placed (or no day can make
progress). Workers keep the **same home depot and the 08:00–18:00 window
every day** (`--depot-fallback` supplies a central depot when a worker has
no coordinate).
* **Value-priority drop** (`--drop-by value`, **default**). When a day is
tight it keeps the **highest-value orders and drops the cheap / short ones
first** — the inverse of the old "drop the short jobs first". `--drop-by
time` (∝ service time) and `--drop-by count` (flat) remain available.
* **Open, over-long-tolerant routes.** A per-day route is *open* (the
return leg is excluded from the objective / travel total) and may carry
**at most one over-long order** (a stop finishing past `--day-end`) per
worker per day, so no single long job is forced unassigned.

* **Open route.** The 18:00 cutoff is the **last task's finish**; the
  home-return leg is excluded from both the objective and the reported travel
  total. Workers re-enter from their latest task's coordinates after each stop.
* **Unmatched 商品 is an error.** A product line that maps to no row in
  `working_hours` (or an address that leaked into the 商品 cell — `address-leak`,
  e.g. a 门牌号 string) **removes that whole task** and is listed in 移除.
* **Blank / `-` hours is an error, never 0.** A product present in the table but
  with blank hours (e.g. 垭口套 1.5米以上) is reported; a task using it is dropped.
* **Quantity is checked (decision #7).** Lines with a parsed quantity ≥
  `--warn-qty` (default 5) are **flagged** (数量警告) but the quantity is taken
  at face value — not capped or dropped.

---

## 8. Troubleshooting / FAQ

* **"matrix(amap) needs AMAP_API_KEY"** — you asked for AMap travel times with
  no key. Set `AMAP_API_KEY`, or pass `--matrix euclidean` / `--coords auto`
  (offline fallback).
* **A task I expected is missing / is in 移除** — check its 商品 cell: it maps to
  no `working_hours` row (`missing-product`), or the cell holds an address that
  leaked in (`address-leak`), or it has blank hours. These are deliberate
  removals, always reported.
* **Spreads across many days** — the sample is 614 h of service vs. 60 h/day of
6 workers (08:00–18:00), i.e. ~10× capacity. The multi-day engine fills the
days (day 1, 2, …), so by default **all orders are placed** (`终止=all-
scheduled`, 0 in 未分配). To see a single day instead, cap with `--max-days`
or shorten `--day-end`; what can't fit then lands in **未分配** (full info)
and its ¥ total appears in the 配置 统计 section.
* **`engine=greedy-multi-day`** — the multi-day engine is a fast, per-day
greedy (value-priority fill, one over-long overflow per worker/day) that
scales to thousands of orders; it is what the CLI now uses. The single-day
`solve_dispatch` OR-Tools path remains for the API. The console and
`result.json` always name the engine that produced the result.
* **Re-run a single step** — point it at `--workdir ./_run`; it loads the prior
  checkpoints and only re-runs its own stage (e.g. tune `--penalty-base` and
  re-run `solve` without re-geocoding).

---

## 9. Related

* `model.md` — the full problem statement, input/output schema, optimisation
  model, and design decisions.
* `examples/smoke_test.py` — a tiny synthetic input → OR-Tools → printed routes.
* `examples/demo_routing.py` — a real AMap geocode + driving-matrix run
  (sets `--matrix amap`; needs a key).
