@echo off
REM Moxa Upload Script - Automated Setup for Windows
REM This script installs all dependencies needed to run the Moxa GUI and CLI

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ========================================
echo   Cortex Upload Script - Setup
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH
    echo.
    echo Please install Python 3.8 or later from:
    echo   https://www.python.org/downloads/
    echo.
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo [OK] Python is installed
python --version
echo.

REM Check Python version (3.8+)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 3.8 or later is required
    pause
    exit /b 1
)

echo.
echo [*] Installing dependencies from requirements.txt...
echo.

python -m pip install --upgrade pip setuptools
if errorlevel 1 (
    echo [ERROR] Failed to upgrade pip
    pause
    exit /b 1
)

python -m pip install -r requirements.txt --no-cache-dir
if errorlevel 1 (
    echo.
    echo [!] Installation had issues. Attempting with user-level installation and no cache...
    echo.
    python -m pip install --user -r requirements.txt --no-cache-dir
    if errorlevel 1 (
        echo [!] Some packages failed, but this may be OK - checking if core packages are available...
        python -c "import PySide6; print('[OK] PySide6 core is installed')" >nul 2>&1
        if errorlevel 1 (
            echo [ERROR] Failed to install dependencies
            echo.
            echo Try running with administrator privileges:
            echo   1. Right-click cmd.exe
            echo   2. Select "Run as administrator"
            echo   3. Navigate to this folder and run setup.bat
            echo.
            pause
            exit /b 1
        ) else (
            echo [OK] Core packages are available. Setup will continue.
        )
    )
)

echo.
echo ========================================
echo   Setup Complete!
echo ========================================
echo.
echo You can now run:
echo.
echo   CLI Mode (upload a device):
echo     python upload_cortex.py DEVICE_NAME [options]
echo.
echo   GUI Mode (interactive interface):
echo     python cortex_gui.py
echo.
echo   For help:
echo     python upload_cortex.py --help
echo.
echo Example devices from deviceList.csv:
echo   python upload_cortex.py 4C33-R09C-Sec1
echo   python upload_cortex.py 4C33-R09C-Sec1 --dry-run
echo.
pause
