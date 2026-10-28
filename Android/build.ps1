<#
.SYNOPSIS
    构建 MaaHJ 的 Android APK（MaaFwApp + 本项目资源 + Python agent 运行时）。

.DESCRIPTION
    步骤与 MaaFwApp/INTEGRATION.md 一致：
      1. 克隆/更新 MaaFwApp
      2. 用 overrides/ 覆盖 MaaFwApp 的源码文件（竖屏支持等改动）
      3. setup_maa_framework.py 铺设 MaaFramework 的 Android .so
         —— tag 必须与 MaaAgentCoreAndroid 提供的 maafw Python 版本一致，
            否则 agent 侧调用原生库会失败（当前 core 是 maafw 5.13.0 → v5.13.0）
      4. build_agent_bundle.py 组 Python agent 运行时（CPython + 项目依赖）
      5. 写 local.properties（sdk.dir / pi.profile / ABI / 版本号）
      6. gradlew :app:assembleRelease

.PARAMETER Version
    写进 APK 的 versionName，默认 0.1.8。

.PARAMETER MaaFwAppDir
    MaaFwApp 的 checkout 目录，默认 ./.build/MaaFwApp。

.PARAMETER Abi
    进包的 ABI 清单，默认 arm64-v8a 与 x86_64（出一个通用包）。只写一个时按该架构出包。

.EXAMPLE
    $env:JAVA_HOME='C:\Program Files\Eclipse Adoptium\jdk-17.0.20.101-hotspot'
    $env:ANDROID_HOME='D:\Android\Sdk'
    .\Android\build.ps1 -Version 0.1.8
#>
[CmdletBinding()]
param(
    [string]$Version = '0.1.8',
    [string]$MaaFwAppDir = (Join-Path $PSScriptRoot '.build/MaaFwApp'),
    [string]$MaaFrameworkTag = 'v5.13.0',
    # 进包的 ABI：默认双架构通用包（一个 APK 内同时带 arm64-v8a 与 x86_64 的 .so 与 agent）
    [ValidateSet('arm64-v8a', 'x86_64')]
    [string[]]$Abi = @('arm64-v8a', 'x86_64'),
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

# 1b. 本项目对 MaaFwApp 源码的改动（竖屏支持），按上游相对路径整文件覆盖
#     改动明细见 patches/*.patch，覆盖内容见 overrides/
$overrides = Join-Path $PSScriptRoot 'overrides'
$MaaFwAppDir = (Resolve-Path $MaaFwAppDir).Path
if (Test-Path $overrides) {
    Get-ChildItem -Recurse -File $overrides | ForEach-Object {
        $rel = $_.FullName.Substring($overrides.Length + 1)
        $target = Join-Path $MaaFwAppDir $rel
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Copy-Item $_.FullName $target -Force
        Write-Host "override $rel"
    }
}
Push-Location $MaaFwAppDir

# 2. MaaFramework Android 产物（.so 进 app/src/main/jniLibs）
$abiArgs = @('--abi', 'all')
if ($Abi.Count -eq 1) { $abiArgs = @('--abi', $Abi[0]) }
python scripts/setup_maa_framework.py --tag $MaaFrameworkTag @abiArgs

# 3. Python agent 运行时：每个 ABI 一份，缺哪个架构哪个架构的包就跑不起来
$agentOut = Join-Path $repoRoot '.build/agent-dist'
$bundleArgs = @('--out', $agentOut, '--requirements', (Join-Path $repoRoot 'requirements.txt'), '--no-deps')
foreach ($a in $Abi) { $bundleArgs += @('--abi', $a) }
python scripts/build_agent_bundle.py @bundleArgs

# 4. local.properties
$sdk = ($env:ANDROID_HOME -replace '\\', '/')
$profile = ((Join-Path $PSScriptRoot 'pi-profile.yaml') -replace '\\', '/')
@(
    "sdk.dir=$sdk"
    "pi.profile=$profile"
    # 逗号分隔；多个 ABI 时 apk 只有一个通用包，MAFW_PACKAGE_ABI 记为 universal
    "build.releaseAbi=$($Abi -join ',')"
    "build.versionName=$Version"
) | Out-File -FilePath local.properties -Encoding ascii

# 5. 出包（release 需要签名，未配置 KEYSTORE_PATH 时打出未签名包）
$gradleArgs = @(':app:assembleRelease', '--console=plain')
if ($env:KEYSTORE_PATH) { $gradleArgs += '--no-daemon' }
& (Join-Path $MaaFwAppDir 'gradlew.bat') @gradleArgs
if ($LASTEXITCODE -ne 0) { throw "gradle 失败：$LASTEXITCODE" }

$apk = Join-Path $MaaFwAppDir 'app/build/outputs/apk/release/app-release.apk'
$abiLabel = if ($Abi.Count -eq 1) { $Abi[0] } else { 'android' }
$out = "MaaHJ-v$Version-$abiLabel.apk"
Copy-Item $apk (Join-Path $repoRoot $out) -Force
Write-Host "APK -> $out（$($Abi -join ', ')）"
Pop-Location
