"""「自动挂机 · 主动战斗」的自定义动作（战斗列表 → 支援筛选 → 刷新 → 挑战斗 → 助战 → 打 → 结算）。

流程按用户需求（2026-10-25）与实机 720x1280（真机 127.0.0.1:16512）逐跳印证：

  ① 依据任务设置的（副本 × 难度）勾选，在「支援筛选设定」弹窗里清空并重设筛选
     → 打开「仅显示筛选」，让战斗支援列表只列用户要打的那几种；
  ② 循环「战斗次数」次：
     如在挂机中 → 点「停止挂机」取消（正在打的那一场先打完）
     → 点刷新 → 读列表每一场的**剩余血量条长度 / 参与人数 / 副本名**
     → 按「副本优先级」挑一场（优先进血量多 = 血条最长；优先进血量少 = 血条最短；满员跳过）
     → 点这一场 → 调用助战选择模块（PickSupport，与本项目其它任务同一套偏好）
     → 编队页「开始战斗」→ 校验真的进了战斗画面（进不去就回到"刷新"节点重来）
     → 调用战斗模块（只点「跳过回合」，与副本挑战/玩法挑战同一套语义）
     → 胜利结算：战斗贡献结算页若有可领取的高难限定宝箱且全局「领取高难限定宝箱」开着，
        点宝箱 →「是否领取高难限定宝箱？」→「确定」；关闭则不领（退本确认框点「确定」照样退出）
     → 好友申请点「取消」→ 回到战斗支援列表 → 下一轮。

实机结论（决定下面这些常量）：
  · 战斗列表页工具栏从左到右：支援筛选漏斗 (68,408) / 仅显示筛选开关 (146,408) / 自动挂机齿轮 /
    支援码 / 刷新 (672,408)。漏斗与刷新是纯图标 OCR 认不到 → 用模板匹配
    （image/AutoIdle/SupportFilterButton.png、RefreshButton.png，实测 1.000）。
  · 「仅显示筛选」开启 = 圆钮亮蓝（29x29 内蓝色像素 467），关闭 = 近黑（0）。
  · 支援列表第一条名字在 y≈505，血量条在 y≈550，条距 160；血量条固定起点 x=312、
    满血右端 x=548（237px），**填充右端越短 = 剩余血量越少**。
    条右侧的「BP-3」是橙色文字，不能拿整行暖色最大值当右端（会量到 580+）。
  · 筛出来没有可支援的战斗时列表区显示「暂无可支援的战斗」(226,813,266,36)。
  · 点列表任意一场直接弹「助战选择」（没有二次确认框）；选完助战进编队页，底部「开始战斗」
    (297,1100,121,37)，按钮上方有本场 BP 消耗。
  · 支援战斗打赢后依次是 战斗贡献结算（「点击空白处继续」+ 右侧宝箱块）→ 战斗获得经验结算
    （「点击空白处继续」）→ 战斗获得物品结算（「确认」）→ 回到战斗支援列表。
  · 传说副本的贡献结算页右侧有「高难限定宝箱 / 今日掉落：3/4」和「可领取高难限定宝箱啦！」，
    点宝箱图标弹「是否领取高难限定宝箱？/ 每日宝箱掉落次数：3」，点「确定」后 3/4 → 2/4。
    没领就点物品结算的「确认」会弹「还未领取当前副本的高难限定宝箱，是否退出副本？」。
  · 挂机中点「停止挂机」立刻退出挂机（没有二次确认），**正在打的那一场会继续打完**。
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
from utils.interrupt import is_stopping
from utils.params import parse_params

# 复用「自动挂机」里已经实机验证过的弹窗导航（「支援筛选设定」与「选择要挂机的副本」是同一个
# 组件：标题栏/系列横幅/卡片栅格/难度弹窗/重置/确定 的实测坐标全部一致）。
from .auto_idle import (
    DUNGEONS,
    _already_idling,
    _apply_selection,
    _ocr_lines,
    _read_selection,
    _wait_text,
)
from .auto_idle import (
    _confirm_dialog as _confirm_filter_dialog,
)

# 复用其它任务里已验证的识别/点击基元与战斗、结算、异常处理能力（同一套 UI，不重复实现）。
from .dungeon_challenge import (
    _back_to_hub,
    _dismiss_friend_request,
    _fight,
    _flag_enabled,
    _match_template,
    _on_hub,
    _screencap,
)
from .gameplay_challenge import (
    PickSupport,
    _box_center_x,
    _box_center_y,
    _read_support_prefs,
    _wait_for_battle_presence,
)
from .machine_god_descent import (
    _MGD_BOTTOM_ROI,
    _SUPPORT_TITLE_ROI,
    _SUPPORT_TITLE_TEXT,
    _WIN_CONFIRM_TEXT,
    _WIN_CONTINUE_TEXT,
    _click_box,
    _find_contains,
    _find_in,
    _wait_for,
    handle_away_full,
)

# ---- 任务设置载体节点（interface.json 的选项覆写它们）--------------------------------
_CHEST_FLAG = "Flag_ClaimHardChest"
_HP_PRIORITY_NODE = "AutoIdle_Config_HpPriority"

# 「战斗次数」取值范围（用户要求：1-9999）
MIN_TIMES = 1
MAX_TIMES = 9999

# ---- 战斗列表页（战斗支援）----------------------------------------------------------
# 页面锚点：左上标题「战斗列表」（实测 (14,12,163,50)）。战斗画面里没有这个标题，
# 所以它同时是"是否已经回到列表"的判据。
_LIST_TITLE_ROI = (0, 0, 300, 90)
_LIST_TITLE_TEXT = "战斗列表"
# 列表头「战斗支援」（实测 (62,308,120,33)）：结算浮层盖在列表上时它会被挡住，用来区分"真在列表"。
_LIST_SECTION_ROI = (20, 290, 260, 70)
_LIST_SECTION_TEXT = "战斗支援"
# 主页右下「战斗列表」入口（与 auto_idle 同一处，实测 (603,1100,86,24)）
_HUB_BATTLE_LIST_ROI = (540, 1060, 180, 110)
_HUB_BATTLE_LIST_TEXT = "战斗列表"
_LIST_OPEN_TIMEOUT = 15.0
_SCREEN_SETTLE_TIMEOUT = 20.0  # 取消挂机/战斗结束后等画面落定（黑屏、加载态）

# 工具栏（实测 y≈390~430）
_FUNNEL_ROI = (20, 365, 110, 90)
_FUNNEL_TEMPLATE = "AutoIdle/SupportFilterButton.png"
_FUNNEL_THRESHOLD = 0.8
_REFRESH_ROI = (600, 360, 120, 100)
_REFRESH_TEMPLATE = "AutoIdle/RefreshButton.png"
_REFRESH_THRESHOLD = 0.8
_REFRESH_POINT = (672, 408)  # 模板认不到时的实测兜底点位
_ONLY_FILTER_TEXT = "仅显示筛选"
_ONLY_FILTER_TEXT_ROI = (150, 375, 180, 60)
_ONLY_FILTER_TOGGLE = (146, 408)
_ONLY_FILTER_HALF = 14
_ONLY_FILTER_BLUE_MIN = 100  # 开启态实测蓝色像素 467、关闭态 0

# 「支援筛选设定」弹窗标题（实测 (259,150,201,38)）；与「选择要挂机的副本」是两个不同标题。
_FILTER_TITLE_ROI = (150, 120, 440, 80)
_FILTER_TITLE_TEXT = "支援筛选设定"
_FILTER_OPEN_TIMEOUT = 10.0

# 列表为空时的提示（实测 (226,813,266,36)）
_EMPTY_TEXT = "暂无可支援的战斗"
_EMPTY_ROI = (100, 700, 520, 220)

# 列表条目：血量条固定起点 / 满血右端 / 扫描区
_BAR_START_X = 312
_BAR_FULL_END = 548
_BAR_SPAN = _BAR_FULL_END - _BAR_START_X + 1
_BAR_SCAN_Y = (430, 1175)
_BAR_SCAN_X = (300, 600)
_BAR_MIN_WIDTH = 12
# 条目文字区（名字在血量条上方、人数在下方）：一次 OCR 覆盖整个列表，再按 y 归到每条血量条上
_LIST_TEXT_ROI = (260, 425, 450, 770)
_NAME_MIN_DY = -72
_NAME_MAX_DY = -26
_COUNT_MIN_DY = -8
_COUNT_MAX_DY = 36
_ENTRY_CLICK_DY = -30
_ENTRY_CLICK_X = 400
_PARTICIPANTS_PATTERN = re.compile(r"(\d+)\s*/\s*(\d+)")

# ---- 挂机中（取消挂机）--------------------------------------------------------------
# 「停止挂机」(299,1189,121,37)：挂机中无论是"战斗中"还是"等待下一场"都在这条按钮带上。
_STOP_IDLE_TEXT = "停止挂机"
# 停止按钮实测 (299,1189,121,37)；挂机中战斗画面的浮层排版偶尔把它挤到别的行，所以再给一条宽底带。
_STOP_IDLE_ROIS = ((240, 1140, 240, 130), (0, 1040, 720, 240))
_IDLING_ROI = (0, 780, 720, 460)  # 与 auto_idle 的「自动挂机中/停止挂机」同一带
# 挂机中右下角的「离线挂机」齿轮标签（实测 (626,1229,79,24)）：非挂机态没有这段文字，
# 作为「自动挂机中」/「停止挂机」之外的第三重判据（OCR 偶发漏读前两个时兜住）。
_IDLE_GEAR_ROI = (560, 1190, 160, 85)
_IDLE_GEAR_TEXT = "离线挂机"
_STOP_IDLE_TIMEOUT = 12.0
_STOP_IDLE_RETRIES = 4

# ---- 助战 / 编队 / 战斗 --------------------------------------------------------------
_SUPPORT_OPEN_TIMEOUT = 12.0
_FORMATION_START_ROI = (250, 1060, 220, 100)
_FORMATION_START_TEXT = "开始战斗"
_FORMATION_START_TIMEOUT = 20.0
_STAMINA_ROI = (180, 120, 380, 100)
_STAMINA_TEXT = "补充体力"
# ---- BP（精力）不足 --------------------------------------------------------------
# 点「开始战斗」时 BP 不够会弹**另一套**弹窗，标题是「补充精力」（与「补充体力」不是同一个框）。
# 用户要求：直接用第一个精力补给，不挑档位（"与补充体力逻辑一致"指的是点使用 → 物品详情 → 使用）。
# ⚠️ 三个 ROI 是**按补充体力弹窗的布局推断**的（标题带 / 右侧「使用」列 / 详情右下「使用」），
#    尚未拿到「补充精力」弹窗的实机截图校准；每一步都有识别门控兜底，认不到就放弃本场、绝不盲点。
_ENERGY_TITLE_ROI = (160, 110, 420, 130)
_ENERGY_TITLE_TEXTS = ("补充精力", "恢复精力")
# 点「开始战斗」后等战斗画面（左上「战斗剩余时间 / 第N回合」）的上限。
# ⚠️ 12s 会漏：点开战后先有一段**没有任何 UI 的过场底图**，HUD 淡入实测 6~18s
#    （2026-10-30 玩法挑战实测：09:22:45 编队就绪 → 09:22:54 才认到战斗画面，9s；
#      另一轮 09:04:03 点开始 → 09:04:20~23 才认到，约 17s）。给到 20s 与战斗模块默认值对齐。
_BATTLE_ENTER_TIMEOUT = 20.0
# 战斗推进直接用副本挑战的 `_fight`（同一套 UI：只点「跳过回合」）。
# 支援战斗是"长血条多人战"（实测剩余时间 20~40 分钟），但开跳过回合时实测二十几秒就结算完；
# 这里给 5 分钟上限：真卡住（例如画面被挂机自动战斗接管、根本点不到「跳过回合」）时及时收手，
# 而不是白等副本挑战里的 600s 默认值（实机 2026-10-25 就白等过一整次）。
_SUPPORT_BATTLE_TIMEOUT = 300.0

# ---- 胜利结算 / 高难限定宝箱 ---------------------------------------------------------
# 贡献结算页右侧宝箱块（实测「高难限定宝箱」(555,498,139,30) /「今日掉落：3/4」(565,524,135,26)）。
# ⚠️ ROI 必须留够上下余量：实机第一版写成 (520,480,190,40) 时把「高难限定宝箱」这行
#    下沿切掉了一截，OCR 直接整行认不出来 → 明明掉了宝箱却当成"没有宝箱"，
#    一路点到物品结算的「还未领取…是否退出副本？」才发现漏领（2026-10-25 实机）。
_CHEST_LABEL_ROI = (495, 468, 225, 78)
_CHEST_LABEL_TEXT = "高难限定宝箱"
_CHEST_REMAIN_ROI = (495, 505, 225, 58)
_CHEST_REMAIN_PATTERN = re.compile(r"今日掉落\s*[:：]?\s*(\d+)\s*/\s*(\d+)")
# 「可领取高难限定宝箱啦！」提示（实测 (460,445,125,23)+(487,466,62,20)）：宝箱块之外的第二重信号
_CHEST_CALLOUT_ROI = (420, 425, 220, 75)
_CHEST_CALLOUT_TEXT = "可领取高难限定"
# 「战斗贡献结算」页锚点（实测「当前排名」(113,444,76,28)）：只在有它的这一页尝试领宝箱，
# 经验结算 / 物品结算页上宝箱块还在、但图标已经点不动了。
_CONTRIB_PAGE_ROI = (60, 430, 220, 140)
_CONTRIB_PAGE_TEXT = "当前排名"
# 宝箱图标本身（实测点 (635,435) 就弹领取确认框）；认不到时退到图标右下的同一点位
_CHEST_ICON_POINTS = ((635, 435), (655, 452))
# 领取 / 未领取退出 两种弹窗共用同一套按钮（实测「确定」(477,834,76,46)「取消」(165,836,77,45)）
_CHEST_DIALOG_TITLE_ROI = (210, 355, 300, 75)
_CHEST_DIALOG_BODY_ROI = (60, 510, 600, 120)
_CHEST_CLAIM_BODY_TEXT = "是否领取高难限定宝箱"
_CHEST_EXIT_BODY_TEXT = "还未领取"
_CHEST_DIALOG_CONFIRM_ROI = (430, 805, 140, 85)
_DIALOG_CONFIRM_TEXT = "确定"
_CHEST_DIALOG_TIMEOUT = 5.0
# 「恭喜获得」遮屏奖励弹窗（与 Exception_RewardClose 同一套判据/点击点）：
# 结算拿到奖励后会盖一整屏，只有「点击空白区域关闭」，没有它结算循环就永远回不到列表。
_OBTAIN_ROI = (250, 925, 220, 60)
_OBTAIN_TEXT = "点击空白区域关闭"
_OBTAIN_CLOSE_POINT = (370, 1215)  # Exception_RewardClose 的 target [330,1185,80,60] 中心

_SETTLE_MAX_ROUNDS = 24
_SETTLE_STEP_SETTLE = 1.2

# ---- 节奏 ----------------------------------------------------------------------------
_CLICK_SETTLE = 1.4
_REFRESH_SETTLE = 1.2
_REFRESH_RETRIES = 4  # 刷新后仍没有可支援的战斗 → 再刷新几次（有界，绝不无限刷）
_LIST_POLL = 1.0
_MAX_ENTRY_FAILURES = 3  # 连续 3 次进不去战斗就收手（避免对着坏状态空转）


def _click_at(context: Context, x: int, y: int) -> None:
    context.tasker.controller.post_click(int(x), int(y)).wait()


def _wait_text_in(context: Context, roi: Sequence[int], wanted: str, timeout: float) -> Any:
    """在 ROI 里有界等待「包含」wanted 的文本，命中返回识别框。"""
    return _wait_text(context, roi, wanted, timeout, exact=False)


# ---- 页面判定 -----------------------------------------------------------------------
def _at_battle_list(context: Context) -> bool:
    """战斗列表页锚点：左上标题「战斗列表」+ 列表头「战斗支援」，且没有结算推进按钮。

    ⚠️ 只认标题会误判：**结算浮层是半透明盖在战斗列表页上的**，「战斗列表」标题照样能被
    OCR 认出（实机 2026-10-25：物品结算页被当成"已回到列表"，于是去点被浮层盖住的
    「支援筛选」，连点两次都点不到 → 整轮任务白丢）。所以必须同时满足：
      · 顶部「战斗支援」分组标题可见（浮层会盖住它）；
      · 底部没有「点击空白处继续 / 确认」（有就说明还在结算屏）。
    """
    if _find_contains(context, _LIST_TITLE_ROI, _LIST_TITLE_TEXT) is None:
        return False
    if _find_contains(context, _LIST_SECTION_ROI, _LIST_SECTION_TEXT) is None:
        return False
    return _settle_button(context) is None


def _in_battle(context: Context) -> bool:
    """是不是正站在战斗画面里（左上「战斗剩余时间 / 第N回合」）。"""
    return _wait_for_battle_presence(context, 0.8)


def _filter_dialog_open(context: Context) -> bool:
    return _find_contains(context, _FILTER_TITLE_ROI, _FILTER_TITLE_TEXT) is not None


def _list_empty(context: Context) -> bool:
    return _find_contains(context, _EMPTY_ROI, _EMPTY_TEXT) is not None


# ---- 进入战斗列表页 -----------------------------------------------------------------
def _wait_screen_settled(context: Context, timeout: float) -> str | None:
    """等画面落定，返回 'list' / 'battle' / 'settle' / None。

    挂机取消 / 战斗结束后画面会先过一帧黑屏或加载，这一瞬间「战斗列表」标题和
    战斗锚点都认不到；直接判"回不到主页"会白丢一整轮（实机 2026-10-25 复现）。
    """
    deadline = time.time() + timeout
    while True:
        if _at_battle_list(context):
            return "list"
        if _in_battle(context):
            return "battle"
        if _settle_button(context) is not None:
            return "settle"
        if time.time() >= deadline:
            return None
        time.sleep(1.0)


def _goto_battle_list(context: Context) -> bool:
    """从主页（或任意子页面）走到战斗列表页。"""
    if _at_battle_list(context):
        return True
    # 挂机中可能正停在战斗画面：先取消挂机，再谈回列表（用户需求里的「如挂机中则取消挂机」）。
    if _idle_running(context):
        _stop_idle(context)
        if _at_battle_list(context):
            return True
    state = _wait_screen_settled(context, _SCREEN_SETTLE_TIMEOUT)
    if state == "battle":
        logger.debug("主动战斗：当前在战斗里，先把这一场打完再回战斗列表。")
        _fight(context, timeout=_SUPPORT_BATTLE_TIMEOUT)
        _settle_battle(context, claim_chest=_flag_enabled(context, _CHEST_FLAG))
        state = _wait_screen_settled(context, _SCREEN_SETTLE_TIMEOUT)
    elif state == "settle":
        logger.debug("主动战斗：当前停在结算屏，先结算完再回战斗列表。")
        _settle_battle(context, claim_chest=_flag_enabled(context, _CHEST_FLAG))
        state = _wait_screen_settled(context, _SCREEN_SETTLE_TIMEOUT)
    if state == "list" or _at_battle_list(context):
        return True

    if not _on_hub(context):
        logger.debug("主动战斗：当前不在主页，先返回主页。")
        if not _back_to_hub(context):
            logger.debug("主动战斗：常规返回没到主页，调用「返回主页」任务兜底。")
            context.run_task("BackToMainHub_Start")
        if not _on_hub(context):
            logger.error("主动战斗：回不到主页，本次放弃。")
            return False
    entry = _find_in(context, _HUB_BATTLE_LIST_ROI, _HUB_BATTLE_LIST_TEXT) or _find_contains(
        context, _HUB_BATTLE_LIST_ROI, _HUB_BATTLE_LIST_TEXT
    )
    if entry is None:
        logger.error("主动战斗：主页上没认出「战斗列表」按钮，本次放弃。")
        return False
    logger.debug("主动战斗：点主页「战斗列表」。")
    _click_box(context, entry)
    if _wait_text_in(context, _LIST_TITLE_ROI, _LIST_TITLE_TEXT, _LIST_OPEN_TIMEOUT) is None:
        logger.error("主动战斗：进不去战斗列表页，本次放弃。")
        return False
    time.sleep(_CLICK_SETTLE)
    return True


# ---- 支援筛选 -----------------------------------------------------------------------
def _open_filter_dialog(context: Context) -> bool:
    """点工具栏最左的漏斗图标打开「支援筛选设定」。"""
    if _filter_dialog_open(context):
        return True
    box = _match_template(context, _FUNNEL_ROI, _FUNNEL_TEMPLATE, _FUNNEL_THRESHOLD)
    if box is None:
        logger.error("主动战斗：认不到「支援筛选」漏斗图标，无法按设置筛选，本次放弃。")
        return False
    for attempt in range(2):
        if attempt:
            logger.debug("主动战斗：上一次点「支援筛选」没弹出设定框，再点一次。")
            box = _match_template(context, _FUNNEL_ROI, _FUNNEL_TEMPLATE, _FUNNEL_THRESHOLD) or box
        _click_box(context, box)
        if _wait_text_in(context, _FILTER_TITLE_ROI, _FILTER_TITLE_TEXT, _FILTER_OPEN_TIMEOUT) is not None:
            time.sleep(_CLICK_SETTLE)
            return True
    logger.error("主动战斗：「支援筛选设定」弹窗没出来，本次放弃。")
    return False


def _apply_filter(context: Context, selected: dict[str, set[str]]) -> bool:
    """把任务设置里的（副本 × 难度）写进「支援筛选设定」：先重置再逐系列勾选，最后确定。"""
    if not _open_filter_dialog(context):
        return False
    total = sum(len(keys) for keys in selected.values())
    logger.debug("主动战斗：按设置重设支援筛选（{} 个副本、{} 个难度）。", len(selected), total)
    if not _apply_selection(context, selected):
        return False
    if not _confirm_filter_dialog(context):
        return False
    if _filter_dialog_open(context):
        logger.warning("主动战斗：点完「确定」筛选弹窗还在，再点一次。")
        _confirm_filter_dialog(context)
        time.sleep(_CLICK_SETTLE)
    logger.debug("主动战斗：支援筛选已按设置更新。")
    return True


def _only_filtered_on(context: Context) -> bool | None:
    """读「仅显示筛选」开关状态：True 开 / False 关 / None 读不到。"""
    img = _screencap(context)
    if img is None:
        return None
    x, y = _ONLY_FILTER_TOGGLE
    x0 = max(0, x - _ONLY_FILTER_HALF)
    y0 = max(0, y - _ONLY_FILTER_HALF)
    patch = img[y0 : y0 + _ONLY_FILTER_HALF * 2 + 1, x0 : x0 + _ONLY_FILTER_HALF * 2 + 1].astype(int)
    if patch.size == 0:
        return None
    blue, green, red = patch[:, :, 0], patch[:, :, 1], patch[:, :, 2]
    bright = int(((blue > 90) & (blue > red + 25) & (green > 60)).sum())
    return bright >= _ONLY_FILTER_BLUE_MIN


def _ensure_only_filtered(context: Context) -> bool:
    """确保「仅显示筛选」开着（否则列表会把没筛选的战斗也列出来）。"""
    state = _only_filtered_on(context)
    if state is True:
        logger.debug("主动战斗：「仅显示筛选」已是开启状态。")
        return True
    if state is None:
        logger.warning("主动战斗：读不到「仅显示筛选」开关状态，按需要点一次。")
    if _find_contains(context, _ONLY_FILTER_TEXT_ROI, _ONLY_FILTER_TEXT) is None:
        logger.error("主动战斗：认不到「仅显示筛选」文字，无法确认筛选生效。")
        return False
    # 开关圆钮在文字左侧（实测 (146,408)）；读不到状态时也点这里
    _click_at(context, _ONLY_FILTER_TOGGLE[0], _ONLY_FILTER_TOGGLE[1])
    time.sleep(_CLICK_SETTLE)
    if _only_filtered_on(context) is True:
        logger.debug("主动战斗：已打开「仅显示筛选」。")
        return True
    logger.error("主动战斗：点过开关后「仅显示筛选」仍未开启，列表可能包含未筛选的战斗。")
    return False


# ---- 刷新 ---------------------------------------------------------------------------
def _refresh(context: Context) -> bool:
    """点工具栏最右的刷新按钮（模板匹配；认不到退实测点位）。"""
    box = _match_template(context, _REFRESH_ROI, _REFRESH_TEMPLATE, _REFRESH_THRESHOLD)
    if box is None:
        logger.debug("主动战斗：模板没认到刷新按钮，按实测点位 {} 点一次。", _REFRESH_POINT)
        _click_at(context, _REFRESH_POINT[0], _REFRESH_POINT[1])
    else:
        logger.debug("主动战斗：点刷新（{},{}）。", _box_center_x(box), _box_center_y(box))
        _click_box(context, box)
    time.sleep(_REFRESH_SETTLE)
    return _at_battle_list(context)


# ---- 读列表条目 ---------------------------------------------------------------------
@dataclass(frozen=True)
class Entry:
    """战斗支援列表里的一条：血量条位置、剩余血量比例、参与人数、副本名。"""

    bar_y: int
    hp_ratio: float
    name: str
    joined: int | None
    capacity: int | None

    @property
    def full(self) -> bool:
        return self.joined is not None and self.capacity is not None and self.joined >= self.capacity

    @property
    def click_y(self) -> int:
        return self.bar_y + _ENTRY_CLICK_DY

    @property
    def joined_text(self) -> str:
        if self.joined is None or self.capacity is None:
            return "人数认不到"
        return f"{self.joined}/{self.capacity}"


def _bar_rows(img: Any) -> list[tuple[int, int]]:
    """找出画面里所有血量条：[(行中心 y, 右端 x)]。

    只认从固定起点 x≈312 起、向右连续暖色的横条（实测满血右端 548）：
      · 起点必须是暖色 —— 左侧立绘/属性徽章也是暖色，但它们不在 x=312 这一列；
      · 右端按"连续暖色"量 —— 条右侧的「BP-3」是橙色文字，直接用整行最大值会量到 580+。
    """
    blue = img[:, :, 0].astype(int)
    red = img[:, :, 2].astype(int)
    warm = (red - blue > 40) & (red > 90)
    y0, y1 = _BAR_SCAN_Y
    x0, x1 = _BAR_SCAN_X
    rows: list[tuple[int, int]] = []
    for y in range(y0, min(y1, img.shape[0])):
        if not warm[y, _BAR_START_X : _BAR_START_X + 6].any():
            continue
        end = _BAR_START_X
        gap = 0
        for x in range(_BAR_START_X, min(x1, img.shape[1])):
            if warm[y, x]:
                end = x
                gap = 0
            else:
                gap += 1
                if gap >= 3:
                    break
        if end - _BAR_START_X + 1 < _BAR_MIN_WIDTH:
            continue
        rows.append((y, end))
    groups: list[list[tuple[int, int]]] = []
    for row in rows:
        if groups and row[0] - groups[-1][-1][0] <= 3:
            groups[-1].append(row)
        else:
            groups.append([row])
    out: list[tuple[int, int]] = []
    for group in groups:
        mid = group[len(group) // 2]
        out.append((mid[0], max(item[1] for item in group)))
    return out


def _read_entries(context: Context) -> list[Entry]:
    """读当前列表里能点的每一条：血量条长度 + 名字 + 参与人数（列表区只 OCR 一次）。"""
    img = _screencap(context)
    if img is None:
        return []
    bars = _bar_rows(img)
    if not bars:
        return []
    lines = _ocr_lines(context, _LIST_TEXT_ROI)
    entries: list[Entry] = []
    for bar_y, right in bars:
        name_parts: list[str] = []
        joined: int | None = None
        capacity: int | None = None
        for text, box in lines:
            dy = _box_center_y(box) - bar_y
            if _NAME_MIN_DY <= dy <= _NAME_MAX_DY:
                name_parts.append(text)
            elif _COUNT_MIN_DY <= dy <= _COUNT_MAX_DY and capacity is None:
                match = _PARTICIPANTS_PATTERN.search(text.replace(" ", ""))
                if match:
                    joined, capacity = int(match.group(1)), int(match.group(2))
        entries.append(
            Entry(
                bar_y=bar_y,
                hp_ratio=round(max(0.0, min(1.0, (right - _BAR_START_X + 1) / _BAR_SPAN)), 3),
                name=" ".join(name_parts).strip(),
                joined=joined,
                capacity=capacity,
            )
        )
    return entries


def _matches_selection(name: str, selected: dict[str, set[str]]) -> bool:
    """列表条目名里是否包含本次要打的某个副本名（OCR 会掉字，按包含匹配）。"""
    plain = name.replace(" ", "")
    if not plain:
        return False
    return any(dungeon.title.replace(" ", "") in plain for dungeon in DUNGEONS if dungeon.title in selected)


def _pick_entry(context: Context, selected: dict[str, set[str]], more_first: bool) -> Entry | None:
    """按副本优先级挑一场：优先进血量多 = 血条最长，优先进血量少 = 血条最短。满员的不选。"""
    entries = _read_entries(context)
    if not entries:
        if _list_empty(context):
            logger.debug("主动战斗：列表显示「暂无可支援的战斗」。")
        else:
            logger.debug("主动战斗：这一屏没读到可点的战斗条目。")
        return None
    for entry in entries:
        logger.debug(
            "主动战斗：候选 y={} 血量={:.0%} 人数={} 名字={!r}",
            entry.bar_y,
            entry.hp_ratio,
            entry.joined_text,
            entry.name,
        )
    wanted = [entry for entry in entries if _matches_selection(entry.name, selected)]
    if not wanted:
        # 名字 OCR 系统性失败时不能因此整段卡死：筛选本身已经生效，退到"全部条目里挑"。
        logger.warning("主动战斗：这一屏的条目名都没对上任务设置里的副本，退回按全部条目挑（筛选已生效）。")
        wanted = entries
    pool = [entry for entry in wanted if not entry.full]
    if not pool:
        logger.debug("主动战斗：这一屏 {} 场都满员了（参与人数=上限），跳过。", len(wanted))
        return None
    chosen = max(pool, key=lambda item: item.hp_ratio) if more_first else min(pool, key=lambda item: item.hp_ratio)
    logger.debug(
        "主动战斗：按「优先进血量{}」选中 y={}（血量={:.0%}，{}）。",
        "多" if more_first else "少",
        chosen.bar_y,
        chosen.hp_ratio,
        chosen.joined_text,
    )
    return chosen


# ---- 取消挂机 -----------------------------------------------------------------------
def _settle_button(context: Context) -> Any:
    """当前屏上的结算推进按钮（「点击空白处继续」/「确认」），没有返回 None。"""
    return _find_in(context, _MGD_BOTTOM_ROI, _WIN_CONTINUE_TEXT) or _find_in(
        context, _MGD_BOTTOM_ROI, _WIN_CONFIRM_TEXT
    )


def _idle_stop_button(context: Context) -> Any:
    """找「停止挂机」按钮（实测 (299,1189,121,37)，再给一条宽底带兜底）。"""
    for roi in _STOP_IDLE_ROIS:
        box = _find_in(context, roi, _STOP_IDLE_TEXT) or _find_contains(context, roi, _STOP_IDLE_TEXT)
        if box is not None:
            return box
    return None


def _idle_running(context: Context) -> bool:
    """是否正在挂机：三重判据任一命中（「自动挂机中」/「停止挂机」/「离线挂机」齿轮标签）。

    实机 2026-10-25：只靠 auto_idle 的 `_already_idling`（两个文案 + 固定 ROI）会漏判 ——
    挂机战斗画面某一帧两个文案都没认出来，于是整段走到「把这一场打完」，
    而挂机里的战斗是游戏自己在打（中间按钮不是「跳过回合」），白等满 600s 超时才收手。
    """
    if _already_idling(context):
        return True
    if _idle_stop_button(context) is not None:
        return True
    return _find_contains(context, _IDLE_GEAR_ROI, _IDLE_GEAR_TEXT) is not None


def _stop_idle(context: Context) -> bool:
    """如正在挂机则点「停止挂机」取消；取消后如果还在战斗里，先把这一场打完。

    实机：点「停止挂机」没有二次确认，挂机立即结束，但**正在打的那一场会继续**，
    所以还要接着调用战斗模块 + 结算，否则会带着战斗画面去点刷新（点不到任何按钮）。
    返回 True = 现在不在挂机中（本来就没挂 / 已经取消掉）。

    ⚠️ 挂机中画面可能在结算/过场（实测「自动挂机中」还在、但「停止挂机」这一帧还没画出来），
    这时不能直接放弃：先把结算按钮点掉，再回头找「停止挂机」重试几次。
    """
    if not _idle_running(context):
        return True
    clicked = False
    for attempt in range(1, _STOP_IDLE_RETRIES + 1):
        stop = _idle_stop_button(context)
        if stop is not None:
            logger.debug("主动战斗：检测到正在挂机，点「停止挂机」取消挂机。")
            _click_box(context, stop)
            clicked = True
            break
        button = _settle_button(context)
        if button is not None:
            logger.debug("主动战斗：挂机中先点掉当前的结算/过场屏（第 {} 次），再找「停止挂机」。", attempt)
            _click_box(context, button)
            time.sleep(_SETTLE_STEP_SETTLE)
            continue
        time.sleep(1.5)
    if not clicked:
        logger.warning("主动战斗：画面在挂机中，但认不到「停止挂机」按钮，跳过取消（后续会按当前画面自愈）。")
        return False

    deadline = time.time() + _STOP_IDLE_TIMEOUT
    while time.time() < deadline:
        if not _idle_running(context):
            logger.debug("主动战斗：挂机已取消。")
            break
        time.sleep(0.8)
    else:
        logger.warning("主动战斗：点过「停止挂机」后画面仍在挂机中，继续后续流程。")
    if _in_battle(context):
        logger.debug("主动战斗：取消挂机时正打着一场，先把这一场打完（不计入战斗次数）。")
        _fight(context, timeout=_SUPPORT_BATTLE_TIMEOUT)
        _settle_battle(context, claim_chest=_flag_enabled(context, _CHEST_FLAG))
    elif _settle_button(context) is not None:
        logger.debug("主动战斗：取消挂机后停在结算屏，先结算完（不计入战斗次数）。")
        _settle_battle(context, claim_chest=_flag_enabled(context, _CHEST_FLAG))
    return True


# ---- 进入一场支援战斗 ---------------------------------------------------------------
def _refill_energy_once(context: Context) -> bool:
    """BP 不足弹出「补充精力」时，用**第一个**精力补给补一次。

    返回 True = 确实弹了精力框（已走完补精力链，调用方可以再点一次「开始战斗」）；
    返回 False = 没弹这个框，调用方按原逻辑继续。

    补精力的点击顺序统一放在 Pipeline `EnergyRefill_Dialog`（认标题 → 点最上面那个「使用」
    → 物品详情「使用」→ 左下角返回关弹窗），这样「仅补充体力」任务和这里用的是同一套逻辑，
    不在 Python 里再维护一份坐标。
    """
    if not any(_find_contains(context, _ENERGY_TITLE_ROI, text) for text in _ENERGY_TITLE_TEXTS):
        return False
    logger.debug("主动战斗：BP 不足弹出了「补充精力」，用第一个精力补给补一次。")
    context.run_task("EnergyRefill_Dialog")
    return True


def _start_battle(context: Context, entry: Entry, prefs: dict[str, str]) -> bool:
    """点列表里的这一场 → 助战选择 → 编队「开始战斗」→ 校验真的进了战斗画面。"""
    logger.debug("主动战斗：点进 y={} 这一场支援战斗。", entry.click_y)
    _click_at(context, _ENTRY_CLICK_X, entry.click_y)
    if handle_away_full(context):
        logger.warning("主动战斗：出现「暂离已满」，这一场没有打出去，交回刷新节点。")
        return False

    support = _wait_for(context, _SUPPORT_TITLE_ROI, _SUPPORT_TITLE_TEXT, _SUPPORT_OPEN_TIMEOUT)
    if support is None:
        if handle_away_full(context):
            return False
        logger.warning("主动战斗：点了这一场但没弹出「助战选择」，交回刷新节点。")
        return False
    logger.debug("主动战斗：已进入「助战选择」，调用助战选择模块（偏好={}）。", prefs or "未配置")
    if not PickSupport().pick(context, prefs):
        logger.warning("主动战斗：助战选择没能进到编队页，交回刷新节点（未开始战斗）。")
        return False

    for attempt in range(2):
        start = _wait_for(context, _FORMATION_START_ROI, _FORMATION_START_TEXT, _FORMATION_START_TIMEOUT)
        if start is None:
            logger.warning("主动战斗：编队页没等到「开始战斗」，交回刷新节点（未开始战斗）。")
            return False
        _click_box(context, start)
        time.sleep(_CLICK_SETTLE)
        if _find_contains(context, _STAMINA_ROI, _STAMINA_TEXT) is not None:
            logger.debug("主动战斗：BP 不足弹出了「补充体力」，按全局体力药设置补一次。")
            context.run_task("StaminaRefill_Dialog")
            time.sleep(_CLICK_SETTLE)
            if attempt == 0:
                continue
        elif _refill_energy_once(context):
            # 「补充精力」是另一套弹窗（BP 不够时弹），用第一个精力补给；补完再点一次「开始战斗」。
            logger.debug("主动战斗：已用第一个精力补给补一次，重新点「开始战斗」。")
            if attempt == 0:
                continue
        break

    if not _wait_for_battle_presence(context, _BATTLE_ENTER_TIMEOUT):
        logger.warning(
            "主动战斗：点「开始战斗」后 {} 秒没进战斗画面，交回刷新节点重新选一场。",
            _BATTLE_ENTER_TIMEOUT,
        )
        return False
    logger.debug("主动战斗：已进入战斗画面。")
    return True


# ---- 胜利结算 / 高难限定宝箱 ---------------------------------------------------------
def _chest_remaining(context: Context) -> tuple[int, int] | None:
    """读贡献结算页的「今日掉落：N/M」→ (N, M)；认不到返回 None。"""
    for text, _box in _ocr_lines(context, _CHEST_REMAIN_ROI):
        match = _CHEST_REMAIN_PATTERN.search(text.replace(" ", ""))
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def _chest_present(context: Context) -> bool:
    """贡献结算页右侧有没有宝箱块：宝箱名 / 今日掉落 /「可领取高难限定宝箱啦！」任一命中即算。"""
    if _find_in(context, _CHEST_LABEL_ROI, _CHEST_LABEL_TEXT) is not None:
        return True
    if _find_contains(context, _CHEST_LABEL_ROI, _CHEST_LABEL_TEXT) is not None:
        return True
    if _find_contains(context, _CHEST_CALLOUT_ROI, _CHEST_CALLOUT_TEXT) is not None:
        return True
    return _chest_remaining(context) is not None


def _chest_claimable(context: Context) -> bool:
    """贡献结算页上是否"有可领取的高难限定宝箱"（只有传说副本会有）。

    ⚠️ 必须**限定在「战斗贡献结算」这一页**里判断：宝箱块会一直留在后续的
       经验结算 / 物品结算页上（实测「今日掉落：2/4」在 p1_exp/p2_item 里仍在），
       但那两页上宝箱图标已经点不动了 —— 只在有「当前排名」的贡献结算页尝试领取。
    """
    if _find_contains(context, _CONTRIB_PAGE_ROI, _CONTRIB_PAGE_TEXT) is None:
        return False
    if not _chest_present(context):
        return False
    remaining = _chest_remaining(context)
    if remaining is None:
        logger.debug("主动战斗：有高难限定宝箱但读不到今日掉落次数，按可领取尝试一次（有确认框兜底）。")
        return True
    left, total = remaining
    if left <= 0:
        logger.debug("主动战斗：高难限定宝箱今日掉落已用完（{}/{}），不点它。", left, total)
        return False
    logger.debug("主动战斗：有可领取的高难限定宝箱（今日掉落 {}/{}）。", left, total)
    return True


def _chest_dialog_kind(context: Context) -> str | None:
    """宝箱弹窗类型：'claim' 是否领取 / 'exit' 未领取是否退出副本 / None 没有弹窗。"""
    title = _find_in(context, _CHEST_DIALOG_TITLE_ROI, _CHEST_LABEL_TEXT) or _find_contains(
        context, _CHEST_DIALOG_TITLE_ROI, _CHEST_LABEL_TEXT
    )
    if title is None:
        return None
    if _find_contains(context, _CHEST_DIALOG_BODY_ROI, _CHEST_CLAIM_BODY_TEXT) is not None:
        return "claim"
    if _find_contains(context, _CHEST_DIALOG_BODY_ROI, _CHEST_EXIT_BODY_TEXT) is not None:
        return "exit"
    return None


def _confirm_chest_dialog(context: Context) -> bool:
    confirm = _find_in(context, _CHEST_DIALOG_CONFIRM_ROI, _DIALOG_CONFIRM_TEXT) or _find_contains(
        context, _CHEST_DIALOG_CONFIRM_ROI, _DIALOG_CONFIRM_TEXT
    )
    if confirm is None:
        logger.warning("主动战斗：宝箱弹窗里认不到「确定」，不盲点。")
        return False
    _click_box(context, confirm)
    time.sleep(_CLICK_SETTLE)
    return True


def _claim_chest(context: Context) -> bool:
    """点宝箱图标 →「是否领取高难限定宝箱？」→「确定」，并按今日掉落次数验证真的领到了。"""
    before = _chest_remaining(context)
    for point in _CHEST_ICON_POINTS:
        _click_at(context, point[0], point[1])
        deadline = time.time() + _CHEST_DIALOG_TIMEOUT
        kind: str | None = None
        while time.time() < deadline:
            kind = _chest_dialog_kind(context)
            if kind is not None:
                break
            time.sleep(0.4)
        if kind == "claim":
            logger.debug("主动战斗：弹出「是否领取高难限定宝箱？」，点「确定」领取。")
            if not _confirm_chest_dialog(context):
                return False
            after = _chest_remaining(context)
            if before is not None and after is not None:
                logger.debug("主动战斗：高难限定宝箱已领取，今日掉落 {} → {}。", before, after)
            else:
                logger.debug("主动战斗：已点「确定」领取高难限定宝箱（读不到领取前后次数，按已领取处理）。")
            return True
        if kind == "exit":
            logger.debug("主动战斗：点宝箱弹的是「未领取是否退出副本」，这一场领不了，交给结算流程处理。")
            return False
    logger.warning("主动战斗：点宝箱图标后没出现领取确认框，本次不领取。")
    return False


def _settle_battle(context: Context, claim_chest: bool) -> bool:
    """胜利结算循环：贡献结算（可领取宝箱）→ 经验结算 → 物品结算 → 回到战斗支援列表。

    实机顺序：贡献结算「点击空白处继续」→ 经验结算「点击空白处继续」→ 物品结算「确认」
    → 回到战斗支援列表。没领宝箱时物品结算的「确认」会弹
    「还未领取当前副本的高难限定宝箱，是否退出副本？」→ 点「确定」照常退出。
    """
    claimed = False
    for round_no in range(1, _SETTLE_MAX_ROUNDS + 1):
        if is_stopping(context):
            return False
        # ① 宝箱相关弹窗（领取确认 / 未领取退本确认）：两种都点「确定」
        kind = _chest_dialog_kind(context)
        if kind is not None:
            logger.debug("主动战斗：结算第 {} 轮，处理「{}」宝箱弹窗 → 点「确定」。", round_no, kind)
            _confirm_chest_dialog(context)
            continue
        # ② 好友申请（用了别人的助战，结算后可能弹）
        if _dismiss_friend_request(context):
            continue
        # ③「恭喜获得」遮屏奖励弹窗：点空白处关掉（Exception_RewardClose 同款判据/点位）
        if _find_contains(context, _OBTAIN_ROI, _OBTAIN_TEXT) is not None:
            logger.debug("主动战斗：结算第 {} 轮，关掉「恭喜获得」遮屏奖励弹窗。", round_no)
            _click_at(context, _OBTAIN_CLOSE_POINT[0], _OBTAIN_CLOSE_POINT[1])
            time.sleep(_SETTLE_STEP_SETTLE)
            continue
        # ④ 已经回到战斗支援列表
        if _at_battle_list(context):
            logger.debug("主动战斗：结算完成，已回到战斗支援列表。")
            return True
        # ⑤ 贡献结算页上有可领取的高难限定宝箱（且全局开关打开）→ 点它领取
        if claim_chest and not claimed and _chest_claimable(context):
            if _claim_chest(context):
                claimed = True
                continue
        # ⑥ 推进结算页：「点击空白处继续」/「确认」
        box = _find_in(context, _MGD_BOTTOM_ROI, _WIN_CONTINUE_TEXT) or _find_in(
            context, _MGD_BOTTOM_ROI, _WIN_CONFIRM_TEXT
        )
        if box is not None:
            logger.debug("主动战斗：结算第 {} 轮，点「继续/确认」。", round_no)
            _click_box(context, box)
            time.sleep(_SETTLE_STEP_SETTLE)
            continue
        time.sleep(_SETTLE_STEP_SETTLE)
    logger.warning("主动战斗：结算循环 {} 轮仍未回到战斗支援列表。", _SETTLE_MAX_ROUNDS)
    return False


# ---- 主流程 -------------------------------------------------------------------------
def _read_times(params: dict[str, Any]) -> int:
    raw = params.get("times", MIN_TIMES)
    try:
        times = int(raw)
    except (TypeError, ValueError):
        logger.warning("主动战斗：战斗次数 {!r} 不是整数，按 {} 次处理。", raw, MIN_TIMES)
        times = MIN_TIMES
    if times < MIN_TIMES:
        logger.warning("主动战斗：战斗次数 {} 小于下限，按 {} 次处理。", times, MIN_TIMES)
        times = MIN_TIMES
    if times > MAX_TIMES:
        logger.warning("主动战斗：战斗次数 {} 超过上限，按 {} 次处理。", times, MAX_TIMES)
        times = MAX_TIMES
    return times


def _hp_more_first(context: Context) -> bool:
    """读「副本优先级」：True = 优先进血量多。"""
    try:
        node = context.get_node_data(_HP_PRIORITY_NODE) or {}
    except Exception as error:  # noqa: BLE001 读不到按默认「血量多」处理，不中断流程
        logger.warning("主动战斗：读取副本优先级节点失败（按血量多处理）：{}", error)
        return True
    expected = ((node.get("recognition") or {}).get("param") or {}).get("expected") or []
    first = str(expected[0]).strip() if expected else ""
    if not first:
        logger.warning("主动战斗：副本优先级节点没有取值（按血量多处理）。")
        return True
    return "少" not in first


@AgentServer.custom_action("AutoIdleBattleRun")
class AutoIdleBattleRun(CustomAction):
    """「自动挂机 · 主动战斗」：按设置的副本筛选去别人的支援请求里打「战斗次数」场。

    ⚠️ 每一场都会真实消耗 BP（实测 2~5 点），并且是在别的玩家的支援请求里参战。
    一个（副本 × 难度）都没勾选时**整段跳过、一个按钮都不点**。

    Examples:
        `custom_action_param`::

            {
                "times": 1
            }
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            params = parse_params(argv.custom_action_param)
        except ValueError as error:
            logger.error("主动战斗：custom_action_param 解析失败：{}", error)
            return CustomAction.RunResult(success=False)

        times = _read_times(params)
        selected = _read_selection(context)
        if not selected:
            logger.info("主动战斗：任务设置里没有勾选任何副本/难度，跳过（不点任何按钮）。")
            return CustomAction.RunResult(success=True)

        more_first = _hp_more_first(context)
        claim_chest = _flag_enabled(context, _CHEST_FLAG)
        prefs = _read_support_prefs(context)
        logger.info(
            "主动战斗：战斗次数={} 副本优先级={} 领取高难限定宝箱={} 助战偏好={}",
            times,
            "优先进血量多" if more_first else "优先进血量少",
            "开" if claim_chest else "关",
            prefs or "未配置",
        )

        if not _goto_battle_list(context):
            return CustomAction.RunResult(success=False)
        if not _apply_filter(context, selected):
            return CustomAction.RunResult(success=False)
        if not _ensure_only_filtered(context):
            logger.warning("主动战斗：「仅显示筛选」没打开成功，继续按当前列表尝试。")

        done = 0
        failures = 0
        while done < times:
            if is_stopping(context):
                logger.info("主动战斗：收到停止请求，中断（已完成 {} 场）。", done)
                return CustomAction.RunResult(success=done > 0)
            _stop_idle(context)
            if not _at_battle_list(context):
                logger.debug("主动战斗：当前不在战斗支援列表，先回列表。")
                if not _goto_battle_list(context):
                    logger.error("主动战斗：回不到战斗支援列表，结束（已完成 {} 场）。", done)
                    break

            entry: Entry | None = None
            for attempt in range(1, _REFRESH_RETRIES + 1):
                if not _refresh(context):
                    logger.warning("主动战斗：点刷新后没认到战斗列表页，重试。")
                    continue
                entry = _pick_entry(context, selected, more_first)
                if entry is not None:
                    break
                logger.debug("主动战斗：第 {} 次刷新后仍没有可支援的战斗，等一会儿再刷。", attempt)
                time.sleep(_LIST_POLL)

            if entry is None:
                logger.warning(
                    "主动战斗：刷新 {} 次都没有可支援的战斗，本次结束（已完成 {} 场）。",
                    _REFRESH_RETRIES,
                    done,
                )
                break

            if not _start_battle(context, entry, prefs):
                failures += 1
                logger.warning(
                    "主动战斗：第 {}/{} 次没能进入战斗，回到「刷新」节点重来。",
                    failures,
                    _MAX_ENTRY_FAILURES,
                )
                if failures >= _MAX_ENTRY_FAILURES:
                    logger.error("主动战斗：连续 {} 次进不去战斗，本次结束（已完成 {} 场）。", failures, done)
                    break
                time.sleep(_LIST_POLL)
                continue

            failures = 0
            won = _fight(context, timeout=_SUPPORT_BATTLE_TIMEOUT)
            logger.info("主动战斗：这一场结束（{}）。", "胜利" if won else "未胜利/超时")
            _settle_battle(context, claim_chest=claim_chest)
            done += 1
            logger.info("主动战斗：已完成 {}/{} 场。", done, times)

        logger.info("主动战斗：本次共完成 {} 场支援战斗（计划 {} 场）。", done, times)
        return CustomAction.RunResult(success=done > 0)
