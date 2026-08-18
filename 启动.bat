@echo off
setlocal
cd /d "%~dp0"
title 个股估值分位查询工具
chcp 65001 >nul 2>nul

echo ============================================
echo   个股 PE/PB 历史分位查询工具  启动中...
echo ============================================
echo.

rem 依次尝试 Python 启动器和常见命令，并要求 Python 3.10+
set PYCMD=
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYCMD=py -3"
)
if not defined PYCMD (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
        if not errorlevel 1 set "PYCMD=python"
    )
)
if not defined PYCMD (
    where python3 >nul 2>nul
    if not errorlevel 1 (
        python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
        if not errorlevel 1 set "PYCMD=python3"
    )
)

if not defined PYCMD (
    echo [错误] 没有找到 Python 3.10 或更高版本。
    echo.
    echo 请从 https://www.python.org/downloads/ 安装 Python。
    echo 安装时请勾选 "Add Python to PATH"，完成后重新双击本文件。
    echo.
    goto end
)

echo 使用的 Python 命令: %PYCMD%
%PYCMD% --version
echo.

rem 确认可选组件（Excel 导出、筹码备用源、行业资金流签名），没有就装
%PYCMD% -c "import openpyxl, baostock, py_mini_racer" 1>nul 2>nul
if errorlevel 1 (
    echo 首次运行，正在安装项目组件，请稍候...
    %PYCMD% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [错误] 项目组件安装失败，请检查网络后重试。
        goto end
    )
    echo.
)

set "APP_BROWSER=edge"
echo 正在启动服务，Edge 会自动打开 http://localhost:8688
echo 用完后直接关闭本窗口即可。
echo --------------------------------------------
%PYCMD% app.py

echo.
echo [程序已退出] 如有报错，请保留本窗口内容以便排查。

:end
echo.
pause
