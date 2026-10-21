@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=
if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" set PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not defined PY set PY=python
"%PY%" server.py
if errorlevel 1 pause
