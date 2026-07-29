"""
_base.py — bucle genérico compartido por los 7 analizadores.

Cada analizador es un proceso independiente (multiprocessing.Process) que:
  1. resetea su disposición de señales (ver senales.reset_en_hijo): el shutdown
     limpio lo coordina el proceso principal a través de `stop_event`, no
     queremos que Ctrl+C mate a los analizadores a mitad de una escritura y
     deje el snapshot inconsistente,
  2. en cada vuelta, llama a su función `extraer_fn` para calcular su
     dimensión (memoria, threads, señales, etc.) para todos los PIDs vivos,
  3. escribe el resultado en `snapshot[nombre_vista]` de un solo saque
     (una sola asignación al Manager.dict = una sola llamada RPC atómica,
     así el Display nunca ve un diccionario a medio construir),
  4. duerme el intervalo configurado (ajustable en caliente vía
     multiprocessing.Value desde el Display con +/-), despertando antes si
     se pide shutdown.

`extraer_fn` recibe siempre la misma firma:

    extraer_fn(historial, cfg, verbose, **kwargs)

  - `historial`: dict propio del analizador que sobrevive entre iteraciones
    (ej: jiffies previos por pid, para poder calcular el delta de CPU%).
  - `cfg`: la config vigente. Arranca con la que leyó main.py de config.json
    (antes arrancaba en {}, así que los valores del archivo se ignoraban hasta
    el primer SIGHUP) y se reemplaza entera cuando llega un reload por Pipe.
  - `verbose`: bool ya desreferenciado del multiprocessing.Value que togglea
    SIGUSR2. Se lee una sola vez por vuelta para que toda la iteración use un
    valor coherente, y para que los analizadores no tengan que saber que del
    otro lado hay un Value compartido.
"""

import time

import senales


def run_loop(nombre_vista, stop_event, interval_value, snapshot, log_queue,
             extraer_fn, reload_conn=None, verbose_value=None, cfg_inicial=None,
             **kwargs):
    senales.reset_en_hijo()

    historial = {}                          # estado entre iteraciones (ej: jiffies previos por pid)
    cfg = dict(cfg_inicial) if cfg_inicial else {}

    while not stop_event.is_set():
        t0 = time.time()

        # ¿Llegó una config nueva por SIGHUP? (no bloqueante)
        if reload_conn is not None:
            try:
                while reload_conn.poll():
                    cfg = reload_conn.recv()
                    _log(log_queue, f"[{nombre_vista}] config recargada por SIGHUP")
            except (OSError, EOFError):
                pass

        verbose = bool(verbose_value.value) if verbose_value is not None else False

        try:
            data = extraer_fn(historial, cfg, verbose, **kwargs)
            snapshot[nombre_vista] = {"data": data, "ts": time.time(), "verbose": verbose}
        except Exception as e:
            _log(log_queue, f"[{nombre_vista}] error inesperado en el loop: {e!r}")

        intervalo = interval_value.value
        transcurrido = time.time() - t0
        espera = max(0.05, intervalo - transcurrido)
        stop_event.wait(espera)


def _log(log_queue, mensaje):
    try:
        log_queue.put_nowait(mensaje)
    except Exception:
        pass  # si la cola de logs está llena, preferimos perder el log antes que trabarnos
