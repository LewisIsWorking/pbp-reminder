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
from pathlib import Path

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


def expire_pending_boons(config: dict, state: dict, *, now: datetime | None = None, **_kw) -> None:
    """Auto-pick boon #1 if winner hasn't chosen within 48 hours."""
    now = now or datetime.now(timezone.utc)
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
_HELP_TEXT = (
    "PBP Reminder Bot\n"
    "\n"
    "I track activity across PBP campaigns and post automated summaries.\n"
    "\n"
    "What I do:\n"
    "- Alert when a campaign goes quiet (configurable hours)\n"
    "- Warn inactive players at 1, 2, 3 weeks; auto-remove at 4\n"
    "- Post party rosters every few days\n"
    "- Award Player of the Week (most consistent poster)\n"
    "- Post weekly pace reports comparing this week vs last\n"
    "- Cross-campaign leaderboard\n"
    "- Ping players who haven't acted during combat\n"
    "- Recruitment notices when a party is under capacity\n"
    "- Campaign anniversary celebrations\n"
    "- Daily tips about bot features (posted randomly across campaigns)\n"
    "\n"
    "GM commands:\n"
    "/round <N> players - Start round N, players' turn\n"
    "/round <N> enemies - Start round N, enemies' turn\n"
    "/endcombat - End combat tracking\n"
    "/pause [reason] - Pause inactivity tracking (planned breaks)\n"
    "/resume - Resume inactivity tracking\n"
    "\n"
    "Everyone:\n"
    "/help - Show this message\n"
    "/status - Campaign health snapshot\n"
    "/campaign - Full scoreboard with roster and stats\n"
    "/mystats - Your personal stats (also: /me)\n"
    "/myhistory - 8-week posting sparkline\n"
    "/whosturn - Who has acted in combat and who hasn't"
)


def _build_status(pid: str, campaign_name: str, state: dict, gm_ids: set) -> str:
    """Build a quick campaign health snapshot for /status command."""
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    # Player count
    players = [
        p for p in state.get("players", {}).values()
        if p.get("pbp_topic_id") == pid
    ]
    player_count = len(players)

    # Last post
    topic_state = state.get("topics", {}).get(pid)
    if topic_state:
        last_time = datetime.fromisoformat(topic_state["last_message_time"])
        elapsed = helpers.hours_since(now, last_time)
        if elapsed < 1:
            last_str = "just now"
        elif elapsed < 24:
            last_str = f"{int(elapsed)}h ago"
        else:
            last_str = f"{int(elapsed / 24)}d {int(elapsed % 24)}h ago"
    else:
        last_str = "no posts tracked yet"

    # Posts this week
    topic_ts = helpers.get_topic_timestamps(state, pid)
    gm_week = player_week = 0
    for uid, timestamps in topic_ts.items():
        count = len(timestamps_in_window(timestamps, week_ago))
        if uid in gm_ids:
            gm_week += count
        else:
            player_week += count

    # At-risk players (1+ weeks inactive)
    at_risk = []
    for p in players:
        last_post = datetime.fromisoformat(p["last_post_time"])
        days_inactive = helpers.days_since(now, last_post)
        if days_inactive >= 7:
            at_risk.append(f"{p['first_name']} ({int(days_inactive)}d)")

    # Active combat
    combat = state.get("combat", {}).get(pid)
    combat_str = ""
    if combat and combat.get("active"):
        combat_str = f"\nCombat: Round {combat['round']}, {combat['current_phase']}' turn"

    lines = [
        f"Status for {campaign_name}:",
        f"Party: {player_count}/{helpers.REQUIRED_PLAYERS}",
        f"Last post: {last_str}",
        f"This week: {player_week} player + {gm_week} GM posts",
    ]
    if at_risk:
        lines.append(f"At risk: {', '.join(at_risk)}")
    if combat_str:
        lines.append(combat_str)

    paused = state.get("paused_campaigns", {}).get(pid)
    if paused:
        lines.append(f"⏸️ PAUSED: {paused.get('reason', 'No reason')}")

    return "\n".join(lines)


def _build_campaign_report(pid: str, config: dict, state: dict, gm_ids: set) -> str:
    """Build a comprehensive campaign scoreboard for /campaign command.

    Combines: header, roster with full stats, weekly pace, at-risk players, combat state.
    """
    now = datetime.now(timezone.utc)

    # Campaign metadata
    pair = None
    for p in config.get("topic_pairs", []):
        if str(p["pbp_topic_ids"][0]) == pid:
            pair = p
            break
    name = pair["name"] if pair else "Unknown"
    created_str = pair.get("created", "") if pair else ""

    # Header
    lines = [f"━━ {name} ━━"]

    paused = state.get("paused_campaigns", {}).get(pid)
    if paused:
        lines.append(f"⏸️ PAUSED: {paused.get('reason', 'No reason')}")

    if created_str:
        created = datetime.strptime(created_str, "%Y-%m-%d").date()
        age_days = (now.date() - created).days
        if age_days >= 365:
            years = age_days // 365
            lines.append(f"Running since {created.strftime('%B %d, %Y')} ({years}y {age_days % 365}d)")
        else:
            lines.append(f"Running since {created.strftime('%B %d, %Y')} ({age_days}d)")

    # Players and counts
    players = [
        p_val for p_val in state.get("players", {}).values()
        if p_val.get("pbp_topic_id") == pid
    ]
    counts = state.get("message_counts", {}).get(pid, {})
    topic_ts = helpers.get_topic_timestamps(state, pid)
    player_count = len(players)

    lines.append(f"\nParty: {player_count}/{helpers.REQUIRED_PLAYERS}")
    if player_count < helpers.REQUIRED_PLAYERS:
        needed = helpers.REQUIRED_PLAYERS - player_count
        lines[-1] += f" (needs {needed} more)"

    # Weekly pace
    pace = helpers.pace_split(topic_ts, gm_ids, now)
    total_this = pace["gm_this"] + pace["player_this"]
    total_last = pace["gm_last"] + pace["player_last"]
    trend = helpers.trend_icon(total_this, total_last)

    lines.append(f"\n{trend} This week: {posts_str(total_this)} ({pace['player_this']} player, {pace['gm_this']} GM)")
    if total_last > 0:
        lines.append(f"Last week: {posts_str(total_last)} ({pace['player_last']} player, {pace['gm_last']} GM)")

    # Roster
    lines.append("\n━━ Roster ━━")
    sorted_players = sorted(players, key=lambda p: counts.get(p["user_id"], 0), reverse=True)

    # GM first
    for gm_id in gm_ids:
        gm_count = counts.get(gm_id, 0)
        raw_ts = topic_ts.get(gm_id, [])
        if gm_count > 0 and raw_ts:
            stats = _roster_user_stats(raw_ts, gm_count, now)
            lines.append("\n" + _roster_block("GM", "", stats))

    for player in sorted_players:
        uid = player["user_id"]
        raw_ts = topic_ts.get(uid, [])
        if not raw_ts:
            continue
        full = helpers.player_full_name(player)
        stats = _roster_user_stats(raw_ts, counts.get(uid, 0), now)
        lines.append("\n" + _roster_block(full, player.get("username", ""), stats))

    # At-risk players
    at_risk = []
    for p in players:
        last_post = datetime.fromisoformat(p["last_post_time"])
        inactive_days = helpers.days_since(now, last_post)
        if inactive_days >= 7:
            week_num = int(inactive_days / 7)
            at_risk.append(f"- {p['first_name']}: {int(inactive_days)}d inactive (warning {week_num}/3)")

    if at_risk:
        lines.append("\n⚠️ At Risk:")
        lines.extend(at_risk)

    # Active combat
    combat = state.get("combat", {}).get(pid)
    if combat and combat.get("active"):
        acted = set(combat.get("players_acted", []))
        missing = [p["first_name"] for p in players if p["user_id"] not in acted]
        lines.append(f"\n⚔️ Combat: Round {combat['round']}, {combat['current_phase']}' turn")
        if missing and combat["current_phase"] == "players":
            lines.append(f"Waiting on: {', '.join(missing)}")

    return "\n".join(lines)


def _build_mystats(pid: str, user_id: str, campaign_name: str,
                   state: dict, gm_ids: set) -> str:
    """Build personal stats for a player's /mystats command."""
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    is_gm = user_id in gm_ids
    role = "GM" if is_gm else "Player"

    # Get their data
    topic_ts = helpers.get_topic_timestamps(state, pid)
    raw_ts = topic_ts.get(user_id, [])
    total_count = state.get("message_counts", {}).get(pid, {}).get(user_id, 0)

    if not raw_ts:
        return f"No posts tracked yet for you in {campaign_name}. Post something and check back!"

    all_posts = sorted(datetime.fromisoformat(ts) for ts in raw_ts)
    sessions = deduplicate_posts(all_posts)
    week_posts = deduplicate_posts(timestamps_in_window(raw_ts, week_ago))
    avg_gap = calc_avg_gap_str(raw_ts)
    last_post_str = fmt_relative_date(now, all_posts[-1])

    # Calculate posting streak (consecutive days with posts)
    streak = _calc_streak(raw_ts, now)

    lines = [
        f"Your stats in {campaign_name} ({role}):",
        f"Total: {posts_str(total_count)} ({len(sessions)} sessions)",
        f"This week: {posts_str(len(week_posts))}",
        f"Avg gap: {avg_gap}",
        f"Last post: {last_post_str}",
    ]
    if streak > 1:
        lines.append(f"🔥 Streak: {streak} consecutive days")
    elif streak == 1:
        lines.append(f"Streak: 1 day (keep it going!)")

    return "\n".join(lines)


_SPARK_CHARS = " ▁▂▃▄▅▆▇█"


def _sparkline(values: list[int]) -> str:
    """Convert a list of integers into a text sparkline using block characters."""
    if not values or max(values) == 0:
        return "▁" * len(values)
    peak = max(values)
    return "".join(
        _SPARK_CHARS[min(round(v / peak * 8), 8)] for v in values
    )


def _build_myhistory(pid: str, user_id: str, campaign_name: str,
                     state: dict, gm_ids: set) -> str:
    """Build a posting history sparkline for the last 8 weeks."""
    now = datetime.now(timezone.utc)
    is_gm = user_id in gm_ids
    role = "GM" if is_gm else "Player"

    topic_ts = helpers.get_topic_timestamps(state, pid)
    raw_ts = topic_ts.get(user_id, [])

    if not raw_ts:
        return f"No posting history yet in {campaign_name}."

    # Calculate weekly post counts for last 8 weeks
    weeks = []
    for w in range(7, -1, -1):
        start = now - timedelta(weeks=w + 1)
        end = now - timedelta(weeks=w)
        count = len(timestamps_in_window(raw_ts, start, end))
        weeks.append(count)

    spark = _sparkline(weeks)
    total = sum(weeks)
    peak = max(weeks)
    current = weeks[-1]

    # Week labels
    label_start = fmt_date(now - timedelta(weeks=8))
    label_end = fmt_date(now)

    lines = [
        f"Posting history in {campaign_name} ({role}):",
        f"",
        f"{label_start}  {spark}  {label_end}",
        f"",
        f"8 weeks: {posts_str(total)} total",
        f"Peak week: {posts_str(peak)}",
        f"This week: {posts_str(current)}",
    ]

    # Trend
    if len(weeks) >= 2 and weeks[-2] > 0:
        trend = helpers.trend_icon(weeks[-1], weeks[-2])
        lines.append(f"Trend: {trend}")

    return "\n".join(lines)


def _calc_streak(raw_timestamps: list[str], now: datetime) -> int:
    """Count consecutive days with at least one post, ending at today or yesterday.

    Returns 0 if no recent posts, otherwise the number of consecutive days.
    """
    if not raw_timestamps:
        return 0

    # Get unique posting dates
    post_dates = sorted({datetime.fromisoformat(ts).date() for ts in raw_timestamps})
    today = now.date()

    # Streak must include today or yesterday
    if post_dates[-1] < today - timedelta(days=1):
        return 0

    # Count backward from the most recent post date
    streak = 1
    for i in range(len(post_dates) - 1, 0, -1):
        gap = (post_dates[i] - post_dates[i - 1]).days
        if gap == 1:
            streak += 1
        elif gap == 0:
            continue  # Same day, skip
        else:
            break

    return streak


def _build_whosturn(pid: str, campaign_name: str, state: dict) -> str:
    """Build combat status for /whosturn command."""
    combat = state.get("combat", {}).get(pid)

    if not combat or not combat.get("active"):
        return f"No active combat in {campaign_name}."

    round_num = combat.get("round", 1)
    phase = combat.get("current_phase", "unknown")
    phase_label = "Players" if phase == "players" else "Enemies"

    phase_start = datetime.fromisoformat(combat["phase_started_at"])
    elapsed = helpers.hours_since(datetime.now(timezone.utc), phase_start)

    lines = [
        f"⚔️ {campaign_name} — Round {round_num}, {phase_label}' turn",
        f"Phase started: {int(elapsed)}h ago",
    ]

    if phase == "players":
        acted = set(combat.get("players_acted", []))
        players = [
            p for p in state.get("players", {}).values()
            if p.get("pbp_topic_id") == pid
        ]
        acted_names = [p["first_name"] for p in players if p["user_id"] in acted]
        waiting_names = [p["first_name"] for p in players if p["user_id"] not in acted]

        if acted_names:
            lines.append(f"✅ Acted: {', '.join(sorted(acted_names))}")
        if waiting_names:
            lines.append(f"⏳ Waiting: {', '.join(sorted(waiting_names))}")
        else:
            lines.append("Everyone has acted!")
    else:
        lines.append("Waiting for GM to resolve enemy turns.")

    return "\n".join(lines)


# ------------------------------------------------------------------ #
#  Daily tips
# ------------------------------------------------------------------ #
_TIPS = [
    "💡 <b>/mystats</b> — Check your personal stats in any PBP topic. "
    "See your total posts, sessions, average gap, weekly activity, and current posting streak.",

    "💡 <b>/whosturn</b> — During combat, see who has acted and who the party is waiting on. "
    "Works for any player, not just the GM.",

    "💡 <b>/campaign</b> — Get a full scoreboard for the current campaign: "
    "party roster, weekly pace with trends, at-risk players, and combat state. All in one message.",

    "💡 <b>/status</b> — Quick health check: party size, last post time, "
    "posts this week, and any at-risk players. Faster than /campaign when you just need the headlines.",

    "💡 <b>/help</b> — Forgot a command? Type /help to see the full list of bot features and GM commands.",

    "💡 <b>Player of the Week</b> — Every week, the bot picks the most consistent poster "
    "(lowest average gap between posts, not just highest count). The winner picks a flavour boon!",

    "💡 <b>Inactivity warnings</b> — The bot notices if you go quiet. "
    "Week 1: friendly nudge. Week 2: concerned check-in. Week 3: urgent. Week 4: removed from roster. "
    "Just post to reset the timer!",

    "💡 <b>Combat tracking</b> — When the GM types <code>/round 1 players</code>, "
    "the bot tracks who has acted. Post anything during the players' phase and you're marked as done. "
    "If players go quiet, the bot pings those who haven't acted yet.",

    "💡 <b>GM commands</b> — GMs can use <code>/round N players</code> or "
    "<code>/round N enemies</code> to advance combat, and <code>/endcombat</code> to wrap it up.",

    "💡 <b>Roster reports</b> — Every few days the bot posts a roster showing everyone's "
    "post count, sessions, weekly activity, average gap, and last post time. "
    "It's the campaign's health dashboard.",

    "💡 <b>Pace reports</b> — Weekly comparison of this week vs last week: "
    "total posts, GM vs player split, posts per day, and trend arrows. "
    "See if your campaign is speeding up or slowing down.",

    "💡 <b>Posting streaks</b> — Post on consecutive days to build a streak. "
    "Check yours with /mystats. The longer the streak, the bigger the 🔥!",

    "💡 <b>/myhistory</b> — See a visual sparkline of your posting activity over the last 8 weeks. "
    "Track your peak weeks and whether you're trending up or down.",

    "💡 <b>/pause</b> and <b>/resume</b> (GM only) — Going on holiday or taking a break between arcs? "
    "Type <code>/pause on holiday</code> to stop inactivity warnings. <code>/resume</code> to restart.",
]


def post_daily_tip(config: dict, state: dict, *, now: datetime | None = None, **_kw) -> None:
    """Post a random tip to a randomly chosen PBP chat topic once per day."""
    group_id = config["group_id"]
    now = now or datetime.now(timezone.utc)

    # Check daily interval
    last_tip_str = state.get("last_daily_tip")
    if last_tip_str:
        last_tip = datetime.fromisoformat(last_tip_str)
        if helpers.hours_since(now, last_tip) < 22:
            return

    # Collect all chat topic IDs
    chat_topics = []
    for pair in config.get("topic_pairs", []):
        pid = str(pair["pbp_topic_ids"][0])
        if helpers.feature_enabled(config, pid, "alerts"):
            chat_topics.append(pair["chat_topic_id"])

    if not chat_topics:
        return

    # Pick a tip we haven't used recently
    used_tips = state.get("used_tip_indices", [])
    available = [i for i in range(len(_TIPS)) if i not in used_tips]
    if not available:
        # Reset cycle
        available = list(range(len(_TIPS)))
        used_tips = []

    tip_idx = random.choice(available)
    topic_id = random.choice(chat_topics)

    print(f"Daily tip #{tip_idx} to topic {topic_id}")
    if tg.send_message(group_id, topic_id, _TIPS[tip_idx], parse_mode="HTML"):
        state["last_daily_tip"] = now.isoformat()
        used_tips.append(tip_idx)
        state["used_tip_indices"] = used_tips


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


def _parse_message(msg: dict, group_id: int, maps) -> dict | None:
    """Validate and extract fields from a Telegram message. Returns None if skipped."""
    chat_id = msg.get("chat", {}).get("id")
    if chat_id != group_id:
        return None

    thread_id = msg.get("message_thread_id")
    if thread_id is None:
        return None

    thread_id_str = str(thread_id)
    if thread_id_str not in maps.all_pbp_ids:
        return None

    from_user = msg.get("from", {})
    if from_user.get("is_bot", False):
        return None

    now_iso = datetime.now(timezone.utc).isoformat()
    msg_date = msg.get("date")
    msg_time_iso = datetime.fromtimestamp(msg_date, tz=timezone.utc).isoformat() if msg_date else now_iso

    raw_text = msg.get("text", "").strip()

    # Detect media type for logging
    media_type = None
    if msg.get("photo"):
        media_type = "image"
    elif msg.get("sticker"):
        sticker = msg["sticker"]
        media_type = f"sticker:{sticker.get('emoji', '?')}"
    elif msg.get("animation"):
        media_type = "gif"
    elif msg.get("video"):
        media_type = "video"
    elif msg.get("voice"):
        media_type = "voice message"
    elif msg.get("video_note"):
        media_type = "video note"
    elif msg.get("document"):
        doc_name = msg["document"].get("file_name", "file")
        media_type = f"document:{doc_name}"

    # Caption on media messages
    caption = msg.get("caption", "").strip()

    return {
        "thread_id": thread_id,
        "pid": maps.to_canonical[thread_id_str],
        "campaign_name": maps.to_name[maps.to_canonical[thread_id_str]],
        "user_id": str(from_user.get("id", "")),
        "user_name": from_user.get("first_name", "Someone"),
        "user_last_name": from_user.get("last_name", ""),
        "username": from_user.get("username", ""),
        "now_iso": now_iso,
        "msg_time_iso": msg_time_iso,
        "text": raw_text.lower() if raw_text else (caption.lower() if caption else ""),
        "raw_text": raw_text,
        "media_type": media_type,
        "caption": caption,
    }


# ------------------------------------------------------------------ #
#  PBP transcript logger (persistent campaign archive)
# ------------------------------------------------------------------ #
_LOGS_DIR = Path(__file__).parent.parent / "data" / "pbp_logs"


def _sanitize_dirname(name: str) -> str:
    """Convert a campaign name to a safe directory name."""
    return "".join(c if c.isalnum() or c in (" ", "-", "_") else "" for c in name).strip().replace(" ", "_")


def _format_log_entry(parsed: dict, gm_ids: set) -> str:
    """Format a single message as a markdown log line."""
    ts = parsed["msg_time_iso"][:19].replace("T", " ")  # 2026-02-26 14:30:05
    name = parsed["user_name"]
    last = parsed.get("user_last_name", "")
    if last:
        name = f"{name} {last}"

    is_gm = parsed["user_id"] in gm_ids
    role_tag = " [GM]" if is_gm else ""

    raw = parsed.get("raw_text", "")
    media = parsed.get("media_type")
    caption = parsed.get("caption", "")

    # Build content
    parts = []
    if media:
        if media.startswith("sticker:"):
            parts.append(f"*[sticker {media[8:]}]*")
        elif media.startswith("document:"):
            parts.append(f"*[{media[9:]}]*")
        else:
            parts.append(f"*[{media}]*")
    if raw:
        parts.append(raw)
    elif caption:
        parts.append(caption)

    content = " ".join(parts) if parts else "*[empty message]*"

    return f"**{name}**{role_tag} ({ts}):\n{content}\n"


def _append_to_transcript(parsed: dict, gm_ids: set) -> None:
    """Append a message to the campaign's monthly transcript file.

    Files: data/pbp_logs/{CampaignName}/{YYYY-MM}.md
    Each file has a header on first creation, then entries appended.
    """
    campaign_name = parsed["campaign_name"]
    dir_name = _sanitize_dirname(campaign_name)
    campaign_dir = _LOGS_DIR / dir_name
    campaign_dir.mkdir(parents=True, exist_ok=True)

    # Month file from message timestamp
    msg_date = parsed["msg_time_iso"][:10]  # YYYY-MM-DD
    month_str = msg_date[:7]  # YYYY-MM
    log_file = campaign_dir / f"{month_str}.md"

    # Create header on first write
    is_new = not log_file.exists()

    with open(log_file, "a", encoding="utf-8") as f:
        if is_new:
            f.write(f"# {campaign_name} — {month_str}\n\n")
            f.write("*PBP transcript archived by PathWarsNudge bot.*\n\n---\n\n")

        entry = _format_log_entry(parsed, gm_ids)
        f.write(entry + "\n")


def update_transcript_index(config: dict) -> None:
    """Generate data/pbp_logs/README.md listing all campaigns and their log files."""
    if not _LOGS_DIR.exists():
        return

    lines = [
        "# PBP Transcripts",
        "",
        "Persistent archive of all play-by-post messages.",
        "Auto-generated by PathWarsNudge bot every hour.",
        "",
        "---",
        "",
    ]

    # Get campaign name mapping
    name_map = {}
    for pair in config.get("topic_pairs", []):
        dir_name = _sanitize_dirname(pair["name"])
        name_map[dir_name] = pair["name"]

    campaign_dirs = sorted(d for d in _LOGS_DIR.iterdir() if d.is_dir())

    for campaign_dir in campaign_dirs:
        display_name = name_map.get(campaign_dir.name, campaign_dir.name)
        log_files = sorted(campaign_dir.glob("*.md"), reverse=True)

        if not log_files:
            continue

        # Count total lines (rough message count)
        total_entries = 0
        for lf in log_files:
            # Each entry starts with ** (bold name)
            total_entries += sum(1 for line in open(lf) if line.startswith("**"))

        lines.append(f"## {display_name}")
        lines.append(f"")
        lines.append(f"*{total_entries} messages across {len(log_files)} months*")
        lines.append(f"")

        for lf in log_files:
            entries = sum(1 for line in open(lf) if line.startswith("**"))
            lines.append(f"- [{lf.stem}]({campaign_dir.name}/{lf.name}) ({entries} messages)")

        lines.append("")

    index_path = _LOGS_DIR / "README.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")


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

        parsed = _parse_message(msg, group_id, maps)
        if not parsed:
            continue

        pid = parsed["pid"]
        thread_id = parsed["thread_id"]
        user_id = parsed["user_id"]
        user_name = parsed["user_name"]
        campaign_name = parsed["campaign_name"]
        now_iso = parsed["now_iso"]
        msg_time_iso = parsed["msg_time_iso"]
        text = parsed["text"]

        # ---- /help command ----
        if text in ("/help", "/pbphelp"):
            tg.send_message(group_id, thread_id, _HELP_TEXT)

        # ---- /status command ----
        if text == "/status":
            status = _build_status(pid, campaign_name, state, gm_ids)
            tg.send_message(group_id, thread_id, status)

        # ---- /campaign command ----
        if text == "/campaign":
            report = _build_campaign_report(pid, config, state, gm_ids)
            tg.send_message(group_id, thread_id, report)

        # ---- /mystats command ----
        if text in ("/mystats", "/me"):
            my_report = _build_mystats(pid, user_id, campaign_name, state, gm_ids)
            tg.send_message(group_id, thread_id, my_report)

        # ---- /whosturn command ----
        if text == "/whosturn":
            turn_report = _build_whosturn(pid, campaign_name, state)
            tg.send_message(group_id, thread_id, turn_report)

        # ---- /myhistory command ----
        if text == "/myhistory":
            history = _build_myhistory(pid, user_id, campaign_name, state, gm_ids)
            tg.send_message(group_id, thread_id, history)

        # ---- /pause command (GM only) ----
        if text.startswith("/pause") and user_id in gm_ids:
            reason = parsed["raw_text"][6:].strip() or "No reason given"
            state.setdefault("paused_campaigns", {})[pid] = {
                "paused_at": now_iso,
                "reason": reason,
            }
            tg.send_message(group_id, thread_id,
                            f"⏸️ {campaign_name} paused. Inactivity tracking disabled.\nReason: {reason}")
            print(f"Paused {campaign_name}: {reason}")

        # ---- /resume command (GM only) ----
        if text == "/resume" and user_id in gm_ids:
            paused = state.get("paused_campaigns", {})
            if pid in paused:
                del paused[pid]
                tg.send_message(group_id, thread_id,
                                f"▶️ {campaign_name} resumed. Inactivity tracking re-enabled.")
                print(f"Resumed {campaign_name}")
            else:
                tg.send_message(group_id, thread_id, f"{campaign_name} is not paused.")

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
                "last_name": parsed["user_last_name"],
                "username": parsed["username"],
                "campaign_name": campaign_name,
                "pbp_topic_id": pid,
                "last_post_time": msg_time_iso,
                "last_warned_week": 0,
            }

            if was_removed:
                del state["removed_players"][player_key]
                print(f"Player {user_name} rejoined {campaign_name}")

        # Log to persistent PBP transcript
        if not text.startswith("/"):
            _append_to_transcript(parsed, gm_ids)

        print(f"Tracked message in {campaign_name} from {user_name}")

    return new_offset


# ------------------------------------------------------------------ #
#  Topic inactivity alerts (4-hour)
# ------------------------------------------------------------------ #
def check_and_alert(config: dict, state: dict, *, now: datetime | None = None, maps=None) -> None:
    """Send alerts to campaigns inactive beyond alert_after_hours."""
    group_id = config["group_id"]
    alert_hours = config.get("alert_after_hours", 4)
    now = now or datetime.now(timezone.utc)

    maps = maps or build_topic_maps(config)

    for pid, chat_topic_id in maps.to_chat.items():
        name = maps.to_name[pid]

        if not helpers.feature_enabled(config, pid, "alerts"):
            continue

        if pid in state.get("paused_campaigns", {}):
            continue

        if pid not in state.get("topics", {}):
            continue
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


def check_player_activity(config: dict, state: dict, *, now: datetime | None = None, maps=None) -> None:
    """Warn inactive players at 1/2/3 weeks, remove at 4 weeks."""
    group_id = config["group_id"]
    now = now or datetime.now(timezone.utc)

    # Build lookup: canonical pbp_topic_id -> chat_topic_id
    maps = maps or build_topic_maps(config)

    players_to_remove = []

    for player_key, player in state["players"].items():
        pbp_topic_id = player["pbp_topic_id"]
        chat_topic_id = maps.to_chat.get(pbp_topic_id)
        if not chat_topic_id:
            continue
        if not helpers.feature_enabled(config, pbp_topic_id, "warnings"):
            continue
        if pbp_topic_id in state.get("paused_campaigns", {}):
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

    Returns dict with: total, sessions, week_count, avg_gap_str, last_post_str, streak.
    """
    week_ago = now - timedelta(days=7)
    all_posts = sorted(datetime.fromisoformat(ts) for ts in raw_timestamps)
    sessions = deduplicate_posts(all_posts)
    week_count = len(deduplicate_posts(timestamps_in_window(raw_timestamps, week_ago)))
    avg_gap_str = calc_avg_gap_str(raw_timestamps)
    last_post_str = fmt_relative_date(now, all_posts[-1]) if all_posts else "N/A"
    streak = _calc_streak(raw_timestamps, now)
    return {
        "total": total_count,
        "sessions": len(sessions),
        "week_count": week_count,
        "avg_gap_str": avg_gap_str,
        "last_post_str": last_post_str,
        "streak": streak,
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
    streak = stats.get("streak", 0)
    if streak >= 2:
        block += f"\n- 🔥 {streak}-day streak!"
    return block


def post_roster_summary(config: dict, state: dict, *, now: datetime | None = None, maps=None) -> None:
    """Post a summary of all tracked players per campaign to CHAT topics."""
    group_id = config["group_id"]
    now = now or datetime.now(timezone.utc)

    maps = maps or build_topic_maps(config)
    campaigns = helpers.players_by_campaign(state)
    gm_ids = helpers.gm_id_set(config)

    for pid, chat_topic_id in maps.to_chat.items():
        if not helpers.feature_enabled(config, pid, "roster"):
            continue
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


def player_of_the_week(config: dict, state: dict, *, now: datetime | None = None, maps=None) -> None:
    """Award Player of the Week based on smallest average gap between posts."""
    group_id = config["group_id"]
    now = now or datetime.now(timezone.utc)
    gm_ids = helpers.gm_id_set(config)

    try:
        with open(helpers.BOONS_PATH) as f:
            boons = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Warning: Could not load boons: {e}")
        boons = ["Something mildly beneficial happens to you today."]

    maps = maps or build_topic_maps(config)
    week_ago = now - timedelta(days=7)

    for pid, chat_topic_id in maps.to_chat.items():
        if not helpers.feature_enabled(config, pid, "potw"):
            continue
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
def check_combat_turns(config: dict, state: dict, *, now: datetime | None = None, maps=None) -> None:
    """During players' phase, ping players who haven't acted yet."""
    group_id = config["group_id"]
    now = now or datetime.now(timezone.utc)

    # Build lookup: canonical pbp_topic_id -> chat_topic_id
    maps = maps or build_topic_maps(config)
    all_campaigns = helpers.players_by_campaign(state)

    for pid, combat in list(state["combat"].items()):
        if not combat.get("active"):
            continue

        if not helpers.feature_enabled(config, pid, "combat"):
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
def archive_weekly_data(config: dict, state: dict, *, now: datetime | None = None, maps=None) -> None:
    """Archive weekly summaries to a JSON file in the repo.

    Stores compact per-campaign stats keyed by ISO week (e.g. '2026-W07').
    The file is committed back to the repo by the GitHub Actions workflow,
    giving full git history and no gist size concerns.
    """
    now = now or datetime.now(timezone.utc)
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
def post_pace_report(config: dict, state: dict, *, now: datetime | None = None, maps=None) -> None:
    """Post weekly pace comparison: posts/day this week vs last week, split GM/players."""
    group_id = config["group_id"]
    now = now or datetime.now(timezone.utc)
    gm_ids = helpers.gm_id_set(config)

    maps = maps or build_topic_maps(config)

    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    for pid, chat_topic_id in maps.to_chat.items():
        if not helpers.feature_enabled(config, pid, "pace"):
            continue
        if not helpers.interval_elapsed(state["last_pace"].get(pid), helpers.PACE_INTERVAL_DAYS, now):
            continue

        name = maps.to_name.get(pid, "Unknown")
        topic_timestamps = helpers.get_topic_timestamps(state, pid)

        if not topic_timestamps:
            continue

        pace = helpers.pace_split(topic_timestamps, gm_ids, now)
        gm_this = pace["gm_this"]
        gm_last = pace["gm_last"]
        player_this = pace["player_this"]
        player_last = pace["player_last"]

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
#  Streak milestone celebrations
# ------------------------------------------------------------------ #
_STREAK_MILESTONES = [7, 14, 30, 60, 90]

_STREAK_MESSAGES = {
    7: "🔥 {name} is on a 7-day posting streak in {campaign}! One full week of consistency.",
    14: "🔥🔥 {name} has hit a 14-day streak in {campaign}! Two solid weeks.",
    30: "🔥🔥🔥 {name} has reached a 30-day streak in {campaign}! A full month of daily posts. Legendary.",
    60: "🌟 {name} has been posting daily for 60 days straight in {campaign}. Absolute dedication.",
    90: "👑 {name} has hit 90 days in {campaign}. Three months without missing a day. Unbelievable.",
}


def check_streak_milestones(config: dict, state: dict, *, now: datetime | None = None, maps=None, **_kw) -> None:
    """Celebrate when a player crosses a streak milestone (7, 14, 30, 60, 90 days)."""
    group_id = config["group_id"]
    now = now or datetime.now(timezone.utc)
    gm_ids = helpers.gm_id_set(config)

    maps = maps or build_topic_maps(config)
    celebrated = state.setdefault("celebrated_streaks", {})

    for pid, chat_topic_id in maps.to_chat.items():
        name = maps.to_name.get(pid, "Unknown")
        topic_ts = helpers.get_topic_timestamps(state, pid)

        for uid, raw_ts in topic_ts.items():
            if uid in gm_ids:
                continue

            streak = _calc_streak(raw_ts, now)
            if streak < _STREAK_MILESTONES[0]:
                continue

            # Find the highest milestone crossed
            milestone = 0
            for m in _STREAK_MILESTONES:
                if streak >= m:
                    milestone = m

            key = f"{pid}:{uid}"
            last_celebrated = celebrated.get(key, 0)

            if milestone <= last_celebrated:
                continue

            player = helpers.get_player(state, pid, uid)
            player_name = player.get("first_name", "Someone") if player else "Someone"

            message = _STREAK_MESSAGES.get(milestone, "🔥 {name} is on a {streak}-day streak in {campaign}!")
            message = message.format(name=player_name, campaign=name, streak=streak)

            print(f"Streak milestone: {player_name} hit {milestone}d in {name}")
            if tg.send_message(group_id, chat_topic_id, message):
                celebrated[key] = milestone


# ------------------------------------------------------------------ #
#  Campaign anniversary alerts
# ------------------------------------------------------------------ #
def check_anniversaries(config: dict, state: dict, *, now: datetime | None = None, **_kw) -> None:
    """Post a celebration when a campaign hits a yearly anniversary."""
    group_id = config["group_id"]
    now = now or datetime.now(timezone.utc)
    today = now.date()

    for pair in config["topic_pairs"]:
        pid = str(pair["pbp_topic_ids"][0])
        chat_topic_id = pair["chat_topic_id"]
        name = pair["name"]

        if not helpers.feature_enabled(config, pid, "anniversary"):
            continue

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
def _gather_leaderboard_stats(config: dict, state: dict, now: datetime) -> tuple[list, dict, list]:
    """Collect per-campaign stats, global player rankings, and top streaks for the leaderboard."""
    gm_ids = helpers.gm_id_set(config)
    seven_days_ago = now - timedelta(days=7)
    three_days_ago = now - timedelta(days=3)
    six_days_ago = now - timedelta(days=6)

    campaign_stats = []
    global_player_posts = {}
    all_streaks = []

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

            # Collect streak data (players only)
            if not is_gm:
                streak = _calc_streak(timestamps, now)
                if streak >= 2 and player_info:
                    all_streaks.append({
                        "name": helpers.player_full_name(player_info),
                        "streak": streak,
                        "campaign": name,
                    })

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

    return campaign_stats, global_player_posts, all_streaks


def _format_leaderboard(campaign_stats: list, global_player_posts: dict,
                        now: datetime, streaks: list | None = None) -> str:
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

    # Streak leaderboard
    if streaks:
        top_streaks = sorted(streaks, key=lambda s: s["streak"], reverse=True)[:5]
        streak_lines = []
        for i, s in enumerate(top_streaks):
            icon = helpers.rank_icon(i)
            streak_lines.append(f"{icon} {s['name']} — {s['streak']}d streak ({s['campaign']})")
        lines.append("\n━━━━━━━━━━━━━━━━\n\n🔥 Longest Active Streaks:\n\n" + "\n".join(streak_lines))

    return "\n".join(lines)


def post_campaign_leaderboard(config: dict, state: dict, *, now: datetime | None = None, maps=None) -> None:
    """Post a cross-campaign activity leaderboard to the ISSUES topic."""
    group_id = config["group_id"]
    leaderboard_topic = config.get("leaderboard_topic_id")
    if not leaderboard_topic:
        return

    now = now or datetime.now(timezone.utc)

    if not helpers.interval_elapsed(state.get("last_leaderboard"), helpers.LEADERBOARD_INTERVAL_DAYS, now):
        return

    campaign_stats, global_player_posts, all_streaks = _gather_leaderboard_stats(config, state, now)

    if not campaign_stats:
        print("No campaign data for leaderboard")
        return

    message = _format_leaderboard(campaign_stats, global_player_posts, now, all_streaks)

    print(f"Posting campaign leaderboard ({len(campaign_stats)} campaigns)")
    if tg.send_message(group_id, leaderboard_topic, message):
        state["last_leaderboard"] = now.isoformat()


# ------------------------------------------------------------------ #
#  Recruitment check (campaigns needing players)
# ------------------------------------------------------------------ #
def check_recruitment_needs(config: dict, state: dict, *, now: datetime | None = None, maps=None) -> None:
    """If a campaign has fewer than helpers.REQUIRED_PLAYERS, post a notice."""
    group_id = config["group_id"]
    now = now or datetime.now(timezone.utc)

    maps = maps or build_topic_maps(config)
    all_campaigns = helpers.players_by_campaign(state)

    for pid, chat_topic_id in maps.to_chat.items():
        name = maps.to_name[pid]

        if not helpers.feature_enabled(config, pid, "recruitment"):
            continue

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
#  Weekly digest (compact cross-campaign newsletter)
# ------------------------------------------------------------------ #
_HEALTH_THRESHOLDS = [(20, "🟢"), (10, "🟡"), (5, "🟠"), (0, "🔴")]


def _health_icon(total_posts_7d: int) -> str:
    """Return a traffic-light icon based on weekly post volume."""
    for threshold, icon in _HEALTH_THRESHOLDS:
        if total_posts_7d >= threshold:
            return icon
    return "🔴"


def _build_weekly_digest(config: dict, state: dict, now: datetime) -> str:
    """Build a compact one-line-per-campaign weekly digest."""
    gm_ids = helpers.gm_id_set(config)
    maps = build_topic_maps(config)
    week_ago = now - timedelta(days=7)

    campaign_lines = []
    all_campaigns = helpers.players_by_campaign(state)

    for pid, name in maps.to_name.items():
        topic_ts = helpers.get_topic_timestamps(state, pid)
        pace = helpers.pace_split(topic_ts, gm_ids, now)
        total = pace["gm_this"] + pace["player_this"]
        total_last = pace["gm_last"] + pace["player_last"]
        trend = helpers.trend_icon(total, total_last)
        health = _health_icon(total)

        # Top contributor this week
        player_week_counts = {}
        for uid, timestamps in topic_ts.items():
            if uid in gm_ids:
                continue
            count = len(timestamps_in_window(timestamps, week_ago))
            if count > 0:
                player = helpers.get_player(state, pid, uid)
                name_str = player.get("first_name", "?") if player else "?"
                player_week_counts[name_str] = count

        top_name = ""
        if player_week_counts:
            top_name = max(player_week_counts, key=player_week_counts.get)

        # Party size
        players = all_campaigns.get(pid, [])
        party = f"{len(players)}/{helpers.REQUIRED_PLAYERS}"

        # Combat?
        combat = state.get("combat", {}).get(pid, {})
        combat_str = " ⚔️" if combat.get("active") else ""

        line = f"{health} {name}: {posts_str(total)} {trend} ({party}){combat_str}"
        if top_name:
            line += f" — MVP: {top_name}"

        campaign_lines.append((total, line))

    # Sort by post count descending
    campaign_lines.sort(key=lambda x: x[0], reverse=True)

    date_str = fmt_date(now)
    header = f"📰 Weekly Digest — {date_str}"
    body = "\n".join(line for _, line in campaign_lines)

    legend = "\n\n🟢 20+ posts | 🟡 10-19 | 🟠 5-9 | 🔴 <5"

    return f"{header}\n\n{body}{legend}"


def post_weekly_digest(config: dict, state: dict, *, now: datetime | None = None, **_kw) -> None:
    """Post a compact weekly digest to the leaderboard topic."""
    group_id = config["group_id"]
    leaderboard_topic = config.get("leaderboard_topic_id")
    if not leaderboard_topic:
        return

    now = now or datetime.now(timezone.utc)

    # Weekly interval (separate from leaderboard)
    if not helpers.interval_elapsed(state.get("last_weekly_digest"), 7, now):
        return

    message = _build_weekly_digest(config, state, now)

    print(f"Posting weekly digest")
    if tg.send_message(group_id, leaderboard_topic, message):
        state["last_weekly_digest"] = now.isoformat()


# ------------------------------------------------------------------ #
#  Main
# ------------------------------------------------------------------ #
def _run_checks(config: dict, bot_state: dict) -> None:
    """Run all scheduled checks, isolating failures so one crash doesn't block others."""
    now = datetime.now(timezone.utc)
    maps = build_topic_maps(config)

    checks = [
        ("Topic alerts", check_and_alert),
        ("Player activity", check_player_activity),
        ("Roster summary", post_roster_summary),
        ("Player of the Week", player_of_the_week),
        ("Boon expiry", expire_pending_boons),
        ("Pace report", post_pace_report),
        ("Streak milestones", check_streak_milestones),
        ("Anniversaries", check_anniversaries),
        ("Combat pings", check_combat_turns),
        ("Leaderboard", post_campaign_leaderboard),
        ("Weekly digest", post_weekly_digest),
        ("Recruitment", check_recruitment_needs),
        ("Archive", archive_weekly_data),
        ("Daily tip", post_daily_tip),
    ]
    for label, func in checks:
        try:
            func(config, bot_state, now=now, maps=maps)
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

    issues = helpers.validate_config(config)
    for issue in issues:
        print(issue)
    if any(i.startswith("ERROR:") for i in issues):
        print("Fatal config errors found, aborting")
        sys.exit(1)

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

    # Regenerate transcript index if logs exist
    try:
        update_transcript_index(config)
    except Exception as e:
        print(f"Error updating transcript index: {e}")

    # Always save state, even if checks failed
    state_store.save(bot_state)
    print("Done")


if __name__ == "__main__":
    main()
