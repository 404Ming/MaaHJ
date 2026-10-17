import os
import sys

if sys.version_info < (3, 13) or sys.version_info >= (3, 14):
    raise SystemExit("Python >=3.13,<3.14 is required, got " + sys.version.split()[0])

# agent 的控制台日志 sink 在 sys.stderr（见 utils/logger.py::_resolve_console_stream）。
# 中文 Windows 上被管道重定向的 stderr 默认按 ANSI 代码页（GBK）编码，而 MaaFW/MPE
# 按 UTF-8 读取子进程输出 ⇒ 中文日志全变 �。stdout/stderr 都要 reconfigure，
# 只修 stdout 修不掉乱码（那正是当初只改 stdout 后 MPE 里仍乱码的原因）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    except AttributeError:
        pass

current_file_path = os.path.abspath(__file__)
agent_dir = os.path.dirname(current_file_path)
project_root_dir = os.path.dirname(agent_dir)

if os.getcwd() != project_root_dir:
    os.chdir(project_root_dir)

if agent_dir not in sys.path:
    sys.path.insert(0, agent_dir)

from maafw_paths import ensure_maafw_binary_path  # noqa: E402

# 必须早于 agent_runtime：utils 包在导入时就连带 import maa，那之后库目录已经定死
ensure_maafw_binary_path(project_root_dir)

from agent_runtime import run_agent  # noqa: E402


def main() -> int:
    return run_agent(project_root_dir=project_root_dir)


if __name__ == "__main__":
    sys.exit(main())
