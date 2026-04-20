#!/usr/bin/env python3
"""
Mini-shell con redirección - Computación II 2026 - Clase 4
Soporta: >, >>, <, y comandos simples
"""
import os
import sys


def parsear_linea(linea):
    """
    Parsea una línea de comando.
    Retorna (comando, args, archivo_salida, modo_salida, archivo_entrada)

    Ejemplos:
      "ls -la"           -> ("ls", ["-la"], None, None, None)
      "ls > out.txt"     -> ("ls", [], "out.txt", "truncar", None)
      "echo hi >> f.txt" -> ("echo", ["hi"], "f.txt", "append", None)
      "cat < in.txt"     -> ("cat", [], None, None, "in.txt")
    """
    partes = linea.split()
    if not partes:
        return None, [], None, None, None

    comando = partes[0]
    args = []
    archivo_salida = None
    modo_salida = None
    archivo_entrada = None

    i = 1
    while i < len(partes):
        token = partes[i]

        if token == ">>":
            if i + 1 >= len(partes):
                print("Error: falta nombre de archivo después de >>", file=sys.stderr)
                return None, [], None, None, None
            archivo_salida = partes[i + 1]
            modo_salida = "append"
            i += 2

        elif token == ">":
            if i + 1 >= len(partes):
                print("Error: falta nombre de archivo después de >", file=sys.stderr)
                return None, [], None, None, None
            archivo_salida = partes[i + 1]
            modo_salida = "truncar"
            i += 2

        elif token == "<":
            if i + 1 >= len(partes):
                print("Error: falta nombre de archivo después de <", file=sys.stderr)
                return None, [], None, None, None
            archivo_entrada = partes[i + 1]
            i += 2

        else:
            args.append(token)
            i += 1

    return comando, args, archivo_salida, modo_salida, archivo_entrada


def ejecutar(comando, args, archivo_salida=None, modo_salida=None, archivo_entrada=None):
    """
    Ejecuta un comando con redirección opcional.

    - archivo_salida + modo_salida="truncar" -> stdout va a un archivo nuevo (>)
    - archivo_salida + modo_salida="append"  -> stdout se agrega al archivo (>>)
    - archivo_entrada                        -> stdin viene de un archivo (<)
    """
    pid = os.fork()

    if pid == 0:
        # ----- PROCESO HIJO -----

        # Redirección de SALIDA
        if archivo_salida:
            flags = os.O_CREAT | os.O_WRONLY
            if modo_salida == "append":
                flags |= os.O_APPEND
            else:
                flags |= os.O_TRUNC

            try:
                fd = os.open(archivo_salida, flags, 0o644)
            except OSError as e:
                print(f"Error al abrir '{archivo_salida}': {e}", file=sys.stderr)
                os._exit(1)

            os.dup2(fd, 1)   # stdout -> archivo
            os.close(fd)

        # Redirección de ENTRADA
        if archivo_entrada:
            try:
                fd = os.open(archivo_entrada, os.O_RDONLY)
            except OSError as e:
                print(f"Error al abrir '{archivo_entrada}': {e}", file=sys.stderr)
                os._exit(1)

            os.dup2(fd, 0)   # stdin <- archivo
            os.close(fd)

        # Ejecutar el comando
        try:
            os.execvp(comando, [comando] + args)
        except OSError as e:
            print(f"minishell: {comando}: {e}", file=sys.stderr)
            os._exit(127)

    else:
        # ----- PROCESO PADRE: esperar al hijo -----
        _, status = os.wait()
        return os.WEXITSTATUS(status)


def main():
    print("Mini-shell con redirección - Clase 4")
    print("Comandos soportados: >, >>, <   |   Escribí 'exit' para salir\n")

    while True:
        try:
            linea = input("minish$ ")
        except EOFError:
            print("\nChau!")
            break

        linea = linea.strip()
        if not linea:
            continue

        if linea == "exit":
            print("Chau!")
            break

        # Comandos built-in
        if linea.startswith("cd "):
            destino = linea[3:].strip()
            try:
                os.chdir(destino)
            except OSError as e:
                print(f"minishell: cd: {e}", file=sys.stderr)
            continue

        if linea == "pwd":
            print(os.getcwd())
            continue

        comando, args, salida, modo, entrada = parsear_linea(linea)

        if comando is None:
            continue

        ejecutar(comando, args, salida, modo, entrada)


if __name__ == "__main__":
    main()