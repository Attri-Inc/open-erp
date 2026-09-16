FROM python:3.14-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install runtime deps first (cached layer), then the package itself.
COPY pyproject.toml requirements.txt README.md ./
RUN pip install -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY run_mcp.py ./
RUN pip install --no-deps .          # registers the `openerp` console script

RUN mkdir -p /data && \
    addgroup --system app && adduser --system --ingroup app app && \
    chown -R app:app /app /data
USER app

ENV OPENERP_DB=/data/openerp.db \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8793

EXPOSE 8793

# Liveness: the server should be accepting TCP connections on MCP_PORT.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os,socket; socket.create_connection(('127.0.0.1', int(os.getenv('MCP_PORT','8793'))), 2)" || exit 1

# Seed a fresh DB on first boot if none exists, then start the MCP server.
CMD ["sh", "-c", "[ -f \"$OPENERP_DB\" ] || python scripts/seed.py; python -m src.mcp_server"]
