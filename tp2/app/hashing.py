"""Trabajo CPU-bound puro de la tarea `hashear`.

Este módulo NO importa asyncio ni nada de la plataforma, a propósito: la
función se manda a ejecutar a otro proceso (ProcessPoolExecutor), y para eso
tiene que poder serializarse con pickle, que la serializa *por referencia*
("app.hashing.hash_iterado"). El proceso hijo importa este módulo para
encontrarla, así que cuanto más liviano sea, más barato es arrancar cada
proceso del pool.
"""

import hashlib


def hash_iterado(datos: bytes, iteraciones: int) -> bytes:
    """Aplica SHA-256 `iteraciones` veces: h0 = datos, h(i) = sha256(h(i-1)).

    Cumple hash_iterado(hash_iterado(x, a), b) == hash_iterado(x, a + b), lo
    que permite partir el trabajo en bloques.

    Es un bucle de Python que tiene tomado el GIL casi todo el tiempo:
    hashlib sólo suelta el GIL para entradas de 2048 bytes o más, y acá
    cada entrada son 32 bytes. Por eso, correrlo en un hilo no alcanza para
    tener paralelismo real (ver README §3.2).
    """
    h = datos
    for _ in range(iteraciones):
        h = hashlib.sha256(h).digest()
    return h
