# job-apply — Job Apply Agents

A Claude-powered web app (and Slack bot) that takes a job posting and produces a
tailored resume, ATS resume, cover letter, and interview prep doc in under 2 minutes.
Includes a full-featured application tracker, calendar, admin dashboard, webhook system, and audit logging.

**Live app:** https://apply.cdlav.us/

---

## Features

### Agent
- **Tailored resume** — styled DOCX with brand colors, targeted bullets, competency grid
- **ATS resume** — plain single-column DOCX, no tables or text boxes, parser-safe
- **Cover letter** — voice-matched DOCX tailored to the role and hiring manager
- **Application Questions** — answer freeform application questions (e.g. "Describe a time you led a cross-functional team") using tailored resume, JD, and profile context; tone selector (professional/conversational/technical/concise), optional character limit, two-phase clarification flow (agent can ask follow-ups before answering), editable answer with copy-to-clipboard and refinement chips
- **Thank You Email** — post-interview thank-you email generator with app picker, round/tone selectors, optional interviewer name and key topics discussed; outputs editable email with subject line, copy-to-clipboard, DOCX download, and Google Drive upload
- **Interview Prep** — compact reference card (0.4" margins, 2-column layout) with 10 sections: elevator pitch (60-second spoken script), interviewer intel, role fit map, gap bridges, dev framework, anchor stories, likely Q&A, questions to ask, differentiating edge, and closing line. Tailored to the interviewer, round type, and focus/slant. Proof points restricted to last 10 years (Applause 2016+, ProdPerfect, HSP Group, eHealth, GitHub projects). Fidelity excluded.
- **Humanizer prompts** — each text-producing agent has a tailored humanizer directive: Voice Builder (resume/cover letter), Natural Flow Editor (interview prep), AI Pattern Remover (optimize resume), Human Rewrite (optimize cover letter), Authenticity Check (application questions), Voice Builder (thank you email)
- **GitHub portfolio** — FlowShift, task-api, and job-apply repos injected into every prep prompt as additional proof points
- **JD persistence** — job description saved as `job_description.md` to Google Drive on every run; when a JD is pasted and the run completes the file is written to the output folder and a `job_description` run is linked to the application so it auto-loads on subsequent runs and prep
- **Google Drive sync** — all output files uploaded automatically to your Drive folder; PDF version generated via Drive conversion
- **SSE progress streaming** — live log output while the agent runs; `done` event includes `replacements_warning` if XML edits partially failed
- **Machine pinning** — `machine_id` returned from POST endpoints; client sets `fly-force-instance-id` cookie before opening EventSource to guarantee SSE stream hits the same Fly.io machine
- **bfcache prevention** — Web Lock acquired on page load keeps all HTML pages ineligible for Chrome's back/forward cache; server-side middleware also injects `no-store` headers, `<meta>` cache tags, and a `pageshow` reload script into every HTML response

### Application Tracker
- Full CRUD for job applications — company (via Logo.dev search), role, status, recruiter, salary, DUA tracking
- **Match scoring** — Claude-powered resume↔JD fit score (0–100) with category badge (Strong Match / Good Match / Stretch / Long Shot) and per-dimension breakdown; Rescore button per row
- **DUA indicator** — tag and filter applications reported to unemployment (DUA)
- **Auto `date_applied`** — setting status to "Applied" automatically sets today's date if not already present
- **Run agent from tracker** — ▶ icon on each row opens the agent page with the app pre-selected and JD loaded from Drive
- **Setup Drive folder** — ⊕ icon on rows without a Drive folder creates the folder and attempts JD capture from the posting URL in the background
- Comments/notes system per application with timestamped history
- Linked agent runs — automatically links generated resumes/prep docs to applications; editing the posting URL re-captures the JD
- Sorting, filtering (status, match score, DUA, search), pagination
- CSV and formatted Excel export with frozen headers and alternating rows

### Calendar
- Create, view, update, and delete calendar events (interviews, deadlines, follow-ups, custom)
- Reminders via email (Resend) and/or Slack DM — configurable offset in minutes, multi-channel
- Per-user event cap (1,000); per-event reminder cap (10)
- Events linkable to application tracker records and run IDs
- Accessible from the web UI (`/calendar.html`) and all `/cal-*` Slack commands

### Auth & Accounts
- Email/password auth with scrypt hashing and HMAC-signed stateless session cookies (30-day TTL)
- **Google OAuth** — sign in with Google; auto-links to existing email/password accounts
- **Email verification** via Resend — verification banner shown until confirmed; all emails sent from `hello@cdlav.us`
- **Email change** — requires current password; triggers re-verification; invalidates existing session
- Role-based access: `user` and `admin` roles
- Admin accounts restricted to the admin dashboard only
- Per-request session validation checks `active` flag and password-change fingerprint (`pwv`)

### Security
- `Strict-Transport-Security`, `X-Frame-Options`, `X-Content-Type-Options`, `Content-Security-Policy`, `Referrer-Policy` on every response
- Rate limiting on login (10/min), register (5/hr), resend-verification (3/hr), change-password (5/hr), change-email (5/hr), resume-upload (10/hr), forgot-password (3/hr), reset-password (5/hr)
- SSRF guard on webhook URLs (DNS resolution + private-net check), re-applied at delivery time
- Webhook HMAC secrets encrypted at rest with AES-256-GCM (key derived from `SESSION_SECRET`)
- `safeHref()` scheme validation on all user-supplied URLs rendered as `href`/`src` in the frontend
- Audit log stored as individual S3 objects (atomic writes, no cross-machine race condition)
- Per-user record cache (30s TTL) avoids S3 round-trips on every authenticated request

### Admin Dashboard
- **Users** — manage all accounts, email verification, role, active/deactivated status; view runs count, last login, joined date; search, filter, sort, paginate
- **All Applications** — cross-user application oversight with full filtering, sorting, pagination, and Excel/CSV export
- **All Agent Runs** — full Drive-backed run history across all users with type detection, filters, sort, export
- **Audit Log** — unified event log across user and application events; server-side pagination, filter by event ID, action, actor, source, date range
- **Webhooks** — create and manage outbound webhooks for event streaming to Slack, MS Teams, Grafana Loki, and custom endpoints
- **Knowledge Base** — create, edit, and delete KB articles and categories; Quill WYSIWYG editor with Source/Preview toggle; seed KB from frontend constants; filter by category or search
- Admin pages have a dedicated header nav (wrench icon → admin dashboard, no tracker/agents/calendar links); lazy loading and mobile card layout on the users table

### Webhooks
- Event-driven delivery for every audit action
- Payload formats: Generic JSON, Slack Block Kit, MS Teams MessageCard, Grafana Loki
- Delivery filters: actor (email/user ID), source, action category, application ID
- HMAC-SHA256 signing (`X-Hub-Signature-256`) for receiver verification; secret encrypted at rest
- Per-webhook delivery history (last 25), stats, test button
- SSRF guard re-applied at delivery time (DNS rebinding protection)

### Knowledge Base
- Public KB at `/kb.html` — searchable article library with category sidebar; articles rendered from HTML body (Quill output)
- Admin-managed via the Knowledge Base tab in `/admin.html`
- Quill WYSIWYG editor with Source/Preview toggle in the article drawer
- Categories with icon (emoji), label, description, and optional `adminOnly` flag
- Seed endpoint (`POST /api/admin/kb/seed-from-file`) re-extracts the built-in article set from `frontend/kb.html` via Node.js
- Stored as a single JSON blob in Tigris (`kb/data.json`); seed data auto-applied if no blob exists yet

### Slack Bot
See [Slack Commands](#slack-commands) section below.

---

## Project Structure

```
job-apply/
├── api.py                     ← FastAPI backend (auth, runs, SSE, Drive proxy)
├── apply.py                   ← Core workflow engine + CLI entry point
├── slack_bot.py               ← Slack bot (all slash commands)
├── slack_manifest.yml         ← Slack app manifest (copy into app config)
├── slack_manifest.json        ← Same manifest in JSON format
├── CLAUDE.md                  ← Agent workflow instructions (Claude Code reads this)
├── profile.md                 ← Corey's voice, stories, metrics, do-not-use phrases
├── frontend/
│   ├── index.html             ← Agent SPA (run form, prep form, progress, results)
│   ├── tracking.html          ← Application tracker
│   ├── calendar.html          ← Calendar view
│   ├── admin.html             ← Admin dashboard (users, apps, runs, audit, webhooks, KB)
│   ├── kb.html                ← Public Knowledge Base (searchable, category sidebar)
│   ├── api-docs.html          ← API reference (rendered from Postman collection; ⬇ download button)
│   ├── login.html             ← Login + Google OAuth
│   ├── register.html
│   ├── profile.html           ← Profile settings (Markdown editor)
│   ├── marked.min.js          ← Bundled marked.js (used by profile.html)
│   └── img/logo.png           ← Single transparent-background logo (light + dark compatible)
├── routers/
│   ├── applications.py        ← Tracker CRUD + comments + linked runs
│   ├── calendar.py            ← Calendar event + reminder CRUD
│   ├── companies.py           ← Logo.dev company search proxy
│   ├── auth_google.py         ← Google OAuth flow
│   ├── admin.py               ← Admin-only endpoints + webhooks + audit
│   └── kb.py                  ← Knowledge Base CRUD (public list + admin create/update/delete/seed)
├── scripts/
│   ├── storage.py             ← Tigris S3 adapter
│   ├── applications.py        ← Application storage layer
│   ├── calendar.py            ← Calendar + reminder storage layer
│   ├── session.py             ← Shared HMAC session token helpers
│   ├── user_audit.py          ← Per-user audit event log (per-event S3 objects)
│   ├── webhooks.py            ← Webhook storage + delivery engine
│   ├── email_verification.py  ← One-time verification tokens
│   └── office/                ← DOCX unpack / pack / validate
├── resumes/
│   └── master.docx            ← Source-of-truth resume (never use an output file)
├── output/                    ← Generated files (gitignored)
├── requirements.txt
├── Dockerfile
└── fly.toml
```

---

## Web App Usage

1. Go to https://apply.cdlav.us/
2. Register (email/password or Google) and upload `master.docx` + paste your `profile.md`
3. **Agent tab** — paste a job posting, enter company + role, hit **Generate**; use **Application Questions** to draft answers to supplemental app questions; or use **Interview Prep** for a prep doc
4. **Tracker tab** — track applications, add notes, link to agent runs
5. **Calendar tab** — view and manage interview events and deadlines with Slack/email reminders
6. **Knowledge Base** (`/kb.html`) — searchable help articles; admin-managed via the KB tab in the admin dashboard
7. **API Reference** (`/api-docs.html`) — full endpoint browser rendered from the Postman collection, with a ⬇ download button for the collection JSON
8. **Profile** — update display name, email, password, profile guide (Markdown editor), and resume
9. Admins are redirected to `/admin.html` automatically

### Application Questions
- Select an existing tracker application — requires a saved `job_description.md` in the app's Drive folder
- Paste the question from the application form, choose a tone, and optionally set a character limit
- The agent may ask clarifying questions before generating (e.g., "Which project should I highlight?") — answer them and it refines
- Output: editable answer with live character count, copy to clipboard, and follow-up refinement chips

### Interview Prep
- Select an existing tracker application — requires a saved `job_description.md` in the app's Drive folder (run the resume agent first if it doesn't exist)
- Enter interview round and optional focus/slant; company and role auto-fill from the selected application
- Output: compact 2-page DOCX reference card uploaded to Drive and available for download

---

## Slack Commands

| Category | Command | Description |
|---|---|---|
| 🤖 Agent | `/apply` | Generate resume + ATS resume + cover letter |
| 🤖 Agent | `/aq` | Answer an application question using your resume & JD |
| 🤖 Agent | `/prep` | Generate interview prep document |
| 🤖 Agent | `/optimize` | Refine an existing run's documents from a prompt (picks most recent Drive folder) |
| 🤖 Agent | `/rescore` | Re-score resume/JD match for an application |
| 🤖 Agent | `/runs` | List recent Drive run folders |
| 📅 Calendar | `/cal-today` | Show today's events |
| 📅 Calendar | `/cal-week` | Show next 7 days |
| 📅 Calendar | `/cal-add` | Add a calendar event (modal — type, date, time, timezone, reminders, linked app) |
| 📅 Calendar | `/cal-view` | View full details of an event |
| 📅 Calendar | `/cal-delete` | Delete an event (two-step confirm) |
| 📋 Tracker | `/tracker` | Pipeline summary by status |
| 📋 Tracker | `/track-list [status]` | List applications (optional status filter) |
| 📋 Tracker | `/track-view` | View full details of an application |
| 📋 Tracker | `/track-add` | Add a new application (Logo.dev company search, all fields except priority) |
| 📋 Tracker | `/track-update` | Two-step: pick app → edit all fields pre-filled (setting status to Applied auto-sets date applied) |
| 📋 Tracker | `/track-note` | Add a comment to an application |
| 📋 Tracker | `/track-delete` | Delete an application (two-step confirm) |
| 🔍 Lookup | `/company [name]` | Search company info via Logo.dev |
| 🔍 Lookup | `/whoami` | Show your account details |
| 👤 Profile | `/profile-resume` | Instructions for uploading a new master resume via DM |
| 👤 Profile | `/profile-guide` | Edit your profile & voice guide (modal, pre-filled) |
| 👤 Profile | `/notifications` | View and toggle email notification preferences |
| 🛠️ System | `/help` | Full command reference |

The bot also publishes a dynamic **App Home tab** showing live pipeline stats, upcoming calendar events, and a quick command reference — opens when you click the app's Home tab in Slack.

---

## CLI Usage

```bash
pip install -r requirements.txt && npm install
export ANTHROPIC_API_KEY=sk-ant-...

python apply.py --job jobs/job.txt --company "Acme" --role "Solutions Engineer"
python apply.py --job jobs/job.txt --company "Acme" --role "SE" --contact "Jane Smith"
python apply.py --job jobs/job.txt --company "Acme" --role "SE" --debug
python apply.py --job jobs/job.txt --company "Acme" --role "SE" --dry-run
```

Output files land in `output/[Company]_[Role]/`:
- `Resume_CoreyLaverdiere_[Company]_[Role].docx`
- `Resume_CoreyLaverdiere_[Company]_[Role]_ATS.docx`
- `CoverLetter_CoreyLaverdiere_[Company]_[Role].docx`
- `job_description.md` — saved for future JD auto-load

---

## Local Development

```bash
pip install -r requirements.txt && npm install

export ANTHROPIC_API_KEY=sk-ant-...
export SESSION_SECRET=any-random-string

# Tigris S3 (user accounts, resumes, profiles, tracker data)
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_ENDPOINT_URL_S3=https://fly.storage.tigris.dev
export BUCKET_NAME=job-apply-corey

uvicorn api:app --reload --port 8000
open http://localhost:8000
```

---

## Google Drive Setup (one-time)

```bash
# Download OAuth credentials from Google Cloud Console
# APIs & Services → Credentials → Create → OAuth client ID → Desktop app
# Save as: gdrive_credentials.json (project root)
python3 setup_gdrive.py

# On Fly.io — push token as a secret:
fly secrets set GDRIVE_TOKEN_JSON="$(cat ~/.config/job-apply/gdrive_token.json)"
```

---

## Google OAuth Setup (one-time)

1. Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client → Web application
2. Authorized redirect URI: `https://apply.cdlav.us/api/auth/google/callback`
3. `fly secrets set GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=...`

---

## Deployment (Fly.io)

```bash
fly deploy --app job-apply-corey
```

The app runs as **two process groups** on Fly.io (defined in `fly.toml`), each scaled to **1 machine**:

| Process | Command | Machine | Notes |
|---|---|---|---|
| `web` | `uvicorn api:app …` | 1 GB, auto-stop | FastAPI web server — 1 machine required (SSE state is in-memory) |
| `bot` | `python slack_bot.py` | 256 MB, always-on | Slack Socket Mode bot |

> **Important:** Keep `web` scaled to exactly 1 machine. Run and prep state is held
> in-memory; multiple web machines will cause SSE streams to 404 on the wrong instance.
> If you need to scale, replace the in-memory `_runs`/`_preps`/`_app_questions` dicts with a shared store (Redis, etc.).

Both process groups share the same Docker image and all Fly secrets.

**Required secrets:**

| Secret | Description |
|--------|-------------|
| `ANTHROPIC_API_KEY` | Claude API key |
| `SESSION_SECRET` | HMAC signing key for session tokens (also used to derive webhook secret encryption key) |
| `AWS_ACCESS_KEY_ID` | Tigris key |
| `AWS_SECRET_ACCESS_KEY` | Tigris secret |
| `AWS_ENDPOINT_URL_S3` | `https://fly.storage.tigris.dev` |
| `BUCKET_NAME` | Tigris bucket name |
| `RESEND_API_KEY` | Resend — email verification, password-change, and calendar reminder emails |
| `RESEND_FROM` | Sender address (default: `Job Apply <hello@cdlav.us>`) |
| `APP_URL` | Public app URL (default: `https://apply.cdlav.us`) |
| `APP_USER_EMAIL` | Primary user email — used by the Slack bot to resolve its API identity |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret |
| `LOGODEV_API_KEY` | Logo.dev secret key (`sk_`) for company search API |
| `BOT_API_KEY` | Shared secret between Slack bot and web API |
| `SLACK_BOT_TOKEN` | Slack bot token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | Slack signing secret |
| `SLACK_APP_TOKEN` | App-level token (`xapp-...`) — **required** for Socket Mode |
| `SLACK_NOTIFY_USER_ID` | Slack user ID to DM for calendar reminders |
| `GDRIVE_TOKEN_JSON` | Google Drive OAuth token JSON |
| `GDRIVE_PARENT_FOLDER_ID` | Drive folder ID for run output (`Job Applications`) |
| `TEST_RUNNER_SLACK_USER_ID` | Slack user ID authorised to run `/run-tests` (falls back to `SLACK_NOTIFY_USER_ID`) |

---

## Admin Dashboard

Navigate to `/admin.html` (admins are redirected there automatically on login).

Use `?tab=` to deep-link to a specific tab:
- `/admin.html?tab=users`
- `/admin.html?tab=applications`
- `/admin.html?tab=runs`
- `/admin.html?tab=auditlog`
- `/admin.html?tab=webhooks`
- `/admin.html?tab=kb`

---

## API

See `JobApply.postman_collection.json` for the full request/response reference.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/health` | — | Liveness check (full details only for authenticated users) |
| POST | `/api/auth/register` | — | Create account + upload resume |
| POST | `/api/auth/login` | — | Get session cookie |
| POST | `/api/auth/logout` | cookie | Clear session |
| GET | `/api/auth/me` | cookie | Current user info + role + email_verified + active model |
| GET | `/api/auth/google` | — | Start Google OAuth flow |
| GET | `/api/auth/google/callback` | — | Google OAuth callback |
| GET | `/api/auth/verify-email?token=` | — | Consume email verification token |
| POST | `/api/auth/resend-verification` | cookie | Resend verification email |
| POST | `/api/auth/forgot-password` | — | Send password-reset link to email (always returns 200; rate-limited: 3/hr) |
| POST | `/api/auth/reset-password` | — | Set new password using a one-time reset token (expires in 1 hour; rate-limited: 5/hr) |
| GET | `/api/profile` | cookie | Profile + resume metadata |
| PUT | `/api/profile` | cookie | Update display name or profile text |
| POST | `/api/profile/resume` | cookie | Replace master resume (rate-limited: 10/hr) |
| POST | `/api/profile/password` | cookie | Change password (rate-limited: 5/hr) |
| POST | `/api/profile/email` | cookie | Change email — requires current password, sends re-verification, invalidates session (rate-limited: 5/hr) |
| GET | `/api/audit/me` | cookie | Current user's audit event log |
| GET | `/api/calendar` | cookie | List events (optional `?from=&to=` ISO range filter) |
| POST | `/api/calendar` | cookie | Create event with optional reminders |
| GET | `/api/calendar/upcoming` | cookie | Next 7 days (used by Slack home tab) |
| GET | `/api/calendar/{id}` | cookie | Get single event |
| PUT | `/api/calendar/{id}` | cookie | Update event (reminders recalculated if datetime changes) |
| DELETE | `/api/calendar/{id}` | cookie | Delete event + all its reminders |
| GET | `/api/applications` | cookie | List applications (paginated) |
| POST | `/api/applications` | cookie | Create application |
| GET | `/api/applications/{id}` | cookie | Get full application record |
| PUT | `/api/applications/{id}` | cookie | Update application — auto-sets `date_applied` on status→Applied; re-captures JD if `url` changes |
| DELETE | `/api/applications/{id}` | cookie | Delete application |
| GET | `/api/applications/{id}/audit` | cookie | Application-level audit log |
| POST | `/api/applications/{id}/comments` | cookie | Add comment |
| PUT | `/api/applications/{id}/comments/{cid}` | cookie | Edit comment |
| DELETE | `/api/applications/{id}/comments/{cid}` | cookie | Delete comment |
| POST | `/api/applications/{id}/runs` | cookie | Link a Drive run to an application |
| DELETE | `/api/applications/{id}/runs/{lid}` | cookie | Unlink a run |
| POST | `/api/applications/{id}/score` | cookie | Run (or re-run) resume↔JD match scoring; persists result to record |
| POST | `/api/applications/{id}/extract-jd` | cookie | Extract JD text from the app's posting URL via Claude |
| POST | `/api/applications/{id}/setup-folder` | cookie | Create Drive folder + attempt JD capture in background; returns 202 immediately |
| GET | `/api/companies/search?q=` | — | Logo.dev company search — returns `name`, `domain`, `description`; logos constructed client-side via `img.logo.dev` |
| POST | `/api/run` | cookie | Start resume generation run → returns `{run_id, machine_id}`; accepts `jd_folder_id` to load JD server-side from Drive |
| GET | `/api/run/{id}/stream` | cookie | SSE progress stream (`done` event includes `replacements_warning` if < 70% XML edits succeeded) |
| GET | `/api/run/{id}/status` | cookie | Poll run status |
| GET | `/api/run/{id}/files/{name}` | cookie | Download output file |
| POST | `/api/prep` | cookie | Start interview prep run → returns `{prep_id, machine_id}` |
| GET | `/api/prep/{id}/stream` | cookie | SSE prep progress stream |
| GET | `/api/prep/{id}/status` | cookie | Poll prep status |
| GET | `/api/prep/{id}/files/{name}` | cookie | Download prep DOCX |
| POST | `/api/aq` | cookie | Start application question run → returns `{aq_id, machine_id}`; agent may emit `clarification` SSE event |
| POST | `/api/aq/{id}/clarify` | cookie | Submit clarification answers to unblock a paused AQ run |
| GET | `/api/aq/{id}/stream` | cookie | SSE stream: `progress`, `clarification`, `done` (answer + char_count + follow_ups), `error` |
| GET | `/api/aq/{id}/status` | cookie | Poll AQ status |
| POST | `/api/thankyou` | cookie | Start thank-you email run → returns `{ty_id, machine_id}` |
| GET | `/api/thankyou/{id}/stream` | cookie | SSE stream: `progress`, `done` (email_text + subject + files), `error` |
| GET | `/api/thankyou/{id}/status` | cookie | Poll thank-you status |
| GET | `/api/thankyou/{id}/files/{name}` | cookie | Download thank-you DOCX |
| POST | `/api/optimize` | cookie | Optimize an existing run's resume/cover letter in place per a user instruction → returns `{optimize_id, machine_id}`; folder ownership verified via Tigris app records; rate-limited to one active optimize per user |
| GET | `/api/optimize/{id}/stream` | cookie | SSE optimize progress stream (`done` event includes `change_summary` list + `replacements_warning`) |
| GET | `/api/optimize/{id}/status` | cookie | Poll optimize status: `queued | running | done | error` |
| GET | `/api/optimize/{id}/files/{name}` | cookie | Download optimized DOCX |
| POST | `/api/jd/format` | cookie | AI-format a raw job description (returns cleaned Markdown) |
| GET | `/api/postman` | — | Download the Postman collection JSON |
| GET | `/api/gdrive/runs` | cookie | List Drive run folders |
| GET | `/api/gdrive/runs/{folder_id}/job_posting` | cookie | Fetch saved JD from Drive — prefers `job_description.md`, falls back to `job_posting.txt`; ownership verified via Tigris app records |
| PUT | `/api/gdrive/runs/{folder_id}/job_posting` | cookie | Upsert `job_description.md` in Drive folder |
| GET | `/api/runs` | cookie | List local run folders by user |
| GET | `/api/runs/{folder}/job_posting` | cookie | Fetch saved JD from local run folder |
| GET | `/api/config/model` | cookie | Get active Claude model |
| PUT | `/api/config/model` | admin | Set active Claude model |
| GET | `/api/config/models` | admin | List allowed models |
| GET | `/api/kb/articles` | cookie | List all KB articles + categories |
| GET | `/api/kb/articles/{id}` | cookie | Get one KB article |
| GET | `/api/kb/categories` | cookie | List KB categories |
| POST | `/api/admin/kb/articles` | admin | Create KB article |
| PUT | `/api/admin/kb/articles/{id}` | admin | Update KB article |
| DELETE | `/api/admin/kb/articles/{id}` | admin | Delete KB article |
| POST | `/api/admin/kb/categories` | admin | Create KB category |
| PUT | `/api/admin/kb/categories/{id}` | admin | Update KB category |
| DELETE | `/api/admin/kb/categories/{id}` | admin | Delete KB category |
| POST | `/api/admin/kb/seed` | admin | Replace entire KB from JSON payload |
| POST | `/api/admin/kb/seed-from-file` | admin | Re-extract KB from `frontend/kb.html` via Node.js and seed to Tigris |
| GET | `/api/notifications/action?token=` | — | Consume a signed one-time notification token (from nudge/follow-up emails) — executes `status` or `snooze` action; redirects to tracker on success |
| GET | `/api/admin/users` | admin | List all users |
| PUT | `/api/admin/users/{id}` | admin | Edit user (name, email, role, active, verified) — invalidates user cache |
| PUT | `/api/admin/users/{id}/role` | admin | Set user role only (`user`/`admin`) — Slack bot compat; invalidates user cache |
| POST | `/api/admin/users/{id}/resend-verification` | admin | Resend verification as admin |
| GET | `/api/admin/users/{id}/applications` | admin | List all applications for a specific user |
| GET | `/api/admin/applications` | admin | All applications across all users |
| GET | `/api/admin/applications/{uid}/{aid}` | admin | Full application record |
| PUT | `/api/admin/applications/{uid}/{aid}` | admin | Admin update application |
| DELETE | `/api/admin/applications/{uid}/{aid}` | admin | Admin delete application |
| POST | `/api/admin/applications/{uid}/{aid}/comments` | admin | Admin add comment |
| GET | `/api/admin/runs` | admin | All Drive run folders across all users |
| GET | `/api/admin/audit` | admin | Unified audit log (paginated) |
| GET | `/api/admin/audit/export` | admin | Full audit log (no pagination, for export) |
| GET | `/api/admin/audit/action-types` | admin | Known audit action type list |
| POST | `/api/admin/log-activity` | admin | Log a custom admin activity event |
| GET | `/api/admin/webhooks` | admin | List webhooks |
| POST | `/api/admin/webhooks` | admin | Create webhook (secret encrypted at rest) |
| GET | `/api/admin/webhooks/{id}` | admin | Get webhook details (secret redacted) |
| PUT | `/api/admin/webhooks/{id}` | admin | Update webhook |
| DELETE | `/api/admin/webhooks/{id}` | admin | Delete webhook |
| POST | `/api/admin/webhooks/{id}/test` | admin | Send test delivery |
| GET | `/api/admin/webhooks/{id}/deliveries` | admin | Last 25 deliveries |

---

## Maintaining the Agent

### If XML replacements start failing
Run with `--debug` to inspect `unpacked/word/document.xml`. Section text drifts
when `master.docx` is edited in Word — update known strings in `profile.md`. The
SSE `done` event now includes a `replacements_warning` field if < 70% of edits
succeeded, so the web UI can surface it without requiring a log review.

### If cover letter voice drifts
Edit `profile.md` → "Voice & Tone Rules" and "DO NOT" sections.

### If framing angle is consistently wrong for a role type
Edit `CLAUDE.md` → "Common Role Type → Framing Angle Reference" table.

### If interview prep content is too verbose
The prompt in `apply.py` (`generate_interview_prep`) has hard `MAX N WORDS` limits
per field. Tighten these if Claude is still over-generating.

### If interview prep proof points reference old roles
The recency rule is enforced in the prompt: only Applause (2016+), ProdPerfect,
HSP Group, eHealth, and GitHub projects are allowed. Fidelity is explicitly excluded.

### If Google Drive token expires
The token is refreshed automatically and persisted to Tigris (`system/gdrive_token.json`) so it survives container restarts. If it's fully revoked (e.g. after revoking app access in Google Account settings), re-authorize:
```bash
rm ~/.config/job-apply/gdrive_token.json
python3 setup_gdrive.py
fly secrets set GDRIVE_TOKEN_JSON="$(cat ~/.config/job-apply/gdrive_token.json)"
# Then clear the stale Tigris copy so the new token takes precedence on next boot:
fly ssh console -C "python3 -c \"from scripts import storage; storage.delete_text('system/gdrive_token.json')\""
fly deploy --app job-apply-corey
```

### If webhook deliveries are being blocked
The SSRF guard runs at both write time and delivery time. A `Delivery blocked` error
in the delivery log means the URL resolved to a private/internal IP at delivery time
(possible DNS rebinding). Update the webhook URL to a public endpoint.

### Webhook secret rotation
Webhook secrets are encrypted with AES-256-GCM using a key derived from `SESSION_SECRET`.
If you rotate `SESSION_SECRET`, existing webhook secrets will fail to decrypt (delivery
will silently send unsigned requests). Re-save each webhook via `PUT /api/admin/webhooks/{id}`
with the secret to re-encrypt under the new key.
