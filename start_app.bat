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
goto :nopython

:found
if not exist "data\polymarket.db" (
  echo First run: downloading public market data...
  %PYTHON_EXE% -m polymarket_app sync --markets 25
  if errorlevel 1 goto :syncfailed
)

start "" "http://127.0.0.1:8765"
%PYTHON_EXE% -m polymarket_app serve
goto :eof

:nopython
echo.
echo Python was not found on this computer.
echo Install Python 3.10 or newer from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup.
echo.
pause
exit /b 1

:syncfailed
echo.
echo The first data sync failed. Check your internet connection and try again.
echo.
pause
exit /b 1
