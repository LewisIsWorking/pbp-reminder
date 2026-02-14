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

PLAYER_WARN_WEEKS = [1, 2, 3]
PLAYER_REMOVE_WEEKS = 4
ROSTER_INTERVAL_DAYS = 3
POTW_INTERVAL_DAYS = 7
POTW_MIN_POSTS = 5
PACE_INTERVAL_DAYS = 7
COMBAT_PING_HOURS = 4


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
            "allowed_updates": json.dumps(["message"]),
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
        username = from_user.get("username", "")
        campaign_name = pbp_topic_ids[thread_id_str]
        now_iso = datetime.now(timezone.utc).isoformat()
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
            "last_message_time": now_iso,
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
        state["post_timestamps"][thread_id_str][user_id].append(now_iso)

        # Update player-level tracking (skip GM)
        if user_id and user_id not in gm_ids:
            player_key = f"{thread_id_str}:{user_id}"
            was_removed = player_key in state["removed_players"]

            state["players"][player_key] = {
                "user_id": user_id,
                "first_name": user_name,
                "username": username,
                "campaign_name": campaign_name,
                "pbp_topic_id": thread_id_str,
                "last_post_time": now_iso,
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

        message = (
            f"No new posts in {name} PBP for {time_str}.\n"
            f"Last post was from {last_user}{count_str}."
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
        username = player.get("username", "")
        campaign = player["campaign_name"]
        mention = f"@{username}" if username else first_name
        days_inactive = int((now - last_post).total_seconds() / 86400)

        # 4+ weeks: remove
        if current_week >= PLAYER_REMOVE_WEEKS:
            if last_warned < PLAYER_REMOVE_WEEKS:
                message = (
                    f"{mention} has not posted in {campaign} PBP for "
                    f"{days_inactive} days. They are no longer tracked "
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
                        f"for {days_inactive} days. Everything okay?"
                    )
                elif week_mark == 2:
                    message = (
                        f"{mention} still no post in {campaign} PBP. "
                        f"It's been {days_inactive} days now."
                    )
                else:
                    message = (
                        f"{mention} it's been {days_inactive} days without "
                        f"a post in {campaign} PBP. 1 week until "
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
            display = player["first_name"]
            count = counts.get(uid, 0)
            last_post = datetime.fromisoformat(player["last_post_time"])
            days_ago = int((now - last_post).total_seconds() / 86400)

            if days_ago == 0:
                time_str = "today"
            elif days_ago == 1:
                time_str = "yesterday"
            else:
                time_str = f"{days_ago}d ago"

            lines.append(f"  {display}: {count} posts (last: {time_str})")

        # Add GM stats if present
        for gm_id in gm_ids:
            gm_count = counts.get(gm_id, 0)
            if gm_count > 0:
                lines.insert(0, f"  GM: {gm_count} posts")

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
            username = player.get("username", "")

            candidates.append({
                "user_id": user_id,
                "first_name": first_name,
                "username": username,
                "avg_gap_hours": avg_gap,
                "post_count": len(week_posts),
            })

        if not candidates:
            print(f"No POTW candidates for {name} (need {POTW_MIN_POSTS}+ posts)")
            continue

        # Winner = smallest average gap
        winner = min(candidates, key=lambda c: c["avg_gap_hours"])
        mention = f"@{winner['username']}" if winner["username"] else winner["first_name"]
        avg_gap_str = f"{winner['avg_gap_hours']:.1f}h"
        boon = random.choice(boons)

        message = (
            f"Player of the Week for {name}: {mention}!\n\n"
            f"{winner['post_count']} posts this week with an average "
            f"gap of {avg_gap_str} between posts. The most consistent "
            f"driver of the story.\n\n"
            f"Your boon: {boon}"
        )

        print(f"POTW for {name}: {winner['first_name']} (avg gap {avg_gap_str})")
        if send_message(group_id, chat_topic_id, message):
            state["last_potw"][pid] = now.isoformat()


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
            mention = f"@{uname}" if uname else player["first_name"]
            missing.append(mention)

        if not missing:
            continue

        campaign_name = combat.get("campaign_name", "Unknown")
        round_num = combat.get("round", 1)
        hours_int = int(hours_elapsed)

        chat_topic_id = chat_topics.get(pid)
        if not chat_topic_id:
            continue

        missing_str = ", ".join(missing)
        message = (
            f"Round {round_num} - waiting on: {missing_str}\n"
            f"({hours_int}h since players' phase started)"
        )

        print(f"Combat ping in {campaign_name}: waiting on {missing_str}")
        if send_message(group_id, chat_topic_id, message):
            combat["last_ping_at"] = now.isoformat()


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

        message = (
            f"{trend_icon} Weekly pace for {name}:\n"
            f"GM: {gm_this} posts ({gm_this / 7.0:.1f}/day)\n"
            f"Players: {player_this} posts ({player_this / 7.0:.1f}/day)\n"
            f"Total: {this_week} posts ({this_avg:.1f}/day)\n"
            f"Last week: {last_week} ({last_avg:.1f}/day)\n"
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

    # Weekly pace report
    post_pace_report(config, state)

    # Campaign anniversaries
    check_anniversaries(config, state)

    # Combat turn pinger
    check_combat_turns(config, state)

    # Prune old timestamps
    cleanup_timestamps(state)

    # Save
    save_state_to_gist(state)
    print("Done")


if __name__ == "__main__":
    main()
