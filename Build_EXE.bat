@echo off
title Building FastShare.exe
cd /d "%~dp0"
where py >nul 2>nul || (echo Python is not installed. Get it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^) & pause & exit /b)
echo Installing PyInstaller...
py -m pip install --upgrade pyinstaller
echo Building...
py -m PyInstaller --noconfirm --onefile --windowed --name FastShare FastShare.py
if exist dist\FastShare.exe (
  copy /y dist\FastShare.exe "%~dp0FastShare.exe" >nul
  echo.
  echo  DONE!  FastShare.exe is in this folder. Copy it to the other PC too.
) else (echo Build failed - see messages above.)
pause
