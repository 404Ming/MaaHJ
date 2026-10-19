"""机神降临（活动）任务的自定义动作。

目前只有一段：进活动后**按需解锁「自动探索」**——锁着时界面会显示
「探索N次开启自动探索」（实测 box (465,976,232,26) score 1.000），按 N 点「探索」即可解锁；
用户自己已经解锁过时这句提示不出现，直接跳过（no-op）。

为什么放 Python 而不是纯 pipeline：要读**动态的 N**、点 N 次、每次点击后校验读数递减，
是典型的状态化逻辑（pipeline 的 expected 是静态全串匹配，抓不住变化中的数字）。
"""

from __future__ import annotations

import re
import time

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
from utils.params import parse_params

from .gameplay_challenge import _box_center_x, _box_center_y, _ocr, _parse_times

# 解锁进度锚点（实测 720x1280）：「探索9次开启自动探索」box (465,976,232,26)
_HINT_ROI = (440, 955, 270, 60)
_HINT_PATTERN = re.compile(r"探索\s*(\d+)\s*次开启自动探索")
# 「探索」按钮自身（实测 box (374,1068,69,36)）
_EXPLORE_ROI = (340, 1030, 120, 90)
_EXPLORE_TEXT = "探索"
# 任务设置「自动探索领取」的开关载体（interface.json 的 switch 选项覆写它的 enabled）
_AUTO_EXPLORE_FLAG = "MachineGodDescent_Flag_AutoExploreClaim"
# ---- 第三段：自动探索本体的控件（全部实机实测 box）------------------------------
# 页面上「已在自动探索中」时出现的按钮
_STOP_RUNNING_ROI = (235, 1040, 240, 80)
_STOP_RUNNING_TEXT = "停止自动探索"
# 点它弹出的「自动获取收获」面板：确认停止
_STOP_PANEL_CONFIRM_ROI = (150, 715, 175, 60)
_STOP_PANEL_CONFIRM_TEXT = "停止探索"
# 未在探索时页面上的「自动探索」按钮（右下角；注意排除面板标题同名文本）
_AUTO_BUTTON_ROI = (560, 1040, 160, 80)
_AUTO_BUTTON_TEXT = "自动探索"
# 面板内：时长读数（如「4小时」）与侦察工具读数（如「240/271」）
_DURATION_ROI = (95, 585, 300, 75)
_TOOLS_ROI = (450, 760, 150, 60)
_PANEL_CONFIRM_ROI = (420, 808, 160, 60)
_PANEL_CONFIRM_TEXT = "开启探索"
# 每小时消耗的侦察工具数（面板原文：「每探索1小时消耗60个侦察工具」）
_TOOLS_PER_HOUR = 60
# 滑块上限（用户确认：除这个之外没有别的硬上限）
_MAX_HOURS = 12
# 每步操作后的等待/重试
_PANEL_TIMEOUT = 12.0
_PANEL_POLL = 1.0

# ---- 第二段补充：点「探索」后**可能遇到挑战**（用户提供 8 张实测截图）----------------
# 实测链路：点探索 → （可能出宝箱的过渡态）→ 底部按钮变成「挑战」→ 点它进编队页
#   「Lv.80 缥缈虫 / 剩余挑战次数: ∞」→ 点「开始战斗」→ 战斗（回合开始/奥义释放/魔兽召唤）
#   →「战斗胜利」→ 点「点击空白处继续」→「战斗胜利·战斗获得」→ 点「确认」→ 回到机神降临页
#   （计数递减、底部重新出现「探索」，可以继续点下一次）。
#
# ⚠️ 所有按钮都在**底部一条横带**里，且文案互不相同（挑战/开始战斗/回合开始/奥义释放/
#    跳过回合/点击空白处继续/确认），所以用「整条底部带 + 全串精确匹配」定位，
#    点击一律取识别框中心 —— 不需要为每个按钮量精确 ROI，也不会互相误命中。
_MGD_BOTTOM_ROI = (0, 990, 720, 290)
_CHALLENGE_TEXT = "挑战"
_FORMATION_START_TEXT = "开始战斗"
_WIN_CONTINUE_TEXT = "点击空白处继续"
_WIN_CONFIRM_TEXT = "确认"
# 战斗内「推进一回合」的按钮，按优先级取第一个识别到的（跳过回合 > 回合开始 > 奥义释放）：
# 「跳过回合」是项目里既有的战斗按钮语义（战斗设置里的同名开关），出现即优先点它。
_BATTLE_ADVANCE_TEXTS = ("跳过回合", "回合开始", "奥义释放")
# 战斗推进的有界上限与节奏：一场战斗正常几十秒，留足余量但不无限点。
_BATTLE_MAX_STEPS = 60
_BATTLE_STEP_SETTLE = 1.2
# ---- 助战选择（元素自律机械点「挑战」后会先弹这个，实测）----
# 弹窗标题「助战选择」box (289,222,138,39)；点列表第一条（按 Rank 行 y 升序）即可进编队页。
_SUPPORT_TITLE_ROI = (250, 195, 220, 75)
_SUPPORT_TITLE_TEXT = "助战选择"
_SUPPORT_LIST_ROI = (0, 360, 720, 820)
_SUPPORT_ENTRY_OFFSET = 30
# ---- 战斗结算后可能弹出的「好友申请」确认框（实测）----
# 用了陌生玩家的助战会弹「是否向该名玩家发送好友申请」，必须点「取消」、绝不点「发送申请」。
_FRIEND_CANCEL_ROI = (120, 815, 160, 80)
_FRIEND_CANCEL_TEXT = "取消"
# ---- 「机神降临!!」挑战邀请弹窗（点探索可能遇到，实测截图 720x1280 原生）----
# 正文「挑战机神可以获得大量积分，是否前往挑战?」(99,628,509,30)、「取消」(177,801,66,35)、
# 「前往」(477,801,60,36)。处理：点「取消」不前往（前往会进机神战斗/耗挑战次数）。
# ⚠️ 正文是长句，`_find_in` 是全等匹配抓不住，用 `_find_contains` 认稳定尾段「是否前往挑战」。
_MGD_DIALOG_BODY_ROI = (60, 600, 600, 80)
_MGD_DIALOG_BODY_TEXT = "是否前往挑战"
_MGD_DIALOG_CANCEL_ROI = (130, 780, 160, 70)
_MGD_DIALOG_CANCEL_TEXT = "取消"

# 单次点击后的等待：界面刷新 + 数字回落
_CLICK_SETTLE = 1.5
# 硬上限：即使读数一直不降也不会无限点（实测一次解锁是 10 次量级）
_MAX_CLICKS = 15
# 连续这么多次点击读数都没下降 → 判定"点了没反应"，停止（避免白耗道具）
_MAX_NO_PROGRESS = 3


def _flag_enabled(context: Context, node_name: str) -> bool:
    """读开关载体节点的 enabled。

    ⚠️ 节点读不到（未预定义）时返回 False：本节点会**消耗探索次数/道具**，
    宁可整段跳过并告警，也不要在接线不闭环的情况下擅自消费。
    """
    data = context.get_node_data(node_name)
    if not data or "enabled" not in data:
        logger.warning("机神降临：读不到开关节点 {}（未预定义？），按关闭处理、不点任何东西。", node_name)
        return False
    return bool(data.get("enabled"))


def _read_unlock_remaining(context: Context) -> int | None:
    """读「探索N次开启自动探索」里的 N；提示不存在（= 已解锁）返回 None。"""
    for text, _box in _ocr(context, _HINT_ROI):
        match = _HINT_PATTERN.search(text.replace(" ", ""))
        if match:
            return int(match.group(1))
    return None


def _find_explore_button(context: Context):
    """在当前画面找到「探索」按钮的识别框（找不到返回 None）。"""
    for text, box in _ocr(context, _EXPLORE_ROI):
        if text.strip() == _EXPLORE_TEXT:
            return box
    return None


def _find_in(context: Context, roi, wanted: str):
    """在 ROI 里找**完全等于** wanted 的文本，返回它的识别框；找不到返回 None。"""
    for text, box in _ocr(context, roi):
        if text.strip() == wanted:
            return box
    return None


def _find_contains(context: Context, roi, wanted: str):
    """在 ROI 里找**包含** wanted 的文本，返回它的识别框；找不到返回 None。

    用于长句门控（如机神邀请弹窗正文）：整串全等匹配容易被标点/空格的 OCR 抖动打断，
    认稳定片段更稳。⚠️ 只做门控定位，点击目标仍要用独立的控件识别（别点在句子中间）。
    """
    for text, box in _ocr(context, roi):
        if wanted in text.replace(" ", ""):
            return box
    return None


def _dismiss_machine_god_dialog(context: Context) -> bool:
    """「机神降临!!」挑战邀请弹窗在场时点「取消」关掉它；没有弹窗时是零开销 no-op。

    实测（720x1280 原生截图）：正文「挑战机神可以获得大量积分，是否前往挑战?」(99,628,509,30)、
    「取消」(177,801,66,35)、「前往」(477,801,60,36)。点「取消」**不前往**——
    前往会进入机神战斗并消耗挑战次数，任务流程里没有这一步。
    认不到「取消」就不点任何东西（绝不盲点），返回 False 交给上层按无进展处理。
    """
    if _find_contains(context, _MGD_DIALOG_BODY_ROI, _MGD_DIALOG_BODY_TEXT) is None:
        return False
    cancel = _find_in(context, _MGD_DIALOG_CANCEL_ROI, _MGD_DIALOG_CANCEL_TEXT)
    if cancel is None:
        logger.warning("机神降临：看到机神挑战邀请弹窗但认不到「取消」，不点任何东西。")
        return False
    logger.info("机神降临：遇到机神挑战邀请弹窗，点「取消」不前往。")
    _click_box(context, cancel)
    time.sleep(_CLICK_SETTLE)
    return True


def _click_box(context: Context, box) -> None:
    """点识别框中心（不写死坐标）。"""
    context.tasker.controller.post_click(_box_center_x(box), _box_center_y(box)).wait()


def _wait_for(context: Context, roi, wanted: str, timeout: float = _PANEL_TIMEOUT):
    """有界等待某文本出现，返回其识别框；超时返回 None。"""
    deadline = time.time() + timeout
    while True:
        box = _find_in(context, roi, wanted)
        if box is not None:
            return box
        if time.time() >= deadline:
            return None
        time.sleep(_PANEL_POLL)


def _parse_int(pattern: str, texts) -> int | None:
    """在一组 OCR 文本里按正则抓第一个整数。"""
    for text in texts:
        match = re.search(pattern, text.replace(" ", ""))
        if match:
            return int(match.group(1))
    return None


def _pick_support(context: Context) -> bool:
    """在「助战选择」弹窗里点最上面一条助战（按 Rank 行 y 升序），点完应进编队页。

    ⚠️ OCR 的 all_results 顺序不保证自上而下，必须按 y 升序取第一条；
    点第一条用「Rank 行中心 y + 偏移」落到条目热区（同项目 PickSupport 的实测结论）。
    """
    title = _find_in(context, _SUPPORT_TITLE_ROI, _SUPPORT_TITLE_TEXT)
    if title is None:
        return False
    ranks = sorted(
        (box for text, box in _ocr(context, _SUPPORT_LIST_ROI) if "Rank" in text),
        key=_box_center_y,
    )
    if not ranks:
        logger.warning("机神降临：助战选择页没识别到任何助战条目。")
        return False
    box = ranks[0]
    x = _box_center_x(box)
    y = _box_center_y(box) + _SUPPORT_ENTRY_OFFSET
    logger.info("机神降临：点助战选择列表第一条（Rank 行 {}，点 ({}, {})）。", box, x, y)
    context.tasker.controller.post_click(x, y).wait()
    return True


def _run_battle(context: Context, *, post_roi, post_text: str) -> bool:
    """打完一整场「挑战」：挑战 →（助战选择）→ 开始战斗 → 战斗 → 继续 → 确认 →
    （好友申请取消）→ 回到原页面；成功判据是 post_text 在 post_roi 重新出现。

    与「点探索后遇到挑战」和「元素自律机械」两条链同形，公用这一段：
      1. 底部「挑战」→ 点它；
      2. 若弹出「助战选择」→ 点第一条进编队页（没有则说明直接进编队页）；
      3. 等「开始战斗」→ 点它；
      4. 有界推进战斗（跳过回合/回合开始/奥义释放，每步 1.2s，至多 60 步）；
      5. 「点击空白处继续」→「确认」；
      6. 若结算后弹「好友申请」→ 点「取消」（绝不发送）；
      7. 后置观测：post_text 在 post_roi 出现才算成功（认不到返回 False，不盲点）。

    每步有界等待；任何一步认不到都返回 False，由上层按"无进展"处理。
    """
    challenge = _find_in(context, _MGD_BOTTOM_ROI, _CHALLENGE_TEXT)
    if challenge is None:
        return False
    _click_box(context, challenge)
    time.sleep(_CLICK_SETTLE)

    # 助战选择（元素自律机械实测必弹；探索挑战不弹）→ 选第一条
    if _wait_for(context, _SUPPORT_TITLE_ROI, _SUPPORT_TITLE_TEXT, 3) is not None:
        if not _pick_support(context):
            return False
        time.sleep(_CLICK_SETTLE)

    start = _wait_for(context, _MGD_BOTTOM_ROI, _FORMATION_START_TEXT, 15)
    if start is None:
        logger.warning("机神降临：没等到编队页「开始战斗」，放弃处理本场挑战。")
        return False
    _click_box(context, start)
    time.sleep(_CLICK_SETTLE)

    for _step in range(_BATTLE_MAX_STEPS):
        if (
            _find_in(context, _MGD_BOTTOM_ROI, _WIN_CONTINUE_TEXT) is not None
            or _find_in(context, _MGD_BOTTOM_ROI, _WIN_CONFIRM_TEXT) is not None
        ):
            break
        for text in _BATTLE_ADVANCE_TEXTS:
            box = _find_in(context, _MGD_BOTTOM_ROI, text)
            if box is not None:
                _click_box(context, box)
                break
        time.sleep(_BATTLE_STEP_SETTLE)
    else:
        logger.warning("机神降临：战斗推进超过 {} 步仍未结束，放弃处理本场挑战。", _BATTLE_MAX_STEPS)
        return False

    box = _wait_for(context, _MGD_BOTTOM_ROI, _WIN_CONTINUE_TEXT, 10)
    if box is not None:
        _click_box(context, box)
        time.sleep(_CLICK_SETTLE)
    box = _wait_for(context, _MGD_BOTTOM_ROI, _WIN_CONFIRM_TEXT, 10)
    if box is not None:
        _click_box(context, box)
        time.sleep(_CLICK_SETTLE)

    # 好友申请弹窗（用了陌生助战会弹）→ 取消；绝不给别的玩家发申请
    if _wait_for(context, _FRIEND_CANCEL_ROI, _FRIEND_CANCEL_TEXT, 3) is not None:
        logger.info("机神降临：结算后弹出好友申请框，点「取消」。")
        _click_box(context, _find_in(context, _FRIEND_CANCEL_ROI, _FRIEND_CANCEL_TEXT))
        time.sleep(_CLICK_SETTLE)

    if _wait_for(context, post_roi, post_text, 10) is None:
        logger.warning("机神降临：挑战打完后没回到预期页面（认不到「{}」）。", post_text)
        return False
    return True


@AgentServer.custom_action("MachineGodDescentUnlockAutoExplore")
class MachineGodDescentUnlockAutoExplore(CustomAction):
    """按需解锁机神降临的「自动探索」。

    流程：
      1. 读任务设置「自动探索领取」开关：关掉 → 整段跳过（no-op，不点任何东西）；
      2. 读解锁进度「探索N次开启自动探索」：
         · 读不到 → 认为**已经解锁**（该提示只在锁着时出现）→ 不点、成功；
         · 读到 N → 点「探索」按钮，每点一次重读进度并要求 N 递减；
      3. 有界：最多 `_MAX_CLICKS` 次；连续 `_MAX_NO_PROGRESS` 次读数不降就停。

    ⚠️ 点击「探索」是**消耗性**动作（消耗探索次数/道具），所以只在开关开启时才点。
    ⚠️ 已知限制：用户"已解锁"时界面长什么样**尚未观测**，这里用"提示文字消失"当判据；
       该分支的动作是安全 no-op（不点），即便判错也不会误操作。
    ⚠️ 只点识别到的按钮框中心，不写死坐标。

    Examples:
        `custom_action_param`::

            {}   # 本动作不需要参数
    """

    def _clear_challenge(self, context: Context) -> bool:
        """打完「点探索后遇到的挑战」；确认回到机神降临页返回 True，否则 False。

        与元素自律机械那场**完全同形**，统一走 `_run_battle`，后置观测锚点用
        机神降临页的「探索」按钮（打完应回到机神降临页、底部重新出现「探索」）。
        """
        if _find_in(context, _MGD_BOTTOM_ROI, _CHALLENGE_TEXT) is None:
            return False
        logger.info("机神降临：点「探索」后遇到了挑战，开始处理这一场。")
        return _run_battle(context, post_roi=_EXPLORE_ROI, post_text=_EXPLORE_TEXT)

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        if not _flag_enabled(context, _AUTO_EXPLORE_FLAG):
            logger.info("机神降临：任务设置「自动探索领取」未开启，跳过自动探索解锁。")
            return CustomAction.RunResult(success=True)

        remaining = _read_unlock_remaining(context)
        if remaining is None:
            logger.info("机神降临：没有「探索N次开启自动探索」提示 → 自动探索已解锁，本段无需处理。")
            return CustomAction.RunResult(success=True)

        logger.info("机神降临：自动探索仍锁着（还需探索 {} 次），开始点「探索」。", remaining)
        clicked = 0
        no_progress = 0
        while clicked < _MAX_CLICKS:
            button = _find_explore_button(context)
            if button is None:
                logger.error("机神降临：认不到「探索」按钮，停止解锁（已点 {} 次）。", clicked)
                return CustomAction.RunResult(success=False)
            context.tasker.controller.post_click(_box_center_x(button), _box_center_y(button)).wait()
            clicked += 1
            time.sleep(_CLICK_SETTLE)

            now = _read_unlock_remaining(context)
            if now is None:
                logger.info("机神降临：自动探索已解锁（共点 {} 次「探索」）。", clicked)
                return CustomAction.RunResult(success=True)
            if now >= remaining:
                # 读数没降 —— 很可能是这次点「探索」触发了弹窗/挑战（实测两种：机神邀请弹窗、挑战）。
                # 先试着关掉机神邀请弹窗（点「取消」），再试着把挑战打掉；都不行才按"无进展"计数。
                if _dismiss_machine_god_dialog(context):
                    after = _read_unlock_remaining(context)
                    if after is None:
                        logger.info("机神降临：自动探索已解锁（共点 {} 次「探索」，途中关掉机神邀请）。", clicked)
                        return CustomAction.RunResult(success=True)
                    no_progress = 0
                    remaining = after
                    continue
                if self._clear_challenge(context):
                    after = _read_unlock_remaining(context)
                    if after is None:
                        logger.info("机神降临：自动探索已解锁（共点 {} 次「探索」，途中清掉挑战）。", clicked)
                        return CustomAction.RunResult(success=True)
                    no_progress = 0
                    remaining = after
                    continue
                no_progress += 1
                logger.warning("机神降临：第 {} 次点「探索」后进度没变（仍显示 {} 次），疑似点击无效。", clicked, now)
                if no_progress >= _MAX_NO_PROGRESS:
                    logger.warning("机神降临：连续 {} 次没有任何进展，停止解锁以免白耗道具。", no_progress)
                    return CustomAction.RunResult(success=True)
            else:
                no_progress = 0
                remaining = now

        logger.warning("机神降临：点满上限 {} 次仍未解锁（还需 {} 次），交给上层处理。", _MAX_CLICKS, remaining)
        return CustomAction.RunResult(success=True)


@AgentServer.custom_action("MachineGodDescentAutoExploreSetup")
class MachineGodDescentAutoExploreSetup(CustomAction):
    """自动探索本体（用户第三段需求）：

      1. 若**已在自动探索中**（页面上是「停止自动探索」）→ 先停止：点它 → 面板确认「停止探索」
         （面板原文：停止会自动获得已获得的奖励 ⇒ 奖励不丢）；
      2. 点右下角「自动探索」打开设置面板；
      3. **校验时长**：面板显示「N小时」与「可用/总量」侦察工具，每 1 小时消耗 60 个，
         因此可用最大值 = `min(12, 可用 ÷ 60)`。用户已确认「默认即最大值」，所以这里只**读出来校验**
         （相等记 info，不等记 warning），不拖滑块——滑块拖拽几何未观测，拖错可能把时长拖成 0；
      4. 点「开启探索」并**后置观测**：页面上应出现「停止自动探索」才算真的开启成功。

    ⚠️ 会**消耗侦察工具**（4 小时 = 240 个）。因此第 0 步先读任务设置「自动探索领取」开关，
       关闭或读不到节点就整段跳过（不消费）。
    ⚠️ 只点识别到的控件框中心，不写死坐标；每步都有界等待，认不到就明确失败/告警，不盲目连点。

    Examples:
        `custom_action_param`::

            {}   # 本动作不需要参数
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        if not _flag_enabled(context, _AUTO_EXPLORE_FLAG):
            logger.info("机神降临：任务设置「自动探索领取」未开启，跳过自动探索（不消耗侦察工具）。")
            return CustomAction.RunResult(success=True)

        # ---- 0. 机神挑战邀请弹窗在场就先关掉（挡住下面的控件识别）----
        if _dismiss_machine_god_dialog(context):
            logger.info("机神降临：进页时有机神挑战邀请弹窗，已点「取消」关闭。")

        # ---- 1. 已在自动探索中 → 先停止 ----
        if _find_in(context, _STOP_RUNNING_ROI, _STOP_RUNNING_TEXT) is not None:
            logger.info("机神降临：已在自动探索中，按流程先停止。")
            _click_box(context, _find_in(context, _STOP_RUNNING_ROI, _STOP_RUNNING_TEXT))
            time.sleep(_CLICK_SETTLE)
            confirm = _wait_for(context, _STOP_PANEL_CONFIRM_ROI, _STOP_PANEL_CONFIRM_TEXT)
            if confirm is None:
                logger.error("机神降临：点「停止自动探索」后没等到「停止探索」确认键，停止本次流程。")
                return CustomAction.RunResult(success=False)
            _click_box(context, confirm)
            time.sleep(_CLICK_SETTLE)
            if _wait_for(context, _AUTO_BUTTON_ROI, _AUTO_BUTTON_TEXT) is None:
                logger.error("机神降临：确认停止后没看到「自动探索」按钮，无法确认已停止。")
                return CustomAction.RunResult(success=False)
            logger.info("机神降临：已停止自动探索（奖励按面板说明保留）。")
        else:
            logger.info("机神降临：当前不在自动探索中，直接进入设置。")

        # ---- 2. 打开自动探索设置面板 ----
        button = _find_in(context, _AUTO_BUTTON_ROI, _AUTO_BUTTON_TEXT)
        if button is None:
            logger.error("机神降临：认不到「自动探索」按钮，停止本次流程。")
            return CustomAction.RunResult(success=False)
        _click_box(context, button)
        confirm = _wait_for(context, _PANEL_CONFIRM_ROI, _PANEL_CONFIRM_TEXT)
        if confirm is None:
            logger.error("机神降临：点「自动探索」后没等到「开启探索」面板，停止本次流程。")
            return CustomAction.RunResult(success=False)

        # ---- 3. 校验时长 = 可用最大值（只读，不拖滑块）----
        hours = _parse_int(r"(\d+)小时", [text for text, _box in _ocr(context, _DURATION_ROI)])
        tools = _parse_int(r"(\d+)/", [text for text, _box in _ocr(context, _TOOLS_ROI)])
        if hours is None or tools is None:
            logger.warning("机神降临：认不出时长/侦察工具读数，按面板当前值开启（读不出就不猜）。")
        else:
            expected = min(_MAX_HOURS, tools // _TOOLS_PER_HOUR)
            if hours == expected:
                logger.info(
                    "机神降临：时长校验通过 {} 小时 = 可用最大值（侦察工具 {}，每 {} 个 1 小时）。",
                    hours,
                    tools,
                    _TOOLS_PER_HOUR,
                )
            elif hours < expected:
                logger.warning(
                    "机神降临：面板时长 {} 小时 < 可用最大值 {} 小时（默认不再是最大值？），"
                    "本次仍按界面当前值开启（不拖滑块）。",
                    hours,
                    expected,
                )
            else:
                logger.warning(
                    "机神降临：面板时长 {} 小时 > 计算出的可用最大值 {} 小时，按界面值开启。",
                    hours,
                    expected,
                )

        # ---- 4. 开启探索 + 后置观测 ----
        _click_box(context, confirm)
        time.sleep(_CLICK_SETTLE)
        if _wait_for(context, _STOP_RUNNING_ROI, _STOP_RUNNING_TEXT) is None:
            logger.error("机神降临：点「开启探索」后没看到「停止自动探索」，无法确认已开启。")
            return CustomAction.RunResult(success=False)
        logger.info("机神降临：自动探索已开启。")
        return CustomAction.RunResult(success=True)


# ---- 第四段：挑战「元素自律机械」（用户提供流程 + 实测截图）--------------------------
# 流程：机神降临界面**左上角**「元素自律机械」→ 进入挑战页 → 「挑战」→ 助战选择 →「开始战斗」
#   → 本活动**没有**「挑战下一关」，所以战斗结束点「确认」回到自律机械挑战页
#   → 按设置次数重复（打满或次数不足就停）→ 退回机神降临页（下一段机神降临挑战要从那里进）。
# 其中「挑战 → 助战选择 → 编队开始战斗 → 战斗 → 战斗胜利继续 → 确认 →（好友申请取消）
# → 回到本页」这一段与「点探索后遇到挑战」**同形**，统一复用 `_run_battle`。
_ELEMENT_MACHINE_FLAG = "MachineGodDescent_Flag_ElementMachineChallenge"
# 左上角入口（实测：「元素自律机械」(15,214,145,32)，其下是「次数：10」(43,280,93,28)）
_ELEMENT_ENTRY_ROI = (0, 190, 220, 70)
_ELEMENT_ENTRY_TEXT = "元素自律机械"
# 挑战页的**剩余可挑战次数**读数：实测「挑战次数：8」box (291,1009,133,32)，
# 在底部「挑战」按钮正上方；与机神降临页左上角那个「次数：N」不是同一处。
_ELEMENT_REMAIN_ROI = (250, 980, 230, 70)
_ELEMENT_REMAIN_PATTERN = re.compile(r"挑战次数\s*[:：]\s*(\d+)")


# 自律机械挑战页左下角返回（与公共返回节点 BackToMainHub_Back 实测同一枚图标 (52,1226,45,29)）。
_MGD_BACK_POINT = (74, 1240)


def _back_to_mgd_page(context: Context, tries: int = 3) -> bool:
    """从「元素自律机械」挑战页退回机神降临页；不在挑战页时零开销 no-op。

    为什么需要它：任务设置里两段挑战都开启时，**自律机械优先**（用户要求），
    而下一段「机神降临挑战」要从机神降临页的「挑战」/「挑战机神」入口进 ——
    停在自律机械挑战页会让它认不到入口直接判失败。所以本段结束必须把页面交还。

    判据用挑战页**独有**的底部读数「挑战次数：N」（机神降临页那里写作「次数：N」，
    左上角还有「元素自律机械」入口），读不到就认为已经不在挑战页，绝不盲点。
    """
    for _ in range(tries):
        if _read_element_remaining(context) is None:
            break
        context.tasker.controller.post_click(*_MGD_BACK_POINT).wait()
        time.sleep(_CLICK_SETTLE)
    if _read_element_remaining(context) is not None:
        logger.warning("机神降临：自律机械段结束后仍停在挑战页（还能读到「挑战次数」），机神降临挑战段可能认不到入口。")
        return False
    if _find_in(context, _ELEMENT_ENTRY_ROI, _ELEMENT_ENTRY_TEXT) is None:
        logger.warning("机神降临：已退出自律机械挑战页，但没看到「元素自律机械」入口，机神降临挑战段可能认不到入口。")
        return False
    return True


def _read_element_remaining(context: Context) -> int | None:
    """读挑战页底部「挑战次数：N」（剩余可挑战次数）；读不到返回 None（不猜）。

    用户明确：任务设置里的「自律机械挑战次数」是**要打的次数**（times），
    这里的「挑战次数」是**剩余可挑战次数**——只用来判断"还能不能打"，绝不参与 times 语义。
    """
    for text, _box in _ocr(context, _ELEMENT_REMAIN_ROI):
        match = _ELEMENT_REMAIN_PATTERN.search(text.replace(" ", ""))
        if match:
            return int(match.group(1))
    return None


@AgentServer.custom_action("MachineGodDescentElementMachineChallenge")
class MachineGodDescentElementMachineChallenge(CustomAction):
    """按设置次数挑战「元素自律机械」。

    流程（用户给出 + 实测逐跳印证）：
      1. 机神降临界面左上角点「元素自律机械」进入挑战页（已在挑战页时点不到、直接跳过）；
      2. 循环 times 次：
         · 先读挑战页底部「挑战次数：N」（**剩余可挑战次数**）——N=0 就提前结束；
         · 打一场：挑战 → 助战选择(第一条) → 编队「开始战斗」→ 战斗 → 「点击空白处继续」
           →「确认」→（好友申请「取消」）→ 回到挑战页（统一走 `_run_battle`）；
      3. 打满 times 或剩余次数不足即结束；**结束时把页面退回机神降临页**，交给下一段
         `MachineGodDescent_GodChallenge`（自律机械优先于机神降临）。

    ⚠️ times 来自任务设置「自律机械挑战次数」（1-99999），是**要打的次数**；
       挑战页读到的「挑战次数：N」是**剩余可挑战次数**，只用来判断还能不能打。
    ⚠️ 会真实消耗挑战次数；任务设置「挑战元素自律机械」关闭或节点读不到时整段跳过（不消费）。

    Examples:
        `custom_action_param`::

            {
                "times": 5
            }
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        if not _flag_enabled(context, _ELEMENT_MACHINE_FLAG):
            logger.info("机神降临：任务设置「挑战元素自律机械」未开启，跳过元素自律机械段。")
            return CustomAction.RunResult(success=True)

        try:
            params = parse_params(argv.custom_action_param)
        except ValueError as error:
            logger.error("机神降临：custom_action_param 解析失败：{}", error)
            return CustomAction.RunResult(success=False)
        times = _parse_times(params.get("times"))
        if times is None:
            logger.error("机神降临：挑战次数非法（需 1-99999），跳过元素自律机械段。")
            return CustomAction.RunResult(success=True)

        # 1. 进入挑战页（点左上角入口；已在挑战页时这里认不到，不影响后续）
        entry = _find_in(context, _ELEMENT_ENTRY_ROI, _ELEMENT_ENTRY_TEXT)
        if entry is not None:
            logger.info("机神降临：点左上角「元素自律机械」进入挑战页。")
            _click_box(context, entry)
            time.sleep(_CLICK_SETTLE)
        else:
            logger.info("机神降临：没看到左上角「元素自律机械」入口（可能已在挑战页）。")

        # 2. 按设置次数循环，剩余可挑战次数不足就提前结束
        done = 0
        for index in range(times):
            remaining = _read_element_remaining(context)
            if remaining is not None and remaining <= 0:
                logger.info("机神降临：元素自律机械剩余可挑战次数已用尽（挑战次数：{}），提前结束。", remaining)
                break
            if not _run_battle(context, post_roi=_MGD_BOTTOM_ROI, post_text=_CHALLENGE_TEXT):
                logger.warning(
                    "机神降临：第 {} 次挑战没能完成（认不到「挑战」或战斗未收尾），停止元素自律机械段。",
                    index + 1,
                )
                break
            done += 1
            logger.info("机神降临：元素自律机械第 {}/{} 次挑战完成。", done, times)

        logger.info("机神降临：元素自律机械段结束，共完成 {}/{} 次。", done, times)
        # 收回页面：下一段是机神降临挑战（自律机械优先），它要在机神降临页认「挑战」/「挑战机神」入口。
        # 停在自律机械挑战页会让它认不到入口直接放弃，所以这里必须退回机神降临页。
        _back_to_mgd_page(context)
        return CustomAction.RunResult(success=True)


# ---- 第五段：挑战「机神降临」（用户提供全流程截图 11-21，实机 OCR 逐跳印证）-----------
# 流程：黄金之国页点「挑战」蓝圆钮 (569,854) **直接进机神挑战页（无邀请框）**；首次进页
#   可能没有「挑战」钮 → 点「挑战机神」(596,1230) /「机神降临!!!」横幅 (246,320) → 弹
#   「机神降临!!」邀请框（正文「是否前往挑战」+「剩余挑战次数：N」，顺带读数做次数门控）
#   →「前往」→ 机神挑战页（LV.150 原生机神）→「创建房间」→「助战选择」(复用 _pick_support)
#   → 编队页「确定」→ 房间页「开始战斗」→「是否开始战斗?」确认框「确定」→ 进入战斗
#   → 有界推进回合（跳过回合/回合开始/奥义释放，同 `_run_battle`；机神 HP 归零即胜利）
#   →「战斗胜利」结算**三屏**（共享排名 → 经验 → 材料奖励）：连点「点击空白处继续」，
#   最后点「确认」→（好友申请「取消」）→ 回到黄金之国页。
# ⚠️ 胜利结算标题是「战斗胜利」、撤退/时间到是「作战失败」——两者文案不同；
#    结算按钮全部落在 `_MGD_BOTTOM_ROI` 底部横带（「点击空白处继续」/「确认」/「撤退」），
#    与元素自律机械同形，复用同一套底部识别，不依赖结算标题。
_GOD_CHALLENGE_FLAG = "MachineGodDescent_Flag_GodChallenge"
# 入口一（图11）：黄金之国页「挑战」蓝圆钮（实测 box (569,854,45,26)）——点它**直接进
# 机神挑战页，不弹邀请框**。实测该按钮白字叠在蓝色圆钮+装饰图标上，ROI 太小会被图标
# 干扰成乱码：(500,700,220,400) 竖带内恰好只认出「挑战」一个结果（score 1.0，最干净）；
# 区域内无其他「挑战」文本（「挑战机神」在 y 1230、「机械构造」在 y 430，均不在带内）。
_GOD_FIGHT_ROI = (500, 700, 220, 400)
_GOD_FIGHT_TEXT = "挑战"
# 入口二：右下角「挑战机神」按钮（实测 box (596,1230,98,30)）→ 弹邀请框
_GOD_ENTRY_ROI = (560, 1195, 160, 70)
_GOD_ENTRY_TEXT = "挑战机神"
# 入口三：首次进页面的「机神降临!!!」横幅（实测 box (246,320,266,84)），contains 认「机神降临」
_GOD_BANNER_ROI = (150, 280, 420, 140)
_GOD_BANNER_TEXT = "机神降临"
# 邀请框「前往」按钮（实测 box (477,801,60,36)）；正文「是否前往挑战」复用 _MGD_DIALOG_*
# ⚠️ ROI 只框「前往」自身，避免和「剩余挑战次数」读数（上方 y 764-790）重叠。
_GOD_GO_ROI = (450, 790, 110, 60)
_GOD_GO_TEXT = "前往"
# 邀请框「剩余挑战次数：N」（实测 box (419,764,175,26)）
_GOD_REMAIN_ROI = (360, 740, 240, 60)
_GOD_REMAIN_PATTERN = re.compile(r"剩余挑战次数\s*[:：]\s*(\d+)")
# 机神挑战页「创建房间」按钮（实测 box (450,1034,129,39)）
_GOD_CREATE_ROOM_ROI = (400, 1000, 220, 90)
_GOD_CREATE_ROOM_TEXT = "创建房间"
# 编队页「确定」按钮（实测 box (325,1025,72,41)）
_GOD_FORMATION_CONFIRM_ROI = (280, 1000, 160, 80)
_GOD_FORMATION_CONFIRM_TEXT = "确定"
# 房间页「开始战斗」按钮（实测 box (435,1181,123,37)）
_GOD_ROOM_START_ROI = (380, 1150, 220, 80)
_GOD_ROOM_START_TEXT = "开始战斗"
# 开始战斗确认框「确定」按钮（实测 box (477,834,76,46)）
_GOD_START_CONFIRM_ROI = (430, 800, 160, 80)
_GOD_START_CONFIRM_TEXT = "确定"
# 编队页「确定」→ 房间页「开始战斗」的过渡等待（用户要求加的超时判断点）：
# 超过这个秒数还看不到「开始战斗」，就当过渡失败，交给公共异常处理流程（Exception_Start）。
_GOD_FORMATION_TO_ROOM_TIMEOUT = 15.0
# 失败兜底（时间到/全员阵亡）：「作战失败」标题（实测 box (240,190,239,71)）+
# 底部「撤退」按钮（实测 box (337,1202,61,31)，在 _MGD_BOTTOM_ROI 内）。
_GOD_LOSE_RESULT_ROI = (150, 150, 420, 140)
_GOD_LOSE_TEXT = "作战失败"
# 战斗推进步数上限：机神战一场最长约 39 分钟（战斗剩余时间 39:xx），每步 settle 1.5s +
# OCR ≈ 2.5s，给 1000 步（≈40 分钟）覆盖最坏情况；战斗结束以底部出现
# 「点击空白处继续」/「确认」（胜利结算）或「作战失败」标题（失败结算）为准。
_GOD_BATTLE_MAX_STEPS = 1000
_GOD_BATTLE_STEP_SETTLE = 1.5
# 胜利结算屏数上限（实测三屏：共享排名 → 经验 → 材料奖励；留余量到 5）
_GOD_RESULT_MAX_SCREENS = 5


def _read_god_remaining(context: Context) -> int | None:
    """读邀请框「剩余挑战次数：N」（剩余可挑战次数）；读不到返回 None（不猜）。"""
    for text, _box in _ocr(context, _GOD_REMAIN_ROI):
        match = _GOD_REMAIN_PATTERN.search(text.replace(" ", ""))
        if match:
            return int(match.group(1))
    return None


def _create_god_room(context: Context) -> bool:
    """「创建房间」模块：从机神挑战页创建房间直到进入战斗。

    流程：机神挑战页「创建房间」→「助战选择」(复用 _pick_support，选第一条)
    → 编队页「确定」→ 房间页「开始战斗」→「是否开始战斗?」确认框「确定」→ 进入战斗。
    成功（已进入战斗）返回 True；任一步认不到即返回 False（不盲点）。

    ⚠️ 本模块是机神降临挑战的前置准备段，从 `_run_god_battle` 抽出独立成模块，
       便于复用与单独调试。
    """
    # 1. 机神挑战页 → 创建房间
    create = _wait_for(context, _GOD_CREATE_ROOM_ROI, _GOD_CREATE_ROOM_TEXT, 15)
    if create is None:
        logger.warning("机神降临：没等到机神挑战页「创建房间」。")
        return False
    _click_box(context, create)
    time.sleep(_CLICK_SETTLE)

    # 2. 助战选择（创建房间后实测必弹）→ 选第一条
    if _wait_for(context, _SUPPORT_TITLE_ROI, _SUPPORT_TITLE_TEXT, 5) is not None:
        if not _pick_support(context):
            return False
        time.sleep(_CLICK_SETTLE)

    # 3. 编队页 → 确定
    confirm = _wait_for(context, _GOD_FORMATION_CONFIRM_ROI, _GOD_FORMATION_CONFIRM_TEXT, 15)
    if confirm is None:
        logger.warning("机神降临：没等到编队页「确定」。")
        return False
    _click_box(context, confirm)
    time.sleep(_CLICK_SETTLE)

    # 4. 房间页 → 开始战斗
    #    这里是「助战选择完成、点编队『确定』→ 下一个节点」之间的**超时判断点**（用户要求）：
    #    超过 _GOD_FORMATION_TO_ROOM_TIMEOUT 还看不到房间页（卡编队页 / 弹窗盖住 / 断线 / 加载不出来），
    #    就进**公共异常处理流程** Exception_Start，再返回 False 结束本次（不盲点「开始战斗」）。
    start = _wait_for(context, _GOD_ROOM_START_ROI, _GOD_ROOM_START_TEXT, _GOD_FORMATION_TO_ROOM_TIMEOUT)
    if start is None:
        logger.error(
            "机神降临：点编队「确定」后 {} 秒内没进到房间页「开始战斗」，进入公共异常处理流程。",
            _GOD_FORMATION_TO_ROOM_TIMEOUT,
        )
        context.run_task("Exception_Start")
        return False
    _click_box(context, start)
    time.sleep(_CLICK_SETTLE)

    # 5. 「是否开始战斗?」确认框 → 确定（有才点；没有说明直接进了战斗）
    start_confirm = _wait_for(context, _GOD_START_CONFIRM_ROI, _GOD_START_CONFIRM_TEXT, 10)
    if start_confirm is not None:
        _click_box(context, start_confirm)
        time.sleep(_CLICK_SETTLE)
    return True


def _run_god_battle(context: Context) -> str:
    """打一场机神降临挑战，返回 'done' / 'exhausted' / 'fail'。

    流程（用户全流程截图 11-21 逐跳印证）：
      1. 黄金之国页点「挑战」蓝圆钮**直接进机神挑战页（无邀请框）**；没有则点
         「挑战机神」/「机神降临!!!」横幅 → 邀请框（顺带读剩余挑战次数）→「前往」；
      2. 机神挑战页「创建房间」→「助战选择」(复用 _pick_support) → 编队「确定」
         → 房间「开始战斗」→「是否开始战斗?」确认「确定」→ 进入战斗；
      3. 有界推进回合（同 `_run_battle`：跳过回合/回合开始/奥义释放）；机神 HP 归零
         「战斗胜利」结算三屏（共享排名 → 经验 → 材料奖励）→ 连点「点击空白处继续」
         →「确认」→（好友申请「取消」）→ 回到黄金之国页。
    """
    # 1. 入口：优先「挑战」蓝圆钮（直接进，无邀请框），否则「挑战机神」/横幅（邀请框路径）
    entry_kind = "direct"
    entry = _find_in(context, _GOD_FIGHT_ROI, _GOD_FIGHT_TEXT)
    if entry is None:
        entry_kind = "dialog"
        entry = _find_in(context, _GOD_ENTRY_ROI, _GOD_ENTRY_TEXT)
        if entry is None:
            entry = _find_contains(context, _GOD_BANNER_ROI, _GOD_BANNER_TEXT)
    if entry is None:
        logger.warning("机神降临：认不到机神挑战入口（挑战/挑战机神/机神降临横幅）。")
        return "fail"
    logger.info(
        "机神降临：点机神挑战入口「{}」({} 路径)。",
        entry_kind,
        "直接进" if entry_kind == "direct" else "邀请框",
    )
    _click_box(context, entry)
    time.sleep(_CLICK_SETTLE)

    # 1.5 邀请框路径：等「前往」→ 读剩余次数（不足则关框）→ 点「前往」
    if entry_kind == "dialog":
        go = _wait_for(context, _GOD_GO_ROI, _GOD_GO_TEXT, 10)
        if go is None:
            logger.warning("机神降临：点入口后没等到机神挑战邀请框「前往」。")
            return "fail"
        remaining = _read_god_remaining(context)
        if remaining is not None and remaining <= 0:
            logger.info("机神降临：机神挑战剩余次数已用尽（剩余挑战次数：{}），关邀请框。", remaining)
            _dismiss_machine_god_dialog(context)
            return "exhausted"
        _click_box(context, go)
        time.sleep(_CLICK_SETTLE)

    # 2-6. 「创建房间」模块：机神挑战页创建房间 → 助战 → 编队确定 → 开始战斗 → 确认
    if not _create_god_room(context):
        return "fail"

    # 7. 战斗：有界推进回合（同 `_run_battle` 的推进；机神 HP 归零即胜利）
    for _step in range(_GOD_BATTLE_MAX_STEPS):
        # 胜利/失败结算都以底部按钮或失败标题出现为准
        if (
            _find_in(context, _MGD_BOTTOM_ROI, _WIN_CONTINUE_TEXT) is not None
            or _find_in(context, _MGD_BOTTOM_ROI, _WIN_CONFIRM_TEXT) is not None
        ):
            break
        if _find_in(context, _GOD_LOSE_RESULT_ROI, _GOD_LOSE_TEXT) is not None:
            logger.warning("机神降临：机神战「作战失败」（时间到/全员阵亡），点底部「撤退」退出。")
            exit_box = _find_in(context, _MGD_BOTTOM_ROI, "撤退")
            if exit_box is not None:
                _click_box(context, exit_box)
                time.sleep(_CLICK_SETTLE)
            return "fail"
        for text in _BATTLE_ADVANCE_TEXTS:
            box = _find_in(context, _MGD_BOTTOM_ROI, text)
            if box is not None:
                _click_box(context, box)
                break
        time.sleep(_GOD_BATTLE_STEP_SETTLE)
    else:
        logger.warning("机神降临：战斗推进超过 {} 步仍未结束，放弃本场。", _GOD_BATTLE_MAX_STEPS)
        return "fail"

    # 8. 胜利结算三屏（共享排名 → 经验 → 材料奖励）：连点「点击空白处继续」，最后「确认」
    for _screen in range(_GOD_RESULT_MAX_SCREENS):
        cont = _wait_for(context, _MGD_BOTTOM_ROI, _WIN_CONTINUE_TEXT, 10)
        if cont is None:
            break
        _click_box(context, cont)
        time.sleep(_CLICK_SETTLE)
    confirm_box = _wait_for(context, _MGD_BOTTOM_ROI, _WIN_CONFIRM_TEXT, 10)
    if confirm_box is None:
        logger.warning("机神降临：胜利结算后没等到「确认」按钮。")
        return "fail"
    _click_box(context, confirm_box)
    time.sleep(_CLICK_SETTLE)

    # 9. 好友申请弹窗（用了陌生助战会弹）→ 取消；绝不给别的玩家发申请
    if _wait_for(context, _FRIEND_CANCEL_ROI, _FRIEND_CANCEL_TEXT, 3) is not None:
        logger.info("机神降临：结算后弹出好友申请框，点「取消」。")
        _click_box(context, _find_in(context, _FRIEND_CANCEL_ROI, _FRIEND_CANCEL_TEXT))
        time.sleep(_CLICK_SETTLE)

    # 10. 后置观测：回到黄金之国页（「挑战」重新出现）
    if _wait_for(context, _GOD_FIGHT_ROI, _GOD_FIGHT_TEXT, 10) is None:
        logger.warning("机神降临：机神战结算后没回到黄金之国页（认不到「挑战」）。")
        return "fail"
    return "done"


@AgentServer.custom_action("MachineGodDescentGodChallenge")
class MachineGodDescentGodChallenge(CustomAction):
    """按设置次数挑战「机神降临」。

    流程（用户全流程截图 11-21 逐跳印证）：
      1. 黄金之国页点「挑战」蓝圆钮**直接进机神挑战页**（无邀请框）；没有则点
         「挑战机神」/「机神降临!!!」横幅 → 弹邀请框 → 读「剩余挑战次数：N」（N 用尽则
         关框提前结束）→ 点「前往」进机神挑战页；
      2. 创建房间 → 助战选择(第一条) → 编队「确定」→ 房间「开始战斗」→ 确认「确定」→ 进战斗；
      3. 有界推进回合（跳过回合/回合开始/奥义释放）；机神 HP 归零 →「战斗胜利」结算
         三屏（共享排名 → 经验 → 材料奖励）→ 连点「点击空白处继续」→「确认」；
         时间到/阵亡 →「作战失败」→「撤退」（本场计失败，循环停止）；
      4. 按 times 循环；最后交给上层返回主页。

    ⚠️ times 来自任务设置「机神降临挑战次数」（1-99999），是**要打的次数**；
       邀请框读到的「剩余挑战次数：N」是**剩余可挑战次数**，只用来判断还能不能打
       （「挑战」直进路径弹不出邀请框、读不到该数，此时只受 times 约束）。
    ⚠️ 会真实消耗挑战次数；任务设置「挑战机神降临」关闭或节点读不到时整段跳过（不消费）。
    ⚠️ 一场机神战最长约 39 分钟：pipeline 节点 timeout 需留足余量（见 MachineGodDescent.json）。

    Examples:
        `custom_action_param`::

            {
                "times": 1
            }
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        if not _flag_enabled(context, _GOD_CHALLENGE_FLAG):
            logger.info("机神降临：任务设置「挑战机神降临」未开启，跳过机神降临挑战段。")
            return CustomAction.RunResult(success=True)

        try:
            params = parse_params(argv.custom_action_param)
        except ValueError as error:
            logger.error("机神降临：custom_action_param 解析失败：{}", error)
            return CustomAction.RunResult(success=False)
        times = _parse_times(params.get("times"))
        if times is None:
            logger.error("机神降临：机神挑战次数非法（需 1-99999），跳过机神降临挑战段。")
            return CustomAction.RunResult(success=True)

        done = 0
        for index in range(times):
            outcome = _run_god_battle(context)
            if outcome == "exhausted":
                logger.info("机神降临：机神挑战剩余次数已用尽，机神降临挑战段提前结束。")
                break
            if outcome != "done":
                logger.warning("机神降临：第 {} 次机神挑战没能完成，停止机神降临挑战段。", index + 1)
                break
            done += 1
            logger.info("机神降临：机神降临挑战第 {}/{} 次完成。", done, times)

        logger.info("机神降临：机神降临挑战段结束，共完成 {}/{} 次。", done, times)
        return CustomAction.RunResult(success=True)
