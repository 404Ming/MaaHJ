"""商店可选商品表：设置界面（interface.json）与运行时（ShopBuySelected）共用的单一事实来源。

字段说明：

- ``node``  : 载体节点名（定义在 ``resource/base/pipeline/Shop/ShopItems.json``）。
              checkbox 的 case 只把对应节点置 ``enabled: true``，运行时用
              ``context.get_node_data`` 读回勾选集合（实测能读到 override 后的值）。
- ``group`` : 归属商店分组，决定这件商品在哪个列表页被查找
              （``gift`` 折扣礼包 / ``badge`` 征伐印记商店 / ``guild`` 公会商店）。
- ``label`` : 设置界面显示的商品全名。
- ``match`` : 卡片匹配规则，元素是 ``(名称前缀, 价格 或 None)``。
              名称前缀已去掉尾部省略号 —— 游戏会把过长的商品名截断成 ``XXX...``，
              而 OCR 对省略号的识别不稳定（同一件商品时而被读成 1 个点、时而 2 个点），
              所以这里一律做前缀匹配，不用完整串精确匹配。

价格只在同名多 SKU 时用于消歧（例如公会商店有 星间通信书 700 与 800）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ShopItem:
    """一件可勾选购买的商品。"""

    node: str
    group: str
    label: str
    match: tuple[tuple[str, str | None], ...]


# 折扣礼包的商品名随活动轮换（2026-10-16 实机已轮换为 冒险祈愿钜惠礼包/限定祈愿钜惠礼包/
# 宝石提升礼包/觉醒提升礼包 与 武器突破礼包/限定祈愿助力礼包/刻印属性重铸礼包/武器六星突破礼包），
# 下面三个是按用户指定保留的固定清单：当前不在列表里时会静默跳过，不会误买别的东西。
_GIFT: tuple[ShopItem, ...] = (
    ShopItem("ShopPick_Gift_DailyWelfare", "gift", "每日福利", (("每日福利", None),)),
    ShopItem("ShopPick_Gift_AdventureWishValue", "gift", "冒险祈愿超值礼包", (("冒险祈愿超值礼包", None),)),
    ShopItem("ShopPick_Gift_WeeklyWelfare", "gift", "每周福利", (("每周福利", None),)),
)

# 公会商店「可兑换」里带每日限购的商品（2026-10-16 实机核对；星间通信书 700 当日已买满不在列表）。
_GUILD: tuple[ShopItem, ...] = (
    # 公会商店的星间通信书实测有 700 / 800 / 1000 三个不同价格的 SKU，但**购买时不需要区分价格**
    # （用户明确要求「不同价格的星间通信书皆视为一种商品」）。所以 match 的价格设为 None：
    # 同名的卡片都会命中，而每张卡片仍按它自己的「每日:N/M / 每周:N/M」买到各自的上限。
    ShopItem("ShopPick_Guild_StarBook", "guild", "星间通信书", (("星间通信书", None),)),
    # label 是给用户看的显示名，不带价格；价格只作为 match 的消歧条件（None = 不校验）。
    ShopItem("ShopPick_Guild_AbyssEye", "guild", "深渊之瞳", (("深渊之瞳", None),)),
    ShopItem("ShopPick_Guild_AbyssClaw", "guild", "深渊之爪", (("深渊之爪", None),)),
    ShopItem("ShopPick_Guild_ValkyrieFeather", "guild", "女武神之羽", (("女武神之羽", None),)),
    ShopItem("ShopPick_Guild_StarSpiritElixir", "guild", "星灵秘药", (("星灵秘药", None),)),
    ShopItem("ShopPick_Guild_DivineElixir", "guild", "神灵秘药", (("神灵秘药", None),)),
)

# 征伐印记商店「可兑换」里带限购的商品（2026-10-16 逐屏实机枚举，79 件）。
_BADGE: tuple[ShopItem, ...] = (
    ShopItem("ShopPick_Badge_001", "badge", "始源巨兽刻印金宝箱", (("始源巨兽刻印金", "2000"),)),
    ShopItem("ShopPick_Badge_002", "badge", "神祝之翼刻印金宝箱", (("神祝之翼刻印金", "2000"),)),
    ShopItem("ShopPick_Badge_003", "badge", "烛之造物刻印金箱", (("烛之造物刻印金", "2000"),)),
    # 真名是「一串香蕉」（模拟器点开物品信息弹窗核对过：标题为「一串香蕉」），
    # 但列表卡片上只显示「串香蕉」（卡片文本宽度只够 3 个字）。所以两种写法都参与匹配，
    # 显示名用真名。
    ShopItem(
        "ShopPick_Badge_004",
        "badge",
        "一串香蕉",
        (("一串香蕉", "2000"), ("串香蕉", "2000")),
    ),
    ShopItem("ShopPick_Badge_005", "badge", "熊牙短匕", (("熊牙短匕", "2000"),)),
    ShopItem("ShopPick_Badge_006", "badge", "缠绕重锤", (("缠绕重锤", "2000"),)),
    ShopItem("ShopPick_Badge_007", "badge", "野猪王之剑", (("野猪王之剑", "2000"),)),
    ShopItem("ShopPick_Badge_008", "badge", "深邃大洋之光", (("深邃大洋之光", "4000"),)),
    ShopItem("ShopPick_Badge_009", "badge", "深邃大洋之弦", (("深邃大洋之弦", "4000"),)),
    ShopItem("ShopPick_Badge_010", "badge", "深邃大洋之芒", (("深邃大洋之芒", "4000"),)),
    ShopItem("ShopPick_Badge_011", "badge", "祝福生命之棱", (("祝福生命之棱", "4000"),)),
    ShopItem("ShopPick_Badge_012", "badge", "祝福生命之种", (("祝福生命之种", "4000"),)),
    ShopItem("ShopPick_Badge_013", "badge", "祝福生命之芽", (("祝福生命之芽", "4000"),)),
    ShopItem("ShopPick_Badge_014", "badge", "远古熔岩之握", (("远古熔岩之握", "4000"),)),
    ShopItem("ShopPick_Badge_015", "badge", "远古熔岩之牙", (("远古熔岩之牙", "4000"),)),
    ShopItem("ShopPick_Badge_016", "badge", "远古熔岩之触", (("远古熔岩之触", "4000"),)),
    ShopItem("ShopPick_Badge_017", "badge", "长空祈愿之力", (("长空祈愿之力", "4000"),)),
    ShopItem("ShopPick_Badge_018", "badge", "长空祈愿之终", (("长空祈愿之终", "4000"),)),
    ShopItem("ShopPick_Badge_019", "badge", "长空祈愿之视", (("长空祈愿之视", "4000"),)),
    # 征伐印记商店的星间通信书价格会随轮换变（枚举到的是 2000），与公会商店一致：
    # **购买时不区分价格**，同名的卡片都算这一件，各自买到自己的限购上限。
    ShopItem("ShopPick_Badge_020", "badge", "星间通信书", (("星间通信书", None),)),
    ShopItem("ShopPick_Badge_021", "badge", "天使之羽", (("天使之羽", "500"),)),
    ShopItem("ShopPick_Badge_022", "badge", "虚界之镰", (("虚界之镰", "500"),)),
    ShopItem("ShopPick_Badge_023", "badge", "幻影之眼", (("幻影之眼", "700"),)),
    ShopItem("ShopPick_Badge_024", "badge", "伊南娜的心核", (("伊南娜的心核", "240"),)),
    ShopItem("ShopPick_Badge_025", "badge", "伊南娜的神魄", (("伊南娜的神魄", "240"),)),
    ShopItem("ShopPick_Badge_026", "badge", "俄耳浦斯的神魄", (("俄耳浦斯的神魄", "240"),)),
    ShopItem("ShopPick_Badge_027", "badge", "俄里翁的心核", (("俄里翁的心核", "240"),)),
    ShopItem("ShopPick_Badge_028", "badge", "俄里翁的神魄", (("俄里翁的神魄", "240"),)),
    ShopItem("ShopPick_Badge_029", "badge", "卡律布狄斯的心核", (("卡律布狄斯的心", "240"),)),
    ShopItem("ShopPick_Badge_030", "badge", "卡律布狄斯的神魄", (("卡律布狄斯的神", "240"),)),
    ShopItem("ShopPick_Badge_031", "badge", "和音傀的心核", (("和音傀的心核", "240"),)),
    ShopItem("ShopPick_Badge_032", "badge", "和音傀的神魄", (("和音傀的神魄", "240"),)),
    ShopItem("ShopPick_Badge_033", "badge", "孕星烛母的神魄", (("孕星烛母的神魄", "240"),)),
    ShopItem("ShopPick_Badge_034", "badge", "断裁偶的心核", (("断裁偶的心核", "240"),)),
    ShopItem("ShopPick_Badge_035", "badge", "断裁偶的神魄", (("断裁偶的神魄", "240"),)),
    ShopItem("ShopPick_Badge_036", "badge", "斯文托维特的神魄", (("斯文托维特的神", "240"),)),
    ShopItem("ShopPick_Badge_037", "badge", "柏勒洛丰的心核", (("柏勒洛丰的心核", "240"),)),
    ShopItem("ShopPick_Badge_038", "badge", "柏勒洛丰的神魄", (("柏勒洛丰的神魄", "240"),)),
    ShopItem("ShopPick_Badge_039", "badge", "欧若拉的神魄", (("欧若拉的神魄", "240"),)),
    ShopItem("ShopPick_Badge_040", "badge", "欲望泉的心核", (("欲望泉的心核", "240"),)),
    ShopItem("ShopPick_Badge_041", "badge", "欲望泉的神魄", (("欲望泉的神魄", "240"),)),
    ShopItem("ShopPick_Badge_042", "badge", "法厄同的心核", (("法厄同的心核", "240"),)),
    ShopItem("ShopPick_Badge_043", "badge", "法厄同的神魄", (("法厄同的神魄", "240"),)),
    ShopItem("ShopPick_Badge_044", "badge", "潘塔菲卡的心核", (("潘塔菲卡的心核", "240"),)),
    ShopItem("ShopPick_Badge_045", "badge", "潘塔菲卡的神魄", (("潘塔菲卡的神魄", "240"),)),
    ShopItem("ShopPick_Badge_046", "badge", "焚尽花的心核", (("焚尽花的心核", "240"),)),
    ShopItem("ShopPick_Badge_047", "badge", "焚尽花的神魄", (("焚尽花的神魄", "240"),)),
    ShopItem("ShopPick_Badge_048", "badge", "珀尔修斯的心核", (("珀尔修斯的心核", "240"),)),
    ShopItem("ShopPick_Badge_049", "badge", "珀尔修斯的神魄", (("珀尔修斯的神魄", "240"),)),
    ShopItem("ShopPick_Badge_050", "badge", "米劳罗的心核", (("米劳罗的心核", "240"),)),
    ShopItem("ShopPick_Badge_051", "badge", "米劳罗的神魄", (("米劳罗的神魄", "240"),)),
    ShopItem("ShopPick_Badge_052", "badge", "苏尔特尔的心核", (("苏尔特尔的心核", "240"),)),
    ShopItem("ShopPick_Badge_053", "badge", "苏尔特尔的神魄", (("苏尔特尔的神魄", "240"),)),
    ShopItem("ShopPick_Badge_054", "badge", "赫拉斯瓦尔格的神魄", (("赫拉斯瓦尔格的", "240"),)),
    ShopItem("ShopPick_Badge_055", "badge", "迪亚波罗的神魄", (("迪亚波罗的神魄", "240"),)),
    ShopItem("ShopPick_Badge_056", "badge", "阿格莱亚的神魄", (("阿格莱亚的神魄", "240"),)),
    ShopItem("ShopPick_Badge_057", "badge", "伊南娜的残魂", (("伊南娜的残魂", "120"),)),
    ShopItem("ShopPick_Badge_058", "badge", "俄耳浦斯的残魂", (("俄耳浦斯的残魂", "120"),)),
    ShopItem("ShopPick_Badge_059", "badge", "俄里翁的残魂", (("俄里翁的残魂", "120"),)),
    ShopItem("ShopPick_Badge_060", "badge", "卡律布狄斯的残魂", (("卡律布狄斯的残", "120"),)),
    ShopItem("ShopPick_Badge_061", "badge", "和音傀的残魂", (("和音傀的残魂", "120"),)),
    ShopItem("ShopPick_Badge_062", "badge", "孕星烛母的残魂", (("孕星烛母的残魂", "120"),)),
    ShopItem("ShopPick_Badge_063", "badge", "断裁偶的残魂", (("断裁偶的残魂", "120"),)),
    ShopItem("ShopPick_Badge_064", "badge", "斯文托维特的残魂", (("斯文托维特的残", "120"),)),
    ShopItem("ShopPick_Badge_065", "badge", "柏勒洛丰的残魂", (("柏勒洛丰的残魂", "120"),)),
    ShopItem("ShopPick_Badge_066", "badge", "欧若拉的残魂", (("欧若拉的残魂", "120"),)),
    ShopItem("ShopPick_Badge_067", "badge", "欲望泉的残魂", (("欲望泉的残魂", "120"),)),
    ShopItem("ShopPick_Badge_068", "badge", "法厄同的残魂", (("法厄同的残魂", "120"),)),
    ShopItem("ShopPick_Badge_069", "badge", "潘塔菲卡的残魂", (("潘塔菲卡的残魂", "120"),)),
    ShopItem("ShopPick_Badge_070", "badge", "焚尽花的残魂", (("焚尽花的残魂", "120"),)),
    ShopItem("ShopPick_Badge_071", "badge", "珀尔修斯的残魂", (("珀尔修斯的残魂", "120"),)),
    ShopItem("ShopPick_Badge_072", "badge", "米劳罗的残魂", (("米劳罗的残魂", "120"),)),
    ShopItem("ShopPick_Badge_073", "badge", "苏尔特尔的残魂", (("苏尔特尔的残魂", "120"),)),
    ShopItem("ShopPick_Badge_074", "badge", "赫拉斯瓦尔格的残魂", (("赫拉斯瓦尔格的", "120"),)),
    ShopItem("ShopPick_Badge_075", "badge", "迪亚波罗的残魂", (("迪亚波罗的残魂", "120"),)),
    ShopItem("ShopPick_Badge_076", "badge", "阿格莱亚的残魂", (("阿格莱亚的残魂", "120"),)),
    ShopItem("ShopPick_Badge_077", "badge", "初级讨伐证明", (("初级讨伐证明", "40"),)),
    ShopItem("ShopPick_Badge_078", "badge", "高级讨伐证明", (("高级讨伐证明", "120"),)),
    ShopItem("ShopPick_Badge_079", "badge", "传奇讨伐证明", (("传奇讨伐证明", "360"),)),
)

ITEMS: tuple[ShopItem, ...] = _GIFT + _GUILD + _BADGE


def items_of(group: str) -> tuple[ShopItem, ...]:
    """返回某个商店分组的全部可选商品。"""
    return tuple(item for item in ITEMS if item.group == group)
