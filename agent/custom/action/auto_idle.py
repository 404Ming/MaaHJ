"""自动挂机任务的自定义动作（主页 → 战斗列表 → 自动挂机 → 勾选副本难度 → 确定）。

流程（实机 720x1280 逐跳核对）：
  主页（惑星之间）右下「战斗列表」→ 战斗列表页 →「自动挂机」
  → 「选择要挂机的副本」弹窗：底部「重置」清空历史勾选，
    再按任务设置逐个系列**模板定位直达副本** → 勾选目标难度 →「确定」
  → 外层「确定」→ 等待战斗画面出现「自动挂机中」/「停止挂机」= 挂机已启动；
    没认到就重新走一遍「自动挂机」流程（有界重试）。

为什么难度要一个 (副本 × 难度) 一个开关节点：
  interface.json 的 checkbox 每个 case 只能覆写**不同的**节点字段（同一字段会被整体替换、
  后选的 case 覆盖先选的），所以「野猪统领-普通」和「野猪统领-困难」必须是两个独立节点。
  本模块的 `SERIES` 表是唯一事实来源：Pipeline 载体节点和 interface 选项都由它生成。

实机结论（决定下面这些常量与算法）：
  · 「重置」会把整张弹窗里所有副本的勾选清零；副本卡片的难度勾选会**持久化**
    （重开弹窗仍是勾选态），所以必须先重置再按设置勾；
  · 副本卡片上只有立绘 + 右下角「已选/总数」，**没有可 OCR 的名字**；
    为了定位副本而逐张点开读弹窗标题是"打开查看"，每张要 0.5~3.5s —— 已废弃；
  · 改用**立绘模板匹配**定位：31 张模板（`image/AutoIdle/dungeon/<node_prefix>.png`，
    卡片左 135px、避开右下角计数）实机逐系列验证 —— 同副本 score 0.990~1.000，
    跨系列最高 0.583，阈值 0.8 两侧都有 0.19 以上余量；
  · 模板必须采自**卡片未选中态**（即「重置」之后的样子）：卡片被勾上会变成金色描边+
    提亮的形态，同一张卡的模板分从 1.000 掉到认不出来（S5_05 就因此整张吊死过）；
  · 模板匹配不能跨滚动位置使用：卡片必须完整落在 `_DIALOG_LIST_ROI` 内才命中，
    所以「滚动找卡片」与「匹配」交替进行，每轮只截一帧；
  · 弹窗内容只比一屏高约 1/3（回顶/到底各滑一次就能覆盖全部 5 个系列），
    所以找系列横幅最多滑两次 —— 原来"回顶 4 连滑 + 逐屏 14 连滑"全是多余的；
  · 难度行的圆形勾选框固定在 x≈517，选中态是**亮金色对勾**（亮金像素 ~171，未选中 ~12）；
  · 传说副本的难度行是「<副本名>难度N」，武器副本是「<副本名>-普通 / -困难」，
    神骸试炼的非传说副本只有一行「<副本名>」（无难度后缀）。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
from utils.interrupt import is_stopping
from utils.params import parse_params

from .dungeon_challenge import (
    _back_to_hub,
    _box_center_y,
    _screencap,
)
from .machine_god_descent import _click_box

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
            ("奥蕾莉亚", "奥蕾莉亚", "weapon"),
            ("倪克丝", "倪克丝", "weapon"),
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
    def template(self) -> str:
        """卡片立绘模板路径（相对 resource/base/image）。"""
        return f"AutoIdle/dungeon/{self.node_prefix}.png"

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
_DUNGEONS_BY_SERIES: dict[str, tuple[Dungeon, ...]] = {
    name: tuple(d for d in DUNGEONS if d.series == name) for name, _key, _items in SERIES
}


def flag_nodes() -> tuple[str, ...]:
    """所有选择载体节点名（Pipeline 必须全部预定义，否则选项静默失效）。"""
    return tuple(d.flag(key) for d in DUNGEONS for key, _name, _alias in d.difficulties)


# ---- 识别节点名（每次 run_recognition 现传 pipeline，不依赖 Pipeline 里预定义）----------
_OCR_NODE = "AutoIdle_OCR"
_TPL_NODE = "AutoIdle_Tpl"

# ---- 主页 / 战斗列表页 ---------------------------------------------------------------
# 主页判定锚点：底部导航第一个按钮（实测 (0,1180,170,100) 内的「惑星之间」）。
_HUB_ROI = (0, 1180, 170, 100)
_HUB_TEXT = "惑星之间"
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
# 弹窗内可滚动的内容区（底部「重置 / 确定」条固定，y≈960 以下不要滑，也不要点）。
_DIALOG_LIST_ROI = (40, 180, 640, 770)
# 往下扫的步长刻意小于"一屏 - 一张卡"（770-88=682）：保证每一行卡片在某一屏里**完整**出现，
# 既不会跳过一行，也不会因为卡片压在视察区边缘而漏匹配。
_SCROLL_LIST_DOWN = ((360, 760), (360, 560))
# 回顶可以一次滑得远一点（滑过头无所谓，反正要的是顶）。
_SCROLL_LIST_UP = ((360, 300), (360, 950))
_SCROLL_DURATION = 400
_RESET_ROI = (55, 1000, 130, 70)
_RESET_TEXT = "重置"
_DIALOG_CONFIRM_ROI = (270, 975, 180, 60)
_DIALOG_CONFIRM_TEXT = "确定"

# 卡片实宽（模板只裁了卡片左 135px，模板框左缘 = 卡片左缘，点击中心要 +卡宽/2）。
_CARD_WIDTH = 200

# 一次性向下扫弹窗的最多屏数 / 回顶的最多滑数（都靠"画面不变"提前收工）。
_SCAN_MAX_STEPS = 14
_SCROLL_MAX_STEPS = 8
# "画面是否还在动"的比较区域（列表内容区）与阈值，标定过程见 `_same_scene`。
_LIST_DIFF_BOX = (180, 950, 40, 680)
_SCENE_SAME_MAX_DIFF = 8.0
# 立绘模板匹配阈值：实机逐系列实测（重置后=卡片未选中态）同副本 0.990~1.000、
# 跨系列最高 0.583，0.8 落在中间，两侧余量都超过 0.19。
_DUNGEON_TPL_THRESHOLD = 0.8

# ---- 副本难度弹窗 --------------------------------------------------------------------
# 难度行列表区（实测行 y=505/580/654/729，行距约 75，一屏 4 行）。
# ⚠️ 认副本只认这里的难度行文案（普通字体、带副本名），不去 OCR 弹窗标题：
#    标题是艺术字，OCR 会掉字（实测「青鬃熊」只认出「王」），拿它认身份必然出错。
_MODAL_ROWS_ROI = (120, 480, 480, 310)
# 手指上滑 → 弹窗内列表往下翻，露出更高难度（难度 5~10 要滚才看得见）
_MODAL_SCROLL_DOWN = ((350, 700), (350, 560))
# 难度最多 10 档、一屏 4 行：每次下滑 140px，留 5 次足够滚到最底（多滚无害，认不到就停）。
_MODAL_SCROLL_TRIES = 5
# 圆形勾选框的固定横坐标（实测中心 x≈517，直径约 45）
_MODAL_CIRCLE_X = 517
_MODAL_CIRCLE_HALF = 22
# 选中态 = 亮金色对勾：实测亮金像素 171，未选中 12（阈值取 60 留足余量）。
_CIRCLE_CHECKED_MIN = 60
# 弹窗「确定」：weapon 弹窗实测 (329,804,61,36)，legend 弹窗（难度行更多）实测确定在
# y≈857 —— ROI 必须同时盖住两者（770~930），否则 legend 弹窗关不掉、滞留在屏幕上
# 挡住后续所有点击。下界 930 < 外层「确定」y≈993，不会混。
_MODAL_CONFIRM_ROI = (270, 770, 180, 160)

# ---- 挂机运行态 ----------------------------------------------------------------------
# 战斗画面里的浮层：实测「自动挂机中」(303,869,107,24)、「停止挂机」(300,1189,120,37)。
_IDLING_ROI = (0, 780, 720, 460)
_IDLING_TEXTS = ("自动挂机中", "停止挂机")

# ---- 节奏 ----------------------------------------------------------------------------
# 一次 ADB screencap ≈180ms、单次 ROI OCR ≈150~400ms：能共用一帧就共用一帧。
_CLICK_SETTLE = 0.6  # 点按钮/确认弹窗后的界面刷新
_CLICK_TICK = 0.3  # 连续点同一弹窗里的勾选框（不截图，只等 UI 响应）
_SCROLL_SETTLE = 0.35  # 滑动后的惯性落定
_EXPAND_SETTLE = 0.5  # 点系列横幅展开后的动画
_MODAL_OPEN_TIMEOUT = 2.5  # 点开卡片后等难度弹窗（认到目标难度行即返回）
_MODAL_OPEN_POLL = 0.25
_DIALOG_OPEN_TIMEOUT = 8.0
_LIST_OPEN_TIMEOUT = 12.0
_POLL_STEP = 0.4
_IDLING_TIMEOUT = 60.0
_IDLING_POLL = 0.8
# 点完外层「确定」后先给游戏几秒切场景，之后再看到「没有正在战斗中的副本」才算真失败。
_IDLING_FAIL_GRACE = 3.0
# 没进挂机就重走「自动挂机」流程的轮数（用户要求：不成功就再点一次）。
_MAX_ATTEMPTS = 3


def _click_at(context: Context, x: int, y: int) -> None:
    context.tasker.controller.post_click(int(x), int(y)).wait()


def _swipe(context: Context, begin: Sequence[int], end: Sequence[int], duration: int = 400) -> None:
    context.tasker.controller.post_swipe(int(begin[0]), int(begin[1]), int(end[0]), int(end[1]), duration).wait()


def _frame(context: Context) -> Any:
    """截一帧（BGR）。失败返回 None —— 调用方一律当"识别不到"处理。"""
    return _screencap(context)


def box_top(box: Any) -> float:
    """识别框的上边界（兼容 list / tuple / MaaFW Rect）。"""
    if isinstance(box, (list, tuple)):
        return float(box[1])
    return float(box.y)


# ---- 一帧多用：OCR / 文本查找 / 模板匹配 --------------------------------------------
def _ocr_lines(context: Context, roi: Sequence[int], img: Any = None) -> list[tuple[str, Any]]:
    """ROI 内 OCR，按 y 升序返回 [(文本, 识别框)]：弹窗里同一屏要按行判断，顺序很重要。

    `img` 给了就在这一帧上识别（不重新截图）。同一屏要判多处时共用一帧能省掉大头 ——
    一次 ADB screencap ≈180ms，而这里每个判断以前都会各自截一帧。
    """
    image = img if img is not None else _frame(context)
    if image is None:
        return []
    detail = context.run_recognition(
        _OCR_NODE,
        image,
        {_OCR_NODE: {"recognition": {"type": "OCR", "param": {"roi": list(roi)}}, "action": {"type": "DoNothing"}}},
    )
    if detail is None or not getattr(detail, "hit", False):
        return []
    lines = [
        (str(result.text).strip(), result.box) for result in (detail.all_results or []) if str(result.text).strip()
    ]
    return sorted(lines, key=lambda item: _box_center_y(item[1]))


def _pick_text(lines: Sequence[tuple[str, Any]], wanted: str) -> Any:
    """在一屏 OCR 结果里找文本：先全等、再包含（都忽略空格）。找不到返回 None。"""
    target = wanted.replace(" ", "")
    for text, box in lines:
        if text.replace(" ", "") == target:
            return box
    for text, box in lines:
        if target in text.replace(" ", ""):
            return box
    return None


def _find_text(context: Context, roi: Sequence[int], wanted: str, img: Any = None) -> Any:
    """ROI 里找文本（全等优先、退化为包含）。返回识别框或 None。

    以前"全等"和"包含"是两次独立调用（`_find_in` + `_find_contains`），每次各截一帧、
    各 OCR 一遍；合成一次只花一半。
    """
    return _pick_text(_ocr_lines(context, roi, img), wanted)


def _match_template(context: Context, img: Any, template: str, roi: Sequence[int] = _DIALOG_LIST_ROI) -> Any:
    """在**已截好的那一帧**上做一次 TemplateMatch，命中返回识别框。"""
    detail = context.run_recognition(
        _TPL_NODE,
        img,
        {
            _TPL_NODE: {
                "recognition": {
                    "type": "TemplateMatch",
                    "param": {"template": [template], "threshold": [_DUNGEON_TPL_THRESHOLD], "roi": list(roi)},
                },
                "action": {"type": "DoNothing"},
            }
        },
    )
    if detail is None or not getattr(detail, "hit", False):
        return None
    return getattr(detail, "box", None)


def _wait_text(context: Context, roi: Sequence[int], wanted: str, timeout: float, *, exact: bool = False) -> Any:
    """在 ROI 里有界等待某文本出现（默认包含匹配），命中返回识别框。"""
    deadline = time.time() + timeout
    while True:
        lines = _ocr_lines(context, roi)
        if exact:
            target = wanted.replace(" ", "")
            box = next((b for t, b in lines if t.replace(" ", "") == target), None)
        else:
            box = _pick_text(lines, wanted)
        if box is not None:
            return box
        if is_stopping(context) or time.time() >= deadline:
            return None
        time.sleep(_POLL_STEP)


# ---- 页面判定 -----------------------------------------------------------------------
def _probe_page(context: Context, img: Any) -> str:
    """在**同一帧**上判当前页面：'dialog' / 'list' / 'hub' / 'unknown'。"""
    if _find_text(context, _DIALOG_TITLE_ROI, _DIALOG_TITLE_TEXT, img) is not None:
        return "dialog"
    if _find_text(context, _LIST_TITLE_ROI, _LIST_TITLE_TEXT, img) is not None:
        return "list"
    if _find_text(context, _HUB_ROI, _HUB_TEXT, img) is not None:
        return "hub"
    return "unknown"


def _dialog_open(context: Context, img: Any = None) -> bool:
    return _find_text(context, _DIALOG_TITLE_ROI, _DIALOG_TITLE_TEXT, img) is not None


def _idle_not_running(context: Context, img: Any) -> bool:
    """战斗列表页顶部明说「没有正在战斗中的副本」→ 挂机确实没起来。"""
    return _find_text(context, _LIST_STATUS_ROI, _LIST_IDLE_EMPTY_TEXT, img) is not None


def _enter_dialog(context: Context) -> bool:
    """从任意页面走到「选择要挂机的副本」弹窗。"""
    img = _frame(context)
    page = _probe_page(context, img)
    if page == "dialog":
        return True

    if page != "list":
        if page != "hub":
            logger.debug("自动挂机：当前不在主页，先返回主页。")
            if not _back_to_hub(context):
                logger.error("自动挂机：回不到主页，本次放弃。")
                return False
            img = _frame(context)
        entry = _find_text(context, _HUB_BATTLE_LIST_ROI, _HUB_BATTLE_LIST_TEXT, img)
        if entry is None:
            logger.error("自动挂机：主页上没认出「战斗列表」按钮，本次放弃。")
            return False
        logger.debug("自动挂机：点主页「战斗列表」。")
        _click_box(context, entry)
        if _wait_text(context, _LIST_TITLE_ROI, _LIST_TITLE_TEXT, _LIST_OPEN_TIMEOUT) is None:
            logger.error("自动挂机：进不去战斗列表页，本次放弃。")
            return False
        # 列表条目是异步加载的：「参与中」有几条会把「自动挂机」按钮的位置推下去，
        # 等半秒让布局落定再取坐标，比原来固定睡 1.2s 快、也不会点空。
        time.sleep(_SCROLL_SETTLE)

    img = _frame(context)
    button = _find_text(context, _AUTO_BUTTON_ROI, _AUTO_BUTTON_TEXT, img)
    if button is None:
        logger.error("自动挂机：战斗列表页上没认出「自动挂机」按钮，本次放弃。")
        return False
    logger.debug("自动挂机：点「自动挂机」打开副本选择弹窗。")
    _click_box(context, button)
    if _wait_text(context, _DIALOG_TITLE_ROI, _DIALOG_TITLE_TEXT, _DIALOG_OPEN_TIMEOUT) is None:
        logger.error("自动挂机：「选择要挂机的副本」弹窗没出来，本次放弃。")
        return False
    return True


# ---- 弹窗内导航 ---------------------------------------------------------------------
def _scroll_list(context: Context, *, up: bool) -> None:
    begin, end = _SCROLL_LIST_UP if up else _SCROLL_LIST_DOWN
    _swipe(context, begin, end, _SCROLL_DURATION)


def _same_scene(prev: Any, cur: Any) -> bool:
    """两帧是否停在"同一屏"：用来判断列表已经滑到头了。

    没有这个判据就只能靠固定次数滑动去"猜"到底在哪 —— 滑多了白等（原来"回顶 4 连滑"
    就是这么来的）、滑少了漏系列。
    判据用列表区域的平均绝对差，阈值由实机实测标定：
      · 静止两帧 0.000~0.235（弹窗背景有轻微呼吸光效，不可能恒等于 0）；
      · 真滚一屏 62.09；
      · 已经到底再滑（只回弹一下）3.55。
    取 8.0 落在"回弹"和"真滚动"之间，两侧都有 4 倍以上余量。
    """
    if prev is None or cur is None:
        return False
    if getattr(prev, "shape", None) != getattr(cur, "shape", None):
        return False
    y0, y1, x0, x1 = _LIST_DIFF_BOX
    delta = prev[y0:y1, x0:x1].astype("int16") - cur[y0:y1, x0:x1].astype("int16")
    return float(abs(delta).mean()) < _SCENE_SAME_MAX_DIFF


def _scroll_and_frame(context: Context, *, up: bool, prev: Any = None) -> tuple[Any, bool]:
    """滑一屏并返回新帧；返回 (新帧, 画面是否变化)。画面不变 = 已经滑到头。

    `prev` 传进来就复用调用方手上那一帧当基准，省掉一次截图（≈180ms）。
    """
    if prev is None:
        prev = _frame(context)
    _scroll_list(context, up=up)
    time.sleep(_SCROLL_SETTLE)
    img = _frame(context)
    return img, not _same_scene(prev, img)


def _scroll_to_top(context: Context, img: Any = None) -> Any:
    """滚到弹窗列表顶部并返回那一帧（"画面不再变化"为到顶判据）。"""
    if img is None:
        img = _frame(context)
    for _ in range(_SCROLL_MAX_STEPS):
        img, changed = _scroll_and_frame(context, up=True, prev=img)
        if not changed:
            break
    return img


def _close_stuck_modal(context: Context, img: Any = None) -> None:
    """难度弹窗没关上时兜底关掉：它盖住整个弹窗会让后面的展开/识别全部失效。

    实测踩过：某次点开副本卡片后 OCR 没读出难度行，弹窗就留在屏幕上，于是后面所有
    横幅/卡片点击都打在弹窗后面，整个系列"谁都点不动"。
    """
    box = _find_text(context, _MODAL_CONFIRM_ROI, _DIALOG_CONFIRM_TEXT, img)
    if box is None:
        return
    logger.debug("自动挂机：发现难度弹窗还开着，先点「确定」关掉。")
    _click_box(context, box)
    time.sleep(_CLICK_SETTLE)


# ---- 模板定位副本 -------------------------------------------------------------------
def _locate_cards(context: Context, img: Any, dungeons: Sequence[Dungeon]) -> list[tuple[Dungeon, int, int]]:
    """在**同一帧**上批量定位副本卡片，返回 [(副本, 点击中心 x, 点击中心 y)]（上→下、左→右）。

    每个目标一次 TemplateMatch，但**只截一次图**：screencap 才是大头，匹配本身只要几毫秒。
    点击中心：模板是"卡片左 135px"的裁剪，所以 x = 模板左缘 + 卡片半宽(100)；
    y = 模板上缘 + 模板高/2（模板高 = 卡片净高 88）。
    """
    hits: list[tuple[Dungeon, int, int]] = []
    for dungeon in dungeons:
        box = _match_template(context, img, dungeon.template)
        if box is None:
            continue
        hits.append((dungeon, int(float(box[0]) + _CARD_WIDTH / 2), int(box_top(box) + float(box[3]) / 2)))
    hits.sort(key=lambda item: (item[2], item[1]))
    return hits


def _series_expanded(context: Context, img: Any, series_name: str) -> bool:
    """该系列是否已展开：判据是"这个系列的任意一张卡片能被模板认出来"。

    比"金色描边行数推算"稳得多（描边会压在立绘暗部上、认不全），也比"点一下看看"安全
    （对已展开的系列再点一下会把它收起来，整屏位置全乱）。
    """
    return any(
        _match_template(context, img, dungeon.template) is not None for dungeon in _DUNGEONS_BY_SERIES[series_name]
    )


def _find_pending_banner(context: Context, img: Any, pending: dict[str, Dungeon]) -> tuple[str, Any] | None:
    """当前屏里"还有未处理副本"的系列横幅，返回 (系列名, 横幅框)；取最靠上的一个。"""
    waiting = {dungeon.series for dungeon in pending.values()}
    if not waiting:
        return None
    best: tuple[str, Any] | None = None
    for text, box in _ocr_lines(context, _DIALOG_LIST_ROI, img):
        plain = text.replace(" ", "")
        for series_name, series_key, _items in SERIES:
            if series_name not in waiting or series_key not in plain:
                continue
            if best is None or _box_center_y(box) < _box_center_y(best[1]):
                best = (series_name, box)
    return best


def _scan_pass(
    context: Context,
    selected: dict[str, set[str]],
    pending: dict[str, Dungeon],
    expanded_series: set[str],
    expand_clicks: dict[str, int],
    img: Any,
) -> Any:
    """从给定那一帧开始**往下**扫：模板命中就点入勾选，遇到未展开的目标系列就展开。

    一屏只截一帧：① 先把本屏能模板定位到的目标全部点入勾完；
    ② 本屏有目标系列但没命中 → 没展开就点横幅，已展开就往下滚（目标在下面几行）；
    ③ 画面滑不动了 = 到底，收工。
    """
    for _screen in range(_SCAN_MAX_STEPS):
        if not pending:
            break
        if img is None:
            img = _frame(context)
            if img is None:
                break
        # ① 本屏能定位到的目标：模板命中即点入、勾难度、关弹窗
        for dungeon, cx, cy in _locate_cards(context, img, list(pending.values())):
            if dungeon.title not in pending:
                continue
            expanded_series.add(dungeon.series)
            _open_and_select(context, dungeon, cx, cy, selected[dungeon.title])
            pending.pop(dungeon.title, None)
        if not pending:
            break

        # ② 本屏还有目标的系列：没展开就点横幅展开（最多两次，第二次是自纠正）
        banner = _find_pending_banner(context, img, pending)
        if banner is not None:
            series_name, box = banner
            if series_name not in expanded_series:
                if _series_expanded(context, img, series_name):
                    expanded_series.add(series_name)
                elif expand_clicks.get(series_name, 0) < 2:
                    expand_clicks[series_name] = expand_clicks.get(series_name, 0) + 1
                    logger.debug("自动挂机：展开「{}」系列。", series_name)
                    _click_box(context, box)
                    time.sleep(_EXPAND_SETTLE)
                    img = _frame(context)
                    continue
                else:
                    expanded_series.add(series_name)

        # ③ 往下滚一屏；滑不动说明到底了。复用手中这一帧当基准，少截一次图
        img, changed = _scroll_and_frame(context, up=False, prev=img)
        if not changed:
            break
    return img


def _scan_dialog(context: Context, selected: dict[str, set[str]]) -> None:
    """把设置里的（副本 × 难度）在弹窗里勾上。

    先就着**当前滚动位置**往下扫一遍（弹窗刚打开/上一次操作后通常就在目标附近，省掉
    "回顶"那几下），没扫完的再回顶补扫一遍。整个弹窗只比一屏高一点，两遍一定能覆盖。
    """
    pending: dict[str, Dungeon] = {d.title: d for d in DUNGEONS if selected.get(d.title)}
    if not pending:
        return
    logger.debug(
        "自动挂机：按设置勾选 {} 个难度（{} 个副本）。",
        sum(len(keys) for keys in selected.values()),
        len(selected),
    )

    # 已经确认展开过的系列：只要在这个系列里命中过任意一张卡，它必然展开着。
    # ⚠️ 不能只靠 `_series_expanded` 现判：选中的卡片会变成金色描边+提亮的形态，
    # 立绘模板（采自未选中态）认不出来 —— 一个系列里"本屏可见的卡都已经被选上、
    # 还有目标在下一行"时就会误判成"没展开"而把系列收起来。
    expanded_series: set[str] = set()
    expand_clicks: dict[str, int] = {}
    img = _scan_pass(context, selected, pending, expanded_series, expand_clicks, _frame(context))
    if pending:
        logger.debug("自动挂机：当前位置往下没扫完，回顶再补扫一遍。")
        img = _scroll_to_top(context, img)
        _scan_pass(context, selected, pending, expanded_series, expand_clicks, img)
    if pending:
        logger.warning("自动挂机：弹窗里没找到这些副本：{}", "、".join(sorted(pending)))


# ---- 难度勾选 -----------------------------------------------------------------------
def _circle_checked(img: Any, box: Any, x: int = _MODAL_CIRCLE_X) -> bool:
    """难度行的圆形勾选框是否为选中态（亮金色对勾）。在**给定的那一帧**上取样，不再截图。"""
    cy = int(_box_center_y(box))
    x0 = max(0, int(x) - _MODAL_CIRCLE_HALF)
    y0 = max(0, cy - _MODAL_CIRCLE_HALF)
    patch = img[y0 : y0 + _MODAL_CIRCLE_HALF * 2, x0 : x0 + _MODAL_CIRCLE_HALF * 2].astype(int)
    if patch.size == 0:
        return False
    green, red = patch[:, :, 1], patch[:, :, 2]
    return int(((red > 170) & (green > 130)).sum()) >= _CIRCLE_CHECKED_MIN


def _read_modal_rows(context: Context) -> list[tuple[str, Any]]:
    return _ocr_lines(context, _MODAL_ROWS_ROI)


def _plain(text: str) -> str:
    return text.replace(" ", "")


def _has_keyword(rows: Sequence[tuple[str, Any]], keyword: str) -> bool:
    return any(keyword in _plain(text) for text, _box in rows)


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


def _match_row(rows: Sequence[tuple[str, Any]], dungeon: Dungeon, key: str) -> Any:
    """在一屏难度行里找目标难度对应的那一行。"""
    aliases = next(alias for k, _n, alias in dungeon.difficulties if k == key)
    for text, box in rows:
        plain = _plain(text)
        if dungeon.keyword not in plain:
            continue
        if key == "Single":
            # 单难度副本（神骸试炼前四个）整张弹窗就一行，名字带不带难度后缀都算它
            return box
        if any(_alias_hit(plain, alias) for alias in aliases):
            return box
    return None


def _click_and_verify(context: Context, dungeon: Dungeon, batch: list[tuple[str, Any]]) -> tuple[set[str], Any]:
    """把一批（同一滚动位置上的）难度一次点完，再截一帧统一验证；没勾上的补点一次。

    返回 (确认勾上的 key 集合, 最后那一帧)。那一帧还能直接给 `_confirm_modal` 用 ——
    弹窗没动，「确定」的位置不变，不用为了找它再截一次。
    """
    done: set[str] = set()
    img: Any = None
    for _attempt in range(2):
        todo = [(key, box) for key, box in batch if key not in done]
        if not todo:
            break
        for _key, box in todo:
            _click_at(context, _MODAL_CIRCLE_X, int(_box_center_y(box)))
            time.sleep(_CLICK_TICK)
        img = _frame(context)
        if img is None:
            break
        for key, box in todo:
            if _circle_checked(img, box):
                done.add(key)
            elif _attempt == 0:
                logger.debug("自动挂机：{}「{}」没勾上，补点一次。", dungeon.title, dungeon.labels.get(key, key))
    return done, img


def _select_difficulties(
    context: Context,
    dungeon: Dungeon,
    wanted: set[str],
    rows: list[tuple[str, Any]],
) -> tuple[int, Any]:
    """在难度弹窗里按设置勾选；返回 (实际勾上的难度数, 弹窗最后一帧)。调用方负责点「确定」。

    先按难度表的自然顺序（难度1 → 难度10）把目标行找出来；弹窗一屏只放 4 行，
    找高难度要滚动弹窗内的列表 —— 滚过之后前面那些行的 y 就失效了，所以**每个
    滚动位置上的行点完、验完再继续往下找**（`_read_modal_rows` 只在滚动时才再叫）。
    「重置」已经把整张弹窗清空，因此不做逐项预检：直接点，点完截一帧统一验证。
    """
    done: set[str] = set()
    batch: list[tuple[str, Any]] = []
    current_rows = rows
    last_img: Any = None

    def flush() -> None:
        nonlocal batch, last_img
        if not batch:
            return
        got, last_img = _click_and_verify(context, dungeon, batch)
        done.update(got)
        batch = []

    for key, _name, _alias in dungeon.difficulties:
        if key not in wanted:
            continue
        box = _match_row(current_rows, dungeon, key)
        if box is None:
            flush()  # 先把当前这一屏能找到的点完再滚，否则滚完坐标就废了
            found = None
            for _ in range(_MODAL_SCROLL_TRIES):
                _swipe(context, _MODAL_SCROLL_DOWN[0], _MODAL_SCROLL_DOWN[1], 400)
                time.sleep(_SCROLL_SETTLE)
                current_rows = _read_modal_rows(context)
                found = _match_row(current_rows, dungeon, key)
                if found is not None:
                    break
            if found is None:
                logger.warning(
                    "自动挂机：{} 弹窗里没有「{}」这一项（可能未解锁），跳过。",
                    dungeon.title,
                    dungeon.labels.get(key, key),
                )
                continue
            box = found
        batch.append((key, box))
    flush()
    return len(done), last_img


def _confirm_modal(context: Context, img: Any = None) -> bool:
    confirm = _find_text(context, _MODAL_CONFIRM_ROI, _DIALOG_CONFIRM_TEXT, img)
    if confirm is None:
        logger.warning("自动挂机：难度弹窗里认不到「确定」。")
        return False
    _click_box(context, confirm)
    time.sleep(_CLICK_SETTLE)
    return True


def _open_card(context: Context, x: int, y: int, dungeon: Dungeon) -> list[tuple[str, Any]] | None:
    """点开一张副本卡片，有界等待**目标副本**的难度弹窗出来；返回难度行，超时返回 None。

    认身份用难度行文案（「野猪统领-普通」这种普通字体的整串），**不靠弹窗标题** ——
    标题是艺术字，OCR 会掉字（实测「青鬃熊」只认出「王」）。等"目标副本的关键字出现"
    而不是"任意文本出现"：外层弹窗的横幅艺术字也会落进这个 ROI，等任意文本就会误判。
    """
    _click_at(context, x, y)
    # 弹窗有开启动画：点完立刻 OCR 一定是空帧（白花一次截图+OCR），先等一个轮询间隔。
    time.sleep(_MODAL_OPEN_POLL)
    deadline = time.time() + _MODAL_OPEN_TIMEOUT
    while True:
        rows = _read_modal_rows(context)
        if _has_keyword(rows, dungeon.keyword):
            return rows
        if is_stopping(context) or time.time() >= deadline:
            logger.warning(
                "自动挂机：点开 ({},{}) 后没等到「{}」的难度弹窗。",
                x,
                y,
                dungeon.title,
            )
            return None
        time.sleep(_MODAL_OPEN_POLL)


def _open_and_select(context: Context, dungeon: Dungeon, cx: int, cy: int, wanted: set[str]) -> bool:
    """模板命中 → 点开 → 勾难度 → 关弹窗。"""
    rows = _open_card(context, cx, cy, dungeon)
    if rows is None:
        _close_stuck_modal(context)
        return False
    logger.debug(
        "自动挂机：模板命中「{}」({}, {})，勾选难度 {}。",
        dungeon.title,
        cx,
        cy,
        "、".join(sorted(dungeon.labels.get(k, k) for k in wanted)),
    )
    got, frame = _select_difficulties(context, dungeon, wanted, rows)
    # 复用验证勾选态时那一帧找「确定」：弹窗没滚过，按钮位置不变，省一次截图+OCR。
    _confirm_modal(context, frame)
    if got == 0:
        logger.warning("自动挂机：{} 一个难度都没勾上。", dungeon.title)
        return False
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
    img = _frame(context)
    reset = _find_text(context, _RESET_ROI, _RESET_TEXT, img)
    if reset is None:
        logger.error("自动挂机：弹窗里认不到「重置」，无法清空历史勾选。")
        return False
    logger.debug("自动挂机：点「重置」清空弹窗里已有的勾选。")
    _click_box(context, reset)
    time.sleep(_CLICK_SETTLE)
    # 不再"回顶 4 连滑"（≈3.2s 白等）：`_scan_dialog` 会自己用"画面不变"判据滚到顶。
    _scan_dialog(context, selected)
    return True


def _confirm_dialog(context: Context) -> bool:
    confirm = _find_text(context, _DIALOG_CONFIRM_ROI, _DIALOG_CONFIRM_TEXT)
    if confirm is None:
        logger.error("自动挂机：认不到弹窗底部「确定」。")
        return False
    _click_box(context, confirm)
    time.sleep(_CLICK_SETTLE)
    return True


def _already_idling(context: Context) -> bool:
    """画面里已经有「自动挂机中 / 停止挂机」= 挂机已经在跑（战斗界面或战斗列表浮层）。"""
    img = _frame(context)
    if img is None:
        return False
    joined = "".join(text for text, _box in _ocr_lines(context, _IDLING_ROI, img))
    return any(text in _plain(joined) for text in _IDLING_TEXTS)


def _wait_idling(context: Context, timeout: float = _IDLING_TIMEOUT) -> bool:
    """等挂机真的跑起来：战斗浮层「自动挂机中 / 停止挂机」；回到空列表就算失败。"""
    deadline = time.time() + timeout
    start = time.time()
    while True:
        img = _frame(context)
        if img is not None:
            joined = "".join(text for text, _box in _ocr_lines(context, _IDLING_ROI, img))
            for text in _IDLING_TEXTS:
                if text in _plain(joined):
                    logger.info("自动挂机：认到「{}」，挂机已启动。", text)
                    return True
            if time.time() - start >= _IDLING_FAIL_GRACE and _idle_not_running(context, img):
                logger.warning("自动挂机：确定后仍显示「没有正在战斗中的副本」，挂机没起来。")
                return False
        if is_stopping(context) or time.time() >= deadline:
            return False
        time.sleep(_IDLING_POLL)


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
            if is_stopping(context):
                logger.info("自动挂机：收到停止请求，中断本次流程。")
                return CustomAction.RunResult(success=False)
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
