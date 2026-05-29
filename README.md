# granola2md

Export your [Granola](https://granola.ai) meeting notes to local Markdown files.

## Requirements

- macOS with Granola installed and signed in
- Python 3.9+
- `cryptography` package (`pip install cryptography`) — required for reading attachments and spaces from Granola's encrypted local cache

## Usage

```bash
# Export to ./notes/ (default)
python3 granola2md.py

# Export to a specific directory
python3 granola2md.py ~/Documents/notes/

# Auto-approve all updates without prompting
python3 granola2md.py --yes
```

## Output

Each meeting produces up to two files, plus an attachments folder when images were captured:

- **`YYYY-MM-DD - Meeting-Title.md`** — AI-generated notes
- **`YYYY-MM-DD - Meeting-Title.transcript.md`** — raw transcript (when available)
- **`attachments/YYYY-MM-DD - Meeting-Title/`** — screenshots attached during the meeting (when present)

Example notes file:

```markdown
---
title: "Weekly Sync"
date: 2026-03-12
time: 10:00
attendees:
  - Alice Smith
  - Bob Johnson
spaces:
  - "Engineering"
source: granola
granola_id: 00000000-0000-0000-0000-000000000000
meeting_link: https://meet.google.com/xxx-yyyy-zzz
---

# Weekly Sync

### Team Updates & Progress

- Alice shared progress on Q1 roadmap items
  - Next step: team to review and leave feedback

## Attachments

![](attachments/2026-03-12 - Weekly-Sync/abc123.png)
```

Example transcript file:

```markdown
---
title: "Weekly Sync"
date: 2026-03-12
source: granola
granola_id: 00000000-0000-0000-0000-000000000000
type: transcript
---

# Weekly Sync — Transcript

**Alice Smith** *(0:00)*
Good morning everyone, let's get started.

**Bob Johnson** *(0:12)*
Thanks Alice. So the Q1 roadmap...
```

Notes with no generated content yet (e.g. future meetings or recordings still processing) are skipped automatically.

## Idempotency

The script tracks processed meetings in `.granola2md_state.json` inside your output directory. On subsequent runs:

- **Unchanged meetings** are skipped silently.
- **Updated meetings** (notes or transcript changed in Granola) prompt you to approve the update before writing. Use `--yes` to auto-approve all updates.
- **First run after upgrading** from an older version: existing files are detected via their `granola_id` frontmatter and adopted into state automatically — no duplicate files are created.

## Automatic daily sync

`launchd.sh` registers a macOS LaunchAgent that runs the sync automatically every morning:

```bash
# Install (runs at 08:00 daily)
./launchd.sh install

# Override the run hour
GRANOLA2MD_HOUR=9 ./launchd.sh install

# Remove the service
./launchd.sh uninstall
```

Logs are written to `~/Library/Logs/granola2md/`.

## How it works

Granola stores auth tokens in `~/Library/Application Support/Granola/supabase.json` (or its encrypted counterpart). The script reads that token, calls the Granola API to fetch notes and transcripts, and converts the internal ProseMirror JSON format to Markdown.

Attachments and Spaces are read from Granola's local encrypted cache (`cache-v6.json.enc`) — no extra API calls needed.

The access token is automatically refreshed if it has expired.
