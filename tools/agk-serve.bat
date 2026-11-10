@echo off
rem ---------------------------------------------------------------------------
rem  One command for both web services.
rem
rem      tools\agk-serve.bat up            start dashboard + review screen, print URLs
rem      tools\agk-serve.bat up --open     ...and open the dashboard
rem      tools\agk-serve.bat status        what is running, and on which port
rem      tools\agk-serve.bat urls          URLs + the exact gate command
rem      tools\agk-serve.bat logs          recent server output
rem      tools\agk-serve.bat down          stop both (or double-click Stop.bat)
rem      tools\agk-serve.bat restart       stop, then start as last time
rem      tools\agk-serve.bat allow         let the project picker open another folder
rem
rem  Servers run detached with output in .agk\logs\ - no window to keep alive.
rem  Ports are chosen automatically and recorded in .agk\ports.json.
rem ---------------------------------------------------------------------------
setlocal
set "PYEXE="
if exist "%~dp0..\.venv\Scripts\python.exe" set "PYEXE=%~dp0..\.venv\Scripts\python.exe"
if not defined PYEXE for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
if not defined PYEXE for %%I in (python.exe) do set "PYEXE=%%~$PATH:I"
if not defined PYEXE for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
if not defined PYEXE goto :nopython
if "%~1"=="" goto :menu
"%PYEXE%" "%~dp0agk_serve.py" %*
exit /b %ERRORLEVEL%

:menu
rem  Double-clicked with no command: show what is running and how to use it,
rem  and wait, instead of flashing the help text and closing.
"%PYEXE%" "%~dp0agk_serve.py" status
echo.
echo   To stop everything:       double-click Stop.bat
echo   To start everything:      double-click Start AssetGatekeeper.bat
echo   To allow another folder:  double-click Allow_Folder.bat
echo.
echo   From a command prompt:    tools\agk-serve.bat up ^| down ^| restart ^| status ^| urls ^| logs
echo.
cd /d "%USERPROFILE%"
pause
exit /b 0

:nopython
echo No Python found - see tools\start_workstation.bat for the venv setup.
exit /b 1
