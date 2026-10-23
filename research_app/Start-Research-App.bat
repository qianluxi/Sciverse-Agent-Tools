@echo off
cd /d "%~dp0\.."

python -c "import streamlit, sciverse, matplotlib" >nul 2>&1
if errorlevel 1 (
    echo Installing the research app dependencies. This may take a few minutes...
    python -m pip install -r research_app\requirements.txt
    if errorlevel 1 (
        echo Dependency installation failed. Please install Python 3.10 or newer and try again.
        pause
        exit /b 1
    )
)

python -m streamlit run research_app\app.py
pause
