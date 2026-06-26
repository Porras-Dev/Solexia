# Solexia — Autonomous Solar Lead-Generation Agent

An end-to-end Python pipeline that finds businesses in Cáceres (Spain) that are
good candidates for rooftop solar installation, scores them with Claude AI, generates
personalised PDF proposals, sends them by email, and books follow-up appointments in
Google Calendar — all orchestrated nightly by n8n running in Docker.

---

## Table of Contents

1. [How it works](#1-how-it-works)
2. [Pipeline architecture](#2-pipeline-architecture)
3. [Tech stack](#3-tech-stack)
4. [Project structure](#4-project-structure)
5. [Installation](#5-installation)
6. [Data & privacy](#6-data--privacy)
7. [Running the pipeline](#7-running-the-pipeline)
8. [API server](#8-api-server)
9. [n8n automation flows](#9-n8n-automation-flows)
10. [Approximate operating costs](#10-approximate-operating-costs)
11. [Author](#11-author)

---

## 1. How it works

```
Every night at 3:00 AM
        │
        ▼
  n8n calls the local API server (port 8765)
        │
        ▼
  5-step pipeline runs automatically
        │
        ▼
  Telegram notification with results
        │
        ▼
  Next morning: manual email send via n8n
        │
        ▼
  Every day at 9:00 AM: automatic follow-up for leads with no reply
```

The pipeline is designed to operate with minimal human intervention.
The only required manual step is reviewing and approving the email batch
before it is sent (via the n8n panel).

---

## 2. Pipeline architecture

### Automated pipeline (5 steps)

```
 ┌─────────────────────────────────────────────────────────────────────┐
 │                      SOLEXIA PIPELINE                               │
 └─────────────────────────────────────────────────────────────────────┘

  STEP 1 ── SCRAPER                    src/scraper.py
  ┌─────────────────────┐
  │  Google Maps        │  Searches for businesses (restaurants, hotels,
  │  Places API         │  warehouses, factories …) within the target area.
  └─────────────────────┘
           │
           ▼  data/companies.json  (all raw leads)
  ─────────────────────────────────────────────────────────────────────
  STEP 2 ── SCORER                     src/scorer.py
  ┌─────────────────────┐
  │  Claude API         │  Rates each company 0-10 on solar potential:
  │  (AI scoring)       │  roof area, energy demand, location, sector.
  └─────────────────────┘
           │
           ▼  data/qualified_leads.json  (score ≥ threshold)
  ─────────────────────────────────────────────────────────────────────
  STEP 3 ── EMAIL GENERATOR            src/email_generator.py
  ┌─────────────────────┐
  │  Claude API         │  Writes a personalised outreach email for each
  │  (copywriting)      │  qualified lead. Subject + body tailored per sector.
  └─────────────────────┘
           │
           ▼  data/generated_emails.json
  ─────────────────────────────────────────────────────────────────────
  STEP 4 ── SOLAR CALCULATOR           src/solar_calculator.py
  ┌─────────────────────┐
  │  PVGIS API          │  Queries the EU Joint Research Centre for real
  │  (EU Commission)    │  irradiation data at each lead's coordinates.
  │                     │  Calculates estimated annual kWh + savings (€).
  └─────────────────────┘
           │
           ▼  data/leads_with_solar_data.json
  ─────────────────────────────────────────────────────────────────────
  STEP 5 ── PDF GENERATOR              src/pdf_generator.py
  ┌─────────────────────┐
  │  ReportLab          │  Generates a branded PDF proposal per lead with
  │  (PDF engine)       │  solar production chart, ROI table, and contact.
  └─────────────────────┘
           │
           ▼  data/proposals/<company>.pdf
```

### Post-pipeline steps (manual / semi-automatic)

```
  EMAIL SENDER         src/email_sender.py   ← triggered from n8n panel
  ┌─────────────────────┐
  │  Brevo SMTP         │  Sends generated emails with 30 s delay between
  │                     │  each one. Updates send status in generated_emails.json.
  └─────────────────────┘

  FOLLOW-UP            src/followup.py       ← runs daily at 9:00 AM via n8n
  ┌─────────────────────┐
  │  Brevo SMTP         │  Detects leads with no reply after N days and
  │                     │  sends a single personalised follow-up email.
  └─────────────────────┘

  CALENDAR MANAGER     src/calendar_manager.py   ← run manually as needed
  ┌─────────────────────┐
  │  Google Calendar    │  Books appointments for leads that replied.
  │  API                │  Reads availability and creates calendar events.
  └─────────────────────┘
```

---

## 3. Tech stack

| Layer | Technology | Purpose |
|---|---|---|
| AI scoring & copywriting | [Claude API](https://console.anthropic.com) (Anthropic) | Lead scoring, email generation |
| Lead discovery | Google Maps Places API | Business search by area and type |
| Solar data | [PVGIS API](https://re.jrc.ec.europa.eu/pvg_tools/) (EU Commission) | Real irradiation data, free |
| Email delivery | [Brevo](https://www.brevo.com) SMTP | Transactional email, free up to 300/day |
| Calendar | Google Calendar API | Appointment booking |
| Notifications | Telegram Bot API | Pipeline result alerts |
| Orchestration | [n8n](https://n8n.io) (self-hosted) | Scheduling, HTTP calls, alerts |
| AI chat (optional) | [Flowise](https://flowiseai.com) (self-hosted) | Visual LLM workflow editor |
| Infrastructure | Docker + Docker Compose | Containerised n8n + Flowise |
| PDF generation | ReportLab | Proposal PDF rendering |
| Runtime | Python 3.10+ | All pipeline scripts |

---

## 4. Project structure

```
agente-solar/
├── src/                        Pipeline scripts
│   ├── scraper.py              Step 1 — Google Maps scraper
│   ├── scorer.py               Step 2 — Claude API lead scorer
│   ├── email_generator.py      Step 3 — Claude API email writer
│   ├── solar_calculator.py     Step 4 — PVGIS solar calculator
│   ├── pdf_generator.py        Step 5 — PDF proposal generator
│   ├── email_sender.py         Sends generated emails via Brevo
│   ├── followup.py             Sends follow-up emails to cold leads
│   ├── calendar_manager.py     Books appointments in Google Calendar
│   ├── pipeline.py             Orchestrator: runs steps 1-5 in sequence
│   └── api_server.py           HTTP server (port 8765) called by n8n
├── data/                       Generated data (git-ignored)
│   ├── companies.json          Raw scraper output
│   ├── qualified_leads.json    Scored and filtered leads
│   ├── generated_emails.json   Email drafts + send status
│   ├── leads_with_solar_data.json  PVGIS-enriched leads
│   └── proposals/              One PDF per qualified lead
├── logs/                       Rotating log files (git-ignored)
│   ├── emails.log
│   ├── pvgis.log
│   ├── followup.log
│   └── server.log
├── n8n-flows/                  n8n flow definitions (import into n8n)
│   ├── nightly-pipeline.json
│   ├── email-send.json
│   └── automatic-followup.json
├── docs/                       Documentation and assets
├── utils.py                    Shared helpers (progress bars, formatting)
├── requirements.txt
├── docker-compose.yml          n8n + Flowise containers
├── .env                        Secrets — never commit (git-ignored)
└── .env.example                Template for .env
```

---

## 5. Installation

### Prerequisites

- Python 3.10 or later
- Docker + Docker Compose
- A terminal with the working directory set to `agente-solar/`

### Step 1 — Python dependencies

```bash
pip install -r requirements.txt
```

### Step 2 — Environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in every value. The required keys are:

| Variable | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) → API Keys |
| `GOOGLE_MAPS_API_KEY` | [Google Cloud Console](https://console.cloud.google.com) → Places API (New) |
| `TELEGRAM_BOT_TOKEN` | Telegram → @BotFather → /newbot |
| `TELEGRAM_CHAT_ID` | `api.telegram.org/bot{TOKEN}/getUpdates` → chat.id |
| `BREVO_SMTP_KEY` | Brevo → SMTP & API → Generate SMTP Key |
| `EMAIL_FROM_ADDRESS` | Your verified sender address in Brevo |

### Step 3 — Google Calendar credentials (optional)

Only needed if you plan to use `calendar_manager.py`:

1. Go to [Google Cloud Console](https://console.cloud.google.com) → APIs → Enable **Google Calendar API**
2. Credentials → Create → **OAuth 2.0** → Desktop application
3. Download the JSON file and save it as `credentials.json` in the project root
4. On first run the browser will open for authorisation; this creates `token_calendar.json`

### Step 4 — Start Docker services

```bash
docker compose up -d
```

This starts:
- **n8n** at `http://localhost:5678` (workflow automation)
- **Flowise** at `http://localhost:3000` (optional LLM UI)

To stop:

```bash
docker compose down
```

---

## 6. Data & privacy

All files containing real company data, credentials, or execution-specific output
are excluded from version control via `.gitignore`. The repository contains
**code and configuration only** — never real business data.

### What is excluded and why

| Path | Excluded because |
|---|---|
| `.env` | Contains all API keys and passwords |
| `credentials.json` | Google OAuth 2.0 client secret |
| `token_calendar.json` | Google OAuth access token (auto-generated on first auth) |
| `data/companies.json` | Real business data scraped from Google Maps |
| `data/qualified_leads.json` | Scored leads with contact details (GDPR) |
| `data/generated_emails.json` | Personalised emails with contact data and send status |
| `data/leads_with_solar_data.json` | Leads enriched with GPS coordinates and irradiation data |
| `data/proposals/` | PDF proposals with company-specific financial projections |
| `data/scoring_checkpoint.json` | Intermediate scorer state — specific to each run |
| `logs/` | Log files may contain email addresses and company names |

### What this means when you clone

When you clone this repository, `data/` and `logs/` will not exist yet.
**No files need to be created manually** — every data file is produced automatically
by the pipeline on first run:

| Script | Creates |
|---|---|
| `src/scraper.py` | `data/companies.json` |
| `src/scorer.py` | `data/qualified_leads.json` |
| `src/email_generator.py` | `data/generated_emails.json` |
| `src/solar_calculator.py` | `data/leads_with_solar_data.json` |
| `src/pdf_generator.py` | `data/proposals/*.pdf` |

Both directories (`data/` and `logs/`) are created automatically by the scripts
the first time they run — no `mkdir` needed.

The only files that must be obtained manually are credentials:
`.env` (copy from `.env.example` and fill in), and optionally `credentials.json`
(download from Google Cloud Console — see [Installation → Step 3](#5-installation)).

---

## 7. Running the pipeline

### Check pipeline status

```bash
python src/pipeline.py --estado
```

Shows which data files exist and which steps have already been completed.

### Run all 5 steps automatically

```bash
python src/pipeline.py
```

The orchestrator runs steps 1 → 5 in sequence. Each step is skipped if its
output file already exists (safe to re-run after partial failures).

### Run a single step

```bash
python src/scraper.py
python src/scorer.py
python src/email_generator.py
python src/solar_calculator.py
python src/pdf_generator.py
```

### Send emails (after reviewing generated_emails.json)

```bash
python src/email_sender.py --enviar
```

Prompts for confirmation before sending. Adds a 30-second delay between emails
to avoid spam filters.

### Send follow-up emails

```bash
python src/followup.py --enviar --dias 3
```

Sends a follow-up to all leads that have `estado_envio == "enviado"` and
have not replied in 3 or more days.

### Manage calendar appointments

```bash
python src/calendar_manager.py --help
```

---

## 8. API server

`src/api_server.py` is a lightweight HTTP server that n8n calls to trigger
pipeline steps without needing shell access inside the Docker container.

### Starting the server

```bash
cd agente-solar/src
python api_server.py
```

The server listens on port **8765**. Keep this terminal open while n8n flows
are active. From inside Docker, n8n reaches it at `http://host.docker.internal:8765`.

### Endpoints

| Method | Endpoint | Action |
|---|---|---|
| `POST` | `/pipeline` | Runs the full 5-step pipeline |
| `POST` | `/emails` | Runs `email_sender.py --enviar --force` |
| `POST` | `/followup` | Runs `followup.py --enviar --dias 3 --force` |
| `GET` | `/estado` | Returns JSON with data file status |

All `POST` endpoints return JSON:
```json
{
  "ok": true,
  "exit_code": 0,
  "stdout": "...",
  "stderr": "...",
  "duracion_s": 142
}
```

### Optional API key authentication

Set `SERVIDOR_API_KEY` in `.env` to a random hex string. The server then
requires an `X-API-Key` header on every request. If the variable is empty,
the server accepts all requests (suitable for local use only).

Generate a key:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### Running as a background service (Windows)

Use Windows Task Scheduler to start the server on login:
```
Program: python
Arguments: C:\path\to\agente-solar\src\api_server.py
```

---

## 9. n8n automation flows

Three flows are provided in `n8n-flows/`. Import them via the n8n UI:
**Settings → Import workflow → Upload JSON file**.

> Before importing: make sure `src/api_server.py` is running and accessible
> at `http://host.docker.internal:8765`.
>
> After importing: add your Telegram credentials in n8n under
> **Credentials → Telegram API** and link them to the Telegram nodes in each flow.

> **Credential placeholders:** the JSON files use `"chatId": "TU_TELEGRAM_CHAT_ID"`
> as a placeholder. Before importing, replace this value with your real Telegram
> chat ID, or update it directly inside n8n after import in the **Telegram** node
> of each flow. The bot token is stored as a named credential (`Telegram Solexia`)
> and must be created in n8n under **Credentials → Telegram API** with your actual
> bot token obtained from @BotFather.

### Flow 1 — Nightly pipeline (`nightly-pipeline.json`)

- **Trigger:** every night at **3:00 AM** (cron)
- **Action:** calls `POST /pipeline`, waits up to 2 hours for completion
- **Result:** extracts step totals from stdout and sends a Telegram summary

### Flow 2 — Email send (`email-send.json`)

- **Trigger:** manual (click "Execute" in the n8n panel)
- **Action:** calls `POST /emails`; process takes ~45 minutes for ~90 leads
- **Result:** Telegram message with sent/error counts when done

### Flow 3 — Automatic follow-up (`automatic-followup.json`)

- **Trigger:** every day at **9:00 AM** (cron)
- **Action:** calls `POST /followup`; detects no-reply leads ≥ 3 days old
- **Result:** silent if no candidates; Telegram alert if follow-ups were sent

---

## 10. Approximate operating costs

Costs per full pipeline run (≈ 400 companies scraped → ≈ 180 qualified leads):

| Service | Cost per run | Notes |
|---|---|---|
| **Google Maps Places API** | ~$1 – 3 | Depends on search radius and page count; $32 per 1,000 requests |
| **Claude API** (scoring) | ~$1 – 2 | ~500 tokens input + 200 tokens output per company |
| **Claude API** (email gen) | ~$1 – 2 | ~800 tokens input + 500 tokens output per lead |
| **PVGIS API** | **Free** | EU Commission public API |
| **Brevo SMTP** | **Free** | Up to 300 emails/day; paid plans from €25/month for higher volume |
| **Google Calendar API** | **Free** | Within standard quotas |
| **Telegram Bot API** | **Free** | |
| **n8n (self-hosted)** | **Free** | Server/VPS cost only |
| **Docker** | **Free** | |

**Estimated total per run: ~$3 – 7** depending on search area size and lead count.

**Estimated monthly cost** (one full run per week + daily follow-ups):
- ~$12 – 28/month for Claude + Google Maps APIs
- Brevo free tier covers up to 300 emails/day; upgrade only if volume exceeds that

> Tip: use `python src/pipeline.py --estado` before running to avoid paying for
> API calls on steps that have already produced valid output files.

---

## 11. Author

**Sergio Porras Martín**
Cáceres — 2026

---

## Licence

MIT License — see [LICENSE](LICENSE) for details.
