# MaaHJ Android 客户端

把本项目（Pipeline + 资源 + Python agent）打进一个 Android APK，基于
[Aliothmoon/MaaFwApp](https://github.com/Aliothmoon/MaaFwApp)（MaaFramework 的 Android 通用 GUI）。

资源在**构建期**打进 APK：换资源就要重新出包，手机上不需要另外拷贝任何文件。

## 产物

- 包名：`com.aliothmoon.maafw.maahj`
- 应用名：哼唧小助手
- ABI：`arm64-v8a` + `x86_64` 通用包（真机与模拟器同一个 APK，`MAFW_PACKAGE_ABI=universal`）
- `minSdk 28` / `targetSdk 36`
- 包内结构：`assets/pi.zip`（interface.json + tasks + resource + agent）、`assets/agent/bundle.zip`（两个 ABI 各一份 CPython 3.13 + maa + loguru + numpy）、`lib/arm64-v8a/*.so` 与 `lib/x86_64/*.so`（MaaFramework + onnxruntime + opencv）

## 构建

### 环境

| 依赖 | 要求 |
| --- | --- |
| JDK | 17 |
| Android SDK | `platforms;android-37.0`、`build-tools;37.0.0`、`platform-tools`（NDK 由 AGP 自动补） |
| Python | 3.13（组 agent 运行时） |
| 网络 | 需要能访问 GitHub（MaaFwApp 与 MaaFramework 产物从 Release 下载） |

### 一键构建

```powershell
$env:JAVA_HOME    = 'C:\Program Files\Eclipse Adoptium\jdk-17.0.20.101-hotspot'
$env:ANDROID_HOME = 'D:\Android\Sdk'
.\Android\build.ps1 -Version 0.1.9
```

默认出双 ABI 通用包。只想出单架构（体积小一半以上）时：

```powershell
.\Android\build.ps1 -Version 0.1.9 -Abi arm64-v8a
```

脚本依次做：克隆 MaaFwApp → 覆盖 `Android/overrides/` 里的源码改动 → 铺 MaaFramework `.so` → 组 agent 运行时 → 写 `local.properties` → `assembleRelease`。

### 对 MaaFwApp 的源码改动

上游 MaaFwApp 的显示逻辑写死了横屏，且虚拟屏上的游戏被系统搬走后不会自己回来。本项目要跑竖屏
（720×1280）配方并依赖后台虚拟屏，因此有 6 个文件被改过：

| 文件 | 改动 |
| --- | --- |
| `app/.../runner/DisplayResolution.kt` | 注释校正（预设校验不再限定横屏） |
| `app/.../ui/tasks/TasksPreviewSection.kt` | 全屏预览按虚拟屏方向锁屏：竖屏档位锁竖屏，横屏档位才锁横屏 |
| `build-logic/.../gradle/BuildProfile.kt` | `display.presets[]` 校验从「必须 `width > height`」放宽为「不能是正方形」 |
| `app/.../bridge/DriverClass.java` | `StartApp` 时先 `AppWatchdog.expectTarget()`，把目标包作为先验知识交给看门狗 |
| `app/.../remote/internal/ActivityUtils.kt` | 启动校验改为「在目标屏上待住 3 秒」才算钉住，挡住二段跳的第二次投放 |
| `app/.../remote/internal/AppWatchdog.kt` | 按先验目标判定漂移：虚拟屏上一次没出现过目标也能拉回，且不被别的应用顶掉目标 |

两个位置保存同一份改动，含义不同：

- `Android/overrides/`：改完后的**完整文件**，按上游相对路径镜像存放；`build.ps1` 克隆完 MaaFwApp 后整文件覆盖，构建一定用上这些改动
- `Android/patches/`：相对上游 `bc55dac` 的 diff，按主题拆开——`0001-portrait-orientation.patch`（竖屏显示）、
  `0002-pin-app-on-virtual-display.patch`（虚拟屏漂移拉回），只用于审阅与升级上游时重做改动（`git apply` 到 MaaFwApp 目录即可）

### 手动构建

```powershell
git clone https://github.com/Aliothmoon/MaaFwApp.git .build\MaaFwApp
cd .build\MaaFwApp
python scripts/setup_maa_framework.py --tag v5.13.0
python scripts/build_agent_bundle.py --out <仓库根>/.build/agent-dist `
    --requirements <仓库根>/requirements.txt --no-deps
```

在 `.build\MaaFwApp\local.properties` 写：

```properties
sdk.dir=D:/Android/Sdk
pi.profile=<仓库根>/Android/pi-profile.yaml
build.releaseAbi=arm64-v8a,x86_64
build.versionName=0.1.9
```

`build.releaseAbi` 逗号分隔；写多个 ABI 时只出一个通用包（`MAFW_PACKAGE_ABI=universal`），
只写一个时按该 ABI 命名。agent 运行时也要按同样清单逐个 `--abi` 生成，缺一个架构那份包就跑不起来。

`pi-profile.yaml` 里的 `${MAAHJ_ROOT}` 需要环境变量，构建前 `$env:MAAHJ_ROOT='<仓库根>'`。

```powershell
.\gradlew.bat :app:assembleRelease
```

产物：`app/build/outputs/apk/release/app-release.apk`。

### 签名

Release 包默认未签名（不能安装）。用 `KEYSTORE_PATH` / `KEYSTORE_PASSWORD` / `KEY_ALIAS` / `KEY_PASSWORD`
（或同名 `local.properties` 键）指定密钥库：

```powershell
& $env:JAVA_HOME\bin\keytool.exe -genkeypair -keystore maahj-release.jks -alias maahj `
    -keyalg RSA -keysize 2048 -validity 10000 `
    -storepass <密码> -keypass <密码> -dname "CN=MaaHJ, O=MaaHJ, C=CN"
```

> 密钥库与密码**不要**提交到仓库。

从 `v0.1.5` 起 Android 包使用新的发布密钥（旧密钥口令遗失）。新旧包签名互不兼容：
从更早的包升级必须先卸载，否则安装会报 `INSTALL_FAILED_UPDATE_INCOMPATIBLE`。

## 在手机上运行

1. 安装 APK，装 [Shizuku](https://shizuku.rikka.app/) 或 root，并在应用内完成授权
2. 授予通知、忽略电池优化（前台服务保活）、悬浮窗权限
3. **把屏幕逻辑分辨率设成 720×1280 @ 320dpi**（关键）：
   ```bash
   wm size 720x1280
   wm density 320
   ```
   恢复：`wm size reset` + `wm density reset`
4. 首次打开会解包 `pi.zip` 与 `bundle.zip`；资源页确认应用名与版本
5. **显示模式选「前台模式」**，游戏保持竖屏在前台
6. 任务需要在运行配置里点「添加任务」勾选（PI 未设 `default_check`，新建配置默认全不勾）

### 为什么必须是 720×1280

MaaFwApp 的前台模式按手机真实分辨率截图（`MaaRunner.buildControllerConfig` 只传 `screen_resolution`），
不会应用 PI 里 `controller[].display_short_side`。本项目 Pipeline 的 ROI 与模板图都按竖屏
**720 短边**标定，屏幕宽度不是 720 或纵向比例不是 16:9（如 720×1600）时识别会整体错位。

## 已知限制

- agent 的 `maafw` Python 绑定由 [MaaAgentCoreAndroid](https://github.com/Aliothmoon/MaaAgentCoreAndroid)
  决定（当前 5.13.0），与 `requirements.txt` 里的版本无关；移植前请确认 agent 未使用更新的 API
- 默认双 ABI 通用包约 128 MB；只出真机包用 `-Abi arm64-v8a`（体积减半），`build.debugAbi` / `build.releaseAbi` 亦可直接调
- 重装且 versionCode 未变时，设备可能复用旧解包；改完资源请清应用数据或「重新解压资源」
- 后台模式（虚拟屏）档位已在 `pi-profile.yaml` 配好 720P 竖屏；运行时仍默认前台模式，两者截图来源不同
- `overrides/` 是整文件覆盖而非合并：上游改了同名文件时，需要人工比对后重新生成 `patches/`

## 目录说明

| 路径 | 作用 |
| --- | --- |
| `Android/pi-profile.yaml` | 打包配方（资源白名单、agent 运行时、包名/图标/显示模式） |
| `Android/build.ps1` | 一键构建 |
| `Android/overrides/` | 对 MaaFwApp 源码的改动（整文件，构建时覆盖） |
| `Android/patches/` | 上述改动的 diff（相对上游 `bc55dac`），供审阅与升级 |
| `Android/maahj-config-android.json` | 手机端导出的一份运行配置（任务/选项/设置），可导入参考 |
| `Android/logo.png` | APK 启动图标 |
| `.build/`（不入库） | MaaFwApp 源码、agent 运行时、Gradle 中间产物 |

### 为什么仓库里没有整份 MaaFwApp 源码

MaaFwApp 是上游开源工程，`build.ps1` 会 clone 到 `.build/MaaFwApp`；本项目对它的全部改动都在
`overrides/`（完整文件）与 `patches/`（diff）里，构建时覆盖上去即可复现 APK。整份拷贝既
体积大、又会和上游版本脱节，所以不入库。

本机构建目录（例如 `_apk-build/`，不入库）里除了 MaaFwApp checkout 还有：`android-sdk/`、
`agent-dist/`（各 ABI 的 CPython 运行时）、签名密钥库与 `gradle-build*.log`。密钥库与口令
只放本机，仓库里只记录出包方式。
