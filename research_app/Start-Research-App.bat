@echo off
cd /d "%~dp0\.."

REM 确认依赖齐全：SDK 必须是本仓库源码装出来的那一个（不能是 PyPI 旧线），
REM 否则检索会因为工具名/参数不一致而失败。见 docs\INSTALL.md。
python -c "import streamlit, matplotlib, sciverse, os, sys; sys.exit(0 if os.path.abspath(sciverse.__file__).startswith(os.path.abspath('packages')) else 1)" >nul 2>&1
if errorlevel 1 (
    echo Installing the research app dependencies. This may take a few minutes...
    python -m pip install -e packages\python
    if errorlevel 1 (
        echo Failed to install the local Sciverse SDK from packages\python.
        pause
        exit /b 1
    )
    python -m pip install -r research_app\requirements.txt
    if errorlevel 1 (
        echo Dependency installation failed. Please install Python 3.10 or newer and try again.
        pause
        exit /b 1
    )
)

python -m streamlit run research_app\app.py
pause
