# Echo (reproducción)

## Qué es

**Echo** graba voz de **grupo** entrante y la reproduce en el TG **9990**. Corre como **PEER** conectado al master **ECHO** del peer server principal (bridge TG 9990).

El runtime de playback forma parte de **`adn-server`**; ejecútalo con **`adn-server.py --echo`** y un **`adn-echo.yaml`** mínimo.

## Configuración

Usa un **YAML aparte y mínimo** — solo lo que el PEER necesita para unirse a **ECHO** en `adn-server.yaml`:

| Campo | Rol |
|-------|------|
| `GLOBAL.SERVER_ID` | Identidad de red del echo (suele ser `9990`) |
| `LOGGER` | Archivo de log (opcional pero recomendado) |
| `SYSTEMS.ECHO` | `MODE: PEER`, `IP`/`PORT` local, `MASTER_IP`/`MASTER_PORT`, `PASSPHRASE`, `RADIO_ID`, `CALLSIGN`, `OPTIONS` |

No hace falta `PROXY`, `ALIASES` ni `REPORTS`. **`MASTER_PORT`** y **`PASSPHRASE`** deben coincidir con **`ECHO`** en el servidor principal.

- Copia **`adn-echo.example.yaml`** → **`adn-echo.yaml`** (no se commitea).
- Ejecuta:

```bash
python adn-server.py --echo -c adn-echo.yaml
```

En producción suele ir en una unidad **systemd** aparte (ver `examples/systemd/adn-echo.service` en el repo), mismo binario:

```bash
sudo cp examples/systemd/adn-echo.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now adn-echo
```

## Relación con TG 9990

El servidor principal expone un master **ECHO** en **TG 9990** para el bridge de eco. El servicio **echo** independiente es un proceso aparte con su propia config que se conecta a ese master.

## Comportamiento con varios hotspots (proxy inject-only)

Cuando los hotspots se conectan a través del **proxy integrado** (`PROXY`),
varios radios pueden compartir el mismo MASTER como peers. En el legado
`adn-dmr-server` cada MASTER tenía un solo peer, así que el eco volvía
naturalmente sólo al llamante. El proxy inject-only multi-peer lo impone
explícitamente:

- **Entrega punto-a-punto.** La reproducción del eco (TG 9990) y los TG de
  servicio bajo demanda (**9991–9999**) se entregan **sólo** al peer exacto
  que originó la llamada (`RX_PEER` en el slot activo), **nunca** a otros
  hotspots del mismo usuario. No hay matching difuso del ID DMR de origen.
- **Fallback a peer único.** Cuando sólo hay un peer conectado, el paquete se
  le entrega (comportamiento legado de peer único).
- Esto aplica tanto al **plano de datos** (enrutado de audio) como al **plano
  de reportes** (`BRDG_EVENT` enviado al monitor): el monitor muestra el chip
  de eco en el hotspot originador, no en un hermano.

Ver [Enrutado de voz y contención — Gate de downlink](../development/routing-and-contention.md#gate-de-downlink-un-peer-recibe-el-paquete)
y [Proxy hotspot — Comportamiento con varios hotspots](hotspot-proxy.md#comportamiento-con-varios-hotspots).

## Documentación

Esta página es el resumen incluido en el repositorio; amplía las notas de despliegue localmente según necesites.

## Eco con informe de señal (llamada privada al 9999)

Una **llamada privada al 9999** funciona como el loro y después añade un breve informe hablado
para saber dónde está un problema:

- **Tasa de error de bits** que mide tu hotspot en tu RF (radio, antena, modulación);
- **Señal**: RSSI en tu hotspot, en dBm;
- **Pérdida de paquetes** entre tu hotspot y el master (tu conexión a internet).

Los hotspots y aplicaciones que no miden RF (DVSwitch, clientes de red) no envían BER/RSSI: el
informe lo indica y solo da las pérdidas. No se mide nada en el camino de vuelta a tu radio.

La reproducción y el informe llegan por el **TG 9, TS2**, desde el ID de voz del servidor
(`VOICE.DMR_ID`, por defecto 1000001), y solo al hotspot que llamó, como las locuciones a la
carta. El idioma sigue al país de tu DMR ID (español en los países hispanohablantes, inglés en el
resto). Las frases son `Audio/es_ES/er_*.ambe` y `Audio/en_GB/er_*.ambe`, hechas con
`tools/echo_report_clips.py` (TTS Piper y un AMBEserver); para añadir un idioma basta con darle
sus palabras en `clip_texts()` y generar sus frases. El TG **9990** no cambia.
