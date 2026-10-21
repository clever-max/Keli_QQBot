@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\lifecycle\start-all.ps1"
if errorlevel 1 pause
