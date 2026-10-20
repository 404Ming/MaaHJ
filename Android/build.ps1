<#
.SYNOPSIS
    构建 MaaHJ 的 Android APK（MaaFwApp + 本项目资源 + Python agent 运行时）。

.DESCRIPTION
    步骤与 MaaFwApp/INTEGRATION.md 一致：
      1. 克隆/更新 MaaFwApp
      2. setup_maa_framework.py 铺设 MaaFramework 的 Android .so
         —— tag 必须与 MaaAgentCoreAndroid 提供的 maafw Python 版本一致，
            否则 agent 侧调用原生库会失败（当前 core 是 maafw 5.13.0 → v5.13.0）
      3. build_agent_bundle.py 组 Python agent 运行时（CPython + 项目依赖）
      4. 写 local.properties（sdk.dir / pi.profile / ABI / 版本号）
      5. gradlew :app:assembleRelease

.PARAMETER Version
    写进 APK 的 versionName，默认 0.1.5。

.PARAMETER MaaFwAppDir
    MaaFwApp 的 checkout 目录，默认 ./.build/MaaFwApp。

.EXAMPLE
    $env:JAVA_HOME='C:\Program Files\Eclipse Adoptium\jdk-17.0.20.101-hotspot'
    $env:ANDROID_HOME='D:\Android\Sdk'
    .\Android\build.ps1 -Version 0.1.5
#>
[CmdletBinding()]
param(
    [string]$Version = '0.1.5',
    [string]$MaaFwAppDir = (Join-Path $PSScriptRoot '.build/MaaFwApp'),
    [string]$MaaFrameworkTag = 'v5.13.0',
    [string]$Proxy = ''
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot          # 仓库根
$env:MAAHJ_ROOT = ($repoRoot -replace '\\', '/')

# 构建依赖检查
foreach ($tool in 'python', 'git') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "缺少 $tool" }
}
if (-not $env:JAVA_HOME) { throw '请先设置 JAVA_HOME（需要 JDK 17）' }
if (-not $env:ANDROID_HOME) { throw '请先设置 ANDROID_HOME（Android SDK）' }
if ($Proxy) {
    $env:HTTPS_PROXY = $Proxy
    $env:HTTP_PROXY = $Proxy
}

# 1. MaaFwApp
if (-not (Test-Path $MaaFwAppDir)) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $MaaFwAppDir) | Out-Null
    git clone https://github.com/Aliothmoon/MaaFwApp.git $MaaFwAppDir
}
Push-Location $MaaFwAppDir

# 2. MaaFramework Android 产物（.so 进 app/src/main/jniLibs）
python scripts/setup_maa_framework.py --tag $MaaFrameworkTag

# 3. Python agent 运行时
$agentOut = Join-Path $repoRoot '.build/agent-dist'
python scripts/build_agent_bundle.py `
    --out $agentOut `
    --requirements (Join-Path $repoRoot 'requirements.txt') `
    --no-deps

# 4. local.properties
$sdk = ($env:ANDROID_HOME -replace '\\', '/')
$profile = ((Join-Path $PSScriptRoot 'pi-profile.yaml') -replace '\\', '/')
@(
    "sdk.dir=$sdk"
    "pi.profile=$profile"
    'build.releaseAbi=arm64-v8a'
    "build.versionName=$Version"
) | Out-File -FilePath local.properties -Encoding ascii

# 5. 出包（release 需要签名，未配置 KEYSTORE_PATH 时打出未签名包）
$gradleArgs = @(':app:assembleRelease', '--console=plain')
if ($env:KEYSTORE_PATH) { $gradleArgs += '--no-daemon' }
& "$PSScriptRoot\..\Android\.build\MaaFwApp\gradlew.bat" @gradleArgs
if ($LASTEXITCODE -ne 0) { throw "gradle 失败：$LASTEXITCODE" }

$apk = Join-Path $MaaFwAppDir 'app/build/outputs/apk/release/app-release.apk'
Copy-Item $apk (Join-Path $repoRoot "MaaHJ-v$Version-arm64.apk") -Force
Write-Host "APK -> MaaHJ-v$Version-arm64.apk"
Pop-Location
