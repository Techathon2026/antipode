@echo off
chcp 65001 >nul
setlocal

rem Antipode try-it launcher.
rem English-only on purpose: cmd mis-reads a batch file whose code page
rem changes mid-file, so every Chinese character comes from Python.
rem The 1-5 menu lives HERE, not in Python: when you double-click,
rem cmd always has a real console, so the prompt can never hit EOF.

cd /d "%~dp0"

set "PY="
if exist "C:\Users\1\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PY=C:\Users\1\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if defined PY goto havepy

where python >nul 2>nul
if errorlevel 1 goto nopython
set "PY=python"

:havepy
if not "%~1"=="" goto runargs

echo.
echo    Antipode  -  read your own account into a vault
echo    ------------------------------------------------
echo      1. Douyin          2. Weibo        3. Bilibili
echo      4. Zhihu           5. Xiaohongshu
echo.
set "PICK="
set /p "PICK=  Choose 1-5, press Enter for Douyin: "

set "PLAT=douyin"
if "%PICK%"=="2" set "PLAT=weibo"
if "%PICK%"=="3" set "PLAT=bilibili"
if "%PICK%"=="4" set "PLAT=zhihu"
if "%PICK%"=="5" set "PLAT=xiaohongshu"

"%PY%" "%~dp0code\try_me.py" --platform %PLAT%
goto done

:runargs
"%PY%" "%~dp0code\try_me.py" %*

:done
echo.
echo    Log file: %~dp0.try_me.log
echo    Press any key to close this window.
pause >nul
exit /b 0

:nopython
echo.
echo    Python not found.
echo    Install Python 3.10+, or put the full path of python.exe into PY.
echo.
pause
exit /b 1
