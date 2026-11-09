@echo off
rem ===========================================================================
rem  AssetGatekeeper - double-click entry point.  No command line required.
rem
rem  Starts the admin page and the 3D review screen as detached background
rem  services (logs in .agk\logs, ports in .agk\ports.json, automatic port
rem  fallback), then opens your browser on the admin dashboard.
rem
rem  Clicking it again while they are running just re-opens the page.
rem  Stop them with Stop.bat (double-click).
rem
rem  To let the project picker reach folders outside this copy, double-click
rem  Allow_Folder.bat and choose the folder. Studios can also set once:
rem      setx AGK_PROJECT_ROOTS "D:\Projects;C:\Users\you\Documents\Game"
rem  or pass them here:   Rules.bat --allow-root "D:\Projects"
rem ===========================================================================
setlocal
cd /d "%~dp0"

set "PYEXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
if not defined PYEXE for %%I in (python.exe) do set "PYEXE=%%~$PATH:I"
if not defined PYEXE for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"

if not defined PYEXE goto :nopython

"%PYEXE%" -c "import trimesh, PIL" >nul 2>&1
if errorlevel 1 goto :nodeps

echo Starting AssetGatekeeper...
echo.
rem  "%~dp0." and not "%~dp0": a trailing backslash escapes the closing quote on
rem  the Windows command line and silently swallows every later argument.
"%PYEXE%" "%~dp0tools\agk_serve.py" up --open admin --allow-root "%~dp0." %*
set "CODE=%ERRORLEVEL%"
echo.
if not "%CODE%"=="0" echo Something went wrong (exit %CODE%). Details: .agk\logs\
if "%CODE%"=="0" echo You can close this window - the pages keep running.
echo.
echo   To stop everything:       double-click Stop.bat
echo   To allow another folder:  double-click Allow_Folder.bat
echo.
rem  Leave this folder before pausing, so this window never stops it being deleted.
cd /d "%USERPROFILE%"
pause
exit /b %CODE%

:nopython
echo.
echo   Python was not found.
echo   Install Python 3.9+ from python.org, then run this file again.
echo.
pause
exit /b 1

:nodeps
echo.
echo   Python is here, but trimesh/Pillow are missing (the gate needs both).
echo   Fix it with:
echo.
echo       "%PYEXE%" -m pip install trimesh pillow
echo.
pause
exit /b 1
