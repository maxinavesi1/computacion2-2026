"""Búsqueda de patrones en archivos o entrada estándar, estilo grep."""
import argparse
import sys


def coincide(linea, patron, ignore_case=False):
    """Retorna True si el patrón se encuentra en la línea."""
    if ignore_case:
        return patron.lower() in linea.lower()
    return patron in linea


def procesar_lineas(nombre_fuente, lineas, patron, args, mostrar_nombre_archivo):
    """Procesa las líneas e imprime las que coinciden según los argumentos."""
    coincidencias = 0
    for numero, linea in enumerate(lineas, start=1):
        linea = linea.rstrip("\n")
        match = coincide(linea, patron, args.ignore_case)
        if args.invert:
            match = not match
        if match:
            coincidencias += 1
            if not args.count:
                prefijo = ""
                if mostrar_nombre_archivo:
                    prefijo += f"{nombre_fuente}:"
                if args.line_number or mostrar_nombre_archivo:
                    prefijo += f"{numero}:"
                if prefijo:
                    print(f"{prefijo} {linea}")
                else:
                    print(linea)
    return coincidencias


def leer_archivo(path):
    """Lee y retorna las líneas de un archivo, o termina el programa si falla."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.readlines()
    except OSError:
        print(f"Error: No se puede leer '{path}'")
        sys.exit(1)


def main():
    """Punto de entrada: parsea argumentos y ejecuta la búsqueda."""
    parser = argparse.ArgumentParser(
        description="Busca un patrón en archivos o en la entrada estándar."
    )
    parser.add_argument("patron", help="Patrón a buscar")
    parser.add_argument("archivos", nargs="*", help="Archivo(s) donde buscar")
    parser.add_argument(
        "-i", "--ignore-case",
        action="store_true",
        help="Ignora mayúsculas y minúsculas"
    )
    parser.add_argument(
        "-n", "--line-number",
        action="store_true",
        help="Muestra número de línea"
    )
    parser.add_argument(
        "-c", "--count",
        action="store_true",
        help="Muestra solo la cantidad de coincidencias"
    )
    parser.add_argument(
        "-v", "--invert",
        action="store_true",
        help="Muestra líneas que NO coinciden"
    )
    args = parser.parse_args()
    total = 0
    if args.archivos:
        multiples = len(args.archivos) > 1
        for archivo in args.archivos:
            lineas = leer_archivo(archivo)
            cant = procesar_lineas(
                archivo,
                lineas,
                args.patron,
                args,
                mostrar_nombre_archivo=multiples
            )
            total += cant
            if args.count:
                if multiples:
                    print(f"{archivo}: {cant} coincidencias")
                else:
                    print(f"{cant} coincidencias")
        if args.count and multiples:
            print(f"Total: {total} coincidencias")
    else:
        if sys.stdin.isatty():
            print("Error: Debe especificar al menos un archivo o usar stdin")
            sys.exit(1)
        lineas = sys.stdin.readlines()
        total = procesar_lineas(
            "",
            lineas,
            args.patron,
            args,
            mostrar_nombre_archivo=False
        )
        if args.count:
            print(f"{total} coincidencias")
    sys.exit(0)


if __name__ == "__main__":
    main()
