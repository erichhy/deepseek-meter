@echo off
chcp 65001 >nul
cd /d %~dp0
title DeepSeek 计量代理
python deepseek_meter.py %*
echo.
echo 代理已退出，按任意键关闭窗口
pause >nul
