#!/usr/bin/env bash
# Wrapper de Steam launch para AMS2: levanta el bridge de telemetria
# (UDP AMS2 -> WebSocket + HTTP del dashboard del celular), ejecuta lo que
# venga en "$@" (el resto de la cadena de launch: overlay, gamescope, proton,
# el juego), y mata el bridge cuando el juego termina por cualquier motivo.
#
# Disenado para encadenarse con el wrapper del overlay de pedales. Uso en
# Steam (AMS2 -> Properties -> Launch Options):
#   .../ams2-dash/ams2-dash-launch.sh .../pedal-overlay/ams2-launch.sh \
#       gamescope -W 2560 -H 1440 -r 165 -f --force-grab-cursor -- mangohud %command%

set -u

DASH_DIR="$HOME/sim/ams2-dash"
PYTHON="$DASH_DIR/.venv/bin/python"
LOG="$DASH_DIR/bridge.log"

bridge_pid=""

cleanup() {
    [ -n "$bridge_pid" ] && kill "$bridge_pid" 2>/dev/null
}
trap cleanup EXIT INT TERM

# Matar cualquier bridge viejo que haya quedado colgado (los puertos WS/HTTP no
# usan reuse_port, asi que una instancia zombi bloquearia el arranque). El patron
# incluye "bridge.py", que no aparece en la linea de este wrapper -> no se autocancela.
pkill -f "$DASH_DIR/bridge.py" 2>/dev/null
sleep 0.3

# Arrancar el bridge en background. Loguea a bridge.log para depurar offsets
# o problemas de conexion despues de la sesion.
if [ -x "$PYTHON" ]; then
    "$PYTHON" "$DASH_DIR/bridge.py" >"$LOG" 2>&1 &
    bridge_pid=$!
else
    echo "[ams2-dash] WARN: no existe el venv ($PYTHON); el dash no se levanta" >&2
fi

# Ejecutar el resto de la cadena (NO exec: necesitamos esperar al juego para
# correr el trap y cerrar el bridge).
"$@"
exit $?
