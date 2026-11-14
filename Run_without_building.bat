@echo off
cd /d "%~dp0"
py -m pip install --quiet tkinterdnd2
start "" pyw FastShare.py
