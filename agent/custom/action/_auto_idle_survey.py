"""临时调查动作（用完即删）：只读读出每个副本弹窗里的难度行，绝不点外层「确定」。"""

from __future__ import annotations

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger

from .auto_idle import (
    _CARD_X,
    _card_rows,
    _confirm_modal,
    _ensure_expanded,
    _enter_dialog,
    _open_card,
    box_top,
)
from .auto_idle import DUNGEONS as _DUNGEONS
from .auto_idle import SERIES as _SERIES


@AgentServer.custom_action("AutoIdleSurvey")
class AutoIdleSurvey(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        if not _enter_dialog(context):
            logger.error("调查：打不开弹窗")
            return CustomAction.RunResult(success=False)
        logger.info("调查：弹窗已打开")
        for series_name, series_key, _items in _SERIES:
            banner = _ensure_expanded(context, series_key)
            if banner is None:
                logger.warning("调查：### {} 展开失败", series_name)
                continue
            top = int(box_top(banner))
            rows = _card_rows(context, top)
            logger.info("调查：### {} banner_top={} rows={}", series_name, top, rows)
            if not rows:
                continue
            for y in rows:
                for x in _CARD_X:
                    opened = _open_card(context, x, y)
                    if opened is None:
                        logger.warning("调查：    ({},{}) 没弹窗", x, y)
                        continue
                    title, modal_rows = opened
                    logger.info(
                        "调查：    ({},{}) title={!r} rows={}",
                        x,
                        y,
                        title,
                        [t for t, _b in modal_rows],
                    )
                    _confirm_modal(context)
        return CustomAction.RunResult(success=True)
