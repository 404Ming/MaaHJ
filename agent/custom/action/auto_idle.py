"""自动挂机任务的自定义动作（主页 → 战斗列表 → 自动挂机 → 勾选副本难度 → 确定）。

流程（实机 720x1280 逐跳核对）：
  主页（惑星之间）右下「战斗列表」→ 战斗列表页 →「自动挂机」
  → 「选择要挂机的副本」弹窗：底部「重置」清空历史勾选，
    再按任务设置逐个系列展开 → 点开副本卡片（弹窗标题 = 副本名）→ 勾选目标难度 →「确定」
  → 外层「确定」→ 等待战斗画面出现「自动挂机中」/「停止挂机」= 挂机已启动；
    没认到就重新走一遍「自动挂机」流程（有界重试）。

为什么难度要一个 (副本 × 难度) 一个开关节点：
  interface.json 的 checkbox 每个 case 只能覆写**不同的**节点字段（同一字段会被整体替换、
  后选的 case 覆盖先选的），所以「野猪统领-普通」和「野猪统领-困难」必须是两个独立节点。
  本模块的 `SERIES` 表是唯一事实来源：Pipeline 载体节点和 interface 选项都由它生成。

实机结论（决定下面这些常量）：
  · 「重置」会把整张弹窗里所有副本的勾选清零（实测点完野猪统领由 1/2 变 0/2）；
  · 副本卡片的难度勾选会**持久化**（重开弹窗仍是勾选态），所以必须先重置再按设置勾；
  · 副本卡片上只有立绘 + 右下角「已选/总数」，没有可 OCR 的名字 → 只能点开读弹窗标题；
  · 难度行的圆形勾选框固定在 x≈517，选中态是**亮金色对勾**（亮金像素 ~171，未选中 ~12）；
  · 传说副本的难度行是「<副本名>难度N」，武器副本是「<副本名>-普通 / -困难」，
    神骸试炼的非传说副本只有一行「<副本名>」（无难度后缀）。
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
from utils.params import parse_params

from .dungeon_challenge import (
    _back_to_hub,
    _box_center_y,
    _on_hub,
    _screencap,
)
from .gameplay_challenge import _ocr
from .machine_god_descent import _click_box, _find_contains, _find_in

# ---- 任务设置载体节点（interface.json 的选项覆写它们）--------------------------------
_FLAG_PREFIX = "AutoIdle"

# ---- 副本表（唯一事实来源）-----------------------------------------------------------
# (系列名, 系列横幅 OCR 关键字, ((副本名, 弹窗标题关键字, 类型), ...))
# 类型：weapon = 武器副本（普通/困难）、legend = 传说副本（难度1~6）、single = 单难度
SERIES: tuple[tuple[str, str, tuple[tuple[str, str, str], ...]], ...] = (
    (
        "王国灾厄",
        "王国",
        (
            ("野猪统领", "野猪", "weapon"),
            ("青鬃熊", "青鬃熊", "weapon"),
            ("狂暴树精", "树精", "weapon"),
            ("猩猩王", "猩猩", "weapon"),
            ("恶魔康斯洛特", "康斯洛特", "legend"),
        ),
    ),
    (
        "始源巨兽",
        "始源",
        (
            ("米劳罗", "米劳罗", "weapon"),
            ("潘塔菲卡", "潘塔菲卡", "weapon"),
            ("苏尔特尔", "苏尔特尔", "weapon"),
            ("赫拉斯瓦尔格", "赫拉斯瓦尔格", "weapon"),
            ("卡律布狄斯", "卡律布狄斯", "weapon"),
            ("伊南娜", "伊南娜", "weapon"),
            ("魔君诺维克", "诺维克", "legend"),
        ),
    ),
    (
        "神祝之翼",
        "神祝",
        (
            ("迪亚波罗", "迪亚波罗", "weapon"),
            ("斯文托维特", "斯文托维特", "weapon"),
            ("欧若拉", "欧若拉", "weapon"),
            ("阿格莱亚", "阿格莱亚", "weapon"),
            ("太阿", "太阿", "weapon"),
            ("戈舆", "戈舆", "weapon"),
            ("女武神艾莉瑞尔", "艾莉瑞尔", "legend"),
        ),
    ),
    (
        "烛之造物",
        "烛之",
        (
            ("焚尽花", "焚尽花", "weapon"),
            ("和音傀", "和音傀", "weapon"),
            ("欲望泉", "欲望泉", "weapon"),
            ("断裁偶", "断裁偶", "weapon"),
            ("绝罚刺", "绝罚刺", "weapon"),
            ("虚妄冠", "虚妄冠", "weapon"),
            ("孕星烛母", "孕星烛母", "legend"),
        ),
    ),
    (
        "神骸试炼",
        "神骸",
        (
            ("天阳御者·法厄同", "法厄同", "single"),
            ("登神天马·柏勒洛丰", "柏勒洛丰", "single"),
            ("屠魔英仙·珀尔修斯", "珀尔修斯", "single"),
            ("净世猎手·俄里翁", "俄里翁", "single"),
            ("无终冥士·俄耳浦斯", "俄耳浦斯", "legend"),
        ),
    ),
)

# 难度定义：key（节点名后缀）、显示名、弹窗行里的匹配别名
# ⚠️ 弹窗里显示哪些难度行**跟账号进度走**（实测王国灾厄是「普通/困难」，
#    始源巨兽是「困难/噩梦」，烛之造物只剩「困难」），所以这里列的是"可能出现的全集"，
#    运行时按别名逐行匹配，弹窗里没有的档位直接跳过并记一条 warning。
_DIFF_WEAPON: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Normal", "普通", ("普通", "简单")),
    ("Hard", "困难", ("困难",)),
    ("Nightmare", "噩梦", ("噩梦",)),
)
# 传说副本实测最高到难度10（弹窗一屏只放 4 行，选高难度要滚动弹窗内的列表）。
_DIFF_LEGEND: tuple[tuple[str, str, tuple[str, ...]], ...] = tuple(
    (f"L{n}", f"难度{n}", (f"难度{n}",)) for n in range(1, 11)
)
_DIFF_SINGLE: tuple[tuple[str, str, tuple[str, ...]], ...] = (("Single", "", ()),)

_DIFF_BY_KIND = {"weapon": _DIFF_WEAPON, "legend": _DIFF_LEGEND, "single": _DIFF_SINGLE}

# 系列 × 隐藏难度：这两个系列的武器副本实测只有「困难」一档（没有普通/噩梦），
# UI 里就不要列那两档了 —— 列出来用户勾了也永远勾不上（运行时只会记 warning 跳过）。
_HIDDEN_DIFFS: dict[str, frozenset[str]] = {
    "神祝之翼": frozenset({"Normal", "Nightmare"}),
    "烛之造物": frozenset({"Normal", "Nightmare"}),
}


@dataclass(frozen=True)
class Dungeon:
    """一个可挂机副本：系列、载体节点、标题关键字、难度清单。"""

    series: str
    series_key: str
    index: int
    title: str
    keyword: str
    kind: str
    node_prefix: str

    @property
    def difficulties(self) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
        """该副本实际提供的难度（已去掉本系列不该出现的档位）。"""
        hidden = _HIDDEN_DIFFS.get(self.series, frozenset()) if self.kind == "weapon" else frozenset()
        return tuple(item for item in _DIFF_BY_KIND[self.kind] if item[0] not in hidden)

    def flag(self, diff_key: str) -> str:
        return f"{self.node_prefix}_{diff_key}"

    @property
    def labels(self) -> dict[str, str]:
        """难度 key → 给用户看的名字（single 用「无难度」占位）。"""
        return {key: (name or "默认") for key, name, _alias in self.difficulties}


def _build_dungeons() -> tuple[Dungeon, ...]:
    out: list[Dungeon] = []
    for series_idx, (series_name, series_key, items) in enumerate(SERIES, start=1):
        for dungeon_idx, (title, keyword, kind) in enumerate(items, start=1):
            out.append(
                Dungeon(
                    series=series_name,
                    series_key=series_key,
                    index=dungeon_idx,
                    title=title,
                    keyword=keyword,
                    kind=kind,
                    node_prefix=f"{_FLAG_PREFIX}_S{series_idx}_{dungeon_idx:02d}",
                )
            )
    return tuple(out)


DUNGEONS: tuple[Dungeon, ...] = _build_dungeons()
_SERIES_KEYS: tuple[str, ...] = tuple(key for _name, key, _items in SERIES)


def flag_nodes() -> tuple[str, ...]:
    """所有选择载体节点名（Pipeline 必须全部预定义，否则选项静默失效）。"""
    return tuple(d.flag(key) for d in DUNGEONS for key, _name, _alias in d.difficulties)


# ---- 主页 / 战斗列表页 ---------------------------------------------------------------
# 主页右侧功能列最下面一项（实测 OCR box (610,1100,79,24)）。主页左侧「惑星之间」是底部导航，
# 「战斗列表」只出现在右侧这一列，所以 ROI 不会和底部导航打架。
_HUB_BATTLE_LIST_ROI = (540, 1060, 180, 110)
_HUB_BATTLE_LIST_TEXT = "战斗列表"

# 战斗列表页：左上标题（实测 (14,12,163,50)）、顶部状态（实测 (191,162,334,36)）。
_LIST_TITLE_ROI = (0, 0, 300, 90)
_LIST_TITLE_TEXT = "战斗列表"
_LIST_STATUS_ROI = (120, 130, 500, 80)
_LIST_IDLE_EMPTY_TEXT = "没有正在战斗中的副本"
# 「自动挂机」按钮（实测文字 box (333,417,75,24)，齿轮圆钮在文字正上方）。
# ROI 要留高一点：顶部「参与中」的战斗有几条，按钮就往下移几行（实测 1 条时 y≈411，
# 2 条时 y≈592），固定小 ROI 会直接认不到按钮、整次任务放弃。
_AUTO_BUTTON_ROI = (240, 330, 280, 430)
_AUTO_BUTTON_TEXT = "自动挂机"

# ---- 「选择要挂机的副本」弹窗 -------------------------------------------------------
_DIALOG_TITLE_ROI = (170, 125, 400, 75)
_DIALOG_TITLE_TEXT = "选择要挂机的副本"
# 弹窗内可滚动的内容区（底部「重置 / 确定」条固定，y≈960 以下不要滑）。
_DIALOG_LIST_ROI = (40, 180, 640, 770)
_DIALOG_SCROLL_TOP = (360, 320, 360, 900)
_DIALOG_SCROLL_DOWN = (360, 870, 360, 520)
_RESET_ROI = (55, 1000, 130, 70)
_RESET_TEXT = "重置"
_DIALOG_CONFIRM_ROI = (270, 975, 180, 60)
_DIALOG_CONFIRM_TEXT = "确定"

# 副本卡片栅格（实测 720x1280：横幅高 170、3 列卡片约 200x90、行距 120）
# 横幅顶部 ≈ 横幅艺术字 OCR 框的 y；第一行卡片中心 = 横幅顶部 + 225（实测 217~238）。
_CARD_X = (154, 364, 574)
_CARD_ROW1_OFFSET = 225
_CARD_ROW_STEP = 120
_CARDS_PER_ROW = 3
_CARD_ROW_MAX = 3
_CARD_SAFE_BOTTOM = 920  # 卡片中心超过它就先滚动，否则点了会落到「重置/确定」条上
# 卡片金色描边检测：一行卡片的上/下边框是长金色横线（3 张卡 ≈600px 连续金色）
# ⚠️ 卡片**净高实测 87~88px**（一行 3 张 200x88），行距才是 120。原来写成 100~140
# 把它们全过滤掉了，`_card_rows` 永远返回空 → `_ensure_expanded` 永远认为"没展开"
# 而去点横幅、把本来展开的系列收起来，后面所有坐标全错（漏「野猪统领」的真凶）。
_GOLD_EDGE_MIN_PX = 150
_GOLD_EDGE_SLACK = 4  # 金线 y 的抖动容差
_CARD_HALF_H = 44  # 卡片净高 88 → 上边框到中心 44
# 卡片列（实测 3 张卡：x 54~254 / 264~464 / 474~674，卡间各留 10px 缝）
_CARD_BOX_X = ((54, 254), (264, 464), (474, 674))
_CARD_GAP_X = ((254, 264), (464, 474))
_CARD_GAP_MAX = 6  # 卡片行在卡缝里应该是全黑的
_CARD_COVER_MIN = 0.8  # 有卡的那一列，边框线基本盖满
_CARD_COVER_EMPTY = 0.15  # 没卡的那一列，边框线基本没有

# ---- 副本难度弹窗 --------------------------------------------------------------------
# 标题（实测「野猪统领」box (291,402,136,39)）
_MODAL_TITLE_ROI = (90, 380, 540, 55)
# 难度行列表区（实测行 y=505/580/654/729，行距约 75）
_MODAL_ROWS_ROI = (120, 480, 480, 310)
_MODAL_SCROLL_UP = (350, 700, 350, 560)
_MODAL_SCROLL_DOWN = (350, 560, 350, 700)
# 难度最多 10 档、一屏 4 行：每次下滑 140px，留 5 次足够滚到最底（多滚无害，认不到就停）。
_MODAL_SCROLL_TRIES = 5
# 圆形勾选框的固定横坐标（实测中心 x≈517，直径约 45）
_MODAL_CIRCLE_X = 517
_MODAL_CIRCLE_HALF = 22
# 选中态 = 亮金色对勾：实测亮金像素 171，未选中 12（阈值取 60 留足余量）。
_CIRCLE_CHECKED_MIN = 60
# 弹窗「确定」（实测 (329,804,61,36)）；注意别和外层弹窗的「确定」(y≈993) 混用。
_MODAL_CONFIRM_ROI = (270, 780, 180, 70)

# ---- 挂机运行态 ----------------------------------------------------------------------
# 战斗画面里的浮层：实测「自动挂机中」(303,869,107,24)、「停止挂机」(300,1189,120,37)。
_IDLING_ROI = (0, 780, 720, 460)
_IDLING_TEXTS = ("自动挂机中", "停止挂机")

# ---- 节奏 ----------------------------------------------------------------------------
_CLICK_SETTLE = 1.2
_SCROLL_SETTLE = 1.0
_MODAL_OPEN_TIMEOUT = 3.5
_DIALOG_OPEN_TIMEOUT = 10.0
_LIST_OPEN_TIMEOUT = 15.0
_IDLING_TIMEOUT = 60.0
# 没进挂机就重走「自动挂机」流程的轮数（用户要求：不成功就再点一次）。
_MAX_ATTEMPTS = 3


def _click_at(context: Context, x: int, y: int) -> None:
    context.tasker.controller.post_click(int(x), int(y)).wait()


def _swipe(context: Context, begin: Sequence[int], end: Sequence[int], duration: int = 400) -> None:
    context.tasker.controller.post_swipe(int(begin[0]), int(begin[1]), int(end[0]), int(end[1]), duration).wait()


def _ocr_lines(context: Context, roi: Sequence[int]) -> list[tuple[str, Any]]:
    """ROI 内 OCR，按 y 升序返回 [(文本, 识别框)]：弹窗里同一屏要按行判断，顺序很重要。"""
    lines = [(str(text).strip(), box) for text, box in _ocr(context, roi) if str(text).strip()]
    return sorted(lines, key=lambda item: _box_center_y(item[1]))


def box_top(box: Any) -> float:
    """识别框的上边界（兼容 list / tuple / MaaFW Rect）。"""
    if isinstance(box, (list, tuple)):
        return float(box[1])
    return float(box.y)


def _wait_text(
    context: Context,
    roi: Sequence[int],
    wanted: str,
    timeout: float,
    *,
    exact: bool = False,
) -> Any:
    """在 ROI 里有界等待某文本出现（默认包含匹配），命中返回识别框。"""
    deadline = time.time() + timeout
    while True:
        box = _find_in(context, roi, wanted) if exact else _find_contains(context, roi, wanted)
        if box is not None:
            return box
        if time.time() >= deadline:
            return None
        time.sleep(0.6)


# ---- 页面判定 -----------------------------------------------------------------------
def _dialog_open(context: Context) -> bool:
    return _find_contains(context, _DIALOG_TITLE_ROI, _DIALOG_TITLE_TEXT) is not None


def _on_list_page(context: Context) -> bool:
    """战斗列表页：左上标题是「战斗列表」，且右侧有「自动挂机」按钮。"""
    if _find_contains(context, _LIST_TITLE_ROI, _LIST_TITLE_TEXT) is None:
        return False
    return _find_in(context, _AUTO_BUTTON_ROI, _AUTO_BUTTON_TEXT) is not None


def _idle_not_running(context: Context) -> bool:
    """战斗列表页顶部明说「没有正在战斗中的副本」→ 挂机确实没起来。"""
    return _find_contains(context, _LIST_STATUS_ROI, _LIST_IDLE_EMPTY_TEXT) is not None


def _enter_dialog(context: Context) -> bool:
    """从任意页面走到「选择要挂机的副本」弹窗。"""
    if _dialog_open(context):
        return True
    if not _on_list_page(context):
        if not _on_hub(context):
            logger.info("自动挂机：当前不在主页，先返回主页。")
            if not _back_to_hub(context):
                logger.error("自动挂机：回不到主页，本次放弃。")
                return False
        if not _find_in(context, _HUB_BATTLE_LIST_ROI, _HUB_BATTLE_LIST_TEXT):
            # 主页右下角「战斗列表」认不到就退到"包含"匹配（文字偶尔带描边/粘连）
            entry = _find_contains(context, _HUB_BATTLE_LIST_ROI, _HUB_BATTLE_LIST_TEXT)
            if entry is None:
                logger.error("自动挂机：主页上没认出「战斗列表」按钮，本次放弃。")
                return False
        else:
            entry = _find_in(context, _HUB_BATTLE_LIST_ROI, _HUB_BATTLE_LIST_TEXT)
        logger.info("自动挂机：点主页「战斗列表」。")
        _click_box(context, entry)
        if _wait_text(context, _LIST_TITLE_ROI, _LIST_TITLE_TEXT, _LIST_OPEN_TIMEOUT) is None:
            logger.error("自动挂机：进不去战斗列表页，本次放弃。")
            return False
        time.sleep(_CLICK_SETTLE)

    button = _find_in(context, _AUTO_BUTTON_ROI, _AUTO_BUTTON_TEXT)
    if button is None:
        button = _find_contains(context, _AUTO_BUTTON_ROI, _AUTO_BUTTON_TEXT)
    if button is None:
        logger.error("自动挂机：战斗列表页上没认出「自动挂机」按钮，本次放弃。")
        return False
    logger.info("自动挂机：点「自动挂机」打开副本选择弹窗。")
    _click_box(context, button)
    if _wait_text(context, _DIALOG_TITLE_ROI, _DIALOG_TITLE_TEXT, _DIALOG_OPEN_TIMEOUT) is None:
        logger.error("自动挂机：「选择要挂机的副本」弹窗没出来，本次放弃。")
        return False
    time.sleep(_CLICK_SETTLE)
    return True


# ---- 弹窗内导航 ---------------------------------------------------------------------
def _scroll_dialog_top(context: Context) -> None:
    for _ in range(4):
        _swipe(context, _DIALOG_SCROLL_TOP[0:2], _DIALOG_SCROLL_TOP[2:4], 300)
        time.sleep(0.5)
    time.sleep(_SCROLL_SETTLE)


def _find_banner(context: Context, series_key: str) -> Any:
    """在当前视野里找某系列的横幅（艺术字 OCR 只能认出前两字，按包含匹配）。"""
    best = None
    for text, box in _ocr_lines(context, _DIALOG_LIST_ROI):
        if series_key in text.replace(" ", ""):
            if best is None or _box_center_y(box) < _box_center_y(best):
                best = box
    return best


def _locate_banner(context: Context, series_key: str) -> Any:
    """滚到能看见目标系列横幅的位置；返回横幅识别框（含系列名艺术字的那一段）。

    先看当前视野，再回到列表顶部逐屏往下找。步长别太大：横幅高 170，
    一次滑 350px 很容易"跳"过横幅让 OCR 始终只看到半截艺术字而认不出。
    """
    box = _find_banner(context, series_key)
    if box is not None:
        return box
    _scroll_dialog_top(context)
    box = _find_banner(context, series_key)
    if box is not None:
        return box
    for _ in range(14):
        _swipe(context, [360, 840], [360, 640], 350)
        time.sleep(0.8)
        box = _find_banner(context, series_key)
        if box is not None:
            return box
    return None


def _gold_lines(img: Any) -> list[tuple[int, int, int, bool]]:
    """画面里的横向亮线：[(y, 金色像素数, 卡缝里的金色数, 是否卡片上边框)]。

    只看"金线计数/长度"区分不了卡片和横幅：横幅金框同样是整条亮线（实测计数 600），
    被艺术字压住时还会断成 166px 的段，长得跟一张卡的边框一模一样（实测把「始源」
    横幅金框当成卡片行，直接点进横幅里）。两条实测差别最可靠：
      · **卡缝**：3 张卡之间各留 10px 缝，卡片的边框线在缝里是纯黑（gaps=0）；
        横幅金框横贯整条内容，缝里照样是金色（gaps≈20）。
      · **列的覆盖率**：卡片上边框横贯整张卡（三列覆盖率≈0.9，且要么有卡、要么整列
        没卡 ≈0）；立绘上的金色（头发/盔甲，实测 0.3~0.7）凑不出"每列都满"的形状。
    """
    y0, y1 = 200, 975
    band = img[y0:y1, 40:680].astype(int)
    red, green, blue = band[:, :, 2], band[:, :, 1], band[:, :, 0]
    gold = (red > 120) & (green > 90) & (blue < 110)
    counts = gold.sum(axis=1)
    groups: list[list[tuple[int, int, int, bool]]] = []
    for i, count in enumerate(counts.tolist()):
        if count < _GOLD_EDGE_MIN_PX:
            continue
        # band 从 x=40 开始截，索引要减掉
        gaps = sum(int(gold[i, left - 40 : right - 40].sum()) for left, right in _CARD_GAP_X)
        cover = [float(gold[i, left - 40 : right - 40].mean()) for left, right in _CARD_BOX_X]
        is_card = (
            gaps <= _CARD_GAP_MAX
            and max(cover) >= _CARD_COVER_MIN
            and all(c >= _CARD_COVER_MIN or c <= _CARD_COVER_EMPTY for c in cover)
        )
        y = y0 + i
        item = (y, count, gaps, is_card)
        # 只合并"紧挨着"的行（边框线通常占 1~2 行）：放宽到 3 会把边框和卡片立绘上的
        # 金色行连成一大坨，行位置被平均到卡片中间（实测「烛之」第 2 行因此算错 29px）。
        if groups and y - groups[-1][-1][0] <= 1:
            groups[-1].append(item)
        else:
            groups.append([item])
    return [
        (
            int(sum(y for y, _c, _g, _k in group) / len(group)),
            max(c for _y, c, _g, _k in group),
            min(g for _y, _c, g, _k in group),
            max(group, key=lambda item: item[1])[3],
        )
        for group in groups
    ]


def _dump_frame(context: Context, tag: str) -> None:
    """调试用（AUTOIDLE_DEBUG=1）：把当前帧存成 _test_out/dbg_<tag>.npy。"""
    if not os.environ.get("AUTOIDLE_DEBUG"):
        return
    img = _screencap(context)
    if img is None:
        return
    import numpy as np  # noqa: PLC0415 只有调试时才需要

    np.save(os.path.join("_test_out", f"dbg_{tag}.npy"), img)


def _gold_dump(context: Context) -> str:
    """诊断用：把当前画面的金线压成一行 "y/计数/缝/是否卡片行"。"""
    img = _screencap(context)
    if img is None:
        return "无截图"
    return " ".join(f"{y}/{count}/{gaps}/{1 if is_card else 0}" for y, count, gaps, is_card in _gold_lines(img))


def _card_rows(context: Context, banner_top: float | None = None) -> list[int]:
    """找出副本卡片的**行中心 y**（像素级，不依赖 OCR）。

    判据见 `_gold_lines`（卡缝 + 列覆盖率）。卡片下边框压在立绘暗部渐变上，实测经常
    认不出来（计数 ~143），所以不靠"上下边框成对"，而靠**相邻两行上边框正好差一个
    行距 120** 的等间距链：链头 + 半个卡高 44 = 第一行中心，然后每 +120 一行。

    banner_top 给了就只认该横幅下面那几行（下界 +30、上界 +620 容得下 3 行），
    给 None 就返回全屏所有行。
    """
    img = _screencap(context)
    if img is None:
        return []
    tops = [y for y, _count, _gaps, is_card in _gold_lines(img) if is_card]
    if not tops:
        return []

    def in_window(y: float) -> bool:
        if banner_top is None:
            return 140 <= y <= _CARD_SAFE_BOTTOM
        # 下界只留 +30：横幅被滚成"半截"时 OCR 框顶会显著偏低（实测「始源」滚到只剩
        # 一条边时框顶 y=251，而它的第一行卡片中心才 315），卡太严会把第一行整行丢掉。
        return banner_top + 30 < y < banner_top + 620

    def has(target: float) -> bool:
        return any(abs(target - t) <= _GOLD_EDGE_SLACK for t in tops)

    # ① 120 等间距链：链头是第一行卡片的上边框
    starts = [t for t in tops if has(t + _CARD_ROW_STEP) and in_window(t + _CARD_HALF_H)]
    rows: list[int] = []
    if starts:
        y = min(starts)
        while has(y) and len(rows) < _CARD_ROW_MAX:
            center = y + _CARD_HALF_H
            if in_window(center):
                rows.append(center)
            y += _CARD_ROW_STEP
    if rows:
        return rows

    # ② 只有一行卡的系列：没有 120 邻居，单独一条也认（计数要求更高，免得被噪声骗）
    singles = [t for t in tops if in_window(t + _CARD_HALF_H)]
    if singles:
        return [min(singles) + _CARD_HALF_H]
    return []


def _ensure_expanded(context: Context, series_key: str) -> float | None:
    """确保系列展开且卡片可见，返回（已按滚动量修正的）横幅顶部 y；失败返回 None。

    展开判定只认"横幅下方出现卡片金色描边"（`_card_rows`）—— 卡片的「0/2」小字
    OCR 时灵时不灵，不能当判据。判定不成立时按两种情况处理：
      ① 没展开 → 点横幅；
      ② 展开了但卡片落在底部「重置/确定」条下面 → 往下滚，同时把横幅 y 一起减掉。
    每次都只做一步、再判一次，绝不"没看到卡片就再点一下横幅"（会把刚展开的收起来）。
    """
    _close_stuck_modal(context)
    box = _locate_banner(context, series_key)
    if box is None:
        logger.warning("自动挂机：滚动找不到「{}」系列横幅。", series_key)
        return None
    top = box_top(box)
    if _card_rows(context, top):
        return top

    logger.info("自动挂机：展开「{}」系列。", series_key)
    _click_box(context, box)
    time.sleep(_CLICK_SETTLE)

    for _ in range(3):
        if _card_rows(context, top):
            return top
        _swipe(context, [360, 840], [360, 700], 350)
        time.sleep(0.8)
        top -= 140
        if _card_rows(context, top):
            return top

    # 滚了 3 屏还看不到卡片：多半是第一下把"本来就展开"的系列收起来了，再点一次
    box = _locate_banner(context, series_key)
    if box is None:
        logger.warning("自动挂机：「{}」系列滚动后找不到横幅。", series_key)
        return None
    logger.info("自动挂机：再点一次「{}」横幅（上一下把它收起来了）。", series_key)
    _click_box(context, box)
    time.sleep(_CLICK_SETTLE)
    top = box_top(box)
    for _ in range(3):
        if _card_rows(context, top):
            return top
        _swipe(context, [360, 840], [360, 700], 350)
        time.sleep(0.8)
        top -= 140
        if _card_rows(context, top):
            return top
    logger.warning("自动挂机：「{}」系列展开后仍看不到副本卡片。", series_key)
    return None


# ---- 难度勾选 -----------------------------------------------------------------------
def _circle_checked(context: Context, box: Any) -> bool:
    """难度行的圆形勾选框是否为选中态（亮金色对勾）。"""
    img = _screencap(context)
    if img is None:
        return False
    cy = int(_box_center_y(box))
    x0 = max(0, _MODAL_CIRCLE_X - _MODAL_CIRCLE_HALF)
    y0 = max(0, cy - _MODAL_CIRCLE_HALF)
    patch = img[y0 : y0 + _MODAL_CIRCLE_HALF * 2, x0 : x0 + _MODAL_CIRCLE_HALF * 2].astype(int)
    if patch.size == 0:
        return False
    green, red = patch[:, :, 1], patch[:, :, 2]
    bright = int(((red > 170) & (green > 130)).sum())
    return bright >= _CIRCLE_CHECKED_MIN


def _read_modal_rows(context: Context) -> list[tuple[str, Any]]:
    return _ocr_lines(context, _MODAL_ROWS_ROI)


def _alias_hit(text: str, alias: str) -> bool:
    """别名命中，但要求后面不紧跟数字。

    ⚠️ 难度到 10 之后 "难度1" 是 "难度10" 的子串：不卡这个边界，选「难度1」会
    命中「难度10」那一行（弹窗滚到下面、只露出 10 的时候尤其容易点错）。
    """
    start = text.find(alias)
    while start >= 0:
        end = start + len(alias)
        if end >= len(text) or not text[end].isdigit():
            return True
        start = text.find(alias, start + 1)
    return False


def _match_row(rows: list[tuple[str, Any]], dungeon: Dungeon, key: str) -> Any:
    """在一屏难度行里找目标难度对应的那一行。"""
    aliases = next(alias for k, _n, alias in dungeon.difficulties if k == key)
    for text, box in rows:
        plain = text.replace(" ", "")
        if dungeon.keyword not in plain:
            continue
        if key == "Single":
            # 单难度副本（神骸试炼前四个）整张弹窗就一行，名字带不带难度后缀都算它
            return box
        if any(_alias_hit(plain, alias) for alias in aliases):
            return box
    return None


def _select_difficulties(
    context: Context,
    dungeon: Dungeon,
    wanted: set[str],
    rows: list[tuple[str, Any]],
) -> int:
    """在难度弹窗里按设置勾选；返回实际勾上的难度数。调用方负责点「确定」。"""
    done: set[str] = set()
    # 按难度表顺序勾（难度1 → 难度10），不要用 sorted()：弹窗只能往下滚，
    # 按字符串排序时 "L10" 排在 "L2" 前面，先勾 10 再回头找 2 会永远找不到。
    for key, _name, _alias in dungeon.difficulties:
        if key not in wanted:
            continue
        box = _match_row(rows, dungeon, key)
        if box is None:
            # 一屏只放得下 4 行：难度 7~10 要滚动弹窗内的列表才露出来
            for _ in range(_MODAL_SCROLL_TRIES):
                _swipe(context, _MODAL_SCROLL_UP[0:2], _MODAL_SCROLL_UP[2:4], 400)
                time.sleep(_SCROLL_SETTLE)
                rows = _read_modal_rows(context)
                box = _match_row(rows, dungeon, key)
                if box is not None:
                    break
        if box is None:
            logger.warning(
                "自动挂机：{} 弹窗里没有「{}」这一项（可能未解锁），跳过。",
                dungeon.title,
                dungeon.labels.get(key, key),
            )
            continue
        if _circle_checked(context, box):
            done.add(key)
            continue
        _click_at(context, _MODAL_CIRCLE_X, int(_box_center_y(box)))
        time.sleep(0.35)
        if _circle_checked(context, box):
            done.add(key)
        else:
            _dump_frame(context, f"modal_{dungeon.node_prefix}_{key}")
            logger.warning(
                "自动挂机：{}「{}」勾选后没变成选中态（点在 x={}, y={}）。",
                dungeon.title,
                dungeon.labels.get(key, key),
                _MODAL_CIRCLE_X,
                int(_box_center_y(box)),
            )
    return len(done)


def _confirm_modal(context: Context) -> bool:
    confirm = _find_in(context, _MODAL_CONFIRM_ROI, _DIALOG_CONFIRM_TEXT)
    if confirm is None:
        confirm = _find_contains(context, _MODAL_CONFIRM_ROI, _DIALOG_CONFIRM_TEXT)
    if confirm is None:
        logger.warning("自动挂机：难度弹窗里认不到「确定」。")
        return False
    _click_box(context, confirm)
    time.sleep(0.8)
    return True


def _close_stuck_modal(context: Context) -> None:
    """难度弹窗没关上时兜底关掉：它盖住整个弹窗会让后面的展开/识别全部失效。

    实测踩过：某次点开副本卡片后 OCR 没读出难度行，`_open_card` 超时返回，
    弹窗就留在屏幕上，于是后面所有横幅点击都打在弹窗后面，整个系列"谁都展开不了"。
    """
    if _find_in(context, _MODAL_CONFIRM_ROI, _DIALOG_CONFIRM_TEXT) is not None:
        logger.info("自动挂机：发现难度弹窗还开着，先点「确定」关掉。")
        _confirm_modal(context)


def _open_card(context: Context, x: int, y: int) -> tuple[str, list[tuple[str, Any]]] | None:
    """点开一张副本卡片，返回 (弹窗标题, 难度行)；没弹窗 / 认不出副本返回 None。

    ⚠️ 标题是艺术字，OCR 会掉字（实测「青鬃熊」只认出「王」），所以**不靠标题认副本**：
    真正用来认身份的是难度行文案（「青鬃熊-普通」这种整串普通字体）。标题只用于日志。
    认不出副本就当没点开 —— 末行不满 3 张卡时点到空位会 OCR 到「0/2」计数，不能当弹窗。
    """
    _close_stuck_modal(context)
    _click_at(context, x, y)
    deadline = time.time() + _MODAL_OPEN_TIMEOUT
    title = ""
    rows: list[tuple[str, Any]] = []
    while time.time() < deadline:
        if not title:
            lines = _ocr_lines(context, _MODAL_TITLE_ROI)
            if lines:
                title = lines[0][0]
        rows = _read_modal_rows(context)
        if _identify_dungeon(title, rows) is not None:
            return title, rows
        time.sleep(0.3)
    _close_stuck_modal(context)
    return None


def _identify_dungeon(title: str, rows: list[tuple[str, Any]]) -> Dungeon | None:
    """按难度行文案认副本（行里带副本名），行认不出来再退回标题。"""
    for text, _box in rows:
        plain = text.replace(" ", "")
        for dungeon in DUNGEONS:
            if dungeon.keyword in plain:
                return dungeon
    return _match_dungeon(title) if title else None


def _match_dungeon(title: str) -> Dungeon | None:
    plain = title.replace(" ", "")
    for dungeon in DUNGEONS:
        if dungeon.keyword in plain:
            return dungeon
    return None


# ---- 系列 → 卡片 --------------------------------------------------------------------
# 横幅艺术字框顶压到 y≈225：再高（<190）横幅上半截会被弹窗标题栏裁掉，OCR 只认到
# 半截字、框顶也就跟着虚高，后面按它推出来的卡片行会整整错一行（实测错到下一行）。
_ALIGN_TARGET_TOP = 225
_ALIGN_PAD = 30
_ALIGN_MIN_STEP = 60
_ALIGN_MAX_STEP = 170


def _align_series_top(context: Context, series_key: str) -> float | None:
    """把目标系列横幅对到可视区上沿附近，返回对齐后的横幅顶 y。

    展开时会一路往下滚，很容易滚过第 1 行（实测因此漏掉「苏尔特尔」）；
    这里先把横幅找回来，再按差额一点点推，直到横幅顶≈225，横幅 + 3 行卡片全在视野内。

    只认 OCR 会出大问题：横幅被推成半截时艺术字经常整片认不出来，「找不到就
    直接 return」会让调用方继续用滚动前的老 top 推卡片行 —— 三个点全落到
    「重置/确定」条下面（y>920 被静默跳过），整行卡片一个都没点（实测漏掉
    「野猪统领」）。所以这里改成"死推算 + OCR 校正"：记下最后一次认到的 y，
    每滚一次就按滚动量递减，OCR 认回来再校准；推完把最终 y 交给调用方。
    """
    box = _find_banner(context, series_key)
    for _ in range(6):
        if box is not None:
            break
        _swipe(context, [360, 700], [360, 900], 350)  # 往上翻，找回横幅
        time.sleep(0.7)
        box = _find_banner(context, series_key)
    if box is None:
        box = _locate_banner(context, series_key)  # 兜底：回列表顶部逐屏往下找
    if box is None:
        return None
    top = box_top(box)
    for _ in range(8):
        shift = top - _ALIGN_TARGET_TOP
        if abs(shift) <= _ALIGN_PAD:
            return top
        step = int(max(-_ALIGN_MAX_STEP, min(_ALIGN_MAX_STEP, shift)))
        if abs(step) < _ALIGN_MIN_STEP:
            step = _ALIGN_MIN_STEP if step > 0 else -_ALIGN_MIN_STEP
        _swipe(context, [360, 700], [360, 700 - step], 320)
        time.sleep(0.7)
        top -= step
        found = _find_banner(context, series_key)
        if found is not None:
            top = box_top(found)
    return top


def _banner_spans(context: Context) -> list[tuple[float, float]]:
    """当前视野里所有系列横幅的纵向范围 [(top, bottom)]，用来避免把行点进下一个横幅。

    横幅 OCR 框只框住艺术字（实测框顶比横幅金框顶低 ≈21px），所以上下各放宽：
    上界 -30 容下金框，下界 +200 覆盖横幅高 170 + 偏移。点到横幅上会把那个系列
    展开/收起、整屏位置全乱（实测「王国」第 3 行就点在「始源」横幅上）。
    """
    spans: list[tuple[float, float]] = []
    for text, box in _ocr_lines(context, _DIALOG_LIST_ROI):
        plain = text.replace(" ", "")
        if any(key in plain for key in _SERIES_KEYS):
            top = box_top(box)
            spans.append((top - 30, top + 200))
    return spans


def _process_series(context: Context, selected: dict[str, set[str]], series_name: str, series_key: str) -> bool:
    """处理一个系列：展开 → 逐张点开卡片 → 认出的副本按设置勾难度。

    卡片上只有立绘，没有可 OCR 的名字，所以只能"点开一张读一张"；
    认出副本后即使不在清单里也要点「确定」把弹窗关掉（不会改动它的勾选）。
    """
    targets = [d for d in DUNGEONS if d.series == series_name and d.title in selected and selected[d.title]]
    if not targets:
        return True
    pending = {d.title for d in targets}
    handled: set[str] = set()

    top = _ensure_expanded(context, series_key)
    if top is None:
        # 单个系列展开不了不该拖垮整次任务：记日志，其它系列（和"确定"）照常走
        logger.warning("自动挂机：「{}」系列没能展开，跳过：{}", series_name, "、".join(sorted(pending)))
        return True

    # 卡片行数由 SERIES 表决定（每行 3 张）：5 个副本 = 2 行、7 个 = 3 行。
    # 以前一律点 3 行，2 行系列的第 3 行正好落在下一个系列的**横幅**上，
    # 一点就把那个系列展开/收起、整屏位置全乱（实测「王国」因此一个目标都没勾上）。
    series_total = sum(1 for d in DUNGEONS if d.series == series_name)
    row_count = max(1, min(_CARD_ROW_MAX, (series_total + _CARDS_PER_ROW - 1) // _CARDS_PER_ROW))
    tapped: set[tuple[int, int]] = set()
    for _round in range(4):
        # 每轮都重新把横幅对到顶部再推行位置：滚过之后老 top 就不成立了，
        # 沿用老 top 会算出屏幕外/被按钮条挡住的 y，静默跳过整行（实测漏过「野猪统领」）。
        aligned = _align_series_top(context, series_key)
        if aligned is None:
            logger.warning("自动挂机：第 {} 轮没能重新认出「{}」横幅，沿用上一轮位置。", _round + 1, series_key)
        else:
            top = aligned
        # 行位置优先用图像识别出来的真实卡片行（金色描边，误差 0）；认不出来才退回
        # "横幅艺术字框顶 +225、行距 120"的版式推算。
        rows = _card_rows(context, top)
        _dump_frame(context, f"{series_key}_{_round}_{int(top)}")
        if rows:
            logger.info("自动挂机：「{}」横幅顶 y={}，识别出卡片行 {}。", series_key, int(top), rows)
        else:
            rows = [int(top) + _CARD_ROW1_OFFSET + _CARD_ROW_STEP * k for k in range(row_count)]
            # 推算出来的位置要挡一下：别落进下一个系列的横幅（点上去会把人家展开/收起）
            spans = _banner_spans(context)
            rows = [y for y in rows if not any(lo <= y <= hi for lo, hi in spans)]
            logger.info("自动挂机：「{}」横幅顶 y={}，没认出卡片描边，按版式推算 {}。", series_key, int(top), rows)
        usable = [y for y in rows if 140 <= y <= _CARD_SAFE_BOTTOM]
        if len(rows) < row_count or not usable:
            logger.info(
                "自动挂机：「{}」只认出 {} 行（应为 {}），金线：{}",
                series_key,
                len(rows),
                row_count,
                _gold_dump(context),
            )
        for y in usable:
            for x in _CARD_X:
                if not pending:
                    break
                if (x, y) in tapped:
                    continue
                tapped.add((x, y))
                opened = _open_card(context, x, int(y))
                if opened is None:
                    # 末行不满 3 张、或卡片被按钮条挡了一半，点到空位很常见：直接跳过。
                    # 如果其实是把系列点收起了，下一轮 `_card_rows` 会是空，会重新展开。
                    logger.info("自动挂机：({},{}) 不是副本卡片（空位/被挡），跳过。", x, y)
                    continue
                title, modal_rows = opened
                dungeon = _identify_dungeon(title, modal_rows)
                if dungeon is None:
                    logger.warning("自动挂机：({},{}) 的弹窗认不出是哪个副本（标题「{}」），跳过。", x, y, title)
                    _confirm_modal(context)
                    continue
                if dungeon.series != series_name:
                    logger.warning("自动挂机：在「{}」系列里点到了「{}」的卡片，跳过。", series_name, dungeon.title)
                    _confirm_modal(context)
                    continue
                if dungeon.title in handled:
                    logger.info("自动挂机：({},{}) 又是「{}」（已处理过），跳过。", x, y, dungeon.title)
                    _confirm_modal(context)
                    continue
                handled.add(dungeon.title)
                if dungeon.title not in pending:
                    logger.info("自动挂机：{} 不在本次要勾选的清单里，跳过。", dungeon.title)
                    _confirm_modal(context)
                    continue
                logger.info(
                    "自动挂机：{} 勾选难度 {}。",
                    dungeon.title,
                    "、".join(sorted(dungeon.labels.get(k, k) for k in selected[dungeon.title])),
                )
                got = _select_difficulties(context, dungeon, selected[dungeon.title], modal_rows)
                _confirm_modal(context)
                pending.discard(dungeon.title)
                if got == 0:
                    logger.warning("自动挂机：{} 一个难度都没勾上。", dungeon.title)
            if not pending:
                break
        if not pending:
            return True
        # 还有没认到的：往下滚一点再扫（7 个副本的系列可能还有没进画面的行）
        _swipe(context, [360, 800], [360, 650], 350)
        time.sleep(0.8)
    if pending:
        logger.warning("自动挂机：「{}」系列里没找到：{}", series_name, "、".join(sorted(pending)))
    return True


# ---- 主流程 -------------------------------------------------------------------------
def _read_selection(context: Context) -> dict[str, set[str]]:
    """读所有 (副本 × 难度) 开关节点，返回 {副本名: {难度 key}}。"""
    selected: dict[str, set[str]] = {}
    for dungeon in DUNGEONS:
        keys = set()
        for key, _name, _alias in dungeon.difficulties:
            node = dungeon.flag(key)
            try:
                data = context.get_node_data(node) or {}
            except Exception as error:  # noqa: BLE001 读不到按未选处理，不能因此崩掉
                logger.warning("自动挂机：读取节点 {} 失败：{}", node, error)
                continue
            if "enabled" not in data:
                logger.warning("自动挂机：节点 {} 没有 enabled 字段（未预定义？），按未选处理。", node)
                continue
            if bool(data.get("enabled")):
                keys.add(key)
        if keys:
            selected[dungeon.title] = keys
    return selected


def _apply_selection(context: Context, selected: dict[str, set[str]]) -> bool:
    # 有难度弹窗盖着的话，「重置」点不到（实测会白白跳过清空，沿用上一次的勾选）
    _close_stuck_modal(context)
    reset = _find_in(context, _RESET_ROI, _RESET_TEXT) or _find_contains(context, _RESET_ROI, _RESET_TEXT)
    if reset is None:
        logger.error("自动挂机：弹窗里认不到「重置」，无法清空历史勾选。")
        return False
    logger.info("自动挂机：点「重置」清空弹窗里已有的勾选。")
    _click_box(context, reset)
    time.sleep(_CLICK_SETTLE)
    _scroll_dialog_top(context)

    total = sum(len(keys) for keys in selected.values())
    logger.info(
        "自动挂机：按设置勾选 {} 个难度（{} 个副本）。",
        total,
        len(selected),
    )
    for series_name, series_key, _items in SERIES:
        if not _process_series(context, selected, series_name, series_key):
            return False
    return True


def _confirm_dialog(context: Context) -> bool:
    confirm = _find_in(context, _DIALOG_CONFIRM_ROI, _DIALOG_CONFIRM_TEXT)
    if confirm is None:
        confirm = _find_contains(context, _DIALOG_CONFIRM_ROI, _DIALOG_CONFIRM_TEXT)
    if confirm is None:
        logger.error("自动挂机：认不到弹窗底部「确定」。")
        return False
    _click_box(context, confirm)
    time.sleep(_CLICK_SETTLE)
    return True


def _already_idling(context: Context) -> bool:
    """画面里已经有「自动挂机中 / 停止挂机」= 挂机已经在跑（战斗界面或战斗列表浮层）。"""
    return any(_find_contains(context, _IDLING_ROI, text) is not None for text in _IDLING_TEXTS)


def _wait_idling(context: Context, timeout: float = _IDLING_TIMEOUT) -> bool:
    """等挂机真的跑起来：战斗浮层「自动挂机中 / 停止挂机」；回到空列表就算失败。"""
    deadline = time.time() + timeout
    while True:
        for text in _IDLING_TEXTS:
            if _find_contains(context, _IDLING_ROI, text) is not None:
                logger.info("自动挂机：认到「{}」，挂机已启动。", text)
                return True
        if _dialog_open(context) is False and _on_list_page(context) and _idle_not_running(context):
            logger.warning("自动挂机：确定后仍显示「没有正在战斗中的副本」，挂机没起来。")
            return False
        if time.time() >= deadline:
            return False
        time.sleep(1.0)


@AgentServer.custom_action("AutoIdleRun")
class AutoIdleRun(CustomAction):
    """按任务设置选好副本与难度并启动「自动挂机」。

    ⚠️ 会真实开始挂机（自动连续战斗、消耗 BP）。
    一个 (副本 × 难度) 都没勾选时**整段跳过、一个按钮都不点**。

    Examples:
        `custom_action_param`::

            {}
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            parse_params(argv.custom_action_param)
        except ValueError as error:
            logger.error("自动挂机：custom_action_param 解析失败：{}", error)
            return CustomAction.RunResult(success=False)

        selected = _read_selection(context)
        if not selected:
            logger.info("自动挂机：任务设置里没有勾选任何副本/难度，跳过（不点任何按钮）。")
            return CustomAction.RunResult(success=True)

        # 用户要的语义：已经在挂机就算完成。不做这一步的话，挂机中的战斗/浮层画面
        # 既不在主页也不在战斗列表，会被误判成"回不到主页"直接失败。
        if _already_idling(context):
            logger.info("自动挂机：画面已经在「自动挂机中」，本次直接完成（不改动游戏里的现有勾选）。")
            return CustomAction.RunResult(success=True)

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            logger.info("自动挂机：第 {} 次尝试。", attempt)
            if not _enter_dialog(context):
                return CustomAction.RunResult(success=False)
            if not _apply_selection(context, selected):
                return CustomAction.RunResult(success=False)
            if not _confirm_dialog(context):
                return CustomAction.RunResult(success=False)
            if _wait_idling(context):
                return CustomAction.RunResult(success=True)
            logger.warning("自动挂机：第 {} 次没进入挂机状态，重走「自动挂机」流程。", attempt)

        logger.error("自动挂机：{} 次尝试都没进入挂机状态，本次失败。", _MAX_ATTEMPTS)
        return CustomAction.RunResult(success=False)
