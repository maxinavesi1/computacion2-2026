#!/usr/bin/env python3
"""
Ejecutor de comandos en paralelo.
Uso: python3 paralelo.py "cmd1" "cmd2" ...
"""
import os
import sys
import time


def main():
    if len(sys.argv) < 2:
        print(f"Uso: {sys.argv[0]} comando1 [comando2 ...]")
        sys.exit(1)

    comandos = sys.argv[1:]

    procesos = {}       # { pid: comando }
    inicio = time.time()

    # ── 1. Lanzar todos los hijos ──────────────────────────────────────────
    for cmd in comandos:
        pid = os.fork()

        if pid == 0:
            # ── HIJO ──────────────────────────────────────────────────────
            partes = cmd.split()          # "sleep 2" → ["sleep", "2"]
            try:
                os.execvp(partes[0], partes)  # reemplaza al hijo con el comando
            except OSError as e:
                print(f"Error ejecutando '{cmd}': {e}", file=sys.stderr)
                os._exit(127)             # código 127 = comando no encontrado
        else:
            # ── PADRE ─────────────────────────────────────────────────────
            procesos[pid] = cmd
            print(f"[{pid}] Iniciado: {cmd}")

    # ── 2. Esperar a que todos los hijos terminen ──────────────────────────
    exitosos = 0
    fallidos = 0

    while procesos:                       # mientras queden hijos sin recoger
        pid, status = os.wait()           # bloquea hasta que ALGÚN hijo termine

        cmd = procesos.pop(pid)           # sacamos el comando asociado a ese PID

        if os.WIFEXITED(status):          # terminó normalmente (no por señal)
            codigo = os.WEXITSTATUS(status)
        else:                             # terminó por señal
            codigo = -1

        print(f"[{pid}] Terminado: {cmd} (código: {codigo})")

        if codigo == 0:
            exitosos += 1
        else:
            fallidos += 1

    # ── 3. Resumen final ───────────────────────────────────────────────────
    duracion = time.time() - inicio
    total = exitosos + fallidos

    print(f"\nResumen:")
    print(f"  - Comandos ejecutados: {total}")
    print(f"  - Exitosos:            {exitosos}")
    print(f"  - Fallidos:            {fallidos}")
    print(f"  - Tiempo total:        {duracion:.2f}s")


if __name__ == "__main__":
    main()