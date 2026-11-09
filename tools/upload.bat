@echo off
rem  Short wrapper for the bulk uploader:  tools\upload.bat <folder> [options...]
rem  Requires the Art Workstation to be running (start_workstation.bat).
setlocal
set "PYEXE="
if exist "%~dp0..\.venv\Scripts\python.exe" set "PYEXE=%~dp0..\.venv\Scripts\python.exe"
if not defined PYEXE for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
if not defined PYEXE for %%I in (python.exe) do set "PYEXE=%%~$PATH:I"
if not defined PYEXE goto :nopython
"%PYEXE%" "%~dp0upload_assets.py" %*
exit /b %ERRORLEVEL%

:nopython
echo No Python found - see start_workstation.bat for the venv setup.
exit /b 1
