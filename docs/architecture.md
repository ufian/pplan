# Architecture

## Product boundaries

The source of truth is a SQLite plan, not Telegram messages, model output, or Google Calendar. A single operator manages two parents and two children. Request creation does not change ownership; only an authorized confirmation does. Parent approval is intentionally absent in solo mode.

`Config` supplies identities, timezone, baseline, and handover policy. A baseline cycle assigns the same parent to both children initially. Confirmed overrides are keyed by `(child_id, handover_date)`, so changing one child leaves the other untouched. Future repeating phases are keyed by `(child_id, start_date)`; a date uses its latest applicable phase when no explicit override exists. A future native Swift app should use an authenticated API around the same domain operations rather than reinterpret Telegram messages.

## Modules

| Module | Responsibility |
| --- | --- |
| `core.py` | Configuration, local handover boundaries, schedule calculation, persistence, request lifecycle, audit |
| `parser.py` | Explicit English fallback syntax without inference |
| `ai.py` | Codex subprocess adapter, structured output validation, private AI HTTP client |
| `ai_server.py` | Private Compose service holding only Codex authentication |
| `telegram.py` | User authorization, conversational context, previews, buttons, polling, reply outbox |
| `calendar.py` | OAuth, dedicated Google calendar, stable event projection, retryable caching |
| `calendar_image.py` | PNG calendar with aspect-limited month columns, before/after color halves, and special-event frames |
| `cli.py` | Setup, offline demo, diagnostics, bot runner, Calendar administration |
| `feedback.py` | Operator improvement queue, chat-driven status transitions, completion notifications |

## Change lifecycle

1. The allowlisted operator sends a private message.
2. Explicit syntax and copied overwrite timestamp lists are parsed locally; otherwise Codex returns `changes`, `schedule`, or `clarify` data. A `changes` result contains up to 12 `assign`, `overwrite`, or `set_cycle` actions. The old single-change envelope remains accepted for compatibility.
3. Python validates identities, children, date bounds, handover timing, and conflicting assignments. A draft stores prior assignments/revisions for affected dates. Repeating changes also snapshot future dates through the one-year change horizon and the selected children's phase definitions.
4. The bot displays every action's children, parent, local boundaries or repeating pattern, and timezone, with Confirm all and Cancel buttons.
5. Confirmation checks authorization, expiry, first handover, and prior revisions inside a write transaction. All assignments and repeating rules apply together or roll back together, with an audit event.
6. The resulting reply and Telegram offset are stored in the same transaction. Google sync is marked dirty.
7. Telegram replies and Calendar projection are delivered independently of the confirmed state.

A revision changes even if ownership later returns to the same parent, protecting against stale previews after a change-and-revert. An unrelated child's change does not invalidate a preview. Drafts expire after 24 hours and also become unconfirmable once their first handover has passed. Cancellation applies only to drafts; reverting a confirmed change requires a new preview and confirmation.

`assign` uses inclusive handover dates. Adjacent natural-language periods sharing a transition date are normalized so one ends exactly when the next begins. `set_cycle` stores one parent ID per cycle day, starts at its configured handover, and repeats indefinitely. It replaces later phases for selected children while preserving earlier history and specific-date exceptions. Existing single-action previews remain confirmable after the additive database migration.

`overwrite` uses assignment fields and applies through the same atomic confirmation path, replacing existing overrides. It also stores explicit date-specific exceptions when the parent already matches, so the selected dates remain pinned under later recurring changes. A copied preview's `Until` is converted to an inclusive last handover date exactly once. Special events are separate records with inclusive start/end dates, a title, and soft removal; commands update only these calendar annotations, never custody assignments. Their ranges are rendered as connected weekly frame segments.

## Delivery and recovery

Run exactly one bot worker per token/database. Telegram update IDs are persisted as a high-water mark; replayed updates do not create another change. Replies are delivered at least once from an outbox. A crash after Telegram accepts a reply but before local deletion can duplicate that reply, but does not duplicate the underlying schedule change. Failed reply delivery is retried in order. Expired callback acknowledgements may be discarded.

Google events use stable IDs derived from plan ID, child ID, and handover date. A changed assignment updates the existing event. Successful event projections are cached individually; partial sync failure preserves confirmed changes and retries the unfinished work. The runner retries every 15 minutes and immediately attempts sync after a confirmation. It projects 60 handover dates, future approved exceptions, a window at each future repeating phase, and already cached future dates affected by new rules. `calendar-sync --force` repairs externally edited or deleted events that a cache-only sync would otherwise skip.

Calendar events represent timed, half-open intervals with an explicit IANA timezone. They are separate per child and handover date; adjacent same-parent periods are not merged. Events are never inferred for dates before the plan anchor. The present policy uses one school timetable for both children and a manually configured list of non-school dates; it does not discover school holidays.

The private AI endpoint has no published host port. It uses a dedicated Codex home, an empty temporary working directory, read-only command sandboxing, disabled shell/image tools and web search, forced ChatGPT authentication, and an environment that excludes API keys and bot credentials. The model proposes data and never receives a confirmation capability. Model interpretation still requires human review, particularly for relative dates and ambiguous wording.

## Next milestones

- Add versioned baseline and handover-policy migrations that preserve existing confirmed history.
- Support different school schedules per child, special holiday calendars, and partial-day exceptions.
- Add partial-day actions and richer holiday rules to the confirmed action list.
- Make setup conversational, including timezone, identities, school times, and Calendar connection.
- Add optional second-parent invitations and approval without changing solo mode.
- Add an authenticated API, then evaluate a Swift calendar app after using the Telegram workflow.
- Add encrypted backup automation and a tested restore drill, then deployment CI.

Live Telegram, subscription inference, and Google writes require real credentials and explicit setup. Offline tests use fake providers to verify domain behavior without modifying a family's calendar.
