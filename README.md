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
