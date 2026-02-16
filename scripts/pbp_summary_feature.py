"""
PBP Weekly Summary Feature (DISABLED - requires Anthropic API key)

To enable this feature:
1. Sign up at console.anthropic.com and add payment method
2. Generate an API key
3. Add it as GitHub secret: ANTHROPIC_API_KEY
4. Add to workflow env: ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
5. Uncomment the integration points in checker.py (search for "pbp_summary")

Cost estimate: ~$0.01-0.02 per weekly cycle across all campaigns (Claude Haiku 4.5)

This file contains all the code needed. To re-integrate:
- Add ANTHROPIC_API_KEY env var
- Add SUMMARY_INTERVAL_DAYS and LOGS_PATH constants
- Re-enable pbp_logs capture in process_updates
- Re-enable log cleanup in cleanup_timestamps
- Add post_pbp_summaries call in main()
"""

import json
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ---- Constants to add to checker.py ----
# ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# SUMMARY_INTERVAL_DAYS = 5
# LOGS_PATH = Path(__file__).parent.parent / "data" / "pbp_logs"

# ---- Add to process_updates, after raw_text is set ----
# (capture message text for summaries, skip bot commands)
#
#     if raw_text and not raw_text.startswith("/"):
#         if "pbp_logs" not in state:
#             state["pbp_logs"] = {}
#         if thread_id_str not in state["pbp_logs"]:
#             state["pbp_logs"][thread_id_str] = []
#         is_gm = user_id in gm_ids
#         role = "GM" if is_gm else user_name
#         state["pbp_logs"][thread_id_str].append({
#             "t": msg_time_iso,
#             "who": role,
#             "text": raw_text[:2000],
#         })

# ---- Add to cleanup_timestamps ----
#
#     log_cutoff = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
#     for pid in list(state.get("pbp_logs", {}).keys()):
#         state["pbp_logs"][pid] = [
#             entry for entry in state["pbp_logs"][pid]
#             if entry.get("t", "") >= log_cutoff
#         ]


# ------------------------------------------------------------------ #
#  PBP Weekly Summary (AI-generated recap)
# ------------------------------------------------------------------ #
SUMMARY_SYSTEM_PROMPT = """You are a narrator summarising a play-by-post tabletop RPG session.
The game is set in Golarion (Pathfinder 2e / Starfinder), an adult 18+ high-fantasy world
with explicit language, powerful magic, and clockwork technologies.

You will receive a transcript of messages from the last several days.
"GM" lines are the Game Master narrating the world. Other names are player characters.

Write a recap in two parts:

1. A SHORT narrative paragraph (3-5 sentences) written like the opening crawl of an episode.
Dramatic, atmospheric, in-world voice. Present tense. No meta/game references.

2. KEY BEATS as a short bullet list (3-7 items). Each bullet is one sentence
capturing a significant moment, decision, or revelation. Use character names.

Keep the total response under 300 words. Do not invent events not in the transcript.
If the transcript is mostly combat mechanics or very sparse, summarise what you can
and note the session was light on narrative."""

SUMMARY_INTERVAL_DAYS = 5


def generate_pbp_summary(campaign_name: str, logs: list, api_key: str) -> str:
    """Call Claude API to summarise PBP logs."""
    if not api_key:
        print("No ANTHROPIC_API_KEY set, skipping summary")
        return ""

    # Build transcript
    transcript_lines = []
    for entry in logs:
        timestamp = entry["t"][:10]  # Just the date
        who = entry["who"]
        text = entry["text"]
        transcript_lines.append(f"[{timestamp}] {who}: {text}")

    transcript = "\n".join(transcript_lines)

    # Truncate if massive (keep last ~12000 chars to stay well within limits)
    if len(transcript) > 12000:
        transcript = "...(earlier messages trimmed)...\n" + transcript[-12000:]

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 600,
                "system": SUMMARY_SYSTEM_PROMPT,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Here is the transcript for the campaign \"{campaign_name}\" "
                            f"over the last {SUMMARY_INTERVAL_DAYS} days:\n\n{transcript}"
                        ),
                    }
                ],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]
    except Exception as e:
        print(f"Claude API error for {campaign_name}: {e}")
        return ""


def post_pbp_summaries(config: dict, state: dict, send_message_fn, display_name_fn):
    """Generate and post AI summaries of PBP activity.

    Args:
        config: Bot config dict
        state: Bot state dict
        send_message_fn: The send_message function from checker.py
        display_name_fn: The display_name function from checker.py
    """
    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return

    group_id = config["group_id"]
    now = datetime.now(timezone.utc)
    logs_path = Path(__file__).parent.parent / "data" / "pbp_logs"

    if "last_summary" not in state:
        state["last_summary"] = {}

    for pair in config["topic_pairs"]:
        pid = str(pair["pbp_topic_id"])
        chat_topic_id = pair["chat_topic_id"]
        name = pair["name"]

        # Check interval
        last_str = state["last_summary"].get(pid)
        if last_str:
            days_since = (now - datetime.fromisoformat(last_str)).total_seconds() / 86400
            if days_since < SUMMARY_INTERVAL_DAYS:
                continue

        # Get logs
        logs = state.get("pbp_logs", {}).get(pid, [])
        if len(logs) < 5:
            print(f"Skipping summary for {name}: only {len(logs)} messages")
            continue

        print(f"Generating summary for {name} ({len(logs)} messages)...")
        summary = generate_pbp_summary(name, logs, api_key)

        if not summary:
            continue

        # Calculate date range from actual log timestamps
        first_date = logs[0]["t"][:10]
        last_date = logs[-1]["t"][:10]

        message = (
            f"\U0001f4dc Story So Far: {name}\n"
            f"({first_date} to {last_date})\n\n"
            f"{summary}"
        )

        if send_message_fn(group_id, chat_topic_id, message):
            print(f"Posted summary for {name}")
            state["last_summary"][pid] = now.isoformat()

            # Archive logs to repo before clearing
            logs_path.mkdir(parents=True, exist_ok=True)
            archive_file = logs_path / f"{pid}.json"
            try:
                with open(archive_file) as f:
                    archived = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                archived = []
            archived.extend(logs)
            with open(archive_file, "w") as f:
                json.dump(archived, f)

            # Clear logs from state
            state["pbp_logs"][pid] = []
