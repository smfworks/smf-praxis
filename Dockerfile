# Praxis Command Deck — containerized governed agent + dashboard.
# Multi-stage: build a wheel (with the bundled web/ assets) then install it into
# a slim runtime image. The core is dependency-free, so the image stays small.
FROM python:3.12-slim AS build
WORKDIR /src
COPY . .
RUN pip install --no-cache-dir build \
    && python -m build --wheel

FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.title="praxis-agent" \
      org.opencontainers.image.source="https://github.com/smfworks/smf-praxis" \
      org.opencontainers.image.description="Hybrid autonomous AI colleague (proactive + governed)"

# Optionally add extras at build time, e.g. --build-arg EXTRAS="[docs]".
ARG EXTRAS=""
ENV PRAXIS_HOME=/data \
    PRAXIS_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

# Non-root runtime user; /data holds the SQLite store, knowledge base, and config.
RUN useradd --create-home --uid 10001 praxis \
    && mkdir -p /data && chown praxis:praxis /data
COPY --from=build /src/dist/*.whl /tmp/
RUN whl="$(ls /tmp/*.whl)" \
    && pip install --no-cache-dir "${whl}${EXTRAS}" \
    && rm -f /tmp/*.whl

USER praxis
WORKDIR /home/praxis
VOLUME ["/data"]
EXPOSE 8643

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8643/status', timeout=4)" || exit 1

# PRAXIS_HOST=0.0.0.0 makes the dashboard reachable through the mapped port.
# Until auth ships (roadmap p12), map the port to 127.0.0.1 on the host (see
# docker-compose.yml) so the unauthenticated dashboard isn't world-reachable.
CMD ["praxis", "daemon", "start", "--port", "8643"]
