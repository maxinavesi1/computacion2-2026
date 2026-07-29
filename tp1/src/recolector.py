"""
recolector.py — el componente "Recolector" de la arquitectura.

Su única responsabilidad es listar los PIDs vivos (leyendo los nombres de
carpeta de /proc) y publicar esa lista en `pids_proxy`, un multiprocessing
Manager().list() compartido. Los 7 analizadores leen de ahí para saber sobre
qué PIDs trabajar; así el trabajo de "listar quién existe" está centralizado
en un solo lugar (evita que 7 procesos distintos hagan os.listdir("/proc")
por su cuenta en cada refresco, y evita inconsistencias del tipo "cada
analizador ve una foto distinta de qué procesos existen").

Corre a su propio intervalo (recolector_interval en config.json), más rápido
que la vista más exigente, para que la lista de PIDs esté razonablemente
fresca sin generar carga innecesaria.
"""

import time

import procfs
import senales


def run(pids_proxy, stop_event, interval_value, log_queue):
    # Igual que los analizadores: el shutdown lo coordina el proceso
    # principal, el recolector no reacciona directamente a Ctrl+C.
    senales.reset_en_hijo()

    while not stop_event.is_set():
        t0 = time.time()
        try:
            nuevos_pids = procfs.list_pids()
            # Reemplazo atómico (una sola llamada RPC al proceso del Manager)
            # en vez de mutar elemento por elemento: evita que un analizador
            # vea la lista a medio actualizar.
            pids_proxy[:] = nuevos_pids
        except Exception as e:
            try:
                log_queue.put_nowait(f"[recolector] error listando /proc: {e!r}")
            except Exception:
                pass

        intervalo = interval_value.value
        transcurrido = time.time() - t0
        stop_event.wait(max(0.05, intervalo - transcurrido))
