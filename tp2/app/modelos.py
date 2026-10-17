"""Esquemas de la API (Pydantic): qué se acepta y qué se devuelve.

El pedido de POST /tareas es una *unión discriminada* por el campo `tipo`:
según diga "descargar", "hashear" o "esperar", Pydantic valida `params` con
el modelo que corresponde. Un pedido inválido nunca llega a la cola: FastAPI
responde 422 con el detalle del error.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.tarea import Estado


class _Estricto(BaseModel):
    # Un campo mal escrito ("segudos") es un 422, no algo que se ignora en silencio.
    model_config = ConfigDict(extra="forbid")


# --- Parámetros por tipo ----------------------------------------------------

class ParamsDescargar(_Estricto):
    urls: list[HttpUrl] = Field(min_length=1, max_length=100,
                                description="URLs http(s) a descargar")
    concurrente: bool = Field(True, description="false = una por una (para comparar)")


class ParamsHashear(_Estricto):
    texto: str = Field(max_length=10_000)
    iteraciones: int = Field(ge=1, le=1_000_000_000, description="Veces que se aplica SHA-256")
    ejecutor: Literal["procesos", "hilos", "ninguno"] = Field(
        "procesos", description='"ninguno" BLOQUEA el event loop: sólo para la medición 2')


class ParamsEsperar(_Estricto):
    segundos: float = Field(ge=0, le=3600)


# --- Pedido de POST /tareas -------------------------------------------------

class _Pedido(_Estricto):
    timeout: float | None = Field(None, gt=0, le=86_400,
                                  description="Segundos; si falta, vale TIMEOUT_DEFECTO")


class PedidoDescargar(_Pedido):
    tipo: Literal["descargar"]
    params: ParamsDescargar


class PedidoHashear(_Pedido):
    tipo: Literal["hashear"]
    params: ParamsHashear


class PedidoEsperar(_Pedido):
    tipo: Literal["esperar"]
    params: ParamsEsperar


PedidoTarea = Annotated[PedidoDescargar | PedidoHashear | PedidoEsperar,
                        Field(discriminator="tipo")]


# --- Respuestas -------------------------------------------------------------

class TareaResumen(BaseModel):
    """Lo que devuelve el listado: todo menos el resultado (que puede ser grande)."""

    id: str
    tipo: str
    estado: Estado
    params: dict[str, Any]
    timeout: float
    progreso: float | None
    error: str | None
    creada: str
    iniciada: str | None
    finalizada: str | None
    espera_s: float | None
    duracion_s: float | None


class TareaDetalle(TareaResumen):
    resultado: Any = None
