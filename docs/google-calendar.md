# Google Calendar setup

The plan remains local until OAuth setup is complete. The application creates one dedicated secondary calendar in the account you authorize. It uses the [calendar.app.created scope](https://developers.google.com/workspace/calendar/api/auth) to manage app-created calendars rather than request access to all existing events.

## Authorize on a computer with a browser

Follow Google's [Python quickstart setup](https://developers.google.com/workspace/calendar/api/quickstart/python) to enable the Calendar API and configure a **Desktop app** OAuth client. Add your account as a test user when appropriate. Download the client configuration as `credentials.json`; do not commit it.

From a local checkout:

```sh
python3 -m venv .venv
.venv/bin/pip install '.[google]'
.venv/bin/python -m pplan.cli calendar-login --credentials credentials.json --token data/google-token.json
```

The command opens Google's browser flow and saves a refreshable token with private file permissions. If operating over SSH, run this step on your own computer rather than exposing an unauthenticated OAuth callback on the NAS. Transfer only the resulting token securely:

```sh
scp data/google-token.json ufian@192.168.1.99:/home/ufian/pplan/data/bot/google-token.json
ssh ufian@192.168.1.99 'chmod 600 /home/ufian/pplan/data/bot/google-token.json'
```

Complete the real handover times and set `handover.confirmed=true` before syncing. On the NAS:

```sh
cd /home/ufian/pplan
docker compose run --rm --no-deps bot python -m pplan.cli --config /config/config.json --database /data/plan.sqlite3 calendar-connect
docker compose run --rm --no-deps bot python -m pplan.cli --config /config/config.json --database /data/plan.sqlite3 calendar-sync
docker compose restart bot
```

The restart enables automatic sync after a token/calendar connection is added. The dedicated calendar can be shared with the other parent manually in Google Calendar; sharing is not required to use the bot.

## Behavior and recovery

- Each child receives a separate timed event, using the configured local handover boundaries and timezone.
- Confirmed changes update stable event IDs. Drafts and cancelled requests never reach Calendar.
- Only app-created events are managed; external edits do not update the plan.
- The normal projection covers 60 handover dates plus future confirmed exceptions and a window at future repeating rules. Already cached future dates are updated when a rule changes. Historical events already written are retained.
- A provider outage does not undo confirmation. Use `/status` to see the last successful sync and a pending error; retries run every 15 minutes.
- To repair manually edited/deleted events, run `calendar-sync --force` with the same CLI/container arguments.
- OAuth revocation or an expired test-app authorization requires another login. Preserve the existing database and use the same Google account. Test-mode OAuth credentials can require periodic reauthorization; review Google's OAuth publishing requirements before relying on unattended long-term access.

Creation and updates use Google's documented [Calendar creation](https://developers.google.com/workspace/calendar/api/v3/reference/calendars/insert) and [event update](https://developers.google.com/workspace/calendar/api/v3/reference/events/update) endpoints. Stable event identifiers follow Google's [event ID requirements](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert).
