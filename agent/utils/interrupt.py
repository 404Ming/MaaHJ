"""客户端「停止」请求的检测工具。

MaaFramework 的 CustomAction 是一次阻塞调用，框架只会在节点边界检查停止标志；
动作内部的长轮询 / 长循环如果不主动查询 `tasker.stopping`，用户点「停止」后
Agent 仍会一直跑到动作自己返回，表现为「停止按钮没反应」。
"""

from __future__ import annotations

import time
from typing import Any


def is_stopping(context: Any) -> bool:
    """当前任务是否已被请求停止（客户端点「停止」/ 任务超时被 post_stop）。"""
    try:
        return bool(context.tasker.stopping)
    except Exception:  # noqa: BLE001 - 查询失败时按「未停止」处理，不影响正常流程
        return False


def sleep_unless_stopping(context: Any, seconds: float, step: float = 0.2) -> bool:
    """可中断的 sleep：返回 True 表示期间收到了停止请求。"""
    remaining = max(0.0, seconds)
    step = max(0.05, step)
    while remaining > 0:
        if is_stopping(context):
            return True
        chunk = min(step, remaining)
        time.sleep(chunk)
        remaining -= chunk
    return is_stopping(context)
