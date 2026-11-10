@echo off
rem ===========================================================================
rem  AssetGatekeeper - install the libraries it uses.  Double-click it.
rem
rem  Installs anything missing into the same Python that Start AssetGatekeeper.bat uses:
rem    - what the gate needs (trimesh, Pillow, ...), if the start file said so
rem    - the Polypaint bake (fast-simplification, xatlas, scipy; Python 3.10+)
rem    - watchdog (faster folder watching)
rem  Nothing that already works is changed, so running it twice is harmless.
rem
rem  Only check, change nothing:   Install_Extras.bat --check
rem ===========================================================================
setlocal
cd /d "%~dp0"

set "PYEXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
if not defined PYEXE for %%I in (python.exe) do set "PYEXE=%%~$PATH:I"
if not defined PYEXE goto :nopython

"%PYEXE%" "%~dp0tools\install_extras.py" %*
set "CODE=%ERRORLEVEL%"
rem  Leave this folder before pausing, so this window never stops it being deleted.
cd /d "%USERPROFILE%"
echo.
if "%CODE%"=="0" echo You can close this window. Double-click Start AssetGatekeeper.bat to begin.
if not "%CODE%"=="0" echo Something was not installed - the lines above say what and why.
echo.
pause
exit /b %CODE%

:nopython
echo.
echo   Python was not found.
echo   Install Python 3.12 from python.org (tick "Add python.exe to PATH"),
echo   then run this file again.
echo.
pause
exit /b 1
