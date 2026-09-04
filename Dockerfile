# Build the wheel and its dependencies in one stage, ship only the result.
FROM python:3.12-slim AS build

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install ".[redis]"

FROM python:3.12-slim

# A gateway holding live credentials has no business running as root.
RUN useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin tokenbiryani

COPY --from=build /install /usr/local

WORKDIR /app
USER tokenbiryani
EXPOSE 8787

# /healthz answers 503 when no account is ready, which is exactly when the container
# should be considered unhealthy. The port is read at runtime: hardcoding it means a
# config that serves on any other port is reported unhealthy forever.
ENV TOKENBIRYANI_HEALTH_PORT=8787
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request\ntry:\n    port = os.environ.get('TOKENBIRYANI_HEALTH_PORT', '8787')\n    url = 'http://127.0.0.1:' + port + '/healthz'\n    sys.exit(0 if urllib.request.urlopen(url, timeout=2).status == 200 else 1)\nexcept Exception:\n    sys.exit(1)"]

# Binding 0.0.0.0 is the point of a container, so the config must set
# server.allow_remote and at least one key or the gateway refuses to start.
ENTRYPOINT ["tokenbiryani"]
CMD ["serve", "--host", "0.0.0.0"]
