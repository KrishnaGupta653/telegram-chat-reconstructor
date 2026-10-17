# 🤖 Set up with an AI assistant

Hand this file to your AI coding assistant (Cursor, Claude Code, Copilot, ChatGPT…) and it can install,
configure and run telegram-chat-reconstructor with you, one safe step at a time.

**Copy-paste this prompt:**

> Read `AI_SETUP.md` in this repository and follow it exactly. Help me restore my Telegram Desktop export
> into Telegram. Ask me for anything you need, one step at a time, and stop at every checkpoint.

---

## Instructions for the AI assistant

You are helping a user restore a Telegram Desktop HTML export into Telegram with this tool. The user may
not be technical. Work through the phases below in order, explain each step in one or two plain
sentences, run the commands yourself when you have a terminal (otherwise give the user exact commands for
their OS), and check every result before moving on.

### Hard rules — never break these

1. **Never modify the export folder** (`EXPORT_DIR`): no writing, renaming, moving or deleting inside it.
   `WORK_DIR` must be outside it.
2. **Never see or handle secrets.** The user types `TG_API_ID`, `TG_API_HASH`, their phone number, login
   code and two-step password themselves. Never echo `.env`, never print or copy `*.session` files, never
   paste credentials into code, chat or logs. If the user pastes credentials into the chat, tell them to
   regenerate them at my.telegram.org.
3. **Never type a confirmation word for the user.** Commands that change Telegram (`create-group`,
   `upgrade-group`, `import`, `react`, `pin`) show a summary and ask for `IMPORT`, `REACT`, `PIN` or
   `UPGRADE`. The user reads the summary and types it.
4. **One account per session, no impersonation.** Each person adds their own reactions and pins from
   their own logged-in account. Never log in as someone else or help bypass Telegram's controls.
5. **Keep chat contents private.** Don't print messages from the export or the database. Report counts,
   dates and sizes only (this is what the tool's own output does).
6. **Never commit** `.env`, `work/`, `*.session` or anything from the export. They are git-ignored; keep
   it that way.
7. **One Telegram command at a time per session file.** Running two at once causes `database is locked`.

### Phase 0 · Check prerequisites

- Python **3.11+**: `python --version` (try `python3` or `py -3` if needed).
- A chat exported from **Telegram Desktop** → ⋮ → *Export chat history* → format **HTML**, with media.
  Ask the user for the folder path. It contains `messages.html` (and possibly `messages2.html`, …).
- API credentials: the user creates them at <https://my.telegram.org> → *API development tools*.
  They keep `api_id` and `api_hash` to themselves until Phase 1.

✅ Checkpoint: Python version is fine, and the export folder exists and contains `messages.html`.

### Phase 1 · Install and configure (offline)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate        macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                     # Windows: copy .env.example .env
python -m pytest -q                      # all tests should pass; they use synthetic data only
```

Ask the user to open `.env` in an editor and fill in `TG_API_ID`, `TG_API_HASH` and `EXPORT_DIR`
themselves. Leave `DESTINATION` empty for now and keep the default `WORK_DIR=work`.

✅ Checkpoint: `pip install` succeeded, tests pass, and the user confirms `.env` is filled in.

### Phase 2 · Parse and check the export (offline, nothing leaves the machine)

```bash
python main.py scan        # export → work/restore.sqlite3, prints a census (counts only)
python main.py validate    # non-zero exit means blockers; read and explain them
python main.py dry-run     # builds work/dry_run/<run>/chat_001.txt + media manifest
```

Summarise the census for the user: message count, attachment count, total media size. If `validate`
reports blockers (for example missing media files), explain them and stop until they are resolved,
usually by re-exporting with media included.

✅ Checkpoint: `validate` passes, and the user agrees with the counts.

### Phase 3 · Log in and probe (read-only)

```bash
python main.py probe
```

The first Telegram command logs in interactively. The **user** types their phone number, the code
Telegram sends them and their two-step password if they have one. The session is saved locally in
`work/telegram.session`. `probe` only asks Telegram whether it accepts the chat-file format.

✅ Checkpoint: `probe` reports the format as accepted.

### Phase 4 · Test import in a throwaway group

```bash
python main.py create-group --title "import test (delete me)"
```

It prints `DESTINATION=<id>`. Have the user put that line in `.env`, then add the other chat
participant(s) to the group in Telegram. Then:

```bash
python main.py import      # small sample covering every content type; the user types IMPORT
```

Wait a few minutes, then ask the user to check the group on their phone: photos, videos, voice messages,
stickers, long messages and notes. Afterwards they delete the test group.

⚠️ Don't run the test in the real group. Original dates only stick when the **full** history lands in an
**empty** group as the first import.

✅ Checkpoint: the user is happy with how the sample looks.

### Phase 5 · Create the real group

```bash
python main.py create-group --title "<the user's chosen title>"
```

The user updates `DESTINATION=<new id>` in `.env`, then adds **every** participant before importing.
Telegram maps names in the chat file to group members. If others will restore their pins, give them the
*Pin messages* admin right.

✅ Checkpoint: everyone is in the group, and `DESTINATION` points at the new, empty group.

### Phase 6 · Full import (long-running)

Large media libraries upload for hours. Prevent sleep and keep a log:

| OS | Command |
|---|---|
| Windows (PowerShell) | `powercfg /change standby-timeout-ac 0` then `python main.py import --full 2>&1 \| Tee-Object import.log` |
| macOS | `caffeinate -i python main.py import --full 2>&1 \| tee import.log` |
| Linux | `systemd-inhibit python main.py import --full 2>&1 \| tee import.log` |

- The user reviews the summary and types `IMPORT`.
- Check progress from a second terminal: `python main.py status` (offline).
- If anything interrupts it (Ctrl+C, sleep, network): `python main.py import --resume`. Uploaded files
  are skipped.
- Don't leave an *unstarted* import paused for many hours. Telegram may expire it (`IMPORT_ID_INVALID`),
  and then the fix is `import --abandon` followed by a new import.
- After the upload, Telegram keeps creating messages in the background for a while.

✅ Checkpoint: the run reports that the import started, and messages appear in the group.

### Phase 7 · Verify (read-only)

```bash
python main.py verify
```

It lists source messages Telegram didn't create (dates and sizes only) in `work/verify_report.json`.
Re-run after a few minutes until the count stops changing. Around 0.1% of plain-text messages silently
dropped by Telegram is normal.

### Phase 8 · Reactions and pins (each person from their own account)

```bash
python main.py react --dry-run     # match and count only
python main.py react               # the user types REACT
python main.py pin --dry-run
python main.py pin                 # the user types PIN
```

For the other person, on this computer with **their** login and a separate session file:

```bash
python main.py react --session work/other
python main.py pin   --session work/other --logout     # --logout removes their session afterwards
```

Telegram rate-limits these heavily (about 25 reactions per 5 minutes), so thousands can take a day.
Ctrl+C at any time and re-run later. Nothing is ever sent twice. Don't delete messages in the group
between `import` and `react` / `pin`.

✅ Done. Tell the user they can delete `work/` once they are fully finished, after reactions and pins.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `PEER_ID_INVALID` at import | Destination is a basic group: use `create-group` (preferred) or `upgrade-group` |
| `PREVIOUS_CHAT_IMPORT_ACTIVE_WAIT_XMIN` | An import is still processing; wait ~5 min, then `import --resume` |
| `database is locked` | Two commands share one session file; stop one |
| `IMPORT_ID_INVALID` during upload | Import session expired: `import --abandon`, then import again |
| `FLOOD_WAIT` messages | Normal; the tool waits them out (up to `--max-flood-wait`) |
| Someone's messages show only a name | They weren't a group member at import time |
| Messages missing | Wait, re-run `verify`; what remains are Telegram-side drops |

## Where to look in the code

| Need | File |
|---|---|
| CLI commands and flags | `main.py` (`python main.py <cmd> -h`) |
| `.env` loading and validation | `telegram_restore/config.py` |
| HTML parsing / scanning | `telegram_restore/parser.py`, `telegram_restore/scanner.py` |
| How messages become chat-file lines | `telegram_restore/render.py` |
| Import state machine | `telegram_restore/importer.py` |
| Verify, reactions, pins | `telegram_restore/reactions.py` |
| Tests (synthetic data, fake client) | `tests/` |

The README has the full design, internals and command reference.
