# BEEHub transform agent — hardened container
#
# API key handling (three options, pick one — see README §'API key handling'):
#   Option A  APPHUB_KEY_FILE=/run/secrets/apphub_key.json   (recommended)
#             Mount your key JSON as a read-only secret:
#             --mount type=bind,source=/your/api.json,target=/run/secrets/apphub_key.json,readonly
#   Option B  Docker secret (Swarm / Compose with secrets:)
#             The secret is placed at /run/secrets/apphub_key automatically.
#   Option C  APPHUB_API_KEY=<plaintext>  (convenient for local dev only,
#             not recommended — the value appears in `docker inspect`)
#
# NEVER bake the key into the image with ENV or ARG.

# ── build stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS build
WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir uv==0.5.*
COPY pyproject.toml uv.lock* ./
RUN uv venv /venv && \
    . /venv/bin/activate && \
    uv pip compile pyproject.toml -o /tmp/requirements.txt && \
    uv pip install --no-cache -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

# ── runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Unprivileged user — agent never runs as root
RUN groupadd -g 10001 agent && \
    useradd  -u 10001 -g 10001 -M -s /sbin/nologin agent && \
    mkdir -p /input /output /app /run/secrets && \
    chown -R agent:agent /output /app
# /input  — intentionally root-owned; mounted :ro from host, agent cannot write.
# /run/secrets — created here so the mount point exists for Option A/B.

COPY --from=build /venv /venv
COPY --chown=agent:agent main.py      /app/main.py
COPY --chown=agent:agent opencode.json /app/opencode.json

ENV PATH="/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BEEHUB_INPUT=/input \
    BEEHUB_OUTPUT=/output \
    BEEHUB_DRY_RUN=1
# APPHUB_KEY_FILE and APPHUB_API_KEY are intentionally NOT set here.
# They must be supplied at `docker run` time (see README).

USER 10001:10001
WORKDIR /app

ENTRYPOINT ["python", "/app/main.py"]
CMD ["plan"]
