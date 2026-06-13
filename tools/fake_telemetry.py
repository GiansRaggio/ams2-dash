#!/usr/bin/env python3
"""Emisor de telemetria AMS2 sintetica para iterar el dashboard sin estar en pista.

Genera paquetes UDP con el layout del protocolo Project CARS 2 (los tres tipos
que bridge.py parsea: telemetry=0, timings=3, time-stats=7) y los manda al
puerto 5606 en localhost. Sirve para revisar/ajustar la UI y validar offsets.

Uso:
    python tools/fake_telemetry.py            # frame "manejando fuerte" fijo
    python tools/fake_telemetry.py --shift    # en zona de cambio (LEDs strobe)
    python tools/fake_telemetry.py --sweep     # rpm oscilando, para ver animacion
"""
import argparse
import socket
import struct
import time

PORT = 5606
VIEWED = 0  # participante que estamos "manejando"


def telemetry(rpm, max_rpm, gear, throttle, brake, speed_kmh, fuel_frac, cap):
    b = bytearray(60)
    b[10] = 0                       # packet type
    b[12] = VIEWED                  # viewedParticipantIndex (signed)
    b[28] = cap                     # fuel capacity
    b[29] = round(brake / 100 * 255)
    b[30] = round(throttle / 100 * 255)
    struct.pack_into("<f", b, 32, fuel_frac)        # fuel level (fraccion)
    struct.pack_into("<f", b, 36, speed_kmh / 3.6)  # speed en m/s
    struct.pack_into("<H", b, 40, rpm)
    struct.pack_into("<H", b, 42, max_rpm)
    b[45] = gear & 0x0F             # 15 = R, 0 = N
    return bytes(b)


def timings(num, event_remaining, ahead, behind, position, lap, current_time):
    b = bytearray(1063)
    b[10] = 3
    b[12] = num
    struct.pack_into("<f", b, 17, event_remaining)
    struct.pack_into("<f", b, 21, ahead)
    struct.pack_into("<f", b, 25, behind)
    base = 33 + VIEWED * 32
    b[base + 14] = position & 0x7F
    b[base + 21] = lap
    struct.pack_into("<f", b, base + 22, current_time)
    return bytes(b)


def timestats(best, last):
    b = bytearray(1040)
    b[10] = 7
    base = 16 + VIEWED * 32
    struct.pack_into("<f", b, base, best)
    struct.pack_into("<f", b, base + 4, last)
    return bytes(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shift", action="store_true", help="rpm en zona de cambio")
    ap.add_argument("--sweep", action="store_true", help="rpm oscilando")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dst = (args.host, PORT)
    max_rpm = 8300
    base_rpm = 7950 if args.shift else 6800

    print(f"[fake] enviando telemetria sintetica a {args.host}:{PORT} (Ctrl+C para parar)")
    t0 = time.time()
    while True:
        t = time.time() - t0
        if args.sweep:
            # oscila entre 4000 y 8200 con periodo ~3.5s
            import math
            rpm = int(6100 + 2100 * math.sin(t * 1.8))
        else:
            rpm = base_rpm
        thr = 92 if rpm > 5000 else 40
        sock.sendto(telemetry(rpm, max_rpm, 4, thr, 0, 213, 28 / 65, 65), dst)
        sock.sendto(timings(20, 1325.0, 0.214, 1.103, 3, 5, 47.382), dst)
        sock.sendto(timestats(94.880, 95.610), dst)
        time.sleep(1 / 20)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[fake] detenido")
