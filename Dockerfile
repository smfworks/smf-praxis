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

# Optionally add extras at build time, e.g. --build-arg EXTRAS="docs,fast".
# EXTRAS is validated against the project's declared optional-dependencies set
# (see pyproject.toml [project.optional-dependencies]) and only allowed names
# are passed to pip in explicit bracket syntax. This prevents build-arg
# injection of arbitrary pip requirement syntax (CWE-88 / PRA-015). The
# "dev", "mutation", and "markitdown" extras are deliberately excluded from
# runtime images.
ARG EXTRAS=""
ENV PRAXIS_HOME=/data \
    PRAXIS_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

# Non-root runtime user; /data holds the SQLite store, knowledge base, and config.
RUN useradd --create-home --uid 10001 praxis \
    && mkdir -p /data && chown praxis:praxis /data
COPY --from=build /src/dist/*.whl /tmp/
RUN set -eu; \
    # Whitelist of extras allowed in a runtime image (mirrors pyproject.toml).
    allowed="docs artifacts mcp multimodal fast browser keyring"; \
    # Normalize EXTRAS: strip surrounding brackets/whitespace.
    clean="${EXTRAS#[}"; clean="${clean%]}"; \
    validated=""; \
    # Convert commas to spaces via tr (portable across dash/bash), then let
    # the shell word-split the result on the default IFS.
    for raw in $(echo "$clean" | tr ',' ' '); do \
        name="$(echo "$raw" | tr -d '[:space:]')"; \
        [ -n "$name" ] || continue; \
        ok=""; \
        for a in $allowed; do [ "$name" = "$a" ] && ok=1 && break; done; \
        if [ -z "$ok" ]; then \
            echo "ERROR: rejected EXTRAS value '$name'; allowed: $allowed" >&2; \
            exit 2; \
        fi; \
        if [ -n "$validated" ]; then validated="${validated},${name}"; \
        else validated="$name"; fi; \
    done; \
    whl="$(ls /tmp/*.whl)"; \
    if [ -n "$validated" ]; then \
        pip install --no-cache-dir "${whl}[${validated}]"; \
    else \
        pip install --no-cache-dir "${whl}"; \
    fi; \
    rm -f /tmp/*.whl

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
