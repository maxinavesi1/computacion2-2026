"""Persistencia mínima: volcar el estado a JSON al apagar, y encontrar
volcados anteriores al arrancar.

Son funciones sincrónicas (I/O de disco común). Desde el event loop se llaman
con `asyncio.to_thread(...)` para no bloquearlo mientras se escribe.
"""

import json
import os
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from app.tarea import Tarea

PATRON = "estado_*.json"


def volcar_estado(tareas: Iterable[Tarea], directorio: Path,
                  ahora: datetime | None = None) -> Path:
    """Escribe todas las tareas en `directorio/estado_<timestamp>.json`.

    La escritura es atómica: se escribe un archivo temporal y después se
    renombra con os.replace(). Si el proceso muere a mitad de camino, queda
    el temporal pero nunca un estado_*.json cortado por la mitad.
    """
    ahora = ahora or datetime.now(UTC)
    directorio.mkdir(parents=True, exist_ok=True)
    tareas = [t.a_dict() for t in tareas]
    contenido = {
        "volcado_en": ahora.isoformat(timespec="seconds"),
        "cantidad": len(tareas),
        "tareas": tareas,
    }
    ruta = directorio / f"estado_{ahora:%Y%m%d-%H%M%S}.json"
    temporal = ruta.with_suffix(".json.tmp")
    temporal.write_text(json.dumps(contenido, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporal, ruta)
    return ruta


def buscar_volcados(directorio: Path) -> list[Path]:
    """Volcados existentes, del más viejo al más nuevo (el nombre ordena por fecha)."""
    if not directorio.is_dir():
        return []
    return sorted(directorio.glob(PATRON))


def resumir_volcado(ruta: Path) -> dict[str, int]:
    """Cuenta las tareas de un volcado por estado, más el total."""
    try:
        tareas = json.loads(ruta.read_text(encoding="utf-8"))["tareas"]
        por_estado = Counter(t["estado"] for t in tareas)
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ValueError(f"no se pudo leer el volcado {ruta.name}: {e}") from e
    return {"total": len(tareas), **por_estado}
