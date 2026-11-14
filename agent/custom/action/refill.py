"""「补充体力 / 补充精力」公共状态机的调用封装。

为什么需要它：快速连打（`_run_battle(repeat_with_next_stage=N)`）把多场战斗放在**同一个
CustomAction** 里，主流程的监督节点（`Event_Start` → `[JumpBack]Exception_Start`）在这期间
不会再跑，体力不足时没有别人替它补体力。旧实现只在「挑战下一关」点完 8 秒后调了一次
`Exception_Start`：那是**整条异常链**（容易顺带点掉结算按钮），而且没有复位 `max_hit`
命中计数，补完也不保证弹窗关掉 —— 用户看到的就是「没体力了不会补，直接停」。

这里只做最窄的一件事：认「补充体力 / 补充精力」标题 → 复位命中计数 → 跑公共状态机
`StaminaRefill_Dialog` / `EnergyRefill_Dialog` → 确认弹窗已关。调用方补完再点被拦下的按钮。
"""

from __future__ import annotations

import time

from utils import logger

from .fastframe import frame_index, pick_contains, pick_exact

# 弹窗标题门控（与 StaminaRefill_Dialog / Exception_Stamina / 终末之战同一套口径）
_STAMINA_TITLE_ROI = (180, 120, 380, 100)
_STAMINA_TITLE_TEXT = "补充体力"
_ENERGY_TITLE_ROI = (200, 110, 340, 120)
_ENERGY_TITLE_TEXTS = ("补充精力", "恢复精力")

# 带 max_hit 的节点：命中计数按 Tasker 累加、run_task 之间不清零。
# 最致命的是 `StaminaRefill_BackClick`（max_hit=3，体力/精力共用）：用光之后弹窗再也关不掉。
_STAMINA_RESET_NODES = (
    "StaminaRefill_Cycle",
    "StaminaRefill_UseSmall",
    "StaminaRefill_UseStandard",
    "StaminaRefill_UseLuxury",
    "StaminaRefill_BackClick",
    "EnergyRefill_UseFirst",
    "EnergyRefill_DetailUse",
    "EnergyRefill_DetailClose",
    "EnergyRefill_Back",
)
_REFILL_CLOSE_RETRY = 2


def reset_stamina_counts(context) -> None:
    """清掉补体力/精力链上所有 max_hit 节点的命中计数（清不掉只记日志，不打断补体力）。"""
    for name in _STAMINA_RESET_NODES:
        try:
            context.clear_hit_count(name)
        except Exception as error:  # noqa: BLE001
            logger.warning("补充体力：复位 {} 的命中计数失败（忽略）：{}", name, error)


def panel_kind(context) -> str:
    """当前挡在画面上的是哪种补充弹窗：``"体力"`` / ``"精力"`` / ``""``（都没有）。"""
    index = frame_index(context, _STAMINA_TITLE_ROI, _ENERGY_TITLE_ROI)
    if pick_exact(index, _STAMINA_TITLE_TEXT) is not None or pick_contains(index, _STAMINA_TITLE_TEXT) is not None:
        return "体力"
    if pick_contains(index, *_ENERGY_TITLE_TEXTS) is not None:
        return "精力"
    return ""


def close_panel(context, kind: str) -> bool:
    """把挡路的补充弹窗关掉（先复位命中计数再点公共返回），返回是否确实关掉了。"""
    for attempt in range(_REFILL_CLOSE_RETRY):
        reset_stamina_counts(context)
        context.run_task("StaminaRefill_Back" if kind == "体力" else "EnergyRefill_Back")
        time.sleep(1.0)
        if not panel_kind(context):
            logger.debug("补充体力：补充{}弹窗已关闭。", kind)
            return True
        logger.warning("补充体力：补充{}弹窗第 {} 次没关掉，复位命中次数后再试。", kind, attempt + 1)
    return not panel_kind(context)


def refill(context, *, wait: float = 3.0) -> bool:
    """弹窗在场时按全局「体力药消耗优先级」补满并关掉弹窗。

    返回 True = 补上了（弹窗已关，调用方可以重新点被拦下的按钮）；
    False = 没有弹窗，或弹窗关掉了但没补上（药不够 / 已到上限），调用方应停止重试。
    没弹窗时本函数只在 `wait` 秒内轮询标题，命中才动。
    """
    deadline = time.time() + wait
    kind = ""
    while time.time() < deadline:
        kind = panel_kind(context)
        if kind:
            break
        time.sleep(0.2)
    if not kind:
        return False

    logger.info("补充体力：检测到「补充{}」弹窗，按全局设置补满后继续。", kind)
    reset_stamina_counts(context)
    context.run_task("StaminaRefill_Dialog" if kind == "体力" else "EnergyRefill_Dialog")
    time.sleep(1.5)
    if not panel_kind(context):
        logger.info("补充体力：补充{}完成，弹窗已关闭。", kind)
        return True
    logger.warning("补充体力：补{}后弹窗还开着（药不够、已到上限或返回键被跳过），尝试直接关闭。", kind)
    return close_panel(context, kind) and not panel_kind(context)
