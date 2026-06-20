# AMS2 Dash — Roadmap y Recomendaciones para Nivel Top-Tier

**Propósito**
Documento guía principal para el desarrollo futuro. Quien trabaje en el proyecto (Claude o tú) debe
leerlo completo **antes** de proponer o implementar cambios significativos.

Instrucciones obligatorias:
1. Leer entero `docs/ROADMAP.md`.
2. Leer `docs/EVAL.md` (rúbrica, backlog y **bitácora de iteraciones** — el estado real vive ahí).
3. Revisar el código clave sin tocarlo todavía: `tools/analyze_telemetry.py`, `ams2_strategy.py`,
   `ams2_dampers.py`, `ams2_telemetry.py`, `bridge_shm.py`, `index.html`.
4. Proponer siempre **plan minimalista de alto impacto**, fiabilidad primero, **una rebanada P0 a la
   vez validada en pista** (no el menú completo).
5. Actualizar este documento y `EVAL.md` al cerrar cada iteración importante.
6. Respetar principios: no mentir con datos stale, guardrails físicos, solo vueltas limpias,
   **español chileno** (tú/dime/maneja) en UI y comentarios.

> **Estado verificado — 2026-06-20** (detalle en `EVAL.md` it.4). Varias suposiciones de la primera
> versión de este doc ya se confirmaron o se cayeron contra telemetría real:
> - Logger a **87 canales** (no 71): +yaw/vel angular, vel local, terrain por rueda, carcass temp,
>   max_rpm/torque, abs/tc settings.
> - **Sectores arreglados** (el S1 venía roto ≈0 → la "vuelta teórica" mentía) + **rescate de sectores
>   limpios** de vueltas invalidadas (`sectors.jsonl` con validez por sector → vuelta ideal real).
> - Canales de gomas **L/C/R verificados** (center entre bordes 100%) y presión decodificada
>   (**Bar×100 → ~27 psi**): el diagnóstico de **camber/presión SÍ es construible** (beta) → sale de
>   "descartados" y pasa a P1.
> - **Bug C3 (RESUELTO, commit 004cdaf):** las recs de *clicks* de damper ahora cruzan el bottoming
>   (`tBottom`) y nunca recomiendan ablandar bump tocando fondo. Refinado por revisión adversarial.
> - Telemetría **Moza: diferida** (no aporta para tiempos; la fuerza load-cell en kg no está expuesta).

---

## Visión
Convertir ams2-dash en **el mejor sistema personal de entrenamiento y análisis para AMS2**, que te
lleve de "bastante rápido (sobre la media)" a nivel consistentemente top-tier.

Enfoque: sistematizar tu práctica · análisis profundo y accionable (no solo ver trazas) · técnicas
profesionales que hoy no explotas · tus datos confiables primero (+ referencias externas después) ·
bajo overhead (SHM), usable en pista (celular) + análisis post serio. Mantener la ventaja actual
frente a herramientas genéricas.

---

## Estado Actual

**Fortalezas reales (ya por encima de la mayoría en AMS2):**
- Dash móvil glanceable (shift lights, splits, fuel con vueltas, TC/ABS por intervención real, pit limiter).
- Grabación rica y eficiente a 50 Hz, **87 canales** (inputs unfiltered, tyre layers L/C/R, slip,
  susp vel/travel, brake temps, ride height, g's, **yaw, vel local, terrain, carcass**).
- Estrategia sofisticada (vueltas verdes, márgenes en vueltas, limitante explícito, plan manual).
- Dampers: histogramas de velocidad (low/high, bump/rebound) + travel + recomendaciones de clicks
  con guardrail C3 (bottoming → nunca ablandar bump).
- Análisis post (CLI): apex/vmin, delta por distancia vs tu best, dónde pierdes/ganas, consistencia
  por sector, coasting, **vuelta ideal por mejores sectores limpios con rescate de vueltas
  invalidadas** (recién arreglado).
- Proceso riguroso: `EVAL.md` con rúbrica, tests, harness de evaluación, niveles de grabación,
  validación de frames. Zero stutter (SHM).

**Brechas principales vs tu meta:**
- Sin comparación visual fácil contra referencias (tus mejores vueltas o externas).
- Análisis mayormente CLI y **print-based** (no devuelve estructuras) → insights requieren leer e
  interpretar a mano.
- Pocas métricas "pro" más allá de lo básico (trail braking, rotation/yaw, slip management,
  smoothness, load transfer) — pese a que **los canales ya están grabados**.
- Sin coaching automatizado que diga qué entrenar la próxima tanda.
- Sin práctica estructurada (drills por debilidad).
- Referencias externas débiles (AMS2 tiene menos comunidad que iRacing).

**Mercado (2026):** SimHub (overlays + logging), Telemetry Tool for AMS2 (Iko Rein), sim-to-motec +
MoTeC (profundo, workflow pesado). Top-tier general: Garage61 (referencias/ghosts, fuerte en
iRacing), VRS (overlays de dónde el rápido hace distinto), Coach Dave Delta (mejor all-in-one:
setups + telemetría + AI insights + video). **Tu ventaja:** rigor en estrategia/dampers/datos
propios + nativo AMS2 + cero fricción.

---

## Principios Rectores (no negociables)
1. **Fiabilidad primero**: nunca mostrar datos stale/corruptos como vivos. Guard de frames, unidades
   correctas, incertidumbre explícita.
2. **Accionable > lindo**: cada feature responde "¿qué hago distinto la próxima vuelta/stint?".
3. **Tus datos primero**: análisis profundo de tu telemetría antes que referencias externas.
4. **Guardrails físicos siempre**: dampers, fuel, bottoming, lift&coast. Nunca recomendar algo
   imposible o que rompa el auto.
5. **Extender, no duplicar**: reusar grabación, canales, estrategia, dampers, analizador, loop EVAL.
6. **Minimalismo de alto impacto**: la mejora más chica que cierre la brecha real; una rebanada por tanda.
7. **Loop cerrado**: toda feature tiene forma de evaluarse en `EVAL.md` y probarse en pista.
8. **Compatibilidad**: facilitar export a MoTeC (los 87 canales ya cubren lo que MoTeC quiere).
9. **Chileno**: UI y comentarios en español chileno (tú/dime/maneja); nada de voseo.

---

## Prioridades (impacto en llegar a top-tier)

**P0 — Fundamentales (primero)**
- **Motor de insights accionables, CLI-first** (veredicto + acción + magnitud) sobre lo que ya
  calculas. *Es la palanca de tiempos de mayor ROI; el texto accionable vale más que la UI linda.*
- Refactor del analyzer para que **devuelva estructuras** (no solo prints) — prerequisito de insights y UI.
- **Referencias propias** (mejor vuelta por auto/pista) + `compare_laps(ref, target)` estructurado.

**P1 — Alto impacto técnico**
- **Métricas pro desde los canales que ya grabas**: trail braking (brake∩steer + desaceleración
  sostenida), rotation (yaw rate en apex vs vmin/slip), slip management (picos de tyre_slip por rueda),
  smoothness (tasa de cambio de throttle/steer), load transfer (susp vel + ride height + accel lateral).
- ✅ **Diagnóstico de gomas (beta)**: presión térmica (centro vs bordes) + camber por rueda + asimetría izq/der (`--tyres`), etiquetado beta. *(pendiente: integrarlo en la UI del dash)*
- Delta bar / ghost live vs best propio.
- Práctica estructurada (focus stint, targets por sector).

**P2 — Pulido y ecosistema**
- **UI de análisis** (página o herramienta) — *después* del motor de insights, no antes.
- Export MoTeC / formatos interoperables.
- Import simple de laps externos.
- Análisis en vivo más rico + TTS accionable. Video (más adelante).

**Descartados / diferidos:**
- ~~Presión de gomas en vivo~~ → **reactivado** (decodificada Bar×100, mostrar beta).
- Coaching contra datos pro externos de AMS2 (poca disponibilidad comunitaria).
- Integración Moza (no aporta para tiempos; eventual módulo opcional "Garage" solo para calibrar pedal).
- Features que requieran cambios grandes en el juego.

---

## Recomendaciones Detalladas por Área

### 1. Análisis y comparación de vueltas (P0)
Ya tienes buena detección de curvas y delta por distancia en el CLI. Falta volverlo usable y repetible.
- **Gestión de laps de referencia**: guardar "mejor vuelta personal" por (track + car + compound);
  marcar manualmente "referencia de sesión / de setup"; cargar referencia externa (CSV simple / MoTeC).
- **Comparación**: delta acumulado por distancia (lógica casi lista), overlay de trazas
  (speed/throttle/brake/steer), mapa de pista con calor de delta, tabla por curva (vmin, entrada, salida,
  tiempo perdido).
- Extender `analyze_telemetry.py`: `compare_laps(ref, target)` que devuelva **estructura** (no print),
  time-loss breakdown por fase (entry/apex/exit), detección de coasting de baja calidad.
- Éxito: tras una tanda ves en <30s "perdí 0.35s en T3 por entrada tardía y vmin -3 km/h".

### 2. Motor de insights accionable (P0 — la pieza clave)
Es lo que más te sube de nivel (lo que hacen Delta y VRS).
- Toma salida del analizador + estrategia + dampers y produce **2-4 recomendaciones por stint**,
  formato **"veredicto + acción + magnitud"**, priorizadas por tiempo perdido real:
  - "Entrada T4: frena 8-12 m después. Pierdes 0.18s por entrada conservadora."
  - "Apex T2: llevas 4 km/h menos que tu ref. Gira antes y mantén más velocidad."
  - "Coasting excesivo saliendo de T6 (42 m). Levanta más tarde."
  - "Dampers: asimetría FL/FR en low-speed bump. Revisa 1 click."
- Guardrails: no contradecir fuel/gomas/dampers. Reglas/heurísticas primero (como ya haces en
  dampers); IA después si quieres. **CLI-first**; UI recién en P2.

### 3. Live experience (móvil + PC)
Fortaleza actual; potenciarla para práctica activa.
- Delta bar live (vs best de sesión o referencia cargada).
- Indicadores de "punto de referencia" ("frenada T3 — +0.08s vs ref").
- Modo "Focus Stint": eliges una curva/fase y el dash resalta solo lo relevante + feedback post-vuelta.
- TTS: solo eventos de alto valor, cooldowns inteligentes, no hablar en curva. Mantener glanceable.

### 4. Métricas de técnica pro (los canales ya están)
- Trail braking: overlap de brake + steer + desaceleración sostenida.
- Rotation / mid-corner: yaw rate en apex, relación con vmin y slip.
- Slip management: picos de tyre_slip por rueda y cómo los controlas.
- Smoothness de throttle y steering: tasa de cambio, jitter.
- Load transfer: susp vel + ride height + accel lateral correlacionados.
- Brake bias real vs setup; consistencia de trazado (frenada repetida, apex estable).
- **Gomas (beta): camber/presión por spread L/C/R** (center entre bordes verificado; presión Bar×100).
  Dirección "probable", banda de confianza por nº de vueltas, sin grados/psi como medidos.
- Extender `_corners`, agregar detectores, exponer en report + insights.

### 5. UI de análisis post-stint (P2, después de insights)
- Página/vista "Análisis" en el dash (o herramienta web liviana), reusando el estilo actual.
- Selector de vueltas + referencia, gráficos simples (delta vs distancia, speed/throttle/brake overlay),
  mapa de pista (SVG/canvas), lista de insights priorizados, botón "Guardar como referencia".
- Mantener el CLI para scripting/power users.

### 6. Entrenamiento estructurado
- "Modo Práctica Dirigida": defines objetivo (ej. "mejorar consistencia de S2", "reducir coasting en
  salida de T6"). Targets por sector. Resumen de stint que mide si cumpliste el foco. Historial simple JSON.

### 7. Ecosistema e interoperabilidad
- Exportador a MoTeC i2 (o formato que sim-to-motec consuma fácil) → acceso al ecosistema MoTeC cuando
  quieras. Import básico de laps para referencias externas manuales.

### 8. Fiabilidad, testing y proceso (vas bien, mantenlo)
- Extender la rúbrica de `EVAL.md` con ejes nuevos (Insights, Referencias, Técnica Pro, Live Delta).
- Toda feature nueva con asserts en `tools/test_*.py`. Mantener los 3 niveles de grabación.
- Guardrails explícitos en cada recomendación. **Primer ítem concreto: arreglar el bug C3 del
  guardrail de dampers (cruzar `tBottom` en `_recommend`).**

---

## Roadmap por Fases

**Fase 0 — Consolidación (en curso)**
- ✅ Sectores arreglados + rescate (`sectors.jsonl`, vuelta ideal).
- ✅ Bug **C3** del guardrail de dampers arreglado (`_recommend` cruza `tBottom`; refinado por revisión adversarial).
- 🔄 **Refactor del analyzer → estructuras**: iniciado (`clean_laps`/`sectors_struct`/`corners_vs_struct`/`coasting_struct`/`build_insights` devuelven dicts).
- ✅ Referencias propias: guardar/cargar mejor vuelta por auto+pista (`--save-ref`) + insights vs benchmark (R2-ref sector con 1 vuelta, R1-ref vmin por curva con 2). *(pendiente: overlay visual de traza vs ref, UI/P2)*
- Actualizar `EVAL.md`.

**Fase 1 — Insights + comparación (alto impacto inmediato)**
- ✅ Motor de insights v1 (R2 peor sector, R1 vmin, R3 coasting; CLI `--insights`, guards anti-FP).
- Métricas pro: trail braking, rotation/yaw, slip.
- Gomas beta (camber/presión spread).
- Probar en pista: ¿identifico y corrijo 1 debilidad clara por sesión?

**Fase 2 — Coaching + live**
- Insights más ricos + priorización por tiempo real perdido.
- Delta bar / ghost live, modo Focus Stint, tracking de consistencia.

**Fase 3 — UI + ecosistema**
- Página de análisis (overlays de trazas, mapa, delta por distancia, tabla por curva).
- Export MoTeC, import de referencias externas.

**Fase 4 (futuro)**
- Video + data overlay, compartir laps de forma controlada, integración con setups.

---

## Cómo pedirle a Claude que trabaje con esto
```
Lee completo:
- C:\Users\gians\sim\ams2-dash\docs\ROADMAP.md
- C:\Users\gians\sim\ams2-dash\docs\EVAL.md   (rúbrica + bitácora = estado real)

Revisa el código actual (analyze_telemetry.py, ams2_*.py, index.html) sin tocar nada todavía.

Propón un plan para la fase/ítem P0 que elijas, siguiendo los principios del ROADMAP:
minimalista de alto impacto, fiabilidad primero, extender lo existente, español chileno.
Incluye: archivos a tocar, orden de cambios, tests/rúbrica nuevos, y cómo se mide el éxito en pista.
Actualiza EVAL.md y ROADMAP al cerrar.
```

---

## Referencias de Mercado (inspiración, no copiar)
- Garage61: comparación de laps, ghosts, time-loss breakdown.
- VRS: overlays claros de "dónde el rápido hace distinto".
- Coach Dave Delta: Auto Insights que convierten data en acción + video.
- MoTeC i2: profundidad de análisis (referencia de qué métricas importan).
- Dampers y estrategia actuales ya están en nivel alto — mantener esa ventaja (corrigiendo el bug C3).

---

**Última actualización:** 2026-06-20 — revisión contra telemetría real (ver `EVAL.md` it.4).
Documento vivo: actualízalo tras cada iteración importante.
