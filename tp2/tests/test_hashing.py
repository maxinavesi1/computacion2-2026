"""Tests de app/hashing.py: la parte CPU-bound pura, sin asyncio."""

import hashlib

from app.hashing import hash_iterado


def test_una_iteracion_es_un_sha256():
    assert hash_iterado(b"hola", 1) == hashlib.sha256(b"hola").digest()


def test_tres_iteraciones_aplica_sha256_tres_veces():
    esperado = b"hola"
    for _ in range(3):
        esperado = hashlib.sha256(esperado).digest()

    assert hash_iterado(b"hola", 3) == esperado


def test_cero_iteraciones_devuelve_la_entrada():
    assert hash_iterado(b"hola", 0) == b"hola"


def test_partir_en_bloques_da_el_mismo_resultado():
    # Esta propiedad es la que permite ejecutar `hashear` en bloques (y así
    # poder cancelarla entre bloque y bloque): hashear 1000 veces de un tirón
    # tiene que dar lo mismo que hashear 300 y seguir 700 más desde ahí.
    de_un_tiron = hash_iterado(b"semilla", 1000)
    en_bloques = hash_iterado(hash_iterado(b"semilla", 300), 700)

    assert en_bloques == de_un_tiron
