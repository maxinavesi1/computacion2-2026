FROM python:3.11-slim

WORKDIR /app

# ncurses-term trae la base de datos completa de terminfo. Lo medí dentro de
# los dos contenedores: python:3.11-slim (que sólo trae ncurses-base) tiene 42
# entradas de terminfo, y con ncurses-term pasa a 1844.
#
# Las 42 de base alcanzan para los TERM más comunes —verifiqué con
# curses.setupterm() que xterm, xterm-256color, screen-256color,
# tmux-256color, alacritty, rxvt-unicode-256color, linux, vt100 y dumb
# funcionan sin instalar nada—. Pero faltan varios de uso corriente:
# xterm-kitty, konsole-256color, st-256color, terminator, putty-256color,
# mlterm, Eterm. Si el host exporta uno de esos, curses.setupterm() tira
# "could not find terminal" y el proceso del Display muere al arrancar,
# dejando el monitor sin TUI. Son ~4 MB de imagen: los pago para que el TP
# levante en la terminal de cualquiera en vez de dejarlo como nota de
# troubleshooting.
#
# procps (ps / pgrep) NO lo usa el monitor: la consigna prohíbe expresamente
# correr ps/top desde el código y parsear su salida, y no lo hacemos en ningún
# lado (`grep -rn subprocess src/` no devuelve nada). Está para que uno pueda,
# a mano dentro del contenedor, buscar el PID de main con `pgrep -f src/main.py`
# para mandarle señales, y contrastar la salida del monitor con `ps -eLf` —
# que es exactamente lo que sugiere la sección "comandos útiles" de la consigna.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ncurses-term procps \
    && rm -rf /var/lib/apt/lists/*

# No hay dependencias de terceros: la TUI usa curses, que es stdlib. El
# requirements.txt está igual (vacío, con la justificación adentro) porque la
# consigna lo pide como parte de la estructura del repo.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

CMD ["python3", "src/main.py"]
