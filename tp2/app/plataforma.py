"""El núcleo: cola de tareas + pool de workers corrutina.

    encolar() ──put_nowait──> asyncio.Queue ──get()──> worker 0..N-1
                                                          │
                                       asyncio.Task "tarea-<id>" (una por ejecución)
                                                          │
                                         tipo(params) con asyncio.timeout

Todo corre en un único hilo, el del event loop. Por eso nada de esto usa
Lock: una corrutina sólo puede ser interrumpida en un `await`, así que los
tramos de código sin await (por ejemplo "sacar de la cola, mirar el estado y
marcarla EJECUTANDO") son atómicos respecto de las demás corrutinas.
"""

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.config import Config
from app.persistencia import buscar_volcados, resumir_volcado, volcar_estado
from app.tarea import Estado, Tarea
from app.tipos import TIPOS, Recursos

log = logging.getLogger(__name__)

# Cuánto espera cancelar() a que una tarea cancelada termine de limpiar.
ESPERA_CANCELACION_S = 5.0


class PlataformaCerrada(Exception):
    """No se aceptan tareas nuevas (el servidor se está apagando)."""


class ColaLlena(Exception):
    """Hay MAX_COLA tareas esperando: se rechaza para no crecer sin límite."""


class TareaNoEncontrada(LookupError):
    pass


class TareaTerminada(Exception):
    """La tarea ya está completada, fallida o cancelada: no hay qué cancelar."""


class TiempoAgotado(Exception):
    """La tarea superó su timeout."""


def _describir(error: BaseException) -> str:
    """Texto del error para el campo `error` de la tarea.

    Los ExceptionGroup (de un TaskGroup) se abren para mostrar las causas
    reales en vez de "unhandled errors in a TaskGroup".
    """
    if isinstance(error, BaseExceptionGroup):
        return "; ".join(_describir(e) for e in error.exceptions)
    return f"{type(error).__name__}: {error}"


def _promedio(valores: list[float]) -> float | None:
    return round(sum(valores) / len(valores), 4) if valores else None


class Plataforma:
    def __init__(self, config: Config, tipos: dict[str, Callable[..., Any]] | None = None,
                 recursos: Any = None):
        self._config = config
        self._tipos = TIPOS if tipos is None else tipos
        self._recursos = recursos
        self._tareas: dict[str, Tarea] = {}
        # asyncio.Queue y no queue.Queue: su get() es una corrutina que
        # suspende al worker (y libera el loop) mientras no haya nada. El
        # get() de queue.Queue bloquea el HILO, o sea, el event loop entero.
        self._cola: asyncio.Queue[Tarea] = asyncio.Queue(maxsize=config.max_cola)
        self._workers: dict[int, asyncio.Task] = {}
        self._ocupados = 0
        self._apagando = False
        self.aceptando = False
        self.apagada = False

    # --- ciclo de vida ------------------------------------------------------

    async def iniciar(self) -> None:
        await asyncio.to_thread(self._reportar_volcados_previos)
        if self._recursos is None:
            self._recursos = Recursos.crear(self._config.procesos_cpu, self._config.max_descargas)
            if self._config.precalentar_procesos:
                await self._recursos.precalentar(self._config.procesos_cpu)
        for numero in range(self._config.workers):
            self._lanzar_worker(numero)
        self.aceptando = True
        log.info("Plataforma lista: %d workers, %d procesos para CPU, cola de %d",
                 self._config.workers, self._config.procesos_cpu, self._config.max_cola)

    def _reportar_volcados_previos(self) -> None:
        """Loguea el volcado más reciente (con el conteo por estado) y cuántos más hay."""
        volcados = buscar_volcados(self._config.dir_datos)
        if not volcados:
            return
        ultimo = volcados[-1]
        try:
            resumen = resumir_volcado(ultimo)
        except ValueError as e:
            log.warning("Volcado previo ilegible: %s", e)
        else:
            total = resumen.pop("total")
            detalle = ", ".join(f"{estado}: {n}" for estado, n in sorted(resumen.items()))
            log.info("Volcado previo encontrado: %s — %d tareas%s",
                     ultimo.name, total, f" ({detalle})" if detalle else "")
        if len(volcados) > 1:
            log.info("Hay además %d volcados anteriores en %s", len(volcados) - 1, self._config.dir_datos)

    def dejar_de_aceptar(self) -> None:
        """Primer paso del apagado. Es sólo un flag: se puede llamar desde un
        handler de señal sin riesgo (ver app/main.py)."""
        self.aceptando = False

    async def apagar(self) -> Path:
        """Apagado ordenado: no aceptar más, esperar con límite a las tareas
        en curso, cancelar las que no lleguen, parar los workers, volcar el
        estado a JSON y liberar los recursos."""
        self.dejar_de_aceptar()
        self._apagando = True  # los workers ya no arrancan tareas nuevas

        en_curso = [t for t in self._tareas.values() if t.estado is Estado.EJECUTANDO]
        if en_curso:
            log.info("Apagando: espero a %d tareas en curso (máx. %g s)",
                     len(en_curso), self._config.timeout_apagado)
        rezagadas = await self._esperar_fin(en_curso, self._config.timeout_apagado)
        for tarea in rezagadas:
            tarea.motivo_cancelacion = (f"cancelada por apagado (no terminó en "
                                        f"{self._config.timeout_apagado:g} s)")
            tarea.ejecucion.cancel()
        await self._esperar_fin(rezagadas, ESPERA_CANCELACION_S)

        # Ahora los workers están todos parados en cola.get(): se cancelan ahí.
        for worker in self._workers.values():
            worker.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)

        ruta = await asyncio.to_thread(volcar_estado, list(self._tareas.values()),
                                       self._config.dir_datos)
        await self._recursos.cerrar()
        self.apagada = True
        log.info("Apagado completo: %d tareas volcadas en %s (%d canceladas por el límite)",
                 len(self._tareas), ruta, len(rezagadas))
        return ruta

    @staticmethod
    async def _esperar_fin(tareas: list[Tarea], limite: float) -> list[Tarea]:
        """Espera hasta `limite` segundos a que terminen. Devuelve las que no."""
        try:
            async with asyncio.timeout(limite):
                for tarea in tareas:
                    await tarea.fin.wait()
        except TimeoutError:
            pass
        return [t for t in tareas if not t.fin.is_set()]

    # --- operaciones que usa la API -----------------------------------------

    def encolar(self, tipo: str, params: dict[str, Any], timeout: float | None = None) -> Tarea:
        """Registra la tarea y la deja en la cola. No espera a que se ejecute:
        por eso el POST responde al instante."""
        if not self.aceptando:
            raise PlataformaCerrada("la plataforma se está apagando")
        if tipo not in self._tipos:
            raise ValueError(f"tipo de tarea desconocido: {tipo!r}")
        tarea = Tarea(tipo=tipo, params=params, timeout=timeout or self._config.timeout_defecto)
        try:
            self._cola.put_nowait(tarea)
        except asyncio.QueueFull:
            raise ColaLlena(f"hay {self._cola.qsize()} tareas en cola") from None
        self._tareas[tarea.id] = tarea
        return tarea

    def obtener(self, tarea_id: str) -> Tarea | None:
        return self._tareas.get(tarea_id)

    def listar(self, estado: Estado | None = None) -> list[Tarea]:
        """Tareas en orden de creación, opcionalmente filtradas por estado."""
        return [t for t in self._tareas.values() if estado is None or t.estado is estado]

    async def cancelar(self, tarea_id: str, motivo: str = "cancelada por el usuario") -> Tarea:
        tarea = self._tareas.get(tarea_id)
        if tarea is None:
            raise TareaNoEncontrada(tarea_id)
        if tarea.estado.terminal:
            raise TareaTerminada(f"la tarea ya está {tarea.estado}")

        if tarea.estado is Estado.PENDIENTE:
            # No se puede sacar un elemento del medio de una asyncio.Queue: se
            # marca cancelada y el worker que la saque la va a saltear.
            tarea.estado = Estado.CANCELADA
            tarea.error = f"{motivo} (estaba en cola)"
            tarea.marcar_fin()
            tarea.fin.set()
            return tarea

        # EJECUTANDO: se cancela su asyncio.Task. El CancelledError aparece en
        # el `await` en el que esté parada (un sleep, una lectura de red, la
        # espera de un bloque del executor) y sube hasta el worker, que la
        # marca CANCELADA. Esperamos a eso para devolver el estado final.
        tarea.motivo_cancelacion = motivo
        tarea.ejecucion.cancel()
        if await self._esperar_fin([tarea], ESPERA_CANCELACION_S):
            log.warning("La tarea %s no terminó de cancelarse en %g s", tarea.id, ESPERA_CANCELACION_S)
        return tarea

    def estadisticas(self) -> dict[str, Any]:
        tareas = list(self._tareas.values())
        por_estado = Counter(t.estado for t in tareas)
        completadas = [t for t in tareas if t.estado is Estado.COMPLETADA]

        por_tipo = {}
        for tipo in sorted({t.tipo for t in tareas}):
            del_tipo = [t for t in completadas if t.tipo == tipo]
            por_tipo[tipo] = {
                "total": sum(1 for t in tareas if t.tipo == tipo),
                "completadas": len(del_tipo),
                "ejecucion_promedio_s": _promedio([t.duracion for t in del_tipo]),
            }

        return {
            "aceptando": self.aceptando,
            "tareas": {"total": len(tareas), **{e.value: por_estado[e] for e in Estado}},
            "workers": {"total": len(self._workers), "ocupados": self._ocupados},
            # en_cola incluye canceladas que todavía no se sacaron de la cola.
            "cola": {"en_cola": self._cola.qsize(), "capacidad": self._config.max_cola},
            "tiempos_promedio_s": {
                "espera": _promedio([t.espera for t in tareas if t.espera is not None]),
                "ejecucion": _promedio([t.duracion for t in completadas]),
            },
            "por_tipo": por_tipo,
        }

    # --- workers --------------------------------------------------------------

    def _lanzar_worker(self, numero: int) -> None:
        worker = asyncio.create_task(self._worker(numero), name=f"worker-{numero}")
        worker.add_done_callback(lambda w: self._al_terminar_worker(numero, w))
        self._workers[numero] = worker

    def _al_terminar_worker(self, numero: int, worker: asyncio.Task) -> None:
        """Supervisor: si un worker muere por un bug, se loguea y se reemplaza.

        Sin esto, cada excepción no prevista se llevaría un worker y el pool
        se iría achicando en silencio hasta quedar en cero.
        """
        if worker.cancelled() or self._apagando:
            return
        log.error("El worker %d murió por una excepción no capturada; lo reemplazo",
                  numero, exc_info=worker.exception())
        self._lanzar_worker(numero)

    async def _worker(self, numero: int) -> None:
        while True:
            tarea = await self._cola.get()
            try:
                # Entre el get() de arriba y estas líneas no hay ningún await:
                # nadie puede cambiar el estado de la tarea en el medio.
                if tarea.estado is not Estado.PENDIENTE:
                    continue  # se canceló mientras esperaba en la cola
                if self._apagando:
                    continue  # no se arranca nada nuevo; queda pendiente en el volcado
                await self._correr(tarea)
            finally:
                self._cola.task_done()

    async def _correr(self, tarea: Tarea) -> None:
        tarea.estado = Estado.EJECUTANDO
        tarea.marcar_inicio()
        self._ocupados += 1
        # La tarea corre en su propia asyncio.Task, separada del worker: así
        # DELETE puede cancelar la tarea sin matar al worker que la corre.
        tarea.ejecucion = asyncio.create_task(self._ejecutar(tarea), name=f"tarea-{tarea.id}")
        try:
            tarea.resultado = await tarea.ejecucion
            tarea.estado = Estado.COMPLETADA
            tarea.progreso = 1.0
        except asyncio.CancelledError:
            tarea.estado = Estado.CANCELADA
            tarea.error = tarea.motivo_cancelacion or "cancelada"
            # Si además cancelaron al worker mismo (no sólo a su tarea), hay
            # que dejar que el CancelledError siga subiendo.
            if asyncio.current_task().cancelling():
                raise
        except TiempoAgotado:
            tarea.estado = Estado.FALLIDA
            tarea.error = f"timeout: superó los {tarea.timeout:g} s"
        except Exception as e:
            log.warning("La tarea %s (%s) falló: %s", tarea.id, tarea.tipo, _describir(e))
            tarea.estado = Estado.FALLIDA
            tarea.error = _describir(e)
        finally:
            tarea.marcar_fin()
            tarea.ejecucion = None
            self._ocupados -= 1
            tarea.fin.set()

    async def _ejecutar(self, tarea: Tarea) -> Any:
        funcion = self._tipos[tarea.tipo]

        def progreso(fraccion: float) -> None:
            tarea.progreso = round(fraccion, 4)

        try:
            async with asyncio.timeout(tarea.timeout) as limite:
                return await funcion(tarea.params, self._recursos, progreso)
        except TimeoutError:
            # Sólo es "nuestro" timeout si venció NUESTRO límite; un
            # TimeoutError que venga de adentro es una falla común.
            if limite.expired():
                raise TiempoAgotado() from None
            raise
