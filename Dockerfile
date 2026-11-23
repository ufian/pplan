FROM node:22-bookworm-slim AS codex
ARG CODEX_VERSION=0.171.0
RUN npm install --global @openai/codex@${CODEX_VERSION}

FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install --yes --no-install-recommends ca-certificates libstdc++6 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app && useradd --uid 1000 --gid app --create-home app
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && codex --version
WORKDIR /app
COPY pyproject.toml ./
COPY --chown=app:app pplan ./pplan
RUN pip install --no-cache-dir '.[google]'
USER app
CMD ["python", "-m", "pplan.cli", "--config", "/config/config.json", "--database", "/data/plan.sqlite3", "bot"]
