# MaaHJ Android 客户端

把本项目（Pipeline + 资源 + Python agent）打进一个 Android APK，基于
[Aliothmoon/MaaFwApp](https://github.com/Aliothmoon/MaaFwApp)（MaaFramework 的 Android 通用 GUI）。

资源在**构建期**打进 APK：换资源就要重新出包，手机上不需要另外拷贝任何文件。

## 产物

- 包名：`com.aliothmoon.maafw.maahj`
- 应用名：哼唧小助手
- ABI：`arm64-v8a`（真机；x86_64 模拟器不适用）
- `minSdk 28` / `targetSdk 36`
- 包内结构：`assets/pi.zip`（interface.json + tasks + resource + agent）、`assets/agent/bundle.zip`（CPython 3.13 + maa + loguru + numpy）、`lib/arm64-v8a/*.so`（MaaFramework + onnxruntime + opencv）

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
.\Android\build.ps1 -Version 0.1.5
```

脚本依次做：克隆 MaaFwApp → 铺 MaaFramework `.so` → 组 agent 运行时 → 写 `local.properties` → `assembleRelease`。

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
build.releaseAbi=arm64-v8a
build.versionName=0.1.5
```

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
- 只出 `arm64-v8a`；`build.debugAbi` / `build.releaseAbi` 可调整
- 重装且 versionCode 未变时，设备可能复用旧解包；改完资源请清应用数据或「重新解压资源」
- 后台模式（虚拟屏）只接受横屏档位，本项目不使用

## 目录说明

| 路径 | 作用 |
| --- | --- |
| `Android/pi-profile.yaml` | 打包配方（资源白名单、agent 运行时、包名/图标/显示模式） |
| `Android/build.ps1` | 一键构建 |
| `Android/logo.png` | APK 启动图标 |
| `.build/`（不入库） | MaaFwApp 源码、agent 运行时、Gradle 中间产物 |
