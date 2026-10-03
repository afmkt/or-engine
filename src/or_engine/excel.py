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
    "师傅",
    "师傅ID",
    "顺序",
    "订单编号",
    "订单类型",
    "商家",
    "安装地址",
    "做单时长(h)",
    "预计上门",
    "预计完工",
]
_SUMMARY_HEADERS = [
    "师傅",
    "派单数",
    "总做单时长(h)",
    "总路程时长(min)",
    "总路程(km)",
]


def export_result(
    path: str | Path,
    result: DispatchResult,
    workers: list[Worker] | None = None,
    orders: list[Order] | None = None,
    removed: object | None = None,
    qty_warnings: object | None = None,
) -> Path:
    """Write *result* to a .xlsx with sheets 结果 / 汇总 / (未分配) / (移除) / (数量警告).

    移除 lists tasks dropped during parsing (reason + offending lines); 数量警告
    lists 商品 lines whose parsed quantity hit the warn threshold. Each extra sheet
    is written only when its argument is a non-empty list.
    """
    order_by_id = {o.id: o for o in (orders or [])}
    wb = Workbook()

    ws = wb.active
    ws.title = "结果"
    ws.append(_RESULT_HEADERS)
    for ar in result.routes:
        for st in ar.assignments:
            o = order_by_id.get(st.order_id)
            ws.append(
                [
                    ar.worker_name,
                    ar.worker_id,
                    st.sequence + 1,
                    st.order_no,
                    o.order_type if o else "",
                    o.merchant if o else "",
                    o.site_address if o else (st.site.to_str() if st.site else ""),
                    round(o.service_hours, 2) if o else 0.0,
                    _to_hhmm(st.arrival_s),
                    _to_hhmm(st.departure_s),
                ]
            )

    ws2 = wb.create_sheet("汇总")
    ws2.append(_SUMMARY_HEADERS)
    for ar in result.routes:
        ws2.append(
            [
                ar.worker_name,
                len(ar.assignments),
                round(ar.total_service_s / 3600, 2),
                round(ar.total_travel_s / 60, 1),
                round(ar.total_distance_m / 1000, 2),
            ]
        )

    if result.unassigned_orders:
        ws3 = wb.create_sheet("未分配")
        ws3.append(["订单编号"])
        for oid in result.unassigned_orders:
            no = order_by_id.get(oid)
            ws3.append([no.order_no if no else oid])

    if removed:
        ws4 = wb.create_sheet("移除")
        ws4.append(["序号", "商家", "原因", "未匹配商品行"])
        for t in removed:
            ws4.append([t.order_no, t.merchant or "", t.reason, " | ".join(t.lines)])

    if qty_warnings:
        ws5 = wb.create_sheet("数量警告")
        ws5.append(["序号", "商品行", "解析数量", "匹配商品", "单位工时(h)"])
        for q in qty_warnings:
            ws5.append([
                q.get("order_no", ""),
                q.get("line", ""),
                q.get("parsed_qty"),
                q.get("matched") or "无匹配",
                q.get("hours_each"),
            ])

    out = Path(path)
    wb.save(out)
    return out
def _to_hhmm(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}"
