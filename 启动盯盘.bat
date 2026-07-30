@echo off
setlocal
cd /d "%~dp0"
title 自选股盯盘 Agent
chcp 65001 >nul 2>nul

set PYCMD=
where py        >nul 2>nul && set PYCMD=py
if not defined PYCMD where python  >nul 2>nul && set PYCMD=python
if not defined PYCMD where python3 >nul 2>nul && set PYCMD=python3
if not defined PYCMD (
    echo [错误] 没找到 Python。请安装 Python 3 并勾选 Add to PATH。
    goto end
)

echo ============================================================
echo   自选股盯盘 Agent
echo   分钟循环使用确定性规则，不调用大模型，不消耗 Token。
echo   按 Ctrl+C 停止。
echo ============================================================
%PYCMD% -X utf8 monitor.py watch

:end
echo.
pause
