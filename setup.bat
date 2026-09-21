@echo off
setlocal

echo === party-graph setup ===
echo.

REM --- Python version check ---
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ from https://python.org
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [OK] Python %PYVER%

REM --- Create venv ---
if not exist ".venv\Scripts\activate.bat" (
    echo [SETUP] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create venv
        exit /b 1
    )
) else (
    echo [OK] venv already exists
)

REM --- Activate ---
call ".venv\Scripts\activate.bat"

REM --- Install Python deps ---
echo [SETUP] Installing Python packages...
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
pip install --quiet -r requirements-scrape.txt
if errorlevel 1 (
    echo [ERROR] pip install failed
    exit /b 1
)
echo [OK] Python deps installed

REM --- Install Playwright Chromium ---
echo [SETUP] Installing Playwright Chromium browser...
python -m playwright install chromium
if errorlevel 1 (
    echo [ERROR] Playwright browser install failed
    exit /b 1
)
echo [OK] Playwright Chromium installed

REM --- Create output dirs ---
if not exist "scraped_events" (
    mkdir "scraped_events"
)
if not exist "data-request" (
    mkdir "data-request"
)
echo [OK] Output directories ready

echo.
echo === Setup complete ===
echo.
echo Next steps:
echo   1. Put your CSV in data-request\rsvp_details.csv
echo   2. Test with a public event:
echo        python scrape_events.py --url https://partiful.com/e/SOME_TOKEN
echo   3. Or run the full crawl (evening window):
echo        python scrape_events.py
echo.

endlocal
