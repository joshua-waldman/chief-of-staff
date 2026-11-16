@echo off
powershell.exe -NoProfile -File "%~dp0Setup-ChiefOfStaff.ps1" %*
if errorlevel 1 pause
