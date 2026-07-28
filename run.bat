@echo off
REM Start MedRAG. Double-click this file.
REM Installs anything missing the first time, then opens the tool in a browser.

cd /d "%~dp0"
echo Starting MedRAG...
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo Python is not installed on this computer.
    echo.
    echo Install it from https://www.python.org/downloads/
    echo IMPORTANT: tick "Add Python to PATH" during installation.
    echo Then run this file again.
    echo.
    pause
    exit /b 1
)

REM A local virtual environment keeps this tool's packages away from the rest
REM of the system, so installing it cannot break anything else.
if not exist ".venv" (
    echo First run - setting up. This takes a couple of minutes.
    python -m venv .venv
)

call .venv\Scripts\activate.bat

python -c "import streamlit" >nul 2>&1
if errorlevel 1 (
    echo Installing the packages MedRAG needs...
    python -m pip install --quiet --upgrade pip
    python -m pip install --quiet -r requirements.txt
)

echo.
echo MedRAG is starting. It will open in your web browser.
echo Leave this window open while you use it. Close it to stop.
echo.

REM Streamlit asks for an email on first run and blocks startup until
REM answered. Pre-answering it with a blank address skips the prompt.
if not exist "%USERPROFILE%\.streamlit" mkdir "%USERPROFILE%\.streamlit"
if not exist "%USERPROFILE%\.streamlit\credentials.toml" (
    echo [general]> "%USERPROFILE%\.streamlit\credentials.toml"
    echo email = "">> "%USERPROFILE%\.streamlit\credentials.toml"
)

streamlit run app.py --server.headless false
pause
