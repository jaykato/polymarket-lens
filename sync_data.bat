@echo off
setlocal
cd /d "%~dp0"

rem Find Python. "py" ships with the official Windows installer from python.org.
set "PYTHON_EXE="
where py >nul 2>nul
if not errorlevel 1 set "PYTHON_EXE=py -3"
if defined PYTHON_EXE goto :found
where python >nul 2>nul
if not errorlevel 1 set "PYTHON_EXE=python"
if defined PYTHON_EXE goto :found

echo.
echo Python was not found on this computer.
echo Install Python 3.10 or newer from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup.
echo.
pause
exit /b 1

:found
%PYTHON_EXE% -m polymarket_app sync --markets 50
pause
