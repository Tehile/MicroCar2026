@echo off
REM ============================================================================
REM  PhoneCamera PC receiving service - one-command launcher
REM
REM  Why a .cmd and not an .exe / Windows service: the requirement was
REM  "just make it usable". Packaging into an exe adds build steps and hides
REM  the config; a service makes debugging harder (logs go to a file you have
REM  to find). This keeps the debugging entry points that the project depends on
REM  (panel, test_server.py, probe_throughput.py).
REM
REM  Edit the four variables below, then double-click this file.
REM ============================================================================
setlocal

REM Python interpreter (the project's known-good environment)
set "PY=C:\Users\liuru\.conda\envs\general_env\python.exe"

REM Listen port. Phone must be able to reach it, so the default binds 0.0.0.0.
set "PORT=8765"

REM Fixed token: makes the address stable across restarts while debugging.
REM Leave it as-is, or change it. It IS the credential - do not share it.
set "TOKEN=ab12cd34"

REM Where sessions get written.
set "DATADIR=%~dp0data"

REM Delivery-lane throttle in MB/s (0 = unlimited). The measured WiFi link is
REM about 5.5 MB/s, so a large value here does not make the link faster.
set "MAXMBPS=40"

echo.
echo Starting PhoneCamera PC service  (port %PORT%, token %TOKEN%)
echo   data dir : %DATADIR%
echo   python   : %PY%
echo.
echo Keep this window open. Closing it stops the service (and disconnects the phone).
echo.

"%PY%" "%~dp0server.py" --port %PORT% --token %TOKEN% --data-dir "%DATADIR%" --max-mbps %MAXMBPS%
set RC=%ERRORLEVEL%
echo.
if not "%RC%"=="0" echo Service exited with code %RC%  (see the messages above)
pause
endlocal
