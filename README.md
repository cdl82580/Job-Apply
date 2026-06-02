# job-apply — Corey's Job Application Agent

A Claude-powered web app (and Slack bot) that takes a job posting and produces a
tailored resume, ATS resume, and cover letter in under 2 minutes. Includes a
full-featured application tracker, admin dashboard, webhook system, and audit logging.

**Live app:** https://job-apply-corey.fly.dev/

---

## Features

### Agent
- **Tailored resume** — styled DOCX with brand colors, targeted bullets, competency grid
- **ATS resume** — plain single-column DOCX, no tables or text boxes, parser-safe
- **Cover letter** — voice-matched DOCX tailored to the role and hiring manager
- **Google Drive sync** — all output files uploaded automatically to your Drive folder
- **Interview Prep** — two-column 9-section reference card (role fit map, gap bridges, anchor stories, likely questions, differentiating edge) tailored to the interviewer and round type
- **SSE progress streaming** — live log output while the agent runs

### Application Tracker
- Full CRUD for job applications — company (via BrandFetch lookup), role, status, priority, recruiter, salary, DUA tracking
- Comments/notes system per application with timestamped history
- Linked agent runs — automatically links generated resumes/prep docs to applications
- Sorting, filtering, pagination, search by ID
- CSV and formatted Excel export with frozen headers and alternating rows

### Auth & Accounts
- Email/password auth with scrypt hashing and HMAC-signed stateless session cookies
- **Google OAuth** — sign in with Google; auto-links to existing email/password accounts
- **Email verification** via Resend — verification banner shown until confirmed
- Role-based access: `user` and `admin` roles
- Admin accounts restricted to the admin dashboard only

### Admin Dashboard
- **Users** — manage all accounts, email verification, role, active/deactivated status; view runs count, last login, joined date; search, filter, sort, paginate
- **All Applications** — cross-user application oversight with full filtering, sorting, pagination, and Excel/CSV export
- **All Agent Runs** — full Drive-backed run history across all users with type detection, filters, sort, export
- **Audit Log** — unified event log across user and application events; server-side pagination, filter by event ID, action, actor, source, date range
- **Webhooks** — create and manage outbound webhooks for event streaming to Slack, MS Teams, Grafana Loki, and custom endpoints

### Webhooks
- Event-driven delivery for every audit action
- Payload formats: Generic JSON, Slack Block Kit, MS Teams MessageCard, Grafana Loki
- Delivery filters: actor (email/user ID), source, action category, application ID
- HMAC-SHA256 signing (`X-Hub-Signature-256`) for receiver verification
- Per-webhook delivery history (last 25), stats, test button

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
│   ├── index.html             ← Agent SPA (form, progress, results, prep)
│   ├── tracking.html          ← Application tracker
│   ├── admin.html             ← Admin dashboard
│   ├── login.html             ← Login + Google OAuth
│   ├── register.html
│   └── profile.html           ← Profile settings (Markdown editor)
├── routers/
│   ├── applications.py        ← Tracker CRUD + comments + linked runs
│   ├── companies.py           ← BrandFetch proxy
│   ├── auth_google.py         ← Google OAuth flow
│   └── admin.py               ← Admin-only endpoints + webhooks + audit
├── scripts/
│   ├── storage.py             ← Tigris S3 adapter
│   ├── applications.py        ← Application storage layer
│   ├── session.py             ← Shared HMAC session token helpers
│   ├── user_audit.py          ← Per-user audit event log
│   ├── webhooks.py            ← Webhook storage + delivery engine
│   ├── email_verification.py  ← One-time verification tokens
│   └── office/                ← DOCX unpack / pack / validate
├── resumes/
│   └── master.docx            ← Source-of-truth resume (never use an output file)
├── output/                    ← Generated files (gitignored)
├── Dockerfile
└── fly.toml
```

---

## Web App Usage

1. Go to https://job-apply-corey.fly.dev/
2. Register (email/password or Google) and upload `master.docx` + paste your `profile.md`
3. **Agent tab** — paste a job posting, enter company + role, hit **Generate**
4. **Tracker tab** — track applications, add notes, link to agent runs
5. **Profile** — update display name, profile guide (Markdown editor), resume
6. Admins are redirected to `/admin.html` automatically

---

## Slack Commands

| Category | Command | Description |
|---|---|---|
| 🤖 Agent | `/apply` | Generate resume + ATS resume + cover letter |
| 🤖 Agent | `/prep` | Generate interview prep document |
| 🤖 Agent | `/runs` | List recent Drive run folders |
| 📋 Tracker | `/tracker` | Pipeline summary by status |
| 📋 Tracker | `/track-list [status]` | List applications (optional status filter) |
| 📋 Tracker | `/track-view` | View full details of an application |
| 📋 Tracker | `/track-add` | Add a new application record |
| 📋 Tracker | `/track-update` | Update application status + optional note |
| 📋 Tracker | `/track-note` | Add a comment to an application |
| 📋 Tracker | `/track-delete` | Delete an application (two-step confirm) |
| 🔍 Lookup | `/company [name]` | Search company info via BrandFetch |
| 🔍 Lookup | `/me` | Show your account details |
| 🔍 Lookup | `/activity` | Show your 10 most recent audit events |
| 🛠️ System | `/jobstatus` | Check API health |
| 🛠️ System | `/resend-verify` | Resend email verification |
| 🛠️ System | `/help` | Full command reference |

---

## CLI Usage

```bash
pip install -r requirements.txt && npm install
export ANTHROPIC_API_KEY=sk-ant-...

python apply.py --job jobs/job.txt --company "Acme" --role "Solutions Engineer"
python apply.py --job jobs/job.txt --company "Acme" --role "SE" --contact "Jane Smith"
python apply.py --job jobs/job.txt --company "Acme" --role "SE" --debug
```

Output files land in `output/[Company]_[Role]/`:
- `Resume_CoreyLaverdiere_[Company]_[Role].docx`
- `Resume_CoreyLaverdiere_[Company]_[Role]_ATS.docx`
- `CoverLetter_CoreyLaverdiere_[Company]_[Role].docx`

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
2. Authorized redirect URI: `https://job-apply-corey.fly.dev/api/auth/google/callback`
3. `fly secrets set GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=...`

---

## Deployment (Fly.io)

```bash
fly deploy --app job-apply-corey
```

| Secret | Description |
|--------|-------------|
| `ANTHROPIC_API_KEY` | Claude API key |
| `SESSION_SECRET` | HMAC signing key for session tokens (persistent across restarts) |
| `AWS_ACCESS_KEY_ID` | Tigris key |
| `AWS_SECRET_ACCESS_KEY` | Tigris secret |
| `AWS_ENDPOINT_URL_S3` | `https://fly.storage.tigris.dev` |
| `BUCKET_NAME` | Tigris bucket name |
| `RESEND_API_KEY` | Resend — email verification + password-change emails |
| `RESEND_FROM` | Sender address (default: `Job Apply <onboarding@resend.dev>`) |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `BRANDFETCH_API_KEY` | BrandFetch API key for company search |
| `BOT_API_KEY` | Slack bot authentication key |
| `SLACK_BOT_TOKEN` | Slack bot token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | Slack signing secret |
| `SLACK_APP_TOKEN` | Slack app-level token (`xapp-...`) for Socket Mode (optional) |
| `GDRIVE_TOKEN_JSON` | Google Drive OAuth token JSON |
| `GDRIVE_PARENT_FOLDER_ID` | Drive folder ID for run output (`Job Applications`) |

---

## Admin Dashboard

Navigate to `/admin.html` (admins are redirected there automatically on login).

Use `?tab=` to deep-link to a specific tab:
- `/admin.html?tab=users`
- `/admin.html?tab=applications`
- `/admin.html?tab=runs`
- `/admin.html?tab=auditlog`
- `/admin.html?tab=webhooks`

---

## API

See `JobApply.postman_collection.json` for the full request/response reference.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/health` | — | Liveness check |
| POST | `/api/auth/register` | — | Create account + upload resume |
| POST | `/api/auth/login` | — | Get session cookie |
| POST | `/api/auth/logout` | cookie | Clear session |
| GET | `/api/auth/me` | cookie | Current user info + role + email_verified |
| GET | `/api/auth/google` | — | Start Google OAuth flow |
| GET | `/api/auth/google/callback` | — | Google OAuth callback |
| GET | `/api/auth/verify-email?token=` | — | Consume email verification token |
| POST | `/api/auth/resend-verification` | cookie | Resend verification email |
| GET | `/api/profile` | cookie | Profile + resume metadata |
| PUT | `/api/profile` | cookie | Update display name or profile text |
| POST | `/api/profile/resume` | cookie | Replace master resume |
| POST | `/api/profile/password` | cookie | Change password |
| GET | `/api/audit/me` | cookie | Current user's audit event log |
| GET | `/api/applications` | cookie | List applications (paginated) |
| POST | `/api/applications` | cookie | Create application |
| GET | `/api/applications/{id}` | cookie | Get full application record |
| PUT | `/api/applications/{id}` | cookie | Update application |
| DELETE | `/api/applications/{id}` | cookie | Delete application |
| GET | `/api/applications/{id}/audit` | cookie | Application-level audit log |
| POST | `/api/applications/{id}/comments` | cookie | Add comment |
| PUT | `/api/applications/{id}/comments/{cid}` | cookie | Edit comment |
| DELETE | `/api/applications/{id}/comments/{cid}` | cookie | Delete comment |
| POST | `/api/applications/{id}/runs` | cookie | Link a Drive run to an application |
| DELETE | `/api/applications/{id}/runs/{lid}` | cookie | Unlink a run |
| GET | `/api/companies/search?q=` | — | BrandFetch company search |
| POST | `/api/run` | cookie | Start resume generation run |
| GET | `/api/run/{id}/stream` | cookie | SSE progress stream |
| GET | `/api/run/{id}/status` | cookie | Poll run status |
| GET | `/api/run/{id}/files/{name}` | cookie | Download output file |
| POST | `/api/prep` | cookie | Start interview prep run |
| GET | `/api/prep/{id}/stream` | cookie | SSE prep progress stream |
| GET | `/api/prep/{id}/status` | cookie | Poll prep status |
| GET | `/api/prep/{id}/files/{name}` | cookie | Download prep DOCX |
| GET | `/api/gdrive/runs` | cookie | List Drive run folders |
| GET | `/api/gdrive/runs/{folder_id}/job_posting` | cookie | Fetch saved JD from Drive |
| GET | `/api/admin/users` | admin | List all users |
| PUT | `/api/admin/users/{id}` | admin | Edit user (name, email, role, active, verified) |
| POST | `/api/admin/users/{id}/resend-verification` | admin | Resend verification as admin |
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
| POST | `/api/admin/webhooks` | admin | Create webhook |
| GET | `/api/admin/webhooks/{id}` | admin | Get webhook details |
| PUT | `/api/admin/webhooks/{id}` | admin | Update webhook |
| DELETE | `/api/admin/webhooks/{id}` | admin | Delete webhook |
| POST | `/api/admin/webhooks/{id}/test` | admin | Send test delivery |
| GET | `/api/admin/webhooks/{id}/deliveries` | admin | Last 25 deliveries |

---

## Maintaining the Agent

### If XML replacements start failing
Run with `--debug` to inspect `unpacked/word/document.xml`. Section text drifts
when `master.docx` is edited in Word — update known strings in `profile.md`.

### If cover letter voice drifts
Edit `profile.md` → "Voice & Tone Rules" and "DO NOT" sections.

### If framing angle is consistently wrong for a role type
Edit `CLAUDE.md` → "Common Role Type → Framing Angle Reference" table.

### If Google Drive token expires
```bash
rm ~/.config/job-apply/gdrive_token.json
python3 setup_gdrive.py
fly secrets set GDRIVE_TOKEN_JSON="$(cat ~/.config/job-apply/gdrive_token.json)"
```
