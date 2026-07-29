"""
Analizador "senales": decodifica las máscaras de bits de /proc/<pid>/status
(SigBlk, SigIgn, SigCgt, SigPnd, ShdPnd) a nombres de señales legibles.

No confundir con src/senales.py (sin el paquete "analizadores"), que son los
handlers de señales que recibe el propio monitor (self-pipe, SIGHUP, etc.).
Son cosas distintas: acá LEEMOS qué señales tiene configuradas cada proceso
del sistema; allá MANEJAMOS las señales que le llegan al monitor mismo.
"""

import procfs
from analizadores._base import run_loop

_CAMPOS = ("SigBlk", "SigIgn", "SigCgt", "SigPnd", "ShdPnd")


def _extraer(historial, cfg, verbose, pids_proxy):
    pids = list(pids_proxy)
    resultado = {}

    for pid in pids:
        status = procfs.read_status(pid)
        if status is None:
            continue

        entrada = {"pid": pid}
        for campo in _CAMPOS:
            hexval = status.get(campo, "0")
            entrada[campo.lower()] = {
                "hex": hexval,
                "nombres": procfs.decode_sigmask(hexval),
            }
        resultado[pid] = entrada
    return resultado


def run(pids_proxy, snapshot, interval_value, stop_event, log_queue, **kwargs):
    run_loop("senales", stop_event, interval_value, snapshot, log_queue,
              _extraer, pids_proxy=pids_proxy, **kwargs)
