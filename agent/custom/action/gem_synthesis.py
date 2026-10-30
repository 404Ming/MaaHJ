"""宝石合成：主页 → 菜单 → 宝石 → 按设置的等级从低到高合成，跑完自动回主页。

实机 720x1280 逐跳印证（2026-10-29，MuMu 127.0.0.1:16512，OCR box 均为真机截图实测）：

  主页「菜单」              (639,1232,51,30)
  → 菜单页「宝石」           (471,977,53,32)
  → 宝石页（合成页签）       标题「宝石」(37,28,92,44)、「成功率0%」(303,506,112,28)
  → 卡位格子 7 列 × 3~4 行   Lv 文案 row y=728/830/932，列 x 步长 96
  → 点卡位弹出宝石详情        「1级有利属性宝石」(231,494,203,35)
                              · 「取消」(214,744,59,36)  · 「放入」(453,741,63,41)
  → 放满 2 颗 成功率75%，3 颗 成功率100%
  → 点「合成」(332,1133,58,37)
      ·「提示 / 是否消耗N颗M级宝石进行合成?」→「确定」(480,836,70,42)
      · 放了不同属性的宝石时会**再弹一次**「放入不同属性的宝石…是否继续?」→「确定」
  → 成功：「恭喜获得」+「点击空白区域关闭」→ 点空白关掉，三格清空
  → 失败：**不弹窗**，直接回合成页（材料已被消耗）
  → 点已放入的格子可以「取下」(456,745,58,36)，宝石退回背包（不消耗）

为什么做成一个 CustomAction：
  · 一轮里有 5 个状态要按运行时画面判定（宝石页 / 宝石详情 / 提示确认 / 领奖遮屏 / 合成页复位），
    静态 next 链表达不了「这个等级的宝石不够就跳过」这种分支；
  · 等级与合成模式来自客户端选项（Flag 载体节点），循环与「宝石不够就跳过」都在运行时判定；
  · 卡位每次合成后都会**按稀有度重新排序**，必须在每一轮重新扫描卡位。

「放进去几颗」不靠 OCR 数格子，而是直接看三个格子的像素亮度：空格子实测灰度均值约 53，
放进宝石后 110+（详见 ``_count_filled``）。卡位上的「数量」在放入期间**不会刷新**，所以只能这样判。

⚠️ 会真实消耗宝石：每轮「合成」都会吃掉放入的材料（二合一失败也会吃）。

设置读取（pipeline_override → 载体节点 enabled，见 GemSynthesis.json）：
  · ``GemSynth_Flag_ModeThree`` / ``GemSynth_Flag_ModeTwo``：三合一（100%）/ 二合一（75%）
  · ``GemSynth_Flag_Lv1`` … ``GemSynth_Flag_Lv8``：要作为材料参与合成的宝石等级（多选；
    客户端目前只暴露 Lv.1-7，Lv.8 的载体先留着，补回 interface.json 的 case 就能放开）
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
from utils.interrupt import is_stopping
from utils.maa_types import ocr_results
from utils.params import parse_params

# CustomAction 内做 OCR 用的探针节点（定义见 GemSynthesis.json）。
OCR_NODE = "GemSynthesis_OcrProbe"
FULL_ROI = (0, 0, 720, 1280)

# ---- 选项载体节点 -----------------------------------------------------------
FLAG_MODE_TWO = "GemSynth_Flag_ModeTwo"
FLAG_MODE_THREE = "GemSynth_Flag_ModeThree"
FLAG_LEVEL_FMT = "GemSynth_Flag_Lv{}"
MIN_LEVEL = 1
MAX_LEVEL = 8

# ---- 页面/控件文案 -----------------------------------------------------------
HUB_MENU_TEXT = "菜单"
MENU_GEM_TEXT = "宝石"
GEM_TITLE_TEXT = "宝石"
SYNTH_TEXT = "合成"
REWARD_TITLE_TEXT = "恭喜获得"
CONFIRM_TEXT = "确定"
CANCEL_TEXT = "取消"
PUT_IN_TEXT = "放入"
REMOVE_TEXT = "取下"
# 领奖遮屏（实测标题「恭喜获得」+ 底部「点击空白区域关闭」）。
CLOSE_TEXTS = ("点击空白区域关闭", "点击空白", "点击空白处关闭")

# ---- 识别 ROI（只框到该控件，避免同名文案串页）--------------------------------
ROI_HUB_MENU = (600, 1190, 120, 90)  # 主页右下角「菜单」
ROI_MENU_GEM = (440, 940, 120, 80)  # 菜单页第三行「宝石」
ROI_GEM_TITLE = (10, 10, 150, 90)  # 宝石页左上角标题「宝石」
ROI_RATE = (250, 470, 240, 80)  # 「成功率N%」
ROI_SYNTH_BUTTON = (300, 1105, 130, 70)  # 底部「合成」大按钮
ROI_SYNTH_TAB = (395, 1215, 110, 60)  # 底部导航页签「合成」（与按钮同名，靠 ROI 区分）
ROI_DIALOG_CONFIRM = (450, 820, 130, 70)  # 提示弹窗右下「确定」
ROI_DIALOG_CANCEL = (140, 820, 130, 70)  # 提示弹窗左下「取消」
ROI_CARD_TITLE = (215, 480, 260, 70)  # 宝石详情弹窗标题「N级XXX宝石」
ROI_POPUP_RIGHT = (430, 725, 130, 70)  # 宝石详情右下「放入」/ 已放入格子的「取下」
ROI_POPUP_CANCEL = (185, 725, 130, 70)  # 宝石详情左下「取消」
ROI_REWARD_HINT = (180, 900, 380, 120)  # 领奖遮屏「点击空白区域关闭」
ROI_REWARD_TITLE = (150, 170, 420, 130)  # 领奖遮屏「恭喜获得」
# ---- 排序控件（右上角「等级 ▾ 排序」，2026-10-30 实机 720x1280）-----------------
# 字段名「等级」显示在 (477,649)，点它弹出字段下拉（等级/品质/有利属性增幅/伤害上限/攻击力，
# 第一项「等级」在 (508,717)）；方向切换点是字段右侧的倒三角图标 (669,664)：
# 默认「从高到低」，点一次变「从低到高」。方向不靠 OCR 读三角（图标太小，会被误读成「三/天」），
# 而是按卡位第一行排列判断（见 _ensure_sort_ascending）。
ROI_SORT_FIELD = (450, 640, 90, 45)  # 排序字段名「等级」
ROI_SORT_DROPDOWN = (450, 700, 160, 40)  # 下拉第一项「等级」
SORT_FIELD_POINT = (505, 666)  # 点字段名弹出下拉
SORT_DROPDOWN_LEVEL_POINT = (529, 731)  # 下拉第一项「等级」落点
SORT_TOGGLE_POINT = (669, 664)  # 倒三角：切换升降序

# ---- 固定落点（控件本身没有可 OCR 的完整文案时用；均在上述 ROI 内）--------------
HUB_MENU_POINT = (664, 1247)
MENU_GEM_POINT = (497, 993)
SYNTH_TAB_POINT = (453, 1261)
SYNTH_BUTTON_POINT = (361, 1151)
DIALOG_CONFIRM_POINT = (515, 857)
DIALOG_CANCEL_POINT = (203, 858)
POPUP_PUTIN_POINT = (484, 761)
POPUP_CANCEL_POINT = (243, 762)
REWARD_CLOSE_POINT = (360, 1000)
# 三个材料格（游戏按从左到右的顺序填充；这里只用来数「放了几颗」，与顺序无关）。
SLOT_POINTS = ((95, 435), (356, 435), (596, 435))

# 材料格亮度判定：空格子实测灰度均值 52~55、>90 的像素占比 0~0.05；
# 放进宝石后均值 110~155、>90 的像素占比 0.71~1.00。取 0.25 留足余量。
SLOT_BRIGHT = 90.0
SLOT_FILLED_RATIO = 0.25
SLOT_RADIUS = 28

# ---- 卡位格子几何（720x1280，实测） -----------------------------------------
# Lv 文案左上角 (22+96i, 714+102r)；卡片中心在 (rx+40, ry+41)。
GRID_LABEL_X0 = 22
GRID_LABEL_Y0 = 714
GRID_DX = 96
GRID_DY = 102
GRID_COLS = 7
GRID_ROWS = 4  # 前三行必现；第 4 行在宝石种类多时才会出现
GRID_LABEL_W = 72
GRID_LABEL_H = 36
GRID_CLICK_DX = 40
GRID_CLICK_DY = 41

RATE_TWO = 75
RATE_THREE = 100
POLL_INTERVAL = 0.7
PLACE_DELAY = 0.8
# 单个等级内「放不进宝石」的容忍上限：超过就放弃该等级（防拖死整轮）。
_MAX_PLACE_FAILS = 6
# 每个等级最多合成轮数：兜底防「状态判错导致无限循环」，正常账号远达不到。
MAX_ROUNDS_PER_LEVEL = 999


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
    """卡位：等级 + 点击落点（由固定格子几何推出）。"""

    level: int
    point: tuple[int, int]


def _rect(box: Any) -> tuple[int, int, int, int]:
    if box is None:
        return (0, 0, 0, 0)
    if isinstance(box, (list, tuple)):
        return (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
    return (int(box.x), int(box.y), int(box.w), int(box.h))


def _screencap(context: Context) -> Any:
    return context.tasker.controller.post_screencap().wait().get()


def _ocr(context: Context, image: Any, roi: tuple[int, int, int, int]) -> list[_Line]:
    """用 GemSynthesis_OcrProbe 节点对指定 ROI 做一次 OCR。"""
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
        lines.append(_Line(text=text, x=x, y=y, w=w, h=h, score=float(getattr(result, "score", 0.0) or 0.0)))
    return lines


def _snapshot(context: Context) -> tuple[Any, list[_Line]]:
    """截一次图 + 整屏 OCR 一次，状态判定都在内存里做，避免重复截图。"""
    image = _screencap(context)
    return image, _ocr(context, image, FULL_ROI)


def _scan(context: Context) -> list[_Line]:
    return _snapshot(context)[1]


def _click(context: Context, point: tuple[int, int]) -> None:
    context.tasker.controller.post_click(int(point[0]), int(point[1])).wait()


def _in_roi(line: _Line, roi: tuple[int, int, int, int]) -> bool:
    x, y, w, h = roi
    return x <= line.cx <= x + w and y <= line.cy <= y + h


def _pick(lines: list[_Line], roi: tuple[int, int, int, int], *texts: str) -> _Line | None:
    """返回 ROI 内第一条包含任一文案的 OCR 行。"""
    for line in lines:
        if _in_roi(line, roi) and any(text in line.text for text in texts):
            return line
    return None


def _click_text(
    context: Context,
    lines: list[_Line],
    roi: tuple[int, int, int, int],
    texts: tuple[str, ...],
    fallback: tuple[int, int],
) -> None:
    """优先点识别到的文本中心，认不到就用兜底落点。"""
    line = _pick(lines, roi, *texts)
    _click(context, (line.cx, line.cy) if line is not None else fallback)


def _count_filled(image: Any) -> int:
    """数三个材料格里已经放了几颗宝石（靠格子中央的亮度判断）。

    实测（2026-10-29）：空格子灰度均值 52~55、>90 像素占比 0~0.05；
    放宝石后灰度均值 110~155、>90 像素占比 0.71~1.00。
    """
    if image is None or getattr(image, "size", 0) == 0:
        return 0
    gray = np.asarray(image)[:, :, :3].astype("float32").mean(axis=2)
    filled = 0
    for cx, cy in SLOT_POINTS:
        patch = gray[max(0, cy - SLOT_RADIUS) : cy + SLOT_RADIUS, max(0, cx - SLOT_RADIUS) : cx + SLOT_RADIUS]
        if patch.size and float((patch > SLOT_BRIGHT).mean()) > SLOT_FILLED_RATIO:
            filled += 1
    return filled


def _node_enabled(context: Context, node_name: str) -> bool:
    """读选项载体节点的 enabled；节点读不到按关闭处理。"""
    try:
        data = context.get_node_data(node_name) or {}
    except Exception as error:  # noqa: BLE001 读不到按未勾选处理，不能因此崩掉整段
        logger.warning("宝石合成：读取节点 {} 失败：{}", node_name, error)
        return False
    if "enabled" not in data:
        logger.warning("宝石合成：节点 {} 没有 enabled 字段（未预定义？），按未勾选处理。", node_name)
        return False
    return bool(data.get("enabled"))


def _read_settings(context: Context) -> tuple[int, list[int]]:
    """读「合成模式」与「合成等级」。返回 (每轮放几颗, 从低到高的等级列表)。"""
    mode_three = _node_enabled(context, FLAG_MODE_THREE)
    mode_two = _node_enabled(context, FLAG_MODE_TWO)
    if not mode_three and not mode_two:
        logger.warning("宝石合成：合成模式两个开关都没读到，按「三合一」（100%）处理。")
        mode_three = True
    need = 3 if mode_three else 2
    levels = [
        level for level in range(MIN_LEVEL, MAX_LEVEL + 1) if _node_enabled(context, FLAG_LEVEL_FMT.format(level))
    ]
    return need, levels


def _parse_level(text: str) -> int | None:
    """从卡位 Lv 文案里取等级（实测文案为 Lv.1 / LV.2，偶发丢数字）。"""
    match = re.search(r"(\d)", text)
    if match is None:
        return None
    if "v" not in text.lower() and not text.strip().isdigit():
        return None
    level = int(match.group(1))
    return level if MIN_LEVEL <= level <= MAX_LEVEL else None


def _read_rate(lines: list[_Line]) -> int | None:
    """读「成功率N%」。认不到返回 None。"""
    for line in lines:
        match = re.search(r"成功率\s*(\d+)", line.text)
        if match is not None:
            return int(match.group(1))
    return None


def _on_hub(lines: list[_Line]) -> bool:
    return _pick(lines, ROI_HUB_MENU, HUB_MENU_TEXT) is not None


def _on_menu(lines: list[_Line]) -> bool:
    return _pick(lines, ROI_MENU_GEM, MENU_GEM_TEXT) is not None


def _on_gem_page(lines: list[_Line]) -> bool:
    """宝石页判定：左上角标题「宝石」+ 底部「合成」页签。"""
    return (
        _pick(lines, ROI_GEM_TITLE, GEM_TITLE_TEXT) is not None and _pick(lines, ROI_SYNTH_TAB, SYNTH_TEXT) is not None
    )


def _return_home(context: Context) -> None:
    """把角色送回主页（与快速炼金同一套做法）。"""
    context.clear_hit_count("BackToMainHub_Back")
    detail = context.run_task("BackToMainHub_Start")
    if detail is None or detail.status.failed:
        logger.warning("宝石合成：返回主页失败，请手动确认当前画面。")


def _goto_gem_page(context: Context) -> bool:
    """从主页/菜单页/宝石页任一状态走到宝石页的「合成」页签。"""
    for _ in range(6):
        if is_stopping(context):
            return False
        lines = _scan(context)
        if _dialog_open(lines):
            # 半路的「提示」弹窗（实测取消在 y≈858，别拿详情弹窗的取消坐标去点）：点取消退出。
            _click_text(context, lines, ROI_DIALOG_CANCEL, (CANCEL_TEXT,), DIALOG_CANCEL_POINT)
            time.sleep(0.8)
            continue
        if (
            _pick(lines, ROI_REWARD_HINT, *CLOSE_TEXTS) is not None
            or _pick(lines, ROI_REWARD_TITLE, REWARD_TITLE_TEXT) is not None
        ):
            # 上次中断留下的领奖遮屏：点空白关掉。
            _click(context, REWARD_CLOSE_POINT)
            time.sleep(1.0)
            continue
        if _popup_open(lines) or _pick(lines, ROI_POPUP_CANCEL, CANCEL_TEXT) is not None:
            # 上次中断留下的宝石详情弹窗：点取消退出。
            _click_text(context, lines, ROI_POPUP_CANCEL, (CANCEL_TEXT,), POPUP_CANCEL_POINT)
            time.sleep(0.8)
            continue
        if _on_gem_page(lines):
            return True
        if _pick(lines, ROI_GEM_TITLE, GEM_TITLE_TEXT) is not None:
            # 已在宝石页但停在「融合 / 分解」页签：切回「合成」。
            _click_text(context, lines, ROI_SYNTH_TAB, (SYNTH_TEXT,), SYNTH_TAB_POINT)
            time.sleep(1.2)
            continue
        if _on_menu(lines):
            _click_text(context, lines, ROI_MENU_GEM, (MENU_GEM_TEXT,), MENU_GEM_POINT)
            time.sleep(1.5)
            continue
        if _on_hub(lines):
            _click_text(context, lines, ROI_HUB_MENU, (HUB_MENU_TEXT,), HUB_MENU_POINT)
            time.sleep(1.2)
            continue
        logger.info("宝石合成：当前不在主页/菜单/宝石页，先返回主页再进。")
        _return_home(context)
        time.sleep(1.0)
    logger.error("宝石合成：6 步之内没能走到宝石页，放弃本次任务。")
    return False


def _ensure_sort_ascending(context: Context) -> None:
    """进入宝石页「合成」后，第一时间把卡位排序切到「等级 · 从低到高」。

    实机（2026-10-30，720x1280）：
      · 排序字段名「等级」显示在 (477,649)，点它弹字段下拉（第一项「等级」在 (508,717)）；
      · 方向切换点是字段右侧的倒三角 (669,664)：默认「从高到低」，点一次变「从低到高」。
    方向不靠 OCR 读三角图标（太小、会被误读成「三/天」），而是按卡位第一行排列判断：
    首格等级 > 末格等级 = 降序，需要点倒三角切换；升序/等级相同则不动。
    """
    # 1. 字段已经是「等级」就不动；否则点开下拉，选第一项「等级」。
    lines = _scan(context)
    if _pick(lines, ROI_SORT_FIELD, "等级") is None:
        _click(context, SORT_FIELD_POINT)
        time.sleep(0.6)
        lines = _scan(context)
        if _pick(lines, ROI_SORT_DROPDOWN, "等级") is not None:
            _click(context, SORT_DROPDOWN_LEVEL_POINT)
            time.sleep(0.8)
        else:
            logger.warning("宝石合成：排序字段下拉没展开或没认到「等级」，跳过排序切换。")
            return

    # 2. 方向：看卡位第一行首末格等级，降序（首 > 末）则点倒三角切升序。
    cards = _scan_cards(context)
    first_row = _first_row_cards(cards)
    if len(first_row) >= 2 and first_row[0].level > first_row[-1].level:
        _click(context, SORT_TOGGLE_POINT)
        time.sleep(0.8)
        logger.debug("宝石合成：排序已切到「等级 · 从低到高」。")
    else:
        logger.debug("宝石合成：排序已是「等级 · 从低到高」，无需切换。")


def _first_row_cards(cards: list[_Card]) -> list[_Card]:
    """取卡位第一行（point.y 最小的那一行）按 x 升序的卡片。"""
    if not cards:
        return []
    top_y = min(card.point[1] for card in cards)
    row = [card for card in cards if abs(card.point[1] - top_y) <= GRID_DY // 2]
    row.sort(key=lambda card: card.point[0])
    return row


def _scan_cards(context: Context, image: Any = None) -> list[_Card]:
    """扫一遍卡位格子：每个格子单独做一次小 ROI OCR（整屏 OCR 会把 Lv 数字认丢）。

    实测（2026-10-29）：小 ROI 识别 Lv 文案 18/19 命中；整屏 OCR 只命中 9/19。
    """
    if image is None:
        image = _screencap(context)
    cards: list[_Card] = []
    for row in range(GRID_ROWS):
        ry = GRID_LABEL_Y0 + GRID_DY * row
        for col in range(GRID_COLS):
            rx = GRID_LABEL_X0 + GRID_DX * col
            for line in _ocr(context, image, (rx, ry, GRID_LABEL_W, GRID_LABEL_H)):
                level = _parse_level(line.text)
                if level is None or line.score < 0.4:
                    continue
                cards.append(_Card(level=level, point=(rx + GRID_CLICK_DX, ry + GRID_CLICK_DY)))
                break
    return cards


def _card_popup_level(lines: list[_Line]) -> int | None:
    """从宝石详情弹窗标题「N级XXX宝石」里读等级。"""
    line = _pick(lines, ROI_CARD_TITLE, "级")
    if line is None:
        return None
    match = re.search(r"(\d+)\s*级", line.text)
    return int(match.group(1)) if match is not None else None


def _dialog_open(lines: list[_Line]) -> bool:
    """「提示 / 是否消耗N颗M级宝石进行合成?」这类确认弹窗是否开着。"""
    return _pick(lines, ROI_DIALOG_CONFIRM, CONFIRM_TEXT) is not None


def _popup_open(lines: list[_Line]) -> bool:
    """宝石详情弹窗是否还开着（右下是「放入」或「取下」）。"""
    return _popup_right_kind(lines) is not None


def _popup_right_kind(lines: list[_Line]) -> str | None:
    """宝石详情弹窗右下角是哪个按钮。

    实测（2026-10-29）：这张卡还有存货时是「放入」；存货用完、宝石已经在材料格里时变成「取下」。
    两者落点完全相同，所以**必须先认出来**，认不到就点「取消」，绝不盲点（盲点会把手里的宝石取下来）。
    """
    if _pick(lines, ROI_POPUP_RIGHT, PUT_IN_TEXT) is not None:
        return PUT_IN_TEXT
    if _pick(lines, ROI_POPUP_RIGHT, REMOVE_TEXT) is not None:
        return REMOVE_TEXT
    return None


def _dismiss_popup(context: Context) -> None:
    """把残留的宝石详情弹窗 / 提示确认弹窗点「取消」关掉。

    实测（2026-10-29）：对已经用空的卡位点「放入」时宝石不会进格子，弹窗**也不会关**，
    再点下一张卡就会打在弹窗上，后面的「放入」全落到同一张空卡上 —— 必须先关掉再继续。
    两种弹窗的「取消」位置不同（详情弹窗 y≈762，提示弹窗 y≈858），分开认。
    """
    lines = _scan(context)
    if _dialog_open(lines):
        _click_text(context, lines, ROI_DIALOG_CANCEL, (CANCEL_TEXT,), DIALOG_CANCEL_POINT)
        time.sleep(0.8)
        return
    if _popup_open(lines) or _pick(lines, ROI_POPUP_CANCEL, CANCEL_TEXT) is not None:
        _click_text(context, lines, ROI_POPUP_CANCEL, (CANCEL_TEXT,), POPUP_CANCEL_POINT)
        time.sleep(0.6)


def _place_gems(context: Context, level: int, need: int) -> int:
    """把 need 颗 level 级宝石放进材料格，返回实际放进的数量。

    卡位被用掉后格子上的数量**不会立刻减少**（实测：放入 1 颗后仍显示原数量，只有合成完才刷新），
    所以每放一颗都用格子亮度复核一次，没涨就再等 0.8s 复核一遍（弹窗/格子动画没走完会误判）。

    「哪张卡不能用」按坐标记失败次数（同一坐标连续失败 2 次就跳过），但**卡位列表真的变了才清零**：
    某张卡存货清零后会从格子里消失、后面的卡整体前移，死记坐标会把顶上来的好卡一起误杀；
    反过来，列表没变就清零会让同一张坏卡被无限重试（实测踩过，一口气白等 70 秒）。
    连续失败超过 ``_MAX_PLACE_FAILS`` 次直接放弃本等级，避免拖死整轮。
    """
    placed = _count_filled(_screencap(context))
    if placed:
        logger.debug("宝石合成：材料格里已经有 {} 颗，先接着用。", placed)
    strikes: dict[tuple[int, int], int] = {}
    cards: list[_Card] = []
    signature: tuple[tuple[int, tuple[int, int]], ...] | None = None
    need_scan = True
    extra_scans = 0
    fails = 0
    for _ in range(need * 4 + _MAX_PLACE_FAILS + 4):
        if placed >= need:
            break
        if is_stopping(context):
            return placed
        if need_scan:
            _dismiss_popup(context)
            cards = _scan_cards(context)
            current_signature = tuple((card.level, card.point) for card in cards)
            if current_signature != signature:
                signature = current_signature
                strikes.clear()
            need_scan = False
        candidates = [card for card in cards if card.level == level and strikes.get(card.point, 0) < 2]
        if not candidates:
            if placed and extra_scans < 1:
                # 放进去几颗之后突然一张都扫不到，多半是 OCR 抽风，再扫一遍确认。
                extra_scans += 1
                need_scan = True
                continue
            break
        card = candidates[0]
        _click(context, card.point)
        time.sleep(PLACE_DELAY)
        lines = _scan(context)
        popup_level = _card_popup_level(lines)
        kind = _popup_right_kind(lines)
        if popup_level != level or kind != PUT_IN_TEXT:
            logger.debug(
                "宝石合成：Lv.{} 卡位 {} 放不进去（弹窗等级={} 按钮={}），换下一格。",
                level,
                card.point,
                popup_level,
                kind,
            )
            strikes[card.point] = strikes.get(card.point, 0) + 1
            if _popup_open(lines):
                _click_text(context, lines, ROI_POPUP_CANCEL, (CANCEL_TEXT,), POPUP_CANCEL_POINT)
                time.sleep(0.6)
            need_scan = True
            fails += 1
        else:
            _click_text(context, lines, ROI_POPUP_RIGHT, (PUT_IN_TEXT,), POPUP_PUTIN_POINT)
            time.sleep(0.8)
            current = _count_filled(_screencap(context))
            if current <= placed:
                time.sleep(0.8)
                current = _count_filled(_screencap(context))
            if current > placed:
                placed = current
                extra_scans = 0
                logger.debug("宝石合成：Lv.{} 已放入 {}/{} 颗。", level, placed, need)
            else:
                strikes[card.point] = strikes.get(card.point, 0) + 1
                logger.debug("宝石合成：Lv.{} 卡位 {} 没放进去（数量没涨），换下一格。", level, card.point)
                need_scan = True
                fails += 1
        if fails >= _MAX_PLACE_FAILS:
            logger.warning("宝石合成：Lv.{} 连续 {} 次放不进宝石，放弃本等级。", level, fails)
            break
    return placed


def _clear_slots(context: Context) -> None:
    """把材料格里放进去的宝石「取下」（不消耗），用于放弃当前等级。"""
    for _ in range(2):
        if _count_filled(_screencap(context)) == 0:
            return
        _dismiss_popup(context)
        for point in SLOT_POINTS:
            if is_stopping(context):
                return
            _click(context, point)
            time.sleep(0.7)
            lines = _scan(context)
            if _pick(lines, ROI_POPUP_RIGHT, REMOVE_TEXT) is not None:
                _click_text(context, lines, ROI_POPUP_RIGHT, (REMOVE_TEXT,), POPUP_PUTIN_POINT)
                time.sleep(0.6)
            elif _pick(lines, ROI_POPUP_CANCEL, CANCEL_TEXT) is not None:
                _click_text(context, lines, ROI_POPUP_CANCEL, (CANCEL_TEXT,), POPUP_CANCEL_POINT)
                time.sleep(0.5)
    if _count_filled(_screencap(context)) != 0:
        logger.warning("宝石合成：材料格没能清空，后面的等级可能受影响。")


def _synthesize(context: Context, need: int) -> str:
    """点「合成」并处理确认弹窗 / 领奖遮屏。

    返回 ``"success"`` / ``"fail"``（材料已被消耗但没出宝石）/ ``"stopped"`` / ``"timeout"``。
    """
    expected = RATE_THREE if need == 3 else RATE_TWO
    image, lines = _snapshot(context)
    rate = _read_rate(lines)
    filled = _count_filled(image)
    if rate != expected or filled != need:
        logger.warning(
            "宝石合成：成功率 {} / 已放入 {} 颗（期望 {}%，{} 颗），不点「合成」。", rate, filled, expected, need
        )
        return "timeout"

    _click_text(context, lines, ROI_SYNTH_BUTTON, (SYNTH_TEXT,), SYNTH_BUTTON_POINT)
    time.sleep(1.0)

    deadline = time.time() + 40
    confirmed = 0
    closed = False
    hint_misses = 0
    last_action = time.time()
    while time.time() < deadline:
        if is_stopping(context):
            logger.info("宝石合成：收到停止请求，中断合成等待。")
            return "stopped"
        image, lines = _snapshot(context)
        if _pick(lines, ROI_DIALOG_CONFIRM, CONFIRM_TEXT) is not None:
            # 「是否消耗N颗M级宝石进行合成?」/「放入不同属性的宝石…是否继续?」都点「确定」。
            _click_text(context, lines, ROI_DIALOG_CONFIRM, (CONFIRM_TEXT,), DIALOG_CONFIRM_POINT)
            confirmed += 1
            hint_misses = 0
            last_action = time.time()
            time.sleep(1.2)
            continue
        if _pick(lines, ROI_REWARD_HINT, *CLOSE_TEXTS) is not None:
            _click_text(context, lines, ROI_REWARD_HINT, CLOSE_TEXTS, REWARD_CLOSE_POINT)
            closed = True
            hint_misses = 0
            last_action = time.time()
            time.sleep(1.0)
            continue
        if _pick(lines, ROI_REWARD_TITLE, REWARD_TITLE_TEXT) is not None:
            # 「恭喜获得」已出来但关闭提示还没渲染完，等下一帧再点；连续认不到就点空白兜底。
            hint_misses += 1
            if hint_misses >= 3:
                logger.info("宝石合成：领奖遮屏没认到关闭提示，点空白处关闭。")
                _click(context, REWARD_CLOSE_POINT)
                closed = True
                last_action = time.time()
                hint_misses = 0
            time.sleep(0.8)
            continue
        if confirmed and _count_filled(image) == 0 and time.time() - last_action >= 2.0:
            # 三格已清空：成功（关过领奖遮屏）或失败（游戏不弹窗，直接复位）。
            if closed:
                return "success"
            # 「失败」只有在成功率文字重新可见时才敢下结论：领奖遮屏挡着时成功率是认不到的，
            # 那多半是遮屏还没渲染完，继续等，别把它误判成失败。
            if _read_rate(lines) is not None:
                return "fail"
        time.sleep(POLL_INTERVAL)
    logger.warning("宝石合成：点「合成」后 40 秒没等到确认/结果，停止本等级。")
    return "timeout"


@AgentServer.custom_action("GemSynthesis")
class GemSynthesis(CustomAction):
    """按客户端选项把指定等级的宝石从低到高合成。

    设置来源（pipeline_override → 载体节点 enabled，见 ``GemSynthesis.json``）：

    - ``GemSynth_Flag_ModeThree`` / ``GemSynth_Flag_ModeTwo``：三合一（100%）/ 二合一（75%）
    - ``GemSynth_Flag_Lv1`` … ``GemSynth_Flag_Lv8``：要作为材料参与合成的宝石等级（多选；
      客户端目前只暴露 Lv.1-7）

    ⚠️ 每一轮都会真实消耗宝石；某个等级的宝石不够一轮（2 颗 / 3 颗）时跳过该等级。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            parse_params(argv.custom_action_param)
        except ValueError as error:
            logger.error(f"宝石合成：参数解析失败（{error}）。")
            return CustomAction.RunResult(success=False)

        need, levels = _read_settings(context)
        if not levels:
            logger.warning("宝石合成：一个等级都没勾选，本次不做任何点击。")
            return CustomAction.RunResult(success=False)
        logger.info(
            "宝石合成：模式=「{}」（每轮 {} 颗），等级={}。",
            "三合一" if need == 3 else "二合一",
            need,
            levels,
        )

        if not _goto_gem_page(context):
            if is_stopping(context):
                logger.info("宝石合成：收到停止请求，已中断。")
            return CustomAction.RunResult(success=False)

        # 进入合成页后第一时间把排序切到「等级 · 从低到高」：
        # 合成是按等级从低到高逐级做的，低等级排到列表前部能减少每轮的卡位查找开销。
        _ensure_sort_ascending(context)

        expected = RATE_THREE if need == 3 else RATE_TWO
        total_done = 0
        failed = 0
        for level in levels:
            if is_stopping(context):
                break
            rounds = 0
            while rounds < MAX_ROUNDS_PER_LEVEL:
                if is_stopping(context):
                    break
                placed = _place_gems(context, level, need)
                if placed == 0:
                    logger.info("宝石合成：Lv.{} 页面上已经没有可用的宝石，跳过。", level)
                    break
                lines = _scan(context)
                rate = _read_rate(lines)
                if placed < need or rate != expected:
                    logger.info(
                        "宝石合成：Lv.{} 宝石不够一轮（放入 {} 颗 / 需要 {} 颗，成功率 {}），取下后跳过。",
                        level,
                        placed,
                        need,
                        rate,
                    )
                    _clear_slots(context)
                    break
                outcome = _synthesize(context, need)
                if outcome == "success":
                    total_done += 1
                    rounds += 1
                    logger.info("宝石合成：Lv.{} 第 {} 轮合成成功。", level, rounds)
                    continue
                if outcome == "fail":
                    total_done += 1
                    failed += 1
                    rounds += 1
                    logger.info("宝石合成：Lv.{} 第 {} 轮合成失败（{} 颗材料已消耗）。", level, rounds, need)
                    continue
                if outcome == "stopped":
                    logger.info("宝石合成：收到停止请求，已中断（本次完成 {} 轮）。", total_done)
                    return CustomAction.RunResult(success=False)
                logger.warning("宝石合成：Lv.{} 合成流程卡住，停止本等级。", level)
                break
            if rounds >= MAX_ROUNDS_PER_LEVEL:
                logger.warning("宝石合成：Lv.{} 达到轮数上限 {}，停止本等级。", level, MAX_ROUNDS_PER_LEVEL)

        if is_stopping(context):
            logger.info("宝石合成：收到停止请求，本次完成 {} 轮，不再回主页。", total_done)
            return CustomAction.RunResult(success=False)

        logger.info("宝石合成：本次共完成 {} 轮（其中失败 {} 轮），准备返回主页。", total_done, failed)
        _return_home(context)
        return CustomAction.RunResult(success=total_done > 0)
