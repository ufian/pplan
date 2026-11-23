# Parenting Plan

A Python, Telegram-first parenting schedule for a family with two children. The owner manages the plan; a second parent can optionally join through a one-time invitation. Each author confirms their own schedule changes.

Each child has an independent assignment. A handover date labels the period from that day's after-school handover to the next day's handover, rather than midnight to midnight. The school-day times, non-school time, school holidays, family timezone, names, identities, and repeating schedule are configuration data.

## Current implementation

- A repeating baseline, confirmed child-specific exceptions, and future-dated repeating schedule changes.
- A private Telegram bot for the configured owner and one second parent connected with a one-time invitation.
- Automatic versions of confirmed schedule changes and special-event edits, with owner-confirmed restoration.
- Free-form messages interpreted by Codex CLI with ChatGPT subscription authentication.
- Exact previews with children, parent, date range, times, and timezone; changes apply only after **your** confirmation.
- Multiple periods and repeating rules from one message, with one atomic confirmation for the full action list.
- Clarification history, cancellation, preview expiry, stale-preview rejection, duplicate-update protection, and an audit trail.
- One-way Google Calendar sync with one timed event per continuous parenting period, combining matching child schedules.
- Docker Compose deployment on a NAS, persistent SQLite storage, health checks, and durable Telegram replies.
- A private Telegram Mini App with month navigation, child filters, handover details, editable special-event annotations, and night totals.

The MVP supports one family, two parents, two children, and one shared school handover timetable. Both children start with the same repeating baseline; exceptions and new repeating rules can put them with different parents. A message can propose up to 12 actions: assigning date ranges and starting repeating cycles from future dates. Two-way swaps and several vacation periods share one preview and confirmation. Each assignment covers up to 31 handover dates; longer periods can be split into adjacent actions. Hourly exceptions, separate school timetables, automatic holiday precedence, multi-family hosting, and second-parent approval are future work.

## Run without credentials

Python 3.11 or newer is required. Scheduling, the explicit parser, Telegram transport, and AI bridge use the Python standard library; Pillow renders calendar images locally.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -v
python -m pplan.cli --config config.example.json check
python -m pplan.cli --config config.example.json demo
```

The demo uses an in-memory database and illustrative handover times. It makes no external requests. The example is a weekly cycle anchored on November 23, 2026; replace it with your own confirmed plan.

## Configure

Copy `config.example.json` to `config.json`. All family names belong in this file, not application code. Set your Telegram user ID, parent and child display names, timezone, anchor date, and cycle. `operator_parent_id` determines who “I” means in natural-language requests; leave it `null` until known.

`handover.school_day_times` maps weekday numbers (`0` Monday through `6` Sunday) to local `HH:MM` times. Days missing from that map, and dates listed in `non_school_dates`, use `non_school_time`. The sample times are placeholders: set `handover.confirmed` to `true` only after replacing or explicitly confirming them. Until then, the bot labels displayed times as examples and blocks changes and Google sync.

A selected date range is inclusive: November 23 through November 24 means **from the November 23 handover until the November 25 handover**. Seattle uses `America/Los_Angeles`; timezone rules handle daylight saving changes. A transfer during an ambiguous/nonexistent DST time is rejected. Requests cannot change periods whose first handover has already passed.

Initial setup may change until the first request is created. Thereafter the application refuses configuration-file changes against the same database to prevent silent reinterpretation of approved periods. Future repeating schedule changes use the bot's confirmed `set_cycle` action and preserve past periods. Editing the original configuration, school calendar, or names needs a migration; do not delete a real database to bypass this guard.

## Telegram and NAS deployment

See [deployment instructions](docs/deployment.md). Create a bot through [BotFather](https://t.me/BotFather), store its token in `secrets/telegram_bot_token`, and send `/start` to the bot. `/whoami` returns only the sender's own Telegram ID; all plan access is allowlisted. Use `/invite` as the owner to generate a one-time code, then share it privately. The second parent sends `/join CODE` in a private chat with the same bot and receives full schedule and special-event access. Codes expire after 24 hours; a new code invalidates the previous one. `/access` shows the connected account; `/revoke` removes its access and invalidates codes without changing family data. Invitation administration and `/improve` remain owner-only. Both parents confirm their own schedule requests; one cannot confirm the other's preview. Group messages are ignored. The bot uses [Telegram long polling](https://core.telegram.org/bots/api#getupdates), so no inbound port or public domain is needed.

```sh
docker compose build
docker compose up -d
docker compose ps
```

The interactive calendar opens with the **Calendar** menu button or `/calendar`. See [Mini App deployment](docs/miniapp.md) for its NAS-to-public-proxy architecture.

Commands: `/start`, `/help`, `/whoami`, `/calendar`, `/schedule`, `/requests`, `/status`. An explicit fallback bypasses AI and remains usable when the subscription is unavailable:

`/schedule` returns a locally rendered calendar image covering the current and next month, starting at the current week. Every visible date has an assignment, including dates in weeks crossing a month boundary; dates before the plan starts remain unassigned. Long ranges use multiple month columns, keeping image height at most 1.5 times its width. A transfer date is split into equal color halves: left before handover, right after, without parent labels inside either half. Stable shared days show one parent's name; children are listed separately only when their assignments differ during that day. Earlier week rows are omitted, and shared weeks are never duplicated between months. Explicit date ranges select calendar months, with the first month cropped to the requested starting week.

Use `/special YYYY-MM-DD` to frame a date, or `/special YYYY-MM-DD YYYY-MM-DD [title]` for one continuous multi-day event. Event dates are inclusive calendar dates. Colored frames connect adjacent dates within each week and continue across months; they never change parenting assignments. `/special list` lists saved events and `/special remove ID` removes a frame. Marking or removing an event returns an updated calendar. In the Mini App, tap an event's **Edit** button or open **Special events** from the hamburger menu to change its title, description, inclusive date range, or color, then press **Save changes**. Existing events keep their purple color until edited.

Calendar requests support up to 700 days, including both endpoints. Months follow chronological order down each column, starting with the leftmost column. The image footer shows scheduled nights for the requested period on the left and nights from January 1 of the requested end date's year through that end date on the right. The schedule reply is a single image. Each block is titled with its date range in `YYYY-MM-DD` format and shows the counts for each child with each parent. Both include their last night and count only dates covered by the plan. Calendar cells shown to complete a week or month are excluded from the requested period. The parent assigned after a day's handover gets that night's count.

Use `/overwrite YYYY-MM-DD through YYYY-MM-DD to <parent> for both` (or a configured child name) to explicitly replace selected days after confirmation. Matching days are pinned too, so later cycle changes preserve them. You can also send `/overwrite` followed by copied numbered `From`/`Until` blocks; their timestamps are parsed locally, with `Until` exclusive, preserving every complete handover period before it. Other days and children remain unchanged. Natural-language corrections also support the `overwrite` action.

Send `/improve <request>` to save an app improvement request and `/improvements` to check recent requests. Only the configured operator can submit them. Requests are stored durably and deduplicated with Telegram updates; they do not alter the parenting schedule. The user chose implementation in the active Codex chat: requests are picked up when work continues there, tested, and deployed to the NAS. Completion sends a Telegram notification. A Telegram message alone does not wake an idle Codex chat; there is no unattended coding worker. See [AGENTS.md](AGENTS.md) for the queue workflow.

Free-form schedule messages can contain consecutive vacation periods and a return to weekly rotation. The AI returns `assign` and `set_cycle` actions. The preview shows exact handover times for every period and the day-by-day repeating pattern. A shared transition date belongs to the next parent starting at that day's handover; the preceding action ends at that handover. Conflicting overlaps require clarification. New repeating rules replace later repeating rules for the selected children, while previously confirmed date-specific exceptions remain in place. The entire action list applies atomically after confirmation.

```text
Assign YYYY-MM-DD through YYYY-MM-DD to <configured parent> for both
Assign YYYY-MM-DD to <configured parent> for <configured child>
```

Natural-language messages may be in any language. The bot's interface, clarification questions, documentation, and code are in English.

## Change history and recovery

The bot saves a complete version of the schedule exceptions, repeating rules, and special events when they change. `/history` (owner only) lists recent versions with their author, action, and timestamp. `/restore VERSION` shows a preview; only the owner's **Restore version** button applies it. The preview expires after 15 minutes or becomes stale after another data change.

Restoration recovers the full shared plan and special events, including past periods, and saves a new history version rather than deleting later history. It cancels pending schedule previews and refreshes revision markers so old confirmation/editor forms cannot overwrite restored data. Access and invitation state are never restored: use `/revoke` first if an account may be compromised, then restore the desired version. This history is private NAS data; it supplements, and does not replace, separate NAS backups. Older changes made before this feature was installed have no reconstructable versions; the migration saves the current plan as its initial baseline.

## Codex subscription

The AI service runs the official `codex exec` command with JSON Schema output and a forced ChatGPT login. The default model is `gpt-6-luna`; select another account-supported model through `CODEX_MODEL` in Compose. No OpenAI API key is used or accepted by this adapter. Subscription usage is subject to plan limits and model availability, rather than unlimited free inference. There is no automatic paid API fallback.

The AI container has its own login volume and no access to the database, Telegram token, Google token, or family configuration file. It receives only the message, a bounded clarification history, configured identities, handover policy, normal rotation patterns, and upcoming schedule context. Normal patterns remain available even when temporary vacations fill the entire upcoming view. Shell tools and web search are disabled; outputs are validated again by Python. Only the domain layer can change assignments, after a button confirmation.

AI interpretation is disabled by default. After the operator consents to sending message text, configured identities, six clarification messages, handover policy, normal rotation patterns, and 42 days of schedule context to OpenAI through Codex, set `PPLAN_AI_ENABLED=true` in the NAS's private `.env` file and recreate the bot with `docker compose up -d bot`. Without that consent, use the local commands and explicit request syntax.

Sign in once on the NAS:

```sh
docker compose run --rm --no-deps ai codex -c 'cli_auth_credentials_store="file"' login --device-auth
```

Follow the displayed URL and device code in your browser. [Device code authentication](https://developers.openai.com/codex/auth/) may need enabling in account security settings. [Non-interactive mode](https://developers.openai.com/codex/noninteractive/) reuses the saved CLI login and supports structured output. Model choices follow the [current Codex catalog](https://developers.openai.com/codex/models/).

## Google Calendar

See [Google Calendar setup](docs/google-calendar.md). Google setup is optional; scheduling works locally without it. A service account can sync to one existing calendar explicitly shared with it, using `calendar.events` and a fixed Calendar ID. User OAuth remains available to create a dedicated secondary calendar with `calendar.app.created`. Calendar edits are not imported into the plan.

## Design and next steps

See [architecture](docs/architecture.md) for domain boundaries, delivery semantics, limitations, and the next development milestones. This is a personal-use MVP, not a multi-user hosted service.
