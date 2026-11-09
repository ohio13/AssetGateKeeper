@echo off
rem  Short wrapper for the gate:  tools\agk.bat <path> [gate options...]
rem  Uses the same interpreter search as start_workstation.bat.
setlocal
set "PYEXE="
if exist "%~dp0..\.venv\Scripts\python.exe" set "PYEXE=%~dp0..\.venv\Scripts\python.exe"
if not defined PYEXE for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
if not defined PYEXE for %%I in (python.exe) do set "PYEXE=%%~$PATH:I"
if not defined PYEXE goto :nopython
"%PYEXE%" "%~dp0..\asset_gatekeeper.py" %*
exit /b %ERRORLEVEL%

:nopython
echo No Python found - see start_workstation.bat for the venv setup.
exit /b 1
