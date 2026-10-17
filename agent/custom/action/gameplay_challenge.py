from __future__ import annotations

import re
import time
from dataclasses import dataclass

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction
from utils import logger
from utils.params import parse_params

MIN_TIMES = 1
MAX_TIMES = 99999

# 连续「没有新战斗开始」的容忍次数。
# 一次 GameplayChallenge_Once 跑完却没让挑战进度前进并不一定是致命错误：体力不足转去补充体力、
# 结算自动再战被体力拦住等都会让这一次空转结束，此时应当重试（体力补上后才能真正打出去），
# 而不是直接停掉整个任务。连续这么多次都没有新战斗开始，才判定为真的打不下去。
MAX_STUCK_ATTEMPTS = 3

# 一次完整挑战的子任务入口（见 resource/base/pipeline/GameplayChallenge/GameplayChallenge.json）。
ONCE_TASK_ENTRY = "GameplayChallenge_Once"
BATTLE_ENTER_NODE = "GameplayChallenge_BattleEnter"
# 「战斗画面已在场」的锚点节点（纯识别节点，不被任何 next 引用，见 GameplayChallenge.json）。
# 登记一场战斗前有界轮询它：结算过渡屏「点击空白处继续」也会走到 BattleEnter，
# 那一屏没有战斗 HUD，不校验就会被白记成一场战斗（实测"计划 3 次却登记到 4/3"）。
BATTLE_PRESENCE_NODE = "GameplayChallenge_BattlePresence"
# 等战斗加载出来的默认上限。实测点「开始战斗」后战斗画面约 2.5s 出现，5s 足够；
# ⚠️ 不要再放大：这段时间是**阻塞**的，如果当前其实是结算页而不是战斗页，
#    等满就会把游戏「2S后自动再次挑战」的窗口整个让出去（实机：结算页按钮一次都没点到，白打 3 局）。
_BATTLE_PRESENCE_TIMEOUT = 5.0
_BATTLE_PRESENCE_POLL = 0.8
# 过渡屏之后（ResultTeamContinue「点击空白处继续」）用的"只看一眼、不等"预算：
# 那一屏后面可能是结算页，必须**尽快**把控制权还给 BattleLoop 去点「挑战下一关」/「确认」。
_BATTLE_PRESENCE_QUICK = 0.3
# 结算页「挑战下一关」快速再战节点：由 BattleModuleRecord 按剩余挑战次数动态启停。
NEXT_STAGE_NODE = "GameplayChallenge_ResultNextStage"

# 每场战斗开始时需要复位命中计数的节点。
# MaaFramework 的 max_hit 计数跨 run_task 累加：不复位的话第二轮起 SettleWait / BattleTurn
# 会被直接跳过，监督节点 next 全部落空 → 「bad next」把整条链路判死。
BATTLE_SCOPED_NODES = (
    "GameplayChallenge_BattleTurn",
    NEXT_STAGE_NODE,
    "GameplayChallenge_ResultTeamContinue",
    "GameplayChallenge_ResultConfirm",
    "GameplayChallenge_FriendCancel",
    "GameplayChallenge_SettleWait",
)


@dataclass
class _BattleRecord:
    """一次任务运行内的战斗进度。

    MaaFramework 每个 Tasker 串行执行节点，因此模块级单例即可承载跨节点的
    战斗次数状态。`planned` 是本次任务计划挑战的次数（「挑战次数」选项），
    `entered` 是已经真正开始过的战斗次数（BattleModuleRecord 每进一次战斗 +1）。
    结算页用「挑战下一关」快速再战时，同一次 run_task 里会连打多局，
    所以循环控制器用 `entered` 而不是 run_task 的次数来判断进度。
    """

    planned: int = 0
    entered: int = 0


_record = _BattleRecord()


def _parse_times(raw: object) -> int | None:
    """把「挑战次数」解析为 1-99999 的整数；非数字、越界或缺失时返回 None。"""
    if isinstance(raw, bool):
        return None
    times: int
    if isinstance(raw, int):
        times = raw
    elif isinstance(raw, float) and raw.is_integer():
        times = int(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text.lstrip("+").isdigit():
            return None
        times = int(text)
    else:
        return None
    if times < MIN_TIMES or times > MAX_TIMES:
        return None
    return times


@AgentServer.custom_action("GameplayChallengeLoop")
class GameplayChallengeLoop(CustomAction):
    """按「挑战次数」打到目标次数。

    每次循环体（GameplayChallenge_Once）内部可能连打多局：副本结算页支持
    「挑战下一关」时会直接再战，跳过副本详情页 / 难度 / 助战 / 编队。
    因此进度以「已开始的战斗次数」为准，打满 times 或连续多次没有新战斗开始才收尾。

    Examples:
        `custom_action_param`::

            {
                "times": 2
            }
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            params = parse_params(argv.custom_action_param)
        except ValueError as error:
            logger.error("GameplayChallengeLoop: {}", error)
            return CustomAction.RunResult(success=False)

        raw_times = params.get("times")
        times = _parse_times(raw_times)
        if times is None:
            logger.error(
                "GameplayChallengeLoop: 挑战次数必须是 {}-{} 的整数，收到 {!r}。",
                MIN_TIMES,
                MAX_TIMES,
                raw_times,
            )
            return CustomAction.RunResult(success=False)

        _record.planned = times
        _record.entered = 0
        logger.info("玩法挑战开始：计划挑战 {} 次。", times)

        stuck = 0
        while _record.entered < times and stuck < MAX_STUCK_ATTEMPTS:
            index = _record.entered + 1
            logger.info(
                "第 {}/{} 次挑战开始{}。",
                index,
                times,
                "" if stuck == 0 else f"（第 {stuck + 1} 次尝试）",
            )

            before = _record.entered
            # 结算页「挑战下一关」只在后面还有次数时启用：打满次数就关掉它，
            # 让结算页走「确认」退回副本详情页收尾，不会多打一局、多扣一次体力。
            override = {
                BATTLE_ENTER_NODE: {
                    "action": {
                        "param": {
                            "custom_action_param": {
                                "times": times,
                            }
                        }
                    }
                },
                NEXT_STAGE_NODE: {
                    "enabled": index < times,
                },
            }
            context.run_task(ONCE_TASK_ENTRY, override)

            if _record.entered == before:
                stuck += 1
                logger.warning(
                    "第 {}/{} 次挑战第 {} 次尝试没有开始新的战斗（链路中断 / 体力或界面异常），重试。",
                    index,
                    times,
                    stuck,
                )

        if _record.entered < times:
            logger.error(
                "连续 {} 次尝试都没有开始新的战斗，只完成 {}/{} 次挑战（体力或界面异常），停止后续挑战。",
                MAX_STUCK_ATTEMPTS,
                _record.entered,
                times,
            )
            return CustomAction.RunResult(success=False)

        logger.info("玩法挑战结束：共完成 {} 次挑战。", _record.entered)
        return CustomAction.RunResult(success=True)


@AgentServer.custom_action("BattleModuleRecord")
class BattleModuleRecord(CustomAction):
    """每场战斗开始时的登记节点。

    三条路径都会走到它：编队页「开始战斗」之后、结算页「挑战下一关」快速再战之后，
    以及结算过渡屏「点击空白处继续」之后（后者未必真的开了新战斗）。

    职责：

    1. 有界等待「战斗画面已在场」的锚点（`GameplayChallenge_BattlePresence`），
       确认在打才累计已开始的战斗次数，供 GameplayChallengeLoop 判断「挑战次数」是否打满；
       等不到就只跳过登记，不返回失败（失败会弄断 BattleLoop 的 next 链）；
    2. 复位战斗内带 max_hit 的节点命中计数（max_hit 计数跨 run_task 累加，不复位会判死链路）；
    3. 按剩余次数启停结算页「挑战下一关」：还有次数就快速再战，打满就让结算页走「确认」收尾。

    Examples:
        `custom_action_param`::

            {
                "times": 2,
                "wait": 5.0
            }

    `wait` 可选，默认 `_BATTLE_PRESENCE_TIMEOUT`（秒）：等战斗画面出现的上限。
    ⚠️ 它是**阻塞**时间，别乱放大：如果本节点是被结算过渡屏走到的，等满就等于把
    游戏「2S后自动再次挑战」的窗口让出去（实机后果：结算页「挑战下一关」「确认」一次都没点到，
    还白打了几局）。过渡屏路径请用 `GameplayChallenge_BattleEnterQuick`（wait=0.3）。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            params = parse_params(argv.custom_action_param)
        except ValueError as error:
            logger.error("BattleModuleRecord: {}", error)
            return CustomAction.RunResult(success=False)

        times = _parse_times(params.get("times"))
        if times is not None:
            # 以循环控制器注入的次数为准，但不允许小于已经打过的场次。
            _record.planned = max(times, _record.entered)

        if _record.planned <= 0:
            logger.error("BattleModuleRecord: 缺少有效的挑战次数，无法登记本场战斗。")
            return CustomAction.RunResult(success=False)

        # 先确认这一刻真的在打：结算过渡屏「点击空白处继续」也会走到本节点。
        # 等不到就**不登记**，但仍然返回成功（返回失败会弄断 BattleLoop 的 next 链）。
        # 等待预算可由节点参数 wait 覆盖：
        #   GameplayChallenge_BattleEnter      默认 5s（点「开始战斗」/「挑战下一关」后有加载过场）
        #   GameplayChallenge_BattleEnterQuick 0.3s（过渡屏之后只看一眼）
        wait = _parse_wait(params.get("wait"))
        if not _wait_for_battle_presence(context, wait):
            logger.warning(
                "BattleModuleRecord: {:.1f} 秒内没有出现战斗画面，本次不登记（多半是结算过渡屏），"
                "交给 BattleLoop 继续处理。",
                wait,
            )
            return CustomAction.RunResult(success=True)

        _record.entered += 1
        remaining = _record.planned - _record.entered

        for node_name in BATTLE_SCOPED_NODES:
            context.clear_hit_count(node_name)
        context.override_pipeline({NEXT_STAGE_NODE: {"enabled": remaining > 0}})

        logger.info("第 {}/{} 次挑战已进入战斗。", _record.entered, _record.planned)
        return CustomAction.RunResult(success=True)


@AgentServer.custom_action("BattleTurnClick")
class BattleTurnClick(CustomAction):
    """战斗回合按钮：点击识别到的「跳过回合」按钮。

    刻意不打印任何日志——回合按钮每回合都要点一次，逐回合输出会刷屏；
    战斗相关的可见输出只有两处：BattleModuleRecord 的「第几次已进入战斗」，
    以及 GameplayChallengeLoop 的「计划/已完成次数」。

    Examples:
        `custom_action_param`::

            {}   # 本动作不需要参数
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        box = argv.box
        if box.w <= 0 or box.h <= 0:
            logger.error("BattleTurnClick: 识别框无效 {}，跳过本次点击。", box)
            return CustomAction.RunResult(success=False)

        context.tasker.controller.post_click(box.x + box.w // 2, box.y + box.h // 2).wait()
        return CustomAction.RunResult(success=True)


# ---------------------------------------------------------------------------
# 助战选择自定义逻辑（PickSupport）：
#   在「助战选择」页挑一条能真正打开「编队」页的助战 → 编队页检测队伍属性 →
#   校验助战效果类型(神/魔/属攻)是否符合全局配置 → 不符则点「助战」回助战选择页换一条
#   → 循环至多 3 轮 → 找不到符合的魔物则回到编队页按当前助战继续（不卡流程）。
#
#   两条硬约束（都由实机观测得出，证据见 _test_out/explore.log 与 test-report）：
#   1. OCR 的 all_results 不保证按 y 排序，必须先按 y 升序再取条目：
#      实测 entries[0] 会取到列表最下面的 Crow19(y=1029)，点击 (200,1059) 落在列表外，
#      条目没被选中、列表也没滚动，页面纹丝不动 —— 整次挑战由此空转。
#   2. 点完条目必须做「是否已进入编队页」的后置校验：
#      助战选择页与编队页显示同一段 buff 文案（实测两页都是
#      「武器技能「神火」的效果上升150%」，且都落在 _FORMATION_BUFF_ROI 内），
#      不校验页面归属就会把「还停在助战选择页」误判成「已选到符合的助战」并假成功。
# ---------------------------------------------------------------------------
_ELEMENTS = "火水风土光暗"
_ELEMENT_KEY = {
    "火": "team_fire",
    "水": "team_water",
    "风": "team_wind",
    "土": "team_earth",
    "光": "team_light",
    "暗": "team_dark",
}
# 属性助战期望的数据节点（A1 接线）：六个属性各一个纯数据节点，
# 由 interface.json 的「X属性编队助战」选项 override 其 recognition.param.expected。
#
# ⚠️ 不能让六个属性共写同一个节点的同一个字段：PI 客户端把每个选中 case 拆成**一条独立的**
# pipeline_override 下发，而 MaaFramework 对同一字段是**整体替换**而非深合并 ——
# 实测六条并列 override 写同一节点时只剩最后一条生效（global_option 里排最后的
# 「暗属性编队助战」一直覆盖其余五个，火属性助战因此永远读到 None，日志表现为「期望=None」）。
_SUPPORT_PREF_NODES = {
    "火": "GameplayChallenge_SupportPref_Fire",
    "水": "GameplayChallenge_SupportPref_Water",
    "风": "GameplayChallenge_SupportPref_Wind",
    "土": "GameplayChallenge_SupportPref_Earth",
    "光": "GameplayChallenge_SupportPref_Light",
    "暗": "GameplayChallenge_SupportPref_Dark",
}
# 助战列表区域（玩家名 + 魔物名 + buff 都在这一带）
_ASSIST_ROI = [0, 360, 720, 820]
# 「助战选择」弹窗标题（实测 box (288,219,142,43) score≈0.999；编队页没有这个标题）
_ASSIST_TITLE_ROI = [260, 200, 200, 70]
# 编队页底部「开始战斗」（实测 box (295,1095,125,44) score≈1.000；助战选择页没有这个按钮）
_FORMATION_START_ROI = [250, 1060, 220, 100]
# 编队页「助战」按钮文字（实测 box (613,776,58,34)，点击取识别框中心 → (642,793)）
_FORMATION_SUPPORT_BTN_ROI = [560, 740, 160, 90]
# 编队页已选助战所在的助战面板（含其 buff 文本，实测 buff box (208,885,316,23)）
_FORMATION_BUFF_ROI = [0, 740, 720, 260]

# ---- 队伍属性识别（元素徽章）-------------------------------------------------
# 编队页「主要成员」每一格头像左上角都有一枚**元素徽章**，同一支队伍里各格同色。
# 用户提供的六份已知属性截图（火/水/风/土/光/暗）逐格分类 6/6 一致，空槽会被跳过。
# 取样框 28x28，y=470，x 按 UI 栅格 54/160/266/372（用户已在 火/土 两图上圈出该位置）。
_ELEMENT_BADGE_X = (54, 160, 266, 372)
_ELEMENT_BADGE_Y = 470
_ELEMENT_BADGE_SIZE = 28
# 六种属性的徽章参考色（HSV）。必须用「色相 + 饱和度 + 亮度」联合判据：
# 土(39.0°) 与 光(43.6°) 色相只差 4.8°，单看色相必混；但亮度差 0.096、饱和度差 0.035，
# 按下面的权重加权后两者距离 0.41~0.57，而正确项距离 0.00~0.24，分得开。
_ELEMENT_BADGE_HSV = {
    "火": (13.6, 0.464, 0.693),
    "水": (175.6, 0.189, 0.650),
    "风": (133.3, 0.301, 0.724),
    "土": (39.0, 0.364, 0.691),
    "光": (43.6, 0.399, 0.787),
    "暗": (292.7, 0.193, 0.571),
}
_BADGE_HUE_WEIGHT = 1.0 / 90.0
_BADGE_SAT_WEIGHT = 2.0
_BADGE_VAL_WEIGHT = 3.0
# 最近的参考色距离超过这个值就当作"没认出来"：宁可不判、由调用方退回旧逻辑，也不要乱判。
_BADGE_MAX_DISTANCE = 0.90

# 「助战选择」页顶部的元素页签：**主路径是按颜色识别**（见 `_detect_element_tabs`），
# 下面这张表只是"颜色识别失败时的兜底映射"。
# 来源：06:00 那轮运行里，逐个点页签后看列表里出现了哪个属性的条目 ——
#   131→火（弹窗默认选中项）  245→风  359→土  472→水  585→光
# ⚠️ 元素页签行是**可横向滚动**的：一屏只放得下 5 个，第 6 个「暗」必须把页签行横向滑出来
#   才点得到（用户实测确认），所以表里没有 暗 —— 暗 靠"滑出来后按颜色认"。
_ELEMENT_TAB_Y = 328
# ---- 元素页签的「按颜色识别」（主路径，替代写死坐标）-------------------------------
# 页签本身就是元素图标，和徽章一样**按颜色**就能认出来（实测用同一套 _ELEMENT_BADGE_HSV
# 参考色 + _rank_elements，5 个可见页签 5/5 全中：131→火 245→风 358→土 472→水 585→光）。
# 因此正常路径是"先认出队伍属性对应的页签 x，直接点它"，而不是写死坐标或逐个位置试。
#
# ⚠️ 取色必须只取**图标核心窄窗**（±12px）：选中的页签是一整块 113px 宽的高亮背景，
#    按整段取样会取到背景色 —— 实测整段取样把火页签读成 [54,67,89] 的暗蓝、判成了水。
_TAB_ROW_Y0 = 300
_TAB_ROW_Y1 = 356
_TAB_ROW_X0 = 0
_TAB_ROW_X1 = 720
_TAB_SAT_THRESHOLD = 45  # 单像素"有颜色"的饱和度门槛（与徽章取样同量级）
_TAB_SAT_MIN_ROWS = 6  # 一列至少这么多行有颜色才算页签图标的一部分
_TAB_ICON_MIN_WIDTH = 14
_TAB_ICON_MAX_WIDTH = 130  # 选中态页签含高亮背景，实测可宽到 113px
_TAB_ICON_HALF = 12  # 取色窄窗半径
# 选中态页签带一整块高亮背景（实测宽 113px），未选中的只有 23~27px。
# ⚠️ 选中态那枚图标的颜色不可靠（被高亮压暗：实测火页签图标变成 [121,72,82]、V 仅 0.475，
#    到火参考色的距离 1.05 超过置信门槛；而把窄窗挪到旁边反而会被判成暗），
#    所以选中页签**不靠颜色认属性**，改用"宽度识别 + 列表内容确认"，见 _locate_element_tab。
_TAB_SELECTED_MIN_WIDTH = 60
# 页签图标的参考色（HSV）—— **与徽章是两套画法，不能共用徽章那张表**：
# 同一个属性，页签图标比徽章更亮更饱和、色相也有偏移，用徽章表会出现边界误判 ——
# 实测暗页签图标 [119,94,164] 到徽章暗参考色距离 1.03（超 0.90 门槛 → 认不出），
# 水 0.87 也贴在门槛上。
# 下表六点**全部由真色截图实测**（三张 MuMu 助战选择页 + 框架 PickSupport 截图，互相印证；
# 其中 风/土/水/光 在两张不同样本里取值完全一致，说明这套参考色是稳定的）。
# ⚠️ 火 必须用实测值：沿用它徽章的 (13.6,.464,.693) 时，实测火图标 (2.0,.525,.617) 的距离是
#    0.48 —— 离下面的 0.50 门槛只差 0.02，任何渲染偏移都会让火认不出来。
_TAB_ICON_HSV = {
    "火": (2.0, 0.525, 0.617),
    "水": (197.2, 0.421, 0.706),
    "风": (155.6, 0.486, 0.713),
    "土": (37.3, 0.344, 0.642),
    "光": (44.8, 0.395, 0.829),
    "暗": (261.5, 0.426, 0.643),
}
# 实测这 5 个属性到各自参考色的距离都 ≤0.15，而参考色两两最小距离约 0.61（风↔水），
# 所以 0.5 的置信门槛既宽松又不可能串味。
_TAB_MAX_DISTANCE = 0.5
_ELEMENT_TAB_X = {"火": 131, "风": 245, "土": 359, "水": 472, "光": 585}
_ELEMENT_TAB_FALLBACK_X = (131, 245, 359, 472, 585)
# 横向滑动元素页签行：手指从右往左推（把行内容左移，露出右边被挡住的页签）。
_ELEMENT_TAB_SWIPE_FROM_X = 600
_ELEMENT_TAB_SWIPE_TO_X = 150
_ELEMENT_TAB_SWIPE_TIMES = 2
_ELEMENT_TAB_SWIPE_SETTLE = 0.6
_TAB_SETTLE = 1.2
# 切页签后把列表滚回顶部：实测切页签**不会**重置滚动位置，不回到顶部就会漏掉最上面几条
# （用户复现："由上往下滑动 3 次"之后才看到符合的助战）。
_SCROLL_TOP_TIMES = 3
_SCROLL_TOP_SETTLE = 0.4

# 条目 Rank 行中心 → 条目热区的纵向偏移（实测按此点最上面一条可稳定进入编队页）
_ENTRY_OFFSET_Y = 30
# 点击后留给页面切换的时间
_PAGE_SETTLE = 3.0
# 助战选择页上最多试几条才判定「打不开编队页」
_MAX_ENTRY_ATTEMPTS = 3
# 助战效果类型不符时最多重选几轮
_MAX_SUPPORT_ATTEMPTS = 3


def _ocr(context: Context, roi=None):
    pipeline = {
        "PickSupport_OCR": {
            "recognition": {"type": "OCR", "param": {}},
            "action": {"type": "DoNothing"},
        }
    }
    if roi is not None:
        pipeline["PickSupport_OCR"]["recognition"]["param"]["roi"] = list(roi)
    img = context.tasker.controller.post_screencap().wait().get(True)
    detail = context.run_recognition("PickSupport_OCR", img, pipeline)
    return [(r.text.strip(), r.box) for r in detail.all_results]


def _parse_wait(raw) -> float:
    """把节点参数里的 `wait`（秒）解析成 [0, 30] 的浮点；缺省/非法时返回默认预算。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _BATTLE_PRESENCE_TIMEOUT
    return min(30.0, max(0.0, value))


def _wait_for_battle_presence(context: Context, timeout: float = _BATTLE_PRESENCE_TIMEOUT) -> bool:
    """在有界时间内等「战斗画面已在场」（左上「战斗剩余时间 39:5x」/「第N回合」）。

    为什么必须等：`GameplayChallenge_BattleEnter` 会被结算过渡屏「点击空白处继续」
    那一步直接走到，而那一屏还没有战斗 HUD；无条件登记就会给"已完成挑战次数"白送 +1
    （实测日志里计划 3 次却登记到第 4/3 次）。游戏自己的 2S 自动再战也在这个窗口内开出战斗，
    所以等一等能同时正确处理"过渡屏之后真的又开了一局"的情况。

    ⚠️ 为什么不能反过来用 pipeline 的识别门控去卡 BattleEnter：它挂在
    `ResultTeamContinue.next` 上，**next 链失败会把整条 BattleLoop 监督链判死** ——
    实测门控一次没中（84ms）就让整轮 run_task 立刻结束，结算页再无人处理、任务失败。
    所以这里只决定"这次要不要登记"，绝不返回失败、绝不卡流程。
    """
    deadline = time.time() + timeout
    while True:
        try:
            img = context.tasker.controller.post_screencap().wait().get(True)
            detail = context.run_recognition(BATTLE_PRESENCE_NODE, img)
        except Exception as error:  # noqa: BLE001  识别异常按"没在场"处理，不能因此中断链路
            logger.warning("BattleModuleRecord: 战斗画面锚点识别异常：{}", error)
            return False
        if detail is not None and detail.hit:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(_BATTLE_PRESENCE_POLL)


def _hit(context: Context, roi, expected: str):
    """在指定 ROI 内用 OCR 找 expected 子串，返回命中的 (text, box) 列表；未命中返回空表。

    只用于页面归属判定（编队页 / 助战选择页），不做任何兜底点击：
    认不到就当作「不在该页」，由调用方决定重试还是放弃。
    """
    return [(t, b) for t, b in _ocr(context, roi) if expected in t]


def _parse_buff(text: str):
    """从助战 buff 文本解析出 (属性, 类型)。类型：神 / 魔 / 属攻 / None。"""
    m = re.search(r"[神魔]([" + _ELEMENTS + r"])", text)
    if m:
        kind = "神" if text[m.start()] == "神" else "魔"
        return m.group(1), kind
    element = None
    m = re.search(r"([" + _ELEMENTS + r"])属性", text)
    if m:
        element = m.group(1)
    typ = None
    if "攻击力上升" in text or "属攻" in text or "属性攻击力" in text:
        typ = "属攻"
    if element is None:
        for e in _ELEMENTS:
            if e in text:
                element = e
                break
    return element, typ


def _read_support_prefs(context: Context) -> dict[str, str]:
    """读六个属性的助战期望类型，返回 {属性: 神/魔/属攻}。

    取值来自 `_SUPPORT_PREF_NODES` 六个数据节点的 `recognition.param.expected[0]`
    （interface.json 的 select 选项按 PI 规范写这个字段）。
    节点缺失、读不到或取值为空时该属性不下发期望，调用方按「不校验」处理。
    """
    prefs: dict[str, str] = {}
    for element, node_name in _SUPPORT_PREF_NODES.items():
        try:
            node = context.get_node_data(node_name) or {}
        except Exception as error:  # noqa: BLE001  读不到就按"未配置"处理，不能因此中断助战链路
            logger.warning("PickSupport: 读取助战偏好节点 {} 失败：{}", node_name, error)
            continue
        expected = ((node.get("recognition") or {}).get("param") or {}).get("expected") or []
        first = expected[0] if expected else None
        if isinstance(first, str) and first.strip():
            prefs[element] = first.strip()
    return prefs


def _bgr_to_hsv(bgr) -> tuple[float, float, float]:
    """BGR(0-255) -> (H 0-360, S 0-1, V 0-1)。"""
    b, g, r = (float(v) / 255.0 for v in bgr)
    high, low = max(r, g, b), min(r, g, b)
    value, delta = high, high - low
    sat = 0.0 if high == 0 else delta / high
    if delta == 0:
        hue = 0.0
    elif high == r:
        hue = ((g - b) / delta) % 6
    elif high == g:
        hue = (b - r) / delta + 2
    else:
        hue = (r - g) / delta + 4
    return hue * 60.0, sat, value


def _hue_distance(a: float, b: float) -> float:
    """两个色相值的环形距离（0~180）。"""
    diff = abs(a - b) % 360.0
    return min(diff, 360.0 - diff)


def _rank_elements(hsv) -> list[tuple[float, str]]:
    """按「色相 + 饱和度 + 亮度」加权距离给六种属性排序，最近的在前。

    详见 _ELEMENT_BADGE_HSV 的注释：土/光 只能靠亮度区分，所以三项都要参与。
    """
    hue, sat, value = hsv
    return sorted(
        (
            _hue_distance(hue, ref_h) * _BADGE_HUE_WEIGHT
            + abs(sat - ref_s) * _BADGE_SAT_WEIGHT
            + abs(value - ref_v) * _BADGE_VAL_WEIGHT,
            element,
        )
        for element, (ref_h, ref_s, ref_v) in _ELEMENT_BADGE_HSV.items()
    )


def _sample_badge_color(img, cx: int):
    """取一格的元素徽章代表色 (B, G, R)；该格为空槽（几乎全是灰度）时返回 None。

    刻意不依赖 numpy：agent 只声明了 loguru/maafw，虽然截图本身就是 numpy 数组，
    这里只用切片 + 纯 Python 统计（28x28=784 像素，开销可忽略）。
    """
    half = _ELEMENT_BADGE_SIZE // 2
    x0 = max(0, cx - half)
    rows = img[_ELEMENT_BADGE_Y : _ELEMENT_BADGE_Y + _ELEMENT_BADGE_SIZE, x0 : x0 + _ELEMENT_BADGE_SIZE]
    pixels: list[tuple[float, float, float, float]] = []
    for row in rows.tolist():
        for pixel in row:
            b, g, r = float(pixel[0]), float(pixel[1]), float(pixel[2])
            pixels.append((max(r, g, b) - min(r, g, b), b, g, r))
    if not pixels:
        return None
    pixels.sort(key=lambda item: item[0])
    keep = pixels[int(len(pixels) * 0.6) :]  # 最饱和的 40%
    if len(keep) < 30 or keep[-1][0] < 20.0:
        return None  # 空槽（头像没填）或整片接近灰度
    count = len(keep)
    return (
        sum(p[1] for p in keep) / count,
        sum(p[2] for p in keep) / count,
        sum(p[3] for p in keep) / count,
    )


def _classify_team_element(img) -> str | None:
    """在编队页截图上按「主要成员 4 格元素徽章」多数表决判定队伍属性；认不出返回 None。

    纯函数（只吃图像数组），便于用固定截图离线回归。
    """
    votes: dict[str, int] = {}
    for cx in _ELEMENT_BADGE_X:
        color = _sample_badge_color(img, cx)
        if color is None:
            continue
        distance, element = _rank_elements(_bgr_to_hsv(color))[0]
        if distance <= _BADGE_MAX_DISTANCE:
            votes[element] = votes.get(element, 0) + 1
    if not votes:
        return None
    return max(votes, key=lambda key: votes[key])


def _detect_team_element(context: Context) -> str | None:
    """截图后判定当前队伍属性；截图或识别失败返回 None（调用方退回旧逻辑）。"""
    try:
        img = context.tasker.controller.post_screencap().wait().get(True)
    except Exception as error:  # noqa: BLE001  识别失败不能让整条助战链路崩掉
        logger.warning("PickSupport: 截图失败，无法识别队伍属性：{}", error)
        return None
    return _classify_team_element(img)


def _screencap(context: Context):
    """截一帧（numpy BGR 数组）；失败返回 None。"""
    try:
        return context.tasker.controller.post_screencap().wait().get(True)
    except Exception as error:  # noqa: BLE001  截图失败不该让整条助战链路崩掉
        logger.warning("PickSupport: 截图失败：{}", error)
        return None


def _sample_icon_color(img, cx: int):
    """取页签**图标核心**（±_TAB_ICON_HALF）内最饱和 30% 像素的平均色 (B,G,R)；取不到返回 None。"""
    rows = img[_TAB_ROW_Y0:_TAB_ROW_Y1, max(0, cx - _TAB_ICON_HALF) : cx + _TAB_ICON_HALF + 1]
    pixels: list[tuple[float, float, float, float]] = []
    for row in rows.tolist():
        for pixel in row:
            b, g, r = float(pixel[0]), float(pixel[1]), float(pixel[2])
            pixels.append((max(r, g, b) - min(r, g, b), b, g, r))
    if not pixels:
        return None
    pixels.sort(key=lambda item: item[0])
    keep = pixels[int(len(pixels) * 0.7) :]
    if len(keep) < 20 or keep[-1][0] < _TAB_SAT_THRESHOLD:
        return None
    count = len(keep)
    return (
        sum(p[1] for p in keep) / count,
        sum(p[2] for p in keep) / count,
        sum(p[3] for p in keep) / count,
    )


def _rank_tab_elements(hsv) -> list[tuple[float, str]]:
    """按「色相 + 饱和度 + 亮度」加权距离给六种属性排序（页签图标专用参考色表）。"""
    hue, sat, value = hsv
    return sorted(
        (
            _hue_distance(hue, ref_h) * _BADGE_HUE_WEIGHT
            + abs(sat - ref_s) * _BADGE_SAT_WEIGHT
            + abs(value - ref_v) * _BADGE_VAL_WEIGHT,
            element,
        )
        for element, (ref_h, ref_s, ref_v) in _TAB_ICON_HSV.items()
    )


def _detect_element_tabs(img) -> list[tuple[str | None, int, int]]:
    """认出「助战选择」页页签行里每个可见页签，按 x 升序返回 (属性|None, 横向中心, 宽度)。

    方法与认元素徽章同款（按饱和度切图标 + 取图标核心最饱和像素群平均色 + 加权比色），
    但**参考色用页签自己那张表** `_TAB_ICON_HSV`（页签图标与徽章是两套画法，见那里的注释）。
    实测 4 个未选中页签（土/水/光/暗）到各自参考色距离 ≤0.15，全部认对。
    属性为 None 表示"这段是页签但颜色认不出属性"，两种情况：
      · 它是**选中态的宽块**（一律不判色，见下面注释）；
      · 该图标的饱和度太低（实测有近乎灰白的页签，连分段阈值都过不了）。
    """
    rows = img[_TAB_ROW_Y0:_TAB_ROW_Y1, _TAB_ROW_X0:_TAB_ROW_X1].tolist()
    if not rows:
        return []
    width = len(rows[0])
    colored_rows = [0] * width
    for row in rows:
        for index, pixel in enumerate(row):
            if (max(pixel) - min(pixel)) > _TAB_SAT_THRESHOLD:
                colored_rows[index] += 1

    tabs: list[tuple[str | None, int, int]] = []
    start = None
    for index in range(width + 1):
        colored = index < width and colored_rows[index] >= _TAB_SAT_MIN_ROWS
        if colored and start is None:
            start = index
        elif not colored and start is not None:
            run = index - start
            if _TAB_ICON_MIN_WIDTH <= run <= _TAB_ICON_MAX_WIDTH:
                center = _TAB_ROW_X0 + (start + index - 1) // 2
                element = None
                # 选中态那个宽块**一律不做颜色判定**：它的图标被高亮背景压暗，
                # 实测会被"自信地"判成别的属性（选中态的光图标 → 判成土，距离 0.69 还在门槛内）。
                if run < _TAB_SELECTED_MIN_WIDTH:
                    color = _sample_icon_color(img, center)
                    if color is not None:
                        distance, found = _rank_tab_elements(_bgr_to_hsv(color))[0]
                        if distance <= _TAB_MAX_DISTANCE:
                            element = found
                tabs.append((element, center, run))
            start = None
    return tabs


def _tab_x_for_element(img, element: str):
    """在页签行里找 element 对应页签的横向中心（只认颜色可靠的页签）；找不到返回 None。"""
    for found, center, _run in _detect_element_tabs(img):
        if found == element:
            return center
    return None


def _selected_tab_x(img):
    """返回"选中态页签"（最宽的那段，带高亮背景）的横向中心；找不到返回 None。"""
    best = None
    for _element, center, run in _detect_element_tabs(img):
        if run >= _TAB_SELECTED_MIN_WIDTH and (best is None or run > best[1]):
            best = (center, run)
    return best[0] if best is not None else None


def _box_center_y(box) -> int:
    """取 OCR box 的纵向中心（整数，供 post_click 使用）。

    不同 binding 版本里 `all_results[].box` 可能是 Rect 对象，也可能是
    [x, y, w, h] 列表；统一处理，避免 AttributeError 让整个助战动作崩掉。
    返回整数：post_click 不接受 float 坐标。
    """
    if isinstance(box, (list, tuple)):
        return int(float(box[1]) + float(box[3]) / 2)
    return int(float(box.y) + float(box.h) / 2)


def _box_center_x(box) -> int:
    """取 OCR box 的横向中心（整数，供 post_click 使用）。与 _box_center_y 同款兼容处理。"""
    if isinstance(box, (list, tuple)):
        return int(float(box[0]) + float(box[2]) / 2)
    return int(float(box.x) + float(box.w) / 2)


def _parse_entries(boxes):
    """把 OCR 结果按「玩家名 Rank」行聚合成助战条目（**按 y 升序**），提取每条的属性/类型。

    一条助战的 buff 行必须取「最贴近、且像 buff 的那一行」，不能取 OCR 顺序里的
    第一行：下一条助战的玩家名/魔物名也落在下方范围内，且在 OCR 结果里往往排在
    真正的 buff 行之前，直接取第一行会张冠李戴（属性/类型全错）。

    ⚠️ 返回前必须按 y 升序排序：OCR `all_results` 的顺序**不保证**自上而下
    （实测同一张助战选择页截图里顺序是 y=1236 → 810 → 608 → 404 → 1019），
    不排序时 `entries[0]` 会取到列表最下面的条目，点击点落到列表外的空白区，
    条目不会被选中，页面也就不会切走 —— 这是「整次挑战空转」的直接原因。
    """
    buff_keys = ("属性", "攻击力上升", "效果上升", "上升")
    ranks = sorted(((t, b) for t, b in boxes if "Rank" in t), key=lambda tb: _box_center_y(tb[1]))
    entries = []
    for _t, b in ranks:
        y = _box_center_y(b)
        best_key = None
        buff = None
        for t2, b2 in boxes:
            if "Rank" in t2:
                continue  # 跳过其它条目的标题行
            d = _box_center_y(b2) - y
            if not (20 <= d <= 180):
                continue
            # 优先取像 buff 的行，其次取最近的行
            key = (0 if any(k in t2 for k in buff_keys) else 1, abs(d))
            if best_key is None or key < best_key:
                best_key, buff = key, t2
        element, typ = _parse_buff(buff) if buff else (None, None)
        entries.append({"y": y, "element": element, "type": typ, "buff": buff, "box": b})
    return entries


def _entry_click_point(entry) -> tuple[int, int]:
    """条目的点击点：横向取 Rank 行中心，纵向下移 _ENTRY_OFFSET_Y 落到条目热区内。

    实测最上面一条 Rank 行 box (113,406,202,27) → 点击 (214,449) 可稳定进入编队页。
    """
    return _box_center_x(entry["box"]), int(entry["y"]) + _ENTRY_OFFSET_Y


@AgentServer.custom_action("PickSupport")
class PickSupport(CustomAction):
    """助战选择自定义动作。

    流程：
      1. 确保站在编队页：已在编队页则 no-op；否则在「助战选择」页按 y 升序挑条目，
         点完**必须**校验是否真的进入了编队页，没进入就换下一条（至多 3 条），
         全失败即返回 success=False（不再有「认不到就盲点默认位置」的兜底）；
      2. 编队页读取已选助战的 buff，得到「队伍属性」与「当前助战效果类型」；
      3. 按 队伍属性 查全局配置（神/魔/属攻），与当前助战类型比对；
      4. 不符则点「助战」回到助战选择页换成符合的魔物，循环至多 3 轮；
      5. 始终找不到符合的魔物则重新打开编队页，按当前助战继续挑战（不卡流程）。

    ⚠️ 只有真实到达编队页（认出「开始战斗」）才可能返回 success；
    停在助战选择页时 _detect_selected 直接返回 None，不会再出现「伪成功」。

    Examples:
        `custom_action_param`::

            {
                "team_water": "魔",
                "team_fire": "神",
                "team_wind": "魔"
            }

        ⚠️ 上面这种把六项写进 `custom_action_param` 的方式**只在单条 override 下有效**，
        而 PI 客户端会为每个选中 case 各下发一条 override，MaaFramework 对同一字段是整体替换，
        六个属性共写同一节点时只有最后一条生效。因此正式接线已改为「一个属性一个数据节点」
        （见 `_SUPPORT_PREF_NODES`）；`custom_action_param` 仅作为旧接线/手工传参的兜底。
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        # 期望值优先读六个偏好数据节点（正式接线），再退回 custom_action_param（兜底）。
        prefs = _read_support_prefs(context)
        try:
            legacy = parse_params(argv.custom_action_param)
        except ValueError as error:
            logger.warning("PickSupport: custom_action_param 解析失败（忽略）：{}", error)
            legacy = {}
        for element, key in _ELEMENT_KEY.items():
            if element not in prefs and legacy.get(key):
                prefs[element] = str(legacy[key])
        logger.debug("PickSupport: 助战期望 = {}", prefs)

        # 第 1 步：确保真的站在编队页上（点完条目必须有后置校验，绝不假成功）
        if not self._open_formation_page(context):
            logger.error(
                "PickSupport: 连续 {} 次都没能打开编队页，本次挑战无法继续。",
                _MAX_ENTRY_ATTEMPTS,
            )
            return CustomAction.RunResult(success=False)

        # 第 2 步：编队页校验助战效果类型（不符则点「助战」重选）
        return self._ensure_desired(context, prefs)

    def _on_formation_page(self, context: Context) -> bool:
        """编队页锚点：底部「开始战斗」。

        实测编队页 box (295,1095,125,44) score≈1.000，助战选择页 hit=False，
        是两页之间最稳的区分依据。
        """
        return bool(_hit(context, _FORMATION_START_ROI, "开始战斗"))

    def _on_support_page(self, context: Context) -> bool:
        """助战选择页锚点：弹窗标题「助战选择」（实测 box (288,219,142,43) score≈0.999）。"""
        return bool(_hit(context, _ASSIST_TITLE_ROI, "助战选择"))

    def _open_formation_page(self, context: Context) -> bool:
        """点开编队页并**校验结果**；返回 False 表示确实没打开，调用方必须放弃本次挑战。

        已经在编队页时直接返回 True（no-op 路径，不做任何点击）。
        否则按 y 升序取助战条目逐个尝试，每条点完都重新认一次「开始战斗」。
        """
        if self._on_formation_page(context):
            return True

        for attempt in range(_MAX_ENTRY_ATTEMPTS):
            entries = _parse_entries(_ocr(context, _ASSIST_ROI))
            if not entries:
                logger.warning("PickSupport: 第 {} 次未识别到助战条目。", attempt + 1)
                return False
            index = min(attempt, len(entries) - 1)
            entry = entries[index]
            x, y = _entry_click_point(entry)
            logger.info(
                "PickSupport: 点击助战条目(属性={}, 类型={}) at ({}, {})，第 {}/{} 条。",
                entry["element"],
                entry["type"],
                x,
                y,
                index + 1,
                len(entries),
            )
            context.tasker.controller.post_click(x, y).wait()
            time.sleep(_PAGE_SETTLE)
            if self._on_formation_page(context):
                return True
            logger.warning("PickSupport: 点击条目 y={} 后仍未进入编队页，换下一条重试。", entry["y"])
        return False

    def _ensure_desired(self, context: Context, prefs) -> CustomAction.RunResult:
        """已在编队页：按**队伍属性**查偏好，元素或类型对不上就重选。"""
        selected = self._detect_selected(context)
        if selected is None:
            logger.error("PickSupport: 已进入编队页但读不到助战 buff，放弃本次挑战。")
            return CustomAction.RunResult(success=False)

        support_element, current = selected
        # 队伍属性必须来自**队伍本身**（主要成员头像左上角的元素徽章，见 _classify_team_element）。
        # 不能拿"已选助战的 buff"顶上：那段文案说的是这个助战**自己**的属性，
        # 拿它当队伍属性是闭环 —— 顶上的条目是火属性时它会一直认定队伍是火，永远纠正不过来。
        team_element = _detect_team_element(context)
        if team_element is None:
            logger.warning(
                "PickSupport: 认不出队伍属性（元素徽章未命中），退回按当前助战属性 {} 处理。",
                support_element,
            )
            team_element = support_element
        desired = prefs.get(team_element)

        if support_element == team_element and (desired is None or current == desired):
            logger.info(
                "PickSupport: 队伍属性={}, 当前助战属性={} 类型={}, 期望={}, 直接开始。",
                team_element,
                support_element,
                current,
                desired,
            )
            return CustomAction.RunResult(success=True)

        logger.info(
            "PickSupport: 队伍属性={}, 当前助战是 {} 属性/{} 类型, 期望 属性={} 类型={}, 需要换。",
            team_element,
            support_element,
            current,
            team_element,
            desired,
        )
        for attempt in range(_MAX_SUPPORT_ATTEMPTS):
            if not self._back_to_support_page(context):
                logger.error("PickSupport: 第 {} 次点「助战」后没有回到助战选择页。", attempt + 1)
                return CustomAction.RunResult(success=False)

            target = self._find_target(context, team_element, desired)
            if target is None:
                logger.warning(
                    "PickSupport: 第 {} 次未找到 属性={} 类型={} 的魔物，按当前助战继续。",
                    attempt + 1,
                    team_element,
                    desired,
                )
                # 找不到符合的魔物就按当前助战继续，但必须重新打开编队页：
                # 此时刚点过「助战」停在助战页，不选一个助战后续「开始战斗」就点不到。
                if not self._open_formation_page(context):
                    logger.error("PickSupport: 未能回到编队页，本次挑战无法继续。")
                    return CustomAction.RunResult(success=False)
                return CustomAction.RunResult(success=True)

            x, y = _entry_click_point(target)
            context.tasker.controller.post_click(x, y).wait()
            time.sleep(_PAGE_SETTLE)
            if not self._on_formation_page(context):
                logger.warning(
                    "PickSupport: 第 {} 次点击条目 y={} 后仍未进入编队页，继续重试。",
                    attempt + 1,
                    target["y"],
                )
                continue
            selected = self._detect_selected(context)
            # 属性必须对上；类型上对不上时，只有 _find_target 明确给了"退让候选"(loose) 才接受，
            # 避免在同一批候选里空转 3 轮。
            if (
                selected
                and selected[0] == team_element
                and (desired is None or selected[1] == desired or target.get("loose"))
            ):
                logger.info("PickSupport: 已选到符合的魔物(属性={}, 类型={})。", team_element, selected[1])
                return CustomAction.RunResult(success=True)
            logger.info("PickSupport: 第 {} 次重选后仍未完全匹配（当前 {}），继续。", attempt + 1, selected)

        logger.warning("PickSupport: 循环 {} 次仍未匹配，按当前助战继续挑战。", _MAX_SUPPORT_ATTEMPTS)
        return CustomAction.RunResult(success=True)

    def _back_to_support_page(self, context: Context) -> bool:
        """编队页点「助战」回到助战选择页并校验；认不到按钮或没切页返回 False。"""
        boxes = _ocr(context, _FORMATION_SUPPORT_BTN_ROI)
        button = next(((t, b) for t, b in boxes if "助战" in t), None)
        if button is None:
            logger.warning("PickSupport: 编队页未识别到「助战」按钮，无法重新选择助战。")
            return False
        context.tasker.controller.post_click(_box_center_x(button[1]), _box_center_y(button[1])).wait()
        time.sleep(_PAGE_SETTLE)
        return self._on_support_page(context)

    def _detect_selected(self, context: Context):
        """编队页读取已选助战的 buff，返回 (属性, 类型)；不在编队页或读不到返回 None。

        ⚠️ 必须先确认「真的在编队页」再读 buff：助战选择页与编队页显示同一段
        「武器技能「神火」的效果上升150%」，且都落在 _FORMATION_BUFF_ROI 内
        （实测助战页 y=906 / 编队页 y=886）。不校验页面归属就会把「还停在
        助战选择页」误判成「已选到符合的助战」，从而返回假的成功。
        """
        if not self._on_formation_page(context):
            return None
        boxes = _ocr(context, _FORMATION_BUFF_ROI)
        for t, _b in boxes:
            if any(k in t for k in ("属性", "神火", "魔火", "攻击力上升", "效果上升")):
                return _parse_buff(t)
        boxes = _ocr(context)
        for t, _b in boxes:
            if any(k in t for k in ("属性", "神火", "魔火", "攻击力上升", "效果上升")):
                return _parse_buff(t)
        return None

    def _switch_tab(self, context: Context, x: int) -> None:
        """切到元素页签 x，并把助战列表**滚回顶部**。

        实测：切页签不会把列表滚动位置复位。上一轮扫描把列表滑到了底部，
        不回到顶部就会漏掉最上面那几条（用户复现："由上往下滑动 3 次"之后才看到符合的助战）。
        """
        context.tasker.controller.post_click(x, _ELEMENT_TAB_Y).wait()
        time.sleep(_TAB_SETTLE)
        for _ in range(_SCROLL_TOP_TIMES):
            context.tasker.controller.post_swipe(360, 500, 360, 1000, 300).wait()
            time.sleep(_SCROLL_TOP_SETTLE)

    def _reveal_tab_row(self, context: Context) -> None:
        """把元素页签行横向滑到底，露出被挡住的第 6 个页签（暗）。

        用户实测：页签行一屏只放得下 5 个（火/风/土/水/光），暗 必须横向滑动标签行才显示。
        滑动后各页签的 x 会整体偏移，所以滑动只负责"把暗露出来"，
        具体点哪个位置由 `_find_target` 逐个试 + 用列表内容验证。
        """
        for _ in range(_ELEMENT_TAB_SWIPE_TIMES):
            context.tasker.controller.post_swipe(
                _ELEMENT_TAB_SWIPE_FROM_X,
                _ELEMENT_TAB_Y,
                _ELEMENT_TAB_SWIPE_TO_X,
                _ELEMENT_TAB_Y,
                400,
            ).wait()
            time.sleep(_ELEMENT_TAB_SWIPE_SETTLE)

    def _active_element(self, context: Context):
        """读助战列表条目的属性 = **当前选中页签**的属性（列表就是按页签过滤的）。"""
        for entry in _parse_entries(_ocr(context, _ASSIST_ROI)):
            if entry["element"]:
                return entry["element"]
        return None

    def _locate_element_tab(self, context: Context, team_element):
        """认出队伍属性对应的元素页签横向中心（必要时先横向滑出页签行）。

        两条判据配合：
          · **按颜色**认未选中的页签（可靠，实测 4/4）；
          · 颜色认不出时，若"当前选中的那个宽块页签"就是队伍属性（用**列表内容**确认），
            就直接用它 —— 选中态图标被高亮压暗、颜色不可靠，但"它是那个 113px 宽块"
            以及"列表里显示的是哪种属性"都是确定的事实。
        返回 None 表示"认不出"，调用方退回实测坐标映射。
        """
        img = _screencap(context)
        if img is not None:
            x = _tab_x_for_element(img, team_element)
            if x is not None:
                logger.info("PickSupport: 按颜色认出 {} 属性页签 x={}。", team_element, x)
                return x
            selected = _selected_tab_x(img)
            if selected is not None:
                active = self._active_element(context)
                if active == team_element:
                    logger.info("PickSupport: 当前选中的页签就是 {}（列表内容确认）x={}。", team_element, selected)
                    return selected
                logger.info("PickSupport: 当前选中页签是 {}，不是 {}，继续找。", active, team_element)
        # 页签行一屏只放得下 5 个（火/风/土/水/光），暗 要横向滑动才露出（用户实测）
        logger.info("PickSupport: 可见页签里没有可用的 {} 属性页签，横向滑动元素页签行后再认。", team_element)
        self._reveal_tab_row(context)
        img = _screencap(context)
        if img is None:
            return None
        x = _tab_x_for_element(img, team_element)
        if x is None:
            # 滑动后目标页签可能正好以"选中态宽块"的形式出现（宽块一律不判色）：
            # 用列表内容确认它是不是队伍属性。
            selected = _selected_tab_x(img)
            if selected is not None and self._active_element(context) == team_element:
                logger.info("PickSupport: 滑动后可见的选中态页签就是 {} x={}。", team_element, selected)
                return selected
        if x is not None:
            logger.info("PickSupport: 滑动后按颜色认出 {} 属性页签 x={}。", team_element, x)
        else:
            logger.warning("PickSupport: 滑动后仍认不出 {} 属性页签，退回实测坐标映射。", team_element)
        return x

    def _find_target(self, context: Context, team_element, desired):
        """在助战选择页找 属性=team_element（且类型=desired）的条目；找不到返回 None。

        顺序由强到弱，且**尽量只切一个页签**（不再"5 个页签盲试"）：
          0. 先用**列表内容**判断当前页签是否已经对了（列表 = 当前页签的属性）：对了就**一步都不切**，
             直接扫当前列表；这一步是实机教训换来的（认不出"选中态宽块"时会白跑一整轮盲试）；
          1. **按颜色认出**该属性对应的页签 x（`_locate_element_tab`：和认元素徽章同一套取色判据，
             不在可见范围内就先横向滑出页签行 —— 暗 就是这么认出来的），认不出才退回实测坐标映射；
          1. 切到该页签、并把列表滚回顶部后扫"属性+类型"；
          2. 该页签里没有"属性+类型"都对上的，退一步接受"属性对、类型不对"的条目
             （对队伍有用得多，胜过跨属性）；
          3. 已知属性到此为止：每个页签只显示一种属性，盲试其它页签实测要白花约 30 次滑动 /
             30+ 秒却必然找不到，所以只把页签与列表**恢复成"队伍属性 + 列表顶部"**再返回 None
             —— 否则调用方收尾会点"最后试过的页签"上的条目（实测：风队配到了光助战，比不换还差）；
          4. 只有**未映射的属性**（暗）才逐个位置点、用列表内容验证（滑动后 x 会整体偏移）。
        """
        # 第 0 步（必须先做）：用**列表内容**判断"当前页签是不是已经对了"。
        # 列表是按页签过滤的，所以"列表显示什么属性"就等于"当前选中哪个页签"。
        # 已经对了就**一步都不用切**，直接扫当前列表 —— 实机教训（07:02 那次）：
        # 队伍与列表都是 暗，暗 恰好是选中态宽块（按设计不判色）、当时又被滑出视野，
        # 于是白跑了"横向滑动页签行 + 逐个位置试"：4 个页签、约 50 秒。
        if self._active_element(context) == team_element:
            logger.info("PickSupport: 当前列表已经是 {} 属性，无需切页签，直接找。", team_element)
            target = self._scan_entries(context, team_element, desired)
            if target is not None:
                return target
            loose = self._scan_entries(context, team_element, None)
            if loose is not None:
                logger.info(
                    "PickSupport: {} 列表里没有 类型={} 的魔物，退一步接受同属性的条目。",
                    team_element,
                    desired,
                )
                loose["loose"] = True
                return loose
            logger.info("PickSupport: {} 列表里没有可用条目（页签本来就是对的，不再换页签）。", team_element)
            return None

        # 主路径：**像认徽章一样按颜色认出**该属性的页签（页签行可横向滚动，必要时先滑出来）。
        tab_x = self._locate_element_tab(context, team_element)
        if tab_x is None:
            # 兜底：认不出（截图失败/颜色异常）才退回实测坐标映射（只覆盖可见的 5 个）
            tab_x = _ELEMENT_TAB_X.get(team_element)
        if tab_x is not None:
            logger.info("PickSupport: 直接切到 {} 属性的元素页签 (x={})。", team_element, tab_x)
            self._switch_tab(context, tab_x)
            target = self._scan_entries(context, team_element, desired)
            if target is not None:
                return target
            loose = self._scan_entries(context, team_element, None)
            if loose is not None:
                logger.info(
                    "PickSupport: {} 页签里没有 类型={} 的魔物，退一步接受同属性(类型={})的条目。",
                    team_element,
                    desired,
                    loose["type"],
                )
                loose["loose"] = True  # 告知调用方：属性对但类型不对，别再循环重选
                return loose
        if tab_x is not None:
            # 已知属性：该属性的条目只可能出现在它自己的页签上（每个页签只显示一种属性），
            # 所以**不再盲试其它 4 个页签** —— 实测那要白花约 30 次滑动 / 30+ 秒。
            # 只把页签与列表恢复成"队伍属性 + 列表在顶部"，便于调用方收尾时挑到同属性条目
            # （实测事故：风队被配到光助战，比不换还差）。
            logger.info("PickSupport: {} 页签里没有可用条目，恢复页签后按当前助战继续。", team_element)
            self._switch_tab(context, tab_x)
            return None
        # 未映射的属性（目前只有 暗，需横向滑动才露出）：滑动后各页签 x 会整体偏移，
        # 所以不写死坐标，逐个位置点、**用列表内容验证**。
        for x in _ELEMENT_TAB_FALLBACK_X:
            logger.info("PickSupport: 试切页签位置 (x={})，找 {} 属性条目。", x, team_element)
            self._switch_tab(context, x)
            target = self._scan_entries(context, team_element, desired)
            if target is not None:
                return target
            loose = self._scan_entries(context, team_element, None)
            if loose is not None:
                logger.info(
                    "PickSupport: 该页签里没有 类型={} 的魔物，退一步接受同属性(类型={})的条目。",
                    desired,
                    loose["type"],
                )
                loose["loose"] = True
                return loose
        return None

    def _scan_entries(self, context: Context, team_element, desired):
        """在当前助战列表里找目标条目，必要时下滑；找不到返回 None。

        优先跳过列表最上面那条（通常就是当前已选中的那条，重复点它不会改变结果），
        只有确实没有别的候选时才允许选它。
        """
        fallback = None
        for scroll in range(4):  # 0=不滑，1~3=下滑
            if scroll > 0:
                context.tasker.controller.post_swipe(360, 1000, 360, 500, 300).wait()
                time.sleep(1.5)
            entries = _parse_entries(_ocr(context, _ASSIST_ROI))
            if scroll == 0:
                # 把"当前页签里到底有哪些 (属性, 类型)"打出来：元素页签与属性的对应关系
                # 就是靠这条日志反推出来的（131→火 245→风 359→土 472→水 585→光），
                # 留着便于以后核对页签位置、也便于确认"到底有没有同属性条目"。
                logger.debug(
                    "PickSupport: 当前列表条目 = {}",
                    [(e["element"], e["type"]) for e in entries],
                )
            for index, entry in enumerate(entries):
                if entry["element"] != team_element:
                    continue
                if desired is not None and entry["type"] != desired:
                    continue
                if index == 0:
                    if fallback is None:
                        fallback = entry
                    continue
                return entry
        return fallback
