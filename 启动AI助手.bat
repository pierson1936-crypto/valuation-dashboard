@echo off
setlocal
cd /d "%~dp0"
title 估值分析 AI 助手 (DeepSeek Agent)
chcp 65001 >nul 2>nul

set PYCMD=
where py        >nul 2>nul && set PYCMD=py
if not defined PYCMD where python  >nul 2>nul && set PYCMD=python
if not defined PYCMD where python3 >nul 2>nul && set PYCMD=python3
if not defined PYCMD (
    echo [错误] 没找到 Python。请安装 Python 3 并勾选 Add to PATH。
    goto end
)

if "%DEEPSEEK_API_KEY%"=="" (
    echo ============================================================
    echo   还没有检测到 DeepSeek 的 API Key。
    echo   申请地址： https://platform.deepseek.com  ^(注册-创建API Key^)
    echo.
    echo   可以现在临时粘贴 Key（仅本次运行有效，不会保存）：
    echo   若想一劳永逸，请把它设为系统环境变量 DEEPSEEK_API_KEY。
    echo ============================================================
    set /p DEEPSEEK_API_KEY=粘贴Key后回车(直接回车则跳过):
)

%PYCMD% agent.py

:end
echo.
pause
