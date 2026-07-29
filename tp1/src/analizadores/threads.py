"""
Analizador "threads": lista de LWPs (light-weight processes, o sea threads a
nivel del kernel) de cada proceso, vía /proc/<pid>/task/<tid>/.

El historial de CPU% se guarda por (pid, tid) en vez de sólo por pid, porque
cada thread tiene su propio utime/stime independiente del resto.
"""

import time
import procfs
from analizadores._base import run_loop


def _extraer(historial, cfg, verbose, pids_proxy):
    pids = list(pids_proxy)
    ahora = time.time()
    resultado = {}
    claves_vistas = set()

    for pid in pids:
        tids = procfs.list_tids(pid)
        threads_de_este_pid = []

        for tid in tids:
            stat = procfs.read_stat(pid, tid)
            if stat is None:
                continue  # el thread murió entre el listado y la lectura
            status = procfs.read_status(pid, tid)
            comm = procfs.read_comm(pid, tid)

            clave = (pid, tid)
            claves_vistas.add(clave)
            jiffies_actuales = stat["utime"] + stat["stime"]
            prev = historial.get(clave)
            if prev is not None:
                jiffies_prev, wall_prev = prev
                cpu_pct = procfs.calcular_cpu_pct(jiffies_prev, jiffies_actuales, wall_prev, ahora)
            else:
                cpu_pct = 0.0
            historial[clave] = (jiffies_actuales, ahora)

            fila = {
                "tid": tid,
                "nombre": comm or stat["comm"],
                "estado": stat["state"],
                "estado_nombre": procfs.state_name(stat["state"]),
                "cpu_pct": round(cpu_pct, 1),
                "voluntary_ctxt_switches": status.get("voluntary_ctxt_switches", 0) if status else 0,
                "nonvoluntary_ctxt_switches": status.get("nonvoluntary_ctxt_switches", 0) if status else 0,
            }
            if verbose:
                # Modo verbose (SIGUSR2): agregamos en qué core corrió el
                # thread por última vez (campo 39 de stat) y sus jiffies
                # crudos, útiles para verificar a mano el cálculo de CPU%.
                fila["cpu_ultimo_core"] = stat.get("processor", -1)
                fila["utime"] = stat.get("utime", 0)
                fila["stime"] = stat.get("stime", 0)
            threads_de_este_pid.append(fila)

        if tids:  # sólo incluir el pid si pudimos leer al menos un thread suyo
            resultado[pid] = threads_de_este_pid

    # purgar historial de threads que ya no existen
    for clave_vieja in list(historial.keys()):
        if clave_vieja not in claves_vistas:
            del historial[clave_vieja]

    return resultado


def run(pids_proxy, snapshot, interval_value, stop_event, log_queue, **kwargs):
    run_loop("threads", stop_event, interval_value, snapshot, log_queue,
              _extraer, pids_proxy=pids_proxy, **kwargs)
