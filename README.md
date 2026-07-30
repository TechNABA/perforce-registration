# Perforce Onboarding System

An automated onboarding pipeline for a Perforce Helix Core server used by art and design students working on collaborative projects.

Students sign up through a bilingual web form. Everything that follows — version control account, team workspace, permissions, communication channel, welcome message — is created from that single submission.

---

## Why it exists

Onboarding students onto a version control server is repetitive work that scales badly. Each new project team needs a set of accounts, a group, a storage area, a permission entry, a place to talk, and a set of credentials delivered to the right people. Done by hand for a few hundred students a year, it is slow, easy to get wrong, and hard to audit afterwards.

The failure modes are the expensive part. A permission entry pointing at the wrong path exposes one team's work to another. An account created but never communicated means a student silently loses days. A student who leaves but keeps access is a problem nobody notices until it matters.

This system turns that work into a form submission and a single command, and keeps a consistent record of who was granted what.

## What it does

**Collects registrations.** A static web form handles both individual students and thesis groups, validates input on the client and again on the server, and supports Italian and English. Group submissions arrive as one batch rather than as separate races against each other.

**Provisions accounts.** One command reads the pending registrations and creates the version control users, the group for each team, the team's storage area, and the permission entry that scopes each group to its own area and nothing else. Every step is idempotent: re-running it skips what already exists rather than duplicating or failing.

**Sets up communication.** Each team gets a private chat channel visible only to its own members, an invite link, and a welcome message listing the team roster and connection details.

**Tracks lifecycle.** Every record carries a status — pending, created, existing, duplicate, removed, error — so the state of any account is answerable without inspecting the server.

**Cleans up.** Separate tools remove departed students along with their workspaces and permission entries, and identify orphaned accounts that no longer have access to anything.

## How it works

```
Registration form  →  Cloudflare Worker  →  Key-value store
                              │
                              └──────────→  Admin notification

Admin command  →  reads pending records  →  version control server
                                         →  chat channels and invites
                                         →  welcome emails
                                         →  writes statuses back
```

The form is a static page. A Cloudflare Worker is the only backend: it validates submissions, stores them, and serves them back to the admin tooling over authenticated endpoints. The admin scripts run locally and hold no persistent credentials.

## Design decisions

**No personal data in this repository.** Registration data lives in the key-value store, never in version control and never in build logs. This repository holds the form, the Worker, and the tooling — no student records, in the current tree or in its history. The admin scripts log usernames only; names are omitted and email addresses are masked.

**No credentials at rest.** Every secret — server password, bot token, email API key, admin token — is entered interactively at runtime or held as a platform secret. Nothing is written to disk, embedded in code, or committed.

**Least privilege by construction.** Each team's permission entry grants access to that team's storage area only. Chat channels deny visibility by default and grant it to a single role.

**Idempotent operations.** Provisioning can be interrupted and re-run safely. A dry-run mode previews every action before anything is modified.

**Server-side validation.** The Worker re-validates and normalises every field regardless of what the client sent: the form is a convenience, not a trust boundary.

## Benefits

| | |
|---|---|
| **Time** | Team onboarding drops from a manual sequence of server operations to one form and one command. |
| **Consistency** | Naming, grouping, and permission scoping follow the same rules every time, so the server stays predictable as it grows. |
| **Isolation** | Teams cannot see each other's work by default. Access is granted deliberately, never inherited. |
| **Auditability** | Every account has a status and a registration timestamp, so the current state is always answerable. |
| **Privacy** | Personal data is confined to a single access-controlled store, which keeps the public repository free of it and makes deletion requests a bounded operation. |
| **Low cost** | Runs entirely on free tiers, with no server to maintain. |

## Built with

Static HTML, CSS and JavaScript on the front end. A Cloudflare Worker with KV storage as the backend. Python 3.10+ for the administrative tooling, using the standard library plus `openpyxl` for spreadsheet export. Perforce Helix Core as the version control server, with Discord and a transactional email service for communication.

The Worker ships with a test suite covering routing, authentication, validation, storage, and pagination.

---

*Leonardo Villa — Digital Tech Laboratory Specialist, Nuova Accademia di Belle Arti, Milan*
