@echo off
REM Starts LANchat from its own folder. Extra arguments are passed through, e.g.
REM   start_lanchat.bat --profile bob        (second instance on the same PC)
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% -c "import tkinter" >nul 2>nul || (
  echo Tkinter is missing. Reinstall Python from python.org and tick "tcl/tk and IDLE".
  pause & exit /b 1
)
%PY% -c "import sv_ttk" >nul 2>nul || (
  echo Installing optional theme package sv-ttk ...
  %PY% -m pip install --quiet -r requirements.txt
)
%PY% main.py %*
if errorlevel 1 pause
