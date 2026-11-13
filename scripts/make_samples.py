"""Generate realistic sample invoices (Excel + PDF) for the declarant system.

Also optionally seeds the mock customs system with historical declarations
(approved / rejected) so the reconciliation and rejection pages have content.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from declarant.common import CATALOG_PATH, catalog  # noqa: E402

BUYERS = [
    ("上海远洋国际贸易有限公司", "SHANGHAI YUANYANG INTERNATIONAL TRADING CO., LTD."),
    ("深圳市海纳供应链有限公司", "SHENZHEN HAINA SUPPLY CHAIN CO., LTD."),
    ("苏州市明达电子科技有限公司", "SUZHOU MINGDA ELECTRONICS TECH CO., LTD."),
    ("宁波保税区华通物流有限公司", "NINGBO FREE TRADE ZONE HUATONG LOGISTICS CO., LTD."),
    ("北京中盛科技发展有限公司", "BEIJING ZHONGSHENG TECHNOLOGY DEVELOPMENT CO., LTD."),
]
SUPPLIERS = [
    ("日本东京电子株式会社", "TOKYO ELECTRON PARTS CO., LTD.", "JPN"),
    ("德国汉莎工业品有限公司", "HANSA INDUSTRIE GMBH", "DEU"),
    ("韩国三星贸易株式会社", "SAMSUNG TRADING CO., LTD.", "KOR"),
    ("意大利米兰皮革贸易公司", "MILANO LEATHER TRADING S.R.L.", "ITA"),
    ("美国西部仪器公司", "WESTERN INSTRUMENTS INC.", "USA"),
]
MODES = [("0110", "一般贸易"), ("0615", "进料对口"), ("0214", "来料加工"),
         ("1200", "保税仓储货物"), ("1210", "保税电商"), ("2600", "暂时进出境货物"),
         ("4561", "退运货物"), ("1523", "租赁贸易")]
PORTS = ["2201", "2210", "3100", "5300", "0101", "4701", "2900"]
TRANSPORT = ["WATER", "AIR", "RAIL", "ROAD"]
TERMS = ["CIF", "FOB", "CFR", "EXW"]
PACKING = ["CARTON", "WOODEN", "PALLET", "BAG", "DRUM"]


def make_invoice(rng: random.Random, idx: int) -> dict:
    cat = catalog()
    products = rng.sample(cat["products"], rng.randint(1, 4))
    mode_code, mode_label = rng.choice(MODES)
    supplier = rng.choice(SUPPLIERS)
    buyer = rng.choice(BUYERS)
    year = 2026
    month = rng.randint(9, 11)
    day = rng.randint(1, 28)
    inv_date = date(year, month, day)
    invoice_no = f"CI-{year}{month:02d}-{idx:04d}"
    contract = f"CT-{year}-{idx:03d}"
    currency = "USD"
    rate = 7.12
    items = []
    for seq, prod in enumerate(products, 1):
        qty = rng.choice([10, 20, 50, 100, 120, 200, 300, 500, 800, 1000, 1500])
        if prod["hs_code"] in ("0902300000", "2204210000", "3926909090", "7326909000"):
            qty = round(rng.uniform(50, 2000), 2)
        unit_price = round(rng.uniform(0.8, 900) if prod["unit"] != "千克" else rng.uniform(0.5, 12), 2)
        total = round(qty * unit_price, 2)
        items.append({
            "seq": seq,
            "hs_code": prod["hs_code"],
            "goods_name": prod["name"],
            "specification": prod["spec"],
            "brand": prod["brand"],
            "model": prod["model"],
            "quantity": qty,
            "unit": prod["unit"],
            "legal_quantity": round(qty * prod.get("legal_qty_per_unit", 1), 3),
            "legal_unit": cat["hs_codes"][prod["hs_code"]]["legal_unit"],
            "second_quantity": (round(qty * prod["second_qty_per_unit"], 3)
                               if prod.get("second_qty_per_unit") else None),
            "second_unit": cat["hs_codes"][prod["hs_code"]].get("second_unit"),
            "unit_price": unit_price,
            "total_price": total,
            "origin_country": prod["origin_country"],
            "elements": dict(prod["elements"]),
        })
    total_amount = round(sum(i["total_price"] for i in items), 2)
    gross = round(sum(i["total_price"] for i in items) * 0.05 + rng.uniform(20, 200), 2)
    linked = _linked_for(mode_code, rng, contract, invoice_no)
    return {
        "invoice_no": invoice_no,
        "client_ref": invoice_no,
        "invoice_date": inv_date.isoformat(),
        "contract_no": contract,
        "trade_mode": mode_code,
        "trade_mode_label": mode_label,
        "supplier": supplier[0],
        "supplier_en": supplier[1],
        "buyer": buyer[0],
        "buyer_en": buyer[1],
        "origin_country": supplier[2],
        "trade_country": supplier[2],
        "dest_country": "USA",
        "port_code": rng.choice(PORTS),
        "transport_mode": rng.choice(TRANSPORT),
        "vessel_voyage": f"VOY-{rng.randint(1000, 9999)}/{rng.randint(1, 9)}",
        "trade_terms": rng.choice(TERMS),
        "currency": currency,
        "exchange_rate": rate,
        "freight": round(rng.uniform(30, 500), 2) if mode_code == "0110" else 0,
        "insurance": round(rng.uniform(5, 80), 2) if mode_code == "0110" else 0,
        "other_charges": 0,
        "package_count": rng.randint(5, 200),
        "packing_type": rng.choice(PACKING),
        "gross_weight": gross,
        "net_weight": round(gross * rng.uniform(0.85, 0.95), 2),
        "total_amount": total_amount,
        "items": items,
        **linked,
    }


def _linked_for(mode: str, rng: random.Random, contract: str, invoice_no: str) -> dict:
    if mode in ("0615", "0214"):
        return {"processing_ratio": rng.choice([50, 60, 70, 80, 100]),
                "bonded_register_no": f"B-{rng.randint(20250000, 20259999)}"}
    if mode == "1200":
        return {"warehouse_no": f"WH-{rng.randint(100, 999)}",
                "storage_days": rng.choice([30, 60, 90, 180])}
    if mode == "1210":
        return {"ecommerce_order_no": f"EC{invoice_no[-7:]}{rng.randint(1,99)}",
                "ecommerce_platform": rng.choice(["天猫国际", "京东国际", "拼多多国际", "抖音国际"])}
    if mode == "2600":
        return {"guarantee_no": f"GT-{rng.randint(100000, 999999)}",
                "guarantee_amount": rng.randint(5000, 200000)}
    if mode == "4561":
        return {"original_decl_no": f"22312025{rng.randint(1000,9999)}",
                "return_reason": rng.choice(["品质不符", "规格不符", "订单取消", "错发货物"])}
    if mode == "1523":
        return {"lease_term_months": rng.choice([6, 12, 24, 36])}
    return {}


# --------------------------------------------------------------------------
# Excel writer — one invoice per sheet, some sheets split into 2 invoices
# --------------------------------------------------------------------------
def write_excel(invoices: list[dict], path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    thin = Side(style="thin", color="B0BEC5")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="1B4F9C")
    for idx, inv in enumerate(invoices):
        ws = wb.create_sheet(title=f"{inv['invoice_no'][-6:]}-{idx}")
        ws.merge_cells("A1:F1")
        ws["A1"] = f"{inv['supplier_en']}\nCOMMERCIAL INVOICE / 商业发票"
        ws["A1"].font = Font(size=14, bold=True)
        ws["A1"].alignment = Alignment(horizontal="center")
        rows = [
            ("发票号 Invoice No.", inv["invoice_no"], "发票日期 Date", inv["invoice_date"]),
            ("合同号 Contract No.", inv["contract_no"], "贸易方式 Trade Mode",
             f"{inv['trade_mode']} {inv['trade_mode_label']}"),
            ("供货方 Supplier", inv["supplier"], "采购方 Buyer", inv["buyer"]),
            ("成交方式 Term", inv["trade_terms"], "币制 Currency", inv["currency"]),
            ("启运国 Origin", inv["origin_country"], "运抵国 Destination", inv["dest_country"]),
            ("申报地海关 Port", inv["port_code"], "运输方式 Transport", inv["transport_mode"]),
            ("运输工具 Vessel/Voyage", inv["vessel_voyage"], "包装件数 Packages", inv["package_count"]),
            ("包装种类 Packing", inv["packing_type"], "毛重 Gross Weight", inv["gross_weight"]),
            ("净重 Net Weight", inv["net_weight"], "总价 Total Amount", inv["total_amount"]),
        ]
        r = 3
        for a, b_, c, d in rows:
            ws.cell(r, 1, a).font = Font(bold=True, color="455A64")
            ws.cell(r, 2, b_)
            ws.cell(r, 4, c).font = Font(bold=True, color="455A64")
            ws.cell(r, 5, d)
            ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=3)
            ws.merge_cells(start_row=r, start_column=5, end_row=r, end_column=6)
            r += 1
        for sfx in ("processing_ratio", "bonded_register_no", "warehouse_no", "storage_days",
                    "ecommerce_order_no", "ecommerce_platform", "guarantee_no", "guarantee_amount",
                    "original_decl_no", "return_reason", "lease_term_months"):
            if inv.get(sfx) not in (None, "", 0):
                label = {"processing_ratio": "加工贸易比例(%)", "bonded_register_no": "加工贸易手册号",
                         "warehouse_no": "保税仓库号", "storage_days": "仓储期限(天)",
                         "ecommerce_order_no": "电商订单号", "ecommerce_platform": "电商平台",
                         "guarantee_no": "担保编号", "guarantee_amount": "担保金额",
                         "original_decl_no": "原报关单号", "return_reason": "退运原因",
                         "lease_term_months": "租赁期限(月)"}[sfx]
                ws.cell(r, 1, label).font = Font(bold=True, color="455A64")
                ws.cell(r, 2, inv[sfx])
                r += 1

        header_row = r + 1
        cols = ["项号", "HS编码", "商品名称", "规格型号", "品牌", "数量", "单位",
                "单价", "总价", "原产国"]
        for ci, title in enumerate(cols, start=1):
            cell = ws.cell(header_row, ci, title)
            cell.fill = head_fill
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(horizontal="center")
            cell.border = border
        for i, it in enumerate(inv["items"], start=1):
            vals = [it["seq"], it["hs_code"], it["goods_name"], it["specification"], it["brand"],
                    it["quantity"], it["unit"], it["unit_price"], it["total_price"],
                    it["origin_country"]]
            for ci, v in enumerate(vals, start=1):
                cell = ws.cell(header_row + i, ci, v)
                cell.border = border
                if ci in (2, 4):
                    cell.number_format = "@"
                if ci in (8, 9):
                    cell.number_format = "0.00"
        last = header_row + len(inv["items"])
        ws.cell(last + 1, 1, "合计 Total").font = Font(bold=True)
        ws.cell(last + 1, 9, f"{inv['currency']} {inv['total_amount']}")
        ws.cell(last + 2, 1, "申报要素 Declaration Elements")
        ws.cell(last + 2, 1).font = Font(bold=True, color="455A64")
        for i, it in enumerate(inv["items"], start=1):
            els = "；".join(f"{k}={v}" for k, v in it["elements"].items())
            ws.cell(last + 2 + i, 1, f"{it['seq']}. {els}").alignment = Alignment(wrap_text=True)
            ws.merge_cells(start_row=last + 2 + i, start_column=1,
                           end_row=last + 2 + i, end_column=10)
        widths = [8, 14, 22, 20, 12, 10, 8, 10, 12, 10]
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = ws.cell(header_row + 1, 1)
    wb.save(path)


def write_excel_multi(invoices: list[dict], path: Path) -> None:
    """One sheet containing two invoices stacked, to exercise invoice splitting."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "INV A+B"
    row = 1
    for inv in invoices:
        ws.cell(row, 1, f"商业发票 {inv['invoice_no']}").font = __import__("openpyxl").styles.Font(size=13, bold=True)
        row += 1
        meta = [("发票号", inv["invoice_no"]), ("合同号", inv["contract_no"]),
                ("贸易方式", f"{inv['trade_mode']} {inv['trade_mode_label']}"),
                ("供货方", inv["supplier"]), ("采购方", inv["buyer"]),
                ("成交方式", inv["trade_terms"]), ("币制", inv["currency"]),
                ("申报地海关", inv["port_code"]), ("运输方式", inv["transport_mode"]),
                ("包装种类", inv["packing_type"]), ("包装件数", inv["package_count"]),
                ("毛重", inv["gross_weight"]), ("净重", inv["net_weight"]),
                ("原产国", inv["origin_country"]),
                ("发票日期", inv["invoice_date"])]
        extra = {"processing_ratio": "加工贸易比例(%)", "bonded_register_no": "加工贸易手册号",
                 "warehouse_no": "保税仓库号", "storage_days": "仓储期限(天)",
                 "ecommerce_order_no": "电商订单号", "ecommerce_platform": "电商平台",
                 "guarantee_no": "担保编号", "guarantee_amount": "担保金额",
                 "original_decl_no": "原报关单号", "return_reason": "退运原因",
                 "lease_term_months": "租赁期限(月)"}
        for k, label in extra.items():
            if inv.get(k) not in (None, "", 0):
                meta.append((label, inv[k]))
        for k, v in meta:
            ws.cell(row, 1, k)
            ws.cell(row, 2, v)
            row += 1
        headers = ["项号", "HS编码", "商品名称", "规格型号", "品牌", "数量", "单位", "单价", "总价", "原产国"]
        for ci, h in enumerate(headers, 1):
            ws.cell(row, ci, h).font = __import__("openpyxl").styles.Font(bold=True)
        row += 1
        for it in inv["items"]:
            vals = [it["seq"], it["hs_code"], it["goods_name"], it["specification"], it["brand"],
                    it["quantity"], it["unit"], it["unit_price"], it["total_price"], it["origin_country"]]
            for ci, v in enumerate(vals, 1):
                ws.cell(row, ci, v)
            row += 1
        ws.cell(row, 1, "申报要素")
        row += 1
        for it in inv["items"]:
            ws.cell(row, 1, f"{it['seq']}. " + "；".join(
                f"{k}={v}" for k, v in it["elements"].items()))
            row += 1
        row += 1
    wb.save(path)


# --------------------------------------------------------------------------
# PDF writer — Chinese invoice using reportlab CJK font
# --------------------------------------------------------------------------
def _register_font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for name, path in [("InvoiceCN", "/System/Library/Fonts/STHeiti Medium.ttc")]:
        try:
            pdfmetrics.registerFont(TTFont(name, path, subfontIndex=0))
            return name
        except Exception:
            continue
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"


def write_pdf(inv: dict, path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    font = _register_font()
    style = ParagraphStyle("cn", fontName=font, fontSize=9, leading=12)
    title_style = ParagraphStyle("title", fontName=font, fontSize=15, leading=20,
                                 alignment=1, spaceAfter=6)
    doc = SimpleDocTemplate(str(path), pagesize=A4, topMargin=14 * mm, bottomMargin=14 * mm,
                            leftMargin=14 * mm, rightMargin=14 * mm,
                            title=inv["invoice_no"], author=inv["supplier_en"])
    story = [
        Paragraph("商业发票 / COMMERCIAL INVOICE", title_style),
        Paragraph(f"<b>{inv['supplier_en']}</b><br/>{inv['supplier']}", style),
        Spacer(1, 6),
    ]
    meta_rows = [
        ["发票号 Invoice No.", inv["invoice_no"], "发票日期 Date", inv["invoice_date"]],
        ["合同号 Contract No.", inv["contract_no"], "贸易方式 Trade Mode",
         f"{inv['trade_mode']} {inv['trade_mode_label']}"],
        ["供货方 Supplier", inv["supplier"], "采购方 Buyer", inv["buyer"]],
        ["成交方式 Term", inv["trade_terms"], "币制 Currency", inv["currency"]],
        ["启运国 Origin", inv["origin_country"], "运抵国 Destination", inv["dest_country"]],
        ["申报地海关 Port", inv["port_code"], "运输方式 Transport", inv["transport_mode"]],
        ["运输工具 Vessel/Voyage", inv["vessel_voyage"], "包装件数 Packages", str(inv["package_count"])],
        ["包装种类 Packing", inv["packing_type"], "毛重 Gross Weight", str(inv["gross_weight"])],
        ["净重 Net Weight", str(inv["net_weight"]), "总价 Total Amount", str(inv["total_amount"])],
    ]
    for sfx in ("processing_ratio", "bonded_register_no", "warehouse_no", "storage_days",
                "ecommerce_order_no", "ecommerce_platform", "guarantee_no", "guarantee_amount",
                "original_decl_no", "return_reason", "lease_term_months"):
        if inv.get(sfx) not in (None, "", 0):
            label = {"processing_ratio": "加工贸易比例(%)", "bonded_register_no": "加工贸易手册号",
                     "warehouse_no": "保税仓库号", "storage_days": "仓储期限(天)",
                     "ecommerce_order_no": "电商订单号", "ecommerce_platform": "电商平台",
                     "guarantee_no": "担保编号", "guarantee_amount": "担保金额",
                     "original_decl_no": "原报关单号", "return_reason": "退运原因",
                     "lease_term_months": "租赁期限(月)"}[sfx]
            meta_rows.append([label, str(inv[sfx]), "", ""])
    t = Table([[Paragraph(str(c), style) for c in row] for row in meta_rows],
              colWidths=[42 * mm, 40 * mm, 42 * mm, 40 * mm])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#90A4AE")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F1F5F9")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#F1F5F9")),
    ]))
    story += [t, Spacer(1, 10)]
    headers = ["项号", "HS编码", "商品名称", "规格型号", "品牌", "数量", "单位", "单价", "总价", "原产国"]
    data = [[Paragraph(f"<b>{h}</b>", style) for h in headers]]
    for it in inv["items"]:
        data.append([Paragraph(str(v), style) for v in
                     [it["seq"], it["hs_code"], it["goods_name"], it["specification"],
                      it["brand"], it["quantity"], it["unit"], it["unit_price"],
                      it["total_price"], it["origin_country"]]])
    widths = [10 * mm, 22 * mm, 30 * mm, 28 * mm, 16 * mm, 16 * mm, 10 * mm,
              15 * mm, 16 * mm, 14 * mm]
    t2 = Table(data, colWidths=widths, repeatRows=1)
    t2.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#90A4AE")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1B4F9C")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
    ]))
    story += [t2, Spacer(1, 6),
              Paragraph(f"<b>合计 Total：{inv['currency']} {inv['total_amount']}</b>", style),
              Spacer(1, 10),
              Paragraph("申报要素 Declaration Elements", ParagraphStyle(
                  "h", fontName=font, fontSize=10, leading=14, spaceAfter=4,
                  textColor=colors.HexColor("#1B4F9C")))]
    for it in inv["items"]:
        els = "；".join(f"{k}={v}" for k, v in it["elements"].items())
        story.append(Paragraph(f"{it['seq']}. {els}", style))
    story += [Spacer(1, 12),
              Paragraph("供货方签章：__________________________　日期：" + inv["invoice_date"], style)]
    doc.build(story)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="samples")
    parser.add_argument("--count", type=int, default=8, help="number of invoices to generate")
    parser.add_argument("--seed", type=int, default=20261113)
    parser.add_argument("--pdf", type=int, default=3, help="how many of them as PDF")
    parser.add_argument("--split", type=int, default=1, help="excel sheets containing 2 invoices")
    args = parser.parse_args()
    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    invoices = [make_invoice(rng, i + 1) for i in range(args.count)]

    # normal excel: first sheet holds two invoices to exercise splitting
    split_no = args.split
    normal = invoices[split_no:]
    write_excel(normal, out / f"invoices_{len(normal)}sheets.xlsx")
    if split_no:
        write_excel_multi(invoices[:split_no * 2], out / "invoices_multi.xlsx")
    for i, inv in enumerate(invoices[: args.pdf]):
        write_pdf(inv, out / f"invoice_{inv['invoice_no']}.pdf")
    manifest = out / "manifest.json"
    manifest.write_text(json.dumps({"invoices": invoices}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"generated {len(invoices)} invoices -> {out}")
    for inv in invoices:
        print(f"  {inv['invoice_no']}  {inv['trade_mode']}  items={len(inv['items'])}  "
              f"total={inv['total_amount']}")


if __name__ == "__main__":
    main()
