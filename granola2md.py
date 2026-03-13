#!/usr/bin/env python3
"""
granola2md.py - Export Granola meeting notes to Markdown files.

Usage:
    python3 granola2md.py [output_dir]

If output_dir is not provided, notes are saved to ./notes/
"""

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
    # iss = "https://auth.granola.ai/user_management/client_01JZJ0XBDAT8PHJWQY09Y0VD61"
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
# Main
# ---------------------------------------------------------------------------

def main():
    output_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("notes")
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Output directory: {output_dir.resolve()}")

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
    skipped = 0

    for doc in active_docs:
        doc_id = doc["id"]
        title = doc.get("title") or "Untitled"
        date = extract_meeting_date(doc)

        print(f"  Processing: {title} ({date})")

        try:
            panels = get_document_panels(doc_id, token)
        except urllib.error.HTTPError as e:
            print(f"    Warning: could not fetch panels for {doc_id}: {e.code}")
            panels = []
        except Exception as e:
            print(f"    Warning: error fetching panels for {doc_id}: {e}")
            panels = []

        # Skip if there's no content at all
        has_panel_content = any(p.get("content") for p in panels)
        has_notes_content = bool(doc.get("notes_plain") or doc.get("notes_markdown"))

        if not has_panel_content and not has_notes_content:
            print(f"    Skipping (no content yet)")
            skipped += 1
            continue

        note_content = build_note(doc, panels)

        # Generate filename: YYYY-MM-DD - Safe-Title.md
        safe_title = make_safe_filename(title)
        filename = f"{date} - {safe_title}.md"
        filepath = output_dir / filename

        # Handle duplicate filenames by appending a counter
        if filepath.exists():
            counter = 1
            while filepath.exists():
                filename = f"{date} - {safe_title} ({counter}).md"
                filepath = output_dir / filename
                counter += 1

        filepath.write_text(note_content, encoding="utf-8")
        print(f"    Saved: {filename}")
        saved += 1

    print(f"\nDone. {saved} notes saved, {skipped} skipped.")


if __name__ == "__main__":
    main()
