"""
PBP Inactivity Checker for GitHub Actions

Runs hourly via cron. Two features:
1. TOPIC ALERTS: Sends alerts to OOC chat when a PBP topic has been
   inactive for the configured threshold (default 4 hours).
2. PLAYER TRACKING: Auto-detects players by their posts. If a player
   hasn't posted in a PBP topic for 1 week, @mentions them weekly.
   After 4 weeks, announces they're out and stops tracking them.
   They can rejoin simply by posting again.

State is persisted between runs using a GitHub Gist.
"""

import os
import sys
import json
import random
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ------------------------------------------------------------------ #
#  Config
# ------------------------------------------------------------------ #
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GIST_TOKEN = os.environ.get("GIST_TOKEN", "")
GIST_ID = os.environ.get("GIST_ID", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
GIST_API = f"https://api.github.com/gists/{GIST_ID}"
STATE_FILENAME = "pbp_state.json"

CONFIG_PATH = Path(__file__).parent.parent / "config.json"
BOONS_PATH = Path(__file__).parent.parent / "boons.json"
ARCHIVE_PATH = Path(__file__).parent.parent / "data" / "weekly_archive.json"


def fmt_date(dt: datetime) -> str:
    """Format a datetime as YYYY-MM-DD."""
    return dt.strftime("%Y-%m-%d")


def html_escape(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def display_name(first_name: str, username: str = "", last_name: str = "") -> str:
    """Format a player name as 'First Last (@username)' or 'First Last' or 'First'."""
    full = f"{first_name} {last_name}".strip() if last_name else first_name
    if username:
        return f"{full} (@{username})"
    return full


def posts_str(n: int) -> str:
    """Return '1 post' or 'N posts'."""
    return f"{n} post" if n == 1 else f"{n} posts"


def fmt_relative_date(now: datetime, then: datetime) -> str:
    """Format as relative + absolute, e.g. '5d ago (2026-02-10)'."""
    days_ago = int((now - then).total_seconds() / 86400)
    date_str = fmt_date(then)
    if days_ago == 0:
        return f"today ({date_str})"
    elif days_ago == 1:
        return f"yesterday ({date_str})"
    else:
        return f"{days_ago}d ago ({date_str})"

PLAYER_WARN_WEEKS = [1, 2, 3]
PLAYER_REMOVE_WEEKS = 4
ROSTER_INTERVAL_DAYS = 3
POTW_INTERVAL_DAYS = 7
POTW_MIN_POSTS = 5
PACE_INTERVAL_DAYS = 7
LEADERBOARD_INTERVAL_DAYS = 3
COMBAT_PING_HOURS = 4
RECRUITMENT_INTERVAL_DAYS = 14
REQUIRED_PLAYERS = 6


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ------------------------------------------------------------------ #
#  Gist state storage
# ------------------------------------------------------------------ #
DEFAULT_STATE = {
    "offset": 0,
    "topics": {},
    "last_alerts": {},
    "players": {},
    "removed_players": {},
    "message_counts": {},
    "last_roster": {},
    "post_timestamps": {},
    "last_potw": {},
    "last_pace": {},
    "last_anniversary": {},
    "combat": {},
}


def load_state_from_gist() -> dict:
    if not GIST_ID or not GIST_TOKEN:
        print("Warning: No GIST_ID or GIST_TOKEN set, starting with empty state")
        return dict(DEFAULT_STATE)

    resp = requests.get(
        GIST_API,
        headers={
            "Authorization": f"token {GIST_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        },
    )

    if resp.status_code != 200:
        print(f"Warning: Could not load gist (HTTP {resp.status_code}), starting fresh")
        return dict(DEFAULT_STATE)

    gist_data = resp.json()
    files = gist_data.get("files", {})

    if STATE_FILENAME in files:
        content = files[STATE_FILENAME]["content"]
        state = json.loads(content)
        # Backwards compat: ensure all keys exist
        for key, default in DEFAULT_STATE.items():
            if key not in state:
                state[key] = default
        return state

    return dict(DEFAULT_STATE)


def save_state_to_gist(state: dict):
    if not GIST_ID or not GIST_TOKEN:
        print("Warning: No GIST_ID or GIST_TOKEN set, cannot save state")
        return

    resp = requests.patch(
        GIST_API,
        headers={
            "Authorization": f"token {GIST_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        },
        json={
            "files": {
                STATE_FILENAME: {
                    "content": json.dumps(state, indent=2)
                }
            }
        },
    )

    if resp.status_code == 200:
        print("State saved to gist")
    else:
        print(f"Warning: Failed to save state (HTTP {resp.status_code})")


# ------------------------------------------------------------------ #
#  Telegram API helpers
# ------------------------------------------------------------------ #
def get_updates(offset: int) -> list:
    resp = requests.get(
        f"{TELEGRAM_API}/getUpdates",
        params={
            "offset": offset,
            "limit": 100,
            "timeout": 5,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        },
    )

    if resp.status_code != 200:
        print(f"Error fetching updates: HTTP {resp.status_code}")
        return []

    data = resp.json()
    if not data.get("ok"):
        print(f"Telegram API error: {data}")
        return []

    return data.get("result", [])


def send_message(chat_id: int, thread_id: int, text: str) -> bool:
    resp = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={
            "chat_id": chat_id,
            "message_thread_id": thread_id,
            "text": text,
            "disable_notification": False,
        },
    )

    if resp.status_code == 200 and resp.json().get("ok"):
        return True
    else:
        print(f"Failed to send message: {resp.text}")
        return False


def send_message_with_buttons(
    chat_id: int, thread_id: int, text: str, buttons: list
) -> int | None:
    """Send a message with inline keyboard buttons. Returns message_id or None."""
    keyboard = {"inline_keyboard": [buttons]}
    resp = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={
            "chat_id": chat_id,
            "message_thread_id": thread_id,
            "text": text,
            "disable_notification": False,
            "reply_markup": keyboard,
        },
    )
    if resp.status_code == 200 and resp.json().get("ok"):
        return resp.json()["result"]["message_id"]
    else:
        print(f"Failed to send button message: {resp.text}")
        return None


def edit_message(chat_id: int, message_id: int, text: str, parse_mode: str = None) -> bool:
    """Edit an existing message, removing inline keyboard."""
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    resp = requests.post(
        f"{TELEGRAM_API}/editMessageText",
        json=payload,
    )
    if resp.status_code == 200 and resp.json().get("ok"):
        return True
    else:
        print(f"Failed to edit message: {resp.text}")
        return False


def answer_callback(callback_id: str, text: str = "") -> bool:
    """Answer a callback query to dismiss the loading spinner."""
    resp = requests.post(
        f"{TELEGRAM_API}/answerCallbackQuery",
        json={
            "callback_query_id": callback_id,
            "text": text,
        },
    )
    return resp.status_code == 200


# ------------------------------------------------------------------ #
#  Boon choice callback handler
# ------------------------------------------------------------------ #
def process_boon_callback(cb: dict, config: dict, state: dict):
    """Handle a player clicking a boon choice button."""
    cb_id = cb.get("id", "")
    cb_data = cb.get("data", "")
    from_user = cb.get("from", {})
    user_id = str(from_user.get("id", ""))
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")

    if not cb_data.startswith("boon:"):
        return

    # Parse: boon:<topic_id>:<choice_index>
    parts = cb_data.split(":")
    if len(parts) != 3:
        answer_callback(cb_id, "Invalid choice.")
        return

    topic_id = parts[1]
    try:
        choice_idx = int(parts[2])
    except ValueError:
        answer_callback(cb_id, "Invalid choice.")
        return

    # Check pending choices
    pending = state.get("pending_potw_boons", {}).get(topic_id)
    if not pending:
        answer_callback(cb_id, "This choice has expired.")
        return

    # Only the winner can choose
    if user_id != pending["winner_user_id"]:
        answer_callback(cb_id, "Only the Player of the Week can choose!")
        return

    if choice_idx < 0 or choice_idx >= len(pending["boons"]):
        answer_callback(cb_id, "Invalid choice.")
        return

    chosen_boon = pending["boons"][choice_idx]

    # Build message with chosen boon highlighted, others struck through
    boon_lines = ""
    for i, b in enumerate(pending["boons"]):
        escaped = html_escape(b)
        if i == choice_idx:
            boon_lines += f"\n{i + 1}. {escaped} ✓\n"
        else:
            boon_lines += f"\n<s>{i + 1}. {escaped}</s>\n"

    # Escape the base message too for HTML mode
    base_escaped = html_escape(pending["base_message"])
    new_text = f"{base_escaped}\n\nChosen boon:{boon_lines}"

    edit_message(chat_id, message_id, new_text, parse_mode="HTML")
    answer_callback(cb_id, f"You chose boon #{choice_idx + 1}!")

    # Clean up pending state
    del state["pending_potw_boons"][topic_id]
    print(f"POTW boon chosen for topic {topic_id}: #{choice_idx + 1}")


def expire_pending_boons(config: dict, state: dict):
    """Auto-pick boon #1 if winner hasn't chosen within 48 hours."""
    now = datetime.now(timezone.utc)
    group_id = config["group_id"]
    pending = state.get("pending_potw_boons", {})

    for topic_id in list(pending.keys()):
        entry = pending[topic_id]
        posted_at = datetime.fromisoformat(entry["posted_at"])
        hours_since = (now - posted_at).total_seconds() / 3600

        if hours_since >= 48:
            chosen_boon = entry["boons"][0]
            boon_lines = ""
            for i, b in enumerate(entry["boons"]):
                escaped = html_escape(b)
                if i == 0:
                    boon_lines += f"\n{i + 1}. {escaped} ✓\n"
                else:
                    boon_lines += f"\n<s>{i + 1}. {escaped}</s>\n"

            base_escaped = html_escape(entry["base_message"])
            new_text = f"{base_escaped}\n\nBoon (auto-selected):{boon_lines}"

            edit_message(group_id, entry["message_id"], new_text, parse_mode="HTML")
            del pending[topic_id]
            print(f"POTW boon auto-expired for topic {topic_id}, picked #1")


# ------------------------------------------------------------------ #
#  Process updates
# ------------------------------------------------------------------ #
def process_updates(updates: list, config: dict, state: dict) -> int:
    group_id = config["group_id"]
    gm_ids = set(str(uid) for uid in config.get("gm_user_ids", []))

    pbp_topic_ids = {}
    for pair in config["topic_pairs"]:
        pbp_topic_ids[str(pair["pbp_topic_id"])] = pair["name"]

    new_offset = state.get("offset", 0)

    for update in updates:
        update_id = update["update_id"]
        new_offset = max(new_offset, update_id + 1)

        msg = update.get("message")
        cb = update.get("callback_query")

        # ---- Handle boon choice callbacks ----
        if cb:
            process_boon_callback(cb, config, state)
            continue

        if not msg:
            continue

        chat_id = msg.get("chat", {}).get("id")
        if chat_id != group_id:
            continue

        thread_id = msg.get("message_thread_id")
        if thread_id is None:
            continue

        thread_id_str = str(thread_id)

        if thread_id_str not in pbp_topic_ids:
            continue

        from_user = msg.get("from", {})
        if from_user.get("is_bot", False):
            continue

        user_id = str(from_user.get("id", ""))
        user_name = from_user.get("first_name", "Someone")
        user_last_name = from_user.get("last_name", "")
        username = from_user.get("username", "")
        campaign_name = pbp_topic_ids[thread_id_str]
        now_iso = datetime.now(timezone.utc).isoformat()
        # Use the actual Telegram message timestamp for gap calculations
        msg_date = msg.get("date")
        if msg_date:
            msg_time_iso = datetime.fromtimestamp(msg_date, tz=timezone.utc).isoformat()
        else:
            msg_time_iso = now_iso
        text = msg.get("text", "").strip().lower()

        # ---- Combat commands (GM only) ----
        if "combat" not in state:
            state["combat"] = {}

        if user_id in gm_ids:
            if text.startswith("/combat"):
                parts = text.split()
                players_first = True
                if len(parts) > 1 and parts[1] == "enemies":
                    players_first = False

                state["combat"][thread_id_str] = {
                    "active": True,
                    "campaign_name": campaign_name,
                    "players_first": players_first,
                    "current_phase": "players" if players_first else "enemies",
                    "round": 1,
                    "phase_started_at": now_iso,
                    "players_acted": [],
                    "last_ping_at": None,
                }
                phase = "Players" if players_first else "Enemies"
                print(f"Combat started in {campaign_name}: {phase} go first")

            elif text.startswith("/round"):
                combat = state["combat"].get(thread_id_str)
                if combat and combat["active"]:
                    old_phase = combat["current_phase"]
                    if old_phase == "players":
                        combat["current_phase"] = "enemies"
                    else:
                        combat["current_phase"] = "players"
                        combat["round"] += 1
                        combat["players_acted"] = []
                    combat["phase_started_at"] = now_iso
                    combat["last_ping_at"] = None
                    print(f"Combat round advance in {campaign_name}: "
                          f"round {combat['round']}, phase: {combat['current_phase']}")

            elif text.startswith("/endcombat"):
                if thread_id_str in state["combat"]:
                    del state["combat"][thread_id_str]
                    print(f"Combat ended in {campaign_name}")

        # ---- Track player action during combat ----
        combat = state["combat"].get(thread_id_str)
        if (combat and combat["active"]
                and combat["current_phase"] == "players"
                and user_id not in gm_ids
                and user_id not in combat.get("players_acted", [])):
            combat["players_acted"].append(user_id)

        # Update topic-level tracking (for 4-hour alerts)
        state["topics"][thread_id_str] = {
            "last_message_time": msg_time_iso,
            "last_user": user_name,
            "last_user_id": user_id,
            "campaign_name": campaign_name,
        }

        # Increment message count for this user in this topic
        if "message_counts" not in state:
            state["message_counts"] = {}
        if thread_id_str not in state["message_counts"]:
            state["message_counts"][thread_id_str] = {}
        user_counts = state["message_counts"][thread_id_str]
        user_counts[user_id] = user_counts.get(user_id, 0) + 1

        # Track post timestamps for Player of the Week gap calculation
        if "post_timestamps" not in state:
            state["post_timestamps"] = {}
        if thread_id_str not in state["post_timestamps"]:
            state["post_timestamps"][thread_id_str] = {}
        if user_id not in state["post_timestamps"][thread_id_str]:
            state["post_timestamps"][thread_id_str][user_id] = []
        state["post_timestamps"][thread_id_str][user_id].append(msg_time_iso)

        # Update player-level tracking (skip GM)
        if user_id and user_id not in gm_ids:
            player_key = f"{thread_id_str}:{user_id}"
            was_removed = player_key in state["removed_players"]

            state["players"][player_key] = {
                "user_id": user_id,
                "first_name": user_name,
                "last_name": user_last_name,
                "username": username,
                "campaign_name": campaign_name,
                "pbp_topic_id": thread_id_str,
                "last_post_time": msg_time_iso,
                "last_warned_week": 0,
            }

            if was_removed:
                del state["removed_players"][player_key]
                print(f"Player {user_name} rejoined {campaign_name}")

        print(f"Tracked message in {campaign_name} from {user_name}")

    return new_offset


# ------------------------------------------------------------------ #
#  Topic inactivity alerts (4-hour)
# ------------------------------------------------------------------ #
def check_and_alert(config: dict, state: dict):
    group_id = config["group_id"]
    alert_hours = config.get("alert_after_hours", 4)
    now = datetime.now(timezone.utc)

    for pair in config["topic_pairs"]:
        pbp_id_str = str(pair["pbp_topic_id"])
        chat_topic_id = pair["chat_topic_id"]
        name = pair["name"]

        if pbp_id_str not in state.get("topics", {}):
            print(f"No messages tracked yet for {name}, skipping")
            continue

        topic_state = state["topics"][pbp_id_str]
        last_time = datetime.fromisoformat(topic_state["last_message_time"])
        elapsed_hours = (now - last_time).total_seconds() / 3600

        if elapsed_hours < alert_hours:
            continue

        # Don't re-alert within alert_hours
        last_alert_str = state["last_alerts"].get(pbp_id_str)
        if last_alert_str:
            since_last = (now - datetime.fromisoformat(last_alert_str)).total_seconds() / 3600
            if since_last < alert_hours:
                print(f"{name}: Already alerted {since_last:.1f}h ago, skipping")
                continue

        hours_int = int(elapsed_hours)
        days = hours_int // 24
        remaining_hours = hours_int % 24
        last_user = topic_state.get("last_user", "someone")
        last_user_id = topic_state.get("last_user_id", "")

        time_str = f"{days}d {remaining_hours}h" if days > 0 else f"{hours_int}h"

        # Look up total message count for last poster
        count = state.get("message_counts", {}).get(pbp_id_str, {}).get(last_user_id, 0)
        count_str = f" ({count} total posts)" if count > 0 else ""

        last_msg_time = datetime.fromisoformat(topic_state["last_message_time"])
        last_date = fmt_date(last_msg_time)

        message = (
            f"No new posts in {name} PBP for {time_str}.\n"
            f"Last post was from {last_user}{count_str} on {last_date}."
        )

        print(f"Sending alert for {name}: {time_str} inactive")
        if send_message(group_id, chat_topic_id, message):
            state["last_alerts"][pbp_id_str] = now.isoformat()


# ------------------------------------------------------------------ #
#  Player inactivity tracking (weekly)
# ------------------------------------------------------------------ #
def check_player_activity(config: dict, state: dict):
    group_id = config["group_id"]
    now = datetime.now(timezone.utc)

    # Build lookup: pbp_topic_id -> chat_topic_id
    chat_topics = {}
    for pair in config["topic_pairs"]:
        chat_topics[str(pair["pbp_topic_id"])] = pair["chat_topic_id"]

    players_to_remove = []

    for player_key, player in state["players"].items():
        pbp_topic_id = player["pbp_topic_id"]
        chat_topic_id = chat_topics.get(pbp_topic_id)
        if not chat_topic_id:
            continue

        last_post = datetime.fromisoformat(player["last_post_time"])
        elapsed_weeks = (now - last_post).total_seconds() / (7 * 86400)
        current_week = int(elapsed_weeks)
        last_warned = player.get("last_warned_week", 0)

        first_name = player["first_name"]
        last_name = player.get("last_name", "")
        username = player.get("username", "")
        campaign = player["campaign_name"]
        mention = display_name(first_name, username, last_name)
        days_inactive = int((now - last_post).total_seconds() / 86400)
        last_date = fmt_date(last_post)

        # 4+ weeks: remove
        if current_week >= PLAYER_REMOVE_WEEKS:
            if last_warned < PLAYER_REMOVE_WEEKS:
                message = (
                    f"{mention} has not posted in {campaign} PBP for "
                    f"{days_inactive} days (last: {last_date}). They are no longer tracked "
                    f"as an active player in this campaign."
                )
                print(f"Removing {first_name} from {campaign} ({days_inactive}d)")
                send_message(group_id, chat_topic_id, message)
                players_to_remove.append(player_key)
            continue

        # 1, 2, 3 week warnings
        for week_mark in PLAYER_WARN_WEEKS:
            if current_week >= week_mark and last_warned < week_mark:
                if week_mark == 1:
                    message = (
                        f"{mention} hasn't posted in {campaign} PBP "
                        f"for {days_inactive} days (last: {last_date}). Everything okay?"
                    )
                elif week_mark == 2:
                    message = (
                        f"{mention} still no post in {campaign} PBP. "
                        f"It's been {days_inactive} days now (last: {last_date})."
                    )
                else:
                    message = (
                        f"{mention} it's been {days_inactive} days without "
                        f"a post in {campaign} PBP (last: {last_date}). 1 week until "
                        f"auto-removal from the campaign."
                    )

                print(f"Warning {first_name} in {campaign}: week {week_mark}")
                if send_message(group_id, chat_topic_id, message):
                    player["last_warned_week"] = week_mark
                break  # One warning per player per run

    # Move removed players out
    for key in players_to_remove:
        removed = state["players"].pop(key)
        state["removed_players"][key] = {
            "removed_at": now.isoformat(),
            "first_name": removed["first_name"],
            "username": removed.get("username", ""),
            "campaign_name": removed["campaign_name"],
        }




# ------------------------------------------------------------------ #
#  Party roster summary (every 3 days)
# ------------------------------------------------------------------ #
def post_roster_summary(config: dict, state: dict):
    """Post a summary of all tracked players per campaign to CHAT topics."""
    group_id = config["group_id"]
    now = datetime.now(timezone.utc)

    if "last_roster" not in state:
        state["last_roster"] = {}

    # Build lookup: pbp_topic_id -> chat_topic_id
    chat_topics = {}
    campaign_names = {}
    for pair in config["topic_pairs"]:
        pid = str(pair["pbp_topic_id"])
        chat_topics[pid] = pair["chat_topic_id"]
        campaign_names[pid] = pair["name"]

    # Group players by campaign (pbp_topic_id)
    campaigns = {}
    for player_key, player in state.get("players", {}).items():
        pid = player["pbp_topic_id"]
        if pid not in campaigns:
            campaigns[pid] = []
        campaigns[pid].append(player)

    # Also include GM message counts
    gm_ids = set(str(uid) for uid in config.get("gm_user_ids", []))

    for pid, chat_topic_id in chat_topics.items():
        # Check if we posted a roster recently
        last_roster_str = state["last_roster"].get(pid)
        if last_roster_str:
            last_roster = datetime.fromisoformat(last_roster_str)
            days_since = (now - last_roster).total_seconds() / 86400
            if days_since < ROSTER_INTERVAL_DAYS:
                continue

        name = campaign_names.get(pid, "Unknown")
        players = campaigns.get(pid, [])
        counts = state.get("message_counts", {}).get(pid, {})

        if not players and not counts:
            # No data yet for this campaign
            continue

        # Build player lines sorted by message count (descending)
        lines = []
        for player in sorted(players, key=lambda p: counts.get(p["user_id"], 0), reverse=True):
            uid = player["user_id"]
            display = display_name(player["first_name"], player.get("username", ""), player.get("last_name", ""))
            count = counts.get(uid, 0)
            last_post = datetime.fromisoformat(player["last_post_time"])
            time_str = fmt_relative_date(now, last_post)

            lines.append(f"  {display}: {posts_str(count)} (last: {time_str})")

        # Add GM stats if present
        for gm_id in gm_ids:
            gm_count = counts.get(gm_id, 0)
            if gm_count > 0:
                lines.insert(0, f"  GM: {posts_str(gm_count)}")

        if not lines:
            continue

        message = f"Party roster for {name}:\n" + "\n".join(lines)

        print(f"Posting roster for {name}")
        if send_message(group_id, chat_topic_id, message):
            state["last_roster"][pid] = now.isoformat()


# ------------------------------------------------------------------ #
#  Player of the Week (weekly, consistency-based)
# ------------------------------------------------------------------ #
def player_of_the_week(config: dict, state: dict):
    """Award Player of the Week based on smallest average gap between posts."""
    group_id = config["group_id"]
    now = datetime.now(timezone.utc)
    gm_ids = set(str(uid) for uid in config.get("gm_user_ids", []))

    if "last_potw" not in state:
        state["last_potw"] = {}

    # Load boons
    try:
        with open(BOONS_PATH) as f:
            boons = json.load(f)
    except Exception as e:
        print(f"Warning: Could not load boons: {e}")
        boons = ["Something mildly beneficial happens to you today."]

    # Build lookup
    chat_topics = {}
    campaign_names = {}
    for pair in config["topic_pairs"]:
        pid = str(pair["pbp_topic_id"])
        chat_topics[pid] = pair["chat_topic_id"]
        campaign_names[pid] = pair["name"]

    week_ago = now - timedelta(days=7)

    for pid, chat_topic_id in chat_topics.items():
        # Check if we already awarded this week
        last_potw_str = state["last_potw"].get(pid)
        if last_potw_str:
            last_potw = datetime.fromisoformat(last_potw_str)
            days_since = (now - last_potw).total_seconds() / 86400
            if days_since < POTW_INTERVAL_DAYS:
                continue

        name = campaign_names.get(pid, "Unknown")
        topic_timestamps = state.get("post_timestamps", {}).get(pid, {})

        # Calculate average gap for each player this week
        candidates = []

        for user_id, timestamps in topic_timestamps.items():
            if user_id in gm_ids:
                continue

            # Filter to this week's posts only
            week_posts = []
            for ts in timestamps:
                post_time = datetime.fromisoformat(ts)
                if post_time >= week_ago:
                    week_posts.append(post_time)

            if len(week_posts) < POTW_MIN_POSTS:
                continue

            # Sort and calculate gaps
            week_posts.sort()
            gaps = []
            for i in range(1, len(week_posts)):
                gap_hours = (week_posts[i] - week_posts[i - 1]).total_seconds() / 3600
                gaps.append(gap_hours)

            avg_gap = sum(gaps) / len(gaps) if gaps else float("inf")

            # Find player name
            player_key = f"{pid}:{user_id}"
            player = state.get("players", {}).get(player_key, {})
            first_name = player.get("first_name", "Unknown")
            last_name = player.get("last_name", "")
            username = player.get("username", "")

            candidates.append({
                "user_id": user_id,
                "first_name": first_name,
                "last_name": last_name,
                "username": username,
                "avg_gap_hours": avg_gap,
                "post_count": len(week_posts),
            })

        if not candidates:
            print(f"No POTW candidates for {name} (need {POTW_MIN_POSTS}+ posts)")
            continue

        # Winner = smallest average gap
        winner = min(candidates, key=lambda c: c["avg_gap_hours"])
        mention = display_name(winner["first_name"], winner["username"], winner["last_name"])
        avg_gap_str = f"{winner['avg_gap_hours']:.1f}h"

        # Date range for display
        date_from = fmt_date(week_ago)
        date_to = fmt_date(now)

        # Pick 3 random boons
        chosen_boons = random.sample(boons, min(3, len(boons)))

        base_message = (
            f"Player of the Week for {name}: {mention}!\n"
            f"({date_from} to {date_to})\n\n"
            f"{posts_str(winner['post_count'])} this week with an average "
            f"gap of {avg_gap_str} between posts. The most consistent "
            f"driver of the story."
        )

        boon_text = "\n\nChoose your boon:\n"
        for i, b in enumerate(chosen_boons):
            boon_text += f"\n{i + 1}. {b}\n"

        full_text = base_message + boon_text

        # Build inline keyboard buttons
        buttons = []
        for i in range(len(chosen_boons)):
            buttons.append({
                "text": f"Boon #{i + 1}",
                "callback_data": f"boon:{pid}:{i}",
            })

        print(f"POTW for {name}: {winner['first_name']} (avg gap {avg_gap_str})")
        msg_id = send_message_with_buttons(group_id, chat_topic_id, full_text, buttons)
        if msg_id:
            state["last_potw"][pid] = now.isoformat()

            # Store pending choice
            if "pending_potw_boons" not in state:
                state["pending_potw_boons"] = {}
            state["pending_potw_boons"][pid] = {
                "message_id": msg_id,
                "winner_user_id": winner["user_id"],
                "boons": chosen_boons,
                "base_message": base_message,
                "posted_at": now.isoformat(),
            }


# ------------------------------------------------------------------ #
#  Combat turn pinger (side-based initiative)
# ------------------------------------------------------------------ #
def check_combat_turns(config: dict, state: dict):
    """During players' phase, ping players who haven't acted yet."""
    group_id = config["group_id"]
    now = datetime.now(timezone.utc)

    if "combat" not in state:
        return

    # Build lookup: pbp_topic_id -> chat_topic_id
    chat_topics = {}
    for pair in config["topic_pairs"]:
        chat_topics[str(pair["pbp_topic_id"])] = pair["chat_topic_id"]

    for pid, combat in list(state["combat"].items()):
        if not combat.get("active"):
            continue

        if combat["current_phase"] != "players":
            continue

        # Check if enough time has passed since phase started
        phase_start = datetime.fromisoformat(combat["phase_started_at"])
        hours_elapsed = (now - phase_start).total_seconds() / 3600

        if hours_elapsed < COMBAT_PING_HOURS:
            continue

        # Don't re-ping within COMBAT_PING_HOURS
        last_ping_str = combat.get("last_ping_at")
        if last_ping_str:
            since_ping = (now - datetime.fromisoformat(last_ping_str)).total_seconds() / 3600
            if since_ping < COMBAT_PING_HOURS:
                continue

        # Find all known players in this campaign who haven't acted
        acted = set(combat.get("players_acted", []))
        missing = []

        for player_key, player in state.get("players", {}).items():
            if player["pbp_topic_id"] != pid:
                continue
            if player["user_id"] in acted:
                continue

            uname = player.get("username", "")
            missing.append(display_name(player["first_name"], uname, player.get("last_name", "")))

        if not missing:
            continue

        campaign_name = combat.get("campaign_name", "Unknown")
        round_num = combat.get("round", 1)
        hours_int = int(hours_elapsed)

        chat_topic_id = chat_topics.get(pid)
        if not chat_topic_id:
            continue

        missing_str = ", ".join(missing)
        phase_date = fmt_date(datetime.fromisoformat(combat["phase_started_at"]))
        message = (
            f"Round {round_num} - waiting on: {missing_str}\n"
            f"({hours_int}h since players' phase started on {phase_date})"
        )

        print(f"Combat ping in {campaign_name}: waiting on {missing_str}")
        if send_message(group_id, chat_topic_id, message):
            combat["last_ping_at"] = now.isoformat()


# ------------------------------------------------------------------ #
#  Weekly data archive (preserves long-term trends)
# ------------------------------------------------------------------ #
def archive_weekly_data(config: dict, state: dict):
    """Archive weekly summaries to a JSON file in the repo.

    Stores compact per-campaign stats keyed by ISO week (e.g. '2026-W07').
    The file is committed back to the repo by the GitHub Actions workflow,
    giving full git history and no gist size concerns.
    """
    now = datetime.now(timezone.utc)
    gm_ids = set(str(uid) for uid in config.get("gm_user_ids", []))

    # Use last week's ISO week number (since current week is still in progress)
    last_week = now - timedelta(days=7)
    year, week_num, _ = last_week.isocalendar()
    week_key = f"{year}-W{week_num:02d}"

    # Check if we already archived this week (tracked in gist state)
    if state.get("last_archived_week") == week_key:
        return

    # Load existing archive from repo file
    ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(ARCHIVE_PATH) as f:
            archive = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        archive = {}

    week_start = now - timedelta(days=now.weekday() + 7)  # Start of last week (Monday)
    week_end = week_start + timedelta(days=7)

    for pair in config["topic_pairs"]:
        pid = str(pair["pbp_topic_id"])
        name = pair["name"]
        topic_timestamps = state.get("post_timestamps", {}).get(pid, {})

        gm_posts = 0
        player_posts = 0
        player_counts = {}
        player_post_times = []

        for uid, timestamps in topic_timestamps.items():
            is_gm = uid in gm_ids
            player_key = f"{pid}:{uid}"
            player_info = state.get("players", {}).get(player_key, {})

            for ts in timestamps:
                post_time = datetime.fromisoformat(ts)
                if week_start <= post_time < week_end:
                    if is_gm:
                        gm_posts += 1
                    else:
                        player_posts += 1
                        player_post_times.append(post_time)
                        p_name = display_name(
                            player_info.get("first_name", "Unknown"),
                            player_info.get("username", ""),
                            player_info.get("last_name", ""),
                        )
                        player_counts[p_name] = player_counts.get(p_name, 0) + 1

        # Calculate player avg gap
        player_avg_gap = None
        if len(player_post_times) >= 2:
            player_post_times.sort()
            gaps = []
            for i in range(1, len(player_post_times)):
                gap_h = (player_post_times[i] - player_post_times[i - 1]).total_seconds() / 3600
                gaps.append(gap_h)
            player_avg_gap = round(sum(gaps) / len(gaps), 1)

        # Count active players
        active_players = len([
            pk for pk, p in state.get("players", {}).items()
            if p.get("pbp_topic_id") == pid
        ])

        archive_key = f"{pid}:{week_key}"
        archive[archive_key] = {
            "campaign": name,
            "week": week_key,
            "gm_posts": gm_posts,
            "player_posts": player_posts,
            "total_posts": gm_posts + player_posts,
            "player_avg_gap_h": player_avg_gap,
            "active_players": active_players,
            "top_players": dict(sorted(
                player_counts.items(), key=lambda x: x[1], reverse=True
            )[:5]),
        }

    # Write archive to repo file
    with open(ARCHIVE_PATH, "w") as f:
        json.dump(archive, f, indent=2)

    state["last_archived_week"] = week_key
    print(f"Archived weekly data for {week_key} to {ARCHIVE_PATH}")


# ------------------------------------------------------------------ #
#  Timestamp cleanup (keep only last 15 days)
# ------------------------------------------------------------------ #
def cleanup_timestamps(state: dict):
    """Prune old timestamps to prevent gist from growing indefinitely."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()

    for pid in list(state.get("post_timestamps", {}).keys()):
        for uid in list(state["post_timestamps"][pid].keys()):
            filtered = [
                ts for ts in state["post_timestamps"][pid][uid]
                if ts >= cutoff
            ]
            if filtered:
                state["post_timestamps"][pid][uid] = filtered
            else:
                del state["post_timestamps"][pid][uid]
        if not state["post_timestamps"][pid]:
            del state["post_timestamps"][pid]


# ------------------------------------------------------------------ #
#  Weekly pace report
# ------------------------------------------------------------------ #
def post_pace_report(config: dict, state: dict):
    """Post weekly pace comparison: posts/day this week vs last week, split GM/players."""
    group_id = config["group_id"]
    now = datetime.now(timezone.utc)
    gm_ids = set(str(uid) for uid in config.get("gm_user_ids", []))

    if "last_pace" not in state:
        state["last_pace"] = {}

    chat_topics = {}
    campaign_names = {}
    for pair in config["topic_pairs"]:
        pid = str(pair["pbp_topic_id"])
        chat_topics[pid] = pair["chat_topic_id"]
        campaign_names[pid] = pair["name"]

    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    for pid, chat_topic_id in chat_topics.items():
        last_pace_str = state["last_pace"].get(pid)
        if last_pace_str:
            days_since = (now - datetime.fromisoformat(last_pace_str)).total_seconds() / 86400
            if days_since < PACE_INTERVAL_DAYS:
                continue

        name = campaign_names.get(pid, "Unknown")
        topic_timestamps = state.get("post_timestamps", {}).get(pid, {})

        if not topic_timestamps:
            continue

        # Count posts split by GM vs players
        gm_this = 0
        gm_last = 0
        player_this = 0
        player_last = 0
        for uid, timestamps in topic_timestamps.items():
            is_gm = uid in gm_ids
            for ts in timestamps:
                post_time = datetime.fromisoformat(ts)
                if post_time >= week_ago:
                    if is_gm:
                        gm_this += 1
                    else:
                        player_this += 1
                elif post_time >= two_weeks_ago:
                    if is_gm:
                        gm_last += 1
                    else:
                        player_last += 1

        this_week = gm_this + player_this
        last_week = gm_last + player_last
        this_avg = this_week / 7.0
        last_avg = last_week / 7.0

        # Determine trend
        if last_avg == 0 and this_avg == 0:
            continue  # No data
        elif last_avg == 0:
            trend = "NEW"
            trend_icon = "🆕"
        elif this_avg > last_avg * 1.15:
            trend = "UP"
            trend_icon = "📈"
        elif this_avg < last_avg * 0.85:
            trend = "DOWN"
            trend_icon = "📉"
        else:
            trend = "STEADY"
            trend_icon = "➡️"

        this_week_start = fmt_date(week_ago)
        this_week_end = fmt_date(now)
        last_week_start = fmt_date(two_weeks_ago)
        last_week_end = fmt_date(week_ago)

        message = (
            f"{trend_icon} Weekly pace for {name}:\n"
            f"\n"
            f"This week ({this_week_start} to {this_week_end}):\n"
            f"  GM: {gm_this} posts ({gm_this / 7.0:.1f}/day)\n"
            f"  Players: {player_this} posts ({player_this / 7.0:.1f}/day)\n"
            f"  Total: {this_week} posts ({this_avg:.1f}/day)\n"
            f"\n"
            f"Last week ({last_week_start} to {last_week_end}):\n"
            f"  GM: {gm_last} posts ({gm_last / 7.0:.1f}/day)\n"
            f"  Players: {player_last} posts ({player_last / 7.0:.1f}/day)\n"
            f"  Total: {last_week} posts ({last_avg:.1f}/day)\n"
            f"\n"
            f"Trend: {trend}"
        )

        print(f"Pace report for {name}: {this_week} vs {last_week} ({trend})")
        if send_message(group_id, chat_topic_id, message):
            state["last_pace"][pid] = now.isoformat()


# ------------------------------------------------------------------ #
#  Campaign anniversary alerts
# ------------------------------------------------------------------ #
def check_anniversaries(config: dict, state: dict):
    """Post a celebration when a campaign hits a yearly anniversary."""
    group_id = config["group_id"]
    now = datetime.now(timezone.utc)
    today = now.date()

    if "last_anniversary" not in state:
        state["last_anniversary"] = {}

    for pair in config["topic_pairs"]:
        pid = str(pair["pbp_topic_id"])
        chat_topic_id = pair["chat_topic_id"]
        name = pair["name"]
        created_str = pair.get("created")

        if not created_str:
            continue

        created = datetime.strptime(created_str, "%Y-%m-%d").date()

        # Check if today is the anniversary (same month and day)
        if today.month != created.month or today.day != created.day:
            continue

        # How many years?
        years = today.year - created.year
        if years < 1:
            continue

        # Don't post the same anniversary twice
        anniversary_key = f"{pid}:{years}"
        if anniversary_key in state["last_anniversary"]:
            continue

        if years == 1:
            year_str = "1 year"
        else:
            year_str = f"{years} years"

        message = (
            f"🎂 {name} is {year_str} old today!\n\n"
            f"Campaign started {created.strftime('%B %d, %Y')}. "
            f"Here's to more adventures ahead."
        )

        print(f"Anniversary for {name}: {year_str}")
        if send_message(group_id, chat_topic_id, message):
            state["last_anniversary"][anniversary_key] = now.isoformat()


# ------------------------------------------------------------------ #
#  Campaign Leaderboard (cross-campaign dashboard)
# ------------------------------------------------------------------ #
def post_campaign_leaderboard(config: dict, state: dict):
    """Post a cross-campaign activity leaderboard to the ISSUES topic."""
    group_id = config["group_id"]
    leaderboard_topic = config.get("leaderboard_topic_id")
    if not leaderboard_topic:
        return

    now = datetime.now(timezone.utc)
    gm_ids = set(str(uid) for uid in config.get("gm_user_ids", []))

    if "last_leaderboard" not in state:
        state["last_leaderboard"] = None

    # Check interval
    last_lb_str = state["last_leaderboard"]
    if last_lb_str:
        days_since = (now - datetime.fromisoformat(last_lb_str)).total_seconds() / 86400
        if days_since < LEADERBOARD_INTERVAL_DAYS:
            return

    seven_days_ago = now - timedelta(days=7)
    three_days_ago = now - timedelta(days=3)
    six_days_ago = now - timedelta(days=6)  # previous 3-day window

    campaign_stats = []

    for pair in config["topic_pairs"]:
        pid = str(pair["pbp_topic_id"])
        name = pair["name"]
        topic_timestamps = state.get("post_timestamps", {}).get(pid, {})

        # Gather all posts in windows
        gm_7d = 0
        player_7d = 0
        posts_recent_3d = 0
        posts_prev_3d = 0
        player_post_counts = {}  # user_id -> {name, username, count}
        all_post_times_7d = []
        player_post_times_7d = []

        for uid, timestamps in topic_timestamps.items():
            is_gm = uid in gm_ids
            player_key = f"{pid}:{uid}"
            player_info = state.get("players", {}).get(player_key, {})
            p_name = player_info.get("first_name", "Unknown")
            p_last_name = player_info.get("last_name", "")
            p_username = player_info.get("username", "")

            for ts in timestamps:
                post_time = datetime.fromisoformat(ts)

                # 7-day window
                if post_time >= seven_days_ago:
                    all_post_times_7d.append(post_time)
                    if is_gm:
                        gm_7d += 1
                    else:
                        player_7d += 1
                        player_post_times_7d.append(post_time)
                        if uid not in player_post_counts:
                            player_post_counts[uid] = {
                                "name": p_name,
                                "last_name": p_last_name,
                                "username": p_username,
                                "count": 0,
                            }
                        player_post_counts[uid]["count"] += 1

                # 3-day trend windows
                if post_time >= three_days_ago:
                    posts_recent_3d += 1
                elif post_time >= six_days_ago:
                    posts_prev_3d += 1

        total_7d = gm_7d + player_7d

        # Average response gap (all posts sorted chronologically)
        avg_gap_str = "N/A"
        if len(all_post_times_7d) >= 2:
            all_post_times_7d.sort()
            gaps = []
            for i in range(1, len(all_post_times_7d)):
                gap_h = (all_post_times_7d[i] - all_post_times_7d[i - 1]).total_seconds() / 3600
                gaps.append(gap_h)
            avg_gap = sum(gaps) / len(gaps)
            avg_gap_str = f"{avg_gap:.1f}h"

        # Player-only average gap
        player_avg_gap = None
        player_avg_gap_str = "N/A"
        if len(player_post_times_7d) >= 2:
            player_post_times_7d.sort()
            p_gaps = []
            for i in range(1, len(player_post_times_7d)):
                gap_h = (player_post_times_7d[i] - player_post_times_7d[i - 1]).total_seconds() / 3600
                p_gaps.append(gap_h)
            player_avg_gap = sum(p_gaps) / len(p_gaps)
            player_avg_gap_str = f"{player_avg_gap:.1f}h"

        # Days since last post
        last_post_time = None
        for uid, timestamps in topic_timestamps.items():
            for ts in timestamps:
                pt = datetime.fromisoformat(ts)
                if last_post_time is None or pt > last_post_time:
                    last_post_time = pt

        if last_post_time:
            days_since_last = (now - last_post_time).total_seconds() / 86400
            if days_since_last < 0.04:  # ~1 hour
                last_post_str = "today"
            elif days_since_last < 1:
                hours = int(days_since_last * 24)
                last_post_str = f"{hours}h ago"
            elif days_since_last < 2:
                last_post_str = "yesterday"
            else:
                last_post_str = f"{int(days_since_last)}d ago"
        else:
            days_since_last = 999
            last_post_str = "never"

        # 3-day trend
        if posts_prev_3d == 0 and posts_recent_3d == 0:
            trend_icon = "💤"
        elif posts_prev_3d == 0:
            trend_icon = "🆕"
        elif posts_recent_3d > posts_prev_3d * 1.15:
            trend_icon = "📈"
        elif posts_recent_3d < posts_prev_3d * 0.85:
            trend_icon = "📉"
        else:
            trend_icon = "➡️"

        # Top 3 players by post count
        top_players = sorted(
            player_post_counts.values(),
            key=lambda p: p["count"],
            reverse=True,
        )[:3]

        campaign_stats.append({
            "name": name,
            "total_7d": total_7d,
            "player_7d": player_7d,
            "gm_7d": gm_7d,
            "trend_icon": trend_icon,
            "avg_gap_str": avg_gap_str,
            "player_avg_gap": player_avg_gap,
            "player_avg_gap_str": player_avg_gap_str,
            "last_post_str": last_post_str,
            "days_since_last": days_since_last,
            "top_players": top_players,
        })

    if not campaign_stats:
        print("No campaign data for leaderboard")
        return

    # Sort by player posts descending
    campaign_stats.sort(key=lambda c: c["player_7d"], reverse=True)

    # Split into active and dead (based on any posts including GM)
    active = [c for c in campaign_stats if c["total_7d"] > 0]
    dead = [c for c in campaign_stats if c["total_7d"] == 0]

    date_from = fmt_date(seven_days_ago)
    date_to = fmt_date(now)
    rank_icons = ["🥇", "🥈", "🥉"]
    player_medals = ["🥇", "🥈", "🥉"]

    lines = [f"📊 Campaign Leaderboard ({date_from} to {date_to})\n"]

    for i, c in enumerate(active):
        rank = rank_icons[i] if i < 3 else f"{i + 1}."
        lines.append(
            f"\n{rank} {c['name']} {c['trend_icon']}\n"
            f"   {c['player_7d']} player posts ({posts_str(c['total_7d'])} total, {c['gm_7d']} GM)\n"
            f"   Avg gap: {c['avg_gap_str']} | Last post: {c['last_post_str']}"
        )

        for j, p in enumerate(c["top_players"]):
            medal = player_medals[j] if j < 3 else f"   {j + 1}."
            display = display_name(p["name"], p.get("username", ""), p.get("last_name", ""))
            lines.append(f"   {medal} {display}: {posts_str(p['count'])}")

    if dead:
        lines.append("\n⚠️ Dead campaigns (0 posts in 7 days):")
        for c in dead:
            lines.append(f"   💀 {c['name']} (last post: {c['last_post_str']})")

    # Player-only avg gap ranking
    gap_ranked = [
        c for c in campaign_stats if c["player_avg_gap"] is not None
    ]
    if gap_ranked:
        gap_ranked.sort(key=lambda c: c["player_avg_gap"])
        lines.append("\n⏱ Fastest player response gaps:")
        for i, c in enumerate(gap_ranked):
            icon = rank_icons[i] if i < 3 else f"{i + 1}."
            lines.append(f"   {icon} {c['name']}: {c['player_avg_gap_str']}")

    message = "\n".join(lines)

    print(f"Posting campaign leaderboard to ISSUES topic")
    if send_message(group_id, leaderboard_topic, message):
        state["last_leaderboard"] = now.isoformat()


# ------------------------------------------------------------------ #
#  Recruitment check (campaigns needing players)
# ------------------------------------------------------------------ #
def check_recruitment_needs(config: dict, state: dict):
    """If a campaign has fewer than REQUIRED_PLAYERS, post a notice."""
    group_id = config["group_id"]
    now = datetime.now(timezone.utc)

    if "last_recruitment_check" not in state:
        state["last_recruitment_check"] = {}

    for pair in config["topic_pairs"]:
        pid = str(pair["pbp_topic_id"])
        chat_topic_id = pair["chat_topic_id"]
        name = pair["name"]

        # Check interval
        last_check_str = state["last_recruitment_check"].get(pid)
        if last_check_str:
            days_since = (now - datetime.fromisoformat(last_check_str)).total_seconds() / 86400
            if days_since < RECRUITMENT_INTERVAL_DAYS:
                continue

        # Count active players (excluding GM)
        active = []
        for player_key, player in state.get("players", {}).items():
            if player.get("pbp_topic_id") == pid:
                p_display = display_name(
                    player["first_name"],
                    player.get("username", ""),
                    player.get("last_name", ""),
                )
                active.append(p_display)

        player_count = len(active)
        needed = REQUIRED_PLAYERS - player_count

        if needed <= 0:
            # Full roster, reset timer
            state["last_recruitment_check"][pid] = now.isoformat()
            continue

        # Build roster display
        if active:
            roster_lines = "\n".join(f"  - {p}" for p in active)
            roster_section = f"Current roster ({player_count}/{REQUIRED_PLAYERS}):\n{roster_lines}"
        else:
            roster_section = f"Current roster: 0/{REQUIRED_PLAYERS} (no active players)"

        message = (
            f"📢 {name} needs {needed} more player{'s' if needed != 1 else ''}!\n\n"
            f"{roster_section}\n\n"
            f"Know anyone who'd like to join? Send them to the recruitment topic!"
        )

        print(f"Recruitment notice for {name}: {player_count}/{REQUIRED_PLAYERS}")
        if send_message(group_id, chat_topic_id, message):
            state["last_recruitment_check"][pid] = now.isoformat()


# ------------------------------------------------------------------ #
#  Main
# ------------------------------------------------------------------ #
def main():
    if not TELEGRAM_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)

    config = load_config()
    state = load_state_from_gist()

    print(f"Loaded state. Offset: {state.get('offset', 0)}")
    print(f"Tracking {len(state.get('topics', {}))} topics, "
          f"{len(state.get('players', {}))} players")

    # Fetch and process new messages
    offset = state.get("offset", 0)
    updates = get_updates(offset)
    print(f"Received {len(updates)} new updates")

    if updates:
        state["offset"] = process_updates(updates, config, state)

    # Topic inactivity alerts (12-hour)
    check_and_alert(config, state)

    # Player inactivity checks (weekly)
    check_player_activity(config, state)

    # Party roster summary (every 3 days)
    post_roster_summary(config, state)

    # Player of the Week (weekly)
    player_of_the_week(config, state)

    # Expire unclaimed boon choices (48h)
    expire_pending_boons(config, state)

    # Weekly pace report
    post_pace_report(config, state)

    # Campaign anniversaries
    check_anniversaries(config, state)

    # Combat turn pinger
    check_combat_turns(config, state)

    # Campaign leaderboard (every 3 days, ISSUES topic)
    post_campaign_leaderboard(config, state)

    # Recruitment notices (every 2 weeks, campaigns under 6 players)
    check_recruitment_needs(config, state)

    # Archive weekly summaries (before pruning timestamps)
    archive_weekly_data(config, state)

    # Prune old timestamps
    cleanup_timestamps(state)

    # Save
    save_state_to_gist(state)
    print("Done")


if __name__ == "__main__":
    main()
