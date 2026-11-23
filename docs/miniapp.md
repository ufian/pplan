# Telegram calendar Mini App

The Mini App is a view of the existing confirmed parenting plan. It supports forward scrolling with automatic loading of later months, month navigation buttons, a month picker, a Today shortcut, child filters in a hamburger menu, day details in a bottom dialog, square day cells, split handover colors, editable special-event markers, and night totals for the month or January 1 through month end. Dates and times use the configured family timezone. Draft changes are never displayed as confirmed assignments. Earlier months are loaded only through month navigation or the month picker; scrolling does not prepend past months. Open it with the bot's **Calendar** menu button or `/calendar`.

## Editing special events

Open a day, then choose **Edit** on its event card; or open **Special events** from the hamburger menu to find any active event, including one outside the visible months. The editor supports a title (120 characters), multiline description (2000 characters), inclusive start/end dates (up to 366 days), and any six-digit RGB color with preset choices. Press **Save changes** to commit; Cancel or closing the editor discards unsaved inputs. Color changes also appear in `/schedule` images. Overlapping events have a segmented color marker in the Mini App.

Saving updates only the selected event and appends an audit record. The ID and creation timestamp are preserved. A revision check prevents overwriting concurrent edits; reopen the editor to load a fresh version after a conflict. An identical save retry is idempotent. Existing event rows migrate transactionally to an empty description, purple color (`#7845a6`) and revision 1. Existing parenting data and pending confirmation requests are preserved.

## Data ownership and connection

```text
Telegram webview -> HTTPS public proxy -> loopback SSH forwarding -> NAS calendar service -> NAS SQLite
```

The NAS remains the source of truth. The public host stores only nginx configuration, its HTTPS certificate, and the tunnel account's public key. It has no family configuration, bot token, SQLite copy, or calendar cache. Access logging and proxy buffering are disabled. HTTP responses have `Cache-Control: no-store`; the app uses no browser storage or service worker. Private calendar responses necessarily pass through the HTTPS proxy in memory. Replacing the public host cannot lose parenting data. This connection is not a replacement for NAS backups.

A dedicated NAS systemd service establishes an outbound SSH connection, exposing the NAS's loopback `8081` only as the public host's loopback `18080`. Its separate private key stays in NAS `secrets/`. Pin the public host's SSH key after verifying it independently. The public tunnel account must allow only remote forwarding to `127.0.0.1:18080`, with no shell, TTY, agent, X11 or local forwarding. The service reconnects after network failures and starts after reboot.

Calendar and event-list reads open SQLite in `mode=ro` with `query_only=ON`, using one transaction for a consistent response. The NAS calendar container now mounts the database directory writable for explicit special-event saves. Its update endpoint opens the existing database without schema initialization and installs a SQLite authorizer that permits only selected special-event metadata updates and append-only audit records. Writes to assignments, repeating rules, requests, settings and outbox are denied. This endpoint cannot create/delete events or confirm parenting changes. Schema migrations run through the normal bot Store startup. The bot retains responsibility for parenting previews and authorized confirmation buttons. Configuration fingerprints must match for all requests.

If the NAS or tunnel is unavailable, the app reports a connection error. It does not serve an old cached plan. The public host's `/health` endpoint returns only availability, never family details.

## Authentication

The browser sends raw `Telegram.WebApp.initData` in an Authorization header. The NAS validates Telegram's HMAC-SHA-256 signature, checks a one-hour maximum age (30 seconds of future clock tolerance), rejects duplicate fields and malformed users, and allows only `operator_telegram_id`. No token or authorization data is placed in URLs or logs. Reopen the Mini App if authorization expires. Direct browser visits show instructions to open the bot; they cannot read the plan.

Reference: [Telegram Mini Apps authentication](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).

## NAS deployment

Add these settings to the private `.env` (retain any other existing settings):

```dotenv
COMPOSE_FILE=compose.yaml:compose.dev.yaml:compose.miniapp.yaml
PPLAN_MINIAPP_URL=https://pplan.ufian.com:8443
```

After upgrading to the special-event editor, take a consistent SQLite backup on the NAS, run the normal Store initialization once to migrate existing event columns, and recreate `miniapp` to apply its updated database mount. Restart the bot to load the new domain and calendar-image code. Run `docker compose up -d --no-deps miniapp bot`. Both use the existing image and source mounts. Restart affected services after source edits; build the image after dependency changes. Package data includes the HTML, CSS and JavaScript assets for image-based installs too.

Create a dedicated Ed25519 tunnel key in `secrets/miniapp_tunnel_key`, put only its public key on the external host, and store its verified host key in `secrets/miniapp_known_hosts`. Install [the tunnel service](../deploy/pplan-tunnel.service) into `/etc/systemd/system/` on the NAS, then run `systemctl daemon-reload` and `systemctl enable --now pplan-tunnel`. Adapt the service's user, paths and host for other installations. Never copy runtime secrets into the repository or a source archive.

## Public host

The current host is `96.44.158.249`; `pplan.ufian.com` points to it. Port 443 already hosts a VPN. The calendar therefore uses **8443**, leaving that service intact. Port 80 serves only ACME challenges and redirects to the HTTPS URL. No NAS inbound port is opened.

The proxy runs as a separate Docker container with the host network, read-only mounts, limited capabilities, and restart enabled. The host network lets nginx reach the SSH listener bound to loopback. The nginx and Certbot image digests are pinned in the deployment files.

Paths on the public host:

- `/opt/pplan-proxy/nginx.conf`: [nginx configuration](../deploy/nginx.conf).
- `/var/www/pplan-acme`: ACME challenge webroot.
- `/etc/letsencrypt`: TLS certificates and ACME account state; private to that host.
- `/home/pplan-tunnel/.ssh/authorized_keys`: only the restricted tunnel public key.

To provision a replacement host, create those directories, configure the restricted SSH account, and start nginx initially with only the port-80 server block (the certificate does not exist yet). Issue the certificate using Certbot's `certonly --non-interactive --agree-tos --register-unsafely-without-email --webroot -w /var/www/acme -d pplan.ufian.com`, with the host network and the certificate/webroot volumes from [the renewal service](../deploy/pplan-cert-renew.service). Then install the full nginx configuration and validate/reload nginx. [start-proxy.sh](../deploy/start-proxy.sh) documents the Docker launch flags. Do not remove or reconfigure unrelated services.

Install [pplan-cert-renew.service](../deploy/pplan-cert-renew.service) and [pplan-cert-renew.timer](../deploy/pplan-cert-renew.timer) in `/etc/systemd/system/`; enable the timer. It checks twice daily and reloads nginx after successful renewal checks. Validate ACME with `renew --dry-run` using the same Docker volumes and host network.

## Telegram settings

Set **Menu Button** to text `Calendar`, URL `https://pplan.ufian.com:8443/`. The owner-specific button may also be set with Bot API `setChatMenuButton`. **Main App** is optional; use the same URL for a profile launch button. Leave **Same-Origin Restriction** enabled. Neither a profile button nor a public landing page grants other users calendar access.

## Verification and recovery

Run `docker compose exec -T bot python -m unittest discover -s /app/tests -q` on the NAS. Mini App tests use synthetic data, including unauthorized/expired signatures, split assignments, events, leap months, HTTP access, read-only calendar reads, restricted event writes, legacy event migration, concurrent-save conflicts, retry idempotence and preservation of pending confirmations.

Check `docker compose ps`, `systemctl status pplan-tunnel`, the public HTTPS `/health`, and an unsigned POST to `/api/calendar` (must return 401). Test signed access without printing initData or family data. Check the certificate timer with `systemctl list-timers pplan-cert-renew.timer`.

Recovery: keep the NAS database and its normal backups, recreate only the proxy host, issue a new HTTPS certificate, install the tunnel public key, update DNS and the pinned SSH host key if needed, and restart the tunnel. No parenting-plan export or database restore to the public host is required.
