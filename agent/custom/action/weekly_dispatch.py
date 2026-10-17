from __future__ import annotations

import re

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger

# 已知周常任务 -> (匹配正则, 类型, 副本名或 None)
# 风/火/水/土 各自对应自己的元素试炼副本；累计消耗体力走史莱姆宝库；抽卡走祈愿流程。
KNOWN_TASKS = [
    (r"风属性副本挑战", "dungeon", "风·元素试炼"),
    (r"火属性副本挑战", "dungeon", "火·元素试炼"),
    (r"水属性副本挑战", "dungeon", "水·元素试炼"),
    (r"土属性副本挑战", "dungeon", "土·元素试炼"),
    (r"消耗体力", "dungeon", "史莱姆宝库"),
    (r"抽卡", "gacha", None),
]


def _ocr_boxes(context: Context):
    img = context.tasker.controller.post_screencap().wait().get(True)
    detail = context.run_recognition(
        "Weekly_OCR",
        img,
        {
            "Weekly_OCR": {
                "recognition": {"type": "OCR", "param": {}},
                "action": {"type": "DoNothing"},
            }
        },
    )
    return [(r.text.strip(), r.box) for r in detail.all_results]


@AgentServer.custom_action("WeeklyDispatch")
class WeeklyDispatch(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        pool = (argv.custom_action_param or {}).get("pool", "友情祈愿")

        try:
            boxes = _ocr_boxes(context)
        except Exception as exc:  # noqa: BLE001
            logger.error("WeeklyDispatch: OCR 失败: %s", exc)
            return CustomAction.RunResult(success=True)

        # 只处理带「前往」的未完成行：收集所有「前往」按钮的纵向中心，定位其所在行。
        go_centers = [b.y + b.h / 2 for t, b in boxes if t == "前往"]

        chosen = None
        for gy in go_centers:
            # 同一行、左侧（x<300）的任务标题
            titles = [t for t, b in boxes if b.x < 300 and abs((b.y + b.h / 2) - gy) <= 60]
            if not titles:
                continue
            row_text = " ".join(titles)
            for pattern, kind, dungeon in KNOWN_TASKS:
                if re.search(pattern, row_text):
                    # 同一行右侧读取进度 x/Y，算剩余次数（避免重复挑战已完成的任务）
                    times = None
                    for t, b in boxes:
                        if re.fullmatch(r"\d+/\d+", t) and b.x > 300 and abs((b.y + b.h / 2) - gy) <= 60:
                            try:
                                cur, tot = map(int, t.split("/"))
                            except ValueError:
                                continue
                            if dungeon == "史莱姆宝库":
                                # 挑战次数固定 15；进度可能是 15 或 1500（体力点），按分母判断
                                times = 15 if tot != 15 else max(1, 15 - cur)
                            else:
                                times = max(1, (tot if tot == 5 else 5) - cur)
                            break
                    if kind == "gacha":
                        chosen = ("gacha", None, 1)
                    else:
                        if times is None:
                            times = 15 if dungeon == "史莱姆宝库" else 5
                        chosen = ("dungeon", dungeon, times)
                    break
            if chosen:
                break

        if not chosen:
            # 没有已知可完成的任务：禁用自身，避免下一轮 [JumpBack]/next 死循环，落到 Weekly_Finish 收尾。
            logger.info("WeeklyDispatch: 没有已知可完成的周常任务，流程结束。")
            context.override_pipeline({"Weekly_Dispatch": {"enabled": False}})
            return CustomAction.RunResult(success=True)

        kind, dungeon, times = chosen
        logger.info("WeeklyDispatch: 派发任务 %s（副本=%s, 次数=%s, 祈愿池=%s）", kind, dungeon, times, pool)

        # 当前在周常页，先回主页，再启动子流程（子流程本身从主页出发、结束于主页）。
        context.run_task("Weekly_ReturnHome")

        if kind == "gacha":
            detail = context.run_task(
                "Wish_Start",
                {"Wish_SelectPool": {"recognition": {"param": {"expected": [pool]}}}},
            )
        else:
            override = {
                "GameplayChallenge_DungeonEntry": {
                    "recognition": {"param": {"expected": [dungeon]}}
                },
                "GameplayChallenge_Difficulty": {
                    "recognition": {"param": {"expected": ["深渊"]}}
                },
                "GameplayChallenge_Start": {
                    "action": {"param": {"custom_action_param": {"times": times}}}
                },
            }
            detail = context.run_task("GameplayChallenge_Start", override)

        if detail is not None and detail.status.failed:
            logger.error("WeeklyDispatch: 子流程执行失败，停止周常流程避免死循环。")
            context.override_pipeline({"Weekly_Dispatch": {"enabled": False}})

        return CustomAction.RunResult(success=True)
