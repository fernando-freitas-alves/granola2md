# granola2md

Export your [Granola](https://granola.ai) meeting notes to local Markdown files.

## Requirements

- macOS with Granola installed and signed in
- Python 3.9+ (no extra dependencies)

## Usage

```bash
# Export to ./notes/ (default)
python3 granola2md.py

# Export to a specific directory
python3 granola2md.py ~/Documents/notes/
```

## Output

Each meeting becomes a `.md` file named `YYYY-MM-DD - Meeting-Title.md`:

```markdown
---
title: "Weekly Sync"
date: 2026-03-12
time: 10:00
attendees:
  - Alice Smith
  - Bob Johnson
source: granola
granola_id: 00000000-0000-0000-0000-000000000000
meeting_link: https://meet.google.com/xxx-yyyy-zzz
---

# Weekly Sync

### Team Updates & Progress

- Alice shared progress on Q1 roadmap items
  - Next step: team to review and leave feedback
    ...
```

Notes with no generated content yet (e.g. future meetings or recordings still processing) are skipped automatically.

## How it works

Granola stores notes in an encrypted local SQLite database (OPFS). The script reads your auth token from `~/Library/Application Support/Granola/supabase.json` and calls the Granola API directly to fetch the AI-generated notes, then converts the internal ProseMirror JSON format to Markdown.

The access token is automatically refreshed if it has expired.
