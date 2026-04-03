"""Saludo personalizado desde la línea de comandos."""
import sys


def main():
    """Imprime un saludo al nombre recibido por argumento."""
    if len(sys.argv) < 2:
        print("Uso: saludo.py <nombre>")
        sys.exit(1)

    nombre = sys.argv[1]
    print(f"Hola, {nombre}!")
    sys.exit(0)


if __name__ == "__main__":
    main()
