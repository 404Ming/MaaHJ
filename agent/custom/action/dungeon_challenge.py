"""副本挑战任务的自定义动作（「副本挑战」任务，多人房间战 / 创建房间流程）。

流程由实机 720x1280 只读探索逐跳印证（系列页、切换系列弹窗、前往图钉、详情页难度页签、
创建房间 → 助战选择 → 编队页 → 房间页 全部实拍核对过）：

  主页「副本」→ 副本挑战页（系列页）
  → 需要时「切换系列」（弹窗里按**横幅模板**选到目标系列）
  → 列表里找到目标副本 → 点该卡片右侧「前往」图钉（按模板匹配定位，不写死坐标）
  → 副本详情页 → 点左侧难度页签（难度N / 简单 / 困难 / 噩梦）
    （神祝之翼 / 烛之造物 / 神骸试炼 的非传说副本当前只有一个难度、**整列页签都不存在**，
     这种页面直接跳过选难度；将来版本加了难度，页签会自动被扫到、走回正常路径）
  → 「创建房间」→ 助战选择（复用玩法挑战的 `PickSupport`：按全局 神/魔/属攻 偏好选魔物）
  → 编队页「确定」→ 房间页「开始战斗」→「是否开始战斗?」确定
  → 战斗（**只点「跳过回合」**，不碰「回合开始/奥义释放/自动/倍速」）
  → 结算循环点「点击空白处继续」/「确认」/「确定」→ 退出房间（有确认框就确定）→ 回主页。

为什么难度/系列/图钉都要用识别而不是固定坐标：
  · 系列名是艺术字，OCR 只认出「始源巨兽」，其余被拆成「王国」+「灾厄」/「神」；
    改用「切换系列」弹窗里的**横幅图模板匹配**后，5 个系列命中 1.000/0.959，互相误配 ≤0.31。
  · 传说副本的卡片标题会把名字和「难度N」「今日剩余次数」粘成一行，且偶发丢字
    （实测「无终冥土·俄耳浦斯难度」），所以按关键字「包含」匹配，不追求整串。
  · 难度页签在一个页面里是「难度1~难度10」，在另一个页面里是「简单/困难/噩梦」，
    文案不统一 → 按选项给的**正则**认页签文字；认不到才退回实测档位栅格（y=236+85*(n-1), x=75）。
"""

from __future__ import annotations

import inspect
import re
import time
from collections.abc import Sequence
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
from utils.interrupt import is_stopping
from utils.params import parse_params

# 复用既有模块里**已验证**的识别/点击基元与战斗常量（机神降临 = 同一套多人房间战 UI）。
from .gameplay_challenge import (
    PickSupport,
    _box_center_x,
    _box_center_y,
    _ocr,
    _read_support_prefs,
    _wait_for_battle_presence,
)
from .machine_god_descent import (
    _FRIEND_CANCEL_ROI,
    _FRIEND_CANCEL_TEXT,
    _GOD_LOSE_RESULT_ROI,
    _GOD_LOSE_TEXT,
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
_TARGET_SERIES_NODE = "DungeonChallenge_TargetSeries"
_TARGET_DUNGEON_NODE = "DungeonChallenge_TargetDungeon"
_TARGET_DIFFICULTY_NODE = "DungeonChallenge_TargetDifficulty"
_CREATE_ROOM_FLAG = "DungeonChallenge_Flag_CreateRoom"

# 「次数」取值范围（用户要求：默认 0，最大 9999）
MIN_TIMES = 0
MAX_TIMES = 9999

# 选项「无」写入的哨兵值：与任何真实文案都不重合，Python 据此判定"用户没选"。
UNSELECTED = "__.未选择__"

# ---- 主页（惑星之间）----------------------------------------------------------------
# 主页判定与项目既有 BackToMainHub_AtMainHub 用同一处锚点（底部导航第一个按钮文案）。
_HUB_ROI = (0, 1180, 170, 100)
_HUB_TEXT = "惑星之间"
# 底部导航「副本」（实测 box (279,1221,53,31) score 1.000）。
_HUB_DUNGEON_NAV_ROI = (230, 1195, 120, 80)
_HUB_DUNGEON_NAV_TEXT = "副本"

# ---- 副本挑战页（系列页）------------------------------------------------------------
# 页面锚点 + 切换入口（实测「切换系列」box (624,274,84,32) score 0.997）。
_SWITCH_SERIES_ROI = (595, 250, 140, 80)
_SWITCH_SERIES_TEXT = "切换系列"
# 卡片列表区（4 张卡：标题 y≈433/657/889/1117；往下 2 张 + 传说副本）
_LIST_ROI = (0, 400, 720, 780)

# 「切换系列」弹窗：实测占 x≈336~680、窗口 y≈320~800，一次可见约 3.5 张卡，可上下滚动。
_SERIES_POPUP_ROI = (336, 325, 344, 430)
_SERIES_POPUP_SWIPE_TO_TOP = (560, 420, 560, 700)
_SERIES_POPUP_SWIPE_TO_BOTTOM = (560, 720, 560, 380)

# 列表滚动：一屏 4 张卡，一次滑动约 4 张卡（实测 迪亚波罗→奥蕾莉亚）。
# ⚠️ 起点必须落在列表下半部分（y≈1150），从列表中部起滑实测**不滚动**。
_LIST_SWIPE_TO_BOTTOM = (360, 1150, 360, 620)  # 内容上移 = 看后面的卡片
_LIST_SWIPE_TO_TOP = (360, 620, 360, 1150)  # 内容下移 = 回到列表最上面
# 卡片贴在列表底/顶边时标题能认到、但右侧「前往」图钉已被裁掉（实测 猩猩王-困难 标题在
# y≈1117，图钉落在列表可视区之外，模板匹配必然失败）。这时把列表**推一整屏**，
# 让目标卡片整体进入可视区再点，而不是直接放弃。
# ⚠️ 小步滑（300~400px）实测不触发滚动，必须用上面已经验证过的一整屏手势。
_LIST_NUDGE_UP = _LIST_SWIPE_TO_BOTTOM
_LIST_NUDGE_DOWN = _LIST_SWIPE_TO_TOP

# ---- 「前往」图钉（白圈 + 橙色圆点的定位图标）----------------------------------------
# 实测 5 个系列所有卡片同一套图：模板 1.000 / 0.998 / 0.992，跨系误配 ≤0.31。
_PIN_TEMPLATE = "DungeonChallenge/GoPin.png"
_PIN_THRESHOLD = 0.75
# 目标卡片贴在列表底边时，图钉下半被底栏（房间码加入 / 战斗列表）上方的暗色渐变压住，
# 实测相似度从 0.99 掉到 0.68~0.69（同屏其他图钉仍是 0.97~1.00；全屏扫一遍，0.6 以上除了图钉没有别的位置）。
# 这种位置的**点击仍然有效**（实测点图钉中心 (634,1170) 能正常进详情页），所以单独给一个放宽阈值；
# 认到就点，避免白跑一轮「推列表 + 全表复扫」——那个整屏手势会把目标卡推到列表可视区之上，反而扫不到
# （实机 2026-10-18 18:54：赫拉斯瓦尔格 连续 3 次都卡在同一个被裁位置，最后放弃）。
_PIN_THRESHOLD_CLIPPED = 0.6
# 底栏按钮带顶边（实测「房间码加入 / 战斗列表」文字在 y≈1242）。图钉中心在这以下会被按钮挡住，不能点。
_LIST_FOOTER_TOP = 1230
# 图钉相对卡片标题框的纵向偏移实测 +36~+40，取 ROI = 标题中心 -10 ~ +90 兜住两种粘行情况。
_PIN_ROI_X = 560
_PIN_ROI_W = 140
_PIN_ROI_TOP = -10
_PIN_ROI_H = 100

# ---- 系列横幅模板（「切换系列」弹窗里按图选系列）------------------------------------
_SERIES_TEMPLATES = {
    "王国灾厄": "DungeonChallenge/Series_WangGuo.png",
    "始源巨兽": "DungeonChallenge/Series_ShiYuan.png",
    "神祝之翼": "DungeonChallenge/Series_ShenZhu.png",
    "烛之造物": "DungeonChallenge/Series_ZhuZhi.png",
    "神骸试炼": "DungeonChallenge/Series_ShenHai.png",
}
_SERIES_TEMPLATE_THRESHOLD = 0.8

# ---- 副本详情页 ---------------------------------------------------------------------
# 页面锚点（实测「今日剩余挑战次数：1/2」box (169,533,208,25)）。
_DETAIL_ANCHOR_ROI = (100, 500, 500, 80)
_DETAIL_ANCHOR_TEXT = "今日剩余挑战次数"
_REMAIN_PATTERN = re.compile(r"今日剩余挑战次数\s*[:：]\s*(\d+)")
# 左侧难度页签列（实测 难度1..难度10 / 简单 / 困难 / 噩梦 全部落在 x 15~135）。
# ⚠️ 神祝之翼 / 烛之造物 / 神骸试炼 的**非传说副本**当前版本只有一个难度，详情页整列页签都不存在
#    （面板左移、没有这一列），此时本 ROI 里只有 LV.xx / 今日剩余挑战次数 等文字 → 扫不到页签。
_DIFFICULTY_TAB_ROI = (0, 180, 140, 880)
# 页签文案的合法形态：难度N / 简单 / 普通 / 困难 / 噩梦。用白名单判定"这一列到底有没有页签"。
_DIFFICULTY_TAB_TOKEN = re.compile(r"^(难度\d+|简单|普通|困难|噩梦)$")
# 实测页签栅格：中心 y = 236 + 85*(n-1)，x≈75（难度1 与 简单 同位，难度2 与 困难 同位）。
_DIFFICULTY_TAB_X = 75
_DIFFICULTY_TAB_Y0 = 236
_DIFFICULTY_TAB_STEP = 85
# 选中页签带高亮底纹：实测选中档金色像素 1015~1035，未选中 220~304（135x76 取样带）。
_TAB_BAND_HALF = 38
_TAB_BAND_X = (5, 140)
_TAB_GOLD_SELECTED = 600
# 「创建房间」：两种详情页布局的按钮横向位置不同，ROI 必须同时兜住：
#   · 有难度页签列（潘塔菲卡等）：面板靠右，「创建房间」box (512,1030,135,46)；
#   · **没有页签列**（神祝之翼 / 烛之造物 / 神骸试炼 的单难度副本，2026-10 版本）：
#     面板左移，按钮 box (447,1030,132,46)。旧 ROI 从 x=470 起，会把「创」字切掉，
#     OCR 只读到「建房间」→ 认不到按钮（实机 2026-10-18 复现）。
# 放宽到 x=430 起；左侧「加入房间」box (144,1032,133,42) 仍在框外，不会误点。
_CREATE_ROOM_ROI = (430, 1000, 230, 100)
_CREATE_ROOM_TEXT = "创建房间"

# ---- 编队页 / 房间页 -----------------------------------------------------------------
# 编队页「确定」（实测 box (325,1025,72,41)；多人房间编队页只能「确定」，没有「开始战斗」）。
_FORMATION_CONFIRM_ROI = (280, 1000, 160, 80)
# 编队页**锚点**：用只属于编队页的「主要成员」（实测 box (187,457,75,23)，助战选择弹窗里没有）。
# 复用的 PickSupport 默认拿底部「开始战斗」当锚点，多人房间编队页没有这个按钮，
# 所以副本这边改成「主要成员」—— 这也正是 PickSupport 支持自定义锚点的原因。
_FORMATION_ANCHOR_ROI = [150, 440, 460, 50]
_FORMATION_ANCHOR_TEXT = "主要成员"
# 房间页「开始战斗」（实测 box (434,1178,124,42)）。
_ROOM_START_ROI = (380, 1150, 220, 80)
_ROOM_START_TEXT = "开始战斗"
# 编队页「确定」→ 房间页「开始战斗」的过渡等待（用户要求加的超时判断点）：
# 超过这个秒数还看不到「开始战斗」，就当过渡失败，交给公共异常处理流程（Exception_Start）。
_FORMATION_TO_ROOM_TIMEOUT = 15.0
# 「是否开始战斗?」「确认退出房间?」两个确认框的「确定」都在 (479,836,72,41)。
_DIALOG_CONFIRM_ROI = (430, 795, 175, 90)
_DIALOG_CONFIRM_TEXT = "确定"
_LEAVE_ROOM_ROI = (200, 540, 320, 80)
_LEAVE_ROOM_TEXT = "确认退出房间"

# ---- 战斗 / 结算 ---------------------------------------------------------------------
# 多人房间战的中间按钮只有两个状态循环：**「跳过回合」→ 点 → 「回合开始」→（下一回合）→「跳过回合」**。
# 「跳过回合」的可点窗口很短，所以开着全局「跳过回合」时必须**快**：窄 ROI + 紧循环轮询，
# 认到立刻点、点完立刻再看下一帧（不睡整拍）。
# 窄 ROI 来自**真机实测**（MuMu 720x1280 房间战截图裁切）：中间按钮「回合开始/跳过回合」
# 在 y≈1085~1145 那一行、文字横向 x≈245~510，所以取 (180,1070,380,110) 兜住整颗按钮。
# ⚠️ 别往上挪：y≈955~1060 是**角色卡片的「奥义值 0%」**区域，会认到一堆卡牌文本。
_BATTLE_TURN_ROI = (180, 1070, 380, 110)
# 兜底：万一窄 ROI 因为分辨率/版式偏差认不到，就退到整条底部带（慢一点，但不会漏）。
# 下界从 1000 起，避开角色卡片区；上界到屏幕底，覆盖按钮行 + 排行榜 + 支援入口。
_BATTLE_TURN_WIDE_ROI = (0, 1000, 720, 280)
_BATTLE_TURN_TEXT = "跳过回合"
_BATTLE_TURN_WAIT_TEXTS = ("回合开始", "等待回合开始")
# 额外的胜利锚点：结算页左下「挑战下一关」（与右侧「确认」并排，实测 box (96,1193,129,32) score 1.000）。
# 战斗进行中这块区域只有「跳过回合 / 回合开始」，绝不会出现「挑战下一关」，
# 所以把它也算成胜利判据：OCR 偶发漏读「确认」时，不会把胜利页当成"还在打"白等到超时。
_WIN_NEXT_STAGE_TEXT = "挑战下一关"
# 极短间隔：一轮「截图 + 窄 ROI OCR」约 0.15~0.25s，再留一点间隙避免空转占满 CPU。
_BATTLE_FAST_SLEEP = 0.05
# 每 N 次没认到按钮状态，就做一次「战斗是否结束」的完整判定（胜利/失败/结算）。
_BATTLE_RESULT_EVERY = 6
# 全局任务配置「战斗设置 · 跳过回合」的开关载体（resource/base/pipeline/GlobalConfig）。
# 未开启时**不识别按钮状态、也不点任何按钮**，只等结算。
_SKIP_TURN_FLAG = "Flag_BattleSkipTurn"
# 一场战斗的最长等待（副本房间战通常 1~3 分钟；10 分钟兜底）。
_BATTLE_TIMEOUT = 600.0
# 不点按钮时的轮询节奏（每轮一次「胜利/失败」判定）。
_BATTLE_STEP_SETTLE = 1.5
# 不点按钮时按帧轮询胜利结算：一帧本身 ≈0.28s，再补 0.15s 间隙 ⇒ 反应 ≤0.45s，
# 又不会像紧循环那样整场战斗都满速截图 OCR。
_BATTLE_RESULT_POLL = 0.15
# 战斗画面在场上限（BattlePresence 轮询，复用玩法挑战已验证的锚点节点）。
_BATTLE_PRESENCE_TIMEOUT = 30.0

# ---- 呼叫支援（战斗左下角「发送多人支援请求」）----------------------------------------
# 全局任务配置（GlobalConfig.json 的载体节点）：
#   Flag_SupportCall          switch  ：要不要发支援请求
#   Flag_SupportScope_{All,Friend,Guild} checkbox：发之前把弹窗里哪几个范围勾上
_SUPPORT_CALL_FLAG = "Flag_SupportCall"
_SUPPORT_SCOPE_FLAGS = (
    ("全体可见", "Flag_SupportScope_All"),
    ("好友可见", "Flag_SupportScope_Friend"),
    ("骑士团可见", "Flag_SupportScope_Guild"),
)
# 左下角入口按钮：实机截图（608x1080）里 pill 按钮 x≈18~158 / y≈1023~1050，
# 换算到 720x1280 是 x≈21~187 / y≈1211~1243，ROI 留足余量；OCR 文本可能带「！」或被拆字。
_SUPPORT_CALL_ROI = (0, 1180, 250, 100)
_SUPPORT_CALL_KEYWORDS = ("多人支援", "支援请求")
# 「呼叫支援」弹窗：标题锚点（截图里在弹窗顶部中央）+ 三个勾选行
_SUPPORT_DIALOG_ROI = (180, 170, 360, 110)
_SUPPORT_DIALOG_TEXT = "呼叫支援"
_SCOPE_ROW_ROI = (20, 720, 690, 140)
# 勾选圈的圆心相对「XX可见」文字框左边缘的横向偏移：真机实测三个都一样是 **-22px**
# （全体可见 box.x=121→圆心 99；好友 312→290；骑士团 503→480），纵向取文字框中心。
_SCOPE_CIRCLE_OFFSET = -22
_SCOPE_SEARCH = 10
_SCOPE_SAMPLE_HALF = 7
# 勾选态是**亮蓝实心圆点**，未勾选是深色空心圈：用蓝色通道亮度 + B-R 差判定。
_SCOPE_BLUE_MIN = 90
_SCOPE_BLUE_DELTA = 35
_SCOPE_BLUE_ON = 0.45
# 「发送」按钮：真机实测按钮本体在 (208~515, 931~1048)（比我按 608 图换算的低了一截），
# 主 ROI 取按钮内部，认不到再退到弹窗下半部分（见 _send_support）。
_SEND_BTN_ROI = (200, 925, 330, 135)
_SEND_BTN_WIDE_ROI = (60, 880, 600, 200)
_SEND_BTN_TEXT = "发送"
# ---- 「好友申请」弹窗（用了陌生玩家助战，结算后必弹）--------------------------------
# 实机 OCR（2026-10-17 09:33 机神降临结算，同一套弹窗组件）：
#   标题「好友申请」(289,361,141,43)、正文「是否向该名玩家发送好友申请」(176,742,366,32) score 1.000、
#   左侧「取消」(157,849,70,40) score 1.000，右侧是「发送申请」——**只点取消**，绝不给别的玩家发申请。
# 门控用正文整句（唯一、不会误伤别的弹窗的「取消」），点击目标仍用独立的「取消」识别，
# 与「玩法挑战」的 GameplayChallenge_FriendCancel 同一个 ROI 口径。
# ROI / 文案与公共异常节点 Exception_FriendRequest 保持一致（那边 expected 用其中的短片段）。
_FRIEND_POPUP_ROI = (150, 720, 440, 80)
_FRIEND_POPUP_TEXT = "是否向该名玩家发送好友申请"
# 正文整句偶发丢字时的第二个门控：标题「好友申请」（实机 box (289,361,141,43)）。
_FRIEND_TITLE_ROI = (240, 330, 240, 90)
_FRIEND_TITLE_TEXT = "好友申请"
# 结算点完「确定」到弹窗渲染有延迟（实测 1~2s），所以离开结算页前要留一个有界等待窗口。
_FRIEND_POPUP_WAIT = 4.0
_FRIEND_POPUP_POLL = 0.5
# 胜利结算最多循环几轮（每轮：认出「点击空白处继续」/「确认」/「确定」就点一下）。
# 实测别只点一次「确定」：战利品 → 结算 → 好友申请框可能连着弹，点一次会停在结算页不回主页。
_RESULT_MAX_ROUNDS = 14
# 回主页时用的返回图标模板（与 BackToMainHub_Back 同一套图）。
_BACK_TEMPLATES = ("Daily/BackButton.png", "Daily/BackButton2.png")
_BACK_ROI = (0, 1180, 200, 100)
# 实测：普通页面 0.962~1.000，但**房间页**的返回图标偏暗只有 0.760，阈值取 0.7 兜住。
_BACK_THRESHOLD = 0.7
# 单次点击后的界面刷新等待
_CLICK_SETTLE = 2.0
# 列表/弹窗滚动后的等待
_SCROLL_SETTLE = 1.2


def _parse_times(raw: object, *, allow_zero: bool) -> int | None:
    """把「次数」解析为 0-9999（allow_zero）或 1-9999 的整数；非法返回 None。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float) and raw.is_integer():
        value = int(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text.lstrip("+").isdigit():
            return None
        value = int(text)
    else:
        return None
    low = MIN_TIMES if allow_zero else 1
    if value < low or value > MAX_TIMES:
        return None
    return value


def _node_expected(context: Context, node_name: str) -> list[str]:
    """读数据节点 `recognition.param.expected`（选项的落点）；读不到返回空表。"""
    try:
        node = context.get_node_data(node_name) or {}
    except Exception as error:  # noqa: BLE001  读不到按"没配置"处理，不能因此崩掉整段
        logger.warning("副本挑战：读取节点 {} 失败：{}", node_name, error)
        return []
    expected = ((node.get("recognition") or {}).get("param") or {}).get("expected") or []
    return [str(value) for value in expected]


def _flag_enabled(context: Context, node_name: str) -> bool:
    """读开关载体节点的 enabled；节点读不到按**关闭**处理（本段会消耗体力，绝不擅自消费）。"""
    try:
        node = context.get_node_data(node_name) or {}
    except Exception as error:  # noqa: BLE001
        logger.warning("副本挑战：读取开关节点 {} 失败：{}", node_name, error)
        return False
    if "enabled" not in node:
        logger.warning("副本挑战：开关节点 {} 没有 enabled 字段（未预定义？），按关闭处理。", node_name)
        return False
    return bool(node.get("enabled"))


def _match_template(
    context: Context,
    roi: Sequence[float],
    template: str,
    threshold: float,
    img: Any = None,
) -> Any:
    """在 ROI 里做模板匹配，命中返回识别框，否则 None。

    `img` 可传入已截好的一帧，连续比对多张模板时省掉重复截图。
    """
    if img is None:
        img = _screencap(context)
    if img is None:
        return None
    try:
        detail = context.run_recognition(
            "DungeonChallenge_TemplateProbe",
            img,
            {
                "DungeonChallenge_TemplateProbe": {
                    "recognition": {
                        "type": "TemplateMatch",
                        "param": {
                            "roi": [int(v) for v in roi],
                            "template": [template],
                            "threshold": threshold,
                        },
                    },
                    "action": {"type": "DoNothing"},
                }
            },
        )
    except Exception as error:  # noqa: BLE001  识别异常按"没命中"处理
        logger.warning("副本挑战：模板匹配 {} 异常：{}", template, error)
        return None
    if detail is None or not detail.hit:
        return None
    return detail.box


def _screencap(context: Context) -> Any:
    """截一帧（BGR 数组）；失败返回 None。"""
    try:
        return context.tasker.controller.post_screencap().wait().get(True)
    except Exception as error:  # noqa: BLE001
        logger.warning("副本挑战：截图失败：{}", error)
        return None


def _roi_around_box(box: object, pad_x: int = 180, pad_y: int = 55) -> list[int]:
    """以识别框为中心扩一圈当轮询 ROI：用实测位置反推，比写死坐标更稳且同样快。"""
    cx = _box_center_x(box)
    cy = _box_center_y(box)
    return [max(0, cx - pad_x), max(0, cy - pad_y), pad_x * 2, pad_y * 2]


def _ocr_index(context: Context, roi: Sequence[float]) -> dict[str, Any]:
    """一次 OCR 返回 {文本: 识别框}：同一屏要判多个文案时共用一帧，省掉重复截图。

    战斗里要高频判「跳过回合 / 回合开始」两个状态，逐条 `_find_in` 会各自截一帧，
    在「跳过回合」只有很窄可点窗口的情况下就是致命的慢。
    """
    index: dict[str, Any] = {}
    for text, box in _ocr(context, roi):
        index.setdefault(text.strip(), box)
    return index


def _ocr_on(context: Context, img: Any, roi: Sequence[float]) -> list[tuple[str, Any]]:
    """在**已经截好的那一帧**上做一次 OCR（不再截图）。

    实测单次 screencap（adb 往返）≈177ms，而 OCR 本身只 10~40ms（窄 ROI 10ms / 720x280 底部带 41ms），
    也就是说「多调一次 `_find_in`」的代价几乎全是多截一帧。所以高频判定必须共用同一帧。
    """
    pipeline = {
        "PickSupport_OCR": {
            "recognition": {"type": "OCR", "param": {"roi": list(roi)}},
            "action": {"type": "DoNothing"},
        }
    }
    detail = context.run_recognition("PickSupport_OCR", img, pipeline)
    if detail is None:
        return []
    return [(str(result.text or "").strip(), result.box) for result in (detail.all_results or [])]


def _index_on(context: Context, img: Any, *rois: Sequence[float]) -> dict[str, Any]:
    """在**已经截好的那一帧**上 OCR 多个 ROI，合并成 {文本: 框}（不再截图）；无帧返回空 dict。

    战斗/结算的高频判定都走这里：一次 ADB screencap ≈177ms 才是大头，同帧多 OCR 一个 ROI
    只要 15~40ms。所以"多判一个文案"要加成到同一帧上，绝不新增一次截图。
    """
    index: dict[str, Any] = {}
    if img is None:
        return index
    for roi in rois:
        for text, box in _ocr_on(context, img, roi):
            if text:
                index.setdefault(text, box)
    return index


def _frame_index(context: Context, *rois: Sequence[float]) -> dict[str, Any]:
    """截**一帧**，对同一帧依次 OCR 多个 ROI，合并成 {文本: 框}；截不到帧返回空 dict。"""
    return _index_on(context, _screencap(context), *rois)


def _pick_exact(index: dict[str, Any], *texts: str) -> Any:
    """在 index 里按**完全相等**找第一个命中的框（OCR 偶尔带空格，比较时去掉空白）。"""
    for text, box in index.items():
        key = text.replace(" ", "")
        if any(key == wanted.replace(" ", "") for wanted in texts):
            return box
    return None


def _pick_contains(index: dict[str, Any], *texts: str) -> Any:
    """在 index 里按**包含**找第一个命中的框（「今日剩余挑战次数：1/2」这类带后缀的锚点用）。"""
    for text, box in index.items():
        key = text.replace(" ", "")
        if any(wanted.replace(" ", "") in key for wanted in texts):
            return box
    return None


def _click_settle_once(
    context: Context,
    box: Any,
    texts: Sequence[str],
    *rois: Sequence[float],
    timeout: float = 4.0,
) -> dict[str, Any] | None:
    """点一下结算按钮，然后**按帧轮询到画面真的切走**（按钮消失或移位），返回切换瞬间那一帧的 index。

    为什么不再 `time.sleep(_CLICK_SETTLE)`：结算屏切换实测 0.3~0.8s，固定睡 2s 全是白等，
    而且多屏连点时每屏都白等一次；反过来「点完立刻再点」又可能在转场屏上点空/点到别的按钮。
    这里用「同一帧里这个按钮消失或挪位」当切换判据，既快又不会重复点同一屏。
    """
    old_x = _box_center_x(box)
    old_y = _box_center_y(box)
    _click_box(context, box)
    deadline = time.time() + timeout
    while time.time() < deadline:
        index = _frame_index(context, *rois)
        if not index:
            continue  # 截图失败：直接重试，别当成"已切换"
        same = _pick_exact(index, *texts)
        if same is None:
            return index  # 按钮没了 = 已经进入下一屏，这一帧 index 直接给调用方复用
        if abs(_box_center_x(same) - old_x) > 12 or abs(_box_center_y(same) - old_y) > 12:
            return index  # 按钮挪位 = 换了新的屏（同一文案的新按钮）
        time.sleep(0.05)
    return None


def _wait_contains(context: Context, roi: Sequence[float], wanted: str, timeout: float) -> Any:
    """有界等待"包含某子串"的文本出现（「今日剩余挑战次数：1/2」这类会带后缀的锚点用）。"""
    deadline = time.time() + timeout
    while True:
        box = _find_contains(context, roi, wanted)
        if box is not None:
            return box
        if is_stopping(context) or time.time() >= deadline:
            return None
        time.sleep(0.8)


def _on_hub(context: Context) -> bool:
    """是否停在主页（底部导航第一个按钮「惑星之间」）。"""
    return _find_in(context, _HUB_ROI, _HUB_TEXT) is not None


def _on_series_page(context: Context) -> bool:
    """是否停在副本挑战页（系列页）—— 以「切换系列」为锚点。"""
    return _find_in(context, _SWITCH_SERIES_ROI, _SWITCH_SERIES_TEXT) is not None


def _dismiss_leave_room(context: Context) -> bool:
    """「确认退出房间？」弹窗在场时点「确定」；不在场时零开销 no-op。"""
    if _find_contains(context, _LEAVE_ROOM_ROI, _LEAVE_ROOM_TEXT) is None:
        return False
    confirm = _find_in(context, _DIALOG_CONFIRM_ROI, _DIALOG_CONFIRM_TEXT)
    if confirm is None:
        logger.warning("副本挑战：看到「确认退出房间」弹窗但认不到「确定」，不盲点。")
        return False
    logger.info("副本挑战：关闭「确认退出房间」弹窗（点确定退出房间）。")
    _click_box(context, confirm)
    time.sleep(_CLICK_SETTLE)
    return True


def _dismiss_friend_request(context: Context, timeout: float = 0.0) -> bool:
    """关掉结算后弹出的「好友申请」框：只点「取消」，绝不给别的玩家发申请。

    必须先用正文「是否向该名玩家发送好友申请」确认弹窗在场，再点左侧「取消」——
    这个坐标在别的弹窗上也是「取消」（例如「快速收获」确认框的取消在 (172,801,59,37)），
    只凭按钮文案会把正常流程的弹窗误点掉。
    `timeout > 0`：先有界等待弹窗出现（结算「确定」→ 弹窗渲染实测会晚 1~2s）；
    `timeout = 0`：看一眼就走，给高频轮询的路径用（结算循环 / 退主页循环）。
    """
    deadline = time.time() + max(0.0, timeout)
    while True:
        present = _find_contains(context, _FRIEND_POPUP_ROI, _FRIEND_POPUP_TEXT) is not None
        if not present:
            # 正文整句容易被 OCR 抖动打断，再用弹窗标题兜一层（两个门控都必须命中同一处弹窗才点）
            present = _find_contains(context, _FRIEND_TITLE_ROI, _FRIEND_TITLE_TEXT) is not None
        if present:
            cancel = _find_in(context, _FRIEND_CANCEL_ROI, _FRIEND_CANCEL_TEXT)
            if cancel is None:
                logger.warning("副本挑战：看到「好友申请」弹窗但认不到「取消」，不盲点。")
                return False
            logger.info("副本挑战：结算后弹出「好友申请」，点「取消」（不发送申请）。")
            _click_box(context, cancel)
            time.sleep(_CLICK_SETTLE * 0.6)
            return True
        if is_stopping(context) or time.time() >= deadline:
            return False
        time.sleep(_FRIEND_POPUP_POLL)


def _click_back(context: Context) -> bool:
    """点左下角「返回」图标（与 BackToMainHub_Back 同一套模板；子页面都有）。"""
    for template in _BACK_TEMPLATES:
        box = _match_template(context, _BACK_ROI, template, _BACK_THRESHOLD)
        if box is not None:
            _click_box(context, box)
            time.sleep(_CLICK_SETTLE)
            return True
    return False


def _back_to_hub(context: Context) -> bool:
    """退到主页：先关「好友申请」/「退出房间」两个弹窗，再逐级点返回图标。"""
    for _ in range(8):
        # ⚠️ 弹窗判定必须在「是否已到主页」之前：「好友申请」是模态框，主页锚点（左下角导航）
        #    被它盖不住时照样能 OCR 到「惑星之间」——先判主页会带着弹窗返回 True，
        #    下一轮点「副本」就点在弹窗上，白丢一次挑战机会（实机现象）。
        if _dismiss_friend_request(context):
            continue
        if _on_hub(context):
            return True
        if _dismiss_leave_room(context):
            continue
        if not _click_back(context):
            # 返回图标认不到：可能是刚弹出来的「好友申请」把按钮盖住了，再等一个窗口就重试
            if _dismiss_friend_request(context, _FRIEND_POPUP_WAIT):
                continue
            break
    return _on_hub(context)


def _read_remaining(context: Context) -> int | None:
    """读详情页「今日剩余挑战次数：N/M」里的 N；读不到返回 None（不猜、不拦）。"""
    for text, _box in _ocr(context, _DETAIL_ANCHOR_ROI):
        match = _REMAIN_PATTERN.search(text.replace(" ", ""))
        if match:
            return int(match.group(1))
    return None


def _goto_series_page(context: Context) -> bool:
    """从任意页面走到副本挑战页（系列页）：主页 →「副本」。"""
    if _on_series_page(context):
        return True
    if not _back_to_hub(context):
        logger.error("副本挑战：退不回主页（认不到「惑星之间」），本次放弃。")
        return False
    nav = _find_in(context, _HUB_DUNGEON_NAV_ROI, _HUB_DUNGEON_NAV_TEXT)
    if nav is None:
        logger.error("副本挑战：主页底部导航认不到「副本」按钮，本次放弃。")
        return False
    logger.info("副本挑战：主页点底部导航「副本」。")
    _click_box(context, nav)
    time.sleep(_CLICK_SETTLE + 0.8)
    if _on_series_page(context):
        return True
    logger.error("副本挑战：点「副本」后没进副本挑战页（认不到「切换系列」），本次放弃。")
    return False


def _scroll_list_to_top(context: Context) -> None:
    """把副本列表滑回最上面（避免上一轮停在底部时漏掉靠前的卡片）。"""
    for _ in range(3):
        context.tasker.controller.post_swipe(*_LIST_SWIPE_TO_TOP, 400).wait()
        time.sleep(_SCROLL_SETTLE)


def _scan_dungeon_list(context: Context, keyword: str, screen: int):
    """扫当前这一屏的副本标题，命中关键字返回识别框，否则 None。"""
    for text, box in _ocr(context, _LIST_ROI):
        if keyword in text.replace(" ", ""):
            logger.info("副本挑战：第 {} 屏找到副本「{}」（{}）。", screen, keyword, text.strip())
            return box
    return None


def _find_dungeon_card(context: Context, keyword: str, reset_top: bool = True):
    """在副本列表里找目标副本卡片，返回标题识别框；找不到返回 None。

    卡片标题形态：`野猪统领-困难` / `天阳御者·法厄同` / `女武神艾莉瑞尔难度1今日剩余次数:1/1`
    （传说副本会把难度和次数一起粘进来，且偶发丢字），所以用**关键字包含**匹配。

    扫描顺序：先回顶 → 一路**下滑**到底（每个系列 5~7 个副本，一屏 4 张卡）；
    到底仍没找到，再**上滑**回去复扫一遍 —— OCR 偶发丢字/列表回弹会漏掉某一屏，
    正向扫到底就放弃会误判"没有这个副本"（实机就是这样漏掉猩猩王的）。

    `reset_top=False`：沿用当前滚动位置直接找 —— 图钉被列表边缘裁掉后推移时用，
    否则每次都会被重新拉回顶部，白跑好几屏 OCR。
    """
    if reset_top:
        _scroll_list_to_top(context)

    box = _scan_dungeon_list(context, keyword, 1)
    if box is not None:
        return box
    for step in range(3):
        context.tasker.controller.post_swipe(*_LIST_SWIPE_TO_BOTTOM, 400).wait()
        time.sleep(_SCROLL_SETTLE)
        box = _scan_dungeon_list(context, keyword, step + 2)
        if box is not None:
            return box

    # 下滑扫到底还没找到 → 反向（上滑）再复扫，覆盖 OCR 偶发丢字的那一屏
    for step in range(3):
        context.tasker.controller.post_swipe(*_LIST_SWIPE_TO_TOP, 400).wait()
        time.sleep(_SCROLL_SETTLE)
        box = _scan_dungeon_list(context, keyword, step + 5)
        if box is not None:
            return box
    return None


def _click_go_icon(context: Context, name_box: object) -> bool:
    """点目标卡片右侧「前往」图钉：以标题框中心为锚推算 ROI，再做模板匹配。"""
    time.sleep(_SCROLL_SETTLE)  # 等列表回弹/惯性停住，否则点击会被当成"停下滚动"而丢掉
    center_y = _box_center_y(name_box)
    roi = (
        _PIN_ROI_X,
        max(0, center_y + _PIN_ROI_TOP),
        _PIN_ROI_W,
        _PIN_ROI_H,
    )
    img = _screencap(context)
    pin = _match_template(context, roi, _PIN_TEMPLATE, _PIN_THRESHOLD, img)
    if pin is None:
        # 贴在列表底边的卡片：图钉被底栏渐变压暗，相似度掉到 0.68 左右，但位置和点击都正常。
        # 只有图钉中心还在底栏按钮带以上才允许用放宽阈值命中，压到按钮底下的必须靠滚动处理。
        loose = _match_template(context, roi, _PIN_TEMPLATE, _PIN_THRESHOLD_CLIPPED, img)
        if loose is not None and _box_center_y(loose) < _LIST_FOOTER_TOP:
            logger.info(
                "副本挑战：目标卡片贴在列表底边，图钉被压暗，按放宽阈值 {} 命中 ({}, {})。",
                _PIN_THRESHOLD_CLIPPED,
                _box_center_x(loose),
                _box_center_y(loose),
            )
            pin = loose
    if pin is None:
        logger.warning("副本挑战：目标卡片右侧没认出「前往」图钉（ROI={}），可能被列表边缘裁掉。", roi)
        return False
    logger.info("副本挑战：点「前往」图钉 ({}, {})。", _box_center_x(pin), _box_center_y(pin))
    _click_box(context, pin)
    time.sleep(_CLICK_SETTLE + 1.0)
    return True


def _match_series_popup(context: Context, templates: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """在「切换系列」弹窗区域里比对一组系列横幅，返回 {系列名: 识别框}（共用一帧截图）。"""
    img = _screencap(context)
    if img is None:
        return {}
    hits: dict[str, Any] = {}
    for name, template in templates:
        box = _match_template(context, _SERIES_POPUP_ROI, template, _SERIES_TEMPLATE_THRESHOLD, img)
        if box is not None:
            hits[name] = box
    return hits


def _open_series_popup(context: Context) -> bool:
    """打开「切换系列」弹窗。

    实测坑：列表刚滑动过（带惯性/回弹）时点「切换系列」会被当成"停下滚动"而丢掉，
    所以这里用「弹窗里能否认出任意一个系列横幅」作为**开没开**的判据，最多点 3 次；
    另外弹窗开着时再点一次这个按钮会把它关掉，因此每次点之前先确认它没开着。
    """
    all_templates = list(_SERIES_TEMPLATES.items())
    for _ in range(3):
        if _match_series_popup(context, all_templates):
            return True
        time.sleep(_SCROLL_SETTLE)
        entry = _find_in(context, _SWITCH_SERIES_ROI, _SWITCH_SERIES_TEXT)
        if entry is None:
            logger.error("副本挑战：认不到「切换系列」入口，没法切系列。")
            return False
        _click_box(context, entry)
        time.sleep(_CLICK_SETTLE)
    if _match_series_popup(context, all_templates):
        return True
    logger.error("副本挑战：「切换系列」弹窗没能打开，本次放弃。")
    return False


def _switch_series(context: Context, series: str) -> bool:
    """「切换系列」弹窗里选到目标系列（按横幅图模板匹配，命中才点）。"""
    template = _SERIES_TEMPLATES.get(series)
    if template is None:
        logger.error("副本挑战：未知系列「{}」（可选：{}）。", series, "/".join(_SERIES_TEMPLATES))
        return False
    if not _open_series_popup(context):
        return False

    # 弹窗一次只看得下约 3.5 张卡，且打开时停在当前系列那一屏：
    # 依次在「打开时的位置」「滑到顶」「滑到底」三个状态里找目标系列横幅。
    for step in range(3):
        box = _match_series_popup(context, [(series, template)]).get(series)
        if box is not None:
            logger.info("副本挑战：在「切换系列」弹窗里点系列「{}」。", series)
            _click_box(context, box)
            time.sleep(_CLICK_SETTLE + 0.8)
            return True
        if step == 0:
            for _ in range(2):
                context.tasker.controller.post_swipe(*_SERIES_POPUP_SWIPE_TO_TOP, 400).wait()
                time.sleep(_SCROLL_SETTLE * 0.5)
        else:
            for _ in range(2):
                context.tasker.controller.post_swipe(*_SERIES_POPUP_SWIPE_TO_BOTTOM, 500).wait()
                time.sleep(_SCROLL_SETTLE * 0.5)
    logger.error("副本挑战：切换系列弹窗里没认出「{}」系列横幅，本次放弃。", series)
    return False


def _tab_regex(expected: list[str]):
    """把选项给的页签文案编译成整串匹配正则（支持「简单|普通|难度1」这种并列写法）。"""
    pattern = next((value.strip() for value in expected if value.strip()), "")
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error:
        logger.warning("副本挑战：难度页签文案「{}」不是合法正则，按字面量匹配。", pattern)
        return re.compile(re.escape(pattern))


def _tab_index(expected: list[str]) -> int | None:
    """从页签文案解析档位序号：难度N → N；简单/普通 → 1；困难 → 2；噩梦 → 3。"""
    pattern = next((value.strip() for value in expected if value.strip()), "")
    for token in pattern.split("|"):
        token = token.strip()
        match = re.fullmatch(r"难度(\d+)", token)
        if match:
            return int(match.group(1))
        if token in ("简单", "普通"):
            return 1
        if token == "困难":
            return 2
        if token == "噩梦":
            return 3
    return None


def _grid_row_is_trustworthy(expected: list[str]) -> bool:
    """实测档位栅格（y = 236 + 85*(n-1)）只对「难度N」页签成立。

    反例（2026-10-18 实机）：潘塔菲卡 / 赫拉斯瓦尔格 的详情页只有 困难 / 噩梦 两档，
    页签整体居中，困难落在第 1 行（y≈236）。若按「困难 = 档位 2」去套栅格，会点到 y≈321
    的噩梦 —— 那是会真消耗体力的错档。所以只有选项本身就是「难度N」时才允许退回栅格。
    """
    pattern = next((value.strip() for value in expected if value.strip()), "")
    tokens = [token.strip() for token in pattern.split("|") if token.strip()]
    return bool(tokens) and all(re.fullmatch(r"难度\d+", token) for token in tokens)


def _tab_selected(img: Any, center_y: int) -> bool:
    """判定**某一行**难度页签是否处于选中态（选中档有高亮底纹：金色像素约 1000，未选约 300）。

    ⚠️ 必须按「实际点中的那一行」校验，不能用「档位序号 → 栅格行号」换算：
    实测同一个「困难」在 3 档页（简单/困难/噩梦）里是第 2 行（y≈321），
    在 2 档页（困难/噩梦，潘塔菲卡 / 赫拉斯瓦尔格）里是第 1 行（y≈236）。
    按序号换算会去看另一行，把「已经选中」误判成「没高亮」，流程随即放弃 → 直接点了返回
    （实机 2026-10-18 18:52 三次复现，配置：潘塔菲卡 / 困难 / 创建房间）。
    """
    center = int(center_y)
    top = max(0, center - _TAB_BAND_HALF)
    band = img[top : center + _TAB_BAND_HALF, _TAB_BAND_X[0] : _TAB_BAND_X[1]].tolist()
    gold = 0
    for row in band:
        for pixel in row:
            blue, green, red = float(pixel[0]), float(pixel[1]), float(pixel[2])
            if red > 140 and green > 105 and blue < 120:
                gold += 1
    return gold >= _TAB_GOLD_SELECTED


def _scan_difficulty_tabs(context: Context) -> list[tuple[str, Any]]:
    """扫左侧难度页签列，返回 [(文案, 识别框)]。

    只收已知的页签文案，避免把面板左缘的其它文字（LV.70 / 今日剩余挑战次数 …）当成页签。
    **单难度副本整列不存在** → 返回空表，由上层据此跳过难度选择。
    """
    tabs: list[tuple[str, Any]] = []
    for text, box in _ocr(context, _DIFFICULTY_TAB_ROI):
        cleaned = text.replace(" ", "")
        if _DIFFICULTY_TAB_TOKEN.fullmatch(cleaned):
            tabs.append((cleaned, box))
    return tabs


def _select_difficulty(context: Context, expected: list[str]) -> bool:
    """选难度；**没有难度页签的单难度副本**直接跳过，交给「创建房间」。

    判定顺序（同时也是「适配后续版本加难度」的关键——每次都按当前画面重新判，不写死系列名）：
      1. 页签里认到目标文案 → 点它 + 校验高亮（正常路径；将来这些系列加了难度，会自动走回这里）；
      2. 页签列里**一个难度页签都没有** → 该副本当前只有一个难度，跳过选择直接去创建房间；
      3. 有页签但认不到目标文案 → 只有「难度N」页签才允许退回栅格，其余放弃（宁可不打，也不打错难度）。
    """
    regex = _tab_regex(expected)
    index = _tab_index(expected)
    tabs = _scan_difficulty_tabs(context)
    box = None
    matched_text = ""
    if regex is not None:
        for text, found in tabs:
            if regex.fullmatch(text):
                box = found
                matched_text = text
                break
    if box is not None:
        click_x = _box_center_x(box)
        click_y = _box_center_y(box)
        logger.info("副本挑战：点难度页签「{}」({}, {})。", matched_text, click_x, click_y)
        _click_box(context, box)
    elif not tabs:
        # 单难度副本：整列页签都不存在（不是"OCR 丢字"，是这类副本本身没有可选难度）。
        # 不点任何难度、直接去创建房间；将来版本加了难度，这里会重新扫到页签，走回上面的正常路径。
        logger.info(
            "副本挑战：详情页没有难度页签（该副本当前只有一个难度），跳过难度选择，直接创建房间。"
            "（任务设置里的难度「{}」对这类副本不生效）",
            expected,
        )
        return True
    elif index is not None and _grid_row_is_trustworthy(expected):
        click_x = _DIFFICULTY_TAB_X
        click_y = _DIFFICULTY_TAB_Y0 + _DIFFICULTY_TAB_STEP * (index - 1)
        logger.warning(
            "副本挑战：没认出难度页签「{}」，按实测档位栅格点第 {} 档 (x={}, y={})。",
            expected,
            index,
            click_x,
            click_y,
        )
        context.tasker.controller.post_click(click_x, click_y).wait()
    else:
        logger.error(
            "副本挑战：详情页有难度页签 {}，但认不到目标难度「{}」，本次放弃（不盲点，免打错难度）。",
            [text for text, _ in tabs],
            expected,
        )
        return False
    time.sleep(_CLICK_SETTLE * 0.8)

    img = _screencap(context)
    if img is None:
        logger.warning("副本挑战：截图失败，无法校验难度页签是否选中，按已选中继续。")
        return True
    if not _tab_selected(img, click_y):
        logger.error(
            "副本挑战：点完 y={} 这一行页签后没有高亮（该难度可能未解锁/点击没生效），本次不创建房间（未消耗体力）。",
            click_y,
        )
        return False
    logger.info("副本挑战：难度已切到「{}」(y={})。", matched_text or expected, click_y)
    time.sleep(1.0)  # 切难度后页面会刷一次，留一点时间再去找「创建房间」
    return True


def _wait_room_setup(context: Context, timeout: float) -> str | None:
    """等「助战选择」弹窗或编队页出现，返回 'support' / 'formation' / None。"""
    deadline = time.time() + timeout
    while True:
        if _find_in(context, _SUPPORT_TITLE_ROI, _SUPPORT_TITLE_TEXT) is not None:
            return "support"
        if _find_in(context, _FORMATION_ANCHOR_ROI, _FORMATION_ANCHOR_TEXT) is not None:
            return "formation"
        if is_stopping(context) or time.time() >= deadline:
            return None
        time.sleep(0.8)


def _pick_support_prefs(context: Context) -> bool:
    """「助战选择」弹窗里按**全局助战偏好**选魔物，选完停在编队页。

    直接复用玩法挑战的 `PickSupport`：副本的助战弹窗与玩法/机神是同一个组件
    （实机核对：元素页签 134/244/358/472/586、条目「Rank→魔物→buff」三行、
    编队页「助战」按钮 (611,794) 全部一致），唯一差别是编队页底部是「确定」不是「开始战斗」，
    所以只把锚点换成「主要成员」，选助战的整套逻辑（切元素页签 → 按 神/魔/属攻 比对 → 重选）
    原样复用。偏好来自 interface.json 的六个全局选项（`X属性编队助战`）。
    """
    prefs = _read_support_prefs(context)
    logger.info("副本挑战：助战偏好 = {}，开始选择助战。", prefs or "未配置（不校验类型）")
    support = PickSupport(
        formation_roi=_FORMATION_ANCHOR_ROI,
        formation_text=_FORMATION_ANCHOR_TEXT,
    )
    if not support.pick(context, prefs):
        logger.error("副本挑战：助战选择没能进到编队页，本次放弃（未开始战斗）。")
        return False
    return True


def _create_room(context: Context) -> bool:
    """副本详情页「创建房间」→ 助战选择 → 编队页「确定」（这一步还不会消耗体力）。

    实测坑：刚点过难度页签时页面还在刷新，第一次「创建房间」会被吃掉，
    所以这里点完要确认真的进了「助战选择 / 编队页」，没进就重新定位按钮再点一次。
    """
    create = _wait_for(context, _CREATE_ROOM_ROI, _CREATE_ROOM_TEXT, 10)
    if create is None:
        logger.error("副本挑战：副本详情页没等到「创建房间」按钮，本次放弃。")
        return False

    for attempt in range(2):
        if attempt:
            logger.info("副本挑战：上一次点「创建房间」没反应，重新点一次。")
        _click_box(context, create)
        time.sleep(0.8)  # 等弹窗画出来：点完「创建房间」若暂离位已满，弹窗会盖在这一帧上
        # 「暂离已满」门控（用户需求）：挂机暂离位满了会弹框，本次挑战不会真正开始。
        # 命中就交给公共异常链（战斗列表 → 待确认 → 领报酬 → 回主页），本次放弃。
        if handle_away_full(context):
            return False
        state = _wait_room_setup(context, 10)
        if state == "support":
            if not _pick_support_prefs(context):
                return False
            time.sleep(_CLICK_SETTLE)
            state = "formation"
        if state is None:
            create = _wait_for(context, _CREATE_ROOM_ROI, _CREATE_ROOM_TEXT, 4)
            if create is None:
                break
            continue
        confirm = _wait_for(context, _FORMATION_CONFIRM_ROI, _DIALOG_CONFIRM_TEXT, 20)
        if confirm is None:
            logger.error("副本挑战：没等到编队页「确定」，本次放弃（未开始战斗）。")
            return False
        _click_box(context, confirm)
        time.sleep(_CLICK_SETTLE)
        return _guard_after_formation_confirm(context)

    logger.error("副本挑战：点「创建房间」后没能进到编队页，本次放弃（未开始战斗）。")
    return False


def _guard_after_formation_confirm(context: Context) -> bool:
    """助战选择完成、点编队页「确定」之后的超时判断（用户要求）。

    只有确认已经进到房间页（底部「开始战斗」）才算这一步走通；15s 内进不去说明过渡失败
    （卡在编队页 / 被弹窗盖住 / 断线重连 / 加载不出来），直接进**公共异常处理流程**
    Exception_Start 收拾，再返回 False 结束本轮 —— 绝不带着错误画面去盲点「开始战斗」。
    """
    if _wait_for(context, _ROOM_START_ROI, _ROOM_START_TEXT, _FORMATION_TO_ROOM_TIMEOUT) is not None:
        return True
    logger.error(
        "副本挑战：点编队「确定」后 {} 秒内没进到房间页「开始战斗」，进入公共异常处理流程。",
        _FORMATION_TO_ROOM_TIMEOUT,
    )
    context.run_task("Exception_Start")
    return False


def _start_battle(context: Context) -> bool:
    """房间页「开始战斗」→（可能弹「是否开始战斗?」）确定。⚠️ 从这里开始真实消耗体力。"""
    start = _wait_for(context, _ROOM_START_ROI, _ROOM_START_TEXT, 20)
    if start is None:
        logger.error("副本挑战：没等到房间页「开始战斗」，本次放弃（未消耗体力）。")
        return False
    logger.info("副本挑战：点「开始战斗」（本次开始真实消耗体力/BP）。")
    _click_box(context, start)
    time.sleep(_CLICK_SETTLE)
    confirm = _wait_for(context, _DIALOG_CONFIRM_ROI, _DIALOG_CONFIRM_TEXT, 8)
    if confirm is not None:
        _click_box(context, confirm)
        time.sleep(_CLICK_SETTLE)
    return True


def _blue_ratio_at(img: Any, x: int, y: int) -> float | None:
    """取样 (x,y) 附近一小块，返回「勾选蓝」像素占比；取不到图返回 None。"""
    half = _SCOPE_SAMPLE_HALF
    x0 = max(0, x - half)
    y0 = max(0, y - half)
    rows = img[y0 : y0 + half * 2, x0 : x0 + half * 2].tolist()
    total = 0
    blue = 0
    for row in rows:
        for pixel in row:
            b, _g, r = float(pixel[0]), float(pixel[1]), float(pixel[2])
            total += 1
            if b >= _SCOPE_BLUE_MIN and b - r >= _SCOPE_BLUE_DELTA:
                blue += 1
    if total == 0:
        return None
    return blue / total


def _scope_blue_ratio(img: Any, cx: int, cy: int) -> float | None:
    """在 (cx,cy) 附近 ±_SCOPE_SEARCH 的网格里取「最蓝」的一格，返回其蓝色占比。

    勾选圈位置是靠「文字框左边缘 + 固定偏移」估出来的，会有几个像素误差；
    在窗口内取最大值等于把圆心重新对准 —— 未勾选时整窗都没有蓝点，最大值依旧很低。
    """
    if img is None:
        return None
    best = None
    for dy in range(-_SCOPE_SEARCH, _SCOPE_SEARCH + 1, 5):
        for dx in range(-_SCOPE_SEARCH, _SCOPE_SEARCH + 1, 5):
            ratio = _blue_ratio_at(img, cx + dx, cy + dy)
            if ratio is None:
                continue
            best = ratio if best is None else max(best, ratio)
    return best


def _scope_items(context: Context) -> dict[str, Any]:
    """OCR 出三个勾选项「XX可见」的位置，返回 {名称: 文字框}。"""
    names = [name for name, _flag in _SUPPORT_SCOPE_FLAGS]
    found: dict[str, Any] = {}
    for text, box in _ocr(context, _SCOPE_ROW_ROI):
        plain = text.replace(" ", "")
        for name in names:
            if name in plain or name[:2] in plain:  # OCR 偶发拆字：「全体可」「见」
                found.setdefault(name, box)
    return found


def _click_scope_item(context: Context, box: Any, circle: tuple[int, int]) -> bool:
    """点一下勾选项并确认状态真的翻了；先点勾选圈，不行再点文字行。"""
    before_img = _screencap(context)
    before = _scope_blue_ratio(before_img, *circle)
    for x, y in (circle, (_box_center_x(box), _box_center_y(box))):
        context.tasker.controller.post_click(int(x), int(y)).wait()
        time.sleep(0.55)
        after = _scope_blue_ratio(_screencap(context), *circle)
        if before is None or after is None:
            return True  # 认不出就当作生效，避免重复点又把状态翻回去
        if (after >= _SCOPE_BLUE_ON) != (before >= _SCOPE_BLUE_ON):
            return True
    return False


def _apply_support_scope(context: Context, wanted: set[str]) -> None:
    """把「呼叫支援」弹窗里的三个勾选项调成 wanted（游戏内默认全选）。"""
    items = _scope_items(context)
    if len(items) < len(_SUPPORT_SCOPE_FLAGS):
        logger.warning(
            "呼叫支援：只认到 {} 个勾选项（{}），跳过范围调整、按游戏内默认全选直接发送。",
            len(items),
            list(items),
        )
        return

    for name, _flag in _SUPPORT_SCOPE_FLAGS:
        box = items[name]
        circle = (int(box[0]) + _SCOPE_CIRCLE_OFFSET, _box_center_y(box))
        ratio = _scope_blue_ratio(_screencap(context), *circle)
        if ratio is None:
            logger.warning("呼叫支援：「{}」认不出勾选状态，保持原样。", name)
            continue
        checked = ratio >= _SCOPE_BLUE_ON
        want = name in wanted
        logger.info(
            "呼叫支援：「{}」当前{}（blue={:.2f}），期望{}。",
            name,
            "勾选" if checked else "未勾选",
            ratio,
            "勾选" if want else "未勾选",
        )
        if checked == want:
            continue
        if not _click_scope_item(context, box, circle):
            logger.warning("呼叫支援：「{}」点了两次都没切换成功。", name)


def _wait_support_entry(context: Context, timeout: float) -> Any:
    """等左下角「发送多人支援请求」按钮出现（有界）；没有就返回 None（调用方跳过）。"""
    deadline = time.time() + timeout
    while True:
        for text, box in _ocr(context, _SUPPORT_CALL_ROI):
            plain = text.replace(" ", "")
            if any(key in plain for key in _SUPPORT_CALL_KEYWORDS):
                return box
        if time.time() >= deadline:
            return None
        time.sleep(0.6)


def _open_support_dialog(context: Context) -> bool:
    """点左下角「发送多人支援请求」打开「呼叫支援」弹窗（一次点不开就再点）。"""
    entry = _wait_support_entry(context, 3.0)
    if entry is None:
        logger.info("副本挑战：战斗左下角没有「发送多人支援请求」按钮，跳过支援请求。")
        return False

    for attempt in range(3):
        _click_box(context, entry)
        time.sleep(1.2)
        if _find_contains(context, _SUPPORT_DIALOG_ROI, _SUPPORT_DIALOG_TEXT) is not None:
            logger.info("副本挑战：已打开「呼叫支援」弹窗。")
            return True
        entry = _wait_support_entry(context, 3.0)
        if entry is None:
            logger.warning("副本挑战：点「发送多人支援请求」后按钮消失、弹窗也没开，放弃发送。")
            return False
        logger.info("副本挑战：第 {} 次点「发送多人支援请求」没打开弹窗，再点一次。", attempt + 1)
    logger.warning("副本挑战：3 次都没打开「呼叫支援」弹窗，放弃发送。")
    return False


def _send_support(context: Context) -> bool:
    """点「发送」，并确认弹窗已关闭。"""
    for attempt in range(2):
        send = _wait_for(context, _SEND_BTN_ROI, _SEND_BTN_TEXT, 3)
        if send is None:
            # 主 ROI 没认到（分辨率/版式偏差）：退到弹窗下半部分再找一次
            send = _wait_for(context, _SEND_BTN_WIDE_ROI, _SEND_BTN_TEXT, 2)
        if send is None:
            # ⚠️ 关弹窗只点弹窗**上沿以外**的战场区域（(360,175)）：绝不点 (360,1100)，
            #    那里正是战斗中「回合开始」按钮所在行，误点会把自动战斗带偏。
            logger.warning("呼叫支援：没找到「发送」按钮，点弹窗外把它关掉。")
            context.tasker.controller.post_click(360, 175).wait()
            time.sleep(_CLICK_SETTLE * 0.6)
            return False
        _click_box(context, send)
        time.sleep(1.2)
        if _find_contains(context, _SUPPORT_DIALOG_ROI, _SUPPORT_DIALOG_TEXT) is None:
            logger.info("呼叫支援：已发送支援请求。")
            return True
        logger.info("呼叫支援：第 {} 次点「发送」后弹窗还在，再点一次。", attempt + 1)
    logger.warning("呼叫支援：点了 2 次「发送」弹窗仍未关闭。")
    return False


def _call_support(context: Context) -> bool:
    """战斗里发一次多人支援请求（一场战斗只发一次；关闭/没勾范围/没按钮都直接跳过）。"""
    if not _flag_enabled(context, _SUPPORT_CALL_FLAG):
        return False

    wanted = {name for name, flag in _SUPPORT_SCOPE_FLAGS if _flag_enabled(context, flag)}
    if not wanted:
        logger.info("副本挑战：开了「发送多人支援请求」但「支援请求范围」一个都没勾，跳过发送。")
        return False

    logger.info("副本挑战：发送多人支援请求（范围={}）。", "/".join(sorted(wanted)))
    if not _open_support_dialog(context):
        return False
    _apply_support_scope(context, wanted)
    return _send_support(context)


def _call_extra_over(extra_over, context: Context, img: Any) -> bool | None:
    """调用附加结束判据：回调接受 `(context, img)` 时把**当前帧**传进去（省一次截图）。

    老回调只接受 `(context)`，所以按签名参数个数分流；签名取不到时按老签名调用。
    """
    try:
        arity = len(inspect.signature(extra_over).parameters)
    except (TypeError, ValueError):
        arity = 1
    if arity >= 2:
        return extra_over(context, img)
    return extra_over(context)


def _battle_result(
    context: Context,
    extra_over=None,
    img: Any = None,
    index: dict[str, Any] | None = None,
) -> bool | None:
    """判定战斗是否已结束：胜利 True / 失败 False / 仍在战斗中 None。

    `extra_over(context[, img]) -> bool | None`：调用方追加的"战斗是否已经结束"判据（返回 None 表示不表态）。
    终末之战用它把「复活提示」也算作本场失败 —— 否则失败弹窗上没有「作战失败」四个字，
    这里会一直认成"还在战斗"，把 10 分钟战斗预算白等完。

    `img` / `index`：调用方**当前这一帧**的截图与 {文本: 框} 索引，传进来就直接复用。

    ⚡ 为什么要复用同一帧：旧版这里逐条 `_find_in`（点击空白处继续 / 确认 / 作战失败 / 撤退，
    终末之战还外加「复活 + 取消」）合计 **5 次截图 + 6 次 OCR，实测 ≈1.1s**，而它每 6 帧才跑一次。
    「战斗胜利」结算页正好渲染在这一拍里时，从胜利页出现到点下去的间隔就被拉到 1s 以上 ——
    这正是终末之战"战斗胜利识别慢"的真凶。复用调用方的帧之后同一拍只多花 ~90ms（几次同帧 OCR），
    帧耗上限回到 ≈0.35s，胜利页一出现就能在 1 帧（≈0.25s）内点掉。
    """
    if img is None:
        img = _screencap(context)
    if extra_over is not None:
        try:
            extra = _call_extra_over(extra_over, context, img)
        except Exception as error:  # noqa: BLE001  附加判据异常不能影响正常判定
            logger.warning("副本挑战：附加结束判据异常（忽略）：{}", error)
            extra = None
        if extra is not None:
            return extra

    band = index if index is not None else _index_on(context, img, _MGD_BOTTOM_ROI)
    if _pick_exact(band, _WIN_CONTINUE_TEXT, _WIN_CONFIRM_TEXT, _WIN_NEXT_STAGE_TEXT) is not None:
        return True

    if _pick_exact(_index_on(context, img, _GOD_LOSE_RESULT_ROI), _GOD_LOSE_TEXT) is not None:
        logger.warning("副本挑战：本场「作战失败」，点底部「撤退」退出。")
        retreat = _pick_exact(band, "撤退")
        if retreat is None:
            retreat = _find_in(context, _MGD_BOTTOM_ROI, "撤退")
        if retreat is not None:
            _click_box(context, retreat)
            time.sleep(_CLICK_SETTLE)
        return False
    return None


def _fight(
    context: Context,
    timeout: float | None = None,
    extra_over=None,
    win_sink: list | None = None,
) -> bool:
    """推进一场房间战，直到出现胜利结算（底部「点击空白处继续」/「确认」）。

    中间按钮只有两个状态循环：**「跳过回合」→ 点 → 「回合开始」→ … → 「跳过回合」**。

    `timeout` 为空时用本模块默认的 `_BATTLE_TIMEOUT`（600s）；主动战斗（自动挂机任务）
    传自己的上限，避免在意外的"挂机自动战斗"画面上白等 10 分钟。

    ⚠️ 战斗里**只点「跳过回合」**（与玩法挑战 GameplayChallenge_BattleTurn 同一套语义）：
      · 「回合开始」绝对不能点 —— 点它会自己开一回合，把游戏内的自动战斗带偏
        （实机表现为"不管有没有跳过回合，一直在点中间的按钮"）；
      · 「奥义释放 / 奥义关闭」是**开关按钮**，点一下就把全局设置里的奥义释放关掉/打开
        （实机就是被这里误点关掉的）—— 所以战斗推进绝不碰它，也不会去点「自动 / 倍速」。

    `win_sink`：调用方传一个 list 进来，胜利那一帧的 {文本: 框} 会 append 进去。
    结算逻辑拿它就能直接点「挑战下一关」/「继续」，**不用再重新识别一遍**
    （重新识别 = 再截一帧 177ms + OCR，胜利到操作的间隔就是这么被拉长的）。

    「跳过回合」是否要连点由全局任务配置 `Flag_BattleSkipTurn` 决定：
      · 开启：用**窄 ROI**（中间按钮）紧循环高频轮询，认到就立刻点，点完立刻再看下一帧
        —— 那个可点窗口很短，慢一拍就错过一整个回合；
      · 未开启：**完全不识别按钮状态、也不点任何按钮**，只低频轮询「战斗是否结束」。
    """
    budget = _BATTLE_TIMEOUT if timeout is None else max(30.0, float(timeout))
    if not _wait_for_battle_presence(context, _BATTLE_PRESENCE_TIMEOUT):
        logger.warning("副本挑战：{} 秒内没看到战斗画面（战斗剩余时间/第N回合），仍按进入战斗处理并继续推进。",
                       _BATTLE_PRESENCE_TIMEOUT)

    # 多人支援请求：一场战斗只发一次，放在推进回合之前（弹窗会盖住中间按钮，妨碍高频轮询）
    try:
        _call_support(context)
    except Exception as error:  # noqa: BLE001  附加功能，绝不能因为支援请求失败而中断战斗
        logger.warning("副本挑战：发送多人支援请求异常（忽略，继续战斗）：{}", error)

    deadline = time.time() + budget

    if not _flag_enabled(context, _SKIP_TURN_FLAG):
        logger.info("副本挑战：全局「跳过回合」未开启 → 不识别按钮状态、不点任何按钮，只等结算。")
        misses = 0
        while time.time() < deadline:
            # 同样是一帧判到底：胜利按钮和「挑战下一关」都在底部带里，
            # 认到就返回（≤0.5s 反应），认不到才每隔几帧做一次失败/复活判定（画面上部，要另外看）——
            # 那次判定复用**这一帧**（`img` / `index`），不再额外截图。
            img = _screencap(context)
            index = _index_on(context, img, _BATTLE_TURN_WIDE_ROI)
            if _pick_exact(index, _WIN_CONTINUE_TEXT, _WIN_CONFIRM_TEXT, _WIN_NEXT_STAGE_TEXT) is not None:
                logger.info("副本挑战：胜利结算已出（未开跳过回合，仅识别）。")
                if win_sink is not None:
                    win_sink.append(index)
                return True
            misses += 1
            if misses % _BATTLE_RESULT_EVERY == 0:
                result = _battle_result(context, extra_over, img=img, index=index)
                if result is not None:
                    return result
            time.sleep(_BATTLE_RESULT_POLL)
        logger.warning("副本挑战：战斗等待超过 {} 秒仍未结束，放弃本场。", budget)
        return False

    logger.info("副本挑战：全局「跳过回合」已开启 → 每帧判按钮 + 胜利结算，认到「跳过回合」立刻点。")
    probe = _BATTLE_TURN_ROI
    misses = 0
    clicks = 0
    while time.time() < deadline:
        img = _screencap(context)
        if img is None:
            time.sleep(_BATTLE_FAST_SLEEP)
            continue

        # ── 一帧判到底 ────────────────────────────────────────────────────────────
        # 窄 ROI（10ms）先抓「跳过回合」：它可点窗口很窄，必须每帧都看。
        band: dict[str, Any] = {}
        turn = _pick_exact(dict(_ocr_on(context, img, probe)), _BATTLE_TURN_TEXT)
        if turn is None:
            # 窄 ROI 没中：**同一帧**再 OCR 整条底部带（41ms，不再截图——截图 177ms 才是大头）。
            # 这一带同时含「跳过回合 / 回合开始 / 点击空白处继续 / 确认 / 挑战下一关」，
            # 所以胜利画面一到就在这一帧里认出来 ⇒ 识别延迟 = 1 帧 ≈ 0.25s，
            # 旧版要等 misses 攒到 _BATTLE_RESULT_EVERY 再做慢判定，最坏 1.5~2s。
            band = _index_on(context, img, _BATTLE_TURN_WIDE_ROI)
            # 先排除"还在打"：按钮状态优先判。否则战斗中偶尔出现的其它「确认」会被误判成胜利结算。
            turn = _pick_exact(band, _BATTLE_TURN_TEXT)
            if turn is not None:
                # 窄 ROI 版式偏了，但宽带认到了：用实测位置把 ROI 收紧，后续每帧更快
                probe = _roi_around_box(turn)
            elif _pick_contains(band, *_BATTLE_TURN_WAIT_TEXTS) is not None:
                misses = 0
                time.sleep(_BATTLE_FAST_SLEEP)
                continue
            elif _pick_exact(band, _WIN_CONTINUE_TEXT, _WIN_CONFIRM_TEXT, _WIN_NEXT_STAGE_TEXT) is not None:
                # 底部带里既没有「跳过回合」也没有「回合开始」，却有结算按钮 ⇒ 本场结束
                logger.info("副本挑战：胜利结算已出（共点「跳过回合」{} 次），立刻接管结算页。", clicks)
                if win_sink is not None:
                    # 把这一帧的整条索引交给结算逻辑：连「挑战下一关」都省一次识别
                    win_sink.append(band)
                return True

        if turn is not None:
            misses = 0
            clicks += 1
            _click_box(context, turn)
            continue  # 点完立刻看下一帧：按钮此刻会变成「回合开始」

        # 底部带里既没有按钮状态也没有结算按钮：过场动画 / 作战失败 / 复活提示
        # 失败/复活判定复用**这一帧**（band 已 OCR 过底部带，只需再 OCR 失败锚点 + 调用方判据），
        # 单拍成本从 ~1.1s 降到 ~90ms ⇒ 胜利页落在这一拍里也只晚 1 帧（≤0.35s）就能点掉。
        misses += 1
        if misses % _BATTLE_RESULT_EVERY == 0:
            result = _battle_result(context, extra_over, img=img, index=band)
            if result is not None:
                logger.info("副本挑战：战斗结束（共点「跳过回合」{} 次）。", clicks)
                return result
        time.sleep(_BATTLE_FAST_SLEEP)

    logger.warning("副本挑战：战斗等待超过 {} 秒仍未结束，放弃本场。", budget)
    return False


def _settle(context: Context) -> bool:
    """胜利结算：**循环**点「点击空白处继续」/「确认」/「确定」，直到离开结算页。

    实机教训：胜利后不止一屏，而且「确定」会连着弹好几次（战利品 → 结算 → 好友申请框）。
    只点一次「确定」就会停在结算页、回不到主页，所以这里每轮重新识别一次按钮并再点，
    直到既没有可点的按钮、又已经回到房间页/主页（或达到轮数上限）为止。

    ⚡ 性能：每轮**只截一帧**（底部带 + 中部「确定」共用这张图，省掉 3 次重复截图 ≈ 530ms），
    点完用「按钮消失 / 挪位」当切屏判据（`_click_settle_once`），
    所以相邻两次点击的间隔 ≈ 一帧(177ms) + OCR(40ms) + 点击(50ms) ≈ 0.3s，
    而不再是固定 `sleep(_CLICK_SETTLE * 0.8)` = 1.6s 的干等。
    """
    index: dict[str, Any] = {}
    for _round in range(_RESULT_MAX_ROUNDS):
        if not index:
            index = _frame_index(context, _MGD_BOTTOM_ROI, _DIALOG_CONFIRM_ROI)
        if not index:
            time.sleep(0.2)  # 截图失败，稍后重试
            continue

        # 先认按钮：结算浮层可能盖在房间页上，「开始战斗」偶尔仍能被 OCR 读到，
        # 所以必须"有结算按钮就先点按钮"，没有按钮才判定已经离开结算页。
        box = (
            _pick_exact(index, _WIN_CONTINUE_TEXT)
            or _pick_exact(index, _WIN_CONFIRM_TEXT)
            or _pick_exact(index, _DIALOG_CONFIRM_TEXT)
        )
        if box is not None:
            logger.info("副本挑战：结算第 {} 轮，点「继续/确认/确定」。", _round + 1)
            index = (
                _click_settle_once(
                    context,
                    box,
                    (_WIN_CONTINUE_TEXT, _WIN_CONFIRM_TEXT, _DIALOG_CONFIRM_TEXT),
                    _MGD_BOTTOM_ROI,
                    _DIALOG_CONFIRM_ROI,
                )
                or {}
            )
            continue

        if _dismiss_friend_request(context):
            index = {}
            continue

        if _pick_exact(index, _HUB_TEXT) is not None or _pick_contains(index, _ROOM_START_TEXT) is not None:
            # 「确定」→「好友申请」有 1~2s 渲染延迟：交回上层之前先留一个有界窗口把它接住，
            # 否则这一轮会"看着像结算完成"提前返回，下一轮退主页时弹窗还盖着 → 退不出去 →
            # 第二次挑战根本开始不了（实机现象）。
            if _dismiss_friend_request(context, _FRIEND_POPUP_WAIT):
                logger.info("副本挑战：好友申请已取消，结算完成。")
            else:
                logger.info("副本挑战：结算完成，已回到房间页/主页。")
            return True

        index = {}
        time.sleep(0.2)  # 这一屏可能正在切下一屏，看下一帧再看

    logger.warning("副本挑战：结算循环 {} 轮仍未回到房间页/主页。", _RESULT_MAX_ROUNDS)
    return False


def _open_detail_page(context: Context, series: str, keyword: str) -> bool:
    """主页 → 副本挑战页 → **先切到目标系列** → 找到副本 → 点「前往」→ 副本详情页。

    实机教训：先"在当前系列里按关键词找、找不到才切系列"，等于一个系列一个系列地白扫
    （每个系列 5~7 个副本、要 4 屏 OCR，而且停在上次的系列时第一轮必然全落空）。
    现在一律先「切换系列」到目标系列，再在列表里找目标副本。
    """
    if not _goto_series_page(context):
        return False
    logger.info("副本挑战：先切到「{}」系列。", series)
    if not _switch_series(context, series):
        return False

    # 找到卡片 → 点右侧「前往」。卡片可能正好贴在列表底边（标题认得到、图钉被裁掉），
    # 这时先把列表小步推一段再重找，最多试 3 次，绝不直接放弃。
    for attempt in range(3):
        name_box = _find_dungeon_card(context, keyword, reset_top=attempt == 0)
        if name_box is None and attempt > 0:
            name_box = _find_dungeon_card(context, keyword)  # 推歪了就回顶部重扫一遍
        if name_box is None:
            logger.error("副本挑战：「{}」系列里没找到副本「{}」，本次放弃。", series, keyword)
            return False
        if _click_go_icon(context, name_box):
            if _wait_contains(context, _DETAIL_ANCHOR_ROI, _DETAIL_ANCHOR_TEXT, 12) is None:
                logger.error("副本挑战：点「前往」后没进副本详情页（认不到「今日剩余挑战次数」）。")
                return False
            return True
        if attempt == 0:
            logger.info("副本挑战：「{}」卡片贴在列表边缘、图钉不在画面里，把列表推一段再找。", keyword)
            context.tasker.controller.post_swipe(*_LIST_NUDGE_UP, 300).wait()
        else:
            context.tasker.controller.post_swipe(*_LIST_NUDGE_DOWN, 300).wait()
        time.sleep(_SCROLL_SETTLE)

    logger.error("副本挑战：3 次都没能点到「{}」的「前往」图钉，本次放弃。", keyword)
    return False


def _run_once(
    context: Context,
    *,
    series: str,
    keyword: str,
    difficulty: list[str],
) -> str:
    """跑一轮副本挑战，返回 'done' / 'exhausted' / 'fail'。"""
    # 上一轮结算残留的「好友申请」框会挡住系列页/前往图钉，开头先清一次（有则点取消，无则零开销）
    _dismiss_friend_request(context)
    if not _open_detail_page(context, series, keyword):
        _back_to_hub(context)
        return "fail"

    if not _select_difficulty(context, difficulty):
        _back_to_hub(context)
        return "fail"

    # ⚠️ 「今日剩余挑战次数」是**按难度分别计数**的（实测同一副本 难度4 是 1/1、难度5 是 0/1），
    # 所以必须在选完难度之后再读，否则会拿别的难度的次数误导自己。
    remaining = _read_remaining(context)
    if remaining is not None and remaining <= 0:
        logger.info("副本挑战：「{}」该难度今日剩余挑战次数已用尽（0），本轮不挑战。", keyword)
        _back_to_hub(context)
        return "exhausted"

    if not _create_room(context):
        _back_to_hub(context)
        return "fail"
    if not _start_battle(context):
        _back_to_hub(context)
        return "fail"

    if not _fight(context):
        _back_to_hub(context)
        return "fail"
    if not _settle(context):
        _back_to_hub(context)
        return "fail"
    _back_to_hub(context)
    return "done"


@AgentServer.custom_action("DungeonChallengeChallenge")
class DungeonChallengeChallenge(CustomAction):
    """按任务设置挑战指定副本（多人房间战 · 创建房间流程）。

    一轮流程：
      主页「副本」→ 系列页 → （必要时）「切换系列」→ 列表找副本 →「前往」→ 详情页
      → 难度页签 →「创建房间」→ 助战选择（第一条）→ 编队「确定」→ 房间「开始战斗」
      →「是否开始战斗?」确定 → 战斗 → 结算「点击空白处继续」/「确定」→ 退出房间 → 主页。

    ⚠️ 每次挑战都会真实消耗体力/BP。以下情况**整段跳过、一个按钮都不点**：
      · 「副本挑战次数」= 0（默认）；
      · 「挑战副本」还是「无」（未选择）；
      · 「难度」还是「无」（未选择）；
      · 「挑战方式」选了「加入房间」（该流程尚未实现）；
      · 任何开关/数据节点读不到（接线不闭环时不消费）。

    Examples:
        `custom_action_param`::

            {
                "times": 3
            }
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            params = parse_params(argv.custom_action_param)
        except ValueError as error:
            logger.error("副本挑战：custom_action_param 解析失败：{}", error)
            return CustomAction.RunResult(success=False)

        times = _parse_times(params.get("times", 0), allow_zero=True)
        if times is None:
            logger.error(
                "副本挑战：挑战次数必须是 {}-{} 的整数，收到 {!r}。",
                MIN_TIMES,
                MAX_TIMES,
                params.get("times"),
            )
            return CustomAction.RunResult(success=True)
        if times == 0:
            logger.info("副本挑战：任务设置「次数」为 0，本次不做任何挑战。")
            return CustomAction.RunResult(success=True)

        series = next(iter(_node_expected(context, _TARGET_SERIES_NODE)), "").strip()
        keyword = next(iter(_node_expected(context, _TARGET_DUNGEON_NODE)), "").strip()
        difficulty = _node_expected(context, _TARGET_DIFFICULTY_NODE)
        difficulty_text = next((value.strip() for value in difficulty if value.strip()), "")
        create_room = _flag_enabled(context, _CREATE_ROOM_FLAG)

        if keyword in ("", UNSELECTED):
            logger.info("副本挑战：任务设置「挑战副本」还没选择（默认无），本次不做任何挑战。")
            return CustomAction.RunResult(success=True)
        if series in ("", UNSELECTED):
            logger.error("副本挑战：读不到「挑战副本」所属系列（节点 {}），本次不做任何挑战。", _TARGET_SERIES_NODE)
            return CustomAction.RunResult(success=True)
        if difficulty_text in ("", UNSELECTED):
            logger.info("副本挑战：任务设置「难度」还没选择（默认无），本次不做任何挑战。")
            return CustomAction.RunResult(success=True)
        if not create_room:
            logger.info("副本挑战：任务设置「挑战方式」为「加入房间」，该流程尚未实现，本次不做任何挑战。")
            return CustomAction.RunResult(success=True)

        logger.info(
            "副本挑战开始：副本={}（{}系列），难度={}，方式=创建房间，计划 {} 次。",
            keyword,
            series,
            difficulty_text,
            times,
        )

        done = 0
        for index in range(times):
            if is_stopping(context):
                logger.info("副本挑战：收到停止请求，中断后续挑战（已完成 {}/{} 次）。", done, times)
                break
            logger.info("副本挑战：第 {}/{} 次开始。", index + 1, times)
            outcome = _run_once(context, series=series, keyword=keyword, difficulty=difficulty)
            if outcome == "exhausted":
                break
            if outcome != "done":
                logger.warning("副本挑战：第 {} 次没能完成，停止后续挑战。", index + 1)
                break
            done += 1
            logger.info("副本挑战：第 {}/{} 次完成。", done, times)

        logger.info("副本挑战结束：共完成 {}/{} 次。", done, times)
        return CustomAction.RunResult(success=True)
