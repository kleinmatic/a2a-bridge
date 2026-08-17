# Mirrors the single-purpose bridge pattern: slim base, no build tooling at runtime.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

ENV A2A_BRIDGE_CONFIG=/app/agents.yml \
    A2A_BRIDGE_HOST=0.0.0.0 \
    A2A_BRIDGE_PORT=8600

EXPOSE 8600
HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=10s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://localhost:'+os.environ.get('A2A_BRIDGE_PORT','8600')+'/healthz').read()"

CMD ["python", "-m", "a2a_bridge.server"]
