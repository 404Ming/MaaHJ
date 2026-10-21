"""让「停止」在动作执行中间也能立刻生效。

MaaFramework 的 CustomAction 是一次阻塞调用：``MaaTaskerPostStop`` 只置停止标志，
框架要等这次调用返回才在节点边界收手。所以动作内部的循环只要不主动查
``tasker.stopping``，用户点了「停止」之后它就还会继续点、继续划，直到动作自己跑完。

逐个循环手写检查的覆盖率靠人肉保证（``shop.py`` 就一处都没有）。本模块改从执行层兜底：

1. 包装所有注册到 ``AgentServer`` 的 CustomAction，执行期间把当前 ``context`` 记在
   线程局部变量里；
2. 把 ``time.sleep`` 换成可中断版本 —— 每 0.1 秒查一次停止标志，一旦置位就抛
   ``StopRequested``；
3. 包装层捕获 ``StopRequested``，立刻返回 ``RunResult(success=False)``。

于是任何动作（包括以后新写的、忘了写 ``is_stopping`` 的）都会在最迟一个 sleep 片段
（≈0.1 秒）内收手。

``StopRequested`` 刻意继承 ``BaseException``：动作里到处是 ``except Exception`` 的重试
分支，继承 Exception 会被它们吞掉，循环照转；继承 BaseException 才能穿过这些兜底。
"""

from __future__ import annotations

import functools
import threading
import time
from typing import Any, Callable

from utils import logger
from utils.interrupt import is_stopping

# 每次 sleep 切片的长度：越小停止越灵敏，但唤醒次数越多。
_SLEEP_STEP = 0.1

_state = threading.local()
_installed = False
_original_sleep = time.sleep


class StopRequested(BaseException):
    """用户请求停止（或在停止中被置位）。只在动作包装层内被捕获。"""


def _current_context() -> Any:
    return getattr(_state, "context", None)


def _interruptible_sleep(seconds: Any) -> None:
    context = _current_context()
    if context is None:
        return _original_sleep(seconds)

    try:
        remaining = float(seconds)
    except (TypeError, ValueError):
        return _original_sleep(seconds)

    while remaining > 0:
        if is_stopping(context):
            raise StopRequested
        chunk = min(_SLEEP_STEP, remaining)
        _original_sleep(chunk)
        remaining -= chunk


def _guard_run(run: Callable[..., Any]) -> Callable[..., Any]:
    if getattr(run, "_stop_guarded", False):
        return run

    @functools.wraps(run)
    def wrapper(self: Any, context: Any, argv: Any) -> Any:
        from maa.custom_action import CustomAction

        previous = _current_context()
        _state.context = context
        try:
            return run(self, context, argv)
        except StopRequested:
            logger.info("收到停止请求，当前动作已中断。")
            return CustomAction.RunResult(success=False)
        finally:
            _state.context = previous

    wrapper._stop_guarded = True  # type: ignore[attr-defined]
    return wrapper


def install() -> None:
    """安装全局停止兜底。必须在 ``custom`` 包被导入之前调用。"""
    global _installed
    if _installed:
        return

    from maa.agent.agent_server import AgentServer

    original_custom_action = AgentServer.custom_action

    def custom_action(name: str) -> Callable[[Any], Any]:
        register = original_custom_action(name)

        def decorate(target: Any) -> Any:
            run = getattr(target, "run", None)
            if isinstance(target, type) and callable(run):
                # 同一个类可能堆叠多个 @custom_action（别名），包装是幂等的。
                target.run = _guard_run(run)  # type: ignore[method-assign]
            return register(target)

        return decorate

    AgentServer.custom_action = custom_action  # type: ignore[method-assign]
    time.sleep = _interruptible_sleep  # type: ignore[assignment]
    _installed = True
    logger.debug("stop_guard installed")
