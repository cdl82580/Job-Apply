# Job Apply — Microsoft Teams Bot

A Microsoft Bot Framework integration that brings the Job Apply agent platform
to Microsoft Teams. Built with the Bot Framework SDK for Python (`botbuilder`).

## Features

| Command | Description |
|---------|-------------|
| `apply` | Generate a tailored resume, ATS resume, and cover letter |
| `aq` | Answer an application question using resume + JD context |
| `prep` | Generate an interview prep reference card |
| `optimize` | Refine existing run documents (resume/cover letter) |
| `tracker` | Pipeline summary (counts by status) |
| `track list [status]` | List applications, optionally filtered |
| `track add` | Add a new application (Adaptive Card form) |
| `track view` | View full application details |
| `runs` | List recent agent runs (structured records with type, status, Drive links) |
| `help` | Command reference |

## Architecture

```
Teams Client
    │
    ▼
Azure Bot Service (webhook relay)
    │
    ▼
app.py (aiohttp web server, port 3978)
    │
    ▼
bot.py (ActivityHandler — command routing + Adaptive Cards)
    │
    ▼
api_client.py (HTTP client → FastAPI backend at flowshift.cdlav.us)
```

- **Adaptive Cards** replace Slack's Block Kit modals for rich form input
- **Proactive messaging** via `ConversationReference` for long-running agent
  jobs (apply, prep, aq) — same thread-and-poll pattern as the Slack bot
- All state lives in the FastAPI backend — the Teams bot is stateless

## Setup

This is a step-by-step walkthrough — follow it in order. Each step depends on
the one before it.

### Single tenant vs. multi-tenant

**Single-tenant works fine.** Most personal/Microsoft 365 work or school
accounts can't create multi-tenant Azure AD app registrations (org policy
blocks it), so single-tenant is actually the common path here. The only
difference: a single-tenant app registration only issues tokens for *your*
Azure AD tenant, and the bot needs to know that tenant ID so it validates
incoming requests correctly. The code now supports this via the
`MICROSOFT_APP_TENANT_ID` env var (see step 3) — without it, a single-tenant
app registration will fail auth with a "multi-tenant token used for
single-tenant app" type error from the Bot Framework adapter. You do not need
multi-tenant for this to work in your own Teams org.

### 1. Create the Azure AD App Registration

This is the identity the bot uses to authenticate with Microsoft. Do this
*before* creating the Azure Bot resource — the Bot resource needs an existing
App ID.

1. Go to the [Azure Portal](https://portal.azure.com) → search **"App registrations"** → **New registration**
2. Name it (e.g. `job-apply-teams-bot`)
3. Under **Supported account types**, choose **"Accounts in this organizational directory only (Single tenant)"** — this is the option that works without special org permissions
4. Click **Register**
5. On the app's **Overview** page, copy and save two values — you'll need them in step 3:
   - **Application (client) ID** → this is `MICROSOFT_APP_ID`
   - **Directory (tenant) ID** → this is `MICROSOFT_APP_TENANT_ID`
6. Go to **Certificates & secrets** → **New client secret** → give it a description and expiry → **Add**
7. **Copy the secret's "Value" immediately** (not the Secret ID) — it's only shown once. This is `MICROSOFT_APP_PASSWORD`.

### 2. Create the Azure Bot Resource

1. In the Azure Portal, **Create a resource** → search **"Azure Bot"** → **Create**
2. **Bot handle**: any unique name
3. **Type of App**: choose **"Use existing app registration"**
4. Paste in the **App ID** and **Tenant ID** from step 1
5. Create the resource
6. Once created, open it → **Settings → Configuration** → set the **Messaging endpoint** to `https://<your-host>/api/messages`
   - For local testing this can be a placeholder for now (e.g. `https://localhost/api/messages`) — you'll update it once you have a real public URL in step 5
7. Under **Settings → Channels**, add the **Microsoft Teams** channel and accept the terms

### 3. Environment Variables

```bash
export MICROSOFT_APP_ID="<Application (client) ID from step 1>"
export MICROSOFT_APP_PASSWORD="<client secret VALUE from step 1>"
export MICROSOFT_APP_TENANT_ID="<Directory (tenant) ID from step 1>"
export BOT_API_KEY="same-key-as-slack-bot"
export JOB_APPLY_API_URL="https://flowshift.cdlav.us"  # optional, this is the default
```

`MICROSOFT_APP_TENANT_ID` is required for single-tenant app registrations
(the common case). Leave it unset only if you registered a true multi-tenant
app.

### 4. Install & Run Locally

```bash
cd teams_bot
pip install -r requirements.txt
python app.py
```

The bot listens on port 3978 by default (set `PORT` to override). Confirm
it's up with `curl http://localhost:3978/health` → should return `OK`.

### 5. Expose it publicly and point the Bot resource at it

Azure needs to reach your bot over HTTPS. For local testing, use a tunnel:

```bash
ngrok http 3978
```

Take the `https://...ngrok-free.app` URL ngrok gives you and:
1. Go back to the Azure Bot resource → **Settings → Configuration**
2. Set **Messaging endpoint** to `https://<ngrok-url>/api/messages`
3. Save

Now go to the Azure Bot resource → **Test in Web Chat** and send a message
(e.g. `help`) to confirm the round trip works end to end before touching
Teams at all. This isolates "is my bot reachable and authenticating" from
"is Teams sideloading working" — much easier to debug one at a time.

For permanent hosting (no ngrok), deploy `teams_bot/` as its own process —
see "Deploying" below — and point the messaging endpoint at that instead.

### 6. Local Development with Bot Framework Emulator (alternative to ngrok)

For testing without any Azure resources at all:
1. Download the [Bot Framework Emulator](https://github.com/microsoft/BotFramework-Emulator/releases)
2. Run `python app.py` with `MICROSOFT_APP_ID`/`MICROSOFT_APP_PASSWORD` empty (unauthenticated mode)
3. Connect the emulator to `http://localhost:3978/api/messages`

This only validates bot logic — it does not test real Azure AD auth or Teams
sideloading, so still do step 5 before going further.

### 7. Build and Sideload the Teams App Package

Only do this once step 5's Web Chat test works.

1. In `manifest/manifest.json`, replace **both** `{{MICROSOFT_APP_ID}}` placeholders (the `id` field and `bots[0].botId`) with your actual Application (client) ID from step 1
2. Add a 32×32 `outline.png` and a 192×192 `color.png` to `manifest/` (transparent background, simple icon — Teams will reject the upload without both files present)
3. Zip **the contents** of the manifest folder (not the folder itself):
   ```bash
   cd manifest && zip ../jobapply-teams.zip manifest.json outline.png color.png && cd ..
   ```
4. In Teams: **Apps** (left rail) → **Manage your apps** → **Upload an app** → **Upload a custom app**
   - If you don't see "Upload a custom app", your Teams admin has custom app uploads disabled org-wide — ask them to enable it in the Teams Admin Center under **Teams apps → Setup policies**, or have them upload/approve it centrally instead
5. Select `jobapply-teams.zip` — Teams installs it and opens a chat with the bot
6. Send `help` to confirm

### Common failure points

| Symptom | Likely cause |
|---|---|
| "Unauthorized" / 401 in bot logs when messaging from Teams | Missing `MICROSOFT_APP_TENANT_ID` on a single-tenant app registration |
| Web Chat test in Azure works, but Teams sideload fails to even install | `{{MICROSOFT_APP_ID}}` placeholder not replaced in `manifest.json`, or zip contains a parent folder instead of the files directly |
| Bot installs in Teams but never responds | Messaging endpoint in Azure Bot config doesn't match where `app.py` is actually reachable (stale ngrok URL is the usual culprit — they expire/rotate) |
| "Upload a custom app" option missing in Teams | Org policy blocks custom app uploads — needs a Teams admin to enable it |
| Cards/forms don't render, plain text does | Adaptive Card JSON schema version mismatch with the Teams client — check `cards/*.json` against the [Adaptive Cards schema explorer](https://adaptivecards.io/explorer/) |

## How It Works

### Command Routing
Users type commands as plain messages (e.g., `apply`, `track list interviewing`).
The bot parses the text, strips @mentions in group chats, and routes to the
appropriate handler.

### Adaptive Cards
Form-based commands (`apply`, `aq`, `prep`, `track add`) respond with an
Adaptive Card containing input fields. When submitted, the card payload comes
back as a message activity with `activity.value` populated — the bot routes
based on `data.action`.

### Long-Running Jobs
Agent commands (apply, prep, aq, optimize) use the same async pattern as the Slack bot:
1. Send an immediate "⏳ Starting…" message
2. Spawn a background thread that POSTs to the API, then polls for completion
3. When done, send a proactive message back to the conversation using the
   saved `ConversationReference`

### Backend Integration
All data flows through `api_client.py` → the FastAPI backend at `flowshift.cdlav.us`.
The Teams bot authenticates with the same `BOT_API_KEY` Bearer token as the
Slack bot. No separate user accounts or auth flow needed.

## File Structure

```
teams_bot/
├── app.py                 # aiohttp entry point
├── bot.py                 # ActivityHandler (command routing + card handling)
├── api_client.py          # HTTP client for FastAPI backend
├── config.py              # Environment variable configuration
├── requirements.txt       # Python dependencies
├── cards/                 # Adaptive Card JSON templates
│   ├── apply_form.json
│   ├── aq_form.json
│   ├── optimize_form.json
│   ├── prep_form.json
│   └── track_add_form.json
├── manifest/              # Teams app manifest
│   └── manifest.json
└── README.md
```
