# model.md — single-day installation-worker dispatch (VRPTW-with-drop)

> A single day of installation work: route a set of **workers** (安装师傅) to a set
> of **order tasks** (订单), one order site visited by at most one worker, so that
> travel is minimised and as much work as possible fits inside the working day.
> Because the day is too small for all the work, the solver may **drop** tasks,
> dropping the *least valuable* ones first. This is a **Vehicle-Routing Problem
> with Time Windows and a drop option** (a.k.a. partial / selective VRP).

---

## 1. The story (what we are optimising for)

Given today's orders and crew, decide *which* task *which* worker does *when*:

- **(a) minimise travel** — total travel time/distance across all routes is small.
- **(b) do as much as possible** — fit as many tasks as the day allows.
- **(c) drop by value** — when a task must be dropped (it won't fit), the penalty
  is **proportional to that task's on-site time**, so short tasks are dropped
  before long ones (we keep the high-value work).
- **(d) sequential chaining** — once a worker is on a task they are busy until it
  finishes, then continue from that task's location (their latest lat/lng). A
  worker may take several tasks in a chain; the route accumulates time until the
  18:00 cutoff.

The **start time of every task is chosen by the solver** (the input carries no
start time — see §4) and reported in the output, so the crew can be told
"your first appointment is 08:30 at …, then 11:05 at …".

---

## 2. Inputs

Three spreadsheets. Headers are matched loosely (English or Chinese aliases,
blank leading rows skipped, first non-empty row = header). Coordinates are
**not** in the files — addresses are geocoded via AMap/高德 (§2.4).

### 2.1 `workers.xlsx` — crew (安装师傅)

| column | canonical | type | required | notes / aliases |
|---|---|---|---|---|
| 姓名 | `name` | str | ✔ | 师傅 |
| 常驻地址 / 常驻位置 | `home_address` | str | ✔ | geocoded to `home_point`; 地址 / home |
| 出行工具 | `transport` | enum | – | `car_sh` / `car_out` / `ebike`. 电瓶车→ebike, 开车→car. default `car_sh` |
| 最早出工 | `available_start` | HH:MM | – | **unused here** — overridden by `--day-start` |
| 最晚完工 | `available_end` | HH:MM | – | **unused here** — overridden by `--day-end` |
| 电话 | `phone` | str | – | carried for the demo |
| 最大套数 | `max_orders` | int | – | per-worker cap; **CLI `--max-orders` overrides** |

Real sample (`docs/workers.xlsx`, 6 rows):

```
师傅        常驻地址                     出行工具
何玉利       浦东新区周浦镇沪南路3700号     开车
杜豪豪       松江洞泾镇欣业路5号           开车
邱宏靖       徐汇区虹梅南路1781弄          电瓶车
...
```

### 2.2 `tasks.xlsx` — orders (订单)

| column | canonical | type | required | notes / aliases |
|---|---|---|---|---|
| 序号 | `order_no` | str | ✔ | natural key |
| 商家 | `merchant` | str | – | – |
| 地址 | `site_address` | str | ✔ | geocoded to `site_point` |
| **商品** | `product` | str | ✔ | multi-line list of items — drives the service time (§3) |
| 联系人 | `contact` | str | – | usually `姓名/电话` |
| 订单总额 | `amount` | float | – | order total value |

> **No start-time / date column exists.** (Verified across all 251 rows × 6 cols:
> the only headers are 序号·商家·地址·商品·联系人·订单总额, and no cell is a
> `datetime`.) Per-order time windows are therefore **not** an input; the solver
> picks start times within the working day (§4).

Real sample (`docs/tasks.xlsx`, 251 rows):

```
序号  商家        地址                          商品                              联系人           订单总额
1     上海雅美乐   浦东新区创新中路593弄18栋1502室  平开门 x1（…）            姚彬彬/18918558707  838
                                          两移门 x1（…）
                                          垭口套 x1（…）
2     上海雅美乐   浦东新区听悦路960弄5号302室     一字淋浴房（三片及以上） x1（…）
```

### 2.3 `working_hours.xlsx` — on-site labour hours per product (工作工时表)

| column | canonical | type | required |
|---|---|---|---|
| 商品 | `product` | str | ✔ |
| 工时（小时） | `hours` | float | ✔ |

Real sample:

```
商品                       工时（小时）
一字淋浴房（两片）            1.25
一字淋浴房（三片及以上）       1.5
L型/钻石型/扇形             1.25      ← one row, THREE variants (split on /)
T型（三片）                 1.5
平开门（含双包套）            1.25
两移门                       2.0
三移门                       3.0
垭口套（1.5米内）             1.0
垭口套（1.5米以上）           -        ← '-' / blank = UNKOWN/legacy → see §3
```

### 2.4 Geocoding (AMap / 高德 Web Service)

Every `home_address` and `site_address` with no coordinate is resolved with
`geocode/geo?city=<--city>` (default **上海**), taking the top hit. `transport`
does not change the base route — it changes the *speed* applied to AMap's
**driving** duration (see config `TRANSPORT_SPEED_FACTORS`):

| mode | factor | meaning |
|---|---|---|
| `car_sh` | 1.0 | ordinary SH-plate car (base) |
| `car_out` | 1.15 | out-of-town plate — city restrictions |
| `ebike` | 1.6 | e-bike is slower |

---

## 3. Service-time resolution (商品 → hours)  【step: `parse-tasks`】

For each task, parse the `商品` cell into on-site service hours `s`:

1. **Split** the cell on newlines (and `\r`) into product *lines*.
2. For each line, extract **quantity** `N` from a `xN` / `×N` token (default 1)
   and the **product head** by stripping the `xN` and the `（…）` spec, and
   trimming whitespace. (e.g. `两移门 x2（1561x2111,…）` → head `两移门`, `N=2`.)
3. **Match** the head to `working_hours` using a normalised lookup:
   - the lookup is indexed by every variant (rows are split on `/ /`, and any
     `（…）` spec is stripped — so `平开门（含双包套）`, `L型/钻石型/扇形` become
     the keys `平开门`, `L型`, `钻石型`, `扇形`, …);
   - match order: **exact** normalised head, else **longest variant that is a
     substring** of the head (so `两移门` matches `两移门`, `钻石淋浴房` matches
     `钻石型`/`钻石`, `扇形淋浴房` matches `扇形`).
4. **Sum**: `s = Σ (hours_matched × N)` over all lines.

**Drop-on-unmatched (your rule + address-leak handling).** A task is kept only
if **every** line matches. If any line is unmatched it is **removed from the task
set** and reported. This covers two cases and is handled differently:

- **Wrong parsing / a genuinely missing product** (e.g. `两折叠`, `三折叠`,
  `钻石淋浴房`, `D门` are not in `working_hours`) → reported, and *recoverable by
  adding the product to `working_hours.xlsx`* (flagged in the report as
  "missing-product").
- **Address leaked into the 商品 cell** (e.g. a line that is actually
  `宝山区抚远路993弄95号302` — no `xN`, a 弄/号 address pattern) → cannot be
  recovered by reparsing; reported and flagged as "likely-address-leak".

**Observed on the real data** (`docs/*.xlsx`) — these numbers drive the demo:

```
166 orders carry a 商品 cell
 144 fully resolvable            → KEPT  (the task set the solver works on)
  22 removed  (20 "missing product" 两折叠/三折叠/D门/钻石淋浴房,  2 "address-leak")
 total on-site service of the 144 ≈ 597 h   (≈ 4.15 h/order)
```

> **The `-`/blank hours problem.** `垭口套（1.5米以上）` has `-` (unknown). Rule:
> a `-`/blank hours value is treated as **unknown** → any task using it is removed
> (safer than guessing 0). Add a real value to `working_hours.xlsx` to recover it.
> **`--workdir`** persists a `removed_tasks.json` (task → reason → offending lines)
> and `working_hours_report.json` for audit.

---

## 4. The optimisation model  【step: `solve`】

### 4.1 Sets & data

- **Workers** `W` — each a depot at `home_point(h_w)`, with a transport speed
  factor `f_w` and the shared working-day window `[D_open, D_close]`
  (defaults **08:00 → 18:00**, `--day-start/--day-end`).
- **Orders** `O` — the 144 survivors; order `o` has site `p_o`, integer service
  time `s_o` (seconds, from §3), and **no individual time window** (open).
- **Travel** — `τ_w(i→j) = dur_amap(i→j) × f_w`, from the AMap driving matrix
  (`step: matrix`). Distance `d(i→j)` is AMap's reported metres.

### 4.2 Decision

For each worker `w` an **ordered sequence** of distinct orders
`(o_{w,1}, …, o_{w,k})`, plus the set of **dropped** orders `Ω \ assigned`.
Each order is served by **at most one** worker (mutually exclusive) — the
"selective/partial" nature.

### 4.3 Time recursion (constraint **(d)**)

Let `t_w` start at `D_open = 08:00` at the home depot. For step `m`:

```
arrive_w(m) = t_w + τ_w(prev → o_{w,m})            # travel from previous site/home
start_w(m)   = arrive_w(m)                          # worker may wait (open window)
finish_w(m)  = start_w(m) + s_o                     # busy until the task is done
t_w          = finish_w(m)                          # worker re-enters from this lat/lng
```

Hard constraint — **the day cutoff** (office hours, constraint **(b)**):
`finish_w(k) ≤ D_close = 18:00` for every completed task. (The optional
`--max-orders` adds `k ≤ max_orders` per worker; default = `|O|`, i.e. the 18:00
cutoff is the binding limit.)

### 4.4 Objective (constraints **(a)** and **(c)**)

```
minimise   Z =  Σ_w Σ_edges  τ_w(edge)            ← travel time (a), base objective
             +  Σ_{o dropped}  π_o
with        π_o = PENALTY_BASE + K · s_o
```

- `PENALTY_BASE` is large ⇒ the solver first **minimises the number of dropped
  tasks** → keep as many as possible (**b**).
- The additive `K · s_o` term then **breaks ties by time**, so among the forced
  drops the solver drops the *shortest* tasks and keeps the *longest* →
  **drop penalty proportional to task time (c)**.
- Default `PENALTY_BASE = 1.0e7`, `K = 1.0` (penalty ≈ lost service-seconds).
  Both are configurable; `--drop-by {time|count}` switches to count-only
  (`K=0`) or pure time.

### 4.5 Solver

OR-Tools **Constraint Solver**, VRPTW-with-disjunction:
- per-worker **Time** dimension (transit = `τ_w`, service folded into departure);
- optional **capacity/count** dimension;
- `AddDisjunction` per order for the drop option with penalty `π_o`;
- first-solution `PATH_CHEAPEST_ARC` + `GUIDED_LOCAL_SEARCH`.
- A window-aware **greedy fallback** if OR-Tools raises / returns nothing.
- **Open route (constraint (d))**: a worker's day ends at the *last finishing
  task*; the return-to-home leg is reported for distance but **not** counted
  against the 18:00 window.

### 4.6 Status

`OPTIMAL` (solver-optimal within time limit, nothing dropped), `FEASIBLE`
(assigned-but-dropped or time-limited), `INFEASIBLE` (a mandatory task unserved —
cannot happen here since all orders are droppable), `ERROR`.

---

## 5. Outputs

### 5.1 `result.xlsx`  【step: `export`】

| sheet | rows | columns |
|---|---|---|
| **结果** | one row per visited task | 师傅 · 师傅ID · 顺序 · 订单编号 · 商家 · 安装地址 · 做单时长(h) · 预计上门 · 预计完工 |
| **汇总** | one row per worker | 师傅 · 派单数 · 总做单时长(h) · 总路程时长(min) · 总路程(km) |
| **未分配** | one row per dropped task | 订单编号 · 商家 · 备注 |
| **移除** | one row per task dropped in §3 | 序号 · 商家 · 原因 · 未匹配商品行 |

(Times as `HH:MM`.)

### 5.2 Console intermediate output  (one block per step, `--verbose` = full)

```
[1/7] parse-hours      ← working_hours.xlsx
     23 product rows → 31 variant keys
[2/7] parse-workers    ← workers.xlsx
     6 workers | car=4 ebike=2
[3/7] parse-tasks      ← tasks.xlsx + hours
     166 orders → 144 KEPT, 22 removed (20 missing-product, 2 address-leak)
     total on-site service ≈ 597.0 h
[4/7] geocode         city=上海  (AMap)
     150 addresses: 150 resolved, 0 failed
[5/7] matrix         N=150  (AMap driving)
     source=amap  11175 pairs computed
[6/7] solve          OR-Tools VRPTW
     status=feasible  18/144 scheduled within 08:00–18:00
     126 tasks DROPPED (penalty ∝ time)   objective(travel)=…  solve_time=12.3s
     max-orders/worker within the day ≈ 3–4
     Zhang: 3 stops  08:12→17:58   Li: 4 stops 08:05→16:30 …
[7/7] export        → result.xlsx
     sheets: 结果(18) 汇总(6) 未分配(126) 移除(22)
```

> The "max-orders/worker ≈ 3–4" line is derived from `D_close − D_open − Σs /
> n_workers`: the dataset is ~10× one day, so the solver returns a best-fit
> **subset** (~10%), which is exactly the intent of (b)/(c)/(3).

### 5.3 Work-dir checkpoints (each step independently re-runnable/verifiable)

`--workdir ./_run` writes JSON/CSV per step: `hours.json`, `workers.raw.json`,
`tasks.raw.json`, `removed_tasks.json`, `workers.geo.json`, `tasks.geo.json`,
`matrix.json`, `result.json`, `result.xlsx`. Any step reads the prior checkpoint.

---

## 6. CLI surface  【standalone demo script】

One script (`examples/dispatch_cli.py`), two modes:

```
# full demo — runs all 7 steps, prints each intermediate, writes result.xlsx
python dispatch_cli.py run -w workers.xlsx -t tasks.xlsx -H working_hours.xlsx \
     --city 上海 -o result.xlsx

# verify a single step (reads prior checkpoint in --workdir, prints its output)
python dispatch_cli.py geocode  --workdir ./_run --city 上海
python dispatch_cli.py solve    --workdir ./_run
```

| arg | default | meaning |
|---|---|---|
| `-w/--workers` | `docs/workers.xlsx` | crew |
| `-t/--tasks` | `docs/tasks.xlsx` | orders |
| `-H/--hours` | `docs/working_hours.xlsx` | labour-hours table |
| `--city` | `上海` | AMap geocode city |
| `--day-start` / `--day-end` | `08:00` / `18:00` | working day (hard cutoff) |
| `--max-orders` | `len(kept)` | per-worker cap; default = unbounded (18:00 binds) |
| `--timeout-s` | *none* | solver wall-clock budget; actual time reported |
| `--drop-by` | `time` | drop-penalty mode: `time` (∝ task time) / `count` |
| `--penalty-base` / `--per-hour-k` | `1.0e7` / `1.0` | drop-penalty coefficients |
| `--no-geocode` | off | skip AMap (Euclidean matrix) — offline demo |
| `--workdir` | `./_run` | checkpoint dir |
| `-o/--out` | `result.xlsx` | output xlsx |
| `--verbose` | off | full per-step dumps |

Each step is a sub-command: `parse-hours · parse-workers · parse-tasks ·
geocode · matrix · solve · export`, plus `run` (= all, in order).

---

## 7. Assumptions & open decisions (please confirm / adjust)

1. **Durations + a single 08–18 window** replace per-order time windows, because
   the data has no start times (§4.3). ✓ matches your point 2.
2. **Best-fit subset** is the output when the day overflows (≈10× capacity here). ✓
   matches your point 1 / (c) / (3).
3. **Drop penalty ∝ task time**, with a large base to keep-as-many-as-possible
   first (§4.4). `--drop-by` lets you switch to pure count. Please confirm the
   **default = `time`** and the base `1.0e7`.
4. **Removed-task policy** (§3): drop the *whole task* if any 商品 line is
   unmatched; report `missing-product` vs `address-leak` separately. ✓
5. **`-`/blank hours** = unknown → task dropped (not 0).
6. **Open routes** (§4.5): the 18:00 cutoff is the last *task* finish; the
   home-return leg is reported but unconstrained. Confirm — or should the worker
   have to return home by 18:00 (closed loop)?
7. **Quantity mis-parse risk**: some cells carry large `xN` (e.g. `两折叠 x20`).
   We take `N` at face value; flag as an assumption — or cap `N`?
8. **Speed factors** `ebike×1.6 / car_out×1.15 / car_sh×1.0` from config.
```

---

## 8. Standalone CLI pipeline (`examples/dispatch_cli.py`)

A single standalone command that runs the 7 decoupled stages, printing each
stage's intermediate data and writing a JSON checkpoint per stage (default `./_run`):

```
python examples/dispatch_cli.py run  \
    -w docs/workers.xlsx -t docs/tasks.xlsx -H docs/working_hours.xlsx \
    --city 上海 --out result.xlsx --workdir ./_run
```

| # | step | reads | writes (checkpoint) | notes |
|---|------|-------|---------------------|-------|
| 1 | parse-hours | `working_hours.xlsx` | `lookup.json`, `unknown.json` | product head → service hours |
| 2 | parse-workers | `workers.xlsx` | `workers_raw.json` | one start depot per worker |
| 3 | parse-tasks | `tasks.xlsx` + lookup | `tasks_raw.json`, `removed.json`, `qty_warnings.json` | product→hours; drop unmatched |
| 4 | geocode | tasks_raw + workers | `tasks_geo.json`, `workers_geo.json` | AMap (set `AMAP_API_KEY`, `--coords auto`) |
| 5 | matrix | geocoded pts | `matrix.json` | AMap directions (or `--matrix euclidean` offline) |
| 6 | solve | matrix + orders | `result.json` | best-fit; drop penalty ∝ service time |
| 7 | export | result | `result.xlsx` | 结果 / 汇总 / 未分配 / 移除 / 数量警告 sheets |

Each sub-command (`hours`, `workers`, `tasks`, `geocode`, `matrix`, `solve`,
`export`, `run`) runs independently, loading prior checkpoints from `--workdir`.
Steps 1–3 are pure data transformation (run fully offline on the real xlsx);
steps 4–7 need `AMAP_API_KEY` (or the labelled offline `--coords auto` /
`--matrix euclidean` fallbacks).

### Worked result on the `docs/*.xlsx` sample (offline)

```
144 tasks KEPT, 22 REMOVED
  {'missing-product': 20, 'address-leak': 2}
  total on-site service of KEPT = 614.0 h   (avg 4.26 h/task)
  6 workers × 08:00–18:00 = 60 h capacity       => 10.2× over capacity
  best-fit subset scheduled within 08:00–18:00: ~18/144
  removed: D门 (lookup has PD), 两折叠/三折叠及以上 (lookup has 折叠门),
           钻石淋浴房 (lookup has 钻石型), + 2 address-leak cells
  5 quantity warnings (flag-only): 平开门×11, ×14, ×13, plus 两折叠×20 / ×7
```

The `docs/tasks.xlsx` day is ~10× over the 6-worker 08:00–18:00 capacity, so the
optimiser is forced to pick a high-value subset; the rest are reported in 未分配
(drop penalty ∝ service time = the `--drop-policy time` default).

## 9. Data / structure findings (real files) and a parsing fix

1. **`tasks.xlsx` 商品 cells are multi-line** (multiple products separated by
   newlines). Parsed line-by-line; the task total is the sum of its lines.
2. **One product line has an empty `-` hours entry** (垭口套 1.5米以上). Per the
   confirmed decision, a *blank / `-`* hours value for a product that IS in the
   table is an error → the task using it is dropped, never silently 0.
3. **Two 商品 cells leak 安装地址 text** (e.g. `浦东新区创新中路593弄18栋1502室`,
   `徐汇区长丰坊27号601室`) → detected by `_looks_address` and dropped with
   reason `address-leak`.
4. **Parsability bug fixed**: the `xN` quantity regex `[x×*]\d+` matched
   *dimensions* inside the spec (e.g. `950x2400`). Fixed by stripping the `（…）`
   spec **before** extracting the quantity, the qty token is the `xN` *outside*
   the parens. (Tests: `tests/test_tasks.py`.)
5. **Unmatched product heads** reported as `missing-product`: `D门` (→ `PD`),
   `两折叠` / `三折叠及以上` (lookup has only `折叠门`), `钻石淋浴房` (lookup has
   `钻石型`). Longest-substring matching (`折叠门` inside `三折叠门`) resolves the
   fold-door family; the bare `两折叠` / `三折叠及以上` do not match.

## 10. Tests

- `tests/test_solver.py` — 5 regression tests over the 6 headline fixes (open
  route, time windows, optional/drop ∝ time, capacity, 60-min travel, no-crash).
- `tests/test_tasks.py` — 14 tests over 商品 parsing, address-leak / missing
  detection, blank-hours handling, quantity flagging, and the real-file import
  counts (144 kept / 22 removed).

Run: `python -m pytest tests/ -q`.
