# Use a suitable base image
FROM python:3.11-slim

# Set environment variables
ENV NIXPACKS_PATH=/opt/venv/bin:$NIXPACKS_PATH

# Install system dependencies for building packages
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Create a virtual environment
RUN python -m venv /opt/venv

# Activate the virtual environment and install requirements
COPY requirements.txt /app/requirements.txt
RUN /opt/venv/bin/python -m pip install --upgrade pip && \
    /opt/venv/bin/python -m pip install -r /app/requirements.txt

# Copy your application files
COPY . /app

# Set the command to run your bot
CMD ["/opt/venv/bin/python", "/app/main.py"]
