<div align="center">

# 💬 telegram-chat-reconstructor

**Lost a Telegram chat but kept the export? Bring it back — for real.**

Rebuilds a Telegram Desktop HTML export inside Telegram with original dates, real senders, real media,
and everyone's own reactions & pins — using only Telegram's official
[history-import API](https://core.telegram.org/api/import).

[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Telethon](https://img.shields.io/badge/telethon-1.45-26A5E4?logo=telegram&logoColor=white)](https://github.com/LonamiWebs/Telethon)
[![Tests](https://img.shields.io/badge/tests-46%20passing-brightgreen?logo=pytest&logoColor=white)](#-development)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

[Quick start](#-quick-start) · [How it works](#-system-design) · [Guide](#-step-by-step-guide) ·
[Limits](#-known-telegram-behaviours) · [FAQ](#-faq)

</div>

---

## ⚡ TL;DR

```bash
python main.py scan && python main.py dry-run     # offline: parse + build
python main.py create-group --title "Our chat"     # empty import-ready group (add members!)
python main.py import --full                       # upload media, start the import
python main.py verify                              # what made it, what Telegram dropped
python main.py react && python main.py pin         # your own reactions & pins, natively
```

No fake accounts. No impersonation. No third-party servers. Runs 100 % locally under your own login. 🔒

---

## ✨ What you get

| From the export | In Telegram | |
|---|---|:-:|
| Text, emoji, line breaks, order | Native messages | ✅ |
| Who sent it | The real member account | ✅ |
| Original date & time | On every message — and on the real timeline in a fresh import group | ✅ |
| Photos, videos, files, audio, voice notes | Native media | ✅ |
| Stickers (`.webp`, `.webm`) | Native stickers | ✅ |
| Animated stickers (`.tgs`) | Rendered locally to video stickers | ✅ |
| Reactions | Re-added by **each person from their own account** | ✅ |
| Pins | Re-pinned by **each person from their own account**, "pinned" lines cleaned up | ✅ |
| Replies, forwards, calls, self-destructing media, albums | Short notes like `[↩ Reply to Name, 30.03.2025 17:50:50: "…"]` | 📝 |
| GIFs | Arrive as short videos | ⚠️ |
| Telegram's tiny **"Imported"** label | Can't be removed — Telegram adds it | ❌ |
| Real reply links, forward headers, original message IDs | Not possible with the import API | ❌ |

> **Real numbers:** on a 247k-message export, 99.9 % of messages and 100 % of media landed, all on their
> original dates. The ~0.1 % Telegram silently drops is listed by `verify`.

---

## 🧠 System design

### Architecture

The pipeline is split into an **offline half** (deterministic, no network, safe to re-run) and an
**online half** (talks to Telegram, every step journaled in SQLite so it can resume).

```mermaid
flowchart LR
    subgraph OFFLINE["🖥️ Offline · never touches Telegram"]
        A[("HTML export<br/>read-only")] -->|parser.py| B[scanner.py]
        B --> C[("SQLite<br/>restore.sqlite3")]
        C -->|validator.py| V{{validate}}
        C -->|render.py + media.py| D[dryrun.py]
        D --> E["chat_001.txt<br/>WhatsApp format"]
        D --> F["media_manifest.json<br/>messages_index.json"]
    end
    subgraph ONLINE["☁️ Online · Telegram MTProto via Telethon"]
        E --> P[probe<br/>read-only checks]
        E & F --> I[importer.py<br/>resumable state machine]
        I --> T[("Telegram group")]
        T --> R[reactions.py<br/>verify · react · pin]
    end
    I -. journal .-> C
    R -. journal .-> C
```

### Import flow

Telegram only imports chats from *other* apps, so the export is converted into a WhatsApp-format chat
file. Nothing is visible in the chat until `startHistoryImport` succeeds.

```mermaid
sequenceDiagram
    autonumber
    participant U as You
    participant CLI as main.py
    participant DB as SQLite
    participant TG as Telegram
    U->>CLI: import --full
    CLI->>TG: checkHistoryImportPeer
    TG-->>CLI: confirm text
    CLI->>U: SOURCE / DESTINATION / MESSAGES / MEDIA
    U->>CLI: types IMPORT
    CLI->>TG: initHistoryImport(chat file, media count)
    TG-->>CLI: import_id
    CLI->>DB: status = uploading
    loop every attachment (skips ones already journaled)
        CLI->>TG: uploadImportedMedia (or reuse an earlier upload)
        CLI->>DB: record upload
    end
    CLI->>TG: startHistoryImport
    CLI->>DB: status = done
    TG-->>TG: creates messages in the background
    U->>CLI: verify, react, pin
```

### Import run state machine

Every chunk is a row in `import_runs`. Each transition is committed the moment it happens, so a crash at
any point is recoverable and a started import is never started again.

```mermaid
stateDiagram-v2
    [*] --> prepared: dry-run built, user confirmed
    prepared --> uploading: initHistoryImport → import_id
    uploading --> uploading: crash / FLOOD_WAIT → import --resume
    uploading --> starting: all media uploaded
    starting --> done: startHistoryImport OK
    prepared --> abandoned: import --abandon
    uploading --> abandoned: import --abandon
    uploading --> expired: IMPORT_ID_INVALID
    starting --> unknown: IMPORT_ID_INVALID on start (check the chat)
    done --> [*]
```

### Data model

```mermaid
erDiagram
    messages ||--o| media : has
    import_runs ||--o{ import_media : uploads
    messages ||--o{ tg_map : "imported as"
    tg_map ||--o{ reactions_sent : "reacted by"
    tg_map ||--o{ pins_sent : "pinned by"
    messages {
        int source_id PK
        int seq
        text sender
        text ts
        text kind
        text text
        text reactions
        int pin_target
    }
    media {
        int source_id PK
        text kind
        text rel_path
        text sha256
        int size
    }
    import_runs {
        int run_id PK
        text destination
        text batch
        text status
        int import_id
        text chat_sha256
        int start_after_id
    }
    import_media {
        int run_id PK
        text import_name PK
        text sha256
    }
    tg_map {
        text destination PK
        int tg_msg_id PK
        int source_id
        int from_id
    }
    reactions_sent {
        text destination PK
        int tg_msg_id PK
        int user_id PK
        text status
    }
    pins_sent {
        text destination PK
        int tg_msg_id PK
        int user_id PK
        text status
    }
```

### Design decisions

<details>
<summary><b>Why convert to WhatsApp format?</b></summary>

Telegram's import API only accepts exports from other messengers. The WhatsApp line grammar
(`[DD/MM/YYYY, HH:MM:SS] Name: text`) is accepted for private chats and, with a "created group" header
line, for groups. `probe` asks Telegram (read-only) whether a generated file is recognised before
anything is written.
</details>

<details>
<summary><b>Why SQLite as a journal?</b></summary>

A full import uploads tens of thousands of files over hours. Every upload, state change, reaction and pin
is committed before moving on, so crashes, sleep, `FLOOD_WAIT`s and Ctrl+C are all just "resume later".
Duplicate protection is enforced by data, not by hope: a second `--full` into the same chat, or a chat
file identical to one already imported, is refused.
</details>

<details>
<summary><b>How are imported messages matched back to the source?</b></summary>

Imported messages carry their original timestamp in `fwd_from.date`. `dry-run` writes
`messages_index.json` (source id + timestamp per chat line, in order); `verify`, `react` and `pin` walk the
chat and the index together, pairing by order and timestamp. The mapping is cached in `tg_map` once the
import has fully landed.
</details>

<details>
<summary><b>Why does each person re-add their own reactions and pins?</b></summary>

Because anything else would be impersonation. The tool only ever acts as the logged-in account. The other
participant logs in themselves with a separate session (`--session work/other --logout`).
</details>

<details>
<summary><b>Why upload repeated media only once?</b></summary>

Files are hashed (SHA-256). After the first upload, later attachments with the same hash reuse the
uploaded document instead of sending the bytes again — in practice ~80 % of attachments were reuses.
</details>

---

## 🧰 Requirements

- 🐍 Python **3.11+** (Windows, macOS, Linux)
- 🔑 API credentials from [my.telegram.org](https://my.telegram.org) → *API development tools*
- 📦 A chat exported from **Telegram Desktop** → ⋮ → *Export chat history* → format **HTML**, media included
- 📶 Upload bandwidth + patience for big media libraries ([timings](#-how-long-it-takes))

## 🚀 Quick start

```bash
git clone https://github.com/KrishnaGupta653/telegram-chat-reconstructor.git
cd telegram-chat-reconstructor
python -m venv .venv
# Windows: .venv\Scripts\activate    ·    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                 # Windows: copy .env.example .env
```

```ini
# .env
TG_API_ID=1234567
TG_API_HASH=0123456789abcdef0123456789abcdef
EXPORT_DIR=/path/to/ChatExport_YYYY-MM-DD
WORK_DIR=work
DESTINATION=                          # filled in after create-group
ANNOTATE=replies,forwards,service,calls,ttl,albums
```

Every command that changes something in Telegram shows a summary and makes you type a confirmation word.
No surprises. 🙅

---

## 🗺️ Step-by-step guide

<details open>
<summary><b>1 · Parse and check the export</b> — offline</summary>

```bash
python main.py scan        # export → work/restore.sqlite3 + census
python main.py validate    # consistency checks (non-zero exit on blockers)
python main.py dry-run     # work/dry_run/<run>/chat_001.txt + manifests
```
The export folder is only ever read.
</details>

<details>
<summary><b>2 · Ask Telegram if it accepts the file</b> — read-only</summary>

```bash
python main.py probe
```
First run logs you in; the session stays local in `work/`.
</details>

<details>
<summary><b>3 · Test import in a throwaway group</b></summary>

```bash
python main.py create-group --title "import test"   # → DESTINATION=<id> in .env, add the other person
python main.py import                                # ~40 messages covering every content type
```
Check photos, stickers, long messages and notes, then delete the test group.

> ⚠️ Don't test in your real group: original dates only stick when the full history lands in an
> **empty** group as a single import.
</details>

<details>
<summary><b>4 · Create the real group and add everyone</b></summary>

```bash
python main.py create-group --title "Our chat"       # → DESTINATION=<id>
```
Add every participant **before** importing (Telegram maps names in the file to members). Give them
*Pin messages* rights if they'll restore their pins.
</details>

<details>
<summary><b>5 · Import everything</b></summary>

```bash
python main.py import --full
python main.py status            # offline progress
python main.py import --resume   # after any interruption — uploaded files are skipped
```
After the upload, Telegram keeps creating messages in the background for a while.
</details>

<details>
<summary><b>6 · Verify</b> — read-only</summary>

```bash
python main.py verify
```
Lists every source message Telegram didn't create (dates & sizes only, no text) →
`work/verify_report.json`. Re-run until the count stops changing.
</details>

<details>
<summary><b>7 · Reactions & pins — everyone from their own account</b></summary>

```bash
python main.py react --dry-run && python main.py react
python main.py pin   --dry-run && python main.py pin

# the other person, logged in as themselves:
python main.py react --session work/other
python main.py pin   --session work/other --logout
```
Resumable, never sends twice, only ever acts as the logged-in account.
</details>

---

## 📖 Command reference

| Command | Telegram? | What it does |
|---|:-:|---|
| `scan` | — | Parse the HTML export into SQLite, print a census |
| `validate` | — | Consistency checks on parsed data |
| `dry-run [--format ios\|android] [--formatting plain\|whatsapp] [--utc-offset +HH:MM] [--max-chunk-mib N]` | — | Build chat file(s) + media manifest |
| `probe [files…] [--peer]` | 👀 | Is the format / destination accepted? |
| `create-group --title T [--about A]` | ✍️ | Empty supergroup flagged for import |
| `upgrade-group [--peer]` | ✍️ | Basic group → supergroup |
| `import [--test\|--full\|--resume\|--abandon]` | ✍️ | Run / resume / discard an import |
| `status` | — | Import runs & upload progress |
| `verify [--batch]` | 👀 | What's missing from the destination |
| `react` / `pin` `[--dry-run] [--session S] [--logout] [--pace s]` | ✍️ | This account's own reactions / pins |

— offline · 👀 read-only · ✍️ writes (asks first). Global: `--env path/to/.env`. Details: `python main.py <cmd> -h`.

---

## ⏱️ How long it takes

Measured on a real export: **~247k messages, ~24.5k attachments (4.6k unique files, 1.5 GB)**, ~0.25 MB/s uplink.

| Phase | Time | Bottleneck |
|---|---|---|
| `scan` + `dry-run` | minutes | disk / CPU |
| Media upload | ~5 h | your upload speed (repeats are reused) |
| Telegram creating messages | ~20 min | Telegram |
| `verify` | ~45 min | Telegram history read limits |
| `react` | ~25 per 5 min | Telegram flood limits — thousands take ~a day |
| `pin` | similar | Telegram flood limits |

Keep the machine awake and online. Everything stops and resumes cleanly. 😴➡️▶️

---

## 🧪 Known Telegram behaviours

Found by testing against the live API — none of it is documented by Telegram.

<details>
<summary><b>Show all findings</b></summary>

- 📅 **Original dates:** the first import into an empty `create-group` group put messages on their real
  dates. Later imports into a non-empty or upgraded group showed the import date.
- 👥 **Groups must be supergroups.** Basic groups fail with `PEER_ID_INVALID` → `create-group` / `upgrade-group`.
- 📎 **Attachments must stand alone.** Text after an attachment line makes Telegram drop the media, so
  captions become a separate message with the same timestamp.
- ✂️ **Long messages vanish silently.** Messages are split at 2,000 chars (single-line) / 1,200 chars (multi-line).
- ⬜ **Empty lines** inside a message make it disappear → written as an invisible word joiner.
- 🎞️ **`.tgs` and `image/gif` uploads are ignored.** Animated stickers are rendered to VP9 `.webm`;
  GIFs go up as MP4 and arrive as videos.
- 🕳️ **~0.1 % of messages are dropped anyway**, mostly plain text, with no error. `verify` lists them.
- ⏳ **Imports finish in the background**, and a new import into the same chat is blocked for ~5 min
  (`PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT_XMIN`).
- 📦 **Very large exports:** chat files over `--max-chunk-mib` (default 30 MiB) are split into several imports;
  only the first is likely to keep original-date placement, and each later chunk needs the ~5 min cooldown
  (`import --resume`).
</details>

---

## 🔒 Safety & privacy

- 📁 Export folder is **never written**; `WORK_DIR` must be outside it (enforced).
- 🧯 `scan`, `validate`, `dry-run`, `status` are offline; `probe`, `verify` only read.
- ✋ Every write shows SOURCE / DESTINATION / counts and needs `IMPORT`, `REACT`, `PIN` or `UPGRADE` typed.
- 🔑 Credentials live in `.env` only. `.env`, sessions, the DB and generated files are git-ignored.
- 🛰️ Data goes to Telegram's servers and nowhere else. Diagnostics show counts, dates and sizes — not text.
- 🌊 `FLOOD_WAIT`s are respected (waited out up to `--max-flood-wait`, max 3 times per request), never hammered.

---

## 🛠️ Troubleshooting

<details>
<summary><b>Common errors and fixes</b></summary>

| Symptom | Fix |
|---|---|
| `PEER_ID_INVALID` at import | Basic group → `upgrade-group`, or better, `create-group` |
| `PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT_XMIN` | An import is still processing; wait, then `import --resume` |
| `database is locked` | Two commands share one session; run one at a time |
| Upload stopped (sleep / network) | `import --resume` |
| `IMPORT_ID_INVALID` during upload | Import session expired → `import --abandon`, then import again |
| `react` / `pin` crawling | Telegram flood limits; leave it running or resume later |
| Messages missing | Wait a few minutes, `verify`; what remains are Telegram drops |
| Someone's messages show only a name | They weren't a group member at import time |
</details>

## ❓ FAQ

<details>
<summary><b>Will it look exactly like the original chat?</b></summary>

Very close: same text, media, senders, dates, reactions and pins, on every device. Telegram adds a small
"Imported" label and doesn't allow real reply links — those become short notes.
</details>

<details>
<summary><b>Can I import into the existing private chat instead of a group?</b></summary>

Yes (`DESTINATION=@username`), but the history lands as a block at the end of the existing chat instead of
on its real timeline. A fresh group is recommended.
</details>

<details>
<summary><b>Can I stop and continue later?</b></summary>

Yes, anything. `import --resume` continues uploads; `react` / `pin` skip what's done. Just don't leave an
unstarted import paused for many hours — Telegram may expire it.
</details>

<details>
<summary><b>Does my chat leave my computer?</b></summary>

Only to Telegram, through your own account. No analytics, no third-party services.
</details>

---

## 🗂️ Project layout

```
main.py                    CLI (argparse subcommands)
telegram_restore/
  config.py                .env loading + validation
  models.py                normalized message / media model
  parser.py · scanner.py   HTML export → SQLite
  validator.py             consistency checks
  render.py                messages → WhatsApp lines (splitting, notes, escaping)
  media.py                 media sniffing + upload specs
  dryrun.py                chat files, media manifest, message index
  telegram_api.py          client, probe, flood handling, group create / upgrade
  importer.py              resumable import state machine
  reactions.py             message matching · verify · react · pin
  database.py              SQLite schema + helpers
tests/                     synthetic fixtures + fake Telegram client (no real data, no network)
scripts/                   read-only analysis tools (census, probes, redacted preview)
CLAUDE.md · .cursor/rules/ coding guidelines for humans and AI assistants
```

## 👩‍💻 Development

```bash
pip install -r requirements.txt
python -m pytest -q
```

Contributions welcome. House rules live in [`CLAUDE.md`](CLAUDE.md) (mirrored in `.cursor/rules/`):
think before coding, keep it simple, make surgical changes, prove it with a test.

## 🤝 Responsible use

Only restore chats you were part of, into groups whose members are on board. Reactions and pins are only
ever sent from the account that's logged in. Follow Telegram's [Terms of Service](https://telegram.org/tos)
and API terms. Not affiliated with Telegram.

## 📄 License

[MIT](LICENSE) — do your thing. ✌️