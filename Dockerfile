# Use Ubuntu 26.04, whose system Python is the CPython 3.14 baseline.
FROM ubuntu:26.04

# Prevent interactive prompts during package installation [cite: 1]
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies for PyQt5, OpenGL, Pygame, and X11
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    python3-setuptools \
    python3-wheel \
    libgl1 \
    libgl1-mesa-dri \
    libglu1-mesa \
    mesa-utils \
    libsdl2-2.0-0 \
    libdbus-1-3 \
    libpulse0 \
    libxcb-xinerama0 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-render-util0 \
    libxcb-shape0 \
    libxcb-xfixes0 \
    libxcb-xkb1 \
    libxkbcommon-x11-0 \
    libxkbcommon0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* 

# Run the editor as an unprivileged account. The container is deliberately not
# root-only: Fio executes Python plugins in-process, so container-root would
# unnecessarily widen the impact of a plugin or other Python-level compromise.
RUN groupadd --system fio \
    && useradd --system --gid fio --create-home --shell /usr/sbin/nologin fio

# Make python3 the default python 
RUN update-alternatives --install /usr/bin/python python /usr/bin/python3 1

# Set working directory 
WORKDIR /app

# Copy requirements first to leverage Docker cache 
COPY requirements.txt .

# Install Python packages into an isolated Python 3.14 environment.
RUN python3 -m venv /opt/fio-venv     && /opt/fio-venv/bin/pip install --upgrade pip     && /opt/fio-venv/bin/pip install --no-cache-dir -r requirements.txt

ENV PATH="/opt/fio-venv/bin:$PATH"

# Copy the rest of the project.
COPY . .
RUN chown -R fio:fio /app

# Default command: launch the editor.
USER fio
CMD ["python", "main.py"]
