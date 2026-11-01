@echo off
rem ---------------------------------------------------------------------------
rem  One command for both web services.
rem
rem      agk-serve.bat up            start dashboard + review screen, print URLs
rem      agk-serve.bat up --open     ...and open the dashboard
rem      agk-serve.bat status        what is running, and on which port
rem      agk-serve.bat urls          URLs + the exact gate command
rem      agk-serve.bat logs          recent server output
rem      agk-serve.bat down          stop both
rem
rem  Servers run detached with output in .agk\logs\ - no window to keep alive.
rem  Ports are chosen automatically and recorded in .agk\ports.json.
rem ---------------------------------------------------------------------------
setlocal
set "PYEXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
if not defined PYEXE for %%I in (python.exe) do set "PYEXE=%%~$PATH:I"
if not defined PYEXE for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
if not defined PYEXE goto :nopython
"%PYEXE%" "%~dp0tools\agk_serve.py" %*
exit /b %ERRORLEVEL%

:nopython
echo No Python found - see start_workstation.bat for the venv setup.
exit /b 1
