@echo off
rem ---------------------------------------------------------------------------
rem  AssetHealth - studio metrics dashboard
rem  Double-click this file. The server runs IN THIS WINDOW (so any error stays
rem  readable). Close the window or press Ctrl+C to stop it.
rem  Extra arguments go straight to asset_health_server.py, e.g.
rem      tools\start_asset_health.bat --port 8090
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0.."

rem --- find an interpreter that can import trimesh (the servers need it) -----
set "PYEXE="
if exist "%~dp0..\.venv\Scripts\python.exe" set "PYEXE=%~dp0..\.venv\Scripts\python.exe"
if not defined PYEXE for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
if not defined PYEXE for %%I in (python.exe) do set "PYEXE=%%~$PATH:I"
if not defined PYEXE for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
if not defined PYEXE goto :nopython
"%PYEXE%" -c "import trimesh" >nul 2>&1
if errorlevel 1 goto :nopython

echo.
echo   AssetHealth  -  studio metrics dashboard
echo   URL      http://127.0.0.1:8080/
echo   Stop     close this window, or press Ctrl+C
echo   Python   %PYEXE%
echo.
echo   Shows zeros until the gate has logged runs into asset_health.db.
echo.

rem open the browser a couple of seconds later without blocking the server
start /b "" "%PYEXE%" "%~dp0open_url_after.py" 2000 "http://127.0.0.1:8080/"

"%PYEXE%" "%~dp0..\server\asset_health_server.py" %*
echo.
echo Server exited.
pause
exit /b 0

:nopython
echo.
echo No Python that can import trimesh was found.
echo Create one with:
echo     py -3 -m venv .venv
echo     .venv\Scripts\python.exe -m pip install trimesh pillow
echo.
pause
exit /b 1
