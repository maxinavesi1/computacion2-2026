#!/usr/bin/env python3
"""Fork + exec para ejecutar ls."""
import os

print(f"Padre (PID {os.getpid()}): voy a ejecutar 'ls -la'")

pid = os.fork()

if pid == 0:
    # Hijo: transformarse en ls
    print(f"Hijo (PID {os.getpid()}): haciendo exec...")
    os.execlp("ls", "ls", "-la", "/tmp")
    # Si llegamos aquí, exec falló
    print("ERROR: exec falló")
    os._exit(1)
else:
    # Padre: esperar
    _, status = os.wait()
    codigo = os.WEXITSTATUS(status)
    print(f"\nPadre: ls terminó con código {codigo}")