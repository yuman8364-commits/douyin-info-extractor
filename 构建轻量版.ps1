$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$appVersion = "1.0.1"
$lightDir = Join-Path $PSScriptRoot "轻量版"
$specFile = Join-Path $lightDir "抖音信息提取工具-轻量版.spec"
$venvPython = Join-Path $PSScriptRoot ".build-venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    $python = $null
    $pythonCandidates = @(
        @(Get-Command python.exe -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
        (Join-Path $env:LOCALAPPDATA "hermes\hermes-agent\venv\Scripts\python.exe")
    ) | Select-Object -Unique
    foreach ($candidate in $pythonCandidates) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        if ($candidate -like "*\WindowsApps\python.exe") { continue }
        $python = $candidate
        break
    }
    if (-not $python) { throw "A working Python 3 installation was not found." }
    & $python -m venv (Split-Path -Parent $venvPython)
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
        throw "Build virtual environment creation failed."
    }
}

& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot "requirements.txt") -r (Join-Path $PSScriptRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

& $venvPython -m PyInstaller --noconfirm --clean $specFile
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$releaseName = "抖音信息提取工具-轻量版"
$releaseDir = Join-Path (Join-Path $PSScriptRoot "dist") $releaseName
$releaseData = Join-Path $releaseDir "data"
New-Item -ItemType Directory -Force -Path $releaseData | Out-Null

# 构建产物不得继承开发机输入、配置或浏览器验证状态。
$configPath = Join-Path $releaseData "config.json"
$cachePath = Join-Path $releaseData "input_cache.txt"
$browserProfilePath = Join-Path $releaseData "browser_profile"
if (Test-Path -LiteralPath $browserProfilePath) {
    Remove-Item -LiteralPath $browserProfilePath -Recurse -Force
}
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($configPath, "{}", $utf8NoBom)
[System.IO.File]::WriteAllText($cachePath, "1.`n", $utf8NoBom)

$configText = Get-Content -LiteralPath $configPath -Raw
$cacheText = Get-Content -LiteralPath $cachePath -Raw
if ($configText -match 'https?://' -or $configText -match '(?i)[A-Z]:\\Users\\' -or
    $cacheText -match 'https?://' -or $cacheText.Trim() -ne "1." -or
    (Test-Path -LiteralPath $browserProfilePath)) {
    throw "Light release privacy check failed."
}

$buildTime = Get-Date -Format "yyyy-MM-dd HH:mm:ss K"
Set-Content -LiteralPath (Join-Path $releaseDir "VERSION.txt") -Value @(
    "Version: $appVersion"
    "Build time: $buildTime"
    "Scope: metadata only (title/tags/likes/collects/comments/work_id/author_id)"
) -Encoding UTF8

$executable = Join-Path $releaseDir "$releaseName.exe"
if (-not (Test-Path -LiteralPath $executable)) {
    throw "The lightweight release executable was not created."
}

# 放到桌面时只更新本轻量版自己的程序文件；已有 data、日志和输出内容不复制、不删除。
$desktopRoot = [Environment]::GetFolderPath("Desktop")
if ([string]::IsNullOrWhiteSpace($desktopRoot)) { throw "Desktop folder was not found." }
$desktopDir = Join-Path $desktopRoot $releaseName
New-Item -ItemType Directory -Force -Path $desktopDir | Out-Null

$managedExe = Join-Path $desktopDir "$releaseName.exe"
$managedInternal = Join-Path $desktopDir "_internal"
$managedVersion = Join-Path $desktopDir "VERSION.txt"
if (Test-Path -LiteralPath $managedExe) {
    $running = @(Get-Process -Name $releaseName -ErrorAction SilentlyContinue)
    if ($running.Count -gt 0) {
        throw "Please close the existing lightweight release before deployment: $managedExe"
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
if (-not (Test-Path -LiteralPath $desktopConfig)) {
    [System.IO.File]::WriteAllText($desktopConfig, "{}", $utf8NoBom)
}
if (-not (Test-Path -LiteralPath $desktopCache)) {
    [System.IO.File]::WriteAllText($desktopCache, "1.`n", $utf8NoBom)
}

$hash = (Get-FileHash -LiteralPath $managedExe -Algorithm SHA256).Hash
Write-Host "Light release created: $managedExe"
Write-Host "Light release version: $appVersion"
Write-Host "Light release SHA-256: $hash"
Write-Host "Light release privacy check: passed"
Write-Host "Existing desktop data/output content was preserved."
