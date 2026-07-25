@echo off
REM ============================================================
REM  AMS2 Dash - instalador (doble clic)
REM
REM  Detecta Python, crea el entorno virtual (.venv), instala las
REM  dependencias (requirements.txt) y pregunta el nombre de piloto
REM  para player.txt (necesario si juegas ONLINE, ver README.md).
REM
REM  Se puede correr de nuevo sin problema: no reinstala el venv si
REM  ya existe, y no pisa player.txt si ya existe.
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   AMS2 Dash - instalador
echo ============================================================
echo.

set "PYCMD="

REM --- 1) Preferir el Python Launcher (py), que no tiene el problema
REM         del alias de la Microsoft Store cuando Python no esta instalado.
py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "PYCMD=py -3"
    goto :python_ok
)

REM --- 2) Probar "python" a secas, pero solo si NO resuelve al alias-stub
REM         de la Microsoft Store (si resuelve ahi y Python no esta instalado
REM         de verdad, ejecutarlo abre la Store en vez de fallar limpio).
set "PYWHERE="
for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYWHERE set "PYWHERE=%%P"

if defined PYWHERE (
    echo !PYWHERE! | findstr /i "WindowsApps" >nul
    if errorlevel 1 (
        python --version >nul 2>&1
        if not errorlevel 1 (
            set "PYCMD=python"
            goto :python_ok
        )
    )
)

echo No encontre una instalacion de Python en este PC.
echo.
echo Para instalarlo:
echo   1. Anda a https://www.python.org/downloads/
echo   2. Descarga el instalador (boton amarillo, Download Python)
echo   3. Al ejecutarlo, MARCA la casilla "Add python.exe to PATH"
echo      en la primera pantalla del instalador (sin eso, no va a andar)
echo   4. Vuelve a correr este setup.bat
echo.
start "" "https://www.python.org/downloads/"
echo.
pause
exit /b 1

:python_ok
echo Python encontrado, usando: %PYCMD%
echo.

if exist ".venv\Scripts\python.exe" goto :venv_ready

echo Creando el entorno virtual en .venv (solo la primera vez, puede tardar un poco) ...
%PYCMD% -m venv .venv
if errorlevel 1 (
    echo.
    echo No se pudo crear el entorno virtual. Revisa el mensaje de arriba.
    pause
    exit /b 1
)
goto :install_deps

:venv_ready
echo El entorno virtual .venv ya existe, no lo toco.

:install_deps
echo.
echo Instalando dependencias (websockets) ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo.
    echo Fallo la instalacion de dependencias. Revisa el mensaje de arriba.
    pause
    exit /b 1
)
echo Dependencias instaladas OK.
echo.

if exist "player.txt" (
    echo Ya existe player.txt, no lo toco. Si quieres cambiar el nombre, editalo a mano.
    goto :fin
)

echo Ultimo paso: tu nombre de piloto en AMS2.
echo.
echo Esto importa SOLO si vas a jugar ONLINE: sin player.txt, en carreras
echo multijugador el dash puede seguir la camara (el auto de OTRO piloto) en vez
echo del tuyo, y toda la telemetria (vueltas, combustible, estrategia) queda mala.
echo En un solo jugador no hace diferencia.
echo.
set "PLAYERNAME="
set /p PLAYERNAME=Nombre de piloto tal como aparece en AMS2 (Enter para omitir):

if "!PLAYERNAME!"=="" (
    echo No se creo player.txt. Puedes crearlo despues a mano si empiezas a jugar online.
) else (
    REM Se escribe con Python, NO con "echo": echo usa el codepage OEM de la consola
    REM (CP850 en Windows en espanol) y un nick con tilde o enie quedaba mal codificado.
    REM Como las codificaciones de un byte nunca fallan al decodificar, el error no se
    REM notaba: el nombre no matcheaba, el dash caia a la camara en multiplayer y la
    REM telemetria salia mala SIN ningun aviso. Python lo deja en UTF-8 y se acabo.
    ".venv\Scripts\python.exe" -c "import sys,io; io.open('player.txt','w',encoding='utf-8').write(sys.argv[1].strip())" "!PLAYERNAME!"
    if errorlevel 1 (
        echo No pude escribir player.txt. Crealo a mano: un archivo de texto con tu nombre.
    ) else (
        echo Guardado en player.txt.
    )
)

:fin
echo.
echo ============================================================
echo   Listo. Para arrancar el dash: doble clic a start-dash.bat
echo ============================================================
echo.
pause
