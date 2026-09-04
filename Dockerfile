# Two targets, one file.
#
#   docker build .                  -> `runtime`, the default: wheel only, no source
#   docker build --target dev .     -> `dev`: editable install, reloads on change
#
# docker-compose.yml builds `dev` and bind-mounts ./src over it, so editing a file
# on the host restarts the gateway inside the container. CI and releases build the
# default target, which carries no source tree and no test dependencies.

# ---- dependencies, built once and shared ------------------------------------
FROM python:3.12-slim AS build

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# secrets, not just redis: the console stores account credentials encrypted, so
# without cryptography every "add account" in a container fails at the first press.
RUN pip install --no-cache-dir --prefix=/install ".[redis,secrets]"


# ---- development ------------------------------------------------------------
# Editable, so the bind-mounted ./src is what actually executes. The dev extra
# brings pytest and watchfiles: this is also the image you exec into to run the
# suite against the running stack.
FROM python:3.12-slim AS dev

RUN useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin tokenbiryani

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[dev,redis,secrets]" \
 && install -d -o tokenbiryani -g tokenbiryani /state \
 && chown -R tokenbiryani:tokenbiryani /app

USER tokenbiryani
EXPOSE 8787
ENV TOKENBIRYANI_HEALTH_PORT=8787 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request\ntry:\n    port = os.environ.get('TOKENBIRYANI_HEALTH_PORT', '8787')\n    url = 'http://127.0.0.1:' + port + '/healthz'\n    sys.exit(0 if urllib.request.urlopen(url, timeout=2).status == 200 else 1)\nexcept Exception:\n    sys.exit(1)"]

ENTRYPOINT ["tokenbiryani"]
CMD ["serve", "--host", "0.0.0.0", "--reload", "--reload-dir", "/app/src"]


# ---- runtime (default target) -----------------------------------------------
FROM python:3.12-slim AS runtime

# A gateway holding live credentials has no business running as root.
RUN useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin tokenbiryani

COPY --from=build /install /usr/local

# Owned by the runtime user. With TOKENBIRYANI_SECRET_KEY unset the gateway writes
# its credential-encryption key here, and a root-owned /app turns the first account
# anyone adds into a PermissionError.
#
# That fallback key lives and dies with the container, which would leave accounts
# stored in a shared Redis undecryptable after a recreate — so set
# TOKENBIRYANI_SECRET_KEY for anything but a throwaway run. See docker-compose.yml.
RUN install -d -o tokenbiryani -g tokenbiryani /app /state
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
