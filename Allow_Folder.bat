@echo off
rem ===========================================================================
rem  AssetGatekeeper - let the Rules & Presets page open another folder.
rem
rem  Double-click it and choose a folder, or drag a folder onto this file.
rem  The list is kept in .agk\allowed_folders.txt inside this copy, so deleting
rem  this copy removes it too. If the page is running it restarts by itself.
rem
rem  Show the list:     Allow_Folder.bat --list
rem  Take one off:      Allow_Folder.bat --remove "D:\Projects\Old"
rem ===========================================================================
setlocal
cd /d "%~dp0"
set "PYEXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
if not defined PYEXE for %%I in (python.exe) do set "PYEXE=%%~$PATH:I"
if not defined PYEXE goto :nopython

"%PYEXE%" "%~dp0tools\agk_serve.py" allow %*
set "CODE=%ERRORLEVEL%"
cd /d "%USERPROFILE%"
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
