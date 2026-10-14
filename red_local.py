"""Lo que el dash deja ver y hacer por la red local (bridge_shm.py y bridge.py).

El dash escucha en 0.0.0.0 para que el celular o la tablet de la misma red lo vean
(HTTP :8080, WebSocket :8765). Hasta el 13-10-2026 el HTTP servia la carpeta ENTERA
del proyecto: cualquiera en la red podia bajar /entrega.json (la credencial personal
del alumno para el servicio de la escuela), player.txt, bridge.log, telemetry/** o
.git/**. Y el WebSocket aceptaba de cualquiera los comandos del dash, entre ellos los
que apagan el servidor o la grabacion (stop_server, set_telemetry), incluso desde una
pagina web cualquiera abierta en el mismo PC: un WebSocket no tiene politica de mismo
origen, el navegador conecta a ws://localhost:8765 desde cualquier sitio.

Dos reglas:
1. HTTP: solo se sirven los archivos de ESTATICOS (lista blanca, ruta exacta); todo lo
   demas es 404. La API del visor (/api/*) no pasa por aqui.
2. WebSocket: el estado lo sigue recibiendo cualquiera de la red (es lo que muestra el
   celular), pero los COMANDOS solo se aceptan de la pagina del propio dash: Origin =
   http://<mismo host al que se conecto el WS>:<puerto HTTP>, con un host que sea una
   IP, localhost o el nombre de ESTE PC (con o sin .local). Lo ultimo cierra el DNS
   rebinding (una pagina de internet cuyo dominio apunta a la IP del PC llega con SU
   dominio en Host y Origin, y ese dominio no es confiable). Sin Origin (un script,
   no un navegador), solo desde este mismo PC.

Lo que NO cubre: alguien de la misma red que arme a mano un cliente con un Origin
falso puede seguir mandando comandos, y /api/* muestra las sesiones (con tiempos) a
quien abra el visor. Por eso el README sigue diciendo: red de casa, no WiFi publico.
"""
import http.server
import ipaddress
import socket
from urllib.parse import unquote, urlsplit

# ruta pedida -> archivo en la raiz del dash. Lo unico que piden index.html y
# analisis.html ademas de /api/* y del WebSocket (si una pagina suma un archivo, va aca).
ESTATICOS = {
    "/": "index.html",
    "/index.html": "index.html",
    "/analisis.html": "analisis.html",
    "/nosleep.mp4": "nosleep.mp4",   # el video mudo que mantiene despierta la pantalla del celular
}


def ruta_estatica(path_http):
    """Archivo (relativo a la raiz del dash) que se sirve para esa ruta, o None (= 404).

    Se compara la ruta YA decodificada contra la lista, exacta: sin normalizar puntos,
    barras ni mayusculas, asi /%2e%2e/entrega.json, /ENTREGA.JSON, /./entrega.json,
    /entrega.json::$DATA o un nombre corto 8.3 de Windows no calzan con nada.
    """
    try:
        ruta = unquote(urlsplit(path_http).path, errors="strict")
    except (ValueError, UnicodeDecodeError):
        return None
    return ESTATICOS.get(ruta)


def host_confiable(nombre):
    """Un host que una pagina ajena no puede poner en Host/Origin con DNS rebinding.

    Solo el nombre de ESTE PC (con o sin .local), no cualquier .local: otro equipo de la
    red podria anunciar su propio nombre .local por mDNS y montar el rebinding con el.
    """
    if not nombre:
        return False
    n = nombre.lower().rstrip(".")
    try:
        ipaddress.ip_address(n)
        return True
    except ValueError:
        pass
    propio = socket.gethostname().lower()
    return n in ("localhost", propio, propio + ".local")


def _es_local(remoto):
    """La conexion viene de este mismo PC (loopback, tambien IPv4 mapeada en IPv6)."""
    try:
        ip = ipaddress.ip_address(str(remoto[0]).split("%")[0])
    except (TypeError, ValueError, IndexError):
        return False
    mapeada = getattr(ip, "ipv4_mapped", None)
    return (mapeada or ip).is_loopback


def comandos_permitidos(ws, http_port):
    """True si esta conexion WebSocket puede mandar comandos al dash (ver regla 2).

    Nunca lanza: una cabecera repetida (websockets.MultipleValuesError, un LookupError)
    o un handshake raro deja la conexion en solo lectura, no tumba el handler.
    """
    try:
        headers = ws.request.headers
        remoto = ws.remote_address
        origin = headers.get("Origin")
        host = headers.get("Host") or ""
    except (AttributeError, LookupError):
        return False
    if origin is None:
        return _es_local(remoto)
    try:
        o = urlsplit(origin)
        host_ws = urlsplit("//" + host).hostname
        puerto = o.port or 80
    except ValueError:
        return False
    if o.scheme != "http" or not o.hostname or o.hostname != host_ws or puerto != http_port:
        return False
    return host_confiable(o.hostname)


def origen(ws):
    """El Origin del handshake, para el log de comandos ignorados."""
    try:
        return ws.request.headers.get("Origin") or "sin Origin"
    except (AttributeError, LookupError):
        return "?"


class SoloEstaticos(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler que solo entrega ESTATICOS (GET y HEAD pasan por send_head)."""

    def send_head(self):
        archivo = ruta_estatica(self.path)
        if archivo is None:
            self.send_error(404, "No encontrado")
            return None
        self.path = "/" + archivo          # "/" va directo a index.html: nunca un listado de carpeta
        return super().send_head()
