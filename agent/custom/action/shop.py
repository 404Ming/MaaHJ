"""商店购买：按用户在任务设置里勾选的商品清单购买。

Pipeline（``resource/base/pipeline/Shop/Shop.json``）负责导航与监督，本模块只负责
「当前商店列表页」这一件事：把勾选的商品买到当日/当周限购上限。

两种页面布局：

- ``grid``：征伐印记商店 / 公会商店的「可兑换」商品网格。每张卡片自上而下是
  名称 → 图标 → 价格条 → 限购行（``每日:0/3`` / ``每周:0/4``），售罄时图标上会盖一条
  「售罄」。点击落点取价格条中心 —— 实测点价格条才会弹「购买确认」，点名称/图标只会弹
  「物品详情」。
- ``gift``：折扣礼包页的礼包卡。每张卡片是 名称 → 图标 → ``每日:0/1`` → 礼包包含 →
  价格按钮（``100000`` / ``免费``），售罄时限购行被「售罄」盖住。

勾选来源：每件可选商品在 ``ShopItems.json`` 里有一个载体节点 ``ShopPick_*``，
interface.json 的 checkbox case 只把对应节点的 ``enabled`` 置 true；本模块用
``context.get_node_data`` 读回勾选集合（实测能读到 override 之后的值）。

**列表会重排**：买满当日限购的商品会直接从「可兑换」消失，售罄的商品会沉到列表最下面，
所以每次购买成功后都重新截图、重新解析，绝不按固定行列定位。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
from utils.maa_types import ocr_results
from utils.params import parse_params

from .shop_items import ShopItem, items_of

# 列表区默认 ROI：[x, y, w, h]（720x1280 竖屏，取卡片带 + 限购行）。
GRID_ROI = (110, 460, 610, 800)
GIFT_ROI = (140, 455, 580, 800)
# 整屏 ROI，用于弹窗定位。
FULL_ROI = (0, 0, 720, 1280)

# 每个分组用哪种布局 / 默认 ROI。
GROUP_LAYOUT = {"gift": "gift", "badge": "grid", "guild": "grid"}
GROUP_ROI = {"gift": GIFT_ROI, "badge": GRID_ROI, "guild": GRID_ROI}

# 限购行：每日:0/3、每周：0/10（游戏里冒号有全角半角两种）。
LIMIT_PATTERN = re.compile(r"每[日周]\s*[:：]\s*(\d+)\s*[/／]\s*(\d+)")
# 价格按钮/价格条上的纯数字。
PRICE_PATTERN = re.compile(r"^\d+$")
SOLD_OUT_TEXT = "售罄"
FREE_TEXT = "免费"

# 「购买确认」弹窗（实测：标题 (303,265,109,33)，取消 (176,793,76,45)，价格按钮 (450,796,125,38)）。
CONFIRM_TITLE = "购买确认"
CANCEL_TEXT = "取消"
# 礼包详情弹窗：点礼包卡的价格按钮不会弹网格商店那种「购买确认」，而是这个 ——
# 标题是礼包名、正文说明、中间「商品内容」、底部一个按钮。按钮文案按商品类型分两种：
#   「领取」= 免费礼包（每日福利，实测 box (311,834,96,56)）；
#   「购买」= 付费礼包（冒险祈愿超值礼包 100000，实测 box (315,840,86,46)、价格 (341,794,106,32)）。
# 两种都既没有「取消」也没有数量滑条，点弹窗外的空白/返回键才会关。
# 识别不到它们时 _buy_card 会当成「弹窗异常」跳过：礼包买不到，而且弹窗一直盖在商店页上，
# 后面的「每周礼包」标签页会永远识别不到（实测卡死 22 次识别）。
CLAIM_TEXT = "领取"
BUY_DIALOG_TEXT = "购买"
CLAIM_CONTENT_TEXT = "商品内容"
DIALOG_FALLBACK_POINT = (358, 862)
DETAIL_TITLE = "物品详情"
DETAIL_CLOSE_POINT = (617, 437)
# 弹窗里有一根**数量滑条**：手柄拖到最右就是一次买满剩余数量，不必一件一件点。
# 实测：数量 1 时手柄在最左（约 (178,660)），从它往右拖到轨道右端后数量变成剩余上限（0/3 → 3）。
# 轨道实测约 x187→470、y≈658，落点取到轨道右端之外以确保拉满。
CONFIRM_SLIDER_FROM = (178, 660)
CONFIRM_SLIDER_TO = (520, 660)
CONFIRM_SLIDER_DURATION = 600
# 认不出价格按钮时的兜底落点：弹窗右侧按钮中心（720x1280 固定布局）。
BUY_FALLBACK_POINT = (520, 760)
# 「恭喜获得」提示的关闭点（与公共 Exception_RewardClose 同一落点）。
CLOSE_HINT = "点击空白区域关闭"
CLOSE_POPUP_POINT = (370, 1215)
# 购买失败的常见提示词。
FAILURE_HINTS = ("不足", "失败", "无法购买")

# 内容区翻页手势：手指上滑 = 列表往下翻。
SCROLL_DOWN_FROM = (360, 1030)
SCROLL_DOWN_TO = (360, 570)
# 内容区回顶手势：手指下滑。实测 (360,570)->(360,1080) 不生效，(360,700)->(360,1150) 生效。
SCROLL_UP_FROM = (360, 700)
SCROLL_UP_TO = (360, 1150)
SCROLL_DURATION = 600

# CustomAction 内做整页 OCR 用的探针节点（定义见 Shop.json）。
OCR_NODE = "Shop_OcrProbe"

# 网格卡片内各行的相对位置（以限购行为基准，实测稳定）。
_PRICE_OFFSET = (110, 10)  # 价格条在限购行上方 10~110px
_NAME_OFFSET = (280, 190)  # 名称在限购行上方 190~280px（再往上会撞到「公会商店」这类分区标题）
_SOLD_OFFSET = (230, 30)  # 售罄盖在限购行上方 30~230px
# 出现在商品名附近的非名称文案。列表顶部的页签（全部/可兑换）与页内标题会落进名称偏移窗口，
# 必须排除，否则顶部那一行会被自己的页签文案顶替掉、导致该商品永远匹配不上。
# 这里是整行文本**精确**比较，不会误伤名字里含这些子串的商品。
_NAME_EXCLUDE = ("NEW", "礼包包含", "全部", "可兑换", "商店", "征伐印记商店", "公会商店", "折扣礼包")
_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]")  # 名称一定含中文
# 礼包卡内各行的相对位置（以限购行为锚，与 grid 用同一种思路定位）。
_GIFT_NAME_ABOVE = (200, 120)  # 名称在限购行上方 120~200px（实测 159）
_GIFT_PRICE_BELOW = (90, 200)  # 价格按钮在限购行下方 90~200px（实测 107~175）
_GIFT_SOLD_AROUND = (30, 30)  # 售罄标记直接盖在限购行上
_COLUMN_TOLERANCE = 90

# 每次扫描找不到目标就翻一页，翻页次数上限（列表约 24 行，正常 12 页内到底）。
_MAX_SCROLLS = 16


@dataclass(frozen=True)
class _Line:
    """一次 OCR 出来的一行文字。"""

    text: str
    x: int
    y: int
    w: int
    h: int
    score: float

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2


@dataclass(frozen=True)
class _Card:
    """一张可买的卡片。"""

    key: str
    name: str
    label: str
    tap: tuple[int, int]
    price: str
    current: int
    maximum: int
    nameless: bool = False

    @property
    def remaining(self) -> int:
        """还差多少件才买到限购上限。"""
        return max(0, self.maximum - self.current)


def _rect(box: Any) -> tuple[int, int, int, int]:
    """把 MaaFramework 的识别框统一成 (x, y, w, h)。"""
    if box is None:
        return (0, 0, 0, 0)
    if isinstance(box, (list, tuple)):
        return (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
    return (int(box.x), int(box.y), int(box.w), int(box.h))


def _screencap(context: Context) -> Any:
    return context.tasker.controller.post_screencap().wait().get()


def _ocr(context: Context, image: Any, roi: tuple[int, int, int, int]) -> list[_Line]:
    """用 Shop_OcrProbe 节点对指定 ROI 做一次 OCR。"""
    detail = context.run_recognition(
        OCR_NODE,
        image,
        {OCR_NODE: {"recognition": {"type": "OCR", "param": {"roi": list(roi)}}}},
    )
    lines: list[_Line] = []
    for result in ocr_results(detail):
        x, y, w, h = _rect(result.box)
        text = str(getattr(result, "text", "")).strip()
        if not text:
            continue
        lines.append(_Line(text=text, x=x, y=y, w=w, h=h, score=float(getattr(result, "score", 0.0))))
    return lines


def _click(context: Context, x: int, y: int) -> None:
    context.tasker.controller.post_click(int(x), int(y)).wait()


def _swipe(context: Context, begin: tuple[int, int], end: tuple[int, int], duration: int) -> None:
    context.tasker.controller.post_swipe(begin[0], begin[1], end[0], end[1], duration).wait()


def _in_column(line: _Line, cx: int, tolerance: int = _COLUMN_TOLERANCE) -> bool:
    return abs(line.cx - cx) <= tolerance


def _pick(
    lines: list[_Line],
    cx: int,
    lower: int,
    upper: int,
    predicate: Any,
) -> _Line | None:
    """在 (cx, lower..upper) 这一块区域里挑一行，多行时取 y 最小的那行。"""
    hits = [line for line in lines if _in_column(line, cx) and lower <= line.y <= upper and predicate(line)]
    if not hits:
        return None
    hits.sort(key=lambda line: line.y)
    return hits[0]


def _is_price(line: _Line) -> bool:
    # 价格数字识别分数很高且够宽；图标上的数量角标是 10px 级的小字，会被这里挡掉。
    return bool(PRICE_PATTERN.match(line.text)) and line.score >= 0.85 and line.w >= 25


def _grid_cards(lines: list[_Line]) -> list[_Card]:
    """grid 布局：产出所有「有限购行」的卡片（含已买满与带名称截断的）。"""
    sold = [line for line in lines if SOLD_OUT_TEXT in line.text]
    limits: list[tuple[_Line, int, int]] = []
    prices: list[_Line] = []
    for line in lines:
        if SOLD_OUT_TEXT in line.text:
            continue
        match = LIMIT_PATTERN.search(line.text)
        if match:
            limits.append((line, int(match.group(1)), int(match.group(2))))
            continue
        if _is_price(line):
            prices.append(line)

    cards: list[_Card] = []
    for limit_line, current, maximum in limits:
        low, high = _SOLD_OFFSET
        if _pick(sold, limit_line.cx, limit_line.y - low, limit_line.y - high, lambda _line: True):
            # 盖了「售罄」的卡片不再出现在候选里（限购行本身也读不到了，这里只是兜底）。
            continue
        low, high = _PRICE_OFFSET
        price = _pick(prices, limit_line.cx, limit_line.y - low, limit_line.y - high, lambda _line: True)
        low, high = _NAME_OFFSET
        name = _pick(
            lines,
            limit_line.cx,
            limit_line.y - low,
            limit_line.y - high,
            lambda line: (
                LIMIT_PATTERN.search(line.text) is None
                and line.text not in _NAME_EXCLUDE
                and bool(_CJK_PATTERN.search(line.text))
            ),
        )
        label_price = price.text if price else ""
        # 列表最顶部那一行会被分区标题挡住、OCR 读不到名称：这种卡片标成 nameless，
        # 不能按名称匹配（勾选清单不认它），只能靠继续翻页让它露出名称。
        nameless = name is None
        label_name = name.text if name else "?"
        key = f"{label_name if not nameless else f'@{limit_line.cx}'}|{label_price}"
        tap = (price.cx, price.cy) if price else (limit_line.cx, limit_line.y - 55)
        cards.append(
            _Card(
                key=key,
                name=label_name if not nameless else "",
                label=f"{label_name} {label_price}".strip(),
                tap=tap,
                price=label_price,
                current=current,
                maximum=maximum,
                nameless=nameless,
            )
        )
    cards.sort(key=lambda card: card.tap[1])
    return cards


def _gift_cards(lines: list[_Line]) -> list[_Card]:
    """gift 布局：产出所有「带限购行」的礼包卡。

    以**限购行**为锚再往上下找名称与价格按钮，而不是拿每一行 OCR 反查限购行 ——
    礼包图标上会有一批识别质量很低的装饰文字（``0Ce`` / ``网`` / ``ofe`` 之类），
    用它们当候选名称会造出大量假卡片。以限购行为锚后，一张卡片只会被产出一条。
    名称找不到（例如被横幅盖住）的卡片直接丢弃：勾选清单按名称匹配，认不出名称就无从匹配。
    """
    sold = [line for line in lines if SOLD_OUT_TEXT in line.text]
    limits: list[tuple[_Line, int, int]] = []
    for line in lines:
        if SOLD_OUT_TEXT in line.text:
            continue
        match = LIMIT_PATTERN.search(line.text)
        if match:
            limits.append((line, int(match.group(1)), int(match.group(2))))

    cards: list[_Card] = []
    for limit_line, current, maximum in limits:
        low, high = _GIFT_SOLD_AROUND
        if _pick(sold, limit_line.cx, limit_line.y - low, limit_line.y + high, lambda _line: True):
            continue
        low, high = _GIFT_NAME_ABOVE
        name = _pick(
            lines,
            limit_line.cx,
            limit_line.y - low,
            limit_line.y - high,
            lambda line: (
                LIMIT_PATTERN.search(line.text) is None
                and line.text not in _NAME_EXCLUDE
                and bool(_CJK_PATTERN.search(line.text))
            ),
        )
        if name is None:
            continue
        low, high = _GIFT_PRICE_BELOW
        button = _pick(
            lines,
            limit_line.cx,
            limit_line.y + low,
            limit_line.y + high,
            lambda item: _is_price(item) or item.text == FREE_TEXT,
        )
        price = button.text if button else ""
        tap = (button.cx, button.cy) if button else (limit_line.cx, limit_line.y + 170)
        cards.append(
            _Card(
                key=f"{name.text}|{price}",
                name=name.text,
                label=f"{name.text} {price}".strip(),
                tap=tap,
                price=price,
                current=current,
                maximum=maximum,
            )
        )
    cards.sort(key=lambda card: card.tap[1])
    return cards


def _cards_for(layout: str, lines: list[_Line]) -> list[_Card]:
    return _gift_cards(lines) if layout == "gift" else _grid_cards(lines)


def _normalize(text: str) -> str:
    """去掉尾部省略号与空白，用于前缀匹配（OCR 对省略号个数不稳定）。"""
    return text.rstrip(".．。…·、,， \t")


def _card_matches(card: _Card, item: ShopItem) -> bool:
    """卡片是否就是这件商品：名称前缀命中，且（若指定了价格）价格一致。"""
    if card.nameless or not card.name:
        return False
    name = _normalize(card.name)
    for prefix, price in item.match:
        wanted = _normalize(prefix)
        if not wanted:
            continue
        if not name.startswith(wanted):
            continue
        if price is not None and card.price != price:
            continue
        return True
    return False


def _dialog_action_point(lines: list[_Line], text: str) -> tuple[int, int]:
    """礼包详情弹窗底部按钮（「领取」/「购买」）的落点。"""
    for line in lines:
        if line.text == text:
            return (line.cx, line.cy)
    return DIALOG_FALLBACK_POINT


def _dialog_button(lines: list[_Line]) -> str:
    """礼包详情弹窗底部的按钮文案，不是礼包详情弹窗时返回空串。

    礼包详情弹窗的固定特征是正文里的「商品内容」；只看「领取」/「购买」会误伤其它页面
    （日常任务页也有「领取」，商店页本身也可能出现「购买」字样）。
    """
    if not any(CLAIM_CONTENT_TEXT in line.text for line in lines):
        return ""
    for text in (CLAIM_TEXT, BUY_DIALOG_TEXT):
        if any(line.text == text for line in lines):
            return text
    return ""


def _dialog_state(context: Context, tries: int = 5, delay: float = 0.5) -> tuple[str, list[_Line]]:
    """点完卡片后判定弹出的是哪个弹窗：confirm / claim / purchase / detail / none。"""
    lines: list[_Line] = []
    for _ in range(tries):
        time.sleep(delay)
        lines = _ocr(context, _screencap(context), FULL_ROI)
        texts = [line.text for line in lines]
        if any(CONFIRM_TITLE in text for text in texts):
            return "confirm", lines
        button = _dialog_button(lines)
        if button == CLAIM_TEXT:
            return "claim", lines
        if button == BUY_DIALOG_TEXT:
            return "purchase", lines
        if any(DETAIL_TITLE in text for text in texts):
            return "detail", lines
    return "none", lines


def _buy_button_point(lines: list[_Line]) -> tuple[int, int]:
    """对话框右下角的购买按钮：按钮上的价格/「免费」文字就是落点。"""
    candidates = [
        line
        for line in lines
        if 690 <= line.cy <= 830
        and line.text != CANCEL_TEXT
        and (_is_price(line) or line.text == FREE_TEXT or line.text == "购买")
    ]
    if not candidates:
        return BUY_FALLBACK_POINT
    candidates.sort(key=lambda line: line.cy)
    return (candidates[0].cx, candidates[0].cy)


def _cancel_button_point(lines: list[_Line]) -> tuple[int, int] | None:
    for line in lines:
        if line.text == CANCEL_TEXT:
            return (line.cx, line.cy)
    return None


def _wait_purchase_result(context: Context, tries: int = 6, delay: float = 0.6) -> bool:
    """购买后等结果：出现奖励提示=成功；对话框仍在=没买成（余额不足等）。"""
    lines: list[_Line] = []
    for _ in range(tries):
        time.sleep(delay)
        lines = _ocr(context, _screencap(context), FULL_ROI)
        texts = [line.text for line in lines]
        if any(CLOSE_HINT in text for text in texts):
            _click(context, *CLOSE_POPUP_POINT)
            time.sleep(0.8)
            return True
        if any(hint in text for hint in FAILURE_HINTS for text in texts):
            break
        if not any(CONFIRM_TITLE in text for text in texts):
            # 对话框已经消失又没有失败提示：视为购买完成（部分商品不弹奖励提示）。
            return True
    cancel = _cancel_button_point(lines)
    if cancel is not None:
        _click(context, *cancel)
        time.sleep(0.8)
    return False


# 弹窗盖屏时按左下角返回可以关掉它（实测礼包详情弹窗、领取弹窗都吃这一下）。
# 页面上没有弹窗时**绝不能点**：商店页这一下会直接退出商店。
BACK_POINT = (73, 1240)


def _dismiss_stray_dialog(context: Context) -> None:
    """收尾兜底：买完发现还留着弹窗就按返回关掉。

    礼包详情弹窗一旦没被点掉就会一直盖在页面上，后面的「每周礼包」标签页再也识别不到
    （实测卡了 22 次识别）。这里只在**正面识别到弹窗**时才动手，正常页面不会误触。
    """
    lines = _ocr(context, _screencap(context), FULL_ROI)
    texts = [line.text for line in lines]
    visible = bool(_dialog_button(lines)) or any(CONFIRM_TITLE in text for text in texts) or any(
        DETAIL_TITLE in text for text in texts
    )
    if not visible:
        return
    logger.warning("商店购买：页面上还留着弹窗，按左下角返回关闭后再结束本页。")
    _click(context, *BACK_POINT)
    time.sleep(1.0)


def _buy_card(context: Context, tap: tuple[int, int]) -> str:
    """买/领一张卡片，返回成功方式：``"confirm"`` / ``"purchase"`` / ``"claim"`` / ``""``（失败）。

    - ``confirm``：网格商店的「购买确认」弹窗，把数量滑条拉满，一次买完剩余数量；
    - ``purchase``：礼包页的礼包详情弹窗，底部「购买」一点即扣费（没有滑条）；
    - ``claim``：礼包页的免费礼包，弹窗底部是「领取」。
    """
    _click(context, *tap)
    state, lines = _dialog_state(context)
    if state == "detail":
        # 点偏了点到了卡片本体：关掉详情再交给调用方重试。
        _click(context, *DETAIL_CLOSE_POINT)
        time.sleep(0.8)
        return ""
    if state == "claim":
        # 免费礼包：弹窗只有「领取」，没有数量滑条，也没有「取消」。
        # 这个弹窗不点掉就一直盖在页面上，后面的「每周礼包」标签会永远识别不到。
        _click(context, *_dialog_action_point(lines, CLAIM_TEXT))
        return "claim" if _wait_purchase_result(context) else ""
    if state == "purchase":
        # 付费礼包：礼包详情弹窗本身就是确认页（底部「购买」，没有滑条也没有取消），
        # 点下去才真正扣费；不点它就等于放弃购买。
        _click(context, *_dialog_action_point(lines, BUY_DIALOG_TEXT))
        return "purchase" if _wait_purchase_result(context) else ""
    if state != "confirm":
        return ""

    _swipe(context, CONFIRM_SLIDER_FROM, CONFIRM_SLIDER_TO, CONFIRM_SLIDER_DURATION)
    time.sleep(0.4)
    # 拉满后重新读一次弹窗：总价变了，按钮宽度也会变，落点要按新的一屏来定位。
    fresh = _ocr(context, _screencap(context), FULL_ROI)
    if not any(CONFIRM_TITLE in line.text for line in fresh):
        logger.warning("商店购买：拉数量滑条后购买确认弹窗消失了，跳过这一张。")
        return ""

    _click(context, *_buy_button_point(fresh))
    return "confirm" if _wait_purchase_result(context) else ""


def _is_enabled(context: Context, node: str) -> bool:
    """读回载体节点的勾选状态（override 之后的值）。"""
    data = context.get_node_data(node)
    if not isinstance(data, dict):
        logger.warning(f"商店购买：载体节点 {node} 不存在，按未勾选处理。")
        return False
    return bool(data.get("enabled", False))


def _selected_items(context: Context, group: str, only: set[str] | None = None) -> list[ShopItem]:
    """返回该分组里被勾选的商品。

    ``only`` 给定时只在这些载体节点里筛（Pipeline 想收窄范围时才传），否则用商品表里该分组的全部。
    """
    return [
        item
        for item in items_of(group)
        if (not only or item.node in only) and _is_enabled(context, item.node)
    ]


@AgentServer.custom_action("ShopSelectionGate")
class ShopSelectionGate(CustomAction):
    """整段前置闸门：该分组一件都没勾选时返回失败，由 Pipeline 的 ``on_error`` 跳过整段。

    没有这道闸门时，即使「折扣礼包商品」一件都没勾选，任务照样会点侧栏的「折扣礼包」、
    进「每日礼包 / 每周礼包」标签页白跑一趟；有了它就直接跳到该页签对应的下一段。

    Examples:
        `custom_action_param`::

            { "group": "gift" }
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            params = parse_params(argv.custom_action_param, "group")
        except ValueError as error:
            logger.error(f"ShopSelectionGate: {error}")
            return CustomAction.RunResult(success=False)

        group = str(params["group"])
        if group not in GROUP_LAYOUT:
            logger.error(f"ShopSelectionGate: group 只能是 {sorted(GROUP_LAYOUT)}，收到 {group!r}。")
            return CustomAction.RunResult(success=False)

        selected = _selected_items(context, group)
        if not selected:
            logger.info(f"商店购买：{group} 分组没有勾选任何商品，整段跳过（不进入该商店的页签）。")
            return CustomAction.RunResult(success=False)

        logger.info(f"商店购买：{group} 分组已勾选 " + "、".join(item.label for item in selected))
        return CustomAction.RunResult(success=True)


def _scroll_content_to_top(context: Context, times: int = 4) -> None:
    """把内容列表滚回顶部，避免上一次运行残留的滚动位置让上半部分商品扫不到。"""
    for _ in range(times):
        _swipe(context, SCROLL_UP_FROM, SCROLL_UP_TO, 800)
        time.sleep(0.6)


def _buy_item(
    context: Context,
    item: ShopItem,
    layout: str,
    roi: tuple[int, int, int, int],
    max_scrolls: int,
) -> int:
    """把一件商品买到限购上限，返回实际买到的数量。

    每次购买成功后都重新截图重扫：买满限购的商品会从列表消失、售罄的会沉到最下面，
    所以不能用固定的行列位置，也不能缓存上一次的解析结果。
    """
    bought = False
    handled: set[str] = set()
    seen_text: set[str] = set()
    scrolls = 0

    # 每件商品都从列表顶部开始扫：上一件（尤其是不存在、扫到底才放弃的那件）会把列表留在底部，
    # 不从顶部重来就会漏掉排在它前面的商品。
    _scroll_content_to_top(context)

    while True:
        lines = _ocr(context, _screencap(context), roi)
        cards = _cards_for(layout, lines)
        target = next(
            (card for card in cards if card.key not in handled and _card_matches(card, item)),
            None,
        )
        if target is None:
            signature = frozenset(line.text for line in lines)
            fresh = signature - seen_text
            seen_text |= signature
            # 三个停止条件：翻到底 / 画面没变 / 翻了一页却没有任何新文本。
            # 最后一条是为了短列表（例如折扣礼包页整页只有两行卡）：翻一页没有新内容就没必要再翻 16 页。
            if not fresh or scrolls >= max_scrolls:
                break
            scrolls += 1
            _swipe(context, SCROLL_DOWN_FROM, SCROLL_DOWN_TO, SCROLL_DURATION)
            time.sleep(0.9)
            continue

        # 同一张卡片只处理一次：购买确认里已经把数量滑条拉满，一次就买完该商品剩余的数量。
        handled.add(target.key)
        if target.remaining <= 0:
            continue

        mode = _buy_card(context, target.tap)
        if mode == "claim":
            bought = True
            logger.info(f"商店购买：已领取 {item.label}（免费礼包，领取弹窗已确认）。")
        elif mode == "purchase":
            bought = True
            logger.info(f"商店购买：已购买 {item.label}（礼包详情弹窗「购买」已确认）。")
        elif mode == "confirm":
            bought = True
            logger.info(f"商店购买：已购买 {item.label}（数量已在购买确认里拉满）。")
        else:
            logger.warning(f"商店购买：{item.label} 这一次没买成（售罄 / 余额不足 / 弹窗异常），跳过这一张。")
            scrolls += 1
            if scrolls > max_scrolls:
                break
            _swipe(context, SCROLL_DOWN_FROM, SCROLL_DOWN_TO, SCROLL_DURATION)
            time.sleep(0.9)

    return bought


@AgentServer.custom_action("ShopBuySelected")
class ShopBuySelected(CustomAction):
    """把当前商店列表页里「用户勾选的商品」买到限购上限。

    勾选为空时不做任何点击，直接成功返回（整段空转）。

    Examples:
        `custom_action_param`::

            {
                "group": "guild",
                "items": ["ShopPick_Guild_StarBook700", "ShopPick_Guild_AbyssEye"],
                "roi": [110, 460, 610, 800]
            }

        - ``group``: ``gift`` / ``badge`` / ``guild``，决定布局与默认 ROI。
        - ``items``: 本组全部载体节点名（不是只传勾选的，勾选状态从节点读回）。
        - ``roi``: 列表区 ROI，缺省按 group 取。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            params = parse_params(argv.custom_action_param, "group")
        except ValueError as error:
            logger.error(f"ShopBuySelected: {error}")
            return CustomAction.RunResult(success=False)

        group = str(params["group"])
        if group not in GROUP_LAYOUT:
            logger.error(f"ShopBuySelected: group 只能是 {sorted(GROUP_LAYOUT)}，收到 {group!r}。")
            return CustomAction.RunResult(success=False)

        # items 可省略：省略时用商品表里该分组的全部载体节点，避免在 Pipeline 里再抄一份清单。
        raw_roi = params.get("roi") or GROUP_ROI[group]
        if not isinstance(raw_roi, (list, tuple)) or len(raw_roi) != 4:
            logger.error(f"ShopBuySelected: roi 必须是 [x, y, w, h]，收到 {raw_roi!r}。")
            return CustomAction.RunResult(success=False)
        roi = (int(raw_roi[0]), int(raw_roi[1]), int(raw_roi[2]), int(raw_roi[3]))

        max_scrolls = int(params.get("max_scrolls") or _MAX_SCROLLS)
        if max_scrolls < 1:
            logger.error(f"ShopBuySelected: max_scrolls 必须 >= 1，收到 {max_scrolls}。")
            return CustomAction.RunResult(success=False)

        nodes = {str(node) for node in params.get("items") or []}
        known = {item.node for item in items_of(group)}
        for node in sorted(nodes - known):
            logger.warning(f"ShopBuySelected: 分组 {group} 里没有登记载体节点 {node}，忽略。")

        selected = _selected_items(context, group, nodes or None)
        if not selected:
            logger.info(f"商店购买：{group} 分组没有勾选任何商品，跳过本页（不做任何点击）。")
            return CustomAction.RunResult(success=True)

        logger.info(f"商店购买：{group} 分组本页要买 " + "、".join(item.label for item in selected))
        layout = GROUP_LAYOUT[group]

        bought_labels: list[str] = []
        missed_labels: list[str] = []
        for item in selected:
            # 每件都重新读一次勾选状态：同一件商品可能被多个页面共用
            # （例如 冒险祈愿超值礼包 在每日礼包页与每周礼包页各有一个不同 SKU）。
            if not _is_enabled(context, item.node):
                continue
            if _buy_item(context, item, layout, roi, max_scrolls):
                bought_labels.append(item.label)
            else:
                missed_labels.append(item.label)

        _dismiss_stray_dialog(context)
        logger.info(
            "商店购买：本页结束（已购买："
            + ("、".join(bought_labels) or "无")
            + "；未找到或未买成："
            + ("、".join(missed_labels) or "无")
            + "）。"
        )
        return CustomAction.RunResult(success=True)
