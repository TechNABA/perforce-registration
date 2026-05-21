# Perforce NABA — User Management System

Automated user registration, provisioning, and lifecycle management for the Perforce Helix Core server at Nuova Accademia di Belle Arti (NABA).

## Overview

This system automates the entire onboarding flow for Perforce users: students register via a web form, their data flows into GitHub, and administrators provision accounts with a single command that also sets up Discord channels and sends welcome emails.

**Registration URL:** `https://p4setup.naba.it`

## Architecture

```
┌─────────────────┐     ┌──────────────────────┐     ┌───────────────────┐
│   Student fills  │────▶│  Cloudflare Worker   │────▶│  GitHub Actions   │
│   web form       │     │  (hides PAT token)   │     │  (register.yml)   │
└─────────────────┘     └──────────────────────┘     └────────┬──────────┘
                                                              │
                         ┌────────────────────────────────────┘
                         ▼
              ┌─────────────────────┐
              │  data/users.csv     │
              │  data/users.xlsx    │
              └────────┬────────────┘
                       │
          ┌────────────┼────────────────┐
          ▼            ▼                ▼
   ┌────────────┐ ┌──────────┐ ┌──────────────┐
   │  Perforce   │ │ Discord  │ │  Email via   │
   │  provision  │ │ channels │ │  Resend API  │
   └────────────┘ └──────────┘ └──────────────┘
```

## Repository Structure

```
├── index.html                              # Registration form (GitHub Pages)
├── CNAME                                   # Custom domain (p4setup.naba.it)
├── .github/workflows/
│   ├── register.yml                        # Auto-updates CSV/XLSX on registration
│   └── cleanup.yml                         # Manual reset of all user data
├── scripts/
│   ├── register_user.py                    # Called by register.yml workflow
│   ├── notify_discord.py                   # Discord webhook notification
│   ├── perforce_provision.py               # Create users, groups, depots, protections
│   ├── perforce_cleanup.py                 # Remove departed students
│   ├── discord_email_provision.py          # Discord channels + welcome emails
│   └── export_p4_users.py                  # Import existing Perforce users
└── data/
    ├── users.csv                           # Master user database
    └── users.xlsx                          # Formatted version with team grouping
```

## Setup

### Prerequisites

- Python 3.10+
- `p4` CLI installed and accessible
- `openpyxl` Python package (`pip install openpyxl`)
- GitHub repository with Pages enabled
- Cloudflare Workers account (free tier)
- Resend account (free tier, 100 emails/day)
- Discord bot with Administrator permission

### Initial Configuration

1. **Cloudflare Worker** — deploy `worker.js` and set environment secrets:
   - `GITHUB_PAT` — Classic token with `repo` scope
   - `GITHUB_OWNER` — your GitHub username
   - `GITHUB_REPO` — repository name

2. **index.html** — update `WORKER_URL` with your Worker URL

3. **GitHub Secrets** — add `DISCORD_WEBHOOK_URL` for registration notifications

4. **discord_email_provision.py** — update `DISCORD_GUILD_ID` and `RESEND_FROM`

5. **GitHub Pages** — enable under Settings → Pages, set custom domain to `p4setup.naba.it`

## Commands Reference

### Provisioning New Users

Create Perforce accounts, groups, depots, Discord channels, and send welcome emails.

```bash
# Preview all actions without making changes (always do this first)
python scripts/perforce_provision.py --dry-run

# Full provisioning: Perforce + Discord + Email
python scripts/perforce_provision.py

# Perforce only (skip Discord and email)
python scripts/perforce_provision.py --skip-discord --skip-email

# Perforce + Discord, no email
python scripts/perforce_provision.py --skip-email

# Set initial password for new Perforce users
python scripts/perforce_provision.py --password "Welcome2025!"

# Use a specific CSV file
python scripts/perforce_provision.py --csv ~/Downloads/users.csv

# Specify Discord category for new channels
python scripts/perforce_provision.py --category "TESI"

# Full example with all options
python scripts/perforce_provision.py --csv data/users.csv --password "Welcome2025!" --category "TESI"
```

The script will interactively ask for:
1. Perforce admin password (for user `villal`)
2. Discord bot token (press Enter to skip)
3. Resend API key (press Enter to skip)

### Removing Departed Students

Cross-reference a CSV of departed students against the system and remove their Perforce accounts.

```bash
# Preview removals (always do this first)
python scripts/perforce_cleanup.py --departed departed_students.csv --dry-run

# Execute removals (will ask you to type CONFIRM)
python scripts/perforce_cleanup.py --departed departed_students.csv

# Also delete empty depots and groups when a team has no remaining members
python scripts/perforce_cleanup.py --departed departed_students.csv --delete-empty-depots

# Use a specific users CSV
python scripts/perforce_cleanup.py --departed departed_students.csv --users data/users.csv
```

The departed CSV must have separate columns for first name and last name. The script will ask you to identify which columns to use. Matching is done by generating `nome_cognome` usernames from the departed list and comparing against `users.csv`.

**What gets deleted per user:**
- All Perforce workspaces/clients
- User membership from their group
- User-specific protection entries
- The Perforce user account
- Status updated to `removed` in users.csv

**With `--delete-empty-depots`:**
- If a team has no remaining members after cleanup, the group, its protections, and the depot are also deleted

### Importing Existing Perforce Users

Export all current Perforce users into the CSV format so they are tracked by the system.

```bash
# Preview what would be exported
python scripts/export_p4_users.py --dry-run

# Export and overwrite users.csv
python scripts/export_p4_users.py

# Merge with existing CSV (adds only new users, no duplicates)
python scripts/export_p4_users.py --merge

# Export to a custom path
python scripts/export_p4_users.py --output ~/Desktop/export.csv
```

Exported users get status `existing` — the provisioning script will never try to recreate them.

### Resetting User Data

Trigger the cleanup workflow from GitHub Actions to reset CSV and XLSX to empty.

1. Go to repository → **Actions** → **Cleanup user data**
2. Click **Run workflow**
3. Type `CONFIRM` in the input field
4. Click **Run workflow**

### Running Discord + Email Standalone

If you need to set up Discord channels and send emails separately from Perforce provisioning:

```bash
# Preview
python scripts/discord_email_provision.py --dry-run

# Execute (processes users with status 'created')
python scripts/discord_email_provision.py

# Skip Discord or email
python scripts/discord_email_provision.py --skip-email
python scripts/discord_email_provision.py --skip-discord

# Specify Discord category
python scripts/discord_email_provision.py --category "TESI"
```

## CSV Data Format

| Column | Description | Example |
|--------|-------------|---------|
| `timestamp` | ISO 8601 registration date | `2026-05-21T14:30:00.000Z` |
| `username` | Perforce username (auto-generated) | `mario_rossi` |
| `full_name` | Display name | `Mario Rossi` |
| `email` | Student email | `mario.rossi@studenti.naba.it` |
| `team` | Team/project name (= Perforce group + depot) | `ProjectAlpha` |
| `tesista` | Thesis student flag | `yes` / `no` |
| `anno_corso` | Year of study (empty if thesis student) | `1` / `2` / `3` |
| `status` | Current state | `pending` / `created` / `existing` / `removed` / `duplicate` / `error` |

### Status Lifecycle

```
pending  →  created   (after perforce_provision.py)
pending  →  error     (if provisioning fails)
pending  →  duplicate (if username already exists)
created  →  removed   (after perforce_cleanup.py)
existing →  removed   (after perforce_cleanup.py)
```

## Data Sorting

Both CSV and XLSX are sorted by: **team** (alphabetical) → **anno_corso** (1, 2, 3, then thesis students) → **full_name** (alphabetical).

The XLSX additionally includes:
- Dark header row with team separator bars
- Member count per team
- Alternating row colors between groups
- Auto-sized columns and auto-filter

## Security Notes

- The GitHub PAT is never exposed in client-side code — it lives in the Cloudflare Worker environment
- Perforce admin password is entered interactively and never stored
- Discord bot token is entered interactively and never stored
- Resend API key is entered interactively and never stored
- The Discord webhook URL is stored as a GitHub Secret
- The registration form validates all inputs both client-side and server-side (in the Worker)

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Form shows "Failed to fetch" | Check that the Cloudflare Worker URL is correct in index.html |
| GitHub Action fails | Check the Actions tab for logs; verify the PAT is valid |
| Discord 403 error | Ensure the bot has Administrator permission in the server |
| Discord 1010 error | User-Agent header issue — update to latest script version |
| Emails not arriving | Check spam; Office 365 may block external domains |
| CSV not found | Run scripts from the repo root, or use `--csv` flag |
| Duplicate users | The system marks them as `duplicate` — check CSV and resolve manually |

## Tech Stack

- **Frontend:** Static HTML/CSS/JS (Apple-inspired dark mode, IT/EN bilingual)
- **Hosting:** GitHub Pages with custom domain
- **Backend proxy:** Cloudflare Workers (free tier)
- **CI/CD:** GitHub Actions
- **Version control server:** Perforce Helix Core (`perforce.naba.it:1666`)
- **Communication:** Discord (bot API + webhooks)
- **Email:** Resend API (`info@p4naba.com`)
- **Data:** CSV + XLSX with Python (openpyxl)

---

*Leonardo Villa — Digital Tech Laboratory Specialist, Nuova Accademia di Belle Arti — NABA, Milano*
