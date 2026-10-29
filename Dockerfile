FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 mcp \
    && mkdir -p /data/vault /data/state \
    && chown -R mcp:mcp /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

USER mcp
ENV OBSIDIAN_MCP_TRANSPORT=http \
    OBSIDIAN_MCP_HOST=0.0.0.0 \
    OBSIDIAN_MCP_PORT=8000 \
    OBSIDIAN_VAULT=/data/vault \
    OBSIDIAN_MCP_STATE_DIR=/data/state \
    MPLCONFIGDIR=/tmp/matplotlib \
    PYTHONUNBUFFERED=1

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

CMD ["obsidian-mcp"]
