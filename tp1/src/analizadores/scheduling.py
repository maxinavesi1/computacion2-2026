"""
Analizador "scheduling": todo lo relacionado al scheduler de Linux para cada
proceso: nice, priority, policy (OTHER/FIFO/RR/BATCH/IDLE), rt_priority,
CPU affinity, context switches voluntarios/involuntarios, utime/stime y
sesión/grupo de procesos.

Nota sobre Nice: la consigna ofrece "/proc/<pid>/stat campo 19 O
/proc/<pid>/status: Nice". La segunda opción no existe: /proc/<pid>/status no
tiene ni tuvo nunca una línea "Nice:" (se puede verificar con
`grep -i nice /proc/self/status`, que no devuelve nada). Los campos de status
relacionados con scheduling son Cpus_allowed_list y los ctxt_switches, que sí
usamos acá. Así que el campo 19 de stat es la única fuente real de nice, y la
alternativa que ofrece la consigna simplemente no existe.
"""

import procfs
from analizadores._base import run_loop


def _extraer(historial, cfg, verbose, pids_proxy):
    pids = list(pids_proxy)
    resultado = {}

    for pid in pids:
        stat = procfs.read_stat(pid)
        status = procfs.read_status(pid)
        if stat is None:
            continue

        resultado[pid] = {
            "pid": pid,
            "nice": stat.get("nice", 0),
            "priority": stat.get("priority", 0),
            "rt_priority": stat.get("rt_priority", 0),
            "policy_num": stat.get("policy", 0),
            "policy": procfs.policy_name(stat.get("policy", 0)),
            "cpus_allowed_list": status.get("Cpus_allowed_list", "?") if status else "?",
            "voluntary_ctxt_switches": status.get("voluntary_ctxt_switches", 0) if status else 0,
            "nonvoluntary_ctxt_switches": status.get("nonvoluntary_ctxt_switches", 0) if status else 0,
            "utime": stat.get("utime", 0),
            "stime": stat.get("stime", 0),
            "sid": stat.get("session", 0),
            "pgid": stat.get("pgrp", 0),
        }
    return resultado


def run(pids_proxy, snapshot, interval_value, stop_event, log_queue, **kwargs):
    run_loop("scheduling", stop_event, interval_value, snapshot, log_queue,
              _extraer, pids_proxy=pids_proxy, **kwargs)
