# AMS2 Dash Web — alternativa a SimDashboard para Linux

Dashboard para el celular alimentado por la telemetría UDP nativa de
Automobilista 2. No requiere el server de Windows de SimDashboard.

## Archivos
- `bridge.py` — escucha el broadcast de AMS2 (UDP :5606, protocolo Project CARS 2),
  parsea telemetría/timings/time-stats y los publica por WebSocket (:8765).
  También sirve la página por HTTP (:8080).
- `index.html` — el dashboard v2 estilo display GT3: tira de 20 LEDs de cambio
  (verde/ámbar/rojo + strobe azul al límite), marcha con glow, paneles chaflanados,
  flash púrpura al mejorar el best lap, warning de fuel <10%, interpolación a 60fps.
- `index-v1-backup.html` — la versión anterior, por si querés volver.
  splits ahead/behind, current/last/best lap, posición, vuelta, fuel,
  barras de freno/acelerador, tiempo restante de sesión).

## Uso
1. `pip install websockets` (Arch: `sudo pacman -S python-websockets`)
2. En AMS2 → Options → System: `UDP = On`, `Protocol = Project CARS 2`, `Frequency = 1`
   (si hay lag, subir a 4). Shared Memory puede quedar en Project CARS 2 para MOZA.
3. `python bridge.py` → imprime la URL.
4. En el celular (misma WiFi): abrir `http://IP_DEL_PC:8080`, girar a horizontal,
   "Agregar a pantalla de inicio" para modo fullscreen. Usa Wake Lock para que
   la pantalla no se apague.

## Notas
- Convive con la app SimDashboard: ambos pueden escuchar el broadcast a la vez
  (el bridge usa `reuse_port`).
- Offsets basados en la spec UDP de Project CARS 2 (la que usa AMS2).
  Validados con paquetes sintéticos; si algún campo se ve raro en pista,
  es ajuste fino de offset — ideal para iterar con Claude Code.
- `connected` pasa a "SIN SEÑAL" si no llegan paquetes por 3 s
  (recordatorio: la telemetría solo se emite en pista, no en menús).
