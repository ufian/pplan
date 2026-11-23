# Google Calendar setup

The NAS remains the source of truth. Two credential modes are supported: a service account writing to one explicitly shared existing calendar, or user OAuth creating a dedicated secondary calendar. Neither mode imports external edits into the parenting plan.

## Use one existing calendar with a service account

Enable Google Calendar API, create a service account, and share only the intended calendar with its email address using **Make changes to events** (`writer`). Cloud project IAM roles and domain-wide delegation are not needed. Download its JSON key and place it only on the NAS at `secrets/google_service_account.json`, owned by the bot's UID and mode `600`. The application uses `calendar.events` as the service-account identity, without impersonating a human user or requesting permission to change sharing settings.

Get the explicit **Calendar ID** from that calendar's Settings > Integrate calendar. Add to the NAS's private `.env` (retain its other settings):

```dotenv
COMPOSE_FILE=compose.yaml:compose.dev.yaml:compose.miniapp.yaml:compose.google-service-account.yaml
GOOGLE_CALENDAR_ID=your-calendar-id@group.calendar.google.com
```

The optional Compose file mounts the key only in the bot container. The AI service, Mini App service and public HTTPS proxy do not receive it. Access tokens refresh in memory; the private key is not rewritten.

On the NAS, connect and project the schedule before recreating the long-running bot:

```sh
cd /home/ufian/pplan
docker compose run --rm --no-deps bot python -m pplan.cli --config /config/config.json --database /data/plan.sqlite3 calendar-connect
docker compose run --rm --no-deps bot python -m pplan.cli --config /config/config.json --database /data/plan.sqlite3 calendar-sync
docker compose up -d --no-deps --force-recreate bot
```

Connection checks `events.list` with `fields=accessRole`, without downloading existing event details or changing the calendar. It requires `writer` or `owner` access before saving the connection. No new calendar is created. The client rejects requests outside the configured calendar's event routes, calendar creation, and requests outside this app's event-ID format. Obsolete events are deleted only after checking the private plan marker and recognizing the schedule-event format. Only the bot's projected events are updated; unrelated calendar events are retained. An existing connection is never silently switched to another Calendar ID: both connection and sync refuse a configuration mismatch.

The sync projects confirmed child assignments and active special-event annotations. Specials use one all-day event for their inclusive date range (Google receives an exclusive end date), including title, description and standard event color. Removed specials are removed from Google on the next sync. Use `/status` to see connection and retry state. A sharing removal or key revocation produces a sync error while the confirmed NAS plan remains intact.

References: [Google service-account authentication](https://developers.google.com/identity/protocols/oauth2/service-account), [Calendar sharing roles](https://developers.google.com/workspace/calendar/api/concepts/sharing), [events.list accessRole](https://developers.google.com/workspace/calendar/api/v3/reference/events/list).

## Create a dedicated calendar with user OAuth

User OAuth uses the [calendar.app.created scope](https://developers.google.com/workspace/calendar/api/auth) and creates a dedicated secondary calendar. Keep `GOOGLE_SERVICE_ACCOUNT_FILE` unset to use this mode.

### Authorize on a computer with a browser

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

## Shared event colors

The Mini App, generated calendar images and Google projection use one standard Calendar v3 event palette. Parent colors follow configured parent order: Basil (`10`) and Tangerine (`6`). Specials default to Grape (`3`) and can select any of the 11 standard colors. The API palette's HEX swatches are kept in `pplan/colors.py`; Google clients may theme their displayed shades differently. No custom labels, calendar metadata scope or sharing-management role is needed. See [event color IDs](https://developers.google.com/workspace/calendar/api/v3/reference/colors).

Existing custom special shades are aligned once to the closest supported hue, preserving the prior history snapshot and recording `palette_aligned`. Old history remains immutable; restoring it uses the supported equivalent color. Annotation edits mark the Google projection dirty, including edits through the restricted Mini App writer. Parenting assignments and pending confirmations are preserved by color alignment.

## Behavior and recovery

- Consecutive dates with the same parent form one timed event per child. Runs with identical parent, start and end are combined into one event naming both children. Distinct runs stay separate; a long run is not fragmented just because the other child has an exception. Exact configured handover boundaries and timezone are preserved across DST.
- Active runs retain their Google event IDs while their end time is extended. Splits, merges and restored retired runs receive fresh reserved IDs, avoiding deleted-event tombstones. IDs are reserved durably before HTTP so interrupted uploads retry without duplicates. Drafts and cancelled requests never reach Calendar.
- Only events with this plan's private identifier and a recognized schedule format are managed. Unrelated entries and other app formats are preserved; external edits do not update the plan. New period events are uploaded before obsolete managed events are removed. Legacy per-day events are replaced automatically, including events accepted before an interrupted upload saved its cache.
- The normal projection covers at least 60 handover dates and continues through the furthest future confirmed exception or repeating-rule window. The first and last runs expand to their full parent period. A run without a change within another 366 days is bounded by the projection horizon and extended on later runs. Previously projected coverage is retained per child, including historical dates; differing child rotation boundaries cannot repeatedly extend each other's horizon.
- Transient Google throttling (`429` or rate-limit `403`) and backend errors use bounded exponential backoff with jitter. Permanent permission errors are not retried inside a request. A provider outage does not undo confirmation. Use `/status` to see the last successful sync and a pending error; retries run every 15 minutes.
- To repair manually edited/deleted events, run `calendar-sync --force` with the same CLI/container arguments.
- OAuth revocation or an expired test-app authorization requires another login. Preserve the existing database and use the same Google account. Test-mode OAuth credentials can require periodic reauthorization; review Google's OAuth publishing requirements before relying on unattended long-term access.

Creation and updates use Google's documented [Calendar creation](https://developers.google.com/workspace/calendar/api/v3/reference/calendars/insert) and [event update](https://developers.google.com/workspace/calendar/api/v3/reference/events/update) endpoints. Event identifiers follow Google's [event ID requirements](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert).

The NAS keeps the active run-to-event mapping in `calendar_period_cache` and special mappings in `calendar_special_cache`. Google projection cache updates do not change the parenting schedule, event annotations, pending confirmations, or plan-history versions. A schedule change during a slow upload leaves the dirty flag set for a follow-up sync.
