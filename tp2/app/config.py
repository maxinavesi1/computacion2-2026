"""Configuración de la plataforma, leída de variables de entorno.

En Docker se setean en docker-compose.yml; fuera de Docker valen los
defaults. Todas son opcionales.

| Variable             | Default        | Qué controla                                  |
|----------------------|----------------|-----------------------------------------------|
| WORKERS              | 8              | Corrutinas consumiendo la cola                |
| PROCESOS_CPU         | cantidad CPUs  | Procesos del pool para tareas CPU-bound       |
| MAX_COLA             | 1000           | Tareas pendientes máximas (después: 503)      |
| TIMEOUT_DEFECTO      | 60             | Segundos por tarea si el pedido no lo indica  |
| TIMEOUT_APAGADO      | 10             | Cuánto se espera a las tareas en curso al apagar |
| DIR_DATOS            | datos          | Dónde se escriben los estado_<timestamp>.json |
| PRECALENTAR_PROCESOS | si             | Arrancar los procesos del pool al iniciar     |
| HOST / PUERTO        | 0.0.0.0 / 8000 | Dónde escucha la API                          |
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path


def _a_bool(texto: str) -> bool:
    return texto.strip().lower() not in ("0", "no", "false", "")


def _cpus() -> int:
    return os.cpu_count() or 2


@dataclass(frozen=True)
class Config:
    workers: int = 8
    procesos_cpu: int = field(default_factory=_cpus)
    max_cola: int = 1000
    timeout_defecto: float = 60.0
    timeout_apagado: float = 10.0
    dir_datos: Path = Path("datos")
    precalentar_procesos: bool = True
    host: str = "0.0.0.0"
    puerto: int = 8000

    def __post_init__(self):
        for nombre in ("workers", "procesos_cpu", "max_cola", "timeout_defecto", "timeout_apagado"):
            if getattr(self, nombre) <= 0:
                raise ValueError(f"{nombre} tiene que ser positivo, vino {getattr(self, nombre)!r}")

    @classmethod
    def desde_entorno(cls, entorno: Mapping[str, str] | None = None) -> "Config":
        """Construye la Config a partir de variables con el nombre del campo en mayúsculas."""
        entorno = os.environ if entorno is None else entorno
        conversores = {int: int, float: float, bool: _a_bool, Path: Path, str: str}
        valores = {}
        for campo in fields(cls):
            texto = entorno.get(campo.name.upper())
            if texto is not None:
                valores[campo.name] = conversores[campo.type](texto)
        return cls(**valores)
