"""Tests for 商品-line parsing + product-hours resolution (or_engine.tasks).

Exercises the two checks from the spec: missing-product and address-leak,
plus blank/`-` hours handling and quantity flagging (decision #7).
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from or_engine.tasks import (
    ProductLine,
    RemovedTask,
    _looks_address,
    import_tasks,
    load_working_hours,
    parse_product_line,
    resolve_task_hours,
)

from openpyxl import Workbook

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "docs"


def _lookup() -> dict:
    """Small product -> working-hours lookup mirroring 工作工时表."""
    return {
        '平开门': 1.25,
        '三折叠门': 3.0,
        '折叠门': 1.0,
        '淋浴房钻石型': 2.0,
        '垭口套': None, # blank/- hours -> never 0
    }


def _write_tasks_xlsx(path, rows):
    """rows: list of (order_no, site_address, merchant, product)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["序号", "安装地址", "商家", "商品", "订单总额", "联系人"])
    for no, addr, merchant, product in rows:
        ws.append([no, addr, merchant, product, 0, ""])
    wb.save(str(path))


def test_plain_product_default_quantity_is_one():
    p = parse_product_line("平开门（950x2400,单包套,内左开）", _lookup())
    assert p.matched_key == "平开门"
    assert p.qty == 1
    assert p.hours == 1.25
    assert p.ok is True


def test_quantity_token_resolved():
    p = parse_product_line("平开门 x11（950x2400,单包套）", _lookup())
    assert p.qty == 11 and p.matched_key == "平开门"


def test_unrecognised_head_not_resolved():
    p = parse_product_line("两折叠 x1（810x2380,双包套）", _lookup())
    assert p.matched_key is None
    assert p.hours is None
    assert p.ok is False


def test_longest_substring_match_not_short():
    lookup = {"折叠门": 1.0, "三折叠门": 3.0}
    p = parse_product_line("三折叠门 x2", lookup)
    assert p.matched_key == "三折叠门"   # not the shorter "折叠门"
    assert p.hours == 3.0


def test_real_address_leak_detected():
    assert _looks_address("浦东新区创新中路593弄18栋1502室")
    assert _looks_address("徐汇区长丰坊27号601室")


def test_product_is_not_flagged_as_address():
    assert not _looks_address("平开门（950x2400,单包套,内左开）")
    assert not _looks_address("三折叠门 x2")


def test_multi_line_sums_total():
    cell = "平开门 x2（a）\n三折叠门 x1（b）"
    total, parsed, offending = resolve_task_hours(cell, _lookup())
    assert round(total, 6) == round(2 * 1.25 + 3.0, 6)
    assert len(parsed) == 2 and offending == []


def test_unknown_hours_line_becomes_offending_never_zero():
    """垭口套 IS in the lookup but its hours are blank -> offending, not 0."""
    total, parsed, offending = resolve_task_hours("垭口套 x1（1.5m）", _lookup())
    assert total == 0.0
    assert len(offending) == 1 and "垭口套" in offending[0]


def test_empty_cell_is_empty_resolution():
    total, parsed, offending = resolve_task_hours("", _lookup())
    assert total == 0.0 and parsed == [] and offending == []


def test_blank_and_dash_hours_are_unknown():
    lookup = {"门": None}
    for cell in ("门", "门（）", "门（-）"):
        total, parsed, offending = resolve_task_hours(cell, lookup)
        assert total == 0.0 and len(offending) == 1


def test_load_working_hours_from_real_file():
    lookup, unknown = load_working_hours(str(DATA / "working_hours.xlsx"))
    assert round(lookup["平开门"], 6) == 1.25
    assert "折叠门" in lookup
    assert "一字淋浴房" in lookup
    # the 垭口套（1.5米以上） row carries '-' hours -> reported (never silently 0);
    # a DIFFERENT 垭口套（1.5米内） row supplies a real 1.0h value, kept in the lookup
    assert any("垭口套" in u for u in unknown)
    assert all(v > 0.0 for v in lookup.values())        # no product is stored as 0
    assert round(lookup["垭口套"], 6) == 1.0


def test_import_tasks_real_workbook_counts():
    lookup, _ = load_working_hours(str(DATA / "working_hours.xlsx"))
    orders, removed, qty_warnings = import_tasks(
             str(DATA / "tasks.xlsx"), lookup, warn_qty=5)
    # 144 kept, 22 removed (20 missing-product + 2 address-leak)
    assert len(orders) == 144
    assert len(removed) == 22
    for o in orders:
        assert o.service_hours > 0.0
        assert o.product        # matched product kept for provenance
        assert o.merchant       # propagated from the sheet
    reasons = {t.reason for t in removed}
    assert "missing-product" in reasons
    assert "address-leak" in reasons
    # qty warnings: only *matched* 商品 lines that cross the threshold are
    # flagged here (unmatched lines are already removed as missing-product,
    # not double-counted). With warn_qty=5 that leaves the 平开门 x11 / x14 rows.
    assert len(qty_warnings) == 2
    assert all(q["matched"] for q in qty_warnings)          # matched product, not missing
    assert {q["parsed_qty"] for q in qty_warnings} == {11, 14}


def test_task_with_one_unmatched_line_is_whole_removed(tmp_path):
    lookup = {"平开门": 1.25}
    f = tmp_path / "t.xlsx"
    _write_tasks_xlsx(f, [(1, "上海市A路1号", "M", "平开门 x1（a）\n两折叠 x1（b）")])
    # 两折叠 (line 2) is missing -> the WHOLE task is removed
    orders, removed, _ = import_tasks(str(f), lookup, warn_qty=5)
    assert orders == []
    assert len(removed) == 1
    assert removed[0].reason == "missing-product"
    assert removed[0].lines and "两折叠" in removed[0].lines[0]


def test_address_leak_task_removed_as_address_leak(tmp_path):
    lookup = {"平开门": 1.25}
    f = tmp_path / "t.xlsx"
    _write_tasks_xlsx(f, [(2, "上海市A路1号", "M", "浦东新区创新中路593弄18栋1502室")])
    # an address leaked into 商品 -> classified as address-leak
    orders, removed, _ = import_tasks(str(f), lookup, warn_qty=5)
    assert orders == []
    assert removed[0].reason == "address-leak"
