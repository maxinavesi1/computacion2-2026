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

    extraer_fn(historial, cfg, verbose, log, **kwargs)

  - `historial`: dict propio del analizador que sobrevive entre iteraciones
    (ej: jiffies previos por proceso, para poder calcular el delta de CPU%).
  - `cfg`: la config vigente. Arranca con la que leyó main.py de config.json
    (antes arrancaba en {}, así que los valores del archivo se ignoraban hasta
    el primer SIGHUP) y se reemplaza entera cuando llega un reload por Pipe.
  - `verbose`: bool ya desreferenciado del multiprocessing.Value que togglea
    SIGUSR2. Se lee una sola vez por vuelta para que toda la iteración use un
    valor coherente, y para que los analizadores no tengan que saber que del
    otro lado hay un Value compartido.
  - `log`: función para reportar incidencias a monitor.log, ya prefijada con el
    nombre de la vista. Se usa sobre todo con `por_pid()`.

Los analizadores recorren los PIDs con `por_pid()`, que aísla el fallo de cada
proceso para que un pid problemático no descarte la vuelta entera.
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


_MAX_ERRORES_LOGUEADOS = 3


def por_pid(pids, fn, log=None):
    """
    Itera los PIDs aislando el fallo de cada uno. Devuelve el dict de resultados.

    `fn(pid)` devuelve `(clave, valor)` para incluir en el resultado, o None
    para saltear ese PID (el caso normal: el proceso murió entre que el
    recolector lo listó y que llegamos a leerlo, o no tenemos permiso).

    El punto es que una excepción inesperada en UN pid no cueste la vuelta
    entera. Sin esto la excepción sube hasta el try/except de `run_loop`, que
    la loguea pero descarta el diccionario completo: la vista se queda con el
    snapshot del ciclo anterior y su `ts` viejo, y si el pid problemático sigue
    vivo, la vista queda congelada indefinidamente sin que nada lo indique en
    pantalla. Con el aislamiento se pierde una fila y el resto se publica.

    Sólo se loguean los primeros `_MAX_ERRORES_LOGUEADOS` de cada vuelta: si
    algo falla para todos los procesos, no queremos escribir cientos de líneas
    idénticas por ciclo en monitor.log.
    """
    resultado = {}
    fallados = 0
    for pid in pids:
        try:
            par = fn(pid)
        except Exception as e:
            fallados += 1
            if log is not None and fallados <= _MAX_ERRORES_LOGUEADOS:
                log(f"pid {pid} descartado en esta vuelta: {e!r}")
            continue
        if par is not None:
            clave, valor = par
            resultado[clave] = valor
    if log is not None and fallados > _MAX_ERRORES_LOGUEADOS:
        log(f"...y {fallados - _MAX_ERRORES_LOGUEADOS} pids más fallaron en esta vuelta")
    return resultado


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

    def log(mensaje):
        """Logger ya prefijado con el nombre de la vista, para pasarle a por_pid()."""
        _log(log_queue, f"[{nombre_vista}] {mensaje}")

    while not stop_event.is_set():
        t0 = time.time()

        cfg = _drenar_reload(reload_conn, log_queue, nombre_vista, cfg)
        verbose = bool(verbose_value.value) if verbose_value is not None else False

        try:
            data = extraer_fn(historial, cfg, verbose, log, **kwargs)
            snapshot[nombre_vista] = {"data": data, "ts": time.time(), "verbose": verbose}
        except Exception as e:
            _log(log_queue, f"[{nombre_vista}] error inesperado en el loop: {e!r}")

        # Dormir hasta t0 + intervalo, en tramos, atendiendo reloads y cambios
        # de verbose. El intervalo se RELEE en cada tramo, no se calcula una
        # sola vez: si el usuario baja Señales de 10s a 5s con la tecla '-'
        # mientras el analizador duerme, el deadline se recalcula y la vuelta
        # siguiente llega a los 5s. Antes se calculaba el restante una vez y
        # sólo se decrementaba, así que el analizador terminaba de dormir los
        # 10s viejos y el cambio recién se notaba en el ciclo siguiente.
        #
        # `fin_minimo` es un piso de 50 ms: evita que una vuelta que tardó más
        # que su propio intervalo (posible en la vista FDs con cientos de
        # procesos) convierta el loop en un busy-wait.
        fin_minimo = time.time() + 0.05
        while True:
            restante = max(t0 + interval_value.value, fin_minimo) - time.time()
            if restante <= 0:
                break
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


def _log(log_queue, mensaje):
    try:
        log_queue.put_nowait(mensaje)
    except Exception:
        pass  # si la cola de logs está llena, preferimos perder el log antes que trabarnos
