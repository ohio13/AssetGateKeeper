@echo off
rem ===========================================================================
rem  AssetGatekeeper - write a rules.toml by answering six questions.
rem
rem  Double-click it, then type your export folder or drag it into this window.
rem  Or drag the folder onto this file. Several engines, programs and platforms
rem  at once are fine: the answers become one rules.toml for all of them.
rem  An existing rules.toml is only replaced if you say so, and a copy is kept.
rem
rem  From a command prompt:   tools\rules_wizard.bat "D:\Projects\MyGame\Assets"
rem ===========================================================================
setlocal
set "PYEXE="
if exist "%~dp0..\.venv\Scripts\python.exe" set "PYEXE=%~dp0..\.venv\Scripts\python.exe"
if not defined PYEXE for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
if not defined PYEXE for %%I in (python.exe) do set "PYEXE=%%~$PATH:I"
if not defined PYEXE goto :nopython

"%PYEXE%" "%~dp0..\asset_gatekeeper.py" %* --wizard
set "CODE=%ERRORLEVEL%"
rem  Leave this folder before pausing, so this window never stops it being deleted.
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
