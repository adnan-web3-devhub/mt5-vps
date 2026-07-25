# EQT Protocol MT5 VPS Agent

Windows VPS agent that reads master MT5 accounts via the **MetaTrader5** Python package and pushes weekly breakdowns to the Laravel API.

It also ships a small **validation service** (`validator_server.py`) so users can connect their own MT5 accounts with **free** real-broker validation — no MetaApi subscription needed.

## Requirements

- Windows Server/VPS with MetaTrader 5 terminal installed
- Python 3.10+
- Network access from VPS to your Laravel app (`/api/arbitrage/*`)

## Setup

1. Copy this folder to your VPS (e.g. `C:\eqt\mt5-vps-agent`).
2. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
3. Copy config:
   ```powershell
   copy config.example.json config.json
   ```
4. Edit `config.json`:
   - `laravel_api_url`: e.g. `https://yourdomain.com/api/arbitrage`
   - `agent_token`: same as `MT5_AGENT_TOKEN` / admin setting **MT5 Agent Token**
   - `hmac_secret`: same as `MT5_AGENT_HMAC_SECRET` / admin setting **MT5 Agent HMAC Secret**
   - `commission_comment_keywords`: keywords to identify auto-deducted commission transfers
   - `mt5_terminal_path`: path to `terminal64.exe`
   - `use_laravel_accounts`: `true` to fetch accounts from Laravel API (recommended)

5. In Laravel admin, create **Master MT5 Accounts** (owner, broker, server, login, master password).

## Laravel `.env` keys

```env
QUEUE_CONNECTION=database
MT5_AGENT_TOKEN=your-long-random-token
MT5_AGENT_HMAC_SECRET=your-long-random-hmac-secret
```

Also configure in **Admin → Site Settings → Arbitrage** (overrides `.env` when saved).

Ensure a queue worker runs on the app server:

```bash
php artisan queue:work --tries=3
```

Or use existing cron: `GET /cron-job/queue`

## Run manually

Daily sync (equity/balance):
```powershell
python agent.py sync
```

Weekly report (last 7 days):
```powershell
python agent.py weekly
```

Custom period:
```powershell
python agent.py weekly --days 7
```

## Windows Task Scheduler

**Daily sync** — run `python agent.py sync` every day at 02:00.

**Weekly report** — run `python agent.py weekly` every Monday at 03:00.

Use "Start in" directory: `C:\eqt\mt5-vps-agent`

## User MT5 validation service (free, replaces MetaApi)

When a user submits their MT5 credentials on the site's **Connect MT5** page,
Laravel calls this always-on service, which performs a real broker login with the
free MetaTrader5 package and returns equity/balance. Laravel then verifies the
account and enforces the minimum-capital rule.

1. Extra config keys in `config.json`:
   - `validator_host`: usually `0.0.0.0`
   - `validator_port`: e.g. `8787`
   - `validator_mt5_terminal_path`: path to a **separate** MT5 terminal used only
     for validation (recommended so it never disrupts the scheduled master jobs).
     Falls back to `mt5_terminal_path` if omitted.

2. Run the service (keep it running, e.g. as a Windows service / NSSM / Task
   Scheduler "at startup"):
   ```powershell
   python validator_server.py
   ```

3. Expose it to your Laravel server over HTTPS (reverse proxy / tunnel / firewall
   rule). Then in **Admin → Site Settings → Arbitrage**, set **MT5 VPS Validation
   URL** to `https://<your-vps-host>/validate` (or the direct `http://<vps-ip>:8787/validate`
   if that path is network-restricted to your app server).

   Laravel authenticates with the same `agent_token` (Bearer) + HMAC body
   signature (`hmac_secret`) used by the reporting API, so no new secrets.

Endpoint contract:

```
POST /validate
Authorization: Bearer <agent_token>
X-Arbitrage-Signature: HMAC-SHA256(raw_body, hmac_secret)
{ "broker": "...", "server": "BrokerName-Live", "login": "123456", "password": "..." }

200 -> { "success": true, "equity": 2500.0, "balance": 2500.0, "message": "..." }
200 -> { "success": false, "message": "Invalid MT5 credentials or server..." }
401 -> { "success": false, "message": "Unauthorized." }
```

> Note: MT5 allows one active login per terminal. The service serializes requests
> with a lock; using a dedicated `validator_mt5_terminal_path` avoids clashing with
> `agent.py sync`/`weekly`.

### Troubleshooting `MT5 initialize failed: (-10005, 'IPC timeout')`

The Python package could not talk to the terminal over its named pipe. It is a
server-side problem, never a wrong user password. Check in this order:

1. **Does the configured terminal exist?** `validator_mt5_terminal_path` must point
   at a real `terminal64.exe`. The default example path
   (`C:\Program Files\MetaTrader 5 Validator\terminal64.exe`) only exists if you
   actually installed a second terminal there — otherwise remove the key so it
   falls back to `mt5_terminal_path`.
2. **Is the terminal already open and logged in?** Start it manually once, log into
   any account with *Save account information* ticked, accept every first-run
   dialog (account wizard, EULA, update prompt), and leave it running. A terminal
   stuck on a modal dialog, mid-self-update, or with no saved account never
   finishes booting and so never answers the pipe.
   A terminal folder that was *copied* rather than installed is the usual culprit:
   MT5 derives its data folder from the install path, so a new path means a brand
   new profile with the account wizard waiting. Prefer running the official
   installer a second time with a custom destination.
3. **Is there an interactive desktop session?** `terminal64.exe` is a GUI app. Run
   the validator from an RDP session, or from Task Scheduler with *Run only when
   user is logged on* plus autologon. As a Windows service in session 0 (NSSM
   without a desktop) it will time out on every call.
4. **Is Python 64-bit?** `python -c "import sys; print(sys.maxsize > 2**32)"` must
   print `True`.
5. **Same session, same user, same elevation?** The pipe does not cross Windows
   sessions. If the terminal process is alive at the right path and it still times
   out, compare `validator_session.session_id` with the terminal's `SessionId` in
   `/diagnose`, check the `Owner` matches, and make sure you did not start one of
   the two "as Administrator" and the other normally.
6. **Is the terminal new enough?** Python IPC needs build 2085 or newer;
   `/diagnose` reports `terminal_build` from the exe.

The service now logs these conditions at startup and pre-boots the terminal, so a
misconfiguration shows up when you start it rather than on a user's first attempt.
For an on-demand check, run this **on the VPS** (localhost only):

```powershell
curl http://127.0.0.1:8787/diagnose
```

It reports Python bitness, package version, whether the terminal path exists, the
terminal build, the Windows session/user/elevation of the validator, every live
`terminal64.exe` with its own session, owner, elevation and open windows, the
MetaTrader named pipes currently published, the raw `last_error()` from a real
`initialize()` call, and a `hints` list that names the specific mismatch it can
see. The same hints are written to the log on a failed warm-up. Failed `/validate`
responses carry `error_code` / `error_detail`.

The pipe list is the most decisive field: **no MetaTrader pipe means the terminal
is running but never finished starting**, so no client of any kind could connect,
and the fix is at the terminal window rather than in this service.

The same checks run standalone, without Flask or a validation request:

```powershell
python win_diagnostics.py "C:\eqt\mt5-validator\terminal64.exe"
```

Timing knobs in `config.json` (defaults are sized to fit inside Laravel's 60s HTTP
timeout — raise them together carefully):
`validator_terminal_warmup_seconds`, `validator_init_timeout_ms`,
`validator_init_attempts`, `validator_init_retry_seconds`.

## Security

- Master passwords are stored encrypted in Laravel and sent to the agent only over HTTPS with Bearer token + HMAC body signature.
- The agent uses the **master/trader password** to log into MT5 and read data only (no trade execution in this code).
- Keep `config.json` permissions restricted on the VPS.

## Data sent per weekly report

| Field | Description |
|-------|-------------|
| gross_profit | Sum of trade deal profits only |
| deal_commission | Broker commission on trades |
| swap | Overnight swap |
| deposits / withdrawals | Balance ops excluded from profit |
| broker_commission_pool | Commission transfers matched by keyword |
| net_profit_platform | gross + commission + swap (losses preserved) |

Laravel stores the full breakdown and distributes `distributable_pool` (broker commission, with optional fallback) across leadership levels.
