@echo off
REM Build the .exe and produce a shareable ZIP for non-technical Windows users.
REM Result: release\LinkedInScraper.zip  containing the .exe and a user guide.

setlocal
cd /d "%~dp0"

echo === Step 1/3: Build the .exe ===
call build.bat
if errorlevel 1 (
  echo Build failed - aborting package.
  pause
  exit /b 1
)

echo.
echo === Step 2/3: Stage release folder ===
if exist release rmdir /s /q release
mkdir release\LinkedInScraper

copy /y dist\LinkedInScraper.exe release\LinkedInScraper\ >nul
copy /y USER_GUIDE.txt release\LinkedInScraper\ >nul

echo.
echo === Step 3/3: Create ZIP ===
REM Use PowerShell's built-in Compress-Archive (available on all Windows 10+ machines).
powershell -NoProfile -Command "Compress-Archive -Path 'release\LinkedInScraper\*' -DestinationPath 'release\LinkedInScraper.zip' -Force"

if not exist release\LinkedInScraper.zip (
  echo ZIP creation failed.
  exit /b 1
)

for %%A in (release\LinkedInScraper.zip) do set ZIPSIZE=%%~zA
set /a ZIPMB=%ZIPSIZE% / 1048576

echo.
echo ========================================
echo  Done!
echo ========================================
echo.
echo  Shareable ZIP: release\LinkedInScraper.zip  (%ZIPMB% MB)
echo.
echo  Send this ZIP to anyone on Windows 10 or 11. They:
echo    1. Unzip it anywhere
echo    2. Double-click LinkedInScraper.exe
echo    3. Read USER_GUIDE.txt if they get stuck
echo.
pause

endlocal
