@echo off
cd /d %~dp0

set PYTHON=.venv\Scripts\python.exe
%PYTHON% dashboard_app.py
