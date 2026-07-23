@echo off
rem ============================================================================
rem  One-click rebuild of the NHL2K10 Mod Launcher  (ONEFILE build)
rem
rem  Produces the whole shippable app:
rem      dist\NHL2K10 Mod Launcher.exe   <- single self-contained exe
rem      dist\tools\                     <- ffmpeg + xma2encode, needed for audio
rem
rem  Ship BOTH. The exe finds tools\ next to itself (bundled_tool() checks there
rem  first), so audio import/encode works on any machine. Without tools\ everything
rem  else still works, but audio features report "ffmpeg.exe not found".
rem
rem  CLOSE THE LAUNCHER FIRST. It runs elevated (uac_admin), so a running - or
rem  CRASHED - instance keeps the exe locked and the build fails with
rem  "Access is denied". A crash leaves a parent+child pair behind that an
rem  ordinary taskkill cannot touch; this script clears them for you.
rem ============================================================================
cd /d "%~dp0"

echo Closing any running/crashed launcher (needs elevation to kill an elevated exe)...
taskkill /F /IM "NHL2K10 Mod Launcher.exe" >nul 2>&1
if errorlevel 1 (
    echo   none running, or not elevated - if the build fails with "Access is denied",
    echo   end "NHL2K10 Mod Launcher.exe" in Task Manager ^(Details tab^) and re-run.
) else (
    echo   closed.
)
rem give Windows a moment to release the file handles
ping -n 3 127.0.0.1 >nul

echo.
echo Building...
python -m PyInstaller nhl2k10_launcher.spec --noconfirm --distpath dist
if errorlevel 1 goto :failed

rem tools\ is NOT bundled into the exe: it is ~75MB (ffmpeg + dlls) and onefile
rem would unpack all of it to %%TEMP%% on every single launch. Ship it beside instead.
echo.
echo Copying tools\ next to the exe...
if exist "tools" (
    robocopy "tools" "dist\tools" /E /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 goto :failed
    echo   done.
) else (
    echo   WARNING: tools\ not found - audio import/encode will not work in the build.
)

echo.
echo ============================================================
echo  Done.  Ship the contents of dist\ :
echo     dist\NHL2K10 Mod Launcher.exe
echo     dist\tools\
echo.
echo  Run it with:  Launch NHL 2K27 Mod Launcher.lnk   (shortcut in this folder)
echo  Settings + your field labels persist in:
echo     %%APPDATA%%\NHL2K10 Mod Launcher\
echo ============================================================
goto :end

:failed
echo.
echo ************************* BUILD FAILED *************************
echo  "Access is denied"      -^> the launcher is still open/crashed.
echo                             End "NHL2K10 Mod Launcher.exe" in Task
echo                             Manager (Details tab), then re-run this.
echo  "BUILD ABORTED - required data file(s) missing"
echo                          -^> a file is missing from launcher\data\.
echo                             The spec refuses to build a broken exe;
echo                             restore it and re-run. (See resources.py
echo                             REQUIRED for the list.)
echo ****************************************************************

:end
pause
