@echo off
rem ---------------------------------------------------------------------------
rem  Art Workstation - 3D review screen
rem  Double-click this file. The server runs IN THIS WINDOW (so any error stays
rem  readable). Close the window or press Ctrl+C to stop it.
rem  Extra arguments go straight to art_workstation_server.py, e.g.
rem      start_workstation.bat --port 8200
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

rem --- find an interpreter that can import trimesh (the servers need it) -----
set "PYEXE="
if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
if not defined PYEXE for %%I in (python.exe) do set "PYEXE=%%~$PATH:I"
if not defined PYEXE for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
if not defined PYEXE goto :nopython
"%PYEXE%" -c "import trimesh" >nul 2>&1
if errorlevel 1 goto :nopython

echo.
echo   Art Workstation  -  3D review screen
echo   URL      http://127.0.0.1:8100/viewer
echo   Stop     close this window, or press Ctrl+C
echo   Python   %PYEXE%
echo.
echo   The page starts empty - drag a .glb / .obj / .gltf onto it to upload.
echo.

rem open the browser a couple of seconds later without blocking the server
start /b "" "%PYEXE%" "%~dp0tools\open_url_after.py" 2000 "http://127.0.0.1:8100/viewer"

"%PYEXE%" "%~dp0server\art_workstation_server.py" %*
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
