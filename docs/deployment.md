# NAS deployment

The current development host is `ufian@192.168.1.99`; project files are in `/home/ufian/pplan`. Use the SSH key managed by your existing 1Password SSH agent. No private SSH key belongs in this repository or container.

## Initial setup

The Compose configuration uses UID/GID `1000:1000`. Adjust both services if deploying as another account. Create private writable directories before starting containers:

```sh
cd /home/ufian/pplan
umask 077
mkdir -p secrets data/bot data/codex
chmod 700 secrets data/bot data/codex
cp config.example.json config.json
nano config.json
nano secrets/telegram_bot_token
chmod 600 config.json secrets/telegram_bot_token
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
```

Do not overwrite an existing family configuration with the example. The token file contains only the Telegram token, without quotes. Compose mounts it as a secret file; it is not an environment variable or image layer. `.gitignore` and `.dockerignore` exclude secrets, runtime configuration, and data. No host port is published; outbound HTTPS is required.

`bot` holds the plan and Google token in `data/bot`. `ai` holds only its independent Codex login in `data/codex`. Both run as a non-root user with a read-only root filesystem. Container restart preserves plan data and credentials. Keep those host directories owned by the service UID; do not fix permission problems by making them world writable.

Send `/start` to the Telegram bot. `/whoami` returns your own Telegram ID, which must match `operator_telegram_id`. Do not allowlist a Telegram username; use the numeric ID.

## Codex login

```sh
docker compose run --rm --no-deps ai codex -c 'cli_auth_credentials_store="file"' login --device-auth
docker compose exec ai codex -c 'cli_auth_credentials_store="file"' login status
```

Complete the browser step yourself. Authentication is stored only in `data/codex`, which must remain writable for refreshes. The service ignores user configuration and forces ChatGPT authentication when interpreting messages. Do not mount your general-purpose personal Codex home or supply an API key. Login or quota failure disables free-text interpretation; explicit requests still work.

AI family-context transmission is disabled by default. Enable `PPLAN_AI_ENABLED=true` in the private NAS `.env` file only after consent to send request text, configured family identities, clarification history, handover policy, normal rotation patterns, and upcoming schedule to OpenAI through Codex. Then run `docker compose up -d bot` to apply the changed environment.

The Dockerfile pins Codex CLI to `0.171.0`. Update that pin deliberately and verify CLI flags and subscription inference before rebuilding. The Node runtime is present only to launch the official Codex package; application code is Python. The NAS image has been designed for standard Linux Docker hosts; verify the image build on your target CPU architecture.

## Operation

```sh
docker compose ps
docker compose logs --tail=50 bot ai
docker compose restart bot
docker compose down
```

Never run a second polling process for the same token. `docker compose run bot ...` should be used only for short administrative commands, not another `bot` loop. Health checks verify polling heartbeat and the private AI HTTP service; an AI health check does not prove ChatGPT login or model entitlement. Check login separately and make a harmless read-only schedule query after setup.

## Updating source

Keep runtime files outside the source bundle. Copy only source, docs, Docker configuration, and tests to the NAS, then:

```sh
docker compose build
docker run --rm --network none --read-only --tmpfs /tmp -v "$PWD/tests:/app/tests:ro" pplan:local python -m unittest discover -v
docker compose up -d
docker compose ps
```

No family data should be deleted during an update. The local workspace can mirror source for review, while builds and runtime validation happen on the NAS.

For development directly on the NAS, add `COMPOSE_FILE=compose.yaml:compose.dev.yaml` to the NAS's private `.env` file. This enables read-only source mounts for both services, so code edits take effect after `docker compose restart bot ai` without reinstalling dependencies. The active development deployment uses this override. Remove the variable and rebuild before a deployment that should run exclusively from the image. The app itself does not load `.env`; Docker Compose reads this setting.

## Backup and restore

Back up configuration, the SQLite database, and credentials to private encrypted storage. For an uncomplicated consistent snapshot, stop the bot first, copy the complete `data/bot` directory including any SQLite WAL files, then restart it. Back up `data/codex` only to equally private storage; it contains account credentials.

To restore, stop the bot, restore the configuration and matching complete database directory, restore permissions/ownership, and restart. Preserve the database's plan ID so Calendar event identities remain stable. A stale backup may replay later Telegram messages or omit changes; compare the restored schedule before resuming normal use. Do not restore an unrelated database into an existing Calendar connection.
