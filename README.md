<!-- markdownlint-disable -->

<div align="center">

<img alt="LOGO" src="./resource/base/image/icon.png" width="200" height="200" />

# MaaHJ

<br>
<div>
    <img alt="platform" src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-blueviolet">
    <img alt="Python" src="https://img.shields.io/badge/Python-3.13-%233776AB?logo=python&logoColor=white">
</div>
<div>
    <img alt="license" src="https://img.shields.io/github/license/404Ming/MaaHJ">
    <img alt="commit" src="https://img.shields.io/github/commit-activity/m/404Ming/MaaHJ?color=%23ff69b4">
    <img alt="version" src="https://img.shields.io/badge/version-v0.1.2-informational">
</div>
<div>
    <img alt="stars" src="https://img.shields.io/github/stars/404Ming/MaaHJ?style=social">
</div>
<br>

MaaHJ 的意思是 Maa 哼唧

一款游戏日常小助手

基于 [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 的图像识别能力与 Python Agent，一键完成日常、周常、商店、骑士团、副本挑战等重复劳动！

绝赞更新中 ✿✿ヽ(°▽°)ノ✿

</div>

## 下载与安装

本仓库只包含**源码与资源**（`agent/`、`resource/`、`tasks/`、`config/`、`interface.json` 等），运行时二进制（Python 运行时、`libs/`、`runtimes/`、`MFAAvalonia` 前端）不纳入版本管理。

- **直接使用**：下载构建好的发布包，解压后运行 `maahj.exe`，无需安装 Python。
- **从源码运行**：见下方 [从源码运行](#从源码运行)。
- **前端**：图形界面基于 [MFAAvalonia](https://github.com/SweetSmellFox/MFAAvalonia)，配置由 `interface.json` 描述。

## 配置指引

> 运行提示：本助手按 **竖屏、短边 720** 的基准做识别，`interface.json` 中的 `Adb` 控制器已内置 `display_short_side: 720`。请把模拟器分辨率设为 `720×1280`，游戏内不要切到横屏，否则 ROI 全部错位。
>
> 反馈指南：遇到卡点欢迎提交 [Issue](https://github.com/404Ming/MaaHJ/issues)。为便于定位，请附上出错前后的截图、`agent/debug/` 下的日志，以及在界面上选的选项组合。

**第一步 · 模拟器**

- 推荐 **MuMu 模拟器 12**，显示设置为 `手机版 720×1280 320DPI`，开启 ADB 调试

**第二步 · 游戏内设置**

自动化流程依赖游戏内的基础设置：
设置截图（基础 / 画面 / 战斗设置）

<img alt="设置-基础" src="./docs/images/settings-basic.png" width="300px"/>
<img alt="设置-画面" src="./docs/images/settings-display.png" width="300px"/>
<img alt="战斗设置" src="./docs/images/settings-battle.png" width="300px"/>


**第三步 · 选择任务**

**PC 端（MFAAvalonia，`maahj.exe`）**

在左侧任务列表勾选需要跑的任务（可多个），必要时点任务右侧箭头展开并修改选项，改完点「开始」。

**Android 端（哼唧小助手 APK）**

1. 打开「任务」页：还没有配置时，点上方选择器新建空配置，或点「设置」滑动到「导入配置」导入作者预设配置[maahj-config-android.json](https://github.com/404Ming/MaaHJ/blob/main/Android/maahj-config-android.json)
2. 全局选项（作用于每次运行）在「设置」里「任务设置」改
3. 点「开始任务」开始跑；运行中点预览可全屏查看，返回键退出全屏

两端可选的选项含义一致：

- 「玩法挑战」需要选择副本与难度，并填写挑战次数（真实消耗体力）
- 「机神降临」可分别开关探索领取、元素自律机械、机神降临，并各自设置次数
- 「商店购买」的商品清单会随活动轮换，未在售的条目会被静默跳过
- 「骑士团」的每日捐献与公会挖宝可分别关闭

## 亮点功能

- **日常任务**：一键完成游戏内日常清单，自动领取各处日常奖励
- **周常任务**：周常流程全自动，支持选择「冒险祈愿 / 友情祈愿」作为抽卡 10 次的祈愿池
- **商店购买**：折扣礼包、公会商店、征伐印记商店，按卡片上的当日 / 当周限购上限**买满**，售罄与买满商品自动重新扫描，绝不误买清单外的商品
- **骑士团**：每日金币捐献 10 次、公会挖宝挑战（不消耗体力）自动完成
- **机神降临**：自动探索领取、挑战「元素自律机械」、挑战多人房间战「机神降临」，次数可自定义，次数不足时自动停止并在日志说明
- **玩法挑战**：（史莱姆宝库 / 火·风·水·土元素试炼）+ 难度（普通 / 困难 / 噩梦 / 深渊）+ 挑战次数，任选其刷
- **古代之塔 / 邮件**：常用资源入口逐个自动化领取
- **战斗模块**：跳过回合可单独开关；六属性编队按队伍属性自动匹配「神 / 魔 / 属攻」助战效果
- **体力补充**：豪华（150）/ 标准（90）/ 小型（30）三档体力药可指定消耗优先级，单次取「不溢出的最大数量」，补满体力上限 999 后自动结束
- **通用能力**：启动游戏、返回主页、异常处理（弹窗关闭、断线重连、体力不足等），全部由 Pipeline 与 Python Agent 协作完成
- **副本挑战**：自动挑战副本
- **快速炼金**：自动进熔合炉炼金，排序固定按「道具数量」从多到少，一键选择后开始炼金并自动关闭领奖弹窗，次数可设 1-999，跑完自动回主页

## 使用说明

### 功能介绍

在图形界面中勾选任务后启动即可，任务均可单独运行：

| 任务 | 说明 |
| --- | --- |
| 日常任务 | 日常清单全流程 |
| 周常任务 | 周常流程，需选择祈愿池 |
| 商店购买 | 折扣礼包 / 公会商店 / 征伐印记商店按限购买满 |
| 快速炼金 | 进熔合炉按「道具数量」排序加料，循环炼金并关闭领奖弹窗，次数可设 1-999 |
| 骑士团 | 每日捐献、公会挖宝（可分别关闭） |
| 古代之塔奖励领取 | 塔奖励逐层领取 |
| 邮件领取 | 一键领取并清理邮件 |
| 机神降临 | 探索领取 + 元素自律机械 + 机神降临，次数可设 |
| 玩法挑战 | 终末之地副本 + 难度 + 次数 |
| 副本挑战 / 活动 | 对应入口的常规挑战与奖励 |
| 启动游戏 / 返回主页 | 作为其他任务的公共前置与收尾 |
| 仅跳过回合 / 仅补充体力 | 独立的小工具流程 |

全局选项（「战斗设置」内统一配置）：跳过回合、体力药消耗优先级，以及水 / 火 / 风 / 土 / 光 / 暗六属性的编队助战偏好。

### 目录结构

```text
agent/                 Python Agent（入口 agent/main.py）
  custom/action/       自定义动作：玩法挑战、商店、体力、周常、机神降临等
  custom/reco/         自定义识别
  utils/               参数、日志、路径工具
resource/base/
  pipeline/            Pipeline 节点（日常、周常、商店、骑士团、机神降临……）
  image/               模板图片
  model/ocr/           OCR 模型（det / rec）
tasks/                 任务入口定义（guide.json / main.json）
config/                运行时配置
interface.json         Project Interface 描述（控制器、资源、任务、选项）
```

### 从源码运行

```bash
uv sync
uv run python agent/main.py
```

发布包自带 Python 运行时与依赖，由包内解释器直接启动 `agent/main.py`。

仓库同时提供格式化与校验工具（需 Node.js >= 22.13）：

```bash
pnpm install
pnpm check       # 格式化 + schema + maa-tools 校验
pnpm check:py    # ruff + pyright
```

### 添加 Python 依赖

```bash
uv add <包名>
create-maa-project --update python-deps
```

`requirements.txt` 需与 `pyproject.toml`、`uv.lock` 一起提交，否则发布包会缺依赖。

## 加入项目

### 主要关联项目

- 自动化框架：[MaaFramework](https://github.com/MaaXYZ/MaaFramework)
- 图形界面：[MFAAvalonia](https://github.com/SweetSmellFox/MFAAvalonia)
- 项目脚手架：[create-maa-project](https://github.com/Windsland52/create-maa-project)
- 公共资源与 OCR 模型：[MaaCommonAssets](https://github.com/MaaXYZ/MaaCommonAssets)

### 工具链

- [MaaPipelineEditor](https://github.com/kqcoxn/MaaPipelineEditor)：可视化构建、审阅、编辑与调试 MaaFramework Pipeline
- [Everything-Maa](https://github.com/KhazixW2/Everything-Maa)：面向 MaaFramework 的技能、MCP 集成与 AI 辅助开发工具集
- [maa-tools](https://www.npmjs.com/package/@nekosu/maa-tools)：`pnpm check` / `pnpm format` 背后的格式化与 schema 校验工具

### 参与开发

- Pipeline 节点：`resource/base/pipeline/`，遵循 MaaFramework Pipeline 协议
- 自定义逻辑：`agent/custom/action`（`@AgentServer.custom_action`）、`agent/custom/reco`、`agent/custom/sink`
- 界面选项：`interface.json` 的 `option` 与节点的 `pipeline_override` 对应，修改选项时请同步检查 Python 侧 `context.get_node_data()` 的读取路径
- 提交前请跑 `pnpm check` 与 `pnpm check:py`

## 致谢

### 开源库

- 自动化框架：[MaaFramework](https://github.com/MaaXYZ/MaaFramework)
- 图形界面：[MFAAvalonia](https://github.com/SweetSmellFox/MFAAvalonia)、[Avalonia](https://github.com/AvaloniaUI/Avalonia)、[SukiUI](https://github.com/kikipoulet/SukiUI)
- 图像识别库：[opencv](https://github.com/opencv/opencv)
- 机器学习推理：[onnxruntime](https://github.com/microsoft/onnxruntime)
- 文字识别模型：[PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)（模型经 [MaaCommonAssets](https://github.com/MaaXYZ/MaaCommonAssets) 分发）
- 日志：[loguru](https://github.com/Delgan/loguru)
- JSON / 校验工具：[ajv](https://github.com/ajv-validator/ajv)、[jsonc-parser](https://github.com/microsoft/node-jsonc-parser)、[prettier](https://github.com/prettier/prettier)

### 贡献 / 参与者

感谢所有参与到开发与测试中的朋友们，是大家的帮助让 MaaHJ 越来越好！ (\*´▽｀)ノノ

## 声明

- 本软件使用 [GNU Affero General Public License v3.0](https://www.gnu.org/licenses/agpl-3.0.html) 开源，详见 [LICENSE](./LICENSE)。
- 本软件开源、免费，仅供学习交流使用。若您遇到商家使用本软件进行代练并收费，产生的问题及后果与本软件无关。
- 本软件与游戏官方无关，不修改游戏文件、不读取游戏内存，仅通过截图识别与模拟触控完成操作。请自行评估账号风险。
- 本项目不含游戏美术资源的所有权，相关素材版权归原游戏厂商所有。

### DirectML 支持说明

发布包在 Windows 平台上附带 Microsoft 提供的独立组件 [DirectML](https://learn.microsoft.com/windows/ai/directml/)，用于 GPU 推理加速。DirectML 并非本项目的开源部分，也不受 AGPL 3.0 约束；如需精简体积可删除对应 DLL，核心功能仍可正常运行。

## 广告

如果觉得软件对你有帮助，帮忙点个 Star 吧！~（网页最上方右上角的小星星），这就是对我最大的支持了！
