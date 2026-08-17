@echo off
setlocal
cd /d "%~dp0"
title 个股估值分位查询工具

echo ============================================
echo   个股 PE/PB 历史分位查询工具  启动中...
echo ============================================
echo.

rem 依次尝试 py 启动器、python、python3
set PYCMD=
where py        >nul 2>nul && set PYCMD=py
if not defined PYCMD where python  >nul 2>nul && set PYCMD=python
if not defined PYCMD where python3 >nul 2>nul && set PYCMD=python3

if not defined PYCMD (
    echo [错误] 没有找到 Python。
    echo.
    echo 你的电脑装了 Python 3.12，但它可能没有加入 PATH。
    echo 解决办法二选一：
    echo   1^) 重新安装 Python 时勾选 "Add Python to PATH"（https://www.python.org/downloads/）
    echo   2^) 或者告诉我，我教你手动运行。
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
    echo.
)

set "APP_BROWSER=edge"
echo 正在启动服务，Edge 会自动打开 http://localhost:8688
echo 用完后直接关闭本窗口即可。
echo --------------------------------------------
%PYCMD% app.py

echo.
echo [程序已退出] 如果上面有红色报错，把它截图发给我。

:end
echo.
pause
