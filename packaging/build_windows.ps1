# Build the Windows package for Narration Studio.
#
#   pwsh ./packaging/build_windows.ps1     ->  dist/NarrationStudio-Windows.zip
#
# Same shape as the macOS bundle: the application source plus a launcher that
# creates the Python runtime on first run. Bundling PyTorch would push the
# download past 2 GB and past GitHub's release limits.

$ErrorActionPreference = "Stop"

$Root    = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Dist    = Join-Path $Root "dist"
$Staging = Join-Path $Dist  "NarrationStudio"

Write-Host ""
Write-Host "Building Narration Studio for Windows"
Write-Host "-------------------------------------"
Write-Host ""

if (Test-Path $Staging) { Remove-Item $Staging -Recurse -Force }
New-Item -ItemType Directory -Path $Staging -Force | Out-Null

# --- application source -------------------------------------------------
Write-Host "  Copying application source..."
Copy-Item (Join-Path $Root "app") (Join-Path $Staging "app") -Recurse
Get-ChildItem $Staging -Include "__pycache__" -Recurse -Directory |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

foreach ($file in @("requirements.txt", "generate_natural_tts.py", "README.md")) {
    $source = Join-Path $Root $file
    if (Test-Path $source) { Copy-Item $source $Staging }
}

# --- launcher -----------------------------------------------------------
Copy-Item (Join-Path $Root "packaging\windows\NarrationStudio.bat") $Staging
Write-Host "  Launcher copied"

# --- a short read-me next to the launcher -------------------------------
@"
Narration Studio
================

Run NarrationStudio.bat to start.

The first launch creates a Python runtime and downloads the local speech
engine (about 2 GB). It takes a few minutes and happens only once.

Requirements
------------
* Python 3.12 or newer   https://www.python.org/downloads/
  Tick "Add python.exe to PATH" during installation.

That is the only thing you need. Everything else, including the audio
components, is installed by the app on first launch.

Everything runs on your own machine. Nothing is uploaded.
"@ | Set-Content (Join-Path $Staging "READ ME FIRST.txt") -Encoding UTF8

# --- icon ---------------------------------------------------------------
$IconDir  = Join-Path $Dist "icon"
$IconFile = Join-Path $IconDir "AppIcon.ico"
if (-not (Test-Path $IconFile)) {
    Write-Host "  Drawing the icon..."
    $Py = Get-Command python -ErrorAction SilentlyContinue
    if ($Py) {
        python -m pip install --quiet PySide6==6.11.1 2>$null
        python (Join-Path $Root "packaging\make_icon.py") $IconDir ico 2>$null | Out-Null
    }
}
if (Test-Path $IconFile) { Write-Host "  Icon ready" }

# --- portable zip -------------------------------------------------------
$Zip = Join-Path $Dist "NarrationStudio-Windows.zip"
if (Test-Path $Zip) { Remove-Item $Zip -Force }
Compress-Archive -Path $Staging -DestinationPath $Zip -CompressionLevel Optimal
Write-Host ("  Portable zip: {0:N1} MB" -f ((Get-Item $Zip).Length / 1MB))

# --- installer ----------------------------------------------------------
# Inno Setup ships on the GitHub Windows runners; install it when missing so
# the script also works on a developer machine.
$ISCC = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $ISCC) {
    Write-Host "  Inno Setup not found, installing..."
    choco install innosetup -y --no-progress 2>&1 | Out-Null
    $ISCC = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
}

if ($ISCC) {
    Write-Host "  Building the installer..."
    $Version = "0.1.0"
    $PyProject = Join-Path $Root "pyproject.toml"
    if (Test-Path $PyProject) {
        $Match = Select-String -Path $PyProject -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1
        if ($Match) { $Version = $Match.Matches[0].Groups[1].Value }
    }
    & $ISCC `
        "/DAppVersion=$Version" `
        "/DSourceDir=$Staging" `
        "/DOutputDir=$Dist" `
        "/DIconFile=$IconFile" `
        (Join-Path $Root "packaging\windows\installer.iss") | Out-Null
    $Setup = Join-Path $Dist "NarrationStudio-Setup.exe"
    if (Test-Path $Setup) {
        Write-Host ("  Installer:    {0:N1} MB" -f ((Get-Item $Setup).Length / 1MB))
    } else {
        Write-Warning "  Inno Setup ran but produced no installer."
    }
} else {
    Write-Warning "  Inno Setup unavailable - shipping the portable zip only."
}

Write-Host ""
Write-Host "  Done: $Dist"
Write-Host ""
