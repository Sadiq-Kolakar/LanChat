@echo off
REM Builds dist\LANchat\LANchat.exe (no console window). Needs internet once, to install PyInstaller.
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% -m pip install -r requirements-dev.txt || goto :fail
%PY% -m PyInstaller --noconfirm --clean --windowed --name LANchat --collect-all sv_ttk main.py || goto :fail
echo.
echo Built: %~dp0dist\LANchat\LANchat.exe
echo Copy the whole dist\LANchat folder to other PCs. First run: allow it in Windows Firewall (Private network).
pause & exit /b 0
:fail
echo Build failed.
pause & exit /b 1
