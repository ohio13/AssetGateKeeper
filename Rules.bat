@echo off
rem  Rules.bat is now "Start AssetGatekeeper.bat" (10 Nov 2026). This file stays so
rem  old shortcuts, pinned taskbar icons and habits keep working.
call "%~dp0Start AssetGatekeeper.bat" %*
exit /b %ERRORLEVEL%
