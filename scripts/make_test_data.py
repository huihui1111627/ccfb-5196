"""Generate realistic test invoices (Excel + PDF) plus a manifest.

Usage:
    python scripts/make_test_data.py --out data/samples --count 12

The generated files deliberately include:
* multiple invoices per workbook / per PDF (拆票 testing)
* one invoice with >20 lines (auto split)
* known-but-invalid data (retired HS code, bad unit, amount mismatch,
  missing element, mismatched currency) to exercise validation

A manifest JSON records the expected parse result so tests can assert.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from openpyxl import Workbook
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from common import catalog
from common.formatting import parse_elements

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

SELLERS = [
    ("深圳市华南电子有限公司", "Shenzhen Huanan Electronics Co., Ltd."),
    ("宁波市海曙家具有限公司", "Ningbo Haishu Furniture Co., Ltd."),
    ("泉州市兴业服饰有限公司", "Quanzhou Xingye Garments Co., Ltd."),
    ("苏州精工五金制品有限公司", "Suzhou Jinggong Hardware Co., Ltd."),
]
BUYERS = [
    ("GLOBAL TRADING LLC", "美国"),
    ("HANSA IMPORT GMBH", "德国"),
    ("TOKYO SOGO CO., LTD.", "日本"),
    ("SAIGON DISTRIBUTION JSC", "越南"),
    ("HONG KONG BEST SOURCE LTD.", "中国香港"),
]
PORTS = catalog.PORTS
ELEMENT_VALUES = {
    "品牌类型": ["无品牌", "境内自主品牌", "境内收购品牌"],
    "出口享惠情况": ["出口货物在最终目的国不享受优惠关税", "出口货物在最终目的国享受优惠关税"],
}
BRANDS = ["LENOVO", "HUAWEI", "XIAOMI", "ANKER", "OEM"]
MATERIALS = ["ABS塑料", "不锈钢304", "棉95%涤5%", "实木橡木", "铝合金"]
USES = ["家用", "办公用", "商业展示", "礼品", "工业配套"]

def _elements_for(hs: dict, rng: random.Random) -> dict:
    out = {}
    for e in hs["elements"]:
        if e in ELEMENT_VALUES:
            out[e] = rng.choice(ELEMENT_VALUES[e])
        elif e == "品牌":
            out[e] = rng.choice(BRANDS)
        elif e == "型号":
            out[e] = f"{rng.choice(['T','X','P','M'])}{rng.randint(10,99)}"
        elif e == "材质":
            out[e] = rng.choice(MATERIALS)
        elif e == "用途":
            out[e] = rng.choice(USES)
        elif e == "面料":
            out[e] = rng.choice(["针织棉", "涤纶", "混纺"])
        elif e == "成分含量":
            out[e] = rng.choice(["棉100%", "棉60%涤40%", "涤100%"])
        elif e == "功率":
            out[e] = f"{rng.choice([45,65,100,120])}W"
        elif e == "长度":
            out[e] = f"{rng.choice([1,1.5,2,3])}m"
        elif e == "保质期":
            out[e] = rng.choice(["12个月", "18个月", "24个月"])
        else:
            out[e] = "无"
    return out


def build_invoice(rng: random.Random, inv_no: str, item_count: int,
                  defects: list[str] | None = None) -> dict:
    defects = defects or []
    mode = rng.choice(catalog.TRADE_MODES)
    buyer, dest = rng.choice(BUYERS)
    seller, _ = rng.choice(SELLERS)
    currency = rng.choice(["USD", "USD", "EUR", "USD"])
    header = {
        "external_ref": inv_no,
        "invoice_date": f"2026-{rng.randint(1, 10):02d}-{rng.randint(1, 28):02d}",
        "trade_mode": f"{mode['name']}({mode['code']})",
        "declaration_type": "出口",
        "port": rng.choice(PORTS),
        "consignor": seller,
        "consignee": buyer,
        "destination_country": dest,
        "currency": currency,
        "license_no": f"LIC-2026-{rng.randint(1000, 9999)}",
        "freight": f"{rng.randint(20, 400)}.00",
        "insurance": f"{rng.randint(5, 60)}.00",
        "processing_agreement_no": f"B2026{rng.randint(10000,99999)}",
        "bonded_ratio": f"{rng.choice([0, 30, 50, 70])}",
        "market_purchase_no": f"SC2026{rng.randint(1000,9999)}",
        "bonded_warehouse_no": f"BW{rng.randint(10000,99999)}",
        "remark": "",
    }
    items = []
    for i in range(1, item_count + 1):
        hs = rng.choice(catalog.HS_CATALOG)
        qty = rng.choice([10, 20, 50, 100, 300, 500, 1000])
        price = round(rng.uniform(0.5, 900), 2)
        element = _elements_for(hs, rng)
        item = {
            "item_no": i,
            "hs_code": hs["code"],
            "goods_name": hs["name"],
            "spec_model": f"{rng.choice(['A','B','C'])}{rng.randint(100,999)}",
            "quantity": str(qty),
            "unit": hs["unit"],
            "unit_price": f"{price:.2f}",
            "total_price": f"{qty * price:.2f}",
            "currency": currency,
            "origin_country": ("中国", "中国", "中国香港", "日本", "德国")[rng.randint(0, 4)],
            "destination_country": dest,
            "elements": element,
        }
        items.append(item)

    if "retired_hs" in defects:
        items[0]["hs_code"] = rng.choice(list(catalog.RETIRED_HS_CODES))
    if "unknown_hs" in defects:
        items[-1]["hs_code"] = "9999999999"
    if "bad_unit" in defects:
        items[0]["unit"] = "米"
    if "amount_mismatch" in defects:
        items[-1]["total_price"] = f"{float(items[-1]['total_price']) + 1.00:.2f}"
    if "missing_element" in defects:
        key = next(iter(items[0]["elements"]))
        items[0]["elements"].pop(key)
    if "bad_element_value" in defects:
        items[0]["elements"]["品牌类型"] = "自有品牌"
    if "currency_mismatch" in defects:
        items[-1]["currency"] = "CNY"
    if "missing_field" in defects:
        header["consignee"] = ""
    if "amount_zero" in defects:
        items[0]["quantity"] = "0"
    if "dup_item_no" in defects:
        items[-1]["item_no"] = 1

    return {"header": header, "items": items}


# ---------------------------------------------------------------------------
# Excel writer — flat sheet, one row per item, header columns repeat
# ---------------------------------------------------------------------------
EXCEL_HEADER_COLUMNS = [
    ("发票号 Invoice No.", "external_ref"),
    ("贸易方式", "trade_mode"),
    ("申报类型", "declaration_type"),
    ("申报口岸", "port"),
    ("境内发货人", "consignor"),
    ("境外收货人", "consignee"),
    ("运抵国", "destination_country"),
    ("币制", "currency"),
    ("许可证号", "license_no"),
    ("运费", "freight"),
    ("保险费", "insurance"),
    ("加工贸易协议号", "processing_agreement_no"),
    ("保税比例", "bonded_ratio"),
    ("市场采购单号", "market_purchase_no"),
    ("保税仓单号", "bonded_warehouse_no"),
    ("备注", "remark"),
]
EXCEL_ITEM_COLUMNS = [
    ("项号", "item_no"),
    ("HS编码", "hs_code"),
    ("商品名称", "goods_name"),
    ("规格型号", "spec_model"),
    ("数量", "quantity"),
    ("单位", "unit"),
    ("单价", "unit_price"),
    ("总价", "total_price"),
    ("原产国", "origin_country"),
    ("最终目的国", "destination_country"),
]


def elements_text(elements: dict) -> str:
    return "；".join(f"{k}={v}" for k, v in elements.items())


def _wrap_elements(elements: dict, width: int = 16) -> list[str]:
    """Wrap 申报要素 into short fragments so nothing is clipped in the PDF."""
    parts = [f"{k}={v}" for k, v in elements.items()]
    lines, current = [], ""
    for part in parts:
        piece = ("；" if current else "") + part
        while len(piece) > width:
            take = width - len(current)
            current += piece[:take]
            piece = piece[take:]
            lines.append(current)
            current = ""
        current += piece
        if len(current) >= width - 2:
            lines.append(current)
            current = ""
    if current:
        lines.append(current)
    return lines or [""]


def write_excel(path: Path, invoices: list[dict], rng: random.Random) -> None:
    wb = Workbook()
    wb.remove(wb.active)
    sheet = wb.create_sheet("发票清单")
    columns = EXCEL_HEADER_COLUMNS + EXCEL_ITEM_COLUMNS + [("申报要素", "elements")]
    money_style = "#,##0.00"
    for c, (label, _) in enumerate(columns, start=1):
        cell = sheet.cell(row=1, column=c, value=label)
        cell.font = cell.font.copy(bold=True)
    row = 2
    column_keys = [k for _, k in EXCEL_HEADER_COLUMNS]
    for inv in invoices:
        for item in inv["items"]:
            for c, (label, key) in enumerate(columns, start=1):
                if key in column_keys:
                    sheet.cell(row=row, column=c, value=inv["header"].get(key, ""))
                elif key == "elements":
                    sheet.cell(row=row, column=c, value=elements_text(item["elements"]))
                else:
                    value = item.get(key, "")
                    if key in ("quantity", "unit_price", "total_price"):
                        try:
                            value = float(value)
                        except (TypeError, ValueError):
                            pass
                    sheet.cell(row=row, column=c, value=value)
            row += 1
        # blank separator row
        row += 1
    for col_letter, width in zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                                 [22, 16, 10, 14, 30, 26, 12, 8, 14, 9, 9, 18, 12, 16, 16, 30,
                                  6, 14, 22, 14, 9, 9, 12, 12, 60]):
        sheet.column_dimensions[col_letter].width = width
    sheet.freeze_panes = "A2"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


# ---------------------------------------------------------------------------
# PDF writer — header block + item table, one invoice per 1-2 pages
# ---------------------------------------------------------------------------
def write_pdf(path: Path, invoices: list[dict], rng: random.Random) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    for inv in invoices:
        header = inv["header"]
        items = inv["items"]
        y = 800

        def line(text, size=10, dy=16, bold=False):
            nonlocal y
            c.setFont("STSong-Light", size)
            c.drawString(40, y, text)
            y -= dy

        c.setFillColorRGB(0.05, 0.24, 0.42)
        c.rect(0, 810, 595, 32, fill=1, stroke=0)
        c.setFillColorRGB(1, 1, 1)
        line("商业发票  COMMERCIAL INVOICE", 14, 26, True)
        c.setFillColorRGB(0, 0, 0)
        y = 780
        line(f"发票号 Invoice No.: {header['external_ref']}", 11, 18, True)
        line(f"开票日期 Date: {header['invoice_date']}", 9, 14)
        line(f"贸易方式: {header['trade_mode']}", 9, 14)
        line(f"申报类型: {header['declaration_type']}    申报口岸: {header['port']}", 9, 14)
        line(f"境内发货人 Consignor: {header['consignor']}", 9, 14)
        line(f"境外收货人 Consignee: {header['consignee']}", 9, 14)
        line(f"运抵国 Destination: {header['destination_country']}    币制 Currency: {header['currency']}", 9, 14)
        line(f"许可证号: {header.get('license_no','')}    加工贸易协议号: {header.get('processing_agreement_no','')}", 9, 14)
        line(f"运费 Freight: {header.get('freight','')}    保险费 Insurance: {header.get('insurance','')}", 9, 14)
        if header.get("market_purchase_no"):
            line(f"市场采购单号: {header['market_purchase_no']}", 9, 14)
        if header.get("bonded_warehouse_no"):
            line(f"保税仓单号: {header['bonded_warehouse_no']}    保税比例: {header.get('bonded_ratio','')}%", 9, 14)
        y -= 8
        # table header: one padded string so extraction keeps columns on a row
        cols = [("项号", 34), ("HS编码", 74), ("商品名称", 92), ("规格型号", 60),
                ("数量", 40), ("单位", 32), ("单价", 44), ("总价", 52),
                ("原产国", 44), ("最终目的国", 56)]
        c.setFont("STSong-Light", 8)
        c.setFillColorRGB(0.90, 0.93, 0.96)
        c.rect(40, y - 4, 515, 16, fill=1, stroke=0)
        c.setFillColorRGB(0, 0, 0)
        header_line = "  ".join(lbl for lbl, _ in cols) + "  申报要素"
        c.drawString(44, y, header_line)
        y -= 16
        c.line(40, y + 10, 555, y + 10)
        page_break_at = 80
        for item in items:
            element_lines = _wrap_elements(item["elements"], 16)
            row_height = max(14, 9 * len(element_lines))
            if y - row_height < page_break_at:
                c.showPage()
                y = 800
                c.setFont("STSong-Light", 8)
            # numeric / descriptive cells as one padded row string
            row_cells = "  ".join([
                str(item["item_no"]), item["hs_code"], item["goods_name"],
                item["spec_model"], item["quantity"], item["unit"],
                item["unit_price"], item["total_price"],
                item["origin_country"], item["destination_country"],
            ])
            c.setFont("STSong-Light", 8)
            c.drawString(44, y, row_cells)
            # elements wrapped in the narrow right column, one fragment per line
            c.setFont("STSong-Light", 6.5)
            ey = y
            for chunk in element_lines:
                c.drawString(474, ey, chunk)
                ey -= 9
            y -= row_height
        y -= 6
        c.line(40, y + 10, 555, y + 10)
        total = sum(float(i["total_price"]) for i in items)
        c.setFont("STSong-Light", 9)
        c.drawString(40, y, f"合计 Total: {header['currency']} {total:,.2f}    共 {len(items)} 项")
        y -= 12
        c.drawString(40, y, "备注: 本发票仅供报关使用，数据以实际货物为准。")
        y -= 20
        c.drawString(40, y, "（盖章）SELLER STAMP")
        c.showPage()
    c.save()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/samples")
    ap.add_argument("--count", type=int, default=8, help="number of source files")
    ap.add_argument("--seed", type=int, default=20261113)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"excel_files": [], "pdf_files": [], "expected": []}

    excel_invoices = []
    for i in range(1, 6):
        inv_no = f"INV-2026-{1000 + i}"
        defects = []
        count = rng.choice([1, 2, 3, 4])
        if i == 2:
            defects = ["retired_hs"]
        if i == 4:
            defects = ["amount_mismatch", "missing_element"]
        excel_invoices.append(build_invoice(rng, inv_no, count, defects))
    # a big one to force 拆票 (24 items -> 20 + 4)
    excel_invoices.append(build_invoice(rng, "INV-2026-2001", 24, []))
    xlsx = out / "客户发票_华东_20261113.xlsx"
    write_excel(xlsx, excel_invoices, rng)
    manifest["excel_files"].append(str(xlsx))
    for inv in excel_invoices:
        manifest["expected"].append({
            "external_ref": inv["header"]["external_ref"],
            "items": len(inv["items"]),
            "source_file": xlsx.name,
        })

    pdf_invoices = []
    for i in range(1, 4):
        inv_no = f"PDF-2026-{2000 + i}"
        defects = (["bad_unit"] if i == 1 else
                   ["currency_mismatch", "bad_element_value"] if i == 3 else [])
        pdf_invoices.append(build_invoice(rng, inv_no, rng.choice([1, 2, 3]), defects))
    pdf_invoices.append(build_invoice(rng, "PDF-2026-2004", 22, []))
    pdf1 = out / "客户发票_华南_20261113.pdf"
    write_pdf(pdf1, pdf_invoices[:2], rng)
    pdf2 = out / "客户发票_华北_20261113.pdf"
    write_pdf(pdf2, pdf_invoices[2:], rng)
    manifest["pdf_files"] = [str(pdf1), str(pdf2)]
    for inv in pdf_invoices:
        manifest["expected"].append({
            "external_ref": inv["header"]["external_ref"],
            "items": len(inv["items"]),
            "source_file": (pdf1 if inv in pdf_invoices[:2] else pdf2).name,
        })

    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"Wrote {len(manifest['excel_files'])} Excel and {len(manifest['pdf_files'])} PDF files to {out}")


if __name__ == "__main__":
    main()
