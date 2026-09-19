# Real run — HubSpot → Gateway → Email Agent

> **For the commands, use [`RUNBOOK.md`](RUNBOOK.md).** This file keeps the port
> reasoning and the troubleshooting history behind them.

## Port map — fixed, do not improvise

| Port | Service | Runs on | Reached by |
|---|---|---|---|
| **8080** | Agent Gateway | **Windows** | ngrok, from the internet |
| **8081** | Email Agent | **Windows** | the gateway, on 127.0.0.1 only |
| 4040 | ngrok inspector | **Windows** | you, in a browser |

**Everything runs on Windows — nothing in WSL.** WSL has its own `localhost`, so a
tunnel started there cannot see a service started here. That was a real failure:
ngrok running in WSL forwarded to a WSL service on 8080 and answered "Not Found",
while the actual gateway sat untouched on Windows. Port number had nothing to do
with it — the two `localhost`s are simply different machines.

### Clearing 8080 before you start

`wslrelay.exe` holding 8080 on Windows is a *symptom*: it means something inside
WSL is listening on 8080 and Windows is relaying it. Kill the relay and WSL may
recreate it, so stop the real listener inside WSL.

**In WSL — find what owns it, then stop that:**
```bash
sudo ss -ltnp | grep :8080        # or: sudo lsof -i :8080
```
Identify it before killing it — it may be something from the LQABR project you
still want. Stop it the way it was started, or `kill <pid>`.

**In Windows — confirm both ports are now free:**
```powershell
Get-NetTCPConnection -LocalPort 8080,8081 -State Listen -ErrorAction SilentlyContinue
```
No output means both are free. If `wslrelay` still appears, the WSL listener is
still up.

End to end on a real contact, with **nothing sent**. The email agent writes the
email to a file instead of mailing it. Run this before wiring Mailgun.

## Before you start

| | |
|---|---|
| `HUBSPOT_APP_CLIENT_SECRET` | ✅ set — from `Agent-Gateway-Bench` → Auth tab |
| `NEXT_AGENT_URL` | ✅ set to `http://127.0.0.1:8081/email/trigger` |
| `.hubspot_mcp_tokens.json` | ✅ present — MCP consent already done |
| `ANTHROPIC_API_KEY` | ⬜ **paste into `.env`** |
| `GATEWAY_PUBLIC_URL` | ⬜ filled in at step 2, after ngrok starts |
| HubSpot webhook subscription | ⬜ created at step 3 |

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt     # picks up `anthropic`, added today
```

## 1. Start both services — two terminals, both in the repo

**Terminal A — gateway (port 8080)**
```powershell
.\.venv\Scripts\Activate.ps1
cd src
uvicorn bench_outreach.gateway.app:app --port 8080
```

**Terminal B — email agent (port 8081)**
```powershell
.\.venv\Scripts\Activate.ps1
cd src
uvicorn bench_outreach.email_agent.app:app --port 8081
```

Check both are healthy:
```powershell
curl.exe -s http://127.0.0.1:8080/readyz     # expect not-ready until step 2 fills GATEWAY_PUBLIC_URL
curl.exe -s http://127.0.0.1:8081/readyz     # expect {"status":"ready","dry_run":true,...}
```

## 2. Expose the gateway

**Terminal C**
```powershell
ngrok http 8080
```
Copy the `https://….ngrok-free.app` line, put it in `.env` as
`GATEWAY_PUBLIC_URL`, then **restart Terminal A** so it reloads.

It must match exactly — HubSpot signs the URL it posted to, and a mismatch
fails every signature check.

## 3. Point HubSpot at it

HubSpot → Development → Legacy Apps → `Agent-Gateway-Bench` → **Webhooks**

- Target URL: `<your ngrok url>/hubspot/events`
- Create subscription: object **Contact**, event **Property change**,
  property **`decision_maker`**
- Make sure the subscription is **active**

If HubSpot won't let you subscribe, the app is missing the
`crm.objects.contacts.read` scope — add it on the Auth tab first.

## 4. Fire it

Pick a contact whose **Decision Maker is No or blank** — the webhook fires on
*change*, so an already-true contact does nothing. Set it to **Yes** and save.

## 5. What you should see, in order

**Terminal A (gateway)** — one JSON line per stage:
```json
{"event":"ingress","events":1,"signature_verified":true}
{"event":"routing_decision","object_id":"5533…","route_id":"decision-maker-yes","target":"next_agent"}
{"event":"handoff","ok":true,"destination":"http://localhost:8081/email/trigger","status_code":200}
{"event":"run_summary","received":1,"routed":1,"handed_off":1,"failed":0}
```

**Terminal B (email agent)**:
```json
{"event":"trigger_received","object_id":"5533…"}
{"event":"lead_worked","status":"sent","dry_run":true,"reference":"salesforce","subject":"…"}
```

**The email itself** — `logs/email_agent/drafts-2026-09-17.md`:
```
TO      someone@example.com
CONTACT 5533…   technology: Salesforce Developer   reference: salesforce
SUBJECT …
------------------------------------------------------------------------------
(the email Claude wrote)
```

**HubSpot** — the contact's `email_status` stays **empty**. Dry run writes nothing.

## If something doesn't appear

| Symptom | Cause |
|---|---|
| Gateway silent, nothing in ngrok | Subscription not active, or target URL wrong |
| `401` in ngrok's inspector | `GATEWAY_PUBLIC_URL` doesn't match the ngrok URL, or the client secret is wrong |
| `"event":"discarded"` | The value wasn't `true` — check what HubSpot actually sent |
| Gateway logs `handoff ... ok:false` | Email agent isn't running on 8081 |
| Email agent `503` | `ANTHROPIC_API_KEY` missing, or HubSpot MCP token expired — run `python scripts\hubspot_auth.py --check` |
| `status":"failed","reason":"no reference file…"` | That contact's technology has no file in `skills/outreach/references/` |

ngrok's inspector at http://localhost:4040 shows every request HubSpot sent and
what the gateway answered — check there first.

## After the run

Reset the test contact: set Decision Maker back to No, so it can be fired again.
