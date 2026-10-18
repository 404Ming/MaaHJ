from __future__ import annotations

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
from utils.params import parse_params

# 玩法挑战里带 max_hit 的节点：MaaFramework 的 max_hit 计数跨 run_task 累加。
# 日常任务会在同一次任务里连续调用两次 GameplayChallenge_Start（史莱姆宝库、火元素试炼），
# 所以每次调用前都要复位，否则第二次可能因为 SwipeList / 兜底节点命中次数用尽而空转。
_RESET_NODES = (
    "GameplayChallenge_SwipeList",
    "GameplayChallenge_UnknownFallback",
    "GameplayChallenge_BattleTurn",
    "GameplayChallenge_SettleWait",
    "GameplayChallenge_ResultNextStage",
    "GameplayChallenge_ResultTeamContinue",
    "GameplayChallenge_ResultConfirm",
    "GameplayChallenge_FriendCancel",
)


@AgentServer.custom_action("ReturnHome")
@AgentServer.custom_action("DailyReturnHome")
class ReturnHome(CustomAction):
    """把角色送回主页（惑星之间）。

    单独做成 CustomAction 而不是在 Pipeline 里用 `[JumpBack]BackToMainHub_Start`：
    后者回到父节点后会从 next 列表头部重新识别，而 BackToMainHub_Start 在主页上永远命中，
    会形成「回主页 → 再回主页」的死循环。这里用一次 run_task 完成收尾。

    同时注册 `DailyReturnHome` 别名，兼容每日任务旧节点。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        # 日常任务会在一次运行里回主页两次（史莱姆、火元素各一次），
        # BackToMainHub_Back 的 max_hit=5 会跨 run_task 累加，这里先复位保证第二次也有完整返回层级。
        context.clear_hit_count("BackToMainHub_Back")
        detail = context.run_task("BackToMainHub_Start")
        if detail is None or detail.status.failed:
            logger.warning("DailyReturnHome: 返回主页失败。")
            return CustomAction.RunResult(success=False)
        return CustomAction.RunResult(success=True)


@AgentServer.custom_action("DailyChallengeGameplay")
class DailyChallengeGameplay(CustomAction):
    """调用「玩法挑战」打指定副本若干次，结束后由玩法挑战自己回到主页。

    Examples:
        `custom_action_param`::

            {
                "dungeon": "史莱姆宝库",
                "difficulty": "深渊",
                "times": 1
            }

    - `dungeon` 必选，覆盖 `GameplayChallenge_DungeonEntry` 的 expected。
    - `difficulty` 可选，默认「深渊」，覆盖 `GameplayChallenge_Difficulty` 的 expected。
    - `times` 可选，默认 1，覆盖 `GameplayChallenge_Start` 的挑战次数。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            params = parse_params(argv.custom_action_param, "dungeon")
            dungeon = params["dungeon"]
            if not isinstance(dungeon, str) or not dungeon:
                raise ValueError("dungeon must be a non-empty string")
            difficulty = params.get("difficulty", "深渊")
            if not isinstance(difficulty, str) or not difficulty:
                raise ValueError("difficulty must be a non-empty string")
            times = int(params.get("times", 1))
            if times < 1:
                raise ValueError("times must be >= 1")
        except (ValueError, KeyError, TypeError) as error:
            logger.error("DailyChallengeGameplay: 参数解析失败: {}", error)
            return CustomAction.RunResult(success=False)

        for node_name in _RESET_NODES:
            context.clear_hit_count(node_name)

        override = {
            "GameplayChallenge_DungeonEntry": {
                "recognition": {"param": {"expected": [dungeon]}},
            },
            "GameplayChallenge_Difficulty": {
                "recognition": {"param": {"expected": [difficulty]}},
            },
            "GameplayChallenge_Start": {
                "action": {
                    "param": {
                        "custom_action_param": {"times": times},
                    }
                },
            },
        }

        logger.info(
            "日常任务：调用玩法挑战，副本={}，难度={}，次数={}。",
            dungeon,
            difficulty,
            times,
        )
        detail = context.run_task("GameplayChallenge_Start", override)

        if detail is None or detail.status.failed:
            logger.error("日常任务：玩法挑战失败（副本={}），尝试回到主页。", dungeon)
            # 尽量把画面恢复到主页，避免后续日常流程停在副本详情 / 战斗页。
            context.run_task("BackToMainHub_Start")
            return CustomAction.RunResult(success=False)

        logger.info("日常任务：玩法挑战完成（副本={}）。", dungeon)
        return CustomAction.RunResult(success=True)
