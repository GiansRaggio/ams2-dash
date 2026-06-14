# AMS2 Dash Web

Dashboard para el celular alimentado por la telemetría nativa de
Automobilista 2.

> **Fork con extensiones** del proyecto original de **Luciano Grandi**
> ([lucianograndim/ams2-dash](https://github.com/lucianograndim/ams2-dash)).
> Este fork añade: variante **Shared Memory** para Windows (sin stutter, `bridge_shm.py`),
> **leaderboard** de tiempos, y un **analizador de dampers** (histograma de velocidad de
> amortiguador + recomendaciones de clicks 4-way) con **guardrail de recorrido/resortes**.

## Archivos
- `bridge.py` — escucha el broadcast de AMS2 (UDP :5606, protocolo Project CARS 2),
  parsea telemetría/timings/time-stats y publica el estado por WebSocket (:8765).
  También calcula economía de combustible y sirve la página por HTTP (:8080).
- `index.html` — el dashboard estilo display GT3.
- `index-v1-backup.html` — la versión anterior, por si quieres volver.
- `ams2-dash-launch.sh` — wrapper de Steam para levantar el bridge junto con el
  juego (ver "Arranque automático"). **Específico del setup del autor**, ajustable.
- `tools/fake_telemetry.py` — emite paquetes sintéticos a :5606 para iterar la UI
  sin estar en pista.

## Qué muestra
- Tira de 20 LEDs de cambio (verde/ámbar/rojo + strobe azul al límite), marcha con
  glow y velocidad.
- Splits a coche de adelante/atrás, posición, vuelta, current/last/best lap (flash
  púrpura al mejorar), tiempo restante de sesión.
- **Combustible**: barra que se vacía con color por nivel, litros, % y **vueltas
  restantes** + consumo por vuelta (se calcula al cruzar meta).
- **Pit limiter** (banner), e indicadores **TC/ABS** que se encienden cuando el
  asistente interviene (no el nivel configurado — eso no viaja por UDP).
- Interpolación a 60fps y Wake Lock para que la pantalla no se apague.

## Setup desde cero (para otra persona)

Lo mínimo, en cualquier SO (Linux o Windows):

1. **Python 3** con el paquete **`websockets`**.
2. **AMS2** con UDP activado: *Options → System → `UDP = On`,
   `Protocol = Project CARS 2`, `Frequency = 1`* (si hay lag, subir a 4).
   El Shared Memory puede quedar en Project CARS 2 para MOZA; son independientes.
3. Un **celular en la misma red WiFi** que el PC.
4. Que el **firewall del PC permita los puertos 8080 y 8765** en la LAN.

```bash
git clone <url-del-repo> ams2-dash
cd ams2-dash

# instalar websockets (cualquiera de estas opciones):
python -m venv .venv && .venv/bin/pip install websockets      # venv estándar
#  o:  uv venv .venv && uv pip install --python .venv/bin/python websockets
#  o:  pip install --user websockets                          # global

# correr el bridge (imprime la URL del dashboard)
.venv/bin/python bridge.py
```

En el celular: abrir `http://IP_DEL_PC:8080`, girar a horizontal y "Agregar a
pantalla de inicio" para modo fullscreen.

`bridge.py` e `index.html` son **100% portables**. En **Windows** ni siquiera hace
falta el launcher: se abre AMS2 normal y se corre `python bridge.py` aparte.

### Windows sin stutter: variante Shared Memory (`bridge_shm.py`)

Activar el **UDP** en AMS2 puede causar **stuttering** en el juego (serializa y emite
un paquete por frame). Para evitarlo en Windows está `bridge_shm.py`, que lee la
**Shared Memory** de AMS2 (`$pcars2$`, formato Project CARS 2) en vez del UDP — el
juego ya la escribe siempre, nosotros solo la leemos: **costo ~cero, sin stutter**.
Salida idéntica (mismo WebSocket, mismo `index.html`).

- En AMS2: *Options → System → `Shared Memory = On`, `Type = Project CARS 2`*
  (no hace falta el UDP).
- Lanzar: doble clic a `start-dash.bat`, o `.venv\Scripts\python.exe bridge_shm.py`.
- Módulo `ams2_shm.py`: mapea la estructura `SharedMemory` v14 con `ctypes`
  (solo lectura, snapshots protegidos por `mSequenceNumber`). Solo Windows.

## Arranque automático con el juego (Linux / Steam)

`ams2-dash-launch.sh` levanta el bridge cuando arranca AMS2 y lo cierra al salir.
En *AMS2 → Properties → Launch Options*:

```
/home/USUARIO/sim/ams2-dash/ams2-dash-launch.sh gamescope -W 2560 -H 1440 -f -- mangohud %command%
```

Notas para adaptarlo:
- El script asume el repo en `~/sim/ams2-dash`; si lo clonas en otro lado, edita
  `DASH_DIR` adentro.
- El setup del autor además **encadena un overlay de pedales** antes de gamescope;
  si no lo tienes, omite esa parte.
- Mata cualquier bridge zombi antes de arrancar (los puertos WS/HTTP no usan
  `reuse_port`).

## Notas
- Usa `reuse_port` en :5606, así que puede convivir con otras apps que escuchen
  el mismo broadcast UDP de AMS2.
- Offsets basados en la spec UDP de Project CARS 2 (la que usa AMS2), verificados
  en pista. El protocolo expone telemetría e intervención de asistentes, pero **no**
  el nivel configurado de TC/ABS.
- `connected` pasa a "SIN SEÑAL" si no llegan paquetes por 3 s: la telemetría solo
  se emite en pista, no en menús.
- Las temperaturas de neumáticos están pendientes de calibrar en caliente.
```
