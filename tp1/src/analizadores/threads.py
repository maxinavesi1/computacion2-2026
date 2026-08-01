"""
Analizador "threads": lista de LWPs (light-weight processes, o sea threads a
nivel del kernel) de cada proceso, vía /proc/<pid>/task/<tid>/.

El historial de CPU% se guarda por thread y no por proceso, porque cada thread
tiene su propio utime/stime independiente del resto. La clave incluye además el
starttime del thread, por el mismo motivo que en los demás analizadores: los
TIDs salen del mismo espacio de números que los PIDs y también se reusan (ver
procfs.clave_proceso).
"""

import time
import procfs
from analizadores._base import por_pid, run_loop


def _extraer(historial, cfg, verbose, log, pids_proxy):
    pids = list(pids_proxy)
    ahora = time.time()
    claves_vistas = set()

    def uno(pid):
        tids = procfs.list_tids(pid)
        threads_de_este_pid = []

        for tid in tids:
            stat = procfs.read_stat(pid, tid)
            if stat is None:
                continue  # el thread murió entre el listado y la lectura
            status = procfs.read_status(pid, tid)
            comm = procfs.read_comm(pid, tid)

            clave = (pid, procfs.clave_proceso(tid, stat))
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

        if not threads_de_este_pid:
            return None  # no pudimos leer ningún thread suyo: el proceso ya no está
        return pid, threads_de_este_pid

    resultado = por_pid(pids, uno, log)

    # purgar historial de threads que ya no existen
    for clave_vieja in list(historial.keys()):
        if clave_vieja not in claves_vistas:
            del historial[clave_vieja]

    return resultado


def run(pids_proxy, snapshot, interval_value, stop_event, log_queue, **kwargs):
    run_loop("threads", stop_event, interval_value, snapshot, log_queue,
              _extraer, pids_proxy=pids_proxy, **kwargs)
