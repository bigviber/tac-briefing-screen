<#
.SYNOPSIS
  Builds the CarIvIBtOverlay RRO (aapt2), the CarRelay privileged companion APK
  (Gradle / AGP / CMake + Oboe) and packages everything into a flashable
  systemless (Magisk) zip for the Lenovo TB-J606F.

.PARAMETER SdkRoot
  Android SDK root. Defaults to $env:ANDROID_HOME, then $env:ANDROID_SDK_ROOT.

.PARAMETER BuildToolsVersion
  Build-tools version folder to use (e.g. 34.0.0). Defaults to the newest installed.

.PARAMETER PlatformApi
  platforms/android-<N>/android.jar used to link the RRO. Default 30 (falls back to the newest).

.PARAMETER SkipRro / SkipApp
  Skip the respective build step (useful when iterating on one side).

.PARAMETER Deploy
  After packaging: adb push the zip, install it through Magisk and offer a reboot.

.EXAMPLE
  .\build_and_package.ps1
  .\build_and_package.ps1 -Deploy
  .\build_and_package.ps1 -SkipApp -BuildToolsVersion 34.0.0
#>
[CmdletBinding()]
param(
    [string]$SdkRoot = $(if ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { $env:ANDROID_SDK_ROOT }),
    [string]$BuildToolsVersion = "",
    [int]$PlatformApi = 30,
    [switch]$SkipRro,
    [switch]$SkipApp,
    [switch]$Deploy
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root        = Split-Path -Parent $MyInvocation.MyCommand.Path
$ModuleSrc   = Join-Path $Root "solution1-native-stack\magisk-module"
$RroSrc      = Join-Path $Root "solution1-native-stack\rro\CarIvIBtOverlay"
$AppDir      = Join-Path $Root "solution2-oboe-bridge-app\app"
$Dist        = Join-Path $Root "dist"
$Stage       = Join-Path $Dist "stage"
$Out         = Join-Path $Root "out"
$KeysDir     = Join-Path $Root "keys"
$Keystore    = Join-Path $KeysDir "carrelay.jks"
$KeyAlias    = "carrelay"
$KeyPass     = "carrelay-dev-key"
$ModuleProp  = Get-Content (Join-Path $ModuleSrc "module.prop")
$ModuleId    = ($ModuleProp | Where-Object { $_ -match '^id=' }) -replace '^id=', ''
$ModuleVer   = ($ModuleProp | Where-Object { $_ -match '^version=' }) -replace '^version=', ''
$ZipName     = "$ModuleId-$ModuleVer.zip"
$ZipPath     = Join-Path $Dist $ZipName

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Info($msg) { Write-Host "    $msg" -ForegroundColor Gray }
function Fail($msg) { Write-Host "!!  $msg" -ForegroundColor Red; exit 1 }

function Invoke-Tool([string]$exe, [string[]]$arguments) {
    Info "$([IO.Path]::GetFileName($exe)) $($arguments -join ' ')"
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { Fail "$([IO.Path]::GetFileName($exe)) failed with exit code $LASTEXITCODE" }
}

# ---------------------------------------------------------------------------
# 0. Toolchain discovery
# ---------------------------------------------------------------------------
Step "Resolving toolchain"
if (-not $SdkRoot -or -not (Test-Path $SdkRoot)) { Fail "Android SDK not found. Set ANDROID_HOME or pass -SdkRoot." }
Info "SDK: $SdkRoot"

$btRoot = Join-Path $SdkRoot "build-tools"
if (-not (Test-Path $btRoot)) { Fail "No build-tools under $SdkRoot" }
if (-not $BuildToolsVersion) {
    $BuildToolsVersion = (Get-ChildItem $btRoot -Directory | Sort-Object { [version]($_.Name -replace '[^\d\.].*$', '') } -Descending | Select-Object -First 1).Name
}
$bt = Join-Path $btRoot $BuildToolsVersion
if (-not (Test-Path $bt)) { Fail "build-tools $BuildToolsVersion not installed" }
$aapt2     = Join-Path $bt "aapt2.exe"
$zipalign  = Join-Path $bt "zipalign.exe"
$apksigner = Join-Path $bt "apksigner.bat"
foreach ($t in @($aapt2, $zipalign, $apksigner)) { if (-not (Test-Path $t)) { Fail "missing $t" } }
Info "build-tools: $BuildToolsVersion"

$platformJar = Join-Path $SdkRoot "platforms\android-$PlatformApi\android.jar"
if (-not (Test-Path $platformJar)) {
    $newest = Get-ChildItem (Join-Path $SdkRoot "platforms") -Directory | Sort-Object { [int]($_.Name -replace 'android-', '') } -Descending | Select-Object -First 1
    if (-not $newest) { Fail "No platforms/android-* installed" }
    $platformJar = Join-Path $newest.FullName "android.jar"
    Info "platforms/android-$PlatformApi missing, linking RRO against $($newest.Name)"
}

$javaHome = $env:JAVA_HOME
$keytool = if ($javaHome) { Join-Path $javaHome "bin\keytool.exe" } else { "keytool" }
$adb = Join-Path $SdkRoot "platform-tools\adb.exe"
if (-not (Test-Path $adb)) { $adb = "adb" }

New-Item -ItemType Directory -Force -Path $Dist, $Out, $KeysDir | Out-Null

# ---------------------------------------------------------------------------
# 1. Signing key (self-signed is sufficient for /system/priv-app + /product/overlay)
# ---------------------------------------------------------------------------
Step "Signing key"
if (-not (Test-Path $Keystore)) {
    Invoke-Tool $keytool @(
        "-genkeypair", "-v", "-keystore", $Keystore, "-storepass", $KeyPass, "-keypass", $KeyPass,
        "-alias", $KeyAlias, "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
        "-dname", "CN=Car IVI Relay, OU=tac-briefing-screen, O=dev, C=US"
    )
} else { Info "using existing $Keystore" }

$ksProps = @"
storeFile=keys/carrelay.jks
storePassword=$KeyPass
keyAlias=$KeyAlias
keyPassword=$KeyPass
"@
Set-Content -Path (Join-Path $Root "keystore.properties") -Value $ksProps -Encoding ASCII

# ---------------------------------------------------------------------------
# 2. RRO: CarIvIBtOverlay.apk (no code, aapt2 compile + link, zipalign, apksigner)
# ---------------------------------------------------------------------------
$rroApk = Join-Path $Out "CarIvIBtOverlay.apk"
if (-not $SkipRro) {
    Step "Building RRO CarIvIBtOverlay"
    $rroOut = Join-Path $Out "rro"
    if (Test-Path $rroOut) { Remove-Item $rroOut -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $rroOut | Out-Null

    $compiled = Join-Path $rroOut "compiled.zip"
    Invoke-Tool $aapt2 @("compile", "--dir", (Join-Path $RroSrc "res"), "-o", $compiled)

    $unsigned = Join-Path $rroOut "CarIvIBtOverlay-unsigned.apk"
    Invoke-Tool $aapt2 @(
        "link", "-o", $unsigned,
        "--manifest", (Join-Path $RroSrc "AndroidManifest.xml"),
        "-I", $platformJar,
        "--min-sdk-version", "30", "--target-sdk-version", "30",
        "--version-code", "1", "--version-name", "1.0",
        "--no-auto-version", "--no-resource-deduping",
        $compiled
    )

    $aligned = Join-Path $rroOut "CarIvIBtOverlay-aligned.apk"
    Invoke-Tool $zipalign @("-f", "-p", "4", $unsigned, $aligned)
    Invoke-Tool $apksigner @(
        "sign", "--ks", $Keystore, "--ks-pass", "pass:$KeyPass", "--ks-key-alias", $KeyAlias, "--key-pass", "pass:$KeyPass",
        "--min-sdk-version", "30", "--v1-signing-enabled", "true", "--v2-signing-enabled", "true",
        "--out", $rroApk, $aligned
    )
    Invoke-Tool $apksigner @("verify", "--print-certs", $rroApk)
    Info "RRO -> $rroApk"
} else { Step "Skipping RRO build" }

# ---------------------------------------------------------------------------
# 3. Companion app (Gradle): Compose UI + jOOR + libsu + Media3 + Oboe/CMake
# ---------------------------------------------------------------------------
$appApk = Join-Path $AppDir "build\outputs\apk\release\app-release.apk"
if (-not $SkipApp) {
    Step "Building CarRelay.apk (assembleRelease)"
    $gradlew = Join-Path $Root "gradlew.bat"
    if (-not (Test-Path $gradlew)) { Fail "gradlew.bat missing - run 'gradle wrapper --gradle-version 8.9' once" }
    Push-Location $Root
    try {
        & $gradlew ":solution2-oboe-bridge-app:app:assembleRelease" "--no-daemon" "--console=plain"
        if ($LASTEXITCODE -ne 0) { Fail "Gradle build failed ($LASTEXITCODE)" }
    } finally { Pop-Location }
    if (-not (Test-Path $appApk)) { Fail "expected APK not found: $appApk" }
    Invoke-Tool $apksigner @("verify", $appApk)
    Info "APP -> $appApk"
} else { Step "Skipping app build" }

# ---------------------------------------------------------------------------
# 4. Stage the Magisk module tree
# ---------------------------------------------------------------------------
Step "Staging module tree"
if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Stage | Out-Null
Copy-Item -Path (Join-Path $ModuleSrc "*") -Destination $Stage -Recurse -Force
Get-ChildItem $Stage -Recurse -Filter ".gitkeep" | Remove-Item -Force

$overlayDst = Join-Path $Stage "system\product\overlay"
$privDst    = Join-Path $Stage "system\priv-app\CarRelay"
$libDst     = Join-Path $privDst "lib\arm64"
New-Item -ItemType Directory -Force -Path $overlayDst, $privDst, $libDst | Out-Null

if (Test-Path $rroApk) { Copy-Item $rroApk (Join-Path $overlayDst "CarIvIBtOverlay.apk") -Force; Info "staged CarIvIBtOverlay.apk" }
elseif (-not $SkipRro) { Fail "RRO apk missing" } else { Info "RRO skipped - module will not carry CarIvIBtOverlay.apk" }

if (Test-Path $appApk) {
    Copy-Item $appApk (Join-Path $privDst "CarRelay.apk") -Force
    Info "staged CarRelay.apk"
    # Mirror the stock /system/app/Bluetooth layout: also expose the native libs
    # under lib/arm64 so PackageManager finds them even if someone rebuilds with
    # compressed native libraries.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($appApk)
    try {
        $libs = $zip.Entries | Where-Object { $_.FullName -like "lib/arm64-v8a/*.so" }
        foreach ($e in $libs) {
            $target = Join-Path $libDst ([IO.Path]::GetFileName($e.FullName))
            [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $target, $true)
            Info "extracted $($e.FullName)"
        }
    } finally { $zip.Dispose() }
} elseif (-not $SkipApp) { Fail "app apk missing" } else { Info "app skipped - module will not carry CarRelay.apk" }

# Normalise line endings of the shell scripts (CRLF breaks /system/bin/sh).
foreach ($sh in @("post-fs-data.sh", "service.sh", "customize.sh", "META-INF\com\google\android\update-binary")) {
    $p = Join-Path $Stage $sh
    if (Test-Path $p) {
        $text = (Get-Content -Raw $p) -replace "`r`n", "`n"
        [IO.File]::WriteAllText($p, $text, [Text.UTF8Encoding]::new($false))
    }
}

# ---------------------------------------------------------------------------
# 5. Zip with forward slashes (Compress-Archive emits backslashes on Windows
#    PowerShell, which Magisk's installer cannot read).
# ---------------------------------------------------------------------------
Step "Packaging $ZipName"
if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$fs = [IO.File]::Open($ZipPath, [IO.FileMode]::CreateNew)
try {
    $archive = New-Object IO.Compression.ZipArchive($fs, [IO.Compression.ZipArchiveMode]::Create)
    try {
        $files = Get-ChildItem $Stage -Recurse -File
        foreach ($f in $files) {
            $rel = $f.FullName.Substring($Stage.Length).TrimStart('\', '/') -replace '\\', '/'
            $level = if ($rel -like "*.apk" -or $rel -like "*.so") { [IO.Compression.CompressionLevel]::NoCompression } else { [IO.Compression.CompressionLevel]::Optimal }
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $f.FullName, $rel, $level) | Out-Null
            Info $rel
        }
    } finally { $archive.Dispose() }
} finally { $fs.Dispose() }

$sha = (Get-FileHash $ZipPath -Algorithm SHA256).Hash
Write-Host "`nModule: $ZipPath" -ForegroundColor Green
Write-Host "SHA256: $sha" -ForegroundColor Green

# ---------------------------------------------------------------------------
# 6. Optional deploy via adb + Magisk
# ---------------------------------------------------------------------------
if ($Deploy) {
    Step "Deploying to device"
    Invoke-Tool $adb @("wait-for-device")
    $remote = "/sdcard/Download/$ZipName"
    Invoke-Tool $adb @("push", $ZipPath, $remote)
    Invoke-Tool $adb @("shell", "su", "-c", "magisk --install-module $remote")
    Write-Host "`nInstalled. Reboot now? [y/N] " -NoNewline -ForegroundColor Yellow
    $answer = Read-Host
    if ($answer -match '^[Yy]') { & $adb reboot }
    else { Info "Reboot later with: adb reboot" }
}

Write-Host "`nDone." -ForegroundColor Green
