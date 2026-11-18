@echo off
rem Double-click to start MixNet on Windows.
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% --version >nul 2>nul || (
  echo Python 3 is required. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup.
  pause
  exit /b 1
)
%PY% -c "import psutil" >nul 2>nul || %PY% -m pip install --user -r requirements.txt
%PY% -m mixnet %*
pause
