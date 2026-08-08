@echo off
REM Narration Studio launcher for Windows.
REM
REM First run creates the Python runtime in %LOCALAPPDATA% and installs the
REM dependencies. Every run after that starts immediately.

setlocal EnableDelayedExpansion

set "APP_DIR=%~dp0"
set "SUPPORT=%LOCALAPPDATA%\Narration Studio"
set "RUNTIME=%SUPPORT%\runtime"
set "PYTHON=%RUNTIME%\Scripts\python.exe"
set "STAMP=%RUNTIME%\.installed"

if not exist "%SUPPORT%" mkdir "%SUPPORT%"

if exist "%STAMP%" goto :launch

echo.
echo  Narration Studio - first-time setup
echo  -----------------------------------
echo  This downloads the speech engine (about 2 GB) and runs once.
echo.

REM Find a suitable Python. The py launcher is the reliable route on Windows.
set "HOST_PYTHON="
for %%V in (3.12 3.13) do (
    if not defined HOST_PYTHON (
        py -%%V -c "import sys" >nul 2>&1 && set "HOST_PYTHON=py -%%V"
    )
)
if not defined HOST_PYTHON (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1 && set "HOST_PYTHON=python"
)
if not defined HOST_PYTHON (
    echo  Python 3.12 or newer is required.
    echo.
    echo  Install it from https://www.python.org/downloads/
    echo  Tick "Add python.exe to PATH" during installation, then run this again.
    echo.
    pause
    exit /b 1
)

echo  Creating the runtime...
%HOST_PYTHON% -m venv "%RUNTIME%"
if errorlevel 1 (
    echo  Could not create the Python runtime.
    pause
    exit /b 1
)

echo  Installing dependencies. This takes a few minutes...
"%PYTHON%" -m pip install --upgrade pip --quiet
"%PYTHON%" -m pip install -r "%APP_DIR%requirements.txt"
if errorlevel 1 (
    echo.
    echo  Dependency installation failed. Check your internet connection
    echo  and run this again.
    pause
    exit /b 1
)

REM FFmpeg is a separate system dependency.
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo.
    echo  NOTE: FFmpeg was not found on your PATH.
    echo  Narration Studio needs it to fit speech to your subtitle timings.
    echo  Install it with:  winget install Gyan.FFmpeg
    echo  then restart Narration Studio.
    echo.
    pause
)

echo. > "%STAMP%"
echo  Setup complete.
echo.

:launch
if not exist "%PYTHON%" (
    if exist "%STAMP%" del "%STAMP%"
    echo  The Python runtime is missing or damaged.
    echo  Run this again to reinstall it.
    pause
    exit /b 1
)

cd /d "%APP_DIR%"
start "" "%RUNTIME%\Scripts\pythonw.exe" -m app %*
endlocal
