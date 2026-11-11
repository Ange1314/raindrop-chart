@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Raindrop Chart - 连接 AI

set "PY=python"
where python >nul 2>nul
if not errorlevel 1 goto :run

set "PY=py"
where py >nul 2>nul
if not errorlevel 1 goto :run

echo.
echo   [错误] 没找到 Python。
echo.
echo   这个脚本要给 AI 客户端写一条配置，得用 Python 来改 JSON。
echo   先装 Python 3.8+：https://www.python.org/downloads/
echo   安装时记得勾上 "Add python.exe to PATH"。
echo.
pause
exit /b 1

:run
cls
echo.
echo   正在把这一份雨滴图注册成 AI 能读的数据源……
"%PY%" "app\register_mcp.py"
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo   [没完成] 上面有原因。看图不受影响，可以不管它。
if "%RC%"=="0" echo   [完成] 按上面三步走一遍就有工具了。
echo.
pause >nul
