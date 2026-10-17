"""La API HTTP (FastAPI).

Todos los endpoints son `async def` a propósito. FastAPI corre los `def`
comunes en un pool de hilos; si estos lo fueran, tocarían el estado de la
Plataforma desde otro hilo mientras el event loop lo modifica (y ahí sí
haría falta un Lock). Como `async def`, corren en el hilo del loop, igual
que los workers, y ninguno hace trabajo pesado: encolar es O(1).
"""

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response

from app.config import Config
from app.modelos import PedidoTarea, TareaDetalle, TareaResumen
from app.plataforma import (ColaLlena, Plataforma, PlataformaCerrada,
                            TareaNoEncontrada, TareaTerminada)
from app.tarea import Estado


def crear_app(config: Config | None = None, tipos: dict | None = None,
              recursos: Any = None) -> FastAPI:
    """Arma la app. `tipos` y `recursos` existen para inyectar dobles en los tests."""
    config = config or Config.desde_entorno()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Lo de antes del yield corre al arrancar el servidor, antes de
        # aceptar conexiones; lo de después, al apagarlo (SIGTERM/SIGINT).
        plataforma = Plataforma(config, tipos=tipos, recursos=recursos)
        await plataforma.iniciar()
        app.state.plataforma = plataforma
        try:
            yield
        finally:
            await plataforma.apagar()

    app = FastAPI(
        title="Plataforma de tareas distribuidas",
        description="TP2 de Computación II: cola de tareas con asyncio, workers y executors.",
        version="1.0.0",
        lifespan=lifespan,
    )

    def plataforma() -> Plataforma:
        return app.state.plataforma

    def buscar(tarea_id: str):
        tarea = plataforma().obtener(tarea_id)
        if tarea is None:
            raise HTTPException(404, f"no existe la tarea {tarea_id}")
        return tarea

    @app.post("/tareas", status_code=202, response_model=TareaDetalle,
              summary="Encola una tarea y devuelve su id sin esperar a que termine")
    async def crear_tarea(pedido: PedidoTarea, response: Response):
        try:
            tarea = plataforma().encolar(pedido.tipo, pedido.params.model_dump(mode="json"),
                                         pedido.timeout)
        except PlataformaCerrada as e:
            raise HTTPException(503, str(e)) from None
        except ColaLlena as e:
            raise HTTPException(503, f"cola llena: {e}", headers={"Retry-After": "5"}) from None
        # 202 Accepted: "lo recibí, todavía no lo hice". Location dice dónde
        # consultar el estado.
        response.headers["Location"] = f"/tareas/{tarea.id}"
        return tarea.a_dict()

    @app.get("/tareas", response_model=list[TareaResumen],
             summary="Lista las tareas (las más nuevas primero), con filtro por estado")
    async def listar_tareas(estado: Estado | None = None,
                            limite: int = Query(100, ge=1, le=1000)):
        # El límite no es cosmético: serializar 10.000 tareas a JSON es
        # trabajo de CPU que se hace en el hilo del loop.
        tareas = plataforma().listar(estado)[-limite:]
        return [t.a_dict(incluir_resultado=False) for t in reversed(tareas)]

    @app.get("/tareas/{tarea_id}", response_model=TareaDetalle,
             summary="Estado y resultado de una tarea")
    async def ver_tarea(tarea_id: str):
        return buscar(tarea_id).a_dict()

    @app.delete("/tareas/{tarea_id}", response_model=TareaDetalle,
                summary="Cancela una tarea pendiente o en ejecución")
    async def cancelar_tarea(tarea_id: str):
        try:
            tarea = await plataforma().cancelar(tarea_id)
        except TareaNoEncontrada:
            raise HTTPException(404, f"no existe la tarea {tarea_id}") from None
        except TareaTerminada as e:
            raise HTTPException(409, str(e)) from None
        return tarea.a_dict()

    @app.get("/estadisticas", summary="Totales por estado, tiempos promedio y workers ocupados")
    async def estadisticas():
        return plataforma().estadisticas()

    @app.get("/salud", summary="Para el healthcheck de Docker")
    async def salud():
        return {"estado": "ok", "aceptando": plataforma().aceptando}

    return app
