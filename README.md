# telegram-chat-reconstructor

Restore a **Telegram Desktop HTML export** back into Telegram — with original dates, real senders, real
media, and each person's own reactions and pins — using only Telegram's official
[history-import API](https://core.telegram.org/api/import). No fake accounts, no impersonation, no
third-party servers: everything runs locally under your own login.

> Lost a chat but still have the export? This turns `ChatExport_YYYY-MM-DD/` back into a living Telegram
> group, ordered on its real timeline and identical on every device.

---

## Contents

- [What you get](#what-you-get)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Step-by-step guide](#step-by-step-guide)
- [Command reference](#command-reference)
- [How long it takes](#how-long-it-takes)
- [Known Telegram behaviours and limits](#known-telegram-behaviours-and-limits)
- [Safety and privacy](#safety-and-privacy)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Development](#development)
- [Responsible use](#responsible-use)
- [License](#license)

---

## What you get

| From the export | In Telegram |
|---|---|
| Text, emoji, line breaks, order | Native messages |
| Sender | The real member account (each participant must be in the group) |
| Original date and time | Shown on the message; with a fresh import group, messages sit on their real dates in the timeline |
| Photos, videos, files, audio, voice notes | Native media |
| Stickers (`.webp`, `.webm`) | Native stickers |
| Animated stickers (`.tgs`) | Converted locally to video stickers (`.webm`) |
| Reactions | Re-added natively by **each person from their own account** (`react`) |
| Pins | Re-pinned natively by **each person from their own account** (`pin`); the "pinned a message" lines are removed |
| Replies, forwards, calls, self-destructing media, albums, service events | Short bracketed notes, e.g. `[↩ Reply to Name, 30.03.2025 17:50:50: "…"]` (configurable) |

What it **cannot** do, because the import API does not allow it:

- Remove Telegram's small **"Imported"** label from messages.
- Recreate real reply links, forward headers or original message IDs (notes are used instead).
- Keep GIFs as looping GIFs — they arrive as short videos.
- Guarantee 100 % delivery — Telegram silently drops a tiny fraction of messages; `verify` lists them.

## How it works

Telegram only imports chats exported from *other* apps (WhatsApp, LINE, …). This tool converts the
Telegram Desktop HTML export into a WhatsApp-format chat file plus media, then drives the official API:

```
HTML export ──scan──▶ SQLite ──dry-run──▶ chat_001.txt + media manifest
                                               │
     checkHistoryImport ◀──probe (read-only)───┤
                                               ▼
initHistoryImport ─▶ uploadImportedMedia × N ─▶ startHistoryImport ─▶ messages appear
                                                                        │
                       verify (what arrived?) ◀─────────────────────────┤
                       react / pin (per account, natively) ◀────────────┘
```

Every step is resumable and recorded in a local SQLite database, so a crash, a rate limit or a closed
laptop never causes duplicates.

## Requirements

- Python **3.11+** (developed on 3.12; Windows, macOS and Linux)
- A Telegram account and API credentials from [my.telegram.org](https://my.telegram.org) → *API development tools*
- The chat exported from **Telegram Desktop** → chat menu → *Export chat history* → format **HTML**, with the
  media types you want included
- Enough upload bandwidth and patience for your media (see [How long it takes](#how-long-it-takes))

## Quick start

```bash
git clone https://github.com/KrishnaGupta653/telegram-chat-reconstructor.git
cd telegram-chat-reconstructor
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                   # Windows: copy .env.example .env
```

Fill in `.env`:

```ini
TG_API_ID=1234567
TG_API_HASH=0123456789abcdef0123456789abcdef
EXPORT_DIR=/path/to/ChatExport_YYYY-MM-DD
WORK_DIR=work
DESTINATION=               # set after create-group (step 4)
ANNOTATE=replies,forwards,service,calls,ttl,albums
```

Then follow the guide below. Every command that changes anything in Telegram shows a summary and asks you
to type a confirmation word first.

## Step-by-step guide

### 1. Parse and check the export (offline)

```bash
python main.py scan        # parse the export into work/restore.sqlite3 and print a census
python main.py validate    # consistency checks; exits non-zero on blocking problems
python main.py dry-run     # build work/dry_run/<run>/chat_001.txt + media_manifest.json + plan.json
```

Nothing here contacts Telegram. The export folder is only ever read.

### 2. Ask Telegram whether it accepts the file (read-only)

```bash
python main.py probe
```

Runs `checkHistoryImport` / `checkHistoryImportPeer`, which change nothing. The first run asks you to log
in; the session is stored locally in `work/`.

### 3. Do a small test import — in a throwaway group

```bash
python main.py create-group --title "import test"     # prints an id
# set DESTINATION=<that id> in .env, add the other participant(s) to the group
python main.py import                                  # ~40 messages covering every content type
```

Open the group and check photos, stickers, long messages, notes. Delete the test group afterwards.

> **Why a throwaway group?** Original dates are only kept reliably when the full history lands in an
> *empty* group as a single import. A test import into your real group would spoil that.

### 4. Create the real group and add everyone

```bash
python main.py create-group --title "Our chat"         # set DESTINATION to the printed id
```

Add every participant **before importing** — Telegram maps the names in the chat file to group members, so
anyone missing would appear as a plain name instead of their account. Make them admins with *Pin messages*
if they will re-create their pins.

### 5. Import everything

```bash
python main.py import --full
```

Uploads all media, then starts the import. If anything interrupts it (sleep, network, rate limit):

```bash
python main.py import --resume     # continues; already uploaded files are skipped
python main.py status              # offline progress view
```

After the upload finishes, Telegram keeps creating messages in the background for a while.

### 6. Check what arrived

```bash
python main.py verify
```

Reads the group back and lists every source message Telegram did not create (dates and sizes only, no
text), written to `work/verify_report.json`. Run it again until the count stops changing.

### 7. Re-create reactions and pins — each person from their own account

```bash
python main.py react --dry-run     # count only
python main.py react
python main.py pin --dry-run
python main.py pin
```

The other participant logs in **themselves** on the same computer with a separate session, and can log
that session out when finished:

```bash
python main.py react --session work/other
python main.py pin   --session work/other --logout
```

Only the logged-in account's own reactions and pins are ever sent. Both commands are resumable; nothing
is sent twice.

## Command reference

| Command | Touches Telegram? | Purpose |
|---|---|---|
| `scan` | no | Parse the HTML export into SQLite, print a census |
| `validate` | no | Consistency checks on the parsed data |
| `dry-run [--format ios\|android] [--formatting plain\|whatsapp] [--utc-offset +HH:MM] [--max-chunk-mib N]` | no | Build the import chat file(s) and media manifest |
| `probe [chat_files…] [--peer]` | read-only | Ask Telegram whether the file format and destination are accepted |
| `create-group --title T [--about A]` | yes | Create an empty supergroup flagged for import |
| `upgrade-group [--peer]` | yes | Convert a basic group into a supergroup (imports need a supergroup) |
| `import [--test\|--full\|--resume\|--abandon]` | yes | Run, resume or discard an import (default: small test) |
| `status` | no | Import runs and upload progress |
| `verify [--batch]` | read-only | List source messages missing from the destination |
| `react` / `pin` `[--dry-run] [--session S] [--logout] [--pace s] [--max-flood-wait s]` | yes | Re-create this account's own reactions / pins |

Global option: `--env path/to/.env`. Run `python main.py <command> -h` for details.

## How long it takes

Measured on a real export of ~247,000 messages and ~24,500 attachments (4,600 unique files, 1.5 GB) over a
~0.25 MB/s uplink:

| Phase | Time | Limited by |
|---|---|---|
| `scan` + `dry-run` | minutes | disk / CPU |
| Media upload (`import --full`) | ~5 hours | your upload speed; repeated files are reused, not re-uploaded |
| Telegram creating the messages | ~20 minutes | Telegram |
| `verify` (reading the group back) | ~45 minutes | Telegram's history read limits |
| `react` | ~25 reactions per 5 minutes | Telegram flood limits — thousands of reactions take a day |
| `pin` | similar flood limits | Telegram |

Keep the computer awake and online during long phases. Everything can be stopped and resumed.

## Known Telegram behaviours and limits

Learned by testing against the live API; none of this is documented by Telegram.

- **Original dates** — the first import into an empty group created with `create-group` placed messages on
  their real dates. Later imports into a non-empty or upgraded group showed the import date instead.
- **Group imports need a supergroup.** A basic group fails with `PEER_ID_INVALID`; use `create-group` or
  `upgrade-group`.
- **Attachments must stand alone.** If text follows an attachment line in the same message, Telegram drops
  the media. Captions are therefore written as a separate message with the same timestamp.
- **Long messages are dropped silently.** Messages are split at 2,000 characters (single-line) and 1,200
  characters (multi-line).
- **Empty lines inside a message** make Telegram lose it; they are written as an invisible word joiner.
- **`.tgs` and `image/gif` uploads are ignored.** Animated stickers are rendered to VP9 `.webm` video
  stickers; GIFs are uploaded as MP4 and arrive as short videos.
- **A small fraction of messages is dropped anyway** (~0.1 % in practice, mostly plain text), with no error.
  `verify` tells you exactly which.
- **Imports finish in the background.** Messages keep appearing after `startHistoryImport` returns; a second
  import into the same chat is refused for ~5 minutes (`PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT_XMIN`).

## Safety and privacy

- The export directory is **never written to**; `WORK_DIR` must be outside it (enforced).
- `scan`, `validate`, `dry-run` and `status` never contact Telegram; `probe` and `verify` only read.
- Every write shows SOURCE / DESTINATION / counts and requires typing `IMPORT`, `REACT`, `PIN` or `UPGRADE`.
- Credentials are read from `.env` only; nothing is hardcoded. `.env`, sessions, the database and all
  generated files are git-ignored because they contain private data.
- Nothing is sent anywhere except Telegram's own servers. Diagnostic output uses counts, dates and sizes —
  not message text.
- `FLOOD_WAIT`s are respected (waited out up to `--max-flood-wait`, at most 3 times per request), never
  hammered.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `PEER_ID_INVALID` at import | Destination is a basic group → `upgrade-group`, or better, `create-group` |
| `PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT_XMIN` | An import into that chat is still processing; wait and `import --resume` |
| `database is locked` | Two commands are using the same session at once; run one at a time |
| Upload stopped (sleep, network) | `import --resume` |
| `IMPORT_ID_INVALID` during upload | Telegram expired the unstarted import → `import --abandon`, then import again |
| `react` / `pin` very slow | Telegram flood limits; leave it running or stop and resume later |
| Messages missing after import | Wait a few minutes, then `verify`; remaining gaps are Telegram drops |
| A participant's messages show only a name | They were not a group member at import time |

## Project layout

```
main.py                    CLI entry point (argparse subcommands)
telegram_restore/
  config.py                .env loading and validation
  models.py                normalized message / media model
  parser.py scanner.py     HTML export → SQLite
  validator.py             consistency checks
  render.py                messages → WhatsApp-format lines (splitting, notes, escaping)
  media.py                 media sniffing and upload specs
  dryrun.py                chat files, media manifest, message index
  telegram_api.py          client, probe, flood handling, group creation/upgrade
  importer.py              resumable import state machine
  reactions.py             matching imported messages, react / pin / verify
  database.py              SQLite schema and helpers
tests/                     synthetic fixtures only — no real chat data
scripts/                   read-only analysis tools used while building (census, probes, redacted preview)
CLAUDE.md, .cursor/rules/  coding guidelines for AI assistants working on this repo
```

## Development

```bash
pip install -r requirements.txt
python -m pytest -q
```

Tests use small synthetic exports and a fake Telegram client; they never touch the network. The coding
guidelines in [`CLAUDE.md`](CLAUDE.md) (mirrored in `.cursor/rules/`) apply to humans and AI assistants
alike: think before coding, keep it simple, make surgical changes, verify with tests.

## Responsible use

Only restore chats you were part of, into groups whose members agree to it. Each person's reactions and pins
are sent only from their own account after they log in themselves — the tool never acts on someone else's
behalf. Respect Telegram's [Terms of Service](https://telegram.org/tos) and API terms. This project is not
affiliated with Telegram.

## License

[MIT](LICENSE)