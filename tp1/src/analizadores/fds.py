"""
Analizador "fds": file descriptors abiertos por cada proceso.

Este analizador es el que demuestra los dos mecanismos de control en caliente
que pide la consigna, y por eso es el más "cargado" de los siete:

  - **SIGHUP / reload por Pipe**: `fds.max_mostrados` se lee de config.json y
    se puede cambiar sin reiniciar el monitor (main.py hace un broadcast de la
    config nueva por el Pipe de cada analizador).
  - **SIGUSR2 / modo verbose por Value**: la consigna define verbose como
    "más detalle en cada proceso, ej: más FDs visibles", así que es acá donde
    tiene su efecto principal. En modo normal recortamos la lista a
    `max_normal` entradas por proceso (alcanza para ver la forma de un proceso
    sin inflar el snapshot que viaja por el Manager); con verbose ON pasamos a
    `max_mostrados` y además agregamos el inodo de cada pipe/socket, que es lo
    que permite ver que dos procesos comparten el MISMO pipe (dos FDs
    distintos apuntando a pipe:[N] con el mismo N) — el ejercicio de la
    clase 5, pero sobre procesos reales.
"""

import procfs
from analizadores._base import por_pid, run_loop

_MAX_NORMAL = 20     # FDs por proceso en modo normal
_MAX_VERBOSE = 200   # tope en modo verbose si config.json no dice otra cosa


def _inodo(destino):
    """
    Extrae el número de inodo de 'pipe:[N]' / 'socket:[N]'. None si no aplica.

    Sólo pipes y sockets: son los únicos donde el número identifica un objeto
    del kernel compartido, que es lo que hace interesante mostrarlo (dos FDs
    con el mismo pipe:[N] son los dos extremos del mismo pipe). Un
    'anon_inode:[eventpoll]' también matchea la forma ':[...]' pero lo que
    tiene adentro es el tipo, no un inodo, así que lo dejamos afuera para no
    rotular como "inodo" algo que no lo es.
    """
    if not destino:
        return None
    for prefijo in ("pipe:[", "socket:["):
        if destino.startswith(prefijo) and destino.endswith("]"):
            n = destino[len(prefijo):-1]
            return n if n.isdigit() else None
    return None


def _extraer(historial, cfg, verbose, log, pids_proxy):
    pids = list(pids_proxy)
    max_verbose = cfg.get("fds", {}).get("max_mostrados", _MAX_VERBOSE)
    limite = max_verbose if verbose else min(_MAX_NORMAL, max_verbose)

    def uno(pid):
        fds = procfs.read_fds(pid)

        if fds is None:
            # El proceso murió entre que el recolector lo listó y que llegamos
            # acá. No es un error: simplemente no aparece en esta vuelta.
            return None
        if fds is procfs.SIN_PERMISO:
            return pid, {
                "pid": pid,
                "error": "sin permiso para leer /proc/<pid>/fd (proceso de otro usuario)",
                "fds": [], "total": 0, "por_tipo": {}, "truncado": False,
            }

        conteo_por_tipo = {}
        for entrada in fds:
            conteo_por_tipo[entrada["tipo"]] = conteo_por_tipo.get(entrada["tipo"], 0) + 1

        visibles = []
        for entrada in fds[:limite]:
            fila = dict(entrada)
            if verbose:
                fila["inodo"] = _inodo(entrada["destino"])
            visibles.append(fila)

        return pid, {
            "pid": pid,
            "total": len(fds),
            "por_tipo": conteo_por_tipo,
            "fds": visibles,
            "truncado": len(fds) > limite,
        }

    return por_pid(pids, uno, log)


def run(pids_proxy, snapshot, interval_value, stop_event, log_queue, **kwargs):
    run_loop("fds", stop_event, interval_value, snapshot, log_queue,
              _extraer, pids_proxy=pids_proxy, **kwargs)
