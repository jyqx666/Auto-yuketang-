@echo off
chcp 65001 >nul
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo 没有找到 Python。请先安装 Python 3.9 或更高版本，安装时勾选 "Add python.exe to PATH"
    pause
    exit /b 1
)

echo 正在安装依赖...
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo 安装失败，请把上面的报错信息截图反馈
    pause
    exit /b 1
)

echo.
echo 安装完成，双击 start.bat 开始挂机
pause
