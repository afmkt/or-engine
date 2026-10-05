"""Order-task loading: parse ``tasks.xlsx`` into :class:`Order` objects and
resolve on-site **service hours** from a ``working_hours.xlsx`` product lookup.

Pure data transformation - no database, no cache.  Each task's ``商品`` cell is a
multi-line item list; every line's product is matched to the product->hours table
(variants split on ``/`` or ``、``; the parenthesised spec ``（…）`` stripped) and
its ``xN`` quantity multiplies the matched hours    (``service = sum(hours x qty)``).

Dropping policy (project decision): a task is **kept** only if *every* 商品 line
resolves in the table.  A task is **removed** from the set with a reason when a
line is genuinely missing from the table (``missing-product``), an address has
leaked into the 商品 cell (``address-leak``), or the 商品 cell is empty
(``no-product``).  A blank / ``-`` / legacy hours value is treated as *unknown*
and forces the task to drop (it needs manual re-entry - never silently 0).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .excel import _rows
from .models import Order

# -- regexes ---------------------------------------------------------------
_QTY_RE = re.compile(r"[x×*]\s*(\d+)")                 # "x2", "×2", "*2"
_SPEC_RE = re.compile(r"[（(][^（）()]*[）)]")             # 全/半角 spec in parens
_VARIANT_SPLIT = re.compile(r"[、/，,]")                 # /、,， between variants
_ADDR_RE = re.compile(                                 # an address that leaked in
    r"(\d+\s*号|\d+\s*弄|\d+\s*号楼|\d+\s*室)"
    r"|((?:省|市|区|县|镇|街道|路|街|大道|弄|小区|花园|广场|商城|公寓|城|湾|里))\s*(?:\d+|号|弄)"
)
_UNKNOWN = {"", "-", "—", "None", "null", "N/A", "n/a", "#N/A", "#REF!"}


def _cell(r: dict, *aliases: object):
    """First present, non-empty value among several possible header spellings."""
    for a in aliases:
        v = r.get(a)
        if v not in (None, ""):
            return v
    return None


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def strip_spec(s: str) -> str:
    """Strip a parenthetical spec: ``平开门（945x2095,双包套,吊轨）`` -> ``平开门``."""
    return _SPEC_RE.sub("", s or "").strip()


# -- working-hours lookup --------------------------------------------------
def load_working_hours(path: str | Path) -> tuple[dict[str, float], list[str]]:
    """Build ``{normalised head -> hours}`` from the 工作工时表. Returns (lookup, unknown).

    Each row's product may list variants separated by ``/ /、`` (e.g.
     ``L型/钻石型/扇形``); every variant is registered under its stripped,
    whitespace-normalised head. Rows whose hours are blank / ``-`` / non-numeric
    are collected in ``unknown`` (never silently 0).
    """
    lookup: dict[str, float] = {}
    unknown: list[str] = []
    for r in _rows(path):
        product = _cell(r, "product", "商品", "项目", "名称", "name")
        hours = _cell(r, "hours", "工时（小时）", "工时(小时)", "工时", "做单时长", "时长")
        if product is None:
            continue
        s_hours = str(hours) if hours is not None else ""
        if hours is None or s_hours.strip() in _UNKNOWN:
            unknown.append(f"{str(product).strip()} (hours={hours!r} -> unknown)")
            continue
        try:
            h = float(hours)
        except (ValueError, TypeError):
            unknown.append(f"{str(product).strip()} (non-numeric hours={hours!r})")
            continue
        for k in _VARIANT_SPLIT.split(str(product)):
            key = _norm(strip_spec(k))
            if key:
                lookup[key] = h                         # last-seen wins on collision
    return lookup, unknown


# -- per-line / per-task parsing ------------------------------------------
@dataclass
class ProductLine:
    """One parsed 商品 line."""
    raw: str
    head: str
    qty: int
    matched_key: str | None
    hours: float | None

    @property
    def ok(self) -> bool:
        return self.matched_key is not None


@dataclass
class TaskReport:
    order: Order
    lines: list[ProductLine] = field(default_factory=list)
    total_hours: float = 0.0

    @property
    def ok(self) -> bool:
        return all(l.ok for l in self.lines)


@dataclass
class RemovedTask:
    """A task dropped because its 商品 could not be resolved."""
    order_no: str
    reason: str             # "missing-product" | "address-leak" | "no-product"
    lines: list[str]
    merchant: str | None = None
    site_address: str | None = None
    contact: str | None = None
    amount: float | None = None

    def as_dict(self) -> dict:
        return {
            "order_no": self.order_no,
            "reason": self.reason,
            "merchant": self.merchant,
            "site_address": self.site_address,
            "contact": self.contact,
            "amount": self.amount,
            "lines": self.lines,
        }


def parse_product_line(raw: str, lookup: dict[str, float]) -> ProductLine:
    """Extract ``xN`` qty and a spec-stripped head, then match the head to the table.

    Matching: exact normalised head first, else the longest lookup key that is a
    substring of the head. Returns a :class:`ProductLine` with ``matched_key``/
    ``hours`` set when resolvable, else ``None``.
    """
    spec_stripped = strip_spec(raw)        # drop （…）dimensions FIRST
    qty_m = _QTY_RE.search(spec_stripped) # xN token is OUTSIDE the spec
    qty = int(qty_m.group(1)) if qty_m else 1
    head = _QTY_RE.sub('', spec_stripped, 1).strip()
    norm_head = _norm(head)

    if norm_head in lookup:
        matched_key = norm_head
    else:
        best = None
        for key, val in lookup.items():
            if not val:
                continue
            if key in norm_head and (best is None or len(key) > len(best)):
                best = key
        matched_key = best
    hours = lookup.get(matched_key) if matched_key is not None else None
    return ProductLine(raw=raw, head=head, qty=qty, matched_key=matched_key, hours=hours)


def resolve_task_hours(
    product_cell: str | None, lookup: dict[str, float]
) -> tuple[float, list[ProductLine], list[str]]:
    """Resolve a 商品 cell -> ``(total_service_hours, parsed_lines, offending)``.

    ``total_service_hours`` is ``sum(matched_hours x qty)``; ``offending`` lists the
    raw lines that could NOT resolve (empty means the task is fine).
    """
    lines = [ln for ln in re.split(r"[\n\r]+", str(product_cell or "")) if ln.strip()]
    parsed: list[ProductLine] = []
    total = 0.0
    offending: list[str] = []
    for ln in lines:
        p = parse_product_line(ln, lookup)
        parsed.append(p)
        if p.matched_key is None or p.hours is None:
             # missing product, OR a matched product whose hours are unknown
             # (blank / '-'): report it, never count it as 0
            offending.append(ln.strip())
        else:
            total += p.hours * p.qty
    return total, parsed, offending


def _looks_address(raw: str) -> bool:
    return bool(_ADDR_RE.search(raw))


# -- task-file loading -----------------------------------------------------
def import_tasks(
    path: str | Path,
    hours_lookup: dict[str, float],
    warn_qty: int = 5,
) -> tuple[list[Order], list[RemovedTask], list[dict]]:
    """Read tasks.xlsx -> ``(orders, removed, qty_warnings)``.

    * an order is **kept** only when every 商品 line resolves in the lookup;
    * an order is **removed** with a reason (missing-product / address-leak /
      no-product);
    * lines with parsed ``qty >= warn_qty`` are flagged in ``qty_warnings``
      (decision #7: flag-only, take N at face value).
    """
    orders: list[Order] = []
    removed: list[RemovedTask] = []
    qty_warnings: list[dict] = []

    for r in _rows(path):
        no = _cell(r, "order_no", "序号", "订单编号", "单号")
        product = _cell(r, "product", "商品")
        contact = _cell(r, "contact", "联系人")
        amount = _cell(r, "amount", "订单总额", "金额", "order_amount")
        addr = _cell(r, "site_address", "地址", "安装地址", "服务地址")
        merchant = _cell(r, "merchant", "商家", "商家名称")

        total, parsed, offending = resolve_task_hours(product, hours_lookup)

        for ln in parsed:
            if ln.qty >= warn_qty and ln.matched_key is not None and ln.hours is not None:
                qty_warnings.append({
                    "order_no": str(no), "line": ln.raw, "parsed_qty": ln.qty,
                    "matched": ln.matched_key, "hours_each": ln.hours,
                })

        if product is None or not str(product).strip():
            _amt = float(amount) if amount not in (None, "") else None
            removed.append(RemovedTask(
                str(no), "no-product", [], merchant,
                site_address=str(addr) if addr else None,
                contact=str(contact) if contact else None, amount=_amt))
            continue
        if offending:
            has_addr = any(_looks_address(x) for x in offending)
            _amt = float(amount) if amount not in (None, "") else None
            removed.append(RemovedTask(
                str(no),
"address-leak" if has_addr else "missing-product",
                offending, merchant,
                site_address=str(addr) if addr else None,
                contact=str(contact) if contact else None, amount=_amt))
            continue

        orders.append(Order(
            order_no=str(no),
            merchant=str(merchant) if merchant is not None else None,
            site_address=str(addr) if addr else None,
            product=str(product),
            contact=str(contact) if contact else None,
            amount=float(amount) if amount not in (None, "") else None,
            service_hours=total,
        ))
    return orders, removed, qty_warnings
