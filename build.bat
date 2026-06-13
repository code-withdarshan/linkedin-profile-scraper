@echo off
REM Build LinkedInScraper.exe — run this from the project root.
REM Result: dist\LinkedInScraper.exe  (single file, ~40 MB, no Python needed on target machine)

cd /d "%~dp0"

REM Sanity check: Python must be on PATH.
where python >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python is not installed or not on PATH.
  echo Install Python 3.10+ from https://www.python.org/downloads/
  echo Tick "Add Python to PATH" during installation.
  pause
  exit /b 1
)

REM Activate the local venv if present, otherwise warn but continue.
if exist ".venv\Scripts\activate.bat" (
  echo === Activating virtual environment ===
  call ".venv\Scripts\activate.bat"
) else (
  echo WARNING: .venv folder not found. Using system Python.
  echo If the build fails, run these first:
  echo     python -m venv .venv
  echo     .venv\Scripts\activate
  echo     pip install -r requirements.txt
  echo     python -m playwright install chromium
  echo.
)

echo === Installing project dependencies ===
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo ERROR: dependency install failed.
  pause
  exit /b 1
)

echo === Verifying openai is importable ===
python -c "import openai; import httpx; import pydantic; import certifi; print('openai version:', openai.__version__)"
if errorlevel 1 (
  echo ERROR: One of the AI dependencies cannot be imported by this Python.
  echo The pyinstaller build will fail. Make sure the venv is active.
  pause
  exit /b 1
)

echo === Killing any running LinkedInScraper.exe ===
taskkill /F /IM LinkedInScraper.exe >nul 2>nul

echo === Cleaning previous build ===
REM Give OneDrive / antivirus a moment to release file handles before deletion.
timeout /t 2 /nobreak >nul
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist LinkedInScraper.spec del /q LinkedInScraper.spec

REM If dist is still there, the lock is stubborn — usually OneDrive sync.
if exist dist (
  echo.
  echo WARNING: dist folder could not be deleted. This is almost always
  echo OneDrive sync holding a lock on the .exe. Fixes:
  echo   1. Right-click the OneDrive icon in the tray, choose "Pause syncing"
  echo   2. Re-run package.bat
  echo   3. Resume OneDrive after the build finishes
  echo.
  pause
  exit /b 1
)

echo === Building executable ===
REM We use Playwright's Python API to talk CDP to the user's installed Chrome.
REM No browsers need to be bundled - just the Python package itself.
pyinstaller ^
  --onefile ^
  --name LinkedInScraper ^
  --add-data "templates;templates" ^
  --collect-submodules playwright ^
  --hidden-import flask ^
  --hidden-import scraper ^
  --hidden-import app ^
  --hidden-import database ^
  --hidden-import ai ^
  --collect-all openai ^
  --collect-all httpx ^
  --collect-all httpcore ^
  --collect-all pydantic ^
  --collect-all certifi ^
  --collect-all tiktoken ^
  --icon NONE ^
  launcher.py

if not exist dist\LinkedInScraper.exe (
  echo.
  echo ============================================
  echo  BUILD FAILED - scroll up to see the error.
  echo ============================================
  pause
  exit /b 1
)

echo.
echo === Build complete ===
echo Output: dist\LinkedInScraper.exe
echo.
echo To distribute: send dist\LinkedInScraper.exe to anyone with Windows 10+.
echo On first launch the app downloads Chromium (~170 MB, one-time).
echo.
pause
