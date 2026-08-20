@echo off
REM ============================================================
REM  AMS2 Dash - lanzador para Windows
REM  Lee la SHARED MEMORY de AMS2 (sin UDP -> sin stutter) y
REM  sirve el dashboard del celular por HTTP (:8080) + WS (:8765).
REM
REM  Requiere en AMS2: Options -> System ->
REM     Shared Memory = On  |  Shared Memory Type = Project CARS 2
REM  (NO hace falta activar el UDP.)
REM
REM  Al arrancar imprime la URL del dashboard. Ctrl+C para parar.
REM  (La version UDP original sigue disponible como: python bridge.py)
REM ============================================================
cd /d "%~dp0"

REM  Guard de instancia unica: si ya hay un dash corriendo, el segundo muere al
REM  instante con WinError 10048 (bind duplicado en 8765) escupiendo un traceback
REM  que se lee como "no arranca" -- cuando en realidad el que si funciona es el
REM  que ya estaba arriba. Avisar en vez de mostrar el stack.
netstat -ano | findstr ":8765" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 goto :venv

echo El dash YA esta corriendo (el puerto 8765 esta ocupado).
echo No hace falta arrancarlo de nuevo: abre en el celular la URL de siempre (puerto 8080).
echo.
echo Si de verdad quieres reiniciarlo: cierra la ventana negra del dash y vuelve a correr esto.
echo.
pause
exit /b 0

:venv
if exist ".venv\Scripts\python.exe" goto :run

echo No encontre el entorno virtual (.venv) todavia.
echo Antes de arrancar el dash hay que instalarlo una vez: corriendo setup.bat.
echo.
echo Lanzando setup.bat ahora ...
echo.
call "%~dp0setup.bat"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo El entorno virtual sigue sin existir. Revisa los mensajes de setup.bat
    echo de mas arriba para ver que fallo.
    pause
    exit /b 1
)

:run
".venv\Scripts\python.exe" "bridge_shm.py"
echo.
echo [bridge detenido]
pause
