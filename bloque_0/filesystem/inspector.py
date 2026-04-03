"""Inspector de metadatos de archivos del filesystem."""
import argparse
import os
import stat
import sys
from datetime import datetime

try:
    import pwd
    import grp
except ImportError:
    pwd = None
    grp = None


def formato_bytes(n):
    """Convierte bytes a representación legible."""
    for unidad in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unidad == "TB":
            return f"{n:.2f} {unidad}" if unidad != "bytes" else f"{n} bytes"
        n /= 1024


def tipo_archivo(modo, path):
    """Retorna una descripción del tipo de archivo según su modo."""
    if stat.S_ISREG(modo):
        return "archivo regular"
    if stat.S_ISDIR(modo):
        return "directorio"
    if stat.S_ISLNK(modo):
        try:
            destino = os.readlink(path)
            if os.path.exists(path):
                return f"enlace simbólico -> {destino}"
            return f"enlace simbólico -> {destino} (roto)"
        except OSError:
            return "enlace simbólico (no se pudo resolver)"
    if stat.S_ISCHR(modo):
        return "dispositivo de caracteres"
    if stat.S_ISBLK(modo):
        return "dispositivo de bloques"
    if stat.S_ISFIFO(modo):
        return "pipe (FIFO)"
    if stat.S_ISSOCK(modo):
        return "socket"
    return "desconocido"


def permisos_legible(modo):
    """Convierte el modo numérico a rwx y octal."""
    bits = [
        (stat.S_IRUSR, "r"), (stat.S_IWUSR, "w"), (stat.S_IXUSR, "x"),
        (stat.S_IRGRP, "r"), (stat.S_IWGRP, "w"), (stat.S_IXGRP, "x"),
        (stat.S_IROTH, "r"), (stat.S_IWOTH, "w"), (stat.S_IXOTH, "x"),
    ]
    rwx = "".join(letra if modo & bit else "-" for bit, letra in bits)
    octal = oct(stat.S_IMODE(modo))[2:]
    return rwx, octal


def nombre_usuario(uid):
    """Retorna el nombre de usuario dado un uid."""
    if pwd is None:
        return str(uid)
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def nombre_grupo(gid):
    """Retorna el nombre de grupo dado un gid."""
    if grp is None:
        return str(gid)
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return str(gid)


def formato_fecha(timestamp):
    """Convierte un timestamp Unix a fecha legible."""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def fecha_creacion(info):
    """Obtiene fecha de creación si el sistema la provee."""
    if hasattr(info, "st_birthtime"):
        return formato_fecha(info.st_birthtime)
    return "no disponible en este sistema"


def inspeccionar(path):
    """Obtiene e imprime la información detallada de un archivo o directorio."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        print(f"Error: '{path}' no existe.")
        sys.exit(1)
    except PermissionError:
        print(f"Error: Sin permisos para acceder a '{path}'.")
        sys.exit(1)
    except OSError as e:
        print(f"Error: No se pudo inspeccionar '{path}': {e}")
        sys.exit(1)

    modo = info.st_mode
    rwx, octal = permisos_legible(modo)
    tipo = tipo_archivo(modo, path)
    usuario = nombre_usuario(info.st_uid)
    grupo = nombre_grupo(info.st_gid)

    print(f"Archivo:              {path}")
    print(f"Tipo:                 {tipo}")
    print(f"Tamaño:               {info.st_size} bytes ({formato_bytes(info.st_size)})")
    print(f"Permisos:             {rwx} ({octal})")
    print(f"Propietario:          {usuario} (uid: {info.st_uid})")
    print(f"Grupo:                {grupo} (gid: {info.st_gid})")
    print(f"Inodo:                {info.st_ino}")
    print(f"Enlaces duros:        {info.st_nlink}")
    print(f"Creación:             {fecha_creacion(info)}")
    print(f"Última modificación:  {formato_fecha(info.st_mtime)}")
    print(f"Último acceso:        {formato_fecha(info.st_atime)}")

    if stat.S_ISDIR(modo):
        try:
            contenido = len(os.listdir(path))
            print(f"Contenido:            {contenido} elementos")
        except PermissionError:
            print("Contenido:            (sin permisos para listar)")
        except OSError:
            print("Contenido:            (no se pudo listar)")


def main():
    """Punto de entrada."""
    parser = argparse.ArgumentParser(
        description="Muestra información detallada sobre un archivo o directorio."
    )
    parser.add_argument("path", help="Ruta del archivo o directorio a inspeccionar")
    args = parser.parse_args()
    inspeccionar(args.path)
    sys.exit(0)


if __name__ == "__main__":
    main()
