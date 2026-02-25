"""
PBP Inactivity Checker for GitHub Actions

Orchestrator that runs hourly via cron. Processes Telegram messages
and triggers all bot features (alerts, rosters, POTW, leaderboards, etc).

State is persisted between runs using a GitHub Gist.
Modules: telegram.py (API), state.py (persistence), helpers.py (utilities).
"""

import os
import sys
import json
import random
from datetime import datetime, timezone, timedelta

import helpers
import telegram as tg
import state as state_store

from helpers import (
    fmt_date, fmt_relative_date, html_escape,
    posts_str, deduplicate_posts, calc_avg_gap_str, build_topic_maps,
    timestamps_in_window,
)


# ------------------------------------------------------------------ #
#  Boon choice callback handler
# ------------------------------------------------------------------ #
def _format_boon_result(boons: list[str], chosen_idx: int, base_message: str, label: str) -> str:
    """Format POTW boon result message with chosen boon highlighted in HTML."""
    boon_lines = ""
    for i, b in enumerate(boons):
        escaped = html_escape(b)
        if i == chosen_idx:
            boon_lines += f"\n{i + 1}. {escaped} ✓\n"
        else:
            boon_lines += f"\n<s>{i + 1}. {escaped}</s>\n"
    return f"{html_escape(base_message)}\n\n{label}:{boon_lines}"


def process_boon_callback(cb: dict, config: dict, state: dict) -> None:
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
        tg.answer_callback(cb_id, "Invalid choice.")
        return

    topic_id = parts[1]
    try:
        choice_idx = int(parts[2])
    except ValueError:
        tg.answer_callback(cb_id, "Invalid choice.")
        return

    # Check pending choices
    pending = state.get("pending_potw_boons", {}).get(topic_id)
    if not pending:
        tg.answer_callback(cb_id, "This choice has expired.")
        return

    # Only the winner can choose
    if user_id != pending["winner_user_id"]:
        tg.answer_callback(cb_id, "Only the Player of the Week can choose!")
        return

    if choice_idx < 0 or choice_idx >= len(pending["boons"]):
        tg.answer_callback(cb_id, "Invalid choice.")
        return

    new_text = _format_boon_result(pending["boons"], choice_idx, pending["base_message"], "Chosen boon")

    tg.edit_message(chat_id, message_id, new_text, parse_mode="HTML")
    tg.answer_callback(cb_id, f"You chose boon #{choice_idx + 1}!")

    # Clean up pending state
    del state["pending_potw_boons"][topic_id]
    print(f"POTW boon chosen for topic {topic_id}: #{choice_idx + 1}")


def expire_pending_boons(config: dict, state: dict) -> None:
    """Auto-pick boon #1 if winner hasn't chosen within 48 hours."""
    now = datetime.now(timezone.utc)
    group_id = config["group_id"]
    pending = state.get("pending_potw_boons", {})

    for topic_id in list(pending.keys()):
        entry = pending[topic_id]
        posted_at = datetime.fromisoformat(entry["posted_at"])
        elapsed = helpers.hours_since(now, posted_at)

        if elapsed >= 48:
            new_text = _format_boon_result(entry["boons"], 0, entry["base_message"], "Boon (auto-selected)")

            tg.edit_message(group_id, entry["message_id"], new_text, parse_mode="HTML")
            del pending[topic_id]
            print(f"POTW boon auto-expired for topic {topic_id}, picked #1")


# ------------------------------------------------------------------ #
#  Process updates
# ------------------------------------------------------------------ #
def _handle_round_command(text: str, pid: str, campaign_name: str,
                          now_iso: str, group_id: int, thread_id: int, state: dict) -> None:
    """Parse and execute /round <N> <players|enemies> command."""
    parts = text.split()
    if len(parts) < 3:
        return

    try:
        round_num = int(parts[1])
    except ValueError:
        return

    phase = parts[2].lower()
    if not round_num or phase not in ("players", "enemies"):
        return

    if pid not in state["combat"]:
        state["combat"][pid] = {
            "active": True,
            "campaign_name": campaign_name,
            "round": round_num,
            "current_phase": phase,
            "phase_started_at": now_iso,
            "players_acted": [],
            "last_ping_at": None,
        }
    else:
        combat = state["combat"][pid]
        if phase == "players" and (
            combat["current_phase"] != "players"
            or combat["round"] != round_num
        ):
            combat["players_acted"] = []
        combat["round"] = round_num
        combat["current_phase"] = phase
        combat["phase_started_at"] = now_iso
        combat["last_ping_at"] = None

    phase_label = "Players" if phase == "players" else "Enemies"
    print(f"Combat in {campaign_name}: Round {round_num}, {phase_label}")
    tg.send_message(group_id, thread_id, f"Round {round_num}. {phase_label}' turn.")


def _handle_combat_message(
    text: str, user_id: str, gm_ids: set, pid: str, campaign_name: str,
    now_iso: str, group_id: int, thread_id: int, state: dict,
) -> None:
    """Process GM combat commands (/round, /endcombat) and track player actions."""
    if user_id in gm_ids:
        if text.startswith("/round"):
            _handle_round_command(text, pid, campaign_name, now_iso, group_id, thread_id, state)
        elif text.startswith("/endcombat") or text == "/combat end":
            if pid in state["combat"]:
                del state["combat"][pid]
                print(f"Combat ended in {campaign_name}")
                tg.send_message(group_id, thread_id, f"Combat ended in {campaign_name}.")

    # Track player action during combat
    combat = state["combat"].get(pid)
    if (combat and combat["active"]
            and combat["current_phase"] == "players"
            and user_id not in gm_ids
            and user_id not in combat.get("players_acted", [])):
        combat["players_acted"].append(user_id)


def process_updates(updates: list, config: dict, state: dict) -> int:
    """Process new Telegram updates, tracking posts and handling commands. Returns new offset."""
    group_id = config["group_id"]
    gm_ids = helpers.gm_id_set(config)

    maps = build_topic_maps(config)

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

        if thread_id_str not in maps.all_pbp_ids:
            continue

        from_user = msg.get("from", {})
        if from_user.get("is_bot", False):
            continue

        user_id = str(from_user.get("id", ""))
        user_name = from_user.get("first_name", "Someone")
        user_last_name = from_user.get("last_name", "")
        username = from_user.get("username", "")
        # Map to canonical topic ID so split topics merge
        pid = maps.to_canonical[thread_id_str]
        campaign_name = maps.to_name[pid]
        now_iso = datetime.now(timezone.utc).isoformat()
        # Use the actual Telegram message timestamp for gap calculations
        msg_date = msg.get("date")
        if msg_date:
            msg_time_iso = datetime.fromtimestamp(msg_date, tz=timezone.utc).isoformat()
        else:
            msg_time_iso = now_iso
        raw_text = msg.get("text", "").strip()
        text = raw_text.lower()

        # ---- Combat commands and tracking ----
        _handle_combat_message(
            text, user_id, gm_ids, pid, campaign_name,
            now_iso, group_id, thread_id, state,
        )

        # Update topic-level tracking (for 4-hour alerts)
        state["topics"][pid] = {
            "last_message_time": msg_time_iso,
            "last_user": user_name,
            "last_user_id": user_id,
            "campaign_name": campaign_name,
        }

        # Increment message count for this user in this topic
        user_counts = state["message_counts"].setdefault(pid, {})
        user_counts[user_id] = user_counts.get(user_id, 0) + 1

        # Track post timestamps for Player of the Week gap calculation
        state["post_timestamps"].setdefault(pid, {}).setdefault(user_id, []).append(msg_time_iso)

        # Update player-level tracking (skip GM)
        if user_id and user_id not in gm_ids:
            player_key = f"{pid}:{user_id}"
            was_removed = player_key in state["removed_players"]

            state["players"][player_key] = {
                "user_id": user_id,
                "first_name": user_name,
                "last_name": user_last_name,
                "username": username,
                "campaign_name": campaign_name,
                "pbp_topic_id": pid,
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
def check_and_alert(config: dict, state: dict) -> None:
    """Send alerts to campaigns inactive beyond alert_after_hours."""
    group_id = config["group_id"]
    alert_hours = config.get("alert_after_hours", 4)
    now = datetime.now(timezone.utc)

    maps = build_topic_maps(config)

    for pid, chat_topic_id in maps.to_chat.items():
        name = maps.to_name[pid]

        if pid not in state.get("topics", {}):
            print(f"No messages tracked yet for {name}, skipping")
            continue

        topic_state = state["topics"][pid]
        last_time = datetime.fromisoformat(topic_state["last_message_time"])
        elapsed_hours = helpers.hours_since(now, last_time)

        if elapsed_hours < alert_hours:
            continue

        # Don't re-alert within alert_hours
        last_alert_str = state["last_alerts"].get(pid)
        if last_alert_str:
            since_last = helpers.hours_since(now, datetime.fromisoformat(last_alert_str))
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
        count = state.get("message_counts", {}).get(pid, {}).get(last_user_id, 0)
        count_str = f" ({count} total posts)" if count > 0 else ""

        last_date = fmt_date(last_time)

        message = (
            f"No new posts in {name} PBP for {time_str}.\n"
            f"Last post was from {last_user}{count_str} on {last_date}."
        )

        print(f"Sending alert for {name}: {time_str} inactive")
        if tg.send_message(group_id, chat_topic_id, message):
            state["last_alerts"][pid] = now.isoformat()


# ------------------------------------------------------------------ #
#  Player inactivity tracking (weekly)
# ------------------------------------------------------------------ #
_INACTIVITY_TEMPLATES = {
    1: "{mention} hasn't posted in {campaign} PBP for {days} days (last: {date}). Everything okay?",
    2: "{mention} still no post in {campaign} PBP. It's been {days} days now (last: {date}).",
    3: "{mention} it's been {days} days without a post in {campaign} PBP (last: {date}). 1 week until auto-removal from the campaign.",
}


def check_player_activity(config: dict, state: dict) -> None:
    """Warn inactive players at 1/2/3 weeks, remove at 4 weeks."""
    group_id = config["group_id"]
    now = datetime.now(timezone.utc)

    # Build lookup: canonical pbp_topic_id -> chat_topic_id
    maps = build_topic_maps(config)

    players_to_remove = []

    for player_key, player in state["players"].items():
        pbp_topic_id = player["pbp_topic_id"]
        chat_topic_id = maps.to_chat.get(pbp_topic_id)
        if not chat_topic_id:
            continue

        last_post = datetime.fromisoformat(player["last_post_time"])
        elapsed_days = helpers.days_since(now, last_post)
        current_week = int(elapsed_days / 7)
        last_warned = player.get("last_warned_week", 0)

        first_name = player["first_name"]
        campaign = player["campaign_name"]
        mention = helpers.player_mention(player)
        days_inactive = int(elapsed_days)
        last_date = fmt_date(last_post)

        # 4+ weeks: remove
        if current_week >= helpers.PLAYER_REMOVE_WEEKS:
            if last_warned < helpers.PLAYER_REMOVE_WEEKS:
                message = (
                    f"{mention} has not posted in {campaign} PBP for "
                    f"{days_inactive} days (last: {last_date}). They are no longer tracked "
                    f"as an active player in this campaign."
                )
                print(f"Removing {first_name} from {campaign} ({days_inactive}d)")
                tg.send_message(group_id, chat_topic_id, message)
                players_to_remove.append(player_key)
            continue

        # 1, 2, 3 week warnings
        for week_mark in helpers.PLAYER_WARN_WEEKS:
            if current_week >= week_mark and last_warned < week_mark:
                template = _INACTIVITY_TEMPLATES.get(week_mark, _INACTIVITY_TEMPLATES[3])
                message = template.format(
                    mention=mention, campaign=campaign,
                    days=days_inactive, date=last_date,
                )
                print(f"Warning {first_name} in {campaign}: week {week_mark}")
                if tg.send_message(group_id, chat_topic_id, message):
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
def _roster_user_stats(raw_timestamps: list[str], total_count: int, now: datetime) -> dict:
    """Compute roster stats from raw ISO timestamp strings.

    Returns dict with: total, sessions, week_count, avg_gap_str, last_post_str.
    """
    week_ago = now - timedelta(days=7)
    all_posts = sorted(datetime.fromisoformat(ts) for ts in raw_timestamps)
    sessions = deduplicate_posts(all_posts)
    week_count = len(deduplicate_posts(timestamps_in_window(raw_timestamps, week_ago)))
    avg_gap_str = calc_avg_gap_str(raw_timestamps)
    last_post_str = fmt_relative_date(now, all_posts[-1]) if all_posts else "N/A"
    return {
        "total": total_count,
        "sessions": len(sessions),
        "week_count": week_count,
        "avg_gap_str": avg_gap_str,
        "last_post_str": last_post_str,
    }


def _roster_block(label: str, username: str, stats: dict) -> str:
    """Format a single roster entry (player or GM)."""
    s_suffix = "s" if stats["sessions"] != 1 else ""
    block = f"{label}\n"
    if username:
        block += f"- @{username}.\n"
    block += (
        f"- {posts_str(stats['total'])} total.\n"
        f"- {stats['sessions']} posting session{s_suffix}.\n"
        f"- {posts_str(stats['week_count'])} in the last week.\n"
        f"- Average gap between posting: {stats['avg_gap_str']}.\n"
        f"- Last post: {stats['last_post_str']}."
    )
    return block


def post_roster_summary(config: dict, state: dict) -> None:
    """Post a summary of all tracked players per campaign to CHAT topics."""
    group_id = config["group_id"]
    now = datetime.now(timezone.utc)

    maps = build_topic_maps(config)
    campaigns = helpers.players_by_campaign(state)
    gm_ids = helpers.gm_id_set(config)

    for pid, chat_topic_id in maps.to_chat.items():
        if not helpers.interval_elapsed(state["last_roster"].get(pid), helpers.ROSTER_INTERVAL_DAYS, now):
            continue

        name = maps.to_name.get(pid, "Unknown")
        players = campaigns.get(pid, [])
        counts = state.get("message_counts", {}).get(pid, {})
        topic_timestamps = helpers.get_topic_timestamps(state, pid)

        if not players and not counts:
            continue

        lines = []

        for player in sorted(players, key=lambda p: counts.get(p["user_id"], 0), reverse=True):
            uid = player["user_id"]
            raw_ts = topic_timestamps.get(uid, [])
            if not raw_ts:
                continue
            full = helpers.player_full_name(player)
            stats = _roster_user_stats(raw_ts, counts.get(uid, 0), now)
            lines.append(_roster_block(full, player.get("username", ""), stats))

        # Add GM stats if present
        for gm_id in gm_ids:
            gm_count = counts.get(gm_id, 0)
            raw_ts = topic_timestamps.get(gm_id, [])
            if gm_count > 0 and raw_ts:
                stats = _roster_user_stats(raw_ts, gm_count, now)
                lines.insert(0, _roster_block("GM", "", stats))

        if not lines:
            continue

        player_count = len(players)
        footer = f"\nParty size: {player_count}/{helpers.REQUIRED_PLAYERS}."
        if player_count < helpers.REQUIRED_PLAYERS:
            needed = helpers.REQUIRED_PLAYERS - player_count
            s = "s" if needed != 1 else ""
            footer += f"\n{name} needs {needed} more player{s}!"

        message = f"Party roster for {name}:\n\n" + "\n\n".join(lines) + footer

        print(f"Posting roster for {name}")
        if tg.send_message(group_id, chat_topic_id, message):
            state["last_roster"][pid] = now.isoformat()


# ------------------------------------------------------------------ #
#  Player of the Week (weekly, consistency-based)
# ------------------------------------------------------------------ #
def _gather_potw_candidates(
    topic_timestamps: dict, gm_ids: set, week_ago: datetime, pid: str, state: dict,
) -> list[dict]:
    """Find POTW candidates: players with enough posts, ranked by avg gap."""
    candidates = []
    for user_id, timestamps in topic_timestamps.items():
        if user_id in gm_ids:
            continue

        sessions = deduplicate_posts(timestamps_in_window(timestamps, week_ago))
        if len(sessions) < helpers.POTW_MIN_POSTS:
            continue

        sessions.sort()
        avg_gap = helpers.avg_gap_hours(sessions) or float("inf")

        player = helpers.get_player(state, pid, user_id)
        candidates.append({
            "user_id": user_id,
            "first_name": player.get("first_name", "Unknown"),
            "last_name": player.get("last_name", ""),
            "username": player.get("username", ""),
            "avg_gap_hours": avg_gap,
            "post_count": len(sessions),
        })
    return candidates


def player_of_the_week(config: dict, state: dict) -> None:
    """Award Player of the Week based on smallest average gap between posts."""
    group_id = config["group_id"]
    now = datetime.now(timezone.utc)
    gm_ids = helpers.gm_id_set(config)

    try:
        with open(helpers.BOONS_PATH) as f:
            boons = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Warning: Could not load boons: {e}")
        boons = ["Something mildly beneficial happens to you today."]

    maps = build_topic_maps(config)
    week_ago = now - timedelta(days=7)

    for pid, chat_topic_id in maps.to_chat.items():
        if not helpers.interval_elapsed(state["last_potw"].get(pid), helpers.POTW_INTERVAL_DAYS, now):
            continue

        name = maps.to_name.get(pid, "Unknown")
        topic_timestamps = helpers.get_topic_timestamps(state, pid)

        candidates = _gather_potw_candidates(topic_timestamps, gm_ids, week_ago, pid, state)
        if not candidates:
            print(f"No POTW candidates for {name} (need {helpers.POTW_MIN_POSTS}+ posts)")
            continue

        winner = min(candidates, key=lambda c: c["avg_gap_hours"])
        mention = helpers.player_mention(winner)
        avg_gap_str = f"{winner['avg_gap_hours']:.1f}h"

        # Pick 3 random flavour boons + 1 mechanical boon
        chosen_boons = random.sample(boons, min(3, len(boons)))
        chosen_boons.append(random.choice(helpers.MECHANICAL_BOONS))

        base_message = (
            f"Player of the Week for {name}: {mention}!\n"
            f"({fmt_date(week_ago)} to {fmt_date(now)})\n\n"
            f"{posts_str(winner['post_count'])} this week with an average "
            f"gap of {avg_gap_str} between posts. The most consistent "
            f"driver of the story."
        )

        boon_text = "\n\nChoose your boon:\n"
        for i, b in enumerate(chosen_boons):
            boon_text += f"\n{i + 1}. {b}\n"

        buttons = [
            {"text": f"Boon #{i + 1}", "callback_data": f"boon:{pid}:{i}"}
            for i in range(len(chosen_boons))
        ]

        print(f"POTW for {name}: {winner['first_name']} (avg gap {avg_gap_str})")
        msg_id = tg.send_message_with_buttons(group_id, chat_topic_id, base_message + boon_text, buttons)
        if msg_id:
            state["last_potw"][pid] = now.isoformat()
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
def check_combat_turns(config: dict, state: dict) -> None:
    """During players' phase, ping players who haven't acted yet."""
    group_id = config["group_id"]
    now = datetime.now(timezone.utc)

    # Build lookup: canonical pbp_topic_id -> chat_topic_id
    maps = build_topic_maps(config)
    all_campaigns = helpers.players_by_campaign(state)

    for pid, combat in list(state["combat"].items()):
        if not combat.get("active"):
            continue

        if combat["current_phase"] != "players":
            continue

        # Check if enough time has passed since phase started
        phase_start = datetime.fromisoformat(combat["phase_started_at"])
        hours_elapsed = helpers.hours_since(now, phase_start)

        if hours_elapsed < helpers.COMBAT_PING_HOURS:
            continue

        # Don't re-ping within helpers.COMBAT_PING_HOURS
        last_ping_str = combat.get("last_ping_at")
        if last_ping_str:
            since_ping = helpers.hours_since(now, datetime.fromisoformat(last_ping_str))
            if since_ping < helpers.COMBAT_PING_HOURS:
                continue

        # Find all known players in this campaign who haven't acted
        acted = set(combat.get("players_acted", []))
        missing = [
            helpers.player_mention(p)
            for p in all_campaigns.get(pid, [])
            if p["user_id"] not in acted
        ]

        if not missing:
            continue

        campaign_name = combat.get("campaign_name", "Unknown")
        round_num = combat.get("round", 1)
        hours_int = int(hours_elapsed)

        chat_topic_id = maps.to_chat.get(pid)
        if not chat_topic_id:
            continue

        missing_str = ", ".join(missing)
        phase_date = fmt_date(phase_start)
        message = (
            f"Round {round_num} - waiting on: {missing_str}\n"
            f"({hours_int}h since players' phase started on {phase_date})"
        )

        print(f"Combat ping in {campaign_name}: waiting on {missing_str}")
        if tg.send_message(group_id, chat_topic_id, message):
            combat["last_ping_at"] = now.isoformat()


# ------------------------------------------------------------------ #
#  Weekly data archive (preserves long-term trends)
# ------------------------------------------------------------------ #
def archive_weekly_data(config: dict, state: dict) -> None:
    """Archive weekly summaries to a JSON file in the repo.

    Stores compact per-campaign stats keyed by ISO week (e.g. '2026-W07').
    The file is committed back to the repo by the GitHub Actions workflow,
    giving full git history and no gist size concerns.
    """
    now = datetime.now(timezone.utc)
    gm_ids = helpers.gm_id_set(config)

    # Use last week's ISO week number (since current week is still in progress)
    last_week = now - timedelta(days=7)
    year, week_num, _ = last_week.isocalendar()
    week_key = f"{year}-W{week_num:02d}"

    # Check if we already archived this week (tracked in gist state)
    if state.get("last_archived_week") == week_key:
        return

    # Load existing archive from repo file
    helpers.ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(helpers.ARCHIVE_PATH) as f:
            archive = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        archive = {}

    week_start = now - timedelta(days=now.weekday() + 7)  # Start of last week (Monday)
    week_end = week_start + timedelta(days=7)

    maps = build_topic_maps(config)
    all_campaigns = helpers.players_by_campaign(state)

    for pid, name in maps.to_name.items():
        topic_timestamps = helpers.get_topic_timestamps(state, pid)

        gm_posts = 0
        player_posts = 0
        player_counts = {}
        player_post_times = []

        for uid, timestamps in topic_timestamps.items():
            is_gm = uid in gm_ids
            player_info = helpers.get_player(state, pid, uid)

            user_sessions = deduplicate_posts(
                timestamps_in_window(timestamps, week_start, week_end)
            )
            session_count = len(user_sessions)

            if is_gm:
                gm_posts += session_count
            else:
                player_posts += session_count
                player_post_times.extend(user_sessions)
                if session_count > 0:
                    p_name = helpers.player_mention(player_info)
                    player_counts[p_name] = player_counts.get(p_name, 0) + session_count

        # Calculate player avg gap
        raw_gap = helpers.avg_gap_hours(sorted(player_post_times))
        player_avg_gap = round(raw_gap, 1) if raw_gap is not None else None

        active_players = len(all_campaigns.get(pid, []))

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
    with open(helpers.ARCHIVE_PATH, "w") as f:
        json.dump(archive, f, indent=2)

    state["last_archived_week"] = week_key
    print(f"Archived weekly data for {week_key} to {helpers.ARCHIVE_PATH}")


# ------------------------------------------------------------------ #
#  Timestamp cleanup (keep only last 15 days)
# ------------------------------------------------------------------ #
def cleanup_timestamps(state: dict) -> None:
    """Prune old timestamps to prevent gist from growing."""
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
def post_pace_report(config: dict, state: dict) -> None:
    """Post weekly pace comparison: posts/day this week vs last week, split GM/players."""
    group_id = config["group_id"]
    now = datetime.now(timezone.utc)
    gm_ids = helpers.gm_id_set(config)

    maps = build_topic_maps(config)

    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    for pid, chat_topic_id in maps.to_chat.items():
        if not helpers.interval_elapsed(state["last_pace"].get(pid), helpers.PACE_INTERVAL_DAYS, now):
            continue

        name = maps.to_name.get(pid, "Unknown")
        topic_timestamps = helpers.get_topic_timestamps(state, pid)

        if not topic_timestamps:
            continue

        # Count posts split by GM vs players, this week vs last week
        gm_this = gm_last = player_this = player_last = 0
        for uid, timestamps in topic_timestamps.items():
            this_count = len(timestamps_in_window(timestamps, week_ago))
            last_count = len(timestamps_in_window(timestamps, two_weeks_ago, week_ago))
            if uid in gm_ids:
                gm_this += this_count
                gm_last += last_count
            else:
                player_this += this_count
                player_last += last_count

        this_week = gm_this + player_this
        last_week = gm_last + player_last
        this_avg = this_week / 7.0
        last_avg = last_week / 7.0

        # Determine trend
        if last_avg == 0 and this_avg == 0:
            continue  # No data
        icon = helpers.trend_icon(int(this_avg * 100), int(last_avg * 100))

        this_week_start = fmt_date(week_ago)
        this_week_end = fmt_date(now)
        last_week_start = fmt_date(two_weeks_ago)
        last_week_end = fmt_date(week_ago)

        message = (
            f"{icon} Weekly pace for {name}:\n"
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
            f"Trend: {icon}"
        )

        print(f"Pace report for {name}: {this_week} vs {last_week} ({icon})")
        if tg.send_message(group_id, chat_topic_id, message):
            state["last_pace"][pid] = now.isoformat()


# ------------------------------------------------------------------ #
#  Campaign anniversary alerts
# ------------------------------------------------------------------ #
def check_anniversaries(config: dict, state: dict) -> None:
    """Post a celebration when a campaign hits a yearly anniversary."""
    group_id = config["group_id"]
    now = datetime.now(timezone.utc)
    today = now.date()

    for pair in config["topic_pairs"]:
        pid = str(pair["pbp_topic_ids"][0])
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
        if tg.send_message(group_id, chat_topic_id, message):
            state["last_anniversary"][anniversary_key] = now.isoformat()


# ------------------------------------------------------------------ #
#  Campaign Leaderboard (cross-campaign dashboard)
# ------------------------------------------------------------------ #
def _gather_leaderboard_stats(config: dict, state: dict, now: datetime) -> tuple[list, dict]:
    """Collect per-campaign stats and global player rankings for the leaderboard."""
    gm_ids = helpers.gm_id_set(config)
    seven_days_ago = now - timedelta(days=7)
    three_days_ago = now - timedelta(days=3)
    six_days_ago = now - timedelta(days=6)

    campaign_stats = []
    global_player_posts = {}

    maps = build_topic_maps(config)

    for pid, name in maps.to_name.items():
        topic_timestamps = helpers.get_topic_timestamps(state, pid)

        gm_7d = 0
        player_7d = 0
        posts_recent_3d = 0
        posts_prev_3d = 0
        player_post_counts = {}
        all_post_times_7d = []
        player_post_times_7d = []

        for uid, timestamps in topic_timestamps.items():
            is_gm = uid in gm_ids
            player_info = helpers.get_player(state, pid, uid)

            user_7d_posts = timestamps_in_window(timestamps, seven_days_ago)
            posts_recent_3d += len(timestamps_in_window(timestamps, three_days_ago))
            posts_prev_3d += len(timestamps_in_window(timestamps, six_days_ago, three_days_ago))

            user_sessions = deduplicate_posts(user_7d_posts)
            session_count = len(user_sessions)

            all_post_times_7d.extend(user_sessions)
            if is_gm:
                gm_7d += session_count
            else:
                player_7d += session_count
                player_post_times_7d.extend(user_sessions)
                if session_count > 0:
                    full = helpers.player_full_name(player_info)
                    player_post_counts.setdefault(uid, {
                        "full_name": full,
                        "username": player_info.get("username", ""),
                        "count": 0,
                    })
                    player_post_counts[uid]["count"] += session_count

        total_7d = gm_7d + player_7d

        # Average response gap (all posts)
        all_post_times_7d.sort()
        all_avg = helpers.avg_gap_hours(all_post_times_7d)
        avg_gap_str = f"{all_avg:.1f}h" if all_avg is not None else "N/A"

        # Player-only average gap
        player_post_times_7d.sort()
        player_avg_gap = helpers.avg_gap_hours(player_post_times_7d)
        player_avg_gap_str = f"{player_avg_gap:.1f}h" if player_avg_gap is not None else "N/A"

        # Last post across all users
        all_ts = [ts for tss in topic_timestamps.values() for ts in tss]
        last_post_time = max((datetime.fromisoformat(ts) for ts in all_ts), default=None) if all_ts else None

        last_post_str, days_since_last = helpers.fmt_brief_relative(now, last_post_time)
        trend = helpers.trend_icon(posts_recent_3d, posts_prev_3d)

        top_players = sorted(
            player_post_counts.values(),
            key=lambda p: p["count"],
            reverse=True,
        )

        for uid, pdata in player_post_counts.items():
            entry = global_player_posts.setdefault(uid, {
                "full_name": pdata["full_name"],
                "username": pdata.get("username", ""),
                "count": 0,
                "campaigns": 0,
            })
            entry["count"] += pdata["count"]
            entry["campaigns"] += 1

        campaign_stats.append({
            "name": name,
            "total_7d": total_7d,
            "player_7d": player_7d,
            "gm_7d": gm_7d,
            "trend_icon": trend,
            "avg_gap_str": avg_gap_str,
            "player_avg_gap": player_avg_gap,
            "player_avg_gap_str": player_avg_gap_str,
            "last_post_str": last_post_str,
            "days_since_last": days_since_last,
            "top_players": top_players,
        })

    return campaign_stats, global_player_posts


def _format_leaderboard(campaign_stats: list, global_player_posts: dict, now: datetime) -> str:
    """Format the leaderboard message from collected stats."""
    seven_days_ago = now - timedelta(days=7)

    campaign_stats.sort(key=lambda c: c["player_7d"], reverse=True)
    active = [c for c in campaign_stats if c["total_7d"] > 0]
    dead = [c for c in campaign_stats if c["total_7d"] == 0]

    date_from = fmt_date(seven_days_ago)
    date_to = fmt_date(now)

    lines = [f"📊 Weekly Campaign Leaderboard ({date_from} to {date_to})"]

    for i, c in enumerate(active):
        rank = helpers.rank_icon(i)
        campaign_block = (
            f"[{rank} {c['name']} {c['trend_icon']}]\n"
            f"- {c['player_7d']} player posts.\n"
            f"- {posts_str(c['total_7d'])} total.\n"
            f"- {c['gm_7d']} GM posts.\n"
            f"- Avg gap: {c['avg_gap_str']}.\n"
            f"- Last post: {c['last_post_str']}."
        )

        player_blocks = []
        for j, p in enumerate(c["top_players"]):
            medal = helpers.rank_icon(j)
            block = f"{medal} {p['full_name']}\n"
            uname = p.get("username", "")
            if uname:
                block += f"- @{uname}\n"
            block += f"- {posts_str(p['count'])}"
            player_blocks.append(block)

        lines.append("\n━━━━━━━━━━━━━━━━\n\n" + campaign_block + "\n\n" + "\n".join(player_blocks))

    if dead:
        lines.append("\n⚠️ Dead campaigns (0 posts in 7 days):")
        for c in dead:
            lines.append(f"💀 [{c['name']}] (last post: {c['last_post_str']})")

    gap_ranked = [c for c in campaign_stats if c["player_avg_gap"] is not None]
    if gap_ranked:
        gap_ranked.sort(key=lambda c: c["player_avg_gap"])
        lines.append("\n━━━━━━━━━━━━━━━━\n\n⏱ Fastest player response gaps:")
        for i, c in enumerate(gap_ranked):
            lines.append(f"{helpers.rank_icon(i)} {c['name']}: {c['player_avg_gap_str']}")

    if global_player_posts:
        lines.append("\n━━━━━━━━━━━━━━━━")
        top_global = sorted(
            global_player_posts.items(),
            key=lambda x: x[1]["count"],
            reverse=True,
        )
        player_blocks = []
        for i, (uid, pdata) in enumerate(top_global):
            icon = helpers.rank_icon(i)
            campaign_word = "campaign" if pdata["campaigns"] == 1 else "campaigns"
            block = f"{icon} {pdata['full_name']}\n"
            if pdata["username"]:
                block += f"- @{pdata['username']}\n"
            block += f"- {posts_str(pdata['count'])} across {pdata['campaigns']} {campaign_word}"
            player_blocks.append(block)
        lines.append("\n⭐ Top Players of the Week:\n\n" + "\n\n".join(player_blocks))

    return "\n".join(lines)


def post_campaign_leaderboard(config: dict, state: dict) -> None:
    """Post a cross-campaign activity leaderboard to the ISSUES topic."""
    group_id = config["group_id"]
    leaderboard_topic = config.get("leaderboard_topic_id")
    if not leaderboard_topic:
        return

    now = datetime.now(timezone.utc)

    if not helpers.interval_elapsed(state.get("last_leaderboard"), helpers.LEADERBOARD_INTERVAL_DAYS, now):
        return

    campaign_stats, global_player_posts = _gather_leaderboard_stats(config, state, now)

    if not campaign_stats:
        print("No campaign data for leaderboard")
        return

    message = _format_leaderboard(campaign_stats, global_player_posts, now)

    print(f"Posting campaign leaderboard ({len(campaign_stats)} campaigns)")
    if tg.send_message(group_id, leaderboard_topic, message):
        state["last_leaderboard"] = now.isoformat()


# ------------------------------------------------------------------ #
#  Recruitment check (campaigns needing players)
# ------------------------------------------------------------------ #
def check_recruitment_needs(config: dict, state: dict) -> None:
    """If a campaign has fewer than helpers.REQUIRED_PLAYERS, post a notice."""
    group_id = config["group_id"]
    now = datetime.now(timezone.utc)

    maps = build_topic_maps(config)
    all_campaigns = helpers.players_by_campaign(state)

    for pid, chat_topic_id in maps.to_chat.items():
        name = maps.to_name[pid]

        # Check interval
        if not helpers.interval_elapsed(state["last_recruitment_check"].get(pid), helpers.RECRUITMENT_INTERVAL_DAYS, now):
            continue

        # Count active players (excluding GM)
        campaign_players = all_campaigns.get(pid, [])
        active = [
            helpers.player_mention(p)
            for p in campaign_players
        ]

        player_count = len(active)
        needed = helpers.REQUIRED_PLAYERS - player_count

        if needed <= 0:
            # Full roster, reset timer
            state["last_recruitment_check"][pid] = now.isoformat()
            continue

        # Build roster display
        if active:
            roster_lines = "\n".join(f"- {p}" for p in active)
            roster_section = f"Current roster ({player_count}/{helpers.REQUIRED_PLAYERS}):\n{roster_lines}"
        else:
            roster_section = f"Current roster: 0/{helpers.REQUIRED_PLAYERS} (no active players)"

        message = (
            f"📢 {name} needs {needed} more player{'s' if needed != 1 else ''}!\n\n"
            f"{roster_section}\n\n"
            f"Know anyone who'd like to join? Send them to the recruitment topic!"
        )

        print(f"Recruitment notice for {name}: {player_count}/{helpers.REQUIRED_PLAYERS}")
        if tg.send_message(group_id, chat_topic_id, message):
            state["last_recruitment_check"][pid] = now.isoformat()


# ------------------------------------------------------------------ #
#  Main
# ------------------------------------------------------------------ #
def _run_checks(config: dict, bot_state: dict) -> None:
    """Run all scheduled checks, isolating failures so one crash doesn't block others."""
    checks = [
        ("Topic alerts", check_and_alert),
        ("Player activity", check_player_activity),
        ("Roster summary", post_roster_summary),
        ("Player of the Week", player_of_the_week),
        ("Boon expiry", expire_pending_boons),
        ("Pace report", post_pace_report),
        ("Anniversaries", check_anniversaries),
        ("Combat pings", check_combat_turns),
        ("Leaderboard", post_campaign_leaderboard),
        ("Recruitment", check_recruitment_needs),
        ("Archive", archive_weekly_data),
    ]
    for label, func in checks:
        try:
            func(config, bot_state)
        except Exception as e:
            print(f"Error in {label}: {e}")


def main() -> None:
    """Entry point: load config/state, process updates, run all scheduled checks, save."""
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    gist_token = os.environ.get("GIST_TOKEN", "")
    gist_id = os.environ.get("GIST_ID", "")

    if not telegram_token:
        print("Error: TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)

    # Initialize modules
    tg.init(telegram_token)
    state_store.init(gist_token, gist_id)

    config = helpers.load_config()
    helpers.load_settings(config)
    bot_state = state_store.load()

    print(f"Loaded state. Offset: {bot_state.get('offset', 0)}")
    print(f"Tracking {len(bot_state.get('topics', {}))} topics, "
          f"{len(bot_state.get('players', {}))} players")

    # Fetch and process new messages
    offset = bot_state.get("offset", 0)
    updates = tg.get_updates(offset)
    print(f"Received {len(updates)} new updates")

    if updates:
        bot_state["offset"] = process_updates(updates, config, bot_state)

    # Run all scheduled checks (error-isolated)
    _run_checks(config, bot_state)

    # Prune old timestamps (lightweight, unlikely to fail)
    cleanup_timestamps(bot_state)

    # Always save state, even if checks failed
    state_store.save(bot_state)
    print("Done")


if __name__ == "__main__":
    main()
