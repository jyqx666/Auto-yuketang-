@echo off
chcp 65001 >nul
cd /d "%~dp0"
python yuketang.py --site xuetangx %*
pause
