FROM python:3.11-slim

WORKDIR /app

# ncurses-term trae la base de datos completa de terminfo. La imagen "slim"
# sólo incluye ncurses-base, que cubre los TERM más comunes (xterm,
# xterm-256color, linux, screen, vt100), pero si el host exporta algo menos
# habitual —screen-256color, tmux-256color, alacritty, rxvt-unicode-256color—
# curses.initscr() falla con "setupterm: could not find terminal" y el proceso
# del Display muere al arrancar. Son ~4 MB y evitan que el TP no levante en la
# terminal de otra persona, así que lo instalamos de entrada en vez de dejarlo
# como instrucción de troubleshooting.
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
