"""「终末之战」任务的自定义动作（用户需求）。

入口 Pipeline：`resource/base/pipeline/FinalBattle/FinalBattle.json` 的 `FinalBattle_Flow`
（导航段：主页「菜单」→ 活动 → 活动一览「终末之战」→ 活动页底部地图入口），走到地图后交给本模块。
地图入口按副本分流（用户 2026-10-28 更正）：
  · 幼年体 / 成年体 →「入侵讨伐」；
  · 终末使徒 / 困难终末使徒 →「深入裂隙」。
分流由任务设置「终末之战-挑战副本」覆写 `FinalBattle_Invade` 的 expected 实现。

地图内流程（按用户口述的副本差异实现）：
  1. 点完地图入口（幼年体/成年体 =「入侵讨伐」，使徒系列 =「深入裂隙」）后等角色自己寻路：
     幼年体 / 成年体 5s，终末使徒系列 10s；
  2. **摇杆位置固定**（中心 360,780）：按住摇杆推到目标方向并**保持** N 秒再松手
     —— 幼年体向左 1.3s、成年体向上 1.5s、终末使徒向上 4.5s + 向右 1s、困难终末使徒向上 4.5s + 向左 1.5s；
     左/右只推摇杆半径以内的偏移量（130px），手指始终落在摇杆上，避免拖出摇杆导致停走；
  3. 每走完一段就看一眼「挑战」按钮；认不到就沿最后方向再推一段（有界，绝不无限滑）；
  4. 点「挑战」：
     · 终末使徒系列：选难度（困难系列先点「困难」页签）→「创建房间」→ 助战选择 →
       编队「确定」→ 房间「开始战斗」→「是否开始战斗?」确定；
     · 幼年体 / 成年体：点「挑战」后若弹「助战选择」就按全局偏好选，再点「开始战斗」/「确定」；
       两个页面都没有说明已经直接进战斗，等战斗画面出现即可；
  5. 战斗模块（只点「跳过回合」，受全局开关 `Flag_BattleSkipTurn` 控制）；
  6. 胜利：结算页优先点「挑战下一关」直接进下一场，否则连点「点击空白处继续/确认/确定」领奖；
  7. 失败（用户要求）：取消复活 → 取消呼叫支援 → 撤退 → 重新点「挑战」；
  8. 按任务设置次数循环，结束回主页。

实机已确认（2026-10-28，720x1280，MuMu 127.0.0.1:16512）：
  · 主页底部「菜单」(637,1232,55,32) → 菜单页左侧「活」(157,600,44,44) → 活动一览里
    「终末之战」卡片 (88,145,256,75) → 活动页底部「入侵讨伐」(465,1217,98,29)；
  · 点「入侵讨伐」后画面仍叠着活动页 HUD，但背景变成可拖动的实时地图（开场约 8~10s 才切完）；
  · 摇杆中心 (360,780)：按住后向上/向左/向右推，场景明显位移（手指松开即停止）；
  · 走近目标后，怪物名牌「虚空蝠【幼年体】/【成年体】」浮在画面上部，**「挑战」浮钮**
    出现在名牌右下方（实测一次 (569,794,46,27)，随角色/目标位置浮动，必须靠识别）；
  · 点「挑战」→ **挑战详情页**（标题 LV.85虚空蝠(成年体)、「今日剩余挑战次数：∞」、
    底部「挑战副本」(295,1033,132,39)）→ 点「挑战副本」→「助战选择」
    → 编队页（「主要成员」+ 底部**「确定」**(325,1025,73,41)，不是「开始战斗」）→ 战斗画面
    （左上「战斗剩余时间39:39」、底部中间「回合开始/跳过回合」）。

终末使徒分支实测（2026-10-28）：深入裂隙 → 等 10s → 上 4.5s → 右 1s → 「挑战」在 (567,792) →
  挑战页左侧难度页签「难度1」(46,221)/「难度2」(42,306)/「难度3」(42,392)（点难度2 后 LV.90→LV.95、
  页签放大、剩余次数变 0/0，确认页签生效）→「创建房间」(514,1033) → 「助战选择」→ 选助战 →
  编队页「确定」(325,1025,72,41) → 房间「开始战斗」→ 确认。

仍未实测（全部有识别门控，认不到就停，绝不盲点）：
  · 「困难」页签（当前版本终末使徒挑战页只有 难度1/2/3，没有困难页签，靠 OCR 找不到就跳过）；
  · 失败流程的三个取消按钮（只认到文案才点）。

Examples:
    `custom_action_param`::

        {
            "times": 3
        }
"""

from __future__ import annotations

import re
import time
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
from utils.interrupt import is_stopping
from utils.params import parse_params

from .dungeon_challenge import _fight, _settle
from .gameplay_challenge import (
    PickSupport,
    _read_support_prefs,
    _wait_for_battle_presence,
)
from .machine_god_descent import (
    _GOD_FORMATION_CONFIRM_ROI,
    _GOD_FORMATION_CONFIRM_TEXT,
    _GOD_ROOM_START_ROI,
    _GOD_ROOM_START_TEXT,
    _GOD_START_CONFIRM_ROI,
    _GOD_START_CONFIRM_TEXT,
    _MGD_BOTTOM_ROI,
    _SUPPORT_TITLE_ROI,
    _SUPPORT_TITLE_TEXT,
    _WIN_CONFIRM_TEXT,
    _WIN_CONTINUE_TEXT,
    _box_center_x,
    _click_box,
    _ensure_support,
    _find_contains,
    _find_in,
    _ocr,
    _wait_for,
    _wait_support_or_formation,
    handle_away_full,
)

# ---- 任务设置 --------------------------------------------------------------------
_TARGET_NODE = "FinalBattle_Config_Target"
MIN_TIMES = 1
MAX_TIMES = 99999

# ---- 摇杆与移动 ------------------------------------------------------------------
# 摇杆位置**固定**（实测 720x1280 底部中间半透明圆盘中心）：用户特别强调左/右走时要注意摇杆位置，
# 所以这里不按"屏幕边缘"取终点，而是从固定中心按方向推一个**摇杆半径以内**的偏移量，然后保持按住。
_JOY_CENTER = (360, 780)
_DIR_OFFSETS = {
    "up": (0, -170),
    "down": (0, 170),
    "left": (-130, 0),
    "right": (130, 0),
}
# 推到目标偏移后先停一小会儿再开始计时：游戏要等摇杆向量建立起来角色才真正起步。
_WALK_PUSH_SETTLE = 0.35
_WALK_SETTLE = 0.8

# ---- 挑战按钮 --------------------------------------------------------------------
# 未观测：先扫角色周围的中部区域，找不到再放宽到整条中间带。
# ⚠️ 两个 ROI 都**避开 y>1050 的聊天/气泡带和底部导航**：世界聊天里经常出现「挑战」两个字，
#    落进 ROI 会被当成按钮误点（点聊天只会白跑一步）。
_CHALLENGE_ROI = (100, 540, 520, 460)
_CHALLENGE_FALLBACK_ROI = (0, 380, 720, 640)
# 实测（2026-10-28）：走近幼年体时，目标名条浮在画面上部（如「虚空蝠」(559,273) +「【幼年生】」(620,270)），
# 挑战入口有时就挂在名条旁边，所以再兜一层右上区域 + 全屏（避开 y>1050 的聊天带）。
_CHALLENGE_PLATE_ROI = (400, 150, 320, 300)
_CHALLENGE_WIDE_ROI = (0, 0, 720, 1050)
_CHALLENGE_TEXT = "挑战"
_CHALLENGE_RETRY = 5  # 认不到「挑战」时最多再走这么多段
_CHALLENGE_EXTRA_SECONDS = 2.0
# 地图切换实测要 8~10s（用户要求点完入口等 5s/10s），第一段补走前再留一点加载余量，
# 否则"5s 后走 1s"很可能整段落在切场景动画里，白丢一次机会。
_MAP_LOAD_GRACE = 2.0

# ---- 终末使徒：难度 ------------------------------------------------------------------
# 页签先在左列找（本作详情页的难度页签都在左侧纵向排），找不到再整屏兜底。
# 只认「难度N」/「困难」这两种白名单文案，避免把说明文字里的数字当页签。
_DIFFICULTY_TAB_ROI = (0, 150, 260, 880)
_DIFFICULTY_ROI = (0, 120, 720, 900)
_DIFFICULTY_PATTERN = re.compile(r"难度\s*(\d+)")
_HARD_TAB_TEXT = "困难"

# ---- 挑战详情页（点完地图「挑战」之后的第一屏）--------------------------------------
# 实测（2026-10-28，幼年体/成年体）：标题「LV.85虚空蝠」「(成年体)」，顶部「今日剩余挑战次数：∞」，
# 底部蓝钮「挑战副本」(295,1033,132,39)。必须先点这个按钮才会进「助战选择」。
# 终末使徒的详情页没有这个按钮，而是「难度N」页签 +「创建房间」。
_CHALLENGE_COPY_ROI = (200, 980, 360, 180)
_CHALLENGE_COPY_WIDE_ROI = (0, 940, 720, 260)
_CHALLENGE_COPY_TEXT = "挑战副本"

# ---- 创建房间（多人房间战）----------------------------------------------------------
# 先把 ROI 放宽到整个面板下半部分，认不到再整条底部带兜底：不同活动的挑战页按钮横向位置不同。
_CREATE_ROOM_ROI = (300, 880, 420, 340)
_CREATE_ROOM_WIDE_ROI = (0, 820, 720, 460)
_CREATE_ROOM_TEXT = "创建房间"

# 实测（2026-10-28）：挑战页「今日剩余挑战次数」在 (169,533) —— 可用时是「∞」，未开放/没次数是「0/0」。
# 困难终末使徒当前就是 0/0，点「创建房间」也进不去助战，提前拦下来省一轮。
_ATTEMPT_ROI = (0, 500, 420, 90)
_NO_ATTEMPT_TEXT = "0/0"
_ENTER_TIMEOUT = 25.0

# ---- 战斗 / 结算 -------------------------------------------------------------------
_BATTLE_TIMEOUT = 600.0
_RESULT_ROUNDS = 14
_NEXT_STAGE_TEXT = "挑战下一关"
# 「挑战下一关」实测/推断在结算页下半部分：先窄后宽，认到就点。
_NEXT_STAGE_ROI = (0, 1000, 720, 280)
_NEXT_STAGE_WIDE_ROI = (0, 800, 720, 480)
_RESULT_CONFIRM_ROI = (430, 795, 175, 90)
_RESULT_CONFIRM_TEXT = "确定"

# ---- 失败处理（取消复活 → 取消呼叫支援 → 撤退）--------------------------------------
_REVIVE_ROI = (80, 250, 560, 620)
_REVIVE_TEXT = "复活"
# 单场战斗失败后最多重打几次（用户要求：撤退后重新点「挑战」再打）
_DEFEAT_RETRY = 3
_DEFEAT_BUTTON_ROI = (60, 760, 600, 360)
_RETREAT_ROI = (250, 1130, 320, 150)
_RETREAT_TEXT = "撤退"
_SUPPORT_DIALOG_TITLE_TEXT = "呼叫支援"
# 地图/活动页底部两个入口：入侵讨伐（幼年体/成年体）与深入裂隙（终末使徒系列）
_AD_MAP_ROI = (400, 1185, 300, 95)
_AD_MAP_TEXTS = ("入侵讨伐", "深入裂隙")


# ---- 任务设置读取 ----------------------------------------------------------------
def _read_target(context: Context) -> str:
    """读任务设置「挑战副本」的值（写在本任务选项载体节点的 expected[0]）。"""
    try:
        node = context.get_node_data(_TARGET_NODE) or {}
    except Exception as error:  # noqa: BLE001  读不到按默认副本处理
        logger.warning("终末之战：读取挑战副本节点失败：{}", error)
        return "幼年体"
    expected = ((node.get("recognition") or {}).get("param") or {}).get("expected") or []
    value = expected[0] if expected else ""
    return value.strip() if isinstance(value, str) and value.strip() else "幼年体"


def _read_times(params: dict[str, Any]) -> int:
    raw = params.get("times", MIN_TIMES)
    try:
        times = int(raw)
    except (TypeError, ValueError):
        logger.warning("终末之战：挑战次数 {!r} 不是整数，按 {} 次处理。", raw, MIN_TIMES)
        times = MIN_TIMES
    return max(MIN_TIMES, min(MAX_TIMES, times))


# ---- 摇杆移动 --------------------------------------------------------------------
def _walk_plan(target: str) -> list[tuple[str, float]]:
    """按用户描述给出寻路脚本：[(方向, 秒数)]。

    用户 2026-10-28 修正后的时长：
      幼年体：向左 1.3s；成年体：向上 1.5s；
      终末使徒：先向上 4.5s 再向右 1s；困难终末使徒：先向上 4.5s 再向左 1.5s。
    """
    if target.startswith("困难"):
        return [("up", 4.5), ("left", 1.5)]
    if target.startswith("终末使徒"):
        return [("up", 4.5), ("right", 1.0)]
    if target == "成年体":
        return [("up", 1.5)]
    return [("left", 1.3)]  # 幼年体


def _hold_walk(context: Context, direction: str, seconds: float) -> None:
    """按住**固定摇杆**向 direction 推住 seconds 秒再松手。

    用 touch_down / touch_move / touch_up 而不是 swipe：swipe 松手即停，只有把手指
    **保持在摇杆的偏移位置**上，角色才会持续朝该方向走完用户要求的时长。
    """
    dx, dy = _DIR_OFFSETS[direction]
    cx, cy = _JOY_CENTER
    controller = context.tasker.controller
    logger.info("终末之战：摇杆 {} 方向按住 {:.1f}s。", direction, seconds)

    try:
        controller.post_touch_down(cx, cy).wait()
        time.sleep(0.12)
        # 分几步推到目标偏移：一步到位偶尔会被游戏当成"点击/滑动"而不是摇杆输入。
        for ratio in (0.4, 0.75, 1.0):
            controller.post_touch_move(int(cx + dx * ratio), int(cy + dy * ratio)).wait()
            time.sleep(0.08)
        time.sleep(max(0.0, seconds - _WALK_PUSH_SETTLE))
        controller.post_touch_up().wait()
    except Exception as error:  # noqa: BLE001  触摸 API 不可用时退回 swipe（同样的起点/方向）
        logger.warning("终末之战：touch 摇杆失败（{}），退回 swipe。", error)
        controller.post_swipe(cx, cy, cx + dx, cy + dy, int(seconds * 1000)).wait()
    time.sleep(_WALK_SETTLE)


def _settle_after_invade(target: str) -> float:
    """点完「入侵讨伐」后等角色寻路的时间：终末使徒 10s，其它 5s（用户给定）。"""
    return 10.0 if ("使徒" in target) else 5.0


# ---- 识别辅助 --------------------------------------------------------------------
def _find_challenge(context: Context):
    """找地图上的「挑战」按钮：由近到远逐级放宽，全都认不到返回 None（绝不盲点）。"""
    return (
        _find_in(context, _CHALLENGE_ROI, _CHALLENGE_TEXT)
        or _find_contains(context, _CHALLENGE_ROI, _CHALLENGE_TEXT)
        or _find_in(context, _CHALLENGE_FALLBACK_ROI, _CHALLENGE_TEXT)
        or _find_contains(context, _CHALLENGE_FALLBACK_ROI, _CHALLENGE_TEXT)
        or _find_in(context, _CHALLENGE_PLATE_ROI, _CHALLENGE_TEXT)
        or _find_contains(context, _CHALLENGE_PLATE_ROI, _CHALLENGE_TEXT)
        or _find_in(context, _CHALLENGE_WIDE_ROI, _CHALLENGE_TEXT)
        or _find_contains(context, _CHALLENGE_WIDE_ROI, _CHALLENGE_TEXT)
    )


def _on_ad_map(context: Context) -> bool:
    """是否已经回到「终末之战」地图（底部「入侵讨伐 / 深入裂隙」始终在活动页/地图 HUD 上）。"""
    return any(_find_in(context, _AD_MAP_ROI, text) is not None for text in _AD_MAP_TEXTS)


def _walk_to_challenge(context: Context, target: str) -> bool:
    """按脚本走到目标附近，直到「挑战」按钮出现。返回是否认到按钮。"""
    plan = _walk_plan(target)
    for direction, seconds in plan:
        _hold_walk(context, direction, seconds)
        if _find_challenge(context) is not None:
            logger.info("终末之战：走完 {} 段后已看到「挑战」按钮。", direction)
            return True

    extra = plan[-1] if plan else ("up", 2.0)
    for index in range(_CHALLENGE_RETRY):
        if index == 0:
            time.sleep(_MAP_LOAD_GRACE)
        logger.info("终末之战：还没看到「挑战」，沿 {} 再走一段（第 {} 段）。", extra[0], index + 1)
        _hold_walk(context, extra[0], _CHALLENGE_EXTRA_SECONDS)
        if _find_challenge(context) is not None:
            return True
    logger.warning("终末之战：走了 {} 段仍没看到「挑战」按钮，本次放弃（不盲点）。", _CHALLENGE_RETRY)
    return False


# ---- 难度选择 --------------------------------------------------------------------
def _difficulty_tabs(context: Context, roi: Any) -> list[tuple[int, Any]]:
    """扫一个 ROI 里的「难度N」页签，返回 [(N, 识别框)]（只收白名单文案）。"""
    tabs: list[tuple[int, Any]] = []
    for text, box in _ocr(context, roi):
        match = _DIFFICULTY_PATTERN.search(text.replace(" ", ""))
        if match:
            tabs.append((int(match.group(1)), box))
    return tabs


def _select_difficulty(context: Context, target: str) -> bool:
    """终末使徒系列选难度；幼年体 / 成年体 没有难度页，直接返回 True。

    规则：
      · 实测（2026-10-28）：普通 / 困难**不是页签**，而是地图上两个不同的传送点——
        向右走是普通终末使徒（LV.90），向左走是困难终末使徒（LV.150），各自只有「难度N」
        页签（普通 难度1/2/3、困难 难度1/2）。所以选难度只按「难度N」点，不看「困难」页签；
      · 目标带「困难」而页面上又能认到「困难」页签时（未来版本可能改版），才额外点它；
      · 再按目标里的数字找「难度N」页签，认到才点；
      · 一个「难度N」都认不到 → 认为该副本当前只有一个难度，跳过（与副本挑战同一处理）；
      · 有页签但认不到目标难度 → 放弃本次（宁可不打，也不打错难度）。
    """
    if "使徒" not in target:
        return True

    digits = "".join(ch for ch in target if ch.isdigit())
    wanted = int(digits) if digits else 1

    if _HARD_TAB_TEXT in target:
        hard = (
            _find_in(context, _DIFFICULTY_TAB_ROI, _HARD_TAB_TEXT)
            or _find_contains(context, _DIFFICULTY_TAB_ROI, _HARD_TAB_TEXT)
            or _find_in(context, _DIFFICULTY_ROI, _HARD_TAB_TEXT)
            or _find_contains(context, _DIFFICULTY_ROI, _HARD_TAB_TEXT)
        )
        if hard is None:
            logger.info("终末之战：难度页没有「困难」页签（普通/困难由走位区分），按当前页选难度。")
        else:
            logger.info("终末之战：点「困难」页签 ({}, {})。", _box_center_x(hard), hard[1] + hard[3] // 2)
            _click_box(context, hard)
            time.sleep(1.2)

    tabs = _difficulty_tabs(context, _DIFFICULTY_TAB_ROI) or _difficulty_tabs(context, _DIFFICULTY_ROI)
    if not tabs:
        logger.info("终末之战：难度页没有「难度N」页签（该副本当前只有一个难度），跳过难度选择。")
        return True
    for number, box in tabs:
        if number == wanted:
            logger.info("终末之战：点难度「难度{}」({}, {})。", wanted, _box_center_x(box), box[1] + box[3] // 2)
            _click_box(context, box)
            time.sleep(1.2)
            return True
    logger.error(
        "终末之战：难度页认到 {}，但认不到「难度{}」，本次不挑战（免打错难度）。",
        [number for number, _ in tabs],
        wanted,
    )
    return False


# ---- 进入战斗 --------------------------------------------------------------------
def _create_room(context: Context) -> bool:
    """终末使徒系列：「创建房间」→ 助战选择 → 编队「确定」→ 房间「开始战斗」→ 确定。"""
    if _find_contains(context, _ATTEMPT_ROI, _NO_ATTEMPT_TEXT) is not None:
        logger.error("终末之战：该难度「今日剩余挑战次数 0/0」（未开放或无次数），本次不创建房间。")
        return False

    create = _wait_for(context, _CREATE_ROOM_ROI, _CREATE_ROOM_TEXT, 10)
    if create is None:
        create = _wait_for(context, _CREATE_ROOM_WIDE_ROI, _CREATE_ROOM_TEXT, 4)
    if create is None:
        logger.error("终末之战：挑战页没等到「创建房间」按钮，本次放弃（未开始战斗）。")
        return False

    logger.info("终末之战：点「创建房间」。")
    _click_box(context, create)
    time.sleep(0.8)
    if handle_away_full(context):
        logger.warning("终末之战：出现「暂离已满」，本次挑战没有真正开始。")
        return False

    state = _wait_support_or_formation(
        context, _GOD_FORMATION_CONFIRM_ROI, _GOD_FORMATION_CONFIRM_TEXT, _ENTER_TIMEOUT
    )
    if state is None:
        logger.error("终末之战：点「创建房间」后既没进助战选择，也没等到编队页「确定」。")
        return False
    if state == "support":
        if not _ensure_support(context, _GOD_FORMATION_CONFIRM_ROI, _GOD_FORMATION_CONFIRM_TEXT):
            logger.error("终末之战：助战选择没有完成（未进入编队页），放弃本次。")
            return False
        time.sleep(1.5)

    confirm = _wait_for(context, _GOD_FORMATION_CONFIRM_ROI, _GOD_FORMATION_CONFIRM_TEXT, 20)
    if confirm is None:
        logger.error("终末之战：没等到编队页「确定」，放弃本次（未开始战斗）。")
        return False
    _click_box(context, confirm)
    time.sleep(1.5)

    start = _wait_for(context, _GOD_ROOM_START_ROI, _GOD_ROOM_START_TEXT, 20)
    if start is None:
        logger.error("终末之战：点编队「确定」后没进到房间页「开始战斗」，进入公共异常处理流程。")
        context.run_task("Exception_Start")
        return False
    logger.info("终末之战：点房间页「开始战斗」。")
    _click_box(context, start)
    time.sleep(1.5)
    confirm_start = _wait_for(context, _GOD_START_CONFIRM_ROI, _GOD_START_CONFIRM_TEXT, 8)
    if confirm_start is not None:
        _click_box(context, confirm_start)
        time.sleep(1.5)
    return True


def _enter_plain_battle(context: Context) -> bool:
    """幼年体 / 成年体：点「挑战」之后的单人挑战入口。

    用户描述是「点击挑战 → 挑战副本 → 调用助战模块选助战 → 确认进入战斗」。不同版本的页面顺序
    不完全一样（有的先弹「助战选择」，有的直接是编队页，有的干脆直接进战斗），所以这里按画面判：
      助战选择 → 按全局偏好选 → 编队页「开始战斗 / 确定」→（确认框）→ 进战斗；
      编队页 → 直接点；
      「创建房间」→ 走多人房间流程；
      战斗画面已在场 → 成功。
    """
    deadline = time.time() + _ENTER_TIMEOUT
    while time.time() < deadline:
        if _find_in(context, _SUPPORT_TITLE_ROI, _SUPPORT_TITLE_TEXT) is not None:
            logger.info("终末之战：进入「助战选择」，按全局偏好选择助战。")
            # 实测（2026-10-28）：幼年体/成年体的编队页底部是「确定」(325,1025,73,41)，
            # 不是玩法挑战的「开始战斗」；锚点必须跟着换，否则 PickSupport 永远认不出编队页。
            support = PickSupport(
                formation_roi=list(_GOD_FORMATION_CONFIRM_ROI),
                formation_text=_GOD_FORMATION_CONFIRM_TEXT,
            )
            if not support.pick(context, _read_support_prefs(context)):
                logger.error("终末之战：助战选择没能打开编队页，放弃本次。")
                return False
            continue

        if _find_in(context, _CREATE_ROOM_ROI, _CREATE_ROOM_TEXT) is not None or _find_in(
            context, _CREATE_ROOM_WIDE_ROI, _CREATE_ROOM_TEXT
        ) is not None:
            return _create_room(context)

        start = _find_in(context, _GOD_ROOM_START_ROI, _GOD_ROOM_START_TEXT)
        start_text = _GOD_ROOM_START_TEXT
        if start is None:
            # 单人副本的编队页底部是「确定」（实测 (325,1025,73,41)）
            start = _find_in(context, _GOD_FORMATION_CONFIRM_ROI, _GOD_FORMATION_CONFIRM_TEXT)
            start_text = _GOD_FORMATION_CONFIRM_TEXT
        if start is not None:
            logger.info("终末之战：点「{}」进入战斗。", start_text)
            _click_box(context, start)
            time.sleep(1.5)
            confirm = _wait_for(context, _GOD_START_CONFIRM_ROI, _GOD_START_CONFIRM_TEXT, 6)
            if confirm is not None:
                _click_box(context, confirm)
                time.sleep(1.5)
            return True

        if _wait_for_battle_presence(context, 0.5):
            logger.info("终末之战：已直接进入战斗。")
            return True
        time.sleep(0.5)

    logger.warning("终末之战：{} 秒内没认出助战/编队/战斗画面，按已进战斗处理。", _ENTER_TIMEOUT)
    return True


def _open_challenge_detail(context: Context, target: str) -> bool:
    """点完地图「挑战」后，先处理可能出现的**挑战详情页**。

    实测（幼年体 / 成年体）：详情页顶部是「今日剩余挑战次数：∞」+ 怪物名「LV.85虚空蝠(成年体)」，
    底部是「挑战副本」蓝钮；不点它就一直停在详情页，后面既等不到助战也进不了战斗。
    终末使徒的详情页则是难度页签 +「创建房间」，本函数认不到「挑战副本」就直接返回。
    """
    box = _find_in(context, _CHALLENGE_COPY_ROI, _CHALLENGE_COPY_TEXT)
    if box is None:
        box = _find_contains(context, _CHALLENGE_COPY_ROI, _CHALLENGE_COPY_TEXT)
    if box is None:
        box = _wait_for(context, _CHALLENGE_COPY_WIDE_ROI, _CHALLENGE_COPY_TEXT, 4)
    if box is None:
        logger.info("终末之战：没有「{}」按钮（终末使徒走难度/创建房间），跳过详情页。", _CHALLENGE_COPY_TEXT)
        return False
    logger.info("终末之战：详情页点「{}」({}, {})。", _CHALLENGE_COPY_TEXT, _box_center_x(box), box[1] + box[3] // 2)
    _click_box(context, box)
    time.sleep(2.0)
    return True


def _enter_battle(context: Context, target: str) -> bool:
    """点完「挑战」后把角色送进战斗（按副本类型分两条入口）。

    顺序很重要：终末使徒**必须先选难度再创建房间**，所以不能先点「挑战副本」——
    否则会带着默认难度直接开局（实机难度页与「挑战副本」按钮同屏的版本就会打错难度）。
    """
    if "使徒" in target:
        if not _select_difficulty(context, target):
            return False
        if _create_room(context):
            return True
        # 少数版本里终末使徒的详情页也只有「挑战副本」（单人入口），退回单人流程再试一次
        logger.warning("终末之战：多人「创建房间」没走通，改试单人「挑战副本」入口。")
        _open_challenge_detail(context, target)
        return _enter_plain_battle(context)

    _open_challenge_detail(context, target)
    return _enter_plain_battle(context)


# ---- 胜利结算 / 挑战下一关 ---------------------------------------------------------
def _claim_rewards(context: Context, *, allow_next: bool = True) -> str:
    """胜利结算：优先「挑战下一关」快速进下一场，否则连点继续/确认/确定领奖。

    allow_next=False 用于**最后一轮**：这时再点「挑战下一关」会把角色送进下一场战斗，
    随后流程又回主页，白开一局。所以最后一轮只领奖、不点「挑战下一关」。

    返回 "next"（已用「挑战下一关」直接进下一场）/ "settled"（结算完，回到地图）。
    """
    for _round in range(_RESULT_ROUNDS):
        next_box = None
        if allow_next:
            next_box = (
                _find_in(context, _NEXT_STAGE_ROI, _NEXT_STAGE_TEXT)
                or _find_contains(context, _NEXT_STAGE_ROI, _NEXT_STAGE_TEXT)
                or _find_in(context, _NEXT_STAGE_WIDE_ROI, _NEXT_STAGE_TEXT)
                or _find_contains(context, _NEXT_STAGE_WIDE_ROI, _NEXT_STAGE_TEXT)
            )
        if next_box is not None:
            logger.info("终末之战：结算页点「{}」，直接进下一场。", _NEXT_STAGE_TEXT)
            _click_box(context, next_box)
            time.sleep(1.5)
            return "next"

        box = (
            _find_in(context, _MGD_BOTTOM_ROI, _WIN_CONTINUE_TEXT)
            or _find_in(context, _MGD_BOTTOM_ROI, _WIN_CONFIRM_TEXT)
            or _find_in(context, _RESULT_CONFIRM_ROI, _RESULT_CONFIRM_TEXT)
        )
        if box is not None:
            logger.info("终末之战：结算第 {} 轮，点「继续/确认/确定」。", _round + 1)
            _click_box(context, box)
            time.sleep(1.2)
            continue
        break

    # 实测（2026-10-28）：清剿地图里结算页停留几秒后会**自动开下一场**，
    # 这时已经不是"回地图"而是"又进战斗"，必须按 next 处理，否则会去地图上白找「挑战」。
    if _wait_for_battle_presence(context, 1.5):
        logger.info("终末之战：结算后游戏自动续战，已进入下一场战斗。")
        return "next"

    # 收尾：好友申请框 / 剩余结算屏交给副本挑战已验证的 _settle（只点取消）。
    _settle(context)
    return "settled"


# ---- 失败处理 --------------------------------------------------------------------
def _revive_prompt(context: Context) -> bool | None:
    """战斗失败判据（传给 `_fight` 的 extra_over）：

    「复活」提示 +「取消」按钮**同时**认到才算战斗结束。只认「复活」两个字不够 ——
    战斗画面里角色的技能/天赋文案也可能出现「复活」，会把正常战斗误判成失败。
    返回 False = 本场已失败（让 `_fight` 立刻收手去走失败处理）；None = 不表态。
    """
    if _find_contains(context, _REVIVE_ROI, _REVIVE_TEXT) is None:
        return None
    if _find_in(context, _DEFEAT_BUTTON_ROI, "取消") is None:
        return None
    logger.warning("终末之战：战斗画面出现「复活」提示，判定本场失败。")
    return False


def _cancel_revive(context: Context) -> bool:
    """战斗失败后的「复活」提示：只点「取消」，不复活（不消耗道具）。"""
    if _revive_prompt(context) is None:
        return False
    cancel = _find_in(context, _DEFEAT_BUTTON_ROI, "取消") or _find_contains(
        context, _DEFEAT_BUTTON_ROI, "取消"
    )
    if cancel is None:
        logger.warning("终末之战：看到「复活」提示但认不到「取消」，不盲点。")
        return False
    logger.info("终末之战：战斗失败，点「取消」不复活。")
    _click_box(context, cancel)
    time.sleep(1.5)
    return True


def _cancel_support_call(context: Context) -> bool:
    """失败/结算时若弹出「呼叫支援」框，点「取消」把它关掉（绝不发送）。"""
    if _find_contains(context, (150, 150, 420, 160), _SUPPORT_DIALOG_TITLE_TEXT) is None:
        return False
    cancel = _find_in(context, _DEFEAT_BUTTON_ROI, "取消") or _find_contains(
        context, _DEFEAT_BUTTON_ROI, "取消"
    )
    if cancel is None:
        logger.warning("终末之战：看到「呼叫支援」弹窗但认不到「取消」，不盲点。")
        return False
    logger.info("终末之战：关闭「呼叫支援」弹窗（点取消）。")
    _click_box(context, cancel)
    time.sleep(1.2)
    return True


def _retreat(context: Context) -> bool:
    """点「撤退」退出失败战斗（有确认框就确定）。"""
    box = _find_in(context, _RETREAT_ROI, _RETREAT_TEXT) or _find_contains(
        context, _RETREAT_ROI, _RETREAT_TEXT
    )
    if box is None:
        return False
    logger.info("终末之战：点「撤退」退出本场战斗。")
    _click_box(context, box)
    time.sleep(1.5)
    confirm = _find_in(context, _RESULT_CONFIRM_ROI, _RESULT_CONFIRM_TEXT) or _find_contains(
        context, _RESULT_CONFIRM_ROI, _RESULT_CONFIRM_TEXT
    )
    if confirm is not None:
        _click_box(context, confirm)
        time.sleep(1.5)
    return True


def _defeat_flow(context: Context) -> bool:
    """战斗失败（用户要求）：取消复活 → 取消呼叫支援 → 撤退 → 回到地图重新挑战。"""
    logger.warning("终末之战：本场战斗失败，按「取消复活 → 取消呼叫支援 → 撤退」处理。")
    for _round in range(4):
        changed = False
        if _cancel_revive(context):
            changed = True
        if _cancel_support_call(context):
            changed = True
        if changed:
            continue
        if _retreat(context):
            return True
        if _on_ad_map(context):
            return True
        time.sleep(1.2)
    logger.warning("终末之战：失败处理没走完（认不到复活/支援/撤退按钮），交给回主页链。")
    return False


# ---- 单次挑战 --------------------------------------------------------------------
def _run_once(
    context: Context, target: str, *, first_entry: bool, allow_next: bool = True
) -> str:
    """跑一场：等寻路 → 走摇杆 → 点挑战 → 进战斗 → 战斗 → 结算。

    allow_next=False 表示这是**最后一轮**：结算页不点「挑战下一关」，只领奖（避免白开下一局）。

    返回：
      "next"   胜利，且结算页用「挑战下一关」直接开了下一场；
      "done"   胜利并结算完，已回到地图；
      "defeat" 战斗失败且已撤退回地图（调用方按用户要求重新点「挑战」再打）；
      "fail"   连挑战都开不起来（认不到挑战/难度/创建房间），本轮放弃。
    """
    if first_entry:
        wait = _settle_after_invade(target)
        logger.info("终末之战：等待角色自己寻路 {:.0f}s。", wait)
        time.sleep(wait)
        if handle_away_full(context):
            logger.warning("终末之战：出现「暂离已满」，本次放弃。")
            return "fail"

    if _wait_for_battle_presence(context, 1.0):
        # 上一场结算后游戏自动续战：人已经在战斗里，再去找「挑战」/拖摇杆只会把流程带偏
        logger.info("终末之战：当前已在战斗中，直接推进本场。")
    else:
        if _find_challenge(context) is None and not _walk_to_challenge(context, target):
            return "fail"

        challenge = _find_challenge(context)
        if challenge is None:
            return "fail"
        logger.info("终末之战：点「挑战」({}, {})。", _box_center_x(challenge), challenge[1] + challenge[3] // 2)
        _click_box(context, challenge)
        time.sleep(2.0)

        if not _enter_battle(context, target):
            return "fail"

    if not _fight(context, _BATTLE_TIMEOUT, extra_over=_revive_prompt):
        _defeat_flow(context)
        return "defeat"

    outcome = _claim_rewards(context, allow_next=allow_next)
    return "next" if outcome == "next" else "done"


@AgentServer.custom_action("FinalBattleRun")
class FinalBattleRun(CustomAction):
    """终末之战：按任务设置的地图寻路 + 挑战 + 战斗，循环「挑战次数」轮。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            params = parse_params(argv.custom_action_param)
        except ValueError as error:
            logger.error("终末之战：custom_action_param 解析失败：{}", error)
            return CustomAction.RunResult(success=False)

        target = _read_target(context)
        times = _read_times(params)
        logger.info("终末之战开始：副本={}，计划 {} 次。", target, times)

        done = 0
        first_entry = True  # 只有刚点完「入侵讨伐」那一次需要等角色寻路
        in_next_battle = False  # 上一场用「挑战下一关」直接开下一局，不用再走地图
        while done < times:
            if is_stopping(context):
                logger.info("终末之战：收到停止请求，中断（已完成 {}/{}）。", done, times)
                break

            if in_next_battle:
                # 已经由「挑战下一关」开好下一场：直接推进战斗
                in_next_battle = False
                if not _fight(context, _BATTLE_TIMEOUT, extra_over=_revive_prompt):
                    _defeat_flow(context)
                    continue
                in_next_battle = _claim_rewards(context, allow_next=done + 1 < times) == "next"
                done += 1
                logger.info("终末之战：第 {}/{} 次完成（挑战下一关）。", done, times)
                continue

            logger.info("终末之战：第 {}/{} 次开始。", done + 1, times)
            outcome = "fail"
            for retry in range(_DEFEAT_RETRY + 1):
                outcome = _run_once(
                    context,
                    target,
                    first_entry=first_entry,
                    allow_next=done + 1 < times,
                )
                first_entry = False
                if outcome != "defeat":
                    break
                logger.warning(
                    "终末之战：第 {} 次战斗失败，已撤退；按设置重新点「挑战」重打（{}/{}）。",
                    done + 1,
                    retry + 1,
                    _DEFEAT_RETRY,
                )
                if not _on_ad_map(context):
                    logger.warning("终末之战：撤退后没回到地图，停止后续挑战。")
                    outcome = "fail"
                    break

            if outcome == "fail":
                logger.warning("终末之战：第 {} 次没能完成，停止后续挑战。", done + 1)
                break
            in_next_battle = outcome == "next"
            done += 1
            logger.info("终末之战：第 {}/{} 次完成。", done, times)

        logger.info("终末之战结束，共完成 {}/{} 次。", done, times)
        context.run_task("BackToMainHub_Start")
        return CustomAction.RunResult(success=True)
