@echo off
title Building FastShare.exe
cd /d "%~dp0"
where py >nul 2>nul || (echo Python is not installed. Get it from https://www.python.org/downloads/ & pause & exit /b)
py -m pip install --upgrade pyinstaller tkinterdnd2
py -m PyInstaller --noconfirm --onefile --windowed --name FastShare --collect-all tkinterdnd2 FastShare.py
if exist dist\FastShare.exe (copy /y dist\FastShare.exe FastShare.exe >nul & echo DONE - FastShare.exe is in this folder.) else (echo Build failed.)
pause
