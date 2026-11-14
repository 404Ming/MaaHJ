"""活动（Event，当前版本 = 「蒸汽国度」）任务的自定义动作：按难度与次数挑战活动副本。

实机 2026-11-12（127.0.0.1:16512，720x1280）取证：
  · 活动页底部「简单副本/蒸汽虫」挑战 (221,1137,76,41)、「普通副本/蒸汽人偶」挑战 (559,1137,80,41)、
    Boss「暴走蒸汽人偶」挑战 (575,985,57,32)；三处文案都是「挑战」，只能靠**窄 ROI** 区分。
  · 点「挑战」→ 助战选择 → 编队「开始战斗」→ 战斗 → 结算，与机神降临
    `MachineGodDescentElementMachineChallenge` 走的是同一套 UI，所以直接复用已实机验证的
    `machine_god_descent._run_battle`（challenge_roi / post_roi / post_texts 由这里按活动页传入）。

⚠️ 会真实消耗活动副本次数/体力：开关「活动-挑战难度」选「不挑战」、载体节点读不到、
    或次数非法时整段跳过，一下都不点。
    难度取值「简单 / 普通 / 困难」分别对应底部简单副本卡、普通副本卡、中部 Boss 高难挑战。
    简单档（用户 2026-11-14 要求）：结算页出现「挑战下一关」就直接点它连打下一场
    （与终末之战成年体同款），不再每场都回活动页/地图重新寻路；其余档保持逐场回活动页。
"""

from __future__ import annotations

import re
import time

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
from utils.interrupt import is_stopping
from utils.params import parse_params

from .fastframe import click_and_await, frame_index, pick_exact
from .gameplay_challenge import _ocr, _parse_times
from .machine_god_descent import (
    _CLICK_SETTLE,
    _FRIEND_CANCEL_ROI,
    _FRIEND_CANCEL_TEXT,
    _MGD_BACK_POINT,
    _MGD_BOTTOM_ROI,
    _RESULT_BAND_ROI,
    _WIN_CONFIRM_TEXT,
    _WIN_CONTINUE_TEXT,
    _click_box,
    _create_god_room,
    _find_in,
    _run_battle,
    _run_battle_turns,
    _wait_any,
    _wait_for,
    set_log_tag,
)

# 任务设置载体（interface.json 的 select / input 选项按 PI 规范覆写）
_FLAG_CHALLENGE = "Event_Flag_Challenge"
_CONFIG_DIFFICULTY = "Event_Config_Difficulty"

# 逐帧实机取证（2026-11-12，每秒 1.5 帧共 23 帧）：
#   活动页 → 助战选择 → 编队「开始战斗」(297,1100,121,37) → 回合制战斗（第N回合 / 跳过回合）
#   →「战斗胜利 / 点击空白处继续」→ 结算页「确认」(528,1192,57,32) /「挑战下一关」
#   → **落在活动实时地图**（左下「返回」「世界」，中部怪物「挑战」(336,742,45,26)），不是活动主页。
# 所以：每轮既可能在活动页（首轮）也可能在地图（次轮起），战后判据要同时兜住两者。
_POST_ROI = (0, 0, 720, 1280)
# 战斗结束后的合法落点：活动页 / 实时地图 / 主页 / 副本详情页（普通档房战）
_POST_TEXTS = ("简单副本", "普通副本", "世界", "返回", "惑星之间", "创建房间")
_HOME_TEXTS = ("简单副本", "普通副本")
# 地图中部怪物的「挑战」按钮（实测 (336,742,45,26)）：战斗结束后用它续战下一轮
_MAP_CHALLENGE_ROI = (280, 700, 120, 90)

# 三档难度（用户指定：简单 / 普通 / 困难）→ 实机对应的「挑战」按钮窄 ROI。
# 困难 = 中部 Boss「暴走蒸汽人偶」的高难挑战 (575,985,57,32)：当前版本活动页
# 只写了「简单副本 / 普通副本」两张卡，没有直接叫「困难副本」的第三张卡。
_DIFFICULTY_CHALLENGE_ROI: dict[str, tuple[int, int, int, int]] = {
    "简单": (180, 1110, 150, 70),
    "普通": (520, 1110, 150, 70),
    # ⚠️ 左界必须 ≥550：左边「扫荡」实测 box (488,988,54,30) 右沿到 542，
    #    原来的 540 会把它裁进来，OCR 读成「扫荡挑战」→ 全等匹配「挑战」失败（实机 2026-11-12 踩过）。
    "困难": (550, 960, 100, 60),
}

# ---- 普通 / 困难档：多人房间副本 --------------------------------------------------------
# 实机 2026-11-12 逐跳取证（点普通副本「挑战」后）：
#   活动页 → （困难档先过实时地图再点地图中部「挑战」）→ 副本详情页
#   「蒸汽人偶 LV.80 / 今日剩余挑战次数：2/2 / 加入房间 (144,1032) / 创建房间 (447,1030,132,46)」
#   → 创建房间 → 助战选择 → 编队「确定」→ 房间「开始战斗」→ 确认 → 回合制战斗 → 结算。
# ⚠️ 这条链与简单档（助战选择 → 编队 → 开始战斗）不同，`_run_battle` 直接套会在第一步就失败
#    （等不到「助战选择」）；房间链复用机神降临已实机验证的 `_create_god_room` +
#    `_run_battle_turns` + 结算三件套，UI 组件与点位一致。
# ⚠️ 只有「普通」是多人房间副本（详情页「创建房间/加入房间」，实机已验证）；
#    「困难」点 Boss「挑战」是**直接弹助战选择**（与简单档同一条链），逐帧诊断确认。
_ROOM_DIFFICULTIES = frozenset({"普通"})
# 结算页「继续」文案：普通副本/机神是「点击空白处继续」，困难 Boss 是「点击空白区域跳过」
_CONTINUE_TEXTS = ("点击空白处继续", "点击空白区域跳过")
_ROOM_DETAIL_TEXTS = ("创建房间", "加入房间")
_CREATE_ROOM_ROI = (400, 1000, 220, 90)
_ROOM_BATTLE_TIMEOUT = 180.0
_ROOM_RESULT_MAX_SCREENS = 5
# 简单 / 困难档（助战链）的战斗推进上限：简单档实测 ~40s，困难 Boss 实测 >120s（默认 120s 会误判超时），
# 给 5 分钟覆盖高血量 Boss；超时仍会放弃本场并记一条 warning。
_SIMPLE_BATTLE_TIMEOUT = 300.0

# 领奖/结算后可能叠的全屏「恭喜获得」浮层；复用公共节点 Exception_RewardClose 的识别与固定空白落点
_OBTAIN_ROI = (240, 925, 240, 70)
_OBTAIN_TEXT = "点击空白区域关闭"
_MAX_OBTAIN_LAYERS = 3
# 连续多少轮「没打成」就收手，避免在未知页面上反复点「挑战」白耗次数
_MAX_STUCK_ATTEMPTS = 3


def _read_difficulty(context: Context) -> str:
    """读 select 选项写进 Event_Config_Difficulty.recognition.param.expected[0] 的难度名。"""
    try:
        node = context.get_node_data(_CONFIG_DIFFICULTY) or {}
    except Exception as error:  # noqa: BLE001 读不到按「不挑战」处理，绝不擅自消费次数
        logger.warning("活动：读取难度载体节点失败：{}", error)
        return ""
    expected = ((node.get("recognition") or {}).get("param") or {}).get("expected") or []
    value = expected[0] if expected else ""
    return value.strip() if isinstance(value, str) else ""


def _dismiss_obtain(context: Context) -> None:
    """关掉可能叠在活动页上的「恭喜获得」遮屏（有界，最多 3 层）。"""
    for _ in range(_MAX_OBTAIN_LAYERS):
        if _find_in(context, _OBTAIN_ROI, _OBTAIN_TEXT) is None:
            return
        # 公共节点自带 OCR 门控 + 实测空白落点 (330,1185,80,60)，比自己再写一套固定点可靠
        context.run_task("Exception_RewardClose")


def _ensure_event_home(context: Context) -> bool:
    """把页面拉回活动页（房间副本每轮都必须从活动页的难度按钮起手）。

    实测落点链（2026-11-12）：副本详情页 --左下返回--> 活动实时地图 --左下返回--> 主页；
    主页右列「蒸汽国度」再进活动页。所有跳转都用 OCR 识别 + 点识别框中心，
    返回箭头没有文字，复用机神降临已实测的左下角点位 `_MGD_BACK_POINT`。
    """
    for _ in range(6):
        _dismiss_obtain(context)
        # 战斗结算后晚弹的好友申请框会挡住一切点击：交给公共异常链（自带条件门控，没异常时零开销）
        if _find_in(context, (120, 815, 160, 80), "取消") is not None:
            context.run_task("Exception_Start")
            continue
        if _wait_any(context, _POST_ROI, _HOME_TEXTS, 1.5):
            return True
        if _find_in(context, (0, 1200, 220, 80), "惑星之间") is not None:
            entry = _find_in(context, (600, 700, 120, 250), "蒸汽国度")
            if entry is None:
                logger.warning("活动：主页上没找到「蒸汽国度」入口。")
                return False
            _click_box(context, entry)
            time.sleep(1.5)
            continue
        context.tasker.controller.post_click(*_MGD_BACK_POINT).wait()
        time.sleep(_CLICK_SETTLE)
    logger.warning("活动：6 步内没能回到活动页。")
    return False


def _run_room_battle(context: Context, difficulty_roi) -> bool:
    """普通 / 困难档：从活动页点难度「挑战」→ 详情页「创建房间」→ 打完一整个房间副本。

    步骤：挑战 →（困难档先过地图再点中部「挑战」）→ 详情页「创建房间」
    → 助战选择 → 编队「确定」→ 房间「开始战斗」→ 确认框「确定」→ 战斗推进
    → 胜利结算（连点「点击空白处继续」→「确认」）→ 好友申请「取消」→ 战后落点。
    任一步认不到就返回 False（不盲点），由上层按"无进展"计数收手。
    """
    challenge = _find_in(context, difficulty_roi, "挑战")
    if challenge is None:
        logger.warning("活动：没找到{}档对应的「挑战」按钮。", "房间副本")
        return False
    _click_box(context, challenge)
    time.sleep(_CLICK_SETTLE)

    # 困难档：点 Boss「挑战」先进实时地图（首次进入要加载几秒），再点地图中部「挑战」才到详情页。
    # 等待给足：地图 12s + 详情页 20s（实机冷进地图 + 加载实测约 8~15s）。
    if _wait_for(context, _CREATE_ROOM_ROI, "创建房间", 10) is None:
        map_challenge = _wait_for(context, _MAP_CHALLENGE_ROI, "挑战", 12)
        if map_challenge is not None:
            _click_box(context, map_challenge)
            time.sleep(_CLICK_SETTLE)
        if _wait_for(context, _CREATE_ROOM_ROI, "创建房间", 20) is None:
            logger.warning("活动：点「挑战」后没等到副本详情页「创建房间」。")
            return False

    # 创建房间 → 助战选择 → 编队「确定」→ 房间「开始战斗」→ 确认「确定」→ 进战斗
    if not _create_god_room(context):
        logger.warning("活动：创建房间 / 助战 / 编队 / 开始战斗 未走通。")
        return False

    def _over(index=None) -> bool:
        # index：战斗循环那一帧的识别结果（正常情况下直接复用，不再截图）
        if index is None:
            index = frame_index(context, _RESULT_BAND_ROI, _MGD_BOTTOM_ROI)
        return (
            pick_exact(index, _WIN_CONTINUE_TEXT) is not None
            or pick_exact(index, _WIN_CONFIRM_TEXT) is not None
        )

    first_index: list = []
    if not _run_battle_turns(context, _over, _ROOM_BATTLE_TIMEOUT, first_index_out=first_index):
        logger.warning("活动：房间副本战斗推进超过 {} 秒仍未结束。", _ROOM_BATTLE_TIMEOUT)
        return False

    # 结算：第一下直接用胜利那一帧，之后按帧推进（按钮消失即换屏，不留固定 sleep）
    index = first_index[0] if first_index else None
    for _screen in range(_ROOM_RESULT_MAX_SCREENS):
        if index is None:
            index = frame_index(context, _RESULT_BAND_ROI, _MGD_BOTTOM_ROI)
        cont = pick_exact(index, *_CONTINUE_TEXTS)
        if cont is None:
            break
        index = click_and_await(
            context, cont, (*_CONTINUE_TEXTS, _WIN_CONFIRM_TEXT), (_RESULT_BAND_ROI, _MGD_BOTTOM_ROI)
        )
    confirm = pick_exact(index, _WIN_CONFIRM_TEXT) if index else None
    if confirm is None:
        confirm = _wait_for(context, _MGD_BOTTOM_ROI, _WIN_CONFIRM_TEXT, 8)
    if confirm is not None:
        click_and_await(context, confirm, (_WIN_CONFIRM_TEXT,), (_RESULT_BAND_ROI,))

    if _wait_for(context, _FRIEND_CANCEL_ROI, _FRIEND_CANCEL_TEXT, 3) is not None:
        logger.debug("活动：结算后弹出好友申请框，点「取消」。")
        _click_box(context, _find_in(context, _FRIEND_CANCEL_ROI, _FRIEND_CANCEL_TEXT))
        time.sleep(_CLICK_SETTLE)

    # 战后落点：详情页 / 活动页 / 地图 / 主页都算完成（下一轮开头 _ensure_event_home 会拉回活动页）
    if not _wait_any(context, _POST_ROI, (*_POST_TEXTS, *_ROOM_DETAIL_TEXTS, "惑星之间"), 10):
        logger.warning("活动：房间副本打完后没回到详情页/活动页/地图。")
        return False
    return True


@AgentServer.custom_action("EventChallengeRun")
class EventChallengeRun(CustomAction):
    """按「活动-挑战难度」选中的副本、打满「活动-挑战次数」(0-99999，0 = 不挑战) 次。

    简单档：一轮 `_run_battle` 用结算页「挑战下一关」连续开下一场（`repeat_with_next_stage`），
    直到次数打满、按钮消失或体力/次数不足，避免每场都回活动页重新寻路。
    普通（房间战）/困难：每轮先 `_ensure_event_home` 回活动页，再点难度卡起手。
    认不到页面 / 战斗没打完会累计"无进展"，连续 3 次就停止，不无限点。

    Examples:
        `custom_action_param`::

            {
                "times": 5
            }
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        # 本段复用 machine_god_descent 的战斗动作：把日志前缀切到「活动」，
        # 活动任务的日志里不再出现「机神降临：」（机神降临任务自己的入口动作会切回去）。
        set_log_tag("活动")
        try:
            flag = context.get_node_data(_FLAG_CHALLENGE) or {}
        except Exception as error:  # noqa: BLE001
            logger.warning("活动：读取挑战开关载体失败：{}，按不挑战处理。", error)
            return CustomAction.RunResult(success=True)
        if not bool(flag.get("enabled", False)):
            logger.info("活动：任务设置「活动-挑战难度」为不挑战，跳过挑战副本段。")
            return CustomAction.RunResult(success=True)

        try:
            params = parse_params(argv.custom_action_param)
        except ValueError as error:
            logger.error("活动：custom_action_param 解析失败：{}", error)
            return CustomAction.RunResult(success=False)
        times = _parse_times(params.get("times"))
        if times is None:
            logger.error("活动：挑战次数非法（需 0-99999），跳过挑战副本段。")
            return CustomAction.RunResult(success=True)
        if times == 0:
            logger.info("活动：任务设置「挑战次数」为 0，跳过挑战副本段。")
            return CustomAction.RunResult(success=True)

        target = _read_difficulty(context)
        challenge_roi = _DIFFICULTY_CHALLENGE_ROI.get(target)
        if challenge_roi is None:
            logger.info("活动：没有指定有效难度（target={!r}），跳过挑战副本段。", target)
            return CustomAction.RunResult(success=True)

        done = 0
        stuck = 0
        # 「简单」副本的结算页有「挑战下一关」：胜利后直接点它连打（与终末之战成年体同款），
        # 不必每场都回活动页 / 地图重新寻路。普通（房间战）和困难保持逐场回活动页的旧逻辑。
        fast_repeat = target == "简单"
        while done < times:
            if is_stopping(context):
                logger.info("活动：收到停止请求，挑战副本段中断（已完成 {}/{} 次）。", done, times)
                break
            # 每轮都先回活动页、再点难度卡：实测战后落在地图，而地图中部「挑战」进的是
            # 多人房间详情页（不是助战链），直接续战第二场必失败（2026-11-12 实机 1/2）。
            if not _ensure_event_home(context):
                stuck += 1
                logger.warning("活动：回不到活动页（连续无进展 {} 次）。", stuck)
                if stuck >= _MAX_STUCK_ATTEMPTS:
                    break
                continue
            finished = 0
            if target in _ROOM_DIFFICULTIES:
                if _run_room_battle(context, challenge_roi):
                    finished = 1
            else:
                battles: list[int] = []
                ok = _run_battle(
                    context,
                    post_roi=_POST_ROI,
                    post_texts=_POST_TEXTS,
                    challenge_roi=challenge_roi,
                    battle_timeout=_SIMPLE_BATTLE_TIMEOUT,
                    # 困难 Boss 的结算过场比机神多、好友申请也晚弹，落点判据偶尔认不到；
                    # 战斗与结算本身已经走完，这里不把"落点没认到"当失败，交给下一轮 _ensure_event_home 恢复。
                    strict_post=False,
                    # 简单档：剩下的次数全部用结算页「挑战下一关」连打（不重复寻路）
                    repeat_with_next_stage=(times - done - 1) if fast_repeat else 0,
                    battles_done=battles,
                )
                finished = battles[0] if battles else 0
                if ok and finished == 0:
                    finished = 1
            if finished == 0:
                stuck += 1
                logger.warning("活动：第 {} 次挑战没能完成（连续无进展 {} 次）。", done + 1, stuck)
                if stuck >= _MAX_STUCK_ATTEMPTS:
                    break
                continue
            stuck = 0
            done += finished
            extra = f"（本轮连打 {finished} 场）" if finished > 1 else ""
            logger.info("活动：「{}」第 {}/{} 次挑战完成{}。", target, done, times, extra)

        logger.info("活动：挑战副本段结束，共完成 {}/{} 次。", done, times)
        return CustomAction.RunResult(success=True)


# ---- 自动探索面板：把「探索次数」拉到当前可使用的最大值 ------------------------------------
# 面板实测（2026-11-14，720x1280，活动页 → 右下「自动探索」）：
#   · 「探索次数：N」读数 (100,620,340,70)；
#   · 滑块轨道 y=702，左端 x≈165（N=1）、右端 x≈575（N=500）；
#   · 右侧「N/500」是上限读数（面板上限恒 500）；
#   · 顶部蒸汽钥匙数（如 916）在 (500,0,180,60)，面板不遮顶部。
# 说明原文「每次探索消耗1个蒸汽钥匙…每次自动探索最多消耗500个蒸汽钥匙」
#   ⇒ 本次可用最大值 = min(500, 当前蒸汽钥匙数)。
_EXPLORE_TIMES_ROI = (100, 620, 340, 70)
_EXPLORE_CAP_ROI = (430, 715, 210, 70)
# 只框顶部的蒸汽钥匙数：实测「916」在 x≈485~512（绿色数字），右边 x≈613 起是金币 307,080；
# ROI 右界必须 < 600，否则会把金币一起 OCR 进来（实测取到 6 → 目标被算成 6）。
_EXPLORE_KEY_ROI = (450, 0, 110, 60)
_EXPLORE_TRACK_Y = 702
_EXPLORE_TRACK_LEFT_X = 165
_EXPLORE_TRACK_RIGHT_X = 575
_EXPLORE_TRACK_MIN = 1
_EXPLORE_LIMIT = 500
_EXPLORE_DRAG_TRIES = 2
_EXPLORE_TIMES_PATTERN = re.compile(r"探索次数[:：]?(\d+)")
# 右侧读数是「当前值/上限」（实测「9/500」）——上限取**分母**，取分子会把目标算成当前值。
_EXPLORE_CAP_PATTERN = re.compile(r"(\d+)/(\d+)")


def _read_first_int(context: Context, roi, pattern: re.Pattern) -> int | None:
    """在 ROI 内 OCR 并按正则抓**第一个分组**的整数；读不到返回 None（不猜）。"""
    for text, _box in _ocr(context, roi):
        match = pattern.search(text.replace(" ", ""))
        if match:
            return int(match.group(1))
    return None


def _read_explore_limit(context: Context) -> int | None:
    """读面板右侧「当前值/上限」的**上限**（分母）；读不到返回 None。"""
    for text, _box in _ocr(context, _EXPLORE_CAP_ROI):
        match = _EXPLORE_CAP_PATTERN.search(text.replace(" ", ""))
        if match:
            return int(match.group(2))
    return None


@AgentServer.custom_action("EventSetMaxExploreTimes")
class EventSetMaxExploreTimes(CustomAction):
    """把「自动探索」面板的「探索次数」拖到**当前可使用的最大值**。

    可用最大值 = min(面板上限 500, 顶部蒸汽钥匙数)：说明写明「每次探索消耗1个蒸汽钥匙」，
    钥匙不够时开满也只消耗现有的那些，未消耗完的会退回。
    只拖滑块（起点按当前次数在轨道上线性推算，终点=轨道右端），拖完必须 OCR 复核；
    复核没到目标就再拖一次；仍不到时**不拦流程**（按当前值开启并告警），避免整段探索被跳过。

    Examples:
        `custom_action_param`::

            {}   # 本动作不需要参数
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        set_log_tag("活动")
        current = _read_first_int(context, _EXPLORE_TIMES_ROI, _EXPLORE_TIMES_PATTERN)
        if current is None:
            logger.warning("活动：认不到面板「探索次数」读数，按当前值直接开启（不拖滑块）。")
            return CustomAction.RunResult(success=True)

        cap = _read_explore_limit(context) or _EXPLORE_LIMIT
        cap = min(cap, _EXPLORE_LIMIT)
        keys = _read_first_int(context, _EXPLORE_KEY_ROI, re.compile(r"(\d+)"))
        target = cap if keys is None else min(cap, keys)
        if target <= 0:
            logger.warning("活动：蒸汽钥匙数读成 {}，按面板上限 {} 设置探索次数。", keys, cap)
            target = cap

        if current >= target:
            logger.info("活动：探索次数已是 {}（目标 {}），无需调整。", current, target)
            return CustomAction.RunResult(success=True)

        span = _EXPLORE_TRACK_RIGHT_X - _EXPLORE_TRACK_LEFT_X
        for attempt in range(_EXPLORE_DRAG_TRIES):
            if attempt == 0:
                ratio = (current - _EXPLORE_TRACK_MIN) / max(_EXPLORE_LIMIT - _EXPLORE_TRACK_MIN, 1)
                start_x = int(_EXPLORE_TRACK_LEFT_X + max(0.0, min(1.0, ratio)) * span)
            else:
                start_x = _EXPLORE_TRACK_LEFT_X
            logger.debug(
                "活动：第 {} 次拖滑块（当前 {} → 目标 {}，起点 x={}）。", attempt + 1, current, target, start_x
            )
            context.tasker.controller.post_swipe(
                start_x, _EXPLORE_TRACK_Y, _EXPLORE_TRACK_RIGHT_X, _EXPLORE_TRACK_Y, 800
            ).wait()
            time.sleep(0.6)
            now = _read_first_int(context, _EXPLORE_TIMES_ROI, _EXPLORE_TIMES_PATTERN)
            if now is not None and now >= target:
                logger.info("活动：探索次数已设为 {}（钥匙 {}，面板上限 {}）。", now, keys, cap)
                return CustomAction.RunResult(success=True)
            if now is not None:
                current = now

        logger.warning(
            "活动：探索次数没拖到目标 {}（当前读作 {}），按当前值开启；如需更多请手动调整。", target, current
        )
        return CustomAction.RunResult(success=True)
