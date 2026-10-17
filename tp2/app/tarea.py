"""Modelo interno de una tarea y sus estados.

Una Tarea vive en memoria (en el diccionario de la Plataforma) desde que se
encola hasta que se apaga el servidor. Los workers la mutan en el lugar: no
hace falta Lock porque todo corre en el mismo hilo (el del event loop) y una
corrutina sólo cede el control en un `await` (ver README §5).
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Estado(StrEnum):
    PENDIENTE = "pendiente"
    EJECUTANDO = "ejecutando"
    COMPLETADA = "completada"
    FALLIDA = "fallida"
    CANCELADA = "cancelada"

    @property
    def terminal(self) -> bool:
        """Una tarea en estado terminal ya no cambia más."""
        return self in (Estado.COMPLETADA, Estado.FALLIDA, Estado.CANCELADA)


def _nuevo_id() -> str:
    return uuid.uuid4().hex


def _iso(instante: float | None) -> str | None:
    if instante is None:
        return None
    return datetime.fromtimestamp(instante, tz=UTC).isoformat(timespec="milliseconds")


def _diferencia(desde: float | None, hasta: float | None) -> float | None:
    if desde is None or hasta is None:
        return None
    return round(hasta - desde, 4)


@dataclass(eq=False)
class Tarea:
    tipo: str
    params: dict[str, Any]
    timeout: float
    id: str = field(default_factory=_nuevo_id)
    estado: Estado = Estado.PENDIENTE
    resultado: Any = None
    error: str | None = None
    # Fracción completada (0..1) para los tipos que la pueden reportar.
    progreso: float | None = None
    # Instantes en segundos desde epoch (time.time()).
    creada: float = field(default_factory=time.time)
    iniciada: float | None = None
    finalizada: float | None = None

    # --- Estado de ejecución: no se serializa ---------------------------
    # La asyncio.Task que corre la tarea mientras está EJECUTANDO. Es lo que
    # cancela DELETE /tareas/{id}.
    ejecucion: asyncio.Task | None = field(default=None, repr=False)
    # Por qué se canceló (usuario, apagado...). Lo setea quien cancela y lo
    # lee el worker al recibir el CancelledError.
    motivo_cancelacion: str | None = field(default=None, repr=False)
    # Se setea cuando la tarea llega a un estado terminal. Permite esperar a
    # que una cancelación termine de procesarse, sin hacer polling.
    fin: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def a_dict(self, incluir_resultado: bool = True) -> dict[str, Any]:
        """Representación JSON-serializable (para la API y para el volcado)."""
        datos = {
            "id": self.id,
            "tipo": self.tipo,
            "estado": self.estado.value,
            "params": self.params,
            "timeout": self.timeout,
            "progreso": self.progreso,
            "error": self.error,
            "creada": _iso(self.creada),
            "iniciada": _iso(self.iniciada),
            "finalizada": _iso(self.finalizada),
            "espera_s": _diferencia(self.creada, self.iniciada),
            "duracion_s": _diferencia(self.iniciada, self.finalizada),
        }
        if incluir_resultado:
            datos["resultado"] = self.resultado
        return datos
