"""战斗 / 结算的高频「单帧多 ROI」识别与点击基元。

实机测量（2026-11-14，MuMu 127.0.0.1:16512，720x1280）：
  · 一次 full screencap 往返：Default 264ms / RawByNetcat 175ms / MinicapDirect 117ms /
    **MinicapStream 16ms** / Encode 517ms；
  · 同一帧多 OCR 一个 ROI 只要 10~40ms（380x110 ≈10ms，720x130 ≈20ms，720x290 ≈41ms）；
  · 一次 post_click 往返 ≈51ms（事件下发前 ≈0ms，wait 返回是 ADB 回包）。

结论：**多判一个文案必须加成到同一帧上，绝不新增一次截图**；命中后直接用这一帧的
识别框点击，不再二次识别。旧版 `_run_battle_turns` 每轮要 2~4 次截图（is_over 里每个
文案各截一帧 + 回合按钮再截一帧），且结算判据 1.5s 才查一次，是延迟的主因。
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from utils import logger
from utils.interrupt import is_stopping

from .gameplay_challenge import _box_center_x, _box_center_y

# 复用项目里已实机验证过的 OCR 探针节点名（pick 助战 / 战斗 / 结算共用同一套）
_OCR_NODE = "PickSupport_OCR"


def screencap(context) -> Any:
    """截一帧（BGR 数组）；失败返回 None。"""
    try:
        return context.tasker.controller.post_screencap().wait().get(True)
    except Exception as error:  # noqa: BLE001
        logger.warning("截图失败：{}", error)
        return None


def ocr_on(context, img: Any, roi: Sequence[float]) -> list[tuple[str, Any]]:
    """在**已经截好的那一帧**上做一次 OCR（不再截图）。"""
    if img is None:
        return []
    pipeline = {
        _OCR_NODE: {
            "recognition": {"type": "OCR", "param": {"roi": list(roi)}},
            "action": {"type": "DoNothing"},
        }
    }
    detail = context.run_recognition(_OCR_NODE, img, pipeline)
    if detail is None:
        return []
    return [(str(result.text or "").strip(), result.box) for result in (detail.all_results or [])]


def index_on(context, img: Any, *rois: Sequence[float]) -> dict[str, Any]:
    """在**同一帧**上 OCR 多个 ROI，合并成 {文本: 框}；无帧返回空 dict。"""
    index: dict[str, Any] = {}
    for roi in rois:
        for text, box in ocr_on(context, img, roi):
            if text:
                index.setdefault(text, box)
    return index


def frame_index(context, *rois: Sequence[float]) -> dict[str, Any]:
    """截一帧并对同一帧 OCR 多个 ROI；截不到帧返回空 dict。"""
    return index_on(context, screencap(context), *rois)


def pick_exact(index: dict[str, Any], *texts: str):
    """按**完全相等**取框（比较时去掉空白，兜住 OCR 抖动）。"""
    for text, box in index.items():
        key = text.replace(" ", "")
        if any(key == wanted.replace(" ", "") for wanted in texts):
            return box
    return None


def pick_contains(index: dict[str, Any], *texts: str):
    """按**包含**取框（带后缀的读数类锚点用）。"""
    for text, box in index.items():
        key = text.replace(" ", "")
        if any(wanted.replace(" ", "") in key for wanted in texts):
            return box
    return None


def click_box(context, box) -> None:
    """点识别框中心（点完不等固定时长，由调用方按帧判断画面是否已切换）。"""
    context.tasker.controller.post_click(_box_center_x(box), _box_center_y(box)).wait()


def click_and_await(
    context,
    box,
    texts: Sequence[str],
    rois: Sequence[Sequence[float]],
    timeout: float = 4.0,
) -> dict[str, Any] | None:
    """点一下，然后**按帧**轮询到这些文案消失或挪位，返回切换瞬间那一帧的 index。

    结算连点用它替代固定 `sleep(1.5)`：按钮一消失就进入下一屏，既快又不会把同一条
    「继续」重复点两次（同文案挪位 = 换了一屏）。
    """
    old_x = _box_center_x(box)
    old_y = _box_center_y(box)
    click_box(context, box)
    deadline = time.time() + timeout
    index: dict[str, Any] | None = None
    while time.time() < deadline:
        if is_stopping(context):
            return None
        img = screencap(context)
        if img is None:
            continue
        index = index_on(context, img, *rois)
        same = pick_exact(index, *texts)
        if same is None:
            return index
        if abs(_box_center_x(same) - old_x) > 12 or abs(_box_center_y(same) - old_y) > 12:
            return index
        time.sleep(0.02)
    return index
