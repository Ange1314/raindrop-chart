@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Raindrop Chart

set "PY=python"
where python >nul 2>nul
if not errorlevel 1 goto :launch

set "PY=py"
where py >nul 2>nul
if not errorlevel 1 goto :launch

echo.
echo   [错误] 没找到 Python。
echo.
echo   请先安装 Python 3.8 或更高版本：https://www.python.org/downloads/
echo   安装时记得勾上 "Add python.exe to PATH"。
echo.
pause
exit /b 1

:launch
cls
echo.
echo   Raindrop Chart 正在启动，请稍等一下……
echo.
"%PY%" "app\live_server.py"
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" echo   [异常] 服务退出，代码 %RC%。把上面几行报错整段发给我。
if "%RC%"=="0" echo   [已停止] 下次要看图，重新双击这个脚本。
echo.
pause >nul
