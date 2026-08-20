@echo off
REM Captura de frame times reales de AMS2 con PresentMon (Intel).
REM
REM Windows pide permiso de administrador UNA vez: es --restart_as_admin, que
REM PresentMon necesita porque una sesion de trazas ETW no se puede abrir sin
REM elevar. Sin eso muere con "access denied".
REM
REM OJO CON EL NOMBRE DEL PROCESO: AMS2 trae DOS ejecutables, AMS2.exe y
REM AMS2AVX.exe, y en esta maquina el que corre de verdad es AMS2AVX.exe (el
REM lanzador elige segun el soporte de AVX de la CPU). Apuntar al otro captura
REM CERO frames sin dar ningun error -- se ve igual que "no hubo problemas".
REM Verificar con: tasklist ^| findstr AMS2
REM
REM LANZAR ESTO **ANTES** DE ABRIR AMS2. No es una preferencia: enganchado a un
REM AMS2 que ya estaba corriendo, PresentMon clasifica todos los frames como
REM "Composed: Flip" en vez de "Hardware Composed: Independent Flip" -- pierde la
REM creacion de la swap chain y ve el camino del compositor. Y en modo compuesto
REM el DWM regulariza los intervalos, asi que los atascos de la aplicacion se
REM ESCONDEN: medido, la misma maquina dio p99 = 6.34 ms enganchando tarde contra
REM 13.05 ms arrancando antes. La captura sale preciosa y no significa nada.
REM Verificar siempre en el CSV que la columna PresentMode diga "Independent Flip".
REM
REM NO USAR --terminate_on_proc_exit: se traba. PresentMon abre un handle al
REM proceso del juego, y ese handle mantiene VIVO el objeto del proceso aunque
REM el juego ya haya muerto (0 hilos, 0 handles propios). Entonces PresentMon
REM espera para siempre a que desaparezca un proceso que el mismo esta
REM sosteniendo, y de paso deja el CSV bloqueado en exclusiva -- ni se puede
REM leer ni se puede matar sin elevar. Paso exactamente eso, dos veces.
REM
REM En su lugar la captura se corta sola por tiempo. Al terminar, PresentMon
REM cierra el archivo y queda legible sin cerrar el juego ni pelear con nadie.
REM Duracion en segundos como primer argumento (default 1800 = 30 min).
REM Para cortarlo antes: Ctrl+C en esta ventana.

setlocal
REM HERRAMIENTA DE DESARROLLO, no de alumnos: PresentMon vive fuera de este
REM repo (lo comparte con el proyecto lmu-dash de esta maquina). Si no existe
REM en tu equipo, descargalo de github.com/GameTechDev/PresentMon y ajusta BIN.
set BIN=C:\Users\gians\sim\lmu-dash\tools\bin
if not exist "%BIN%\PresentMon.exe" (
    echo No encontre PresentMon.exe en %BIN%
    echo Esta es una herramienta de DESARROLLO, no hace falta para usar el dash.
    echo Descarga PresentMon de github.com/GameTechDev/PresentMon y ajusta BIN.
    pause
    exit /b 1
)
set SALIDA=%~dp0..\frametime
set DURACION=%~1
if "%DURACION%"=="" set DURACION=1800

if not exist "%SALIDA%" mkdir "%SALIDA%"

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set TS=%%i
set CSV=%SALIDA%\ams2_%TS%.csv

echo.
echo   grabando frame times de AMS2  -^>  %CSV%
echo   la captura se corta sola a los %DURACION% segundos
echo.

"%BIN%\PresentMon.exe" ^
  --process_name "AMS2AVX.exe" ^
  --output_file "%CSV%" ^
  --v2_metrics ^
  --timed %DURACION% ^
  --terminate_after_timed ^
  --stop_existing_session ^
  --restart_as_admin

echo.
echo   listo: %CSV%
pause
