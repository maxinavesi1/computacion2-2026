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
     se pide shutdown y atendiendo los reloads de config que lleguen mientras
     duerme (ver la nota sobre `_TRAMO_MAX` más abajo).

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

# Trozo máximo de sueño. El loop duerme en tramos en vez de hacer un solo
# stop_event.wait(intervalo), y entre tramo y tramo mira el Pipe de reload.
#
# El motivo lo encontré probando: mandando un SIGHUP y un SIGTERM 3 segundos
# después, sólo 5 de los 7 analizadores habían registrado el reload. Los dos
# que faltaban eran Señales y Scheduling, los de intervalo 10s: estaban
# dormidos en un único wait(10) y el Pipe se miraba únicamente al principio de
# la vuelta, así que la config nueva les llegaba hasta 10 segundos tarde.
#
# Ahora la latencia de reload es <= 0.5s para los siete, sin cambiar la cadencia
# de refresco de datos (que sigue siendo el intervalo configurado). El costo son
# 2 wakeups por segundo por analizador, cada uno un sem_timedwait que vuelve por
# timeout: irrelevante al lado de parsear cientos de archivos de /proc.
_TRAMO_MAX = 0.5


def _drenar_reload(reload_conn, log_queue, nombre_vista, cfg_actual):
    """
    Aplica los reloads pendientes en el Pipe (no bloqueante).

    Devuelve la config vigente: la última recibida, o `cfg_actual` si no había
    nada. Se llama al principio de cada vuelta y también entre tramos de sueño.
    """
    if reload_conn is None:
        return cfg_actual
    cfg = cfg_actual
    try:
        while reload_conn.poll():
            cfg = reload_conn.recv()
            _log(log_queue, f"[{nombre_vista}] config recargada por SIGHUP")
    except (OSError, EOFError):
        pass  # el padre cerró su punta del Pipe: estamos apagando
    return cfg


def run_loop(nombre_vista, stop_event, interval_value, snapshot, log_queue,
             extraer_fn, reload_conn=None, verbose_value=None, cfg_inicial=None,
             **kwargs):
    senales.reset_en_hijo()

    historial = {}                          # estado entre iteraciones (ej: jiffies previos por pid)
    cfg = dict(cfg_inicial) if cfg_inicial else {}

    while not stop_event.is_set():
        t0 = time.time()

        cfg = _drenar_reload(reload_conn, log_queue, nombre_vista, cfg)
        verbose = bool(verbose_value.value) if verbose_value is not None else False

        try:
            data = extraer_fn(historial, cfg, verbose, **kwargs)
            snapshot[nombre_vista] = {"data": data, "ts": time.time(), "verbose": verbose}
        except Exception as e:
            _log(log_queue, f"[{nombre_vista}] error inesperado en el loop: {e!r}")

        # Dormir hasta completar el intervalo, en tramos, atendiendo reloads y
        # cambios de verbose.
        restante = max(0.05, interval_value.value - (time.time() - t0))
        while restante > 0:
            if stop_event.wait(min(_TRAMO_MAX, restante)):
                return  # shutdown pedido: cortamos sin terminar de dormir
            cfg = _drenar_reload(reload_conn, log_queue, nombre_vista, cfg)
            # Un SIGUSR2 corta el sueño y fuerza una vuelta ya. Sin esto, el
            # efecto de verbose tardaba hasta un intervalo completo en verse
            # (10 s en Señales y Scheduling): lo medí mandando SIGUSR2 y un
            # SIGUSR1 un segundo después, y el dump salía con verbose=False.
            # Sólo interrumpe la cadencia normal cuando el usuario realmente
            # togglea —una acción explícita y poco frecuente—, así que no
            # rompe la propiedad de que cada analizador tiene su propio ritmo.
            if verbose_value is not None and bool(verbose_value.value) != verbose:
                break
            restante -= _TRAMO_MAX


def _log(log_queue, mensaje):
    try:
        log_queue.put_nowait(mensaje)
    except Exception:
        pass  # si la cola de logs está llena, preferimos perder el log antes que trabarnos
