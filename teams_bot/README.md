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
api_client.py (HTTP client → FastAPI backend at apply.cdlav.us)
```

- **Adaptive Cards** replace Slack's Block Kit modals for rich form input
- **Proactive messaging** via `ConversationReference` for long-running agent
  jobs (apply, prep, aq) — same thread-and-poll pattern as the Slack bot
- All state lives in the FastAPI backend — the Teams bot is stateless

## Setup

### 1. Azure Bot Registration

1. Go to [Azure Portal](https://portal.azure.com) → Create a resource → Azure Bot
2. Choose **Multi Tenant** for the bot type
3. Note the **Microsoft App ID** and generate a **client secret** (App Password)
4. Set the messaging endpoint to `https://<your-host>/api/messages`

### 2. Environment Variables

```bash
export MICROSOFT_APP_ID="your-app-id"
export MICROSOFT_APP_PASSWORD="your-app-password"
export BOT_API_KEY="same-key-as-slack-bot"
export JOB_APPLY_API_URL="https://apply.cdlav.us"  # optional, this is the default
```

### 3. Install & Run

```bash
cd teams_bot
pip install -r requirements.txt
python app.py
```

The bot listens on port 3978 by default (set `PORT` to override).

### 4. Local Development with Bot Framework Emulator

For local testing without Azure:
1. Download the [Bot Framework Emulator](https://github.com/microsoft/BotFramework-Emulator/releases)
2. Run `python app.py` (leave `MICROSOFT_APP_ID` and `MICROSOFT_APP_PASSWORD` empty)
3. Connect the emulator to `http://localhost:3978/api/messages`

### 5. Teams App Package

To sideload into Teams:
1. Replace `{{MICROSOFT_APP_ID}}` in `manifest/manifest.json` with your actual App ID
2. Add 32×32 `outline.png` and 192×192 `color.png` icons to `manifest/`
3. Zip the manifest folder: `cd manifest && zip ../jobapply-teams.zip *`
4. Upload via Teams Admin → Manage Apps → Upload custom app

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
All data flows through `api_client.py` → the FastAPI backend at `apply.cdlav.us`.
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
