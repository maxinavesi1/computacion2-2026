"""Servidor HTTP auxiliar que simula sitios lentos.

Las mediciones de descargas contra internet dependen de la red del momento:
dos corridas seguidas pueden dar números muy distintos. Este servidor
responde con una demora y un tamaño controlados, así las mediciones son
reproducibles. En docker-compose.yml es el servicio `lento`.

    GET /lento?segundos=1&kb=10   espera y después manda todo de una vez
    GET /goteo?segundos=10&kb=100 manda el cuerpo de a 1 KB, repartido en el
                                  tiempo (para cancelar a mitad de descarga)

Se levanta con: uvicorn app.servidor_lento:app --port 8001
"""

import asyncio

from fastapi import FastAPI, Query, Response
from fastapi.responses import StreamingResponse

app = FastAPI(title="Servidor lento (auxiliar para mediciones)")

KB = 1024


@app.get("/lento")
async def lento(segundos: float = Query(1.0, ge=0, le=60),
                kb: int = Query(10, ge=0, le=10 * 1024)):
    await asyncio.sleep(segundos)
    return Response(b"x" * (kb * KB), media_type="application/octet-stream")


@app.get("/goteo")
async def goteo(segundos: float = Query(10.0, ge=0, le=600),
                kb: int = Query(100, ge=1, le=10 * 1024)):
    pausa = segundos / kb

    async def pedazos():
        for _ in range(kb):
            await asyncio.sleep(pausa)
            yield b"x" * KB

    return StreamingResponse(pedazos(), media_type="application/octet-stream",
                             headers={"Content-Length": str(kb * KB)})
