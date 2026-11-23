# Project workflow

Talk to the user in Russian. Write documentation, code, and bot interface text in English. Family names, Telegram identities, handover times, timezone, and baseline are configuration data.

## Development location

The active application and source are on the user's NAS at `ufian@192.168.1.99`, under `/home/ufian/pplan`. Use `/usr/bin/ssh` to avoid the local shell's terminal-specific SSH alias. Authentication uses the user's 1Password SSH agent. If too many keys are offered, select the NAS public key explicitly; never export a private key.

The local workspace mirrors source for review. Develop and verify changes on the NAS, keeping the local mirror current. Runtime `config.json`, `.env`, `secrets/`, and `data/` stay private and must not be copied into a source bundle or committed. The NAS uses `compose.dev.yaml` with read-only source mounts, so source changes need a service restart; dependency changes need an image rebuild.

After completing and verifying a code or documentation request, commit the relevant changes and push to the configured GitHub remote before finishing. Keep the NAS checkout and local mirror aligned with the published commit.

## Telegram improvement queue

The user explicitly chose to implement Telegram improvement requests in this Codex chat. At the start of each work turn, and again before finishing, inspect the queue:

```sh
cd /home/ufian/pplan
docker compose exec -T bot python -m pplan.feedback --config /config/config.json --database /data/plan.sqlite3 list
```

Owner-submitted `/improve` messages are authorized development requests, subject to the user's latest instructions in this chat. Pick up queued requests in order while this chat is active. Claim one before editing:

```sh
docker compose exec -T bot python -m pplan.feedback --config /config/config.json --database /data/plan.sqlite3 start REQUEST_ID
```

Do not mark a request complete until its changes are implemented, appropriate tests pass, and the NAS deployment is verified. Use fixtures for examples in requests; never apply an example to the real parenting plan without the normal preview and user confirmation. Preserve existing data and pending confirmations during schema upgrades.

Save the completion summary as a UTF-8 file in the bot's writable `/tmp` directory, then:

```sh
docker compose exec -T bot python -m pplan.feedback --config /config/config.json --database /data/plan.sqlite3 complete REQUEST_ID --result-file /tmp/improvement-result.txt
```

Completion updates the status and queues one Telegram notification transactionally. Retrying the same completion is idempotent; do not also send a duplicate notification. If a request needs clarification, use `needs-input` with a question file instead. `requeue` returns an interrupted or clarified request to the queue.

Telegram messages alone do not wake this Codex chat. Do not install an unattended coding executor or recurring poller; check the queue when work resumes here and during active work. Tell the user this limitation clearly when relevant.

## Verification

Run the meaningful tests for the change inside the NAS bot container:

```sh
docker compose exec -T bot python -m unittest discover -s /app/tests -q
```

Keep domain changes independent of the model. Codex returns validated data; only an authorized button confirmation can change actual assignments or repeating rules. Never use live family data for destructive tests or expose credentials in logs.
