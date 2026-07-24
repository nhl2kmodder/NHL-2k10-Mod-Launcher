@echo off
setlocal enabledelayedexpansion

echo ========================================
echo   NHL 2K10 Mod Launcher - Auto Update
echo ========================================
echo.

:: 1. Pull the latest changes from the current Git branch
echo [1/2] Pulling latest changes from Git...
git pull
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Git pull failed. Please check your internet connection or git setup.
    pause
    exit /b %errorlevel%
)

echo.
echo ========================================
echo   Building Executable
echo ========================================
echo.

:: 2. Navigate to the app folder and run the build script
echo [2/2] Running app\rebuild_exe.bat...
cd /d "%~dp0app"

if not exist "rebuild_exe.bat" (
    echo [ERROR] Could not find rebuild_exe.bat in the app directory.
    pause
    exit /b 1
)

call rebuild_exe.bat

:: Return to the repository root directory
cd /d "%~dp0"

echo.
echo ========================================
echo   Process Complete!
echo ========================================
echo.
pause