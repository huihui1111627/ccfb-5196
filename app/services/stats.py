"""Aggregates and Excel export for daily entry volume / success rate."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.db import Database, json_loads


def overall_stats(db: Database) -> dict:
    docs = db.query("SELECT status, COUNT(*) n FROM documents GROUP BY status")
    counts = {r["status"]: r["n"] for r in docs}
    subs = db.query("SELECT state, COUNT(*) n FROM submissions GROUP BY state")
    sub_counts = {r["state"]: r["n"] for r in subs}
    accepted_decl = db.scalar(
        "SELECT COUNT(*) FROM submission_attempts WHERE kind='settle' AND outcome='accepted'")
    rejected_attempts = db.scalar(
        "SELECT COUNT(*) FROM submission_attempts WHERE kind='submit' AND outcome='rejected'")
    items_entered = db.scalar(
        "SELECT COUNT(*) FROM run_steps WHERE step='item' AND status IN ('ok','warning')")
    verified_ok = counts.get("verified", 0)
    verified_bad = counts.get("verified_mismatch", 0)
    verified_total = verified_ok + verified_bad
    success_rate = None
    if verified_total:
        success_rate = round((verified_ok / verified_total) * 100, 2)
    submission_total = accepted_decl + rejected_attempts
    submission_rate = (round(accepted_decl / submission_total * 100, 2)
                       if submission_total else None)
    return {
        "documents": sum(counts.values()),
        "by_status": counts,
        "accepted_declarations": accepted_decl,
        "rejected_attempts": rejected_attempts,
        "items_entered": items_entered or 0,
        "verification_success_rate": success_rate,
        "submission_acceptance_rate": submission_rate,
        "verified_ok": verified_ok,
        "verified_mismatch": verified_bad,
        "submission_states": sub_counts,
        "open_rejections": db.scalar(
            "SELECT COUNT(*) FROM rejections WHERE status IN ('open','handling')") or 0,
        "uncertain": sub_counts.get("uncertain", 0),
    }


def daily_summary(db: Database, days: int = 30) -> list[dict]:
    """Per-day entry volume and success rate.

    * 录入商品项 — item steps (each saved line counts once)
    * 申报受理 / 退单 — submission attempts settled that day (a re-entry after a
      rejection is a second attempt, which is what a success *rate* should use)
    * 回读一致 / 差异 — *documents* whose latest verification fell on that day,
      so re-checking the same ticket does not inflate the numbers
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    buckets: dict[str, dict] = {}

    def bucket(day: str) -> dict:
        if day not in buckets:
            buckets[day] = {
                "day": day, "items_entered": 0, "submit_ok": 0, "submit_rejected": 0,
                "verified_ok": 0, "verified_mismatch": 0, "recovered_accepted": 0,
                "relogins": 0, "documents": 0,
            }
        return buckets[day]

    item_events = db.query(
        "SELECT created_at FROM run_steps WHERE created_at >= ? AND step='item' "
        "AND status IN ('ok','warning')", (since + "T00:00:00",))
    for e in item_events:
        bucket(e["created_at"][:10])["items_entered"] += 1

    relogins = db.query(
        "SELECT created_at FROM run_steps WHERE created_at >= ? AND step='session' "
        "AND status='warning'", (since + "T00:00:00",))
    for e in relogins:
        bucket(e["created_at"][:10])["relogins"] += 1

    attempts = db.query(
        "SELECT created_at, kind, outcome FROM submission_attempts "
        "WHERE created_at >= ? AND kind='submit'", (since + "T00:00:00",))
    for a in attempts:
        b = bucket(a["created_at"][:10])
        if a["outcome"] == "accepted":
            b["submit_ok"] += 1
        elif a["outcome"] == "rejected":
            b["submit_rejected"] += 1

    docs = db.query(
        "SELECT updated_at, status FROM documents WHERE updated_at >= ?", (since + "T00:00:00",))
    for d in docs:
        b = bucket(d["updated_at"][:10])
        b["documents"] += 1
        if d["status"] == "verified":
            b["verified_ok"] += 1
        elif d["status"] == "verified_mismatch":
            b["verified_mismatch"] += 1

    recovery = db.query(
        "SELECT created_at, message FROM run_steps WHERE created_at >= ? "
        "AND step IN ('recovery','preflight') AND status IN ('ok','warning')",
        (since + "T00:00:00",))
    for e in recovery:
        if e["message"] and "已有申报单" in e["message"]:
            bucket(e["created_at"][:10])["recovered_accepted"] += 1

    out = []
    for day in sorted(buckets):
        b = buckets[day]
        total_sub = b["submit_ok"] + b["submit_rejected"]
        b["accept_rate"] = (round(b["submit_ok"] / total_sub * 100, 2)
                            if total_sub else None)
        ver_total = b["verified_ok"] + b["verified_mismatch"]
        b["verify_match_rate"] = (round(b["verified_ok"] / ver_total * 100, 2)
                                  if ver_total else None)
        out.append(b)
    return out


def export_daily_excel(db: Database, out_dir: Path, days: int = 30) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"报关录入统计_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "每日统计"
    head_fill = PatternFill("solid", fgColor="0B5CAD")
    head_font = Font(color="FFFFFF", bold=True)
    headers = ["日期", "录入商品项数", "申报受理票数", "申报退单票数", "受理成功率(%)",
               "回读一致", "回读差异", "回读一致率(%)", "会话重登次数"]
    ws.append(headers)
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = head_fill
        cell.font = head_font
        cell.alignment = Alignment(horizontal="center")
    for b in daily_summary(db, days):
        ws.append([b["day"], b["items_entered"], b["submit_ok"], b["submit_rejected"],
                   b["accept_rate"], b["verified_ok"], b["verified_mismatch"],
                   b["verify_match_rate"], b["relogins"]])
    widths = [12, 14, 14, 14, 14, 10, 10, 14, 12]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    ws2 = wb.create_sheet("单据明细")
    headers2 = ["外部单号", "报关单号", "状态", "贸易方式", "项数", "导入时间",
                "申报时间", "回读差异数", "退单代码"]
    ws2.append(headers2)
    for c in range(1, len(headers2) + 1):
        cell = ws2.cell(row=1, column=c)
        cell.fill = head_fill
        cell.font = head_font
    rows = db.query(
        "SELECT d.external_ref, d.status, d.header_json, d.items_json, d.created_at, "
        "d.updated_at, d.compare_json, s.declaration_no, s.settled_at, r.reason_code "
        "FROM documents d LEFT JOIN submissions s ON s.doc_id=d.id "
        "LEFT JOIN rejections r ON r.doc_id=d.id ORDER BY d.sequence_no, d.id")
    for r in rows:
        header = json_loads(r["header_json"]) or {}
        items = json_loads(r["items_json"]) or []
        compare = json_loads(r["compare_json"]) or {}
        ws2.append([
            r["external_ref"], r["declaration_no"] or "",
            r["status"], header.get("trade_mode", ""), len(items),
            r["created_at"], r["settled_at"] or "",
            len(compare.get("differences") or []) if compare else "",
            r["reason_code"] or "",
        ])
    for i in range(1, len(headers2) + 1):
        ws2.column_dimensions[get_column_letter(i)].width = 18
    ws2.freeze_panes = "A2"

    ws3 = wb.create_sheet("退单统计")
    headers3 = ["退单代码", "类别", "原因", "次数", "已处理", "未处理", "处理建议"]
    ws3.append(headers3)
    for c in range(1, len(headers3) + 1):
        ws3.cell(row=1, column=c).fill = head_fill
        ws3.cell(row=1, column=c).font = head_font
    for r in rejection_stats(db):
        ws3.append([r["reason_code"], r["category"], r["reason_text"], r["count"],
                    r["resolved"], r["open"], r["hint"]])
    for i in range(1, len(headers3) + 1):
        ws3.column_dimensions[get_column_letter(i)].width = 22

    wb.save(path)
    return path


def rejection_stats(db: Database) -> list[dict]:
    rows = db.query(
        "SELECT reason_code, category, reason_text, hint, COUNT(*) n, "
        "SUM(CASE WHEN status IN ('resolved','waived') THEN 1 ELSE 0 END) resolved, "
        "SUM(CASE WHEN status IN ('open','handling') THEN 1 ELSE 0 END) open_n "
        "FROM rejections GROUP BY reason_code, reason_text, category, hint "
        "ORDER BY n DESC")
    return [{
        "reason_code": r["reason_code"] or "UNKNOWN",
        "category": r["category"] or "other",
        "reason_text": r["reason_text"] or "",
        "hint": r["hint"] or "",
        "count": r["n"], "resolved": r["resolved"] or 0, "open": r["open_n"] or 0,
    } for r in rows]


def rejection_categories_label(category: str | None) -> str:
    return {
        "element": "申报要素", "hs": "HS编码", "unit": "计量单位",
        "amount": "金额", "document": "随附单证", "party": "收发货人",
        "recovery": "恢复流程", "other": "其他",
    }.get(category or "other", "其他")
