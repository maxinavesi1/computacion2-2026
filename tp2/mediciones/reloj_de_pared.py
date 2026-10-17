"""¿Salta el reloj de pared? Diagnóstico que explica por qué las duraciones
de las tareas se miden con time.monotonic() y no con time.time().

    docker compose run --rm --entrypoint python mediciones mediciones/reloj_de_pared.py

Durante 40 s (con una CPU ocupada, como durante una medición) compara
time.time() contra time.monotonic(). Si el desfase entre los dos cambia de
golpe, el reloj de pared saltó. En Docker Desktop sobre Windows lo hace
seguido: la VM de Linux resincroniza su hora con la de Windows (ver README).
"""

import hashlib
import threading
import time


def ocupar_cpu(segundos):
    fin = time.monotonic() + segundos
    h = b"x"
    while time.monotonic() < fin:
        h = hashlib.sha256(h).digest()


threading.Thread(target=ocupar_cpu, args=(40,), daemon=True).start()
inicial = anterior = time.time() - time.monotonic()
for i in range(160):
    # time.sleep está bien acá: este script es sincrónico (no hay event loop
    # ni corrutinas) y no forma parte de la plataforma.
    time.sleep(0.25)
    desfase = time.time() - time.monotonic()
    if abs(desfase - anterior) > 0.05:
        print(f"t={i * 0.25:5.2f} s  el reloj de pared saltó {desfase - anterior:+.3f} s")
    anterior = desfase
print(f"Diferencia acumulada entre los dos relojes en 40 s: {anterior - inicial:+.3f} s")
