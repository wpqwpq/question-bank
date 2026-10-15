@echo off
rem 题库审核打标台启动脚本（本机单人用）
cd /d "%~dp0backend"
set PYTHONPATH=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Lib\site-packages
"%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8642
