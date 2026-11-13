"""Shared between the mock customs system and the broker-side automation.

Keeping the trade-mode -> field matrix, HS catalog and declaration-element rules in
one place means the mock system and the importer can never drift apart.
"""

# ---------------------------------------------------------------------------
# Trade modes (贸易方式).  Field `requires` drives which extra fields appear in the
# declaration form.  The mock UI shows them dynamically after a mode change.
# ---------------------------------------------------------------------------
TRADE_MODES = [
    {
        "code": "0100",
        "name": "一般贸易",
        "requires": ["license_no", "freight", "insurance"],
        "prefill": {},
        "desc": "常规进出口货物，需填许可证号、运费、保险费",
    },
    {
        "code": "0214",
        "name": "来料加工",
        "requires": ["processing_agreement_no", "bonded_ratio", "freight"],
        "prefill": {},
        "desc": "来料加工贸易，需填加工贸易协议号与保税比例",
    },
    {
        "code": "0615",
        "name": "进料对口",
        "requires": ["processing_agreement_no", "freight", "insurance", "license_no"],
        "prefill": {},
        "desc": "进料加工对口合同，需合同号、运费、保险",
    },
    {
        "code": "1039",
        "name": "市场采购",
        "requires": ["market_purchase_no", "freight"],
        "prefill": {},
        "desc": "市场采购贸易，需市场采购单号",
    },
    {
        "code": "1210",
        "name": "保税电商",
        "requires": ["bonded_warehouse_no", "freight", "insurance"],
        "prefill": {},
        "desc": "保税跨境电商，需保税仓单号",
    },
]

TRADE_MODE_BY_CODE = {m["code"]: m for m in TRADE_MODES}


def mode_name(code: str) -> str:
    m = TRADE_MODE_BY_CODE.get(code)
    return m["name"] if m else f"未知贸易方式({code})"


def required_fields(code: str) -> list[str]:
    m = TRADE_MODE_BY_CODE.get(code)
    return list(m["requires"]) if m else []


# Dynamic fields are *also* shown for every mode but hidden; a real customs UI
# often has a few always-visible optional fields.  We model each dynamic field
# with a label so both UIs render identically.
FIELD_LABELS = {
    "license_no": "许可证号",
    "freight": "运费(USD)",
    "insurance": "保险费(USD)",
    "processing_agreement_no": "加工贸易协议号",
    "bonded_ratio": "保税比例(%)",
    "market_purchase_no": "市场采购单号",
    "bonded_warehouse_no": "保税仓单号",
}

# ---------------------------------------------------------------------------
# HS catalog (8-digit).  Each code carries what a real 申报要素 would require.
# ---------------------------------------------------------------------------
HS_CATALOG = [
    {
        "code": "8471301000",
        "name": "便携式笔记本电脑",
        "unit": "台",
        "elements": ["品牌类型", "出口享惠情况", "品牌", "型号"],
        "valid_units": ["台", "个", "千克"],
    },
    {
        "code": "8517131000",
        "name": "智能手机",
        "unit": "台",
        "elements": ["品牌类型", "出口享惠情况", "品牌", "型号"],
        "valid_units": ["台", "个", "千克"],
    },
    {
        "code": "8504402000",
        "name": "电源适配器",
        "unit": "个",
        "elements": ["品牌类型", "出口享惠情况", "品牌", "型号", "功率"],
        "valid_units": ["个", "台", "千克"],
    },
    {
        "code": "3926909090",
        "name": "塑料制品",
        "unit": "千克",
        "elements": ["品牌类型", "出口享惠情况", "材质", "用途"],
        "valid_units": ["千克", "个"],
    },
    {
        "code": "6109100090",
        "name": "棉制针织T恤",
        "unit": "件",
        "elements": ["品牌类型", "出口享惠情况", "品牌", "面料", "成分含量"],
        "valid_units": ["件", "千克"],
    },
    {
        "code": "9403609090",
        "name": "木制家具",
        "unit": "件",
        "elements": ["品牌类型", "出口享惠情况", "材质", "用途"],
        "valid_units": ["件", "套", "千克"],
    },
    {
        "code": "7321111000",
        "name": "不锈钢餐具",
        "unit": "千克",
        "elements": ["品牌类型", "出口享惠情况", "材质", "用途"],
        "valid_units": ["千克", "套"],
    },
    {
        "code": "8544421990",
        "name": "数据线",
        "unit": "个",
        "elements": ["品牌类型", "出口享惠情况", "品牌", "型号", "长度"],
        "valid_units": ["个", "根", "千克"],
    },
    {
        "code": "9503002100",
        "name": "塑料玩具",
        "unit": "个",
        "elements": ["品牌类型", "出口享惠情况", "品牌", "材质"],
        "valid_units": ["个", "件", "千克"],
    },
    {
        "code": "2101111000",
        "name": "速溶咖啡",
        "unit": "千克",
        "elements": ["品牌类型", "出口享惠情况", "品牌", "保质期"],
        "valid_units": ["千克", "箱"],
    },
]

HS_BY_CODE = {h["code"]: h for h in HS_CATALOG}

# Element values accepted by the validator.  `brand_type` and `export_pref`
# have controlled vocabularies; the rest are free text.
ELEMENT_VOCAB = {
    "品牌类型": ["无品牌", "境内自主品牌", "境内收购品牌", "境外品牌(贴牌生产)", "境外品牌(其他)"],
    "出口享惠情况": ["出口货物在最终目的国不享受优惠关税", "出口货物在最终目的国享受优惠关税", "不能确定"],
}

# Legacy / retired HS codes that must be rejected during import.
RETIRED_HS_CODES = {
    "8471300000": "已于2023年调整，请使用 8471301000",
    "8517121000": "旧版手机编码，请使用 8517131000",
}

# ---------------------------------------------------------------------------
# Rejection (退单) reasons returned randomly by the mock system.  Each has a
# code, a message template and the operator action expected.
# ---------------------------------------------------------------------------
REJECTION_REASONS = [
    {
        "code": "R01",
        "message": "申报要素填报不完整：{element}必填未填",
        "category": "element",
        "hint": "补齐申报要素后重录",
    },
    {
        "code": "R02",
        "message": "HS编码与货物名称不符：{hs} 对应名称为{hs_name}",
        "category": "hs",
        "hint": "核对HS编码与品名，必要时拆分申报",
    },
    {
        "code": "R03",
        "message": "数量单位与法定计量单位不一致：填报{unit}，法定单位{legal_unit}",
        "category": "unit",
        "hint": "按法定计量单位换算后重录",
    },
    {
        "code": "R04",
        "message": "总价与单价×数量计算结果不符：申报{total}，应为{expected}",
        "category": "amount",
        "hint": "核对金额，必要时修改发票",
    },
    {
        "code": "R05",
        "message": "贸易方式与随附单证不匹配：{mode}需提供{field}",
        "category": "document",
        "hint": "补充随附单证编号后重录",
    },
    {
        "code": "R06",
        "message": "境内发货人与报关单收发货人不一致",
        "category": "party",
        "hint": "核对发货人信息后重录",
    },
]

# Rejections that can plausibly be raised on otherwise-valid data (人工审单
# style).  They deliberately avoid claims that contradict the payload -- a
# random "总价237850.00应为237850.00" would destroy trust in the queue.  The
# data-contradicting reasons (R03/R04/R05) are only produced by real validation.
RANDOM_REJECTION_REASONS = [
    {
        "code": "T01",
        "message": "人工审单：{goods_name}归类存疑，请补充归类说明及用途证明",
        "category": "hs",
        "hint": "补充归类说明后重新申报",
    },
    {
        "code": "T02",
        "message": "人工审单：随附单证与申报信息需进一步核对，请补充合同复印件",
        "category": "document",
        "hint": "补齐随附单证扫描件后重新申报",
    },
    {
        "code": "T03",
        "message": "人工审单：申报要素描述不够具体（{goods_name}），请细化品牌、型号及用途",
        "category": "element",
        "hint": "细化申报要素后重新申报",
    },
    {
        "code": "T04",
        "message": "人工审单：收货人信息需与提单保持一致，请核对后重新申报",
        "category": "party",
        "hint": "核对收发货人信息后重新申报",
    },
    {
        "code": "T05",
        "message": "人工审单：该票价格水平偏离同期同类货物，请提供价格说明",
        "category": "amount",
        "hint": "补充价格构成说明后重新申报",
    },
]

REJECTION_BY_CODE = {r["code"]: r for r in REJECTION_REASONS + RANDOM_REJECTION_REASONS}

DECLARATION_TYPES = ["出口", "进口"]

CURRENCIES = ["USD", "EUR", "CNY", "JPY", "HKD"]

COUNTRIES = ["中国", "中国香港", "美国", "德国", "日本", "越南", "韩国", "英国", "澳大利亚"]

PORTS = ["上海洋山港", "深圳盐田港", "宁波北仑港", "青岛前湾港", "广州南沙港"]

# ---------------------------------------------------------------------------
# Field set of a single declaration.  The source documents (Excel / PDF) use
# these exact header keys.  The automation sends this dict to the mock system.
# ---------------------------------------------------------------------------
DECLARATION_FIELD_LABELS = {
    "declaration_no": "报关单号",
    "trade_mode": "贸易方式",
    "declaration_type": "申报类型",
    "port": "申报口岸",
    "consignor": "境内发货人",
    "consignee": "境外收货人",
    "destination_country": "运抵国",
    "currency": "币制",
    "license_no": "许可证号",
    "freight": "运费(USD)",
    "insurance": "保险费(USD)",
    "processing_agreement_no": "加工贸易协议号",
    "bonded_ratio": "保税比例(%)",
    "market_purchase_no": "市场采购单号",
    "bonded_warehouse_no": "保税仓单号",
    "remark": "备注",
}

ITEM_FIELD_LABELS = {
    "item_no": "项号",
    "hs_code": "HS编码",
    "goods_name": "商品名称",
    "spec_model": "规格型号",
    "quantity": "数量",
    "unit": "成交计量单位",
    "unit_price": "单价",
    "total_price": "总价",
    "currency": "币制",
    "origin_country": "原产国",
    "destination_country": "最终目的国",
    "elements": "申报要素",
}

# Fields compared during the read-back verification (source vs system).
COMPARE_FIELDS = [
    "trade_mode", "declaration_type", "port", "consignor", "consignee",
    "destination_country", "currency",
]

ITEM_COMPARE_FIELDS = [
    "hs_code", "goods_name", "spec_model", "quantity", "unit", "unit_price",
    "total_price", "origin_country",
]
