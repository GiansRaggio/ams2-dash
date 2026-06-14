@echo off
REM ============================================================
REM  AMS2 Dash - lanzador para Windows (PC-MASTERRACE)
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
".venv\Scripts\python.exe" "bridge_shm.py"
echo.
echo [bridge detenido]
pause
