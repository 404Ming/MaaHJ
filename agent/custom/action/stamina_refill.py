from __future__ import annotations

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
from utils.params import parse_params

DEFAULT_REASON = "补充体力失败：无法继续消耗体力药，已中断所有运行任务。"


@AgentServer.custom_action("StaminaRefillAbort")
class StaminaRefillAbort(CustomAction):
    """中断所有运行任务，并把「补充体力」的失败原因写进日志。

    补充体力流程本身由 Pipeline 状态机承载（`resource/base/pipeline/StaminaRefill/StaminaRefill.json`），
    Python 侧只保留这一个终止动作：状态机判定无法继续补充时调用它，统一停掉所有任务并留下可排查的原因。

    Examples:
        `custom_action_param`::

            {
                "reason": "补充体力失败：豪华/标准/小型体力补给的「使用」按钮均不可用……"
            }
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            params = parse_params(argv.custom_action_param)
        except ValueError as error:
            logger.error("StaminaRefillAbort: %s", error)
            params = {}

        reason = params.get("reason")
        if not isinstance(reason, str) or not reason:
            reason = DEFAULT_REASON

        logger.error("{}", reason)
        context.tasker.post_stop()
        return CustomAction.RunResult(success=False)
