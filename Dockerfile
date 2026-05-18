FROM python:3.12-slim

# Install system dependencies + curl for opencode install
RUN apt-get update && apt-get install -y \
    curl \
    bash \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install OpenCode
RUN curl -fsSL https://opencode.ai/install | bash

WORKDIR /app

# Copy project files
COPY . .

# Install Python dependencies via uv
RUN uv sync

# Mount point for PDFs — the actual folder is mounted at runtime
VOLUME ["/pdfs"]

CMD ["uv", "run", "main.py"]