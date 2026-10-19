from __future__ import annotations

import re
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
from utils.params import parse_params

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

# 祈愿池名 -> 识别用正则。
# 池名是美术字，OCR 实测会丢字/串字/繁简混用：
#   「友情祈愿」→ 友倩祈愿 / 友宿祈愿 / 友祈愿(丢字)
#   「冒险祈愿」→ 冒險祈願(繁体)
# 因此统一用「首字 + 0~3 个任意字符 + 末字(含繁简)」：首字是各池唯一的区分点，
# 末端放宽到繁简两种写法，中间允许 OCR 抖动。对应 Wish_VerifyPool / Wish_SelectPool 的 override。
POOL_LAST_CHAR_VARIANTS = {
    "愿": "[愿願]",
    "請": "[请請]",
    "请": "[请請]",
}


def pool_pattern(pool: str) -> str:
    """把池名转成容错正则；单字池名退化为精确匹配。"""
    if len(pool) < 2:
        return re.escape(pool)
    head = re.escape(pool[0])
    tail = POOL_LAST_CHAR_VARIANTS.get(pool[-1], re.escape(pool[-1]))
    return f"{head}.{{0,3}}{tail}"


# 行定位：周常列表里「前往」按钮可能在标题下方约 60px，也可能与标题同行。
# 用按钮中心向上 120px / 向下 25px 作为整行范围，既能覆盖标题+进度，也不会串到相邻行（行距约 155px）。
ROW_UP = 120
ROW_DOWN = 25

# 每次派发前/后需要复位的 max_hit 计数：
# - BackToMainHub_Back：一次任务里会多次回主页
# - Weekly_ClickDailyEntry / Weekly_ClickWeeklyTab：一次任务里会多次重新进入周常页
RESET_BEFORE_RETURN = ("BackToMainHub_Back",)
RESET_BEFORE_REOPEN = ("Weekly_ClickDailyEntry", "Weekly_ClickWeeklyTab")


def _box_xywh(box: Any) -> tuple[float, float, float, float]:
    """OCR 结果的 box 可能是 [x, y, w, h] 列表，也可能是 Rect，统一取值。"""
    if isinstance(box, (list, tuple)):
        return float(box[0]), float(box[1]), float(box[2]), float(box[3])
    return float(box.x), float(box.y), float(box.w), float(box.h)


def _center_y(box: Any) -> float:
    _, y, _, h = _box_xywh(box)
    return y + h / 2


def _left_x(box: Any) -> float:
    x, _, _, _ = _box_xywh(box)
    return x


def _ocr_boxes(context: Context) -> list[tuple[str, Any]]:
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
    if detail is None or not detail.hit:
        return []
    return [(r.text.strip(), r.box) for r in detail.all_results or []]


def _parse_action_params(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    return parse_params(raw)


def _calc_times(boxes: list[tuple[str, Any]], row_top: float, row_bottom: float, dungeon: str) -> int:
    """按同一行右侧的 x/Y 进度算剩余次数。"""
    for text, box in boxes:
        if not re.fullmatch(r"\d+/\d+", text) or _left_x(box) <= 300:
            continue
        if not (row_top <= _center_y(box) <= row_bottom):
            continue
        try:
            cur, tot = map(int, text.split("/"))
        except ValueError:
            continue
        if dungeon == "史莱姆宝库":
            # 史莱姆宝库的进度可能是 15 次，也可能显示体力点（如 0/1500），固定按 15 次处理。
            return 15 if tot != 15 else max(1, 15 - cur)
        return max(1, (tot if tot == 5 else 5) - cur)
    return 15 if dungeon == "史莱姆宝库" else 5


def _choose_task(
    boxes: list[tuple[str, Any]], max_times: int | None
) -> tuple[str, str | None, int] | None:
    """在所有带「前往」的行里，按 KNOWN_TASKS 顺序选第一个可自动完成的任务。"""
    go_boxes = [box for text, box in boxes if text == "前往"]
    # 按列表视觉顺序从上到下处理，保证优先派发靠上的任务。
    go_boxes.sort(key=_center_y)
    for go in go_boxes:
        gy = _center_y(go)
        row_top = gy - ROW_UP
        row_bottom = gy + ROW_DOWN

        # 同一行左侧的任务标题（可能被 OCR 拆成多段，拼起来再匹配）
        row_texts = [
            text for text, box in boxes if _left_x(box) < 300 and row_top <= _center_y(box) <= row_bottom
        ]
        if not row_texts:
            continue
        row_text = " ".join(row_texts)

        for pattern, kind, dungeon in KNOWN_TASKS:
            if not re.search(pattern, row_text):
                continue
            if kind == "gacha":
                return ("gacha", None, 1)
            times = _calc_times(boxes, row_top, row_bottom, dungeon or "")
            if max_times is not None:
                times = min(times, max(1, max_times))
            return (kind, dungeon, times)
    return None


@AgentServer.custom_action("WeeklyDispatch")
class WeeklyDispatch(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            params = _parse_action_params(argv.custom_action_param)
            pool = params.get("pool", "友情祈愿")
            if not isinstance(pool, str) or not pool:
                raise ValueError("pool must be a non-empty string")
            raw_max_times = params.get("max_times")
            max_times = None if raw_max_times is None else int(raw_max_times)
            if max_times is not None and max_times < 1:
                raise ValueError("max_times must be >= 1")
            raw_max_dispatches = params.get("max_dispatches")
            max_dispatches = None if raw_max_dispatches is None else int(raw_max_dispatches)
            if max_dispatches is not None and max_dispatches < 1:
                raise ValueError("max_dispatches must be >= 1")
            dry_run = bool(params.get("dry_run", False))
        except (ValueError, KeyError, TypeError) as error:
            logger.error("WeeklyDispatch: 参数解析失败: {}", error)
            return CustomAction.RunResult(success=False)

        hit_count = context.get_hit_count("Weekly_Dispatch")
        logger.info("WeeklyDispatch: hit_count={} max_dispatches={}", hit_count, max_dispatches)
        if max_dispatches is not None and hit_count > max_dispatches:
            logger.info("WeeklyDispatch: 已达到 max_dispatches={}，结束派发。", max_dispatches)
            context.override_pipeline({"Weekly_Dispatch": {"enabled": False}})
            return CustomAction.RunResult(success=True)

        try:
            boxes = _ocr_boxes(context)
        except Exception as exc:  # noqa: BLE001
            logger.error("WeeklyDispatch: OCR 失败: {}", exc)
            return CustomAction.RunResult(success=True)

        chosen = _choose_task(boxes, max_times)
        if not chosen:
            # 没有已知可完成的任务：禁用自身，避免下一轮 next 死循环，落到 Weekly_Finish 收尾。
            logger.info("WeeklyDispatch: 没有已知可完成的周常任务，流程结束。")
            context.override_pipeline({"Weekly_Dispatch": {"enabled": False}})
            return CustomAction.RunResult(success=True)

        kind, dungeon, times = chosen
        logger.info(
            "WeeklyDispatch: 派发任务 kind={} dungeon={} times={} pool={}",
            kind,
            dungeon,
            times,
            pool,
        )

        if dry_run:
            # 诊断模式：只识别并记录派发结果，不进入玩法挑战/祈愿，不消耗体力或抽卡资源。
            logger.info("WeeklyDispatch: dry_run=True，仅记录派发结果，不执行子流程。")
            context.override_pipeline({"Weekly_Dispatch": {"enabled": False}})
            # 允许后续 Weekly_OpenPage 再进一次周常页，验证重入链路；最后 Weekly_Finish 正常回主页。
            for node_name in (*RESET_BEFORE_RETURN, *RESET_BEFORE_REOPEN):
                context.clear_hit_count(node_name)
            return CustomAction.RunResult(success=True)

        # 当前在周常页，先回主页，再启动子流程（子流程本身从主页出发、结束于主页）。
        for node_name in RESET_BEFORE_RETURN:
            context.clear_hit_count(node_name)
        home_detail = context.run_task("Weekly_ReturnHome")
        if home_detail is None or home_detail.status.failed:
            logger.error("WeeklyDispatch: 回主页失败，停止派发避免从错误页面启动子流程。")
            context.override_pipeline({"Weekly_Dispatch": {"enabled": False}})
            return CustomAction.RunResult(success=True)

        if kind == "gacha":
            # 祈愿页的美术字池名 OCR 会抖（「友情祈愿」常读成「友倩祈愿」/「友祈愿」），
            # 统一用 pool_pattern 放宽；两个节点必须用同一条正则，否则会出现
            # 「选中了但复核不过」的假失败（旧版 冒险祈愿 就是这个原因：OCR 读出繁体「冒險祈願」）。
            pattern = pool_pattern(pool)
            # 泳道扫描节点是 max_hit 计数的：同一次周常里第二次进入祈愿流程时，
            # 上次用尽的额度会让扫池直接空转，所以每次进入前都要复位。
            for node_name in (
                "Wish_SelectPool",
                "Wish_SwipeLaneBack",
                "Wish_SwipeLane",
                "Wish_Draw",
                "Wish_TapReveal",
            ):
                context.clear_hit_count(node_name)
            detail = context.run_task(
                "Wish_Start",
                {
                    "Wish_VerifyPool": {"recognition": {"param": {"expected": [pattern]}}},
                    "Wish_SelectPool": {"recognition": {"param": {"expected": [pattern]}}},
                },
            )
            if detail is None or detail.status.failed:
                logger.error("WeeklyDispatch: 祈愿池 {} 未能选中/确认（pattern={}），跳过抽卡。", pool, pattern)
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
            for node_name in (
                "GameplayChallenge_SwipeList",
                "GameplayChallenge_UnknownFallback",
                "GameplayChallenge_BattleTurn",
                "GameplayChallenge_SettleWait",
                "GameplayChallenge_ResultNextStage",
                "GameplayChallenge_ResultTeamContinue",
                "GameplayChallenge_ResultConfirm",
                "GameplayChallenge_FriendCancel",
            ):
                context.clear_hit_count(node_name)
            detail = context.run_task("GameplayChallenge_Start", override)

        if detail is None or detail.status.failed:
            logger.error("WeeklyDispatch: 子流程执行失败，停止周常流程避免死循环。")
            context.override_pipeline({"Weekly_Dispatch": {"enabled": False}})

        # 子流程结束后会在主页；复位进入周常页的两个点击节点，
        # 让 Weekly_Dispatch.next = Weekly_OpenPage 能再次「每日任务 → 周常」进入。
        for node_name in RESET_BEFORE_REOPEN:
            context.clear_hit_count(node_name)

        return CustomAction.RunResult(success=True)
