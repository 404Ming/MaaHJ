from __future__ import annotations

import re
from typing import Any, cast

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_recognition import CustomRecognition
from maa.define import RectType
from utils import logger
from utils.maa_types import is_hit
from utils.params import parse_params

Box = tuple[int, int, int, int]


def _as_box(box: Any) -> Box:
    """OCR 结果的 box 可能是 [x, y, w, h] 列表，也可能是 Rect，统一成四元组。"""
    if isinstance(box, (list, tuple)):
        return (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
    return (int(box.x), int(box.y), int(box.w), int(box.h))


@AgentServer.custom_recognition("DailyTrophyGate")
class DailyTrophyGate(CustomRecognition):
    """只有「火元素试炼」挑战节点已经命中过，才允许进入奖杯结算。

    日常任务列表里史莱姆宝库和元素试炼的先后顺序会变，但奖杯检查必须等两个挑战都尝试过；
    否则第一次挑战领奖后奖杯就可能已经 >= 30，导致元素试炼被提前跳过。
    如果元素试炼任务本身已完成/不存在，则走 Daily_NoMoreTask 的兜底结算。
    """

    FIRE_CHALLENGE_NODE = "Daily_Challenge_Fire"

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> CustomRecognition.AnalyzeResult | None:
        if context.get_hit_count(self.FIRE_CHALLENGE_NODE) < 1:
            logger.debug("DailyTrophyGate: 火元素试炼尚未挑战，跳过奖杯结算。")
            return None
        return CustomRecognition.AnalyzeResult(
            box=cast(RectType, [0, 0, 1, 1]),
            detail={"reason": "fire challenge done"},
        )


@AgentServer.custom_recognition("DailyTaskRowGo")
class DailyTaskRowGo(CustomRecognition):
    """点击「每日任务」列表中指定任务所在行的「前往」按钮。

    任务列表会随完成状态重排，因此不能固定取第 N 个「前往」。
    本识别器先按标题正则定位目标任务的行，再在该行纵向范围内查找右侧操作列的「前往」。

    参数（JSON 字符串）：
        title: str        标题匹配正则，必填。例：".*史莱姆宝库.*"
        title_roi: list   搜索标题的 ROI，默认 [100, 440, 360, 640]
        go_roi: list      搜索「前往」按钮的 ROI，默认 [480, 440, 180, 640]
        row_extend: int   标题下沿再向下扩展的像素，默认 110
    """

    DEFAULT_TITLE_ROI: tuple[int, int, int, int] = (100, 440, 360, 640)
    DEFAULT_GO_ROI: tuple[int, int, int, int] = (480, 440, 180, 640)
    DEFAULT_ROW_EXTEND: int = 110

    def _ocr(
        self, context: Context, image: Any, name: str, roi: tuple[int, int, int, int]
    ) -> list[tuple[str, Box]]:
        detail = context.run_recognition(
            name,
            image,
            {
                name: {
                    "recognition": {"type": "OCR", "param": {"roi": list(roi)}},
                    "action": {"type": "DoNothing"},
                }
            },
        )
        if not is_hit(detail):
            return []
        return [(r.text.strip(), _as_box(r.box)) for r in detail.all_results or []]

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> CustomRecognition.AnalyzeResult | None:
        try:
            params = parse_params(argv.custom_recognition_param, "title")
            title_pattern: str = params["title"]
            title_roi: tuple[int, int, int, int] = tuple(params.get("title_roi", self.DEFAULT_TITLE_ROI))  # type: ignore[assignment]
            go_roi: tuple[int, int, int, int] = tuple(params.get("go_roi", self.DEFAULT_GO_ROI))  # type: ignore[assignment]
            row_extend: int = int(params.get("row_extend", self.DEFAULT_ROW_EXTEND))
        except (ValueError, KeyError) as error:
            logger.error("DailyTaskRowGo: 参数解析失败: {}", error)
            return None

        try:
            title_regex = re.compile(title_pattern)
        except re.error as error:
            logger.error("DailyTaskRowGo: 标题正则无效 {!r}: {}", title_pattern, error)
            return None

        try:
            title_items = self._ocr(context, argv.image, "DailyTaskRowGo_Title", title_roi)
            go_items = self._ocr(context, argv.image, "DailyTaskRowGo_Button", go_roi)
        except Exception as exc:  # noqa: BLE001
            logger.error("DailyTaskRowGo: OCR 失败: {}", exc)
            return None

        # 找出所有匹配标题的行；按 y 排序，优先最上方（与列表视觉顺序一致）
        matched_titles = [(text, box) for text, box in title_items if title_regex.search(text)]
        if not matched_titles:
            logger.debug("DailyTaskRowGo: 未找到标题匹配 {!r}", title_pattern)
            return None
        matched_titles.sort(key=lambda item: item[1][1])
        _, title_box = matched_titles[0]

        # 行纵向范围：标题上沿 ~ 标题下沿 + row_extend
        _, title_y, _, title_h = title_box
        row_top = title_y
        row_bottom = title_y + title_h + row_extend

        # 在 go 列中找同一行的「前往」
        for text, box in go_items:
            if text != "前往":
                continue
            center_y = box[1] + box[3] / 2
            if row_top <= center_y <= row_bottom:
                logger.info(
                    "DailyTaskRowGo: 命中 {!r}, 行范围 [{}, {}], 前往 box={}",
                    title_pattern,
                    row_top,
                    row_bottom,
                    list(box),
                )
                return CustomRecognition.AnalyzeResult(
                    box=cast(RectType, box),
                    detail={
                        "title_pattern": title_pattern,
                        "title_box": list(title_box),
                        "go_box": list(box),
                    },
                )

        logger.debug(
            "DailyTaskRowGo: 找到标题 {!r} 但未找到同行「前往」(行范围 [{}, {}])",
            title_pattern,
            row_top,
            row_bottom,
        )
        return None
