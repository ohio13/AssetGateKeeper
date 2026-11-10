@echo off
rem ===========================================================================
rem  AssetGatekeeper - stop everything.  Double-click it.
rem
rem  Stops the admin page / dashboard and the 3D review screen that Start AssetGatekeeper.bat
rem  started, plus any other server still running from this folder. Afterwards
rem  nothing holds this folder open, so it can be moved or deleted.
rem ===========================================================================
setlocal
cd /d "%~dp0"
set "PYEXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
if not defined PYEXE for %%I in (python.exe) do set "PYEXE=%%~$PATH:I"
if not defined PYEXE goto :nopython

echo Stopping AssetGatekeeper...
echo.
"%PYEXE%" "%~dp0tools\agk_serve.py" down
set "CODE=%ERRORLEVEL%"
rem  Leave this folder before pausing: a window whose current folder is this one
rem  is itself enough to stop Windows deleting it.
cd /d "%USERPROFILE%"
echo.
if "%CODE%"=="0" echo Everything is stopped. Close this window, and this folder can be deleted.
if not "%CODE%"=="0" echo Something could not be stopped. Open Task Manager, end the Python entries, then try again.
echo.
pause
exit /b %CODE%

:nopython
echo.
echo   Python was not found.
echo   Install Python 3.9+ from python.org, then run this file again.
echo.
pause
exit /b 1
