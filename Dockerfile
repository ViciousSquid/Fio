# Use Ubuntu 22.04 as the base [cite: 1]
FROM ubuntu:22.04

# Prevent interactive prompts during package installation [cite: 1]
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies for PyQt5, OpenGL, Pygame, and X11
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-setuptools \
    python3-wheel \
    libgl1-mesa-glx \
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

# Make python3 the default python 
RUN update-alternatives --install /usr/bin/python python /usr/bin/python3 1

# Set working directory 
WORKDIR /app

# Copy requirements first to leverage Docker cache 
COPY requirements.txt .

# Install Python packages [cite: 3]
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy the rest of the project [cite: 3]
COPY . .

# Default command: launch the editor [cite: 4]
CMD ["python3", "main.py"]
