$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$appVersion = "1.1.0"
$appName = "抖音视频批量提取工具"
$videoDir = Join-Path $PSScriptRoot "视频版"
$specFile = Join-Path $videoDir "$appName.spec"
$venvPython = Join-Path $PSScriptRoot ".build-venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Build Python was not found: $venvPython"
}

& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot "requirements.txt") -r (Join-Path $PSScriptRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

& $venvPython -m PyInstaller --noconfirm --clean $specFile
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$releaseDir = Join-Path (Join-Path $PSScriptRoot "dist") $appName
$executable = Join-Path $releaseDir "$appName.exe"
if (-not (Test-Path -LiteralPath $executable)) {
    throw "The video-only executable was not created."
}

$buildTime = Get-Date -Format "yyyy-MM-dd HH:mm:ss K"
Set-Content -LiteralPath (Join-Path $releaseDir "VERSION.txt") -Value @(
    "Version: $appVersion"
    "Build time: $buildTime"
    "Scope: batch video files only; no workbook, captions, covers, or image posts"
) -Encoding UTF8

$desktopRoot = [Environment]::GetFolderPath("Desktop")
if ([string]::IsNullOrWhiteSpace($desktopRoot)) { throw "Desktop folder was not found." }
$desktopDir = Join-Path $desktopRoot $appName
New-Item -ItemType Directory -Force -Path $desktopDir | Out-Null

$managedExe = Join-Path $desktopDir "$appName.exe"
$managedInternal = Join-Path $desktopDir "_internal"
$managedVersion = Join-Path $desktopDir "VERSION.txt"
$resolvedDesktopDir = [System.IO.Path]::GetFullPath($desktopDir).TrimEnd('\')
$resolvedInternal = [System.IO.Path]::GetFullPath($managedInternal)
if (-not $resolvedInternal.StartsWith($resolvedDesktopDir + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to replace files outside the video-only app folder: $resolvedInternal"
}
if (Test-Path -LiteralPath $managedExe) {
    $running = @(Get-Process -Name $appName -ErrorAction SilentlyContinue)
    if ($running.Count -gt 0) {
        throw "Please close the existing video-only release before deployment: $managedExe"
    }
}

if (Test-Path -LiteralPath $managedInternal) {
    Remove-Item -LiteralPath $managedInternal -Recurse -Force
}
Copy-Item -LiteralPath (Join-Path $releaseDir "_internal") -Destination $managedInternal -Recurse -Force
Copy-Item -LiteralPath $executable -Destination $managedExe -Force
Copy-Item -LiteralPath (Join-Path $releaseDir "VERSION.txt") -Destination $managedVersion -Force

$desktopData = Join-Path $desktopDir "data"
New-Item -ItemType Directory -Force -Path $desktopData | Out-Null
$desktopConfig = Join-Path $desktopData "config.json"
$desktopCache = Join-Path $desktopData "input_cache.txt"
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
if (-not (Test-Path -LiteralPath $desktopConfig)) {
    [System.IO.File]::WriteAllText($desktopConfig, "{}", $utf8NoBom)
}
if (-not (Test-Path -LiteralPath $desktopCache)) {
    [System.IO.File]::WriteAllText($desktopCache, "", $utf8NoBom)
}

$hash = (Get-FileHash -LiteralPath $managedExe -Algorithm SHA256).Hash
Write-Host "Video-only release created: $managedExe"
Write-Host "Video-only release version: $appVersion"
Write-Host "Video-only release SHA-256: $hash"
Write-Host "Existing desktop data and video output were preserved."
