$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$appVersion = "2.0.15"

$python = $null
$pythonCandidates = @(
    @(Get-Command python.exe -All -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
    (Join-Path $env:LOCALAPPDATA "hermes\hermes-agent\venv\Scripts\python.exe")
) | Select-Object -Unique
foreach ($candidate in $pythonCandidates) {
    if (-not (Test-Path -LiteralPath $candidate)) {
        continue
    }
    if ($candidate -like "*\WindowsApps\python.exe") {
        continue
    }
    $python = $candidate
    break
}
if (-not $python) {
    throw "A working Python 3 installation was not found."
}

$venvDir = Join-Path $PSScriptRoot ".build-venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $python -m venv $venvDir
    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw "Build virtual environment creation failed."
    }
}

$specFiles = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter "*.spec" -File)
if ($specFiles.Count -ne 1) {
    throw "The PyInstaller spec file was not found."
}
$specFile = $specFiles[0]

$install = Start-Process -FilePath $venvPython -ArgumentList @(
    "-m", "pip", "install", "--disable-pip-version-check",
    "-r", "requirements.txt", "-r", "requirements-build.txt"
) -NoNewWindow -Wait -PassThru
if ($install.ExitCode -ne 0) {
    throw "Dependency installation failed."
}

$build = Start-Process -FilePath $venvPython -ArgumentList @(
    "-m", "PyInstaller", "--noconfirm", "--clean", $specFile.FullName
) -NoNewWindow -Wait -PassThru
if ($build.ExitCode -ne 0) {
    throw "PyInstaller build failed."
}

$releaseDir = Join-Path (Join-Path $PSScriptRoot "dist") $specFile.BaseName
$dataDir = Join-Path $releaseDir "data"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

# Release packages must never inherit developer state or cached input.
$configPath = Join-Path $dataDir "config.json"
$cachePath = Join-Path $dataDir "input_cache.txt"
$browserProfilePath = Join-Path $dataDir "browser_profile"
if (Test-Path -LiteralPath $browserProfilePath) {
    Remove-Item -LiteralPath $browserProfilePath -Recurse -Force
}
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($configPath, "{}", $utf8NoBom)
[System.IO.File]::WriteAllText($cachePath, "1.`n", $utf8NoBom)
if (-not (Test-Path -LiteralPath $configPath) -or -not (Test-Path -LiteralPath $cachePath)) {
    throw "Release state initialization failed."
}

$configText = Get-Content -LiteralPath $configPath -Raw
$cacheText = Get-Content -LiteralPath $cachePath -Raw
if ($configText -match 'https?://' -or $configText -match '(?i)[A-Z]:\\Users\\' -or
    $cacheText -match 'https?://' -or $cacheText.Trim() -ne "1." -or
    (Test-Path -LiteralPath $browserProfilePath)) {
    throw "Release privacy check failed: data contains a URL, user path, or non-empty cache."
}

$buildTime = Get-Date -Format "yyyy-MM-dd HH:mm:ss K"
Set-Content -LiteralPath (Join-Path $releaseDir "VERSION.txt") -Value @(
    "Version: $appVersion"
    "Build time: $buildTime"
) -Encoding UTF8

$executable = Join-Path $releaseDir ($specFile.BaseName + ".exe")
if (-not (Test-Path -LiteralPath $executable)) {
    throw "The release executable was not created."
}
Write-Host "Release created: $executable"
Write-Host "Release privacy check: passed"
