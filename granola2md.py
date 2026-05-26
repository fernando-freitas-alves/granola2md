#!/usr/bin/env python3
"""
granola2md.py - Export Granola meeting notes to Markdown files.

Usage:
    python3 granola2md.py [--yes] [output_dir]

Options:
    --yes, -y   Auto-approve all updates without prompting

If output_dir is not provided, notes are saved to ./notes/
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GRANOLA_APP_SUPPORT = Path.home() / "Library" / "Application Support" / "Granola"
SUPABASE_JSON = GRANOLA_APP_SUPPORT / "supabase.json"
API_BASE = "https://api.granola.ai/v1"
AUTH_BASE = "https://auth.granola.ai/user_management"
STATE_FILENAME = ".granola2md_state.json"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def load_tokens() -> dict:
    with open(SUPABASE_JSON) as f:
        data = json.load(f)
    tokens = json.loads(data["workos_tokens"])
    return tokens


def is_token_expired(tokens: dict) -> bool:
    obtained_at_ms = tokens.get("obtained_at", 0)
    expires_in_s = tokens.get("expires_in", 0)
    now_ms = int(time.time() * 1000)
    # Refresh 5 minutes before actual expiry
    return (now_ms - obtained_at_ms) / 1000 > (expires_in_s - 300)


def refresh_token(tokens: dict) -> dict:
    """Refresh access token using the refresh_token."""
    print("Refreshing access token...")

    # Read client_id from the stored token's iss claim (JWT)
    iss = _decode_jwt_payload(tokens["access_token"]).get("iss", "")
    client_id = iss.split("/")[-1] if "/" in iss else ""

    payload = json.dumps({
        "client_id": client_id,
        "refresh_token": tokens["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()

    req = urllib.request.Request(
        f"{AUTH_BASE}/authenticate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        new_tokens = json.loads(resp.read())

    new_tokens["obtained_at"] = int(time.time() * 1000)
    return new_tokens


def _decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload without verification."""
    import base64
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    # Add padding
    payload += "=" * (4 - len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def get_access_token() -> str:
    tokens = load_tokens()
    if is_token_expired(tokens):
        try:
            tokens = refresh_token(tokens)
            # Save refreshed tokens back
            with open(SUPABASE_JSON) as f:
                data = json.load(f)
            data["workos_tokens"] = json.dumps(tokens)
            with open(SUPABASE_JSON, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Warning: Token refresh failed ({e}). Using existing token.")
    return tokens["access_token"]


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

def api_post(endpoint: str, token: str, body: dict = None) -> object:
    url = f"{API_BASE}/{endpoint}"
    payload = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()
        # Handle gzip
        encoding = resp.headers.get("Content-Encoding", "")
        if encoding == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return json.loads(raw)


def get_documents(token: str) -> list[dict]:
    return api_post("get-documents", token)


def get_document_panels(doc_id: str, token: str) -> list[dict]:
    return api_post("get-document-panels", token, {"document_id": doc_id})


def get_transcript(doc_id: str, token: str, debug: bool = False) -> list[dict]:
    """Fetch transcript segments for a document. Returns [] if unavailable."""
    try:
        result = api_post("get-document-transcript", token, {"document_id": doc_id})
    except urllib.error.HTTPError as e:
        if debug:
            print(f"    [debug] get-document-transcript HTTP {e.code}")
        return []
    except Exception as e:
        if debug:
            print(f"    [debug] get-document-transcript error: {e}")
        return []

    if isinstance(result, list):
        # Keep only final segments
        return [s for s in result if s.get("is_final", True)]
    return []


# ---------------------------------------------------------------------------
# ProseMirror JSON → Markdown
# ---------------------------------------------------------------------------

def prosemirror_to_markdown(node: dict, indent: int = 0) -> str:
    """Recursively convert a ProseMirror/TipTap JSON node to Markdown."""
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type", "")
    content = node.get("content", [])

    if node_type == "doc":
        return "\n".join(
            prosemirror_to_markdown(child, indent) for child in content
        ).strip()

    if node_type == "heading":
        level = node.get("attrs", {}).get("level", 1)
        text = _inline_content(content)
        return f"{'#' * level} {text}"

    if node_type == "paragraph":
        text = _inline_content(content)
        return text  # blank lines added by caller

    if node_type == "bulletList":
        lines = []
        for item in content:
            lines.extend(_list_item_lines(item, indent, ordered=False, index=0))
        return "\n".join(lines)

    if node_type == "orderedList":
        lines = []
        start = node.get("attrs", {}).get("start", 1)
        for i, item in enumerate(content):
            lines.extend(_list_item_lines(item, indent, ordered=True, index=start + i))
        return "\n".join(lines)

    if node_type == "listItem":
        # Rendered by parent (bulletList/orderedList)
        return _inline_content(content)

    if node_type == "blockquote":
        inner = "\n".join(prosemirror_to_markdown(c, indent) for c in content)
        return "\n".join(f"> {line}" for line in inner.splitlines())

    if node_type == "codeBlock":
        lang = node.get("attrs", {}).get("language", "")
        code = _inline_content(content, strip_marks=True)
        return f"```{lang}\n{code}\n```"

    if node_type == "horizontalRule":
        return "---"

    if node_type == "hardBreak":
        return "  \n"

    if node_type == "image":
        attrs = node.get("attrs", {})
        alt = attrs.get("alt", "")
        src = attrs.get("src", "")
        return f"![{alt}]({src})"

    if node_type == "text":
        return _apply_marks(node.get("text", ""), node.get("marks", []))

    # Fallback: recurse into children
    return "\n".join(prosemirror_to_markdown(c, indent) for c in content)


def _list_item_lines(item: dict, indent: int, ordered: bool, index: int) -> list[str]:
    """Convert a listItem node to indented lines."""
    prefix = "  " * indent
    marker = f"{index}." if ordered else "-"

    lines = []
    for child in item.get("content", []):
        child_type = child.get("type", "")

        if child_type == "paragraph":
            text = _inline_content(child.get("content", []))
            if not lines:
                lines.append(f"{prefix}{marker} {text}")
            else:
                lines.append(f"{prefix}  {text}")

        elif child_type in ("bulletList", "orderedList"):
            sub_lines = []
            sub_start = child.get("attrs", {}).get("start", 1)
            for j, sub_item in enumerate(child.get("content", [])):
                sub_ordered = child_type == "orderedList"
                sub_idx = sub_start + j if sub_ordered else 0
                sub_lines.extend(
                    _list_item_lines(sub_item, indent + 1, sub_ordered, sub_idx)
                )
            lines.extend(sub_lines)

        else:
            text = prosemirror_to_markdown(child, indent + 1)
            if text:
                lines.append(text)

    return lines


def _inline_content(content: list, strip_marks: bool = False) -> str:
    """Render inline nodes to a string."""
    parts = []
    for node in content:
        if node.get("type") == "text":
            text = node.get("text", "")
            if not strip_marks:
                text = _apply_marks(text, node.get("marks", []))
            parts.append(text)
        elif node.get("type") == "hardBreak":
            parts.append("\n")
        elif node.get("type") == "image":
            attrs = node.get("attrs", {})
            parts.append(f"![{attrs.get('alt', '')}]({attrs.get('src', '')})")
        else:
            # Recurse for inline nodes
            parts.append(_inline_content(node.get("content", []), strip_marks))
    return "".join(parts)


def _apply_marks(text: str, marks: list) -> str:
    """Apply ProseMirror marks to text."""
    for mark in marks:
        mark_type = mark.get("type", "")
        if mark_type == "bold":
            text = f"**{text}**"
        elif mark_type == "italic":
            text = f"*{text}*"
        elif mark_type == "code":
            text = f"`{text}`"
        elif mark_type == "strike":
            text = f"~~{text}~~"
        elif mark_type == "underline":
            text = f"<u>{text}</u>"
        elif mark_type == "link":
            href = mark.get("attrs", {}).get("href", "")
            text = f"[{text}]({href})"
        elif mark_type == "highlight":
            text = f"=={text}=="
    return text


def doc_to_markdown_sections(content_node: dict) -> str:
    """Convert a top-level ProseMirror doc to Markdown with proper spacing."""
    if not isinstance(content_node, dict):
        return ""

    blocks = content_node.get("content", [])
    rendered = []
    for block in blocks:
        md = prosemirror_to_markdown(block)
        if md.strip():
            rendered.append(md)

    return "\n\n".join(rendered)


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def extract_meeting_date(doc: dict) -> str:
    """Extract meeting date as YYYY-MM-DD from google_calendar_event or created_at."""
    cal = doc.get("google_calendar_event") or {}
    start = cal.get("start", {})
    dt_str = start.get("dateTime") or start.get("date") or doc.get("created_at", "")
    if not dt_str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Parse ISO datetime
    try:
        # Handle timezone offset like -03:00
        dt_str_clean = re.sub(r"(\.\d+)?([+-]\d{2}:\d{2})$", "", dt_str).replace("Z", "")
        dt = datetime.fromisoformat(dt_str_clean)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return dt_str[:10]


def extract_meeting_time(doc: dict) -> str:
    """Extract meeting start time (HH:MM) from calendar event."""
    cal = doc.get("google_calendar_event") or {}
    start = cal.get("start", {})
    dt_str = start.get("dateTime", "")
    if not dt_str:
        return ""
    try:
        dt_str_clean = re.sub(r"(\.\d+)?([+-]\d{2}:\d{2})$", r"\2", dt_str)
        # Try to parse with timezone
        from datetime import timezone as tz
        dt = datetime.fromisoformat(dt_str_clean.replace("Z", "+00:00"))
        # Convert to local time
        local_dt = dt.astimezone()
        return local_dt.strftime("%H:%M")
    except Exception:
        return ""


def extract_attendees(doc: dict) -> list[str]:
    """Extract attendee names/emails from the document."""
    people = doc.get("people") or {}
    attendees = []

    # Add creator
    creator = people.get("creator") or {}
    if creator.get("name"):
        attendees.append(creator["name"])

    # Add other attendees
    for attendee in (people.get("attendees") or []):
        name = attendee.get("name") or attendee.get("email", "")
        if name and name not in attendees:
            attendees.append(name)

    # Fallback: use google_calendar_event attendees
    if not attendees:
        cal = doc.get("google_calendar_event") or {}
        for a in cal.get("attendees", []):
            email = a.get("email", "")
            if email and not a.get("self"):
                attendees.append(email)

    return attendees


def make_safe_filename(title: str) -> str:
    """Convert a title to a safe filename."""
    # Remove emoji and special chars
    safe = re.sub(r"[^\w\s\-–—]", "", title, flags=re.UNICODE)
    safe = re.sub(r"[\s\-–—]+", "-", safe.strip())
    safe = safe.strip("-")
    return safe[:100] if safe else "untitled"


# ---------------------------------------------------------------------------
# Note builder
# ---------------------------------------------------------------------------

def build_note(doc: dict, panels: list[dict]) -> str:
    """Build a complete Markdown note from a document and its panels."""
    title = doc.get("title") or "Untitled"
    date = extract_meeting_date(doc)
    meeting_time = extract_meeting_time(doc)
    attendees = extract_attendees(doc)

    cal = doc.get("google_calendar_event") or {}
    meeting_link = ""
    for ep in (cal.get("conferenceData") or {}).get("entryPoints", []):
        if ep.get("entryPointType") == "video":
            meeting_link = ep.get("uri", "")
            break

    # YAML frontmatter
    lines = ["---"]
    lines.append(f"title: {json.dumps(title)}")
    lines.append(f"date: {date}")
    if meeting_time:
        lines.append(f"time: {meeting_time}")
    if attendees:
        lines.append("attendees:")
        for a in attendees:
            lines.append(f"  - {a}")
    lines.append(f"source: granola")
    lines.append(f"granola_id: {doc['id']}")
    if meeting_link:
        lines.append(f"meeting_link: {meeting_link}")
    lines.append("---")
    lines.append("")

    # Title
    lines.append(f"# {title}")
    lines.append("")

    # Panels (AI-generated notes)
    if panels:
        for panel in panels:
            panel_title = panel.get("title", "Notes")
            content = panel.get("content")
            if not content:
                continue

            # Add panel heading if it's not the default "Summary" with only one panel
            if len(panels) > 1 or panel_title not in ("Summary", "Notes"):
                lines.append(f"## {panel_title}")
                lines.append("")

            md = doc_to_markdown_sections(content)
            if md.strip():
                lines.append(md)
                lines.append("")
    else:
        # Fallback: use the notes field from the document
        notes = doc.get("notes")
        if notes and isinstance(notes, dict):
            md = doc_to_markdown_sections(notes)
            if md.strip():
                lines.append(md)
                lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Transcript builder
# ---------------------------------------------------------------------------

def _format_timestamp(seconds: float) -> str:
    """Format seconds as H:MM:SS or M:SS."""
    s = int(seconds)
    h, remainder = divmod(s, 3600)
    m, s = divmod(remainder, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def build_transcript(doc: dict, segments: list[dict]) -> str:
    """Build a transcript Markdown file from raw transcript segments."""
    if not segments:
        return ""

    title = doc.get("title") or "Untitled"
    date = extract_meeting_date(doc)
    meeting_time = extract_meeting_time(doc)

    # "microphone" = the note-taker; "system" = other participants
    people = doc.get("people") or {}
    creator_name = (people.get("creator") or {}).get("name") or "Me"

    # Meeting start for computing relative timestamps
    cal = doc.get("google_calendar_event") or {}
    start_str = (cal.get("start") or {}).get("dateTime") or doc.get("created_at") or ""
    try:
        meeting_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
    except Exception:
        meeting_start = None

    def speaker_label(seg: dict) -> str:
        detected = seg.get("detected_speaker_name")
        if detected:
            return detected
        return creator_name if seg.get("source") == "microphone" else "Others"

    def rel_ts(seg: dict) -> str | None:
        if not meeting_start:
            return None
        try:
            seg_dt = datetime.fromisoformat(seg["start_timestamp"].replace("Z", "+00:00"))
            elapsed = max(0.0, (seg_dt - meeting_start).total_seconds())
            return _format_timestamp(elapsed)
        except Exception:
            return None

    # Group consecutive segments from the same speaker
    groups: list[tuple[str, list[dict]]] = []
    for seg in segments:
        label = speaker_label(seg)
        if groups and groups[-1][0] == label:
            groups[-1][1].append(seg)
        else:
            groups.append((label, [seg]))

    lines = ["---"]
    lines.append(f"title: {json.dumps(title)}")
    lines.append(f"date: {date}")
    if meeting_time:
        lines.append(f"time: {meeting_time}")
    lines.append(f"source: granola")
    lines.append(f"granola_id: {doc['id']}")
    lines.append(f"type: transcript")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title} — Transcript")
    lines.append("")

    for speaker, group_segs in groups:
        ts = rel_ts(group_segs[0])
        ts_str = f" *({ts})*" if ts else ""
        lines.append(f"**{speaker}**{ts_str}")
        text = " ".join(s["text"].strip() for s in group_segs if s.get("text", "").strip())
        if text:
            lines.append(text)
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state(output_dir: Path) -> dict:
    state_file = output_dir / STATE_FILENAME
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(output_dir: Path, state: dict) -> None:
    state_file = output_dir / STATE_FILENAME
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def read_frontmatter_id(filepath: Path) -> str | None:
    """Read granola_id from YAML frontmatter of an existing note file."""
    try:
        text = filepath.read_text(encoding="utf-8")
        match = re.search(r"^granola_id:\s*(\S+)", text, re.MULTILINE)
        return match.group(1) if match else None
    except Exception:
        return None


def find_file_for_doc(
    output_dir: Path,
    doc_id: str,
    date: str,
    safe_title: str,
    suffix: str = "",
) -> tuple[Path, bool]:
    """
    Find the right filepath for this document.

    suffix is appended before .md, e.g. ".transcript" → "2026-01-01 - Title.transcript.md"

    Returns (filepath, already_exists_for_this_doc).
    already_exists_for_this_doc=True means we found an existing file whose granola_id matches.
    """
    ext = f"{suffix}.md"
    base = f"{date} - {safe_title}{ext}"
    candidates = [base] + [f"{date} - {safe_title} ({i}){ext}" for i in range(1, 200)]

    for candidate in candidates:
        filepath = output_dir / candidate
        if not filepath.exists():
            return filepath, False
        existing_id = read_frontmatter_id(filepath)
        if existing_id == doc_id:
            return filepath, True
        # File exists but belongs to a different doc — keep searching

    # Extremely unlikely fallback
    import uuid
    return output_dir / f"{date} - {safe_title}-{uuid.uuid4().hex[:6]}{ext}", False


# ---------------------------------------------------------------------------
# Interactive update prompt
# ---------------------------------------------------------------------------

def prompt_update(label: str, filename: str, old_size: int, new_size: int) -> bool:
    """Ask user whether to update a changed file. Returns True to update."""
    delta = new_size - old_size
    sign = "+" if delta >= 0 else ""
    print(f"    {label} changed: {filename}  ({old_size} → {new_size} bytes, {sign}{delta})")
    try:
        answer = input("    Update? [y/N]: ").strip().lower()
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    auto_yes = "--yes" in sys.argv or "-y" in sys.argv
    debug = "--debug" in sys.argv
    pos_args = [a for a in sys.argv[1:] if not a.startswith("-")]
    output_dir = Path(pos_args[0]) if pos_args else Path("notes")
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Output directory: {output_dir.resolve()}")

    state = load_state(output_dir)

    token = get_access_token()
    print("Fetching documents...")

    try:
        docs = get_documents(token)
    except urllib.error.HTTPError as e:
        print(f"Error fetching documents: {e.code} {e.reason}")
        sys.exit(1)

    # Filter out deleted documents
    active_docs = [d for d in docs if not d.get("deleted_at")]
    print(f"Found {len(active_docs)} active documents (out of {len(docs)} total).")

    saved = 0
    updated = 0
    skipped = 0

    for doc in active_docs:
        doc_id = doc["id"]
        title = doc.get("title") or "Untitled"
        date = extract_meeting_date(doc)

        print(f"  Processing: {title} ({date})")

        try:
            panels = get_document_panels(doc_id, token)
        except urllib.error.HTTPError as e:
            print(f"    Warning: could not fetch panels: {e.code}")
            panels = []
        except Exception as e:
            print(f"    Warning: error fetching panels: {e}")
            panels = []

        try:
            transcript_segments = get_transcript(doc_id, token, debug=debug)
        except Exception as e:
            print(f"    Warning: error fetching transcript: {e}")
            transcript_segments = []

        if debug:
            print(f"    [debug] transcript_segments: {len(transcript_segments)}")

        has_panel_content = any(p.get("content") for p in panels)
        has_notes_content = bool(doc.get("notes_plain") or doc.get("notes_markdown"))

        if not has_panel_content and not has_notes_content:
            print(f"    Skipping (no content yet)")
            skipped += 1
            continue

        note_content = build_note(doc, panels)
        note_hash = content_hash(note_content)

        transcript_content = build_transcript(doc, transcript_segments) if transcript_segments else None
        transcript_hash = content_hash(transcript_content) if transcript_content else None

        safe_title = make_safe_filename(title)
        state_entry = state.get(doc_id)

        if state_entry:
            # Known document — check for content changes
            notes_changed = note_hash != state_entry.get("notes_hash")
            transcript_changed = (
                transcript_content is not None
                and transcript_hash != state_entry.get("transcript_hash")
            )

            if not notes_changed and not transcript_changed:
                print(f"    Unchanged: {state_entry['notes_file']}")
                skipped += 1
                continue

            doc_updated = False

            if notes_changed:
                notes_file = state_entry["notes_file"]
                notes_filepath = output_dir / notes_file
                old_size = notes_filepath.stat().st_size if notes_filepath.exists() else 0
                do_update = auto_yes or prompt_update(
                    "Notes", notes_file, old_size, len(note_content.encode())
                )
                if do_update:
                    notes_filepath.write_text(note_content, encoding="utf-8")
                    state_entry["notes_hash"] = note_hash
                    print(f"    Updated notes: {notes_file}")
                    doc_updated = True

            if transcript_changed:
                transcript_file = state_entry.get("transcript_file")
                if not transcript_file:
                    # Transcript wasn't saved before — find a filename for it
                    tf_path, _ = find_file_for_doc(
                        output_dir, doc_id, date, safe_title, ".transcript"
                    )
                    transcript_file = tf_path.name

                tf_path = output_dir / transcript_file
                old_size = tf_path.stat().st_size if tf_path.exists() else 0
                do_update = auto_yes or prompt_update(
                    "Transcript", transcript_file, old_size, len(transcript_content.encode())
                )
                if do_update:
                    tf_path.write_text(transcript_content, encoding="utf-8")
                    state_entry["transcript_file"] = transcript_file
                    state_entry["transcript_hash"] = transcript_hash
                    print(f"    Updated transcript: {transcript_file}")
                    doc_updated = True

            state[doc_id] = state_entry
            if doc_updated:
                updated += 1
            else:
                skipped += 1

        else:
            # New document (or first run after adding state tracking)
            notes_filepath, notes_existed = find_file_for_doc(
                output_dir, doc_id, date, safe_title
            )
            notes_filename = notes_filepath.name

            if notes_existed:
                # Migration: file was written by an earlier run without state tracking
                existing_hash = content_hash(notes_filepath.read_text(encoding="utf-8"))
                if existing_hash == note_hash:
                    print(f"    Adopted (unchanged): {notes_filename}")
                    state_entry = {
                        "notes_file": notes_filename,
                        "notes_hash": note_hash,
                        "title": title,
                        "date": date,
                    }
                    skipped += 1
                else:
                    old_size = notes_filepath.stat().st_size
                    do_update = auto_yes or prompt_update(
                        "Notes (migrated)", notes_filename,
                        old_size, len(note_content.encode())
                    )
                    if do_update:
                        notes_filepath.write_text(note_content, encoding="utf-8")
                        print(f"    Updated notes (migrated): {notes_filename}")
                        updated += 1
                    else:
                        # Keep whatever was on disk; store its hash
                        note_hash = content_hash(notes_filepath.read_text(encoding="utf-8"))
                        skipped += 1
                    state_entry = {
                        "notes_file": notes_filename,
                        "notes_hash": note_hash,
                        "title": title,
                        "date": date,
                    }
            else:
                notes_filepath.write_text(note_content, encoding="utf-8")
                print(f"    Saved notes: {notes_filename}")
                state_entry = {
                    "notes_file": notes_filename,
                    "notes_hash": note_hash,
                    "title": title,
                    "date": date,
                }
                saved += 1

            # Handle transcript for new/migrated document
            if transcript_content:
                tf_path, tf_existed = find_file_for_doc(
                    output_dir, doc_id, date, safe_title, ".transcript"
                )
                transcript_filename = tf_path.name

                if tf_existed:
                    existing_hash = content_hash(tf_path.read_text(encoding="utf-8"))
                    if existing_hash == transcript_hash:
                        print(f"    Adopted transcript (unchanged): {transcript_filename}")
                        state_entry["transcript_file"] = transcript_filename
                        state_entry["transcript_hash"] = transcript_hash
                    else:
                        old_size = tf_path.stat().st_size
                        do_update = auto_yes or prompt_update(
                            "Transcript (migrated)", transcript_filename,
                            old_size, len(transcript_content.encode())
                        )
                        if do_update:
                            tf_path.write_text(transcript_content, encoding="utf-8")
                            print(f"    Updated transcript (migrated): {transcript_filename}")
                        final_hash = content_hash(tf_path.read_text(encoding="utf-8"))
                        state_entry["transcript_file"] = transcript_filename
                        state_entry["transcript_hash"] = final_hash
                else:
                    tf_path.write_text(transcript_content, encoding="utf-8")
                    print(f"    Saved transcript: {transcript_filename}")
                    state_entry["transcript_file"] = transcript_filename
                    state_entry["transcript_hash"] = transcript_hash

            state[doc_id] = state_entry

    save_state(output_dir, state)
    print(f"\nDone. {saved} saved, {updated} updated, {skipped} skipped.")


if __name__ == "__main__":
    main()
