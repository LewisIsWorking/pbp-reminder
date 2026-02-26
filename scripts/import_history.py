#!/usr/bin/env python3
"""Import historical PBP messages from a Telegram Desktop JSON export.

Usage:
    1. In Telegram Desktop: Settings → Advanced → Export chat history
       - Select the Path_Wars supergroup
       - Format: Machine-readable JSON
       - Uncheck everything except "Text messages" (media metadata is kept)
       - Export
    2. Run this script:
       python3 scripts/import_history.py path/to/result.json [--dry-run]

The script reads config.json to map message_thread_id values to campaign
names, then writes historical messages to data/pbp_logs/{Campaign}/{YYYY-MM}.md
in the same format the live bot uses.

Existing transcript files are respected: the script tracks message IDs
already imported and only appends new ones. Safe to run multiple times.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent
ROOT_DIR = SCRIPT_DIR.parent
LOGS_DIR = ROOT_DIR / "data" / "pbp_logs"
CONFIG_PATH = ROOT_DIR / "config.json"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def build_thread_map(config: dict) -> dict:
    """Map thread_id (int) → campaign name for all PBP topics."""
    mapping = {}
    for pair in config.get("topic_pairs", []):
        name = pair["name"]
        for tid in pair.get("pbp_topic_ids", []):
            mapping[tid] = name
    return mapping


def build_gm_map(config: dict) -> dict:
    """Map campaign name → set of GM user ID strings."""
    global_gms = set(str(uid) for uid in config.get("gm_user_ids", []))
    gm_map = {}
    for pair in config.get("topic_pairs", []):
        name = pair["name"]
        if "gm_user_ids" in pair:
            gm_map[name] = set(str(uid) for uid in pair["gm_user_ids"])
        else:
            gm_map[name] = global_gms
    return gm_map


def sanitize_dirname(name: str) -> str:
    return "".join(c if c.isalnum() or c in (" ", "-", "_") else "" for c in name).strip().replace(" ", "_")


def extract_text(msg: dict) -> str:
    """Extract readable text from a Telegram export message.

    The 'text' field can be a plain string OR a list of mixed text/entity
    objects like [{"type": "bold", "text": "hello"}, " world"].

    Desktop exports may also use 'text_entities' as a list of
    {"type": "...", "text": "..."} objects.
    """
    raw = msg.get("text", "")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(raw, list) and raw:
        parts = []
        for chunk in raw:
            if isinstance(chunk, str):
                parts.append(chunk)
            elif isinstance(chunk, dict):
                parts.append(chunk.get("text", ""))
        result = "".join(parts).strip()
        if result:
            return result

    # Fallback: text_entities (Telegram Desktop export format)
    entities = msg.get("text_entities", [])
    if entities:
        parts = [e.get("text", "") for e in entities if isinstance(e, dict)]
        return "".join(parts).strip()

    return ""


def detect_media(msg: dict) -> str | None:
    """Detect media type from Telegram export message.

    Handles both Bot API format and Desktop export format.
    """
    # Desktop export format
    media_type = msg.get("media_type")
    if media_type == "animation":
        return "gif"
    if media_type == "video_file":
        return "video"
    if media_type == "voice_message":
        return "voice message"
    if media_type == "video_message":
        return "video note"
    if media_type == "sticker":
        emoji = msg.get("sticker_emoji", "?")
        return f"sticker:{emoji}"

    # Photo (Desktop export uses "photo" as a file path string)
    if msg.get("photo"):
        return "image"

    # Document/file
    if msg.get("file") and not media_type:
        fname = str(msg.get("file", "")).split("/")[-1] if msg.get("file") else "file"
        return f"document:{fname}"

    return None


def format_entry(msg: dict, is_gm: bool) -> str:
    """Format a message as a transcript entry."""
    # Parse timestamp
    date_str = msg.get("date", "")
    ts = date_str[:19].replace("T", " ")  # 2025-01-15 14:30:05

    # Name
    name = msg.get("from", "Unknown")
    role_tag = " [GM]" if is_gm else ""

    # Content
    text = extract_text(msg)
    media = detect_media(msg)

    parts = []
    if media:
        if media.startswith("sticker:"):
            parts.append(f"*[sticker {media[8:]}]*")
        elif media.startswith("document:"):
            parts.append(f"*[{media[9:]}]*")
        else:
            parts.append(f"*[{media}]*")

    if text:
        parts.append(text)

    content = " ".join(parts) if parts else "*[empty message]*"

    return f"**{name}**{role_tag} ({ts}):\n{content}\n"


def get_imported_ids(campaign_dir: Path) -> set:
    """Read the tracking file of already-imported message IDs."""
    tracker = campaign_dir / ".imported_ids"
    if tracker.exists():
        return set(tracker.read_text().strip().split("\n"))
    return set()


def save_imported_ids(campaign_dir: Path, ids: set) -> None:
    """Save the set of imported message IDs."""
    tracker = campaign_dir / ".imported_ids"
    tracker.write_text("\n".join(sorted(ids)) + "\n", encoding="utf-8")


def import_messages(export_path: str, *, dry_run: bool = False) -> dict:
    """Import messages from a Telegram Desktop export JSON file.

    Returns a dict of campaign_name → count of imported messages.
    """
    config = load_config()
    thread_map = build_thread_map(config)
    gm_map = build_gm_map(config)

    print(f"Loading export: {export_path}")
    with open(export_path, encoding="utf-8") as f:
        export = json.load(f)

    messages = export.get("messages", [])
    print(f"Total messages in export: {len(messages)}")

    # Filter to PBP topic messages only
    pbp_messages = {}
    for msg in messages:
        if msg.get("type") != "message":
            continue

        # Telegram Desktop exports use reply_to_message_id for topic threading
        # (the topic's root message ID). The Bot API uses message_thread_id.
        thread_id = msg.get("message_thread_id") or msg.get("reply_to_message_id")
        if thread_id is None or thread_id not in thread_map:
            continue

        # Skip service messages (joins, pins, etc.)
        if msg.get("action"):
            continue

        campaign_name = thread_map[thread_id]
        pbp_messages.setdefault(campaign_name, []).append(msg)

    print(f"\nPBP messages found:")
    for name, msgs in sorted(pbp_messages.items()):
        print(f"  {name}: {len(msgs)} messages")

    if not pbp_messages:
        print("\nNo PBP messages found. Check that:")
        print("  - The export contains the correct supergroup")
        print(f"  - PBP topic IDs in config match: {dict(thread_map)}")
        return {}

    results = {}

    for campaign_name, msgs in sorted(pbp_messages.items()):
        dir_name = sanitize_dirname(campaign_name)
        campaign_dir = LOGS_DIR / dir_name
        gm_ids = gm_map.get(campaign_name, set())

        if not dry_run:
            campaign_dir.mkdir(parents=True, exist_ok=True)

        # Track what's already imported
        imported_ids = get_imported_ids(campaign_dir) if not dry_run else set()
        new_count = 0

        # Group by month
        by_month = {}
        for msg in sorted(msgs, key=lambda m: m.get("date", "")):
            msg_id = str(msg.get("id", ""))

            if msg_id in imported_ids:
                continue

            date_str = msg.get("date", "")[:7]  # YYYY-MM
            if not date_str:
                continue

            by_month.setdefault(date_str, []).append(msg)
            new_count += 1

        if new_count == 0:
            print(f"  {campaign_name}: nothing new to import")
            results[campaign_name] = 0
            continue

        if dry_run:
            print(f"  {campaign_name}: would import {new_count} new messages across {len(by_month)} months")
            date_range = sorted(by_month.keys())
            print(f"    Date range: {date_range[0]} to {date_range[-1]}")
            results[campaign_name] = new_count
            continue

        new_ids = set()
        for month_str, month_msgs in sorted(by_month.items()):
            log_file = campaign_dir / f"{month_str}.md"
            is_new = not log_file.exists()

            with open(log_file, "a", encoding="utf-8") as f:
                if is_new:
                    f.write(f"# {campaign_name} — {month_str}\n\n")
                    f.write("*PBP transcript archived by PathWarsNudge bot.*\n\n---\n\n")

                for msg in month_msgs:
                    user_id = str(msg.get("from_id", "")).replace("user", "")
                    is_gm = user_id in gm_ids
                    entry = format_entry(msg, is_gm)
                    f.write(entry + "\n")
                    new_ids.add(str(msg.get("id", "")))

        # Save imported IDs
        all_ids = imported_ids | new_ids
        save_imported_ids(campaign_dir, all_ids)

        results[campaign_name] = new_count
        months = sorted(by_month.keys())
        print(f"  {campaign_name}: imported {new_count} messages ({months[0]} to {months[-1]})")

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Import PBP history from Telegram Desktop JSON export"
    )
    parser.add_argument("export_file", help="Path to result.json from Telegram Desktop export")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be imported without writing files")
    args = parser.parse_args()

    if not Path(args.export_file).exists():
        print(f"Error: File not found: {args.export_file}")
        sys.exit(1)

    results = import_messages(args.export_file, dry_run=args.dry_run)

    total = sum(results.values())
    if args.dry_run:
        print(f"\nDry run complete. Would import {total} messages total.")
    else:
        print(f"\nImport complete. {total} new messages imported.")
        print("Run 'git add data/pbp_logs && git commit' to save to repo.")


if __name__ == "__main__":
    main()
