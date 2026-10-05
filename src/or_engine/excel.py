"""excel.py — read workers/orders from .xlsx and write results back.

Uses :mod:`openpyxl`. Header matching is lenient: each canonical field has
alias spellings in both Chinese (the source workbooks) and English; the first
present, non-empty header wins. Missing columns degrade to defaults.

Expected source sheets: ``workers`` (安装师傅) and ``orders`` (订单).
A dispatch run can be exported to a new .xlsx via :func:`export_result`.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

from .models import (
    DispatchResult,
    Order,
    Point2D,
    TimeWindow,
    TransportMode,
    Worker,
    WorkingHour,
)

# ── header aliases: canonical field -> accepted header spellings ────────────
_WORKER_ALIASES = {
    "name": ["name", "姓名", "师傅"],
    "home_address": ["home_address", "常驻位置", "地址", "home"],
    "homeLng": ["home_lng", "lng", "经度"],
    "homeLat": ["home_lat", "lat", "纬度"],
    "transport": ["transport", "出行工具", "交通工具"],
    "available_start": ["available_start", "最早出工", "出工时间", "start"],
    "available_end": ["available_end", "最晚完工", "完工时间", "end"],
    "max_orders": ["max_orders", "最大套数", "每日上限", "max"],
    "phone": ["phone", "电话", "联系方式"],
}

_ORDER_ALIASES = {
    "order_no": ["order_no", "订单编号", "单号"],
    "order_type": ["order_type", "订单类型", "类型"],
    "merchant": ["merchant", "商家", "商家名称"],
    "site_address": ["site_address", "安装地址", "服务地址", "地址"],
    "siteLng": ["site_lng", "lng", "经度"],
    "siteLat": ["site_lat", "lat", "纬度"],
    "date": ["date", "安装日期", "日期"],
    "window_start": ["window_start", "开始时间", "上门开始", "start"],
    "window_end": ["window_end", "结束时间", "上门结束", "end"],
    "service_hours": ["service_hours", "做单时长", "工时", "时长"],
    "quantity": ["quantity", "商品数量", "数量", "套数"],
    "amount": ["amount", "金额", "订单金额"],
    "note": ["note", "备注", "说明"],
}



# working_hours (工作工时表): product description -> on-site labour hours.
# Source header is 商品 / 工时（小时）; 工时 cells may hold stray spaces or a
# placeholder "-" for "unset", which _float() maps to 0.0.
_WORKING_HOUR_ALIASES = {
    "product": ["product", "商品", "项目", "名称", "name"],
    "hours": [
        "hours",
        "工时（小时）",
        "工时(小时)",
        "工时",
        "时长",
        "做单时长",
    ],
}

# ── generic reader ──────────────────────────────────────────────────────────
def _rows(path: str | Path, sheet: str | None = None) -> list[dict]:
    """Read a sheet into a list of dicts keyed by *original* header text.
    Falls back to the active sheet if *sheet* is not found."""
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = None
    if sheet and sheet in wb.sheetnames:
        ws = wb[sheet]
    if ws is None:
        ws = wb.active
    rows = [r for r in ws.iter_rows(values_only=True) if r is not None]
    wb.close()
     # skip leading fully-blank rows; the first non-blank row is the header
    first = next((k for k, row in enumerate(rows) if any(c is not None for c in row)), None)
    if first is None:
        return []
    headers = [str(c).strip() if c is not None else "" for c in rows[first]]
    out: list[dict] = []
    for raw in rows[first + 1:]:
        if raw is None or all(c is None for c in raw):
            continue
        out.append(
             {
                headers[i]: (raw[i] if i < len(raw) else None)
                for i in range(len(headers))
             }
         )
    return out


def _pick(row: dict, aliases: list[str]):
    """First present, non-empty value among the alias spellings."""
    norm = {str(k).strip().lower(): v for k, v in row.items()}
    for a in aliases:
        v = norm.get(a.strip().lower())
        if v not in (None, ""):
            return v
    return None


def _hhmm(v):
    """seconds-since-midnight, or 'HH:MM' string -> int seconds."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    try:
        if ":" in s:
            h, m = s.split(":")
            return int(h) * 3600 + int(m) * 60
        return int(float(s))
    except ValueError:
        return None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _opt_str(v):
    return None if v in (None, "") else str(v).strip()


def _point(lng, lat):
    return Point2D(lng=float(str(lng)), lat=float(str(lat)))


# ── public import API ───────────────────────────────────────────────────────
def import_workers(path: str | Path, sheet: str | None = None) -> list[Worker]:
    rows = _rows(path, sheet or "workers") or _rows(path)
    out: list[Worker] = []
    for r in rows:
        w = Worker(
            id=str(uuid4()),
            name=_opt_str(_pick(r, _WORKER_ALIASES["name"])) or "worker",
            home_address=_opt_str(_pick(r, _WORKER_ALIASES["home_address"])),
            transport=TransportMode.coerce(_pick(r, _WORKER_ALIASES["transport"])),
            available_start=_hhmm(_pick(r, _WORKER_ALIASES["available_start"])),
            available_end=_hhmm(_pick(r, _WORKER_ALIASES["available_end"])),
            max_orders=int(_float(_pick(r, _WORKER_ALIASES["max_orders"]) or 20)),
            phone=_opt_str(_pick(r, _WORKER_ALIASES["phone"])),
        )
        lng, lat = _pick(r, _WORKER_ALIASES["homeLng"]), _pick(
            r, _WORKER_ALIASES["homeLat"]
        )
        if lng not in (None, "") and lat not in (None, ""):
            w.home_point = _point(lng, lat)
        out.append(w)
    return out


def import_orders(path: str | Path, sheet: str | None = None) -> list[Order]:
    rows = _rows(path, sheet or "orders") or _rows(path)
    out: list[Order] = []
    for r in rows:
        tw_start = _hhmm(_pick(r, _ORDER_ALIASES["window_start"]))
        tw_end = _hhmm(_pick(r, _ORDER_ALIASES["window_end"]))
        tw = (
            TimeWindow(start=tw_start, end=tw_end)
            if (tw_start is not None or tw_end is not None)
            else None
        )
        o = Order(
            id=str(uuid4()),
            order_no=_opt_str(_pick(r, _ORDER_ALIASES["order_no"]))
            or f"O-{uuid4().hex[:8]}",
            order_type=_opt_str(_pick(r, _ORDER_ALIASES["order_type"])),
            merchant=_opt_str(_pick(r, _ORDER_ALIASES["merchant"])),
            site_address=_opt_str(_pick(r, _ORDER_ALIASES["site_address"])),
            date=_opt_str(_pick(r, _ORDER_ALIASES["date"])),
            time_window=tw,
            service_hours=_float(_pick(r, _ORDER_ALIASES["service_hours"])),
            quantity=int(_float(_pick(r, _ORDER_ALIASES["quantity"]) or 1)),
            amount=_float(_pick(r, _ORDER_ALIASES["amount"])) or None,
            note=_opt_str(_pick(r, _ORDER_ALIASES["note"])),
        )
        lng, lat = _pick(r, _ORDER_ALIASES["siteLng"]), _pick(
            r, _ORDER_ALIASES["siteLat"]
        )
        if lng not in (None, "") and lat not in (None, ""):
            o.site_point = _point(lng, lat)
        out.append(o)
    return out



def import_tasks(path: str | Path, sheet: str | None = None) -> list[Order]:
    """Read tasks.xlsx into :class:`Order` rows.

    ``tasks`` is the on-disk name of the work-order sheet; the dispatch engine
    *derives* the tasks to route from these orders. This is an alias of
    :func:`import_orders` that also accepts the ``tasks`` sheet name.
    """
    return import_orders(path, sheet or "tasks")


def import_working_hours(
    path: str | Path, sheet: str | None = None
) -> list[WorkingHour]:
    """Read working_hours.xlsx (商品 -> 工时) into :class:`WorkingHour` rows.

    Product names are stripped; rows with no product are skipped. A
    placeholder/empty 工时 cell decodes to ``0.0`` via :func:`_float`.
    """
    rows = _rows(path, sheet or "working_hours") or _rows(path)
    out: list[WorkingHour] = []
    for r in rows:
        product = _opt_str(_pick(r, _WORKING_HOUR_ALIASES["product"]))
        if not product:
            continue
        out.append(
            WorkingHour(
                product=product,
                hours=_float(_pick(r, _WORKING_HOUR_ALIASES["hours"])),
            )
        )
    return out

# ── export API ──────────────────────────────────────────────────────────────
_RESULT_HEADERS = [
    "日期", "师傅", "师傅ID", "顺序", "订单编号", "订单类型", "商家",
    "联系电话", "安装地址", "金额(¥)", "做单时长(h)",
    "预计上门", "预计完工", "是否超窗",
]
_SUMMARY_HEADERS = [
    "日期", "师傅", "师傅ID", "运输工具",
    "派单数", "总服务工时(h)", "总路程时长(min)", "总路程(km)",
]
_UNASSIGNED_HEADERS = [
    "订单编号", "订单类型", "商家", "联系电话", "安装地址",
    "金额(¥)", "做单时长(h)", "备注",
]
_REMOVED_HEADERS = [
    "订单编号", "商家", "联系电话", "安装地址", "金额(¥)",
    "做单时长(h)", "未匹配/非法商品行", "原因",
]
_QTY_HEADERS = [
    "订单编号", "商品/行", "解析数量", "匹配商品", "单位工时(h)",
]

_HEADER_FONT = Font(bold=True, color="FFFFFF")
_HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", patternType="solid")
_SECTION_FONT = Font(bold=True, size=11, color="4472C4")
_LABEL_FONT = Font(bold=True)


def _style_header(ws):
    for c in ws[1]:
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
        c.alignment = Alignment(horizontal="left", vertical="center")
    ws.freeze_panes = "A2"


def _amt(v) -> str:
    try:
        return "¥" + format(float(v), ",.0f")
    except (TypeError, ValueError):
        return str(v)


def _write_config_sheet(wb: Workbook, cfg: dict) -> None:
    """Render the run configuration as the first (置顶) sheet '配置'."""
    ws = wb.active
    ws.title = "配置"
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 56
    ws.column_dimensions["C"].width = 26
    ws.sheet_properties.tabColor = "4472C4"
    r = 1

    def put(label, value):
        nonlocal r
        ws.cell(row=r, column=1, value=label).font = _LABEL_FONT
        ws.cell(row=r, column=2, value=_str(value)).alignment = Alignment(horizontal="left")
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=3)
        r += 1

    def section(title):
        nonlocal r
        ws.cell(row=r, column=1, value=title).font = _SECTION_FONT
        r += 1

    put("生成时间", cfg.get("run_at", ""))
    section("运行")
    put("城市", cfg.get("city", ""))
    put("坐标来源", cfg.get("coords_source", ""))
    put("矩阵来源", cfg.get("matrix_source", ""))
    put("求解引擎", cfg.get("engine", ""))
    put("排程模式", cfg.get("mode", ""))

    section("每日时间窗 / 排程")
    put("每日时间窗", cfg.get("day_window", ""))
    put("实际使用天数", cfg.get("days_used", ""))
    put("终止原因", cfg.get("termination", ""))
    put("每天每工超窗限额", cfg.get("allow_overflow", ""))
    fb = cfg.get("depot_fallback")
    put("中心回退 depot", (str(fb) if fb else "(每个师傅各自合成坐标)"))

    section("丢弃 / 排入优先级 (--drop-by)")
    put("模式", cfg.get("drop_by", ""))
    put("惩罚基准 base", cfg.get("penalty_base", ""))
    put("每小时 / 每单位 k", cfg.get("per_hour_k", ""))
    put("数量告警阈值 N≥", cfg.get("warn_qty", ""))
    put("最大天数(安全上限)", cfg.get("max_days", ""))

    section("统计")
    cnt = cfg.get("counts", {})
    amt = cfg.get("amounts", {})
    put("订单总数", cnt.get("total", ""))
    put("已排入 / 未排入", f"{cnt.get('assigned','')} / {cnt.get('unassigned','')}")
    put("已排金额 / 未排金额", f"{_amt(amt.get('assigned', 0))} / {_amt(amt.get('unassigned', 0))}")
    put("总金额", _amt(amt.get("total", 0)))
    util = cfg.get("utilization_pct", 0.0) or 0.0
    put("利用率", f"{util:.1f}%" + ("" if util >= 100.0 else "   (未排单见『未分配』表)"))
    unreasons = cfg.get("unassigned_reasons", {}) or {}
    if unreasons:
        txt = "、".join(f"{k}={v}" for k, v in unreasons.items())
        note = ws.cell(row=r, column=1, value=f"未排原因: {txt}")
        note.font = Font(color="C00000", italic=True)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        r += 1

    section("师傅 × depot")
    ws.cell(row=r, column=1, value="师傅").font = _HEADER_FONT
    ws.cell(row=r, column=2, value="depot 坐标   [来源]").font = _HEADER_FONT
    ws.cell(row=r, column=3, value="可用时窗 / 日上限 / 总派单").font = _HEADER_FONT
    r += 1
    for w in cfg.get("workers", []):
        a = w.get("available_start", "") or ""
        b = w.get("available_end", "") or ""
        win = f"{a}-{b}" if (a or b) else "全天/不限"
        ws.cell(row=r, column=1, value=f"{w.get('name','')} ({_str(w.get('transport',''))})")
        ws.cell(row=r, column=2, value=f"{w.get('home','(无坐标)')}   [{w.get('home_source','')}]")
        ws.cell(row=r, column=3,
                value=f"{win} / 日{w.get('max_orders','∞')} / 总派{w.get('stops_all_days',0)}")
        r += 1
    if not cfg.get("workers"):
        ws.cell(row=r + 1, column=2, value="(无师傅)").font = Font(italic=True)

    section("每日概览")
    ws.cell(row=r, column=1, value="天").font = _HEADER_FONT
    ws.cell(row=r, column=2, value="派单数 / 服务工时").font = _HEADER_FONT
    ws.cell(row=r, column=3, value="金额(¥)").font = _HEADER_FONT
    r += 1
    for d in cfg.get("day_summary", []):
        ov = d.get("overflow", 0)
        ws.cell(row=r, column=1, value=d.get("day", ""))
        ws.cell(row=r, column=2,
                value=f"{d.get('assigned',0)} 单 · {d.get('service_hours',0):.1f}h"
                        + (f" · 超窗 {ov} 次" if ov else ""))
        ws.cell(row=r, column=3, value=_amt(d.get("assigned_amount", 0.0)))
        r += 1
    ws.sheet_properties.tabColor = "4472C4"


def _str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return format(v, ",.1f")
    return str(v)


def _transport(w) -> str:
    m = getattr(w, "transport", None) or getattr(w, "mode", None)
    if isinstance(m, str):
        return m
    if m is not None and hasattr(m, "value"):
        return str(getattr(m, "value", m))
    return ""


def export_result(
    path: str | Path,
    result: DispatchResult,
    workers: list[Worker] | None = None,
    orders: list[Order] | None = None,
    removed: object | None = None,
    qty_warnings: object | None = None,
    config: dict | None = None,
) -> Path:
    """Write *result* to a .xlsx.

    Sheets in order: ``配置`` (when *config* is given), ``结果`` (per stop),
    ``汇总`` (per 天 × 师傅 with 运输工具), then ``未分配`` / ``移除`` /
    ``数量警告`` (each only when non-empty). Every row carries full 订单 / 商品 /
    原因 so a dispatcher can act on it directly.
    """
    order_by_id = {o.id: o for o in (orders or [])}
    wb = Workbook()

    if config:
        _write_config_sheet(wb, config)
        ws = wb.create_sheet("结果")
    else:
        ws = wb.active
        ws.title = "结果"

    # 结果 ──────────────────────────────────────────────────────
    ws.append(_RESULT_HEADERS)
    for ar in result.routes:
        for st_ in ar.assignments:
            o = order_by_id.get(st_.order_id)
            ws.append(
                [
                    f"第{st_.day or ar.day or 1}天",
                    ar.worker_name,
                    ar.worker_id,
                    st_.sequence + 1,
                    st_.order_no,
                    o.order_type if o else "",
                    (o.merchant or "") if o else "",
                    (o.contact or "") if o else "",
                    (o.site_address or (st_.site.to_str() if st_.site else "")) if o else "",
                    round((o.amount or 0.0), 2) if o is not None else 0.0,
                    round(o.service_hours, 2) if o else 0.0,
                    _to_hhmm(st_.arrival_s),
                    _to_hhmm(st_.departure_s),
                    "超窗" if getattr(st_, "overflow", False) else "",
                ]
            )
    _style_header(ws)

    # 汇总 ──────────────────────────────────────────────────────
    ws2 = wb.create_sheet("汇总")
    ws2.append(_SUMMARY_HEADERS)
    transport = {w.id: _transport(w) for w in (workers or [])}
    agg: dict = {}
    for ar in result.routes:
        key = (ar.day, ar.worker_id)
        c = agg.setdefault(key, {
            "name": ar.worker_name, "n": 0, "svc": 0.0, "trav": 0.0, "dist": 0.0,
        })
        c["n"] += len(ar.assignments)
        c["svc"] += ar.total_service_s
        c["trav"] += ar.total_travel_s
        c["dist"] += ar.total_distance_m
    for (day, wid) in sorted(agg):
        c = agg[(day, wid)]
        ws2.append(
            ["第%d天" % day, c["name"], wid, transport.get(wid, ""),
            c["n"], round(c["svc"] / 3600, 2),
            round(c["trav"] / 60, 1), round(c["dist"] / 1000, 2)]
        )
    _style_header(ws2)

    if result.unassigned_orders:
        ws3 = wb.create_sheet("未分配")
        ws3.append(_UNASSIGNED_HEADERS)
        for oid in result.unassigned_orders:
            o = order_by_id.get(oid)
            ws3.append(
                [
                    o.order_no if o else oid,
                    o.order_type if o else "",
                    (o.merchant or "") if o else "",
                    (o.contact or "") if o else "",
                    (o.site_address or "") if o else "",
                    round((o.amount or 0.0), 2) if o else 0.0,
                    round(o.service_hours, 2) if o else 0.0,
                    "未在排程内落点(详见『配置』表)",
                ]
            )
        _style_header(ws3)

    if removed:
        ws4 = wb.create_sheet("移除")
        ws4.append(_REMOVED_HEADERS)
        for t in removed:
            d = t.as_dict()
            ws4.append(
                [
                    d.get("order_no", ""),
                    d.get("merchant") or "",
                    d.get("contact") or "",
                    d.get("site_address") or "",
                    round(d.get("amount") or 0.0, 2),
                    "",
                    " ; ".join(d.get("lines") or []) or "—",
                    d.get("reason", ""),
                ]
            )
        _style_header(ws4)

    if qty_warnings:
        ws5 = wb.create_sheet("数量警告")
        ws5.append(_QTY_HEADERS)
        for q in qty_warnings:
            ws5.append(
                [
                    q.get("order_no", ""),
                    q.get("line", ""),
                    q.get("parsed_qty"),
                    q.get("matched") or "无匹配",
                    q.get("hours_each"),
                ]
            )
        _style_header(ws5)

    out = Path(path)
    wb.save(out)
    return out


def _to_hhmm(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}"
