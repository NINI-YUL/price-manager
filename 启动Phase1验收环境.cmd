@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [ERROR] Python environment was not found.
    echo Please open PowerShell in this folder and install the project environment first.
    pause
    exit /b 1
)

start "Phase1 Acceptance" ".venv\Scripts\pythonw.exe" -m src.acceptance
exit /b 0
