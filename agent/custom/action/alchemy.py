"""快速炼金：主页 → 菜单 → 熔合炉 → 炼金，按「炼金次数」循环加料并炼金。

实机 720x1280 逐跳印证（2026-10-20，OCR box 均为真机截图实测）：

  主页「菜单」            (637,1232,55,32)
  → 菜单页「熔合炉」       (322,836,76,33)
  → 合成页右下「炼金」     (639,1245,51,31)
  → 炼金页瓶身中央「+」    (380,710)（纯图标，无可 OCR 文案）
  → 「选择材料」弹窗       (37,313,129,41)
      · 排序按钮「炼金能量」 (415,324,107,32) —— 切到「道具数量」后同一位置变成 (415,322,106,34)
      · 下拉里的「道具数量」 (457,442,77,24)
      · 「一键选择」         (318,350,69,28)（图标压住首字，OCR 常见「键选择」）
      · 「确定」             (502,916,60,36)
      · 「取消」             (176,917,56,32)
  → 回炼金页后底部由「请往瓶中添加原料」(267,1078,229,32) 变成「开始炼金」(320,1074,120,38)
  → 点「开始炼金」→「点击空白…关闭」领奖弹窗 → 瓶子清空、底部复现「请往瓶中添加原料」，即下一轮起点。

为什么做成一个 CustomAction：
  · 一轮里有 4 个弹窗/页面状态要按运行时画面判定（炼金页 / 选择材料弹窗 / 已装料 / 领奖弹窗），
    静态 next 链表达不了「排序已是道具数量就跳过」这种分支；
  · 次数来自客户端选项，循环与「没有材料就提前收工」都在运行时判定；
  · 「一键选择」的首字被图标压住，OCR 只能认到「键选择」，需要做包含匹配 + 兜底落点。

⚠️ 会真实消耗材料：每次「开始炼金」都会把一键选择挑中的材料换掉，次数由用户设置。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
from utils.interrupt import is_stopping
from utils.maa_types import ocr_results
from utils.params import parse_params

# CustomAction 内做 OCR 用的探针节点（定义见 Alchemy.json）。
OCR_NODE = "Alchemy_OcrProbe"
FULL_ROI = (0, 0, 720, 1280)

# ---- 页面/控件文案 -----------------------------------------------------------
HUB_MENU_TEXT = "菜单"
FURNACE_TEXT = "熔合炉"
SYNTH_TITLE_TEXT = "合成"
ALCHEMY_TITLE_TEXT = "炼金"
DIALOG_TITLE_TEXT = "选择材料"
SORT_ENERGY_TEXT = "炼金能量"
SORT_QUANTITY_TEXT = "道具数量"
EMPTY_BAR_TEXT = "请往瓶中添加原料"
START_TEXT = "开始炼金"
# 「一键选择」的首字被勾选图标压住：实测 OCR 只会给出「键选择」。
AUTOSELECT_TEXTS = ("一键选择", "键选择", "选 择")
CANCEL_TEXT = "取消"
CONFIRM_TEXT = "确定"
# 领奖弹窗：标题「恭喜获得」+ 关闭提示「点击空白区域关闭」（均为真机实测文案）。
POPUP_TEXTS = ("恭喜获得",)
CLOSE_TEXTS = ("点击空白区域关闭", "点击空白", "点击空白处关闭")

# ---- 识别 ROI（只框到该控件，避免同名文案串页）--------------------------------
ROI_HUB_MENU = (600, 1190, 120, 90)  # 主页右下角「菜单」
ROI_MENU_FURNACE = (280, 800, 160, 80)  # 菜单页第二行「熔合炉」
ROI_SYNTH_TITLE = (0, 0, 220, 90)  # 合成页页内标题「合成」
ROI_BOTTOM_NAV = (590, 1210, 130, 70)  # 合成/炼金页右下角页签「炼金」
ROI_ALCHEMY_TITLE = (200, 0, 320, 110)  # 炼金页页内标题「炼金」
ROI_ALCHEMY_BAR = (240, 1040, 250, 80)  # 底部状态条：请往瓶中添加原料 / 开始炼金
ROI_DIALOG_TITLE = (10, 295, 240, 70)  # 「选择材料」弹窗标题
ROI_SORT_BUTTON = (395, 305, 150, 60)  # 排序按钮（文案 炼金能量 / 道具数量）
ROI_SORT_DROPDOWN = (440, 375, 120, 100)  # 展开后的两项：炼金能量 / 道具数量
ROI_AUTOSELECT = (285, 330, 130, 60)  # 「一键选择」
ROI_DIALOG_CANCEL = (140, 890, 120, 80)  # 弹窗左下「取消」
ROI_DIALOG_CONFIRM = (470, 890, 130, 80)  # 弹窗右下「确定」

# ---- 兜底落点（控件本身没有可 OCR 的完整文案时用；均在上述 ROI 内）------------
FLASK_PLUS = (380, 710)  # 炼金页瓶身中央的「+」
SORT_POINT = (470, 340)
QUANTITY_OPTION_POINT = (495, 454)
AUTOSELECT_POINT = (355, 362)
CONFIRM_POINT = (532, 934)
CANCEL_POINT = (204, 933)
START_POINT = (380, 1093)
# 认不到「点击空白…」时轮流点这两处空白（弹窗是「点空白关闭」型）。
POPUP_FALLBACK_POINTS = ((370, 1215), (360, 1000))

MIN_TIMES = 1
MAX_TIMES = 999
POLL_INTERVAL = 0.6


@dataclass(frozen=True)
class _Line:
    """一次 OCR 出来的一行文字。"""

    text: str
    x: int
    y: int
    w: int
    h: int

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2


def _rect(box: Any) -> tuple[int, int, int, int]:
    if box is None:
        return (0, 0, 0, 0)
    if isinstance(box, (list, tuple)):
        return (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
    return (int(box.x), int(box.y), int(box.w), int(box.h))


def _screencap(context: Context) -> Any:
    return context.tasker.controller.post_screencap().wait().get()


def _ocr(context: Context, image: Any, roi: tuple[int, int, int, int]) -> list[_Line]:
    """用 Alchemy_OcrProbe 节点对指定 ROI 做一次 OCR。"""
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
        lines.append(_Line(text=text, x=x, y=y, w=w, h=h))
    return lines


def _scan(context: Context) -> list[_Line]:
    """整屏 OCR 一次，后续状态判定都在内存里做，避免重复截图。"""
    return _ocr(context, _screencap(context), FULL_ROI)


def _click(context: Context, point: tuple[int, int]) -> None:
    context.tasker.controller.post_click(int(point[0]), int(point[1])).wait()


def _center(line: _Line) -> tuple[int, int]:
    return (line.cx, line.cy)


def _in_roi(line: _Line, roi: tuple[int, int, int, int]) -> bool:
    x, y, w, h = roi
    return x <= line.cx <= x + w and y <= line.cy <= y + h


def _pick(
    lines: list[_Line],
    roi: tuple[int, int, int, int],
    *texts: str,
) -> _Line | None:
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
    _click(context, _center(line) if line is not None else fallback)


def _dialog_open(lines: list[_Line]) -> bool:
    return _pick(lines, ROI_DIALOG_TITLE, DIALOG_TITLE_TEXT) is not None


def _on_hub(lines: list[_Line]) -> bool:
    return _pick(lines, ROI_HUB_MENU, HUB_MENU_TEXT) is not None


def _on_menu(lines: list[_Line]) -> bool:
    return _pick(lines, ROI_MENU_FURNACE, FURNACE_TEXT) is not None


def _on_synth(lines: list[_Line]) -> bool:
    return _pick(lines, ROI_SYNTH_TITLE, SYNTH_TITLE_TEXT) is not None


def _on_alchemy(lines: list[_Line]) -> bool:
    return (
        _pick(lines, ROI_ALCHEMY_TITLE, ALCHEMY_TITLE_TEXT) is not None
        and _pick(lines, ROI_ALCHEMY_BAR, EMPTY_BAR_TEXT, START_TEXT) is not None
    )


def _wait_for(context: Context, predicate: Any, timeout: float) -> bool:
    """轮询整屏 OCR，直到 predicate(lines) 为真、超时或收到停止请求。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_stopping(context):
            return False
        if predicate(_scan(context)):
            return True
        time.sleep(POLL_INTERVAL)
    return False


def _return_home(context: Context) -> None:
    """把角色送回主页（与 DailyReturnHome 同一套做法）。"""
    context.clear_hit_count("BackToMainHub_Back")
    detail = context.run_task("BackToMainHub_Start")
    if detail is None or detail.status.failed:
        logger.warning("快速炼金：返回主页失败，请手动确认当前画面。")


def _goto_alchemy(context: Context) -> bool:
    """从主页/菜单页/合成页/炼金页任一状态走到炼金页。"""
    for _ in range(6):
        if is_stopping(context):
            return False
        lines = _scan(context)
        if _dialog_open(lines):
            # 弹窗卡在半路：先取消，回到炼金页重新开始。
            _click_text(context, lines, ROI_DIALOG_CANCEL, (CANCEL_TEXT,), CANCEL_POINT)
            time.sleep(0.8)
            continue
        if _on_alchemy(lines):
            return True
        if _on_menu(lines):
            _click_text(context, lines, ROI_MENU_FURNACE, (FURNACE_TEXT,), (360, 852))
            time.sleep(1.5)
            continue
        if _on_synth(lines):
            _click_text(context, lines, ROI_BOTTOM_NAV, (ALCHEMY_TITLE_TEXT,), (664, 1260))
            time.sleep(1.5)
            continue
        if _on_hub(lines):
            _click_text(context, lines, ROI_HUB_MENU, (HUB_MENU_TEXT,), (637, 1232))
            time.sleep(1.2)
            continue
        logger.info("快速炼金：当前不在主页/菜单/合成/炼金页，先返回主页再进。")
        _return_home(context)
        time.sleep(1.0)
    logger.error("快速炼金：6 步之内没能走到炼金页，放弃本次任务。")
    return False


def _close_reward_popup(context: Context, timeout: float = 18.0) -> bool:
    """关掉「炼金获得材料」弹窗（实测内容：恭喜获得 / 点击空白区域关闭）。

    判据：点掉弹窗后底部重新出现「请往瓶中添加原料」（瓶子清空）。
    弹窗有入场动画，刚点完「开始炼金」的头一两帧 OCR 几乎读不到内容（实测只剩标题「炼金」），
    所以：
      · 看到「恭喜获得」= 弹窗还在入场，继续等，不去空点；
      · 看到「点击空白区域关闭」= 点它；
      · 连续两帧都只看到「请往瓶中添加原料」才算真的回到炼金页（防动画期间误判）。
    """
    time.sleep(1.6)
    deadline = time.time() + timeout
    fallback = 0
    misses = 0
    empty_hits = 0
    while time.time() < deadline:
        if is_stopping(context):
            logger.info("快速炼金：收到停止请求，中断领奖弹窗处理。")
            return False
        lines = _scan(context)
        if _dialog_open(lines):
            # 弹窗没关掉反而回到了选择材料页：按取消退出，交给上层判定失败。
            _click_text(context, lines, ROI_DIALOG_CANCEL, (CANCEL_TEXT,), CANCEL_POINT)
            return False
        hint = _pick(lines, FULL_ROI, *CLOSE_TEXTS)
        if hint is not None:
            logger.info(f"快速炼金：点「{hint.text}」关闭炼金结果弹窗。")
            _click(context, _center(hint))
            misses = 0
            empty_hits = 0
        elif _pick(lines, FULL_ROI, *POPUP_TEXTS) is not None:
            # 「恭喜获得」已出来但关闭提示还没渲染完，等下一帧再点。
            misses = 0
            empty_hits = 0
        elif _pick(lines, ROI_ALCHEMY_BAR, EMPTY_BAR_TEXT) is not None:
            empty_hits += 1
            if empty_hits >= 2:
                logger.info("快速炼金：瓶子已清空，回到炼金页。")
                return True
        else:
            empty_hits = 0
            misses += 1
            if misses >= 2:
                logger.info("快速炼金：没认到关闭提示，试点空白处。")
                _click(context, POPUP_FALLBACK_POINTS[fallback % len(POPUP_FALLBACK_POINTS)])
                fallback += 1
                misses = 0
        time.sleep(0.9)
    logger.warning("快速炼金：领奖弹窗一直没关掉（底部没恢复「请往瓶中添加原料」）。")
    return False


def _one_round(context: Context, index: int, total: int) -> str:
    """完成一轮「加料 → 一键选择 → 确定 → 开始炼金 → 关弹窗」。

    返回 ``"done"`` / ``"empty"``（没有可选材料）/ ``"fail"`` / ``"stopped"``（用户点了停止）。
    """
    if is_stopping(context):
        return "stopped"
    logger.info(f"快速炼金：第 {index}/{total} 次 —— 打开选择材料。")
    _click(context, FLASK_PLUS)
    if not _wait_for(context, _dialog_open, 8.0):
        if is_stopping(context):
            return "stopped"
        logger.warning("快速炼金：点「+」后没等到「选择材料」弹窗。")
        return "fail"

    # 排序：已经是「道具数量」就不动（用户要求），否则切过去。
    lines = _scan(context)
    if _pick(lines, ROI_SORT_BUTTON, SORT_QUANTITY_TEXT) is None:
        _click_text(context, lines, ROI_SORT_BUTTON, (SORT_ENERGY_TEXT, SORT_QUANTITY_TEXT), SORT_POINT)
        if not _wait_for(
            context,
            lambda scan: _pick(scan, ROI_SORT_DROPDOWN, SORT_QUANTITY_TEXT) is not None,
            5.0,
        ):
            if is_stopping(context):
                return "stopped"
            logger.warning("快速炼金：排序下拉没展开或没认到「道具数量」。")
            return "fail"
        lines = _scan(context)
        _click_text(context, lines, ROI_SORT_DROPDOWN, (SORT_QUANTITY_TEXT,), QUANTITY_OPTION_POINT)
        time.sleep(0.8)
        logger.info("快速炼金：排序已切到「道具数量」（从多到少）。")
    else:
        logger.info("快速炼金：排序已经是「道具数量」，跳过切换。")

    # 一键选择（首字被图标压住，OCR 常只给「键选择」）。
    lines = _scan(context)
    _click_text(context, lines, ROI_AUTOSELECT, AUTOSELECT_TEXTS, AUTOSELECT_POINT)
    time.sleep(1.2)

    # 确定 → 回到炼金页。
    lines = _scan(context)
    _click_text(context, lines, ROI_DIALOG_CONFIRM, (CONFIRM_TEXT,), CONFIRM_POINT)
    if not _wait_for(
        context,
        lambda scan: _pick(scan, ROI_ALCHEMY_BAR, START_TEXT) is not None,
        10.0,
    ):
        if is_stopping(context):
            return "stopped"
        logger.warning("快速炼金：确定之后底部没有出现「开始炼金」，多半是没材料可加，提前收工。")
        return "empty"

    # 用户要求：点「确定」到点「开始炼金」之间留 1 秒，等加料动画/进度条落定再点。
    time.sleep(1.0)

    # 开始炼金 → 关领奖弹窗。
    if is_stopping(context):
        return "stopped"
    lines = _scan(context)
    _click_text(context, lines, ROI_ALCHEMY_BAR, (START_TEXT,), START_POINT)
    if not _close_reward_popup(context):
        if is_stopping(context):
            return "stopped"
        return "fail"

    logger.info(f"快速炼金：第 {index}/{total} 次完成。")
    return "done"


@AgentServer.custom_action("QuickAlchemy")
class QuickAlchemy(CustomAction):
    """按「炼金次数」循环执行快速炼金。

    Examples:
        `custom_action_param`::

            { "times": 1 }

        - ``times``: 由客户端选项「炼金次数」覆写，1-999；每次都会真实消耗材料。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            params = parse_params(argv.custom_action_param)
        except ValueError as error:
            logger.error(f"快速炼金：参数解析失败（{error}）。")
            return CustomAction.RunResult(success=False)

        raw_times = params.get("times", 1)
        try:
            times = int(raw_times)
        except (TypeError, ValueError):
            logger.error(f"快速炼金：times 必须是整数，收到 {raw_times!r}。")
            return CustomAction.RunResult(success=False)
        if times < MIN_TIMES or times > MAX_TIMES:
            logger.error(f"快速炼金：times 必须在 {MIN_TIMES}-{MAX_TIMES} 之间，收到 {times}。")
            return CustomAction.RunResult(success=False)

        if not _goto_alchemy(context):
            if is_stopping(context):
                logger.info("快速炼金：收到停止请求，已中断。")
            return CustomAction.RunResult(success=False)

        done = 0
        stopped = False
        for index in range(1, times + 1):
            outcome = _one_round(context, index, times)
            if outcome == "done":
                done += 1
                continue
            if outcome == "stopped":
                stopped = True
                logger.info(f"快速炼金：收到停止请求，已中断（完成 {done}/{times} 次）。")
                break
            if outcome == "empty":
                logger.warning(f"快速炼金：没有可用材料，已提前结束（完成 {done}/{times} 次）。")
                break
            if is_stopping(context):
                stopped = True
                logger.info(f"快速炼金：收到停止请求，已中断（完成 {done}/{times} 次）。")
                break
            logger.warning(f"快速炼金：第 {index} 次失败，停止循环（完成 {done}/{times} 次）。")
            break

        if stopped:
            # 用户主动停止时不再跑「回主页」，避免和客户端的停止流程抢控制权。
            return CustomAction.RunResult(success=False)

        logger.info(f"快速炼金：本次完成 {done}/{times} 次，准备返回主页。")
        _return_home(context)
        return CustomAction.RunResult(success=done > 0)
