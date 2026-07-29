"""
vistas.py — tabla única de las 7 vistas del monitor.

Antes esta información estaba duplicada en dos lugares: display.py tenía los
intervalos mínimos por vista (para el ajuste con +/-) y main.py tenía los
intervalos por defecto (para el fallback si falta config.json). El resultado
era que SIGHUP podía bajar el intervalo de una vista por debajo del mínimo
que la propia TUI se negaba a cruzar con la tecla '-', porque cada archivo
conocía sólo la mitad de la tabla.

Ahora la tabla vive acá y la importan display.py, main.py y los tests. Los
valores son exactamente los que pide la consigna (tabla "7 vistas
alternables").
"""

# (nombre_interno, tecla_numerica, tecla_alfabetica, titulo, intervalo_default, intervalo_minimo)
VISTAS = [
    ("resumen",    "1", "r", "Resumen",          2.0,  0.5),
    ("memoria",    "2", "m", "Memoria",          3.0,  1.0),
    ("fds",        "3", "f", "File Descriptors", 5.0,  2.0),
    ("threads",    "4", "t", "Threads",          2.0,  0.5),
    ("senales",    "5", "s", "Señales",          10.0, 5.0),
    ("scheduling", "6", "p", "Scheduling",       10.0, 5.0),
    ("sistema",    "7", "g", "Sistema global",   2.0,  1.0),
]

NOMBRES = [v[0] for v in VISTAS]
TITULOS = {v[0]: v[3] for v in VISTAS}
DEFAULTS = {v[0]: v[4] for v in VISTAS}
MINIMOS = {v[0]: v[5] for v in VISTAS}

# El recolector no es una vista, pero su intervalo se configura igual que las
# demás; lo dejamos acá para que config.json tenga un único lugar de defaults.
DEFAULT_RECOLECTOR = 1.0
MINIMO_RECOLECTOR = 0.2

# tecla (numérica o alfabética) -> nombre de vista
TECLA_A_VISTA = {}
for _v in VISTAS:
    TECLA_A_VISTA[_v[1]] = _v[0]
    TECLA_A_VISTA[_v[2]] = _v[0]


def clamp_intervalo(nombre_vista, valor):
    """
    Aplica el piso mínimo de una vista. Se usa desde el Display (teclas +/-) y
    desde el reload por SIGHUP, para que las dos rutas que pueden cambiar un
    intervalo respeten exactamente el mismo límite.
    """
    return max(MINIMOS[nombre_vista], round(float(valor), 2))
