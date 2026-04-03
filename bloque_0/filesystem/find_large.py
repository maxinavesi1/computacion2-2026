"""Buscador de archivos grandes en el filesystem."""
import argparse
import os
import sys


UNIDADES = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}


def parsear_tamano(texto):
    """Convierte una cadena como '1M' o '500K' a bytes."""
    texto = texto.strip()
    if not texto:
        raise argparse.ArgumentTypeError("El tamaño no puede estar vacío.")

    if texto[-1].upper() in UNIDADES:
        try:
            return int(float(texto[:-1]) * UNIDADES[texto[-1].upper()])
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"Tamaño inválido '{texto}'. Usá un número o sufijo K, M, G (ej: 100K, 1M)."
            ) from exc

    try:
        return int(texto)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Tamaño inválido '{texto}'. Usá un número o sufijo K, M, G (ej: 100K, 1M)."
        ) from exc


def formato_bytes(n):
    """Convierte bytes a representación legible."""
    for unidad in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unidad == "TB":
            return f"{n:.1f} {unidad}" if unidad != "bytes" else f"{n} bytes"
        n /= 1024


def calcular_tamano_dir(directorio):
    """Calcula el tamaño total en bytes de todos los archivos dentro de un directorio."""
    total = 0
    for raiz, _, archivos in os.walk(directorio, onerror=lambda _: None, followlinks=False):
        for nombre in archivos:
            ruta = os.path.join(raiz, nombre)
            try:
                if not os.path.islink(ruta):
                    total += os.path.getsize(ruta)
            except OSError:
                continue
    return total


def buscar_archivos(directorio, min_bytes, tipo):
    """Recorre recursivamente el directorio y retorna elementos que superan el tamaño mínimo."""
    resultados = []
    try:
        for entrada in os.scandir(directorio):
            try:
                if entrada.is_symlink():
                    continue

                if entrada.is_dir(follow_symlinks=False):
                    if tipo in (None, "d"):
                        tamaño = calcular_tamano_dir(entrada.path)
                        if tamaño >= min_bytes:
                            resultados.append((entrada.path, tamaño))
                    resultados.extend(buscar_archivos(entrada.path, min_bytes, tipo))

                elif entrada.is_file(follow_symlinks=False):
                    if tipo in (None, "f"):
                        tamaño = entrada.stat().st_size
                        if tamaño >= min_bytes:
                            resultados.append((entrada.path, tamaño))

            except PermissionError:
                continue
            except OSError:
                continue

    except PermissionError:
        print(f"Advertencia: Sin permisos para leer '{directorio}'", file=sys.stderr)
    except OSError:
        print(f"Advertencia: No se pudo leer '{directorio}'", file=sys.stderr)

    return resultados


def main():
    """Punto de entrada: parsea argumentos y ejecuta la búsqueda."""
    parser = argparse.ArgumentParser(
        description="Busca archivos o directorios que superen un tamaño mínimo."
    )
    parser.add_argument("directorio", help="Directorio donde buscar")
    parser.add_argument(
        "--min-size",
        type=parsear_tamano,
        default=0,
        metavar="TAMAÑO",
        help="Tamaño mínimo (ej: 100K, 1M, 1G). Por defecto: 0"
    )
    parser.add_argument(
        "--type",
        choices=["f", "d"],
        dest="tipo",
        metavar="TIPO",
        help="Filtrar por tipo: f=archivo, d=directorio"
    )
    parser.add_argument(
        "--top",
        type=int,
        metavar="N",
        help="Mostrar solo los N más grandes"
    )
    args = parser.parse_args()

    if not os.path.isdir(args.directorio):
        print(f"Error: '{args.directorio}' no es un directorio válido.")
        sys.exit(1)

    if args.top is not None and args.top <= 0:
        print("Error: --top debe ser mayor que 0.")
        sys.exit(1)

    resultados = buscar_archivos(args.directorio, args.min_size, args.tipo)
    resultados.sort(key=lambda x: x[1], reverse=True)

    if not resultados:
        print("No se encontraron elementos que cumplan el criterio.")
        sys.exit(0)

    if args.top is not None:
        resultados = resultados[:args.top]
        print(f"Los {args.top} elementos más grandes:\n")
        for i, (path, tamaño) in enumerate(resultados, 1):
            print(f"  {i}. {path} ({formato_bytes(tamaño)})")
    else:
        for path, tamaño in resultados:
            print(f"{path} ({formato_bytes(tamaño)})")

    total_bytes = sum(t for _, t in resultados)
    print(f"\nTotal: {len(resultados)} elemento(s), {formato_bytes(total_bytes)}")
    sys.exit(0)


if __name__ == "__main__":
    main()
