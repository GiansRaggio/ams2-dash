# AMS2 Dash Web

Dashboard para el celular alimentado por la telemetría nativa de
Automobilista 2.

> **Fork con extensiones** del proyecto original de **Luciano Grandi**
> ([lucianograndim/ams2-dash](https://github.com/lucianograndim/ams2-dash)).
> Este fork añade: variante **Shared Memory** para Windows (sin stutter, `bridge_shm.py`),
> **leaderboard** de tiempos, páginas de **GOMAS**, **ESTRATEGIA** y **DAMPERS**, un
> **grabador de telemetría a disco** y un **analizador offline** (`tools/analyze_telemetry.py`).

## Qué muestra

- **Visor de análisis** (`http://<IP>:8080/analisis.html`, botón 📈 del dash):
  después de girar, tus vueltas quedan grabadas solas — mapa de la pista con las
  curvas numeradas, comparación entre dos vueltas, delta de tiempo metro a metro
  y export a CSV. Es la herramienta con la que tu coach revisa tu sesión.

- **Página principal**: tira de 20 LEDs de cambio (verde/ámbar/rojo + strobe azul al
  límite), marcha con glow y velocidad. Splits a coche de adelante/atrás, posición,
  vuelta, current/last/best lap (flash púrpura al mejorar), tiempo restante de sesión.
  **Combustible**: barra que se vacía con color por nivel, litros, % y **vueltas
  restantes** + consumo por vuelta (se calcula al cruzar meta). **Pit limiter**
  (banner), e indicadores **TC/ABS** que se encienden cuando el asistente interviene
  (no el nivel configurado — eso no viaja por telemetría). Interpolación a 60fps y
  Wake Lock para que la pantalla no se apague.
- **LEADERBOARD**: posiciones y tiempos del resto de la parrilla.
- **GOMAS** (`ams2_tyres.py`): temperatura por zona (interior/medio/exterior),
  presión en caliente con delta contra el objetivo, desgaste, temp de carcasa y de
  freno, y sesgo térmico entre ejes. Ver detalle más abajo.
- **ESTRATEGIA** (`ams2_strategy.py`): combustible/vueltas restantes, plan de
  carrera y el semáforo de cruce lluvia→lisos. Ver `docs/SETUP-NOTES.md` para el
  detalle de cómo se calcula.
- **DAMPERS** (`ams2_dampers.py`): histograma de velocidad de amortiguador por
  esquina + recomendaciones heurísticas de clicks 4-way.
- **Grabador de telemetría a disco** (parte de `ams2_telemetry.py`, corre solo con
  `bridge_shm.py`): por cada vuelta válida guarda un resumen y la traza completa
  (todos los canales) en `telemetry/<pista>__<auto>__<sesión>__<fecha>/`, pensado
  para analizar después.
- **Analizador offline** (`tools/analyze_telemetry.py`): lee lo que grabó el
  punto anterior. `--list` (sesiones agrupadas), `--insights`, `--tyres`,
  `--balance` (sobre/subviraje), `--vs LAP1 LAP2` (compara dos vueltas), `--combo`
  (tendencia entre prácticas de un mismo auto+pista), `--race-fuel <min>` (carga
  de combustible estimada desde el consumo real medido). Ver `docs/SETUP-NOTES.md`
  para cómo se usa en la práctica.

## Setup desde cero, en Windows (el camino real para instalar esto)

Esto asume que nunca has tocado Python ni una terminal. Sigue los pasos en orden.

### 1. Instalar Python

En Windows 11 limpio, si escribes `python` en una terminal sin tener Python
instalado, **Windows abre la Microsoft Store** — no es un error, pero tampoco es
lo que queremos.

- Instálalo desde **[python.org/downloads](https://www.python.org/downloads/)**
  (botón amarillo "Download Python"). Al ejecutar el instalador, **marca la
  casilla "Add python.exe to PATH"** en la primera pantalla — si no la marcas,
  nada de lo que sigue va a encontrar `python`.
- Alternativa si ya tienes el *Python Launcher* de Windows: usar `py -3` en vez
  de `python` en cualquier comando de esta guía funciona igual.

### 2. Descargar el proyecto

En la página del repo: botón verde **Code → Download ZIP**, y descomprime el ZIP
en una carpeta tuya (Documentos, por ejemplo — **no lo dejes en Descargas**, que
suele limpiarse). Eso es todo: no necesitas git.

Si ya usas git, lo de siempre:

```
git clone https://github.com/GiansRaggio/ams2-dash.git
cd ams2-dash
```

### 3. Instalar dependencias y configurar el nombre de piloto

Doble clic a **`setup.bat`**. Crea el entorno virtual (`.venv`), instala lo
necesario, y te pregunta tu nombre de piloto en AMS2 (para `player.txt`, ver
el paso 4 — por qué importa). Si algo falla (por ejemplo Python no instalado),
`setup.bat` te lo dice en español en vez de cerrarse solo.

Si prefieres hacerlo a mano en vez de `setup.bat`:

```
py -3 -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt
```

### 4. `player.txt` — obligatorio si vas a jugar ONLINE (multiplayer)

**Este paso no es opcional si corres carreras online.** AMS2 expone en su
telemetría un índice de "participante visto" que en **multiplayer sigue a la
CÁMARA** (transmisión, otros pilotos, replay), no necesariamente a TU auto. Sin
anclar el dash a tu nombre, en cuanto la cámara se va a otro competidor el dash
empieza a leer y grabar **el auto de otra persona**: vueltas, distancia,
combustible, estrategia y la telemetría grabada a disco quedan corrompidas sin
ningún error visible. En single-player no pasa (el participante visto ya eres tú),
pero en online es un bug real y silencioso.

La solución: el archivo `player.txt` (en la raíz del proyecto, uno por línea, tu
nombre de piloto tal como aparece en AMS2) o la variable de entorno
`AMS2_PLAYER_NAME`. Con eso, el bridge te busca por nombre entre los participantes
en vez de confiar en la cámara. `setup.bat` te lo pide y lo crea la primera vez;
si necesitas cambiarlo después, edita `player.txt` a mano (no hace falta que sea
el nombre completo, basta con que sea parte única de tu nombre en el juego).

### 5. Configurar AMS2

*Options → System*:

- **`Shared Memory = On`**, **`Shared Memory Type = Project CARS 2`**.
- No hace falta tocar el UDP para el camino recomendado (ver más abajo si de
  verdad quieres esa variante).

### 6. Firewall — el paso que falla en silencio

En un PC/red nueva, Windows 11 suele clasificar el WiFi como red **Pública**. La
primera vez que corras el dash, Windows va a mostrar un cuadro de diálogo pidiendo
permiso de firewall para `python.exe` — **pero ese cuadro trae marcada solo la
casilla "Redes privadas"**. Si tu red quedó como Pública, el permiso no aplica:
el bridge arranca perfecto, en la consola dice que todo está bien, pero el celular
**nunca va a conectar** y no vas a ver ningún error que lo explique.

Cómo arreglarlo:

1. **Poner la red en Privada**: *Configuración → Red e Internet → Wi-Fi* → clic en
   la red conectada → **Perfil de red: Privada** (u "Otros PC's pueden verlo").
2. Cuando corras el bridge por primera vez y aparezca el aviso de firewall de
   Windows Defender, **marca las dos casillas** (Privadas y Públicas, o al menos
   Privadas si ya hiciste el paso 1) y **Permitir acceso**.
3. Si ya lo cerraste sin marcarlo: *Configuración → Privacidad y seguridad →
   Seguridad de Windows → Firewall y protección de red → Permitir una app a
   través del firewall* → busca **Python** → marca **Privada**.

### 7. Arrancar el dash

Doble clic a **`start-dash.bat`**. La consola va a imprimir algo como:

```
[bridge-shm] Dash : http://192.168.1.XX:8080  <- abrir en el celular
```

Esa es la IP real de tu PC en tu red — **cópiala tal cual la imprime la
consola** en vez de adivinarla. En el celular (misma red WiFi): abre esa URL,
gira a horizontal y "Agregar a pantalla de inicio" para que quede en modo
fullscreen como una app.

> ⚠️ **Úsalo en tu red de casa, no en WiFi público.** Mientras el bridge corre,
> cualquiera en la misma red puede abrir esa URL — y además el servidor expone la
> carpeta del proyecto, o sea tu telemetría grabada y tu `player.txt`. No hay
> contraseña. En tu casa da lo mismo; en la WiFi de un café o una universidad, no
> lo levantes.

## Archivos

- `setup.bat` — instalador de doble clic: detecta Python, crea `.venv`, instala
  `requirements.txt` y configura `player.txt`.
- `start-dash.bat` — lanzador de doble clic: corre `bridge_shm.py`. Si falta el
  `.venv`, te manda a correr `setup.bat` primero en vez de fallar en silencio.
- `requirements.txt` — dependencias fijadas (`websockets`).
- `player.txt` — tu nombre de piloto en AMS2, para anclar el dash a tu auto en
  multiplayer (ver paso 4 más arriba). No se versiona (está en `.gitignore`).
- `bridge_shm.py` — **el bridge recomendado en Windows.** Lee la Shared Memory
  de AMS2 (sin UDP, sin stutter) y publica el estado por WebSocket (:8765) +
  sirve el dashboard por HTTP (:8080). Emite telemetría, leaderboard, estrategia,
  gomas y dampers.
- `ams2_shm.py` — mapea la Shared Memory de AMS2 (`$pcars2$`, v14) con `ctypes`.
  Solo Windows.
- `ams2_telemetry.py`, `ams2_strategy.py`, `ams2_tyres.py`, `ams2_dampers.py` —
  los módulos de cada página / feature, todos alimentados por `ams2_shm.py`.
- `index.html` — el dashboard estilo display GT3, servido al celular.
- `index-v1-backup.html` — la versión anterior, por si quieres volver.
- `tools/analyze_telemetry.py` — analizador offline de lo grabado en `telemetry/`.
- `tools/informe.py` — genera el **informe de una página** del alumno a partir de
  una carpeta de sesión (ver más abajo). Usado por la Escuela de Conducción
  Deportiva AMS2 Chile.
- `entrega.py` — cliente de entrega del alumno: valida la sesión, la empaqueta y
  la sube al servicio de la escuela.
- `tools/fake_telemetry.py` — emite paquetes UDP sintéticos a :5606 para iterar
  la UI de la variante UDP sin estar en pista.
- `bridge.py` — variante **UDP** (protocolo Project CARS 2, puerto 5606), la
  original del fork base. Ver la sección "Variante UDP" más abajo: **no** emite
  leaderboard, estrategia, gomas ni dampers, y no graba telemetría a disco.
- `ams2-dash-launch.sh` — wrapper de Steam para levantar `bridge.py` junto con el
  juego en Linux (ver "Arranque automático en Linux"). Específico del setup del
  autor, ajustable.

## Compartir telemetría con tu coach

Cada sesión queda **autocontenida** en una carpeta
`telemetry/<pista>__<auto>__<sesión>__<fecha>/`: metadatos (`session.json`),
resumen por vuelta (`summary.jsonl`), línea de tiempo (`timeline.jsonl`) y una
traza completa por vuelta (`L###_<tiempo>s.csv.gz`, ~95 canales a 50 Hz; las
vueltas invalidadas van como `X###`). Para enviarla, **comprime la carpeta
completa y mándala** — el coach la deja en su `telemetry/` y la ve en el visor
(`http://<su-ip>:8080/analisis.html`) con mapa de pista, curvas numeradas,
comparación entre vueltas y delta de tiempo.

También se puede **exportar una tanda a CSV** desde el propio visor (botón ⬇),
para abrirla en Excel o donde quieras.

Si usas la app **Sim Racing Telemetry**, `ams2_srt.py` convierte en ambos
sentidos: `python ams2_srt.py <archivo.srt> --importar` trae vueltas ajenas al
visor, y `ams2_srt.exportar(...)` genera un `.srt` que esa app abre.

## Informe de una página (`tools/informe.py`)

Convierte una carpeta de sesión en un HTML autocontenido para entregarle al
alumno. Es la herramienta de coaching de la escuela, no un dump de datos:

```bash
python tools/informe.py telemetry/<carpeta> --alumno "Nombre" --nivel 2 --abrir
```

Sale en `informes/` (gitignoreado: el informe lleva el nombre del alumno y sus
números, que son dato personal de un tercero).

Las cinco secciones, en este orden: **tus dos números** con su escala dibujada al
lado (gap% contra la referencia del combo y CV% de dispersión), **dónde se te va
el tiempo** por distancia en metros con la causa medida y la acción que la
corrige, **el mapa** de tu vuelta con la referencia encima y los tramos marcados,
**una sola cosa** para trabajar, y **tu nivel** expresado en permisos.

Lo que el informe **no** hace, a propósito:

- **No inventa.** Sin la referencia guardada del combo, el gap% sale como "sin
  dato todavía" y dice por qué; nunca se rellena con otra cosa.
- **No lista un tramo sin causa identificable.** Una brecha sin ruta de acción es
  la forma de feedback que peor mide.
- **No deduce el nivel de los números.** `--nivel` lo pone el instructor: el nivel
  es un permiso que abren la limpieza, la percepción del entorno y una evaluación
  en vivo — no el cronómetro.
- **No compara al alumno con nadie más.**
- **No numera las curvas**: las dos herramientas del dash las numeran distinto
  entre sí, así que todo se ubica por distancia en metros.

Si la sesión mezcló condiciones a mitad de tanda (pista, compuesto, ayudas), no
emite ningún número y lo dice: cualquier cifra saldría creíble y equivocada.

## Variante UDP (Linux, o si de verdad la necesitas en Windows): `bridge.py`

`bridge.py` es la variante original del fork base: escucha el broadcast UDP de
AMS2 (puerto 5606, protocolo Project CARS 2) en vez de leer la Shared Memory.
Es la única opción portable a Linux/macOS (la Shared Memory es exclusiva de
Windows), pero tiene dos limitaciones importantes frente a `bridge_shm.py`:

- **Activar el UDP en AMS2 puede causar stuttering** en el juego, porque lo
  obliga a serializar y emitir un paquete por frame.
- **Solo emite la página principal** (velocidad, marcha, combustible, splits,
  TC/ABS). **No tiene leaderboard, ESTRATEGIA, GOMAS ni DAMPERS**, y no graba
  telemetría a disco — esas features viven en `bridge_shm.py` / `ams2_*.py` y
  dependen de la Shared Memory. Si usas `bridge.py`, esas 4 páginas van a
  quedar vacías; no es un bug, es que la fuente de datos no las trae.

En AMS2: *Options → System → `UDP = On`, `Protocol = Project CARS 2`,
`Frequency = 1`* (si hay lag, subir a 4). Correr con:

```
.venv\Scripts\python.exe bridge.py
```

(en Linux/macOS: `.venv/bin/python bridge.py`, instalando antes con
`.venv/bin/pip install -r requirements.txt`). El resto del setup (celular,
firewall) es igual al de la variante Shared Memory.

## Arranque automático con el juego (Linux / Steam)

`ams2-dash-launch.sh` levanta `bridge.py` cuando arranca AMS2 y lo cierra al
salir. En *AMS2 → Properties → Launch Options*:

```
/home/USUARIO/sim/ams2-dash/ams2-dash-launch.sh gamescope -W 2560 -H 1440 -f -- mangohud %command%
```

Notas para adaptarlo:
- El script asume el repo en `~/sim/ams2-dash`; si lo clonas en otro lado, edita
  `DASH_DIR` adentro.
- El setup del autor además **encadena un overlay de pedales** antes de gamescope;
  si no lo tienes, omite esa parte.
- Mata cualquier bridge zombi antes de arrancar (los puertos WS/HTTP no usan
  `reuse_port` en todas las plataformas).

## Notas

- `connected` pasa a "SIN SEÑAL" si no llegan paquetes/snapshots por 3 s: la
  telemetría solo se emite en pista, no en menús.
- Offsets basados en la spec UDP / Shared Memory de Project CARS 2 (la que usa
  AMS2), verificados en pista. El protocolo expone telemetría e intervención de
  asistentes, pero **no** el nivel configurado de TC/ABS.

## Página GOMAS (`ams2_tyres.py`) — **BETA**

> ⚠️ **Esta página está en beta.** Las **mediciones** (temperaturas, presiones,
> desgaste) son directas del juego y confiables. Los **veredictos** (qué presión
> agregar/sacar, el estado térmico, el camber) siguen en validación: úsalos como
> referencia y contrasta con lo que sientes en pista, no como verdad. El detalle
> de qué está verificado y qué no vive en `docs/ESTADO.md`.

Por esquina: el **corte térmico por profundidad** (SUP/BULK/CARC — piel, masa,
carcasa), los bordes **interior/exterior** (spread → camber), presión en caliente
con el **delta contra el objetivo** (cuánto agregar o sacar), desgaste y freno.
En el spine, el sesgo térmico entre ejes.

El objetivo de presión se fija con el botón 🎯 y el bridge lo **persiste por auto**
en `tyre_targets.json`. El delta se aplica **en frío** en el garaje: el cambio se
traslada casi 1:1 a la presión en caliente. Sólo aparece con la goma en temperatura
(en frío la lectura no decide nada, y el dash lo dice en vez de mostrar un número
que engaña).

Los canales se **midieron en pista** con `tools/tyre_probe.py` y después se
**contrastaron contra el corpus grabado** (58 sesiones / 22 autos / 22 pistas al escribir esto; crece con cada tanda) con
`tools/tyre_replay.py`, no se sacaron de la documentación. Lo que salió de ahí:

- `mAirPressure` viene en **Bar×100**; `mTyreCarcassTemp` en **Kelvin**.
- **La carcasa es el ÚNICO canal térmico confiable** (vivo en el 100% de las
  sesiones). El modelo de superficie (bulk/layer/bordes) viene **muerto en ~10% de
  las sesiones** — pegado al ambiente con la carcasa a 75-130 °C, por sesión y no
  por auto — así que el bridge lo detecta en vivo (`surf_dead`) y emite esos canales
  en null en vez de mostrar basura.
  **Ese ~10% no está repartido al azar**: las 6 sesiones muertas del corpus son las 6
  sesiones de **lluvia** (bulk en 30-36 °C con la carcasa en 110-131), o sea 6 de las
  10 sesiones con goma de agua. Justo donde vive el semáforo de cruce lluvia→lisos,
  que leía ese canal. Por eso el bridge le pasa `surf_alive` al director de estrategia
  y la señal de "wets recalentando" **no opina** cuando el canal está muerto: el panel
  muestra `wets sin señal` en vez de inventar una goma helada. Se verifica con
  `tools/crossover_replay.py` (replay del semáforo contra las sesiones de lluvia reales).
- **No existe ventana térmica absoluta que generalice.** Las medianas de carcasa van
  de 52 °C (Formula Vee) a 143 °C (protos): cualquier umbral fijo dispara "hot" o
  "cold" en conducción normal según el auto. Por eso el color y el veredicto son
  **auto-referenciales**: `rel` (esta esquina vs la media de las 4, pinta la banda
  CARC), `tdev` (la estructura se movió contra su propia norma lenta → acento de
  borde; en la gran mayoría de las sesiones reproducidas está apagado el 100% del tiempo),
  `trend` (calentando/estable/enfriando) y `warm` (cerca del plateau propio, no de
  un 60 fijo).
- `mTyreTempLeft/Right` están en marco **absoluto del auto** (izquierda/derecha de la
  pista), no relativo a la rueda: hay que mapear interior/exterior por lado. Se dedujo
  del dato, porque el borde interior salió más caliente en las cuatro esquinas de forma
  espejada — firma de camber negativo, que sólo cuadra si L/R son absolutos.
- **`mTyreTempCenter` es idéntico a `mTyreTemp`**, o sea el "centro" no es una tercera
  medición independiente del piso de la goma. Por eso el dash **no** deriva presión del
  perfil centro-vs-hombros (el clásico "centro caliente = sobreinflado"): ese diagnóstico
  necesita un centro real. Las tres zonas se muestran igual, y el spread
  interior-exterior sí se usa (esa sí es distribución lateral medida) para opinar del
  camber. Caveat de referencia: SimHub #632.
- **El camber sólo se lee en la rueda que la pista CARGA, y ésa es la de spread BAJO.**
  Sobre las 69 sesiones con vueltas del corpus, la asimetría izquierda-derecha del
  spread correlaciona **0.85** con la direccionalidad del circuito, no con el camber.
  Atribuyendo la carga por **temperatura** —la goma que trabaja es la que se calienta—
  la rueda cargada tiene spread mediano **+5.0 °C** y la descargada **+8.2**.
  La física cierra: la goma de afuera es la cargada, el rolido se come su camber
  negativo estático y le calienta el hombro **exterior**, así que su spread baja. Ésa
  es la medición de camber que sirve (*¿tengo suficiente camber estático para
  sobrevivir al rolido?*). La de adentro conserva su camber, marca alto y no dice nada.
  Goiania con el Audi R8 GT3 (cargan las izquierdas: carcasa 106/114 °C contra 87/96)
  mide **FL +2 / FR +9 / RL +1 / RR +8** — las cargadas son las de +2 y +1.
  **De ahí salía el bug reportado** (*"siempre dice poco camber negativo, incluso con
  el máximo camber posible"*): la ventana absoluta original `[3,12]` estaba centrada en
  la distribución de la rueda **descargada**, así que acusaba al **27 %** de los ejes
  cargados del corpus, para siempre y sin arreglo posible por setup.
  Ahora el dash acumula qué lado carga la pista con `mLocalAcceleration[0]` y la rueda
  descargada muestra su número pero **no opina** (rombo hueco, sin banda verde).
  ⚠️ El signo se fijó **por temperatura** (corr −0.895 entre el índice de `accel_x` y
  el calor izq−der), **no** por `mSuspensionTravel`: más travel es rueda *extendida*, o
  sea descargada (corr −0.872), y deducirlo al revés invierte el veredicto entero.
  El veredicto de la rueda cargada es **auto-referencial igual que el térmico**: se
  compara (±3 °C) contra el spread con que cerró **la tanda anterior de ese mismo auto
  en esa misma pista** — persistido junto al objetivo de presión. La pista va en la
  llave y no es un detalle: el ruido inter-tanda del mismo auto es **p90 2.1 °C dentro
  de un circuito contra 4.3 mezclándolos**, así que sin cualificar por pista el
  instrumento reporta el cambio de trazado como si fuera un cambio de setup.
  Así responde lo que el piloto de verdad pregunta: *"moví el camber, ¿cambió algo?"*.
  Qué lado carga la pista se decide recién con **60 s acumulados de curva** y con
  histéresis: a los 20 s el lado que declara el índice coincide con el de la tanda
  completa sólo el 55 % de las veces (una moneda al aire) y sin histéresis el veredicto
  de dos ruedas parpadeaba decenas de veces por tanda. Sin tanda previa cae a
  una ventana de respaldo **+0…+10 °C**, sacada de la distribución del eje cargado
  (p05 −0.2 · mediana +5.2 · p95 +10.0), que acusa 6 % por abajo y 4 % por arriba.
  **Ya no existe un veredicto de "poco camber"**: el corpus no autoriza esa afirmación.
  Sólo se opina en los extremos — spread negativo (hombro exterior más caliente que el
  interior) o sobre +10.
  Dos alternativas plausibles quedaron descartadas **con medición**: normalizar el
  spread por el nivel térmico de la goma no ayuda ni entre autos (CV 0.69 → 0.67) ni
  dentro del mismo auto (desvío relativo p50 14.6 % → 16.8 %, *peor*); y segmentar por
  G lateral instantáneo tampoco, porque el modelo de bordes de AMS2 está tan filtrado
  que la mediana en curva cargada, en recta y descargada difiere menos de 0.5 °C —
  sirve el índice **acumulado** de la tanda, no un gate instantáneo.
