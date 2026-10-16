@echo off
rem Question bank tagging workbench - local single-user launcher.
rem Keep this file ASCII-only + CRLF: CJK comments break cmd under codepage 936.
cd /d "%~dp0backend"
set PYTHONPATH=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Lib\site-packages
"%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8642
