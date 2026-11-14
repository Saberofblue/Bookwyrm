@echo off
rem Opens Bookwyrm. If it has not been set up on this PC yet, runs the installer first.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" goto :install
start "" ".venv\Scripts\pythonw.exe" -m bookwyrm %*
exit /b 0
:install
call "%~dp0Install Bookwyrm.bat"
