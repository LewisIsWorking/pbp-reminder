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
    "/combat [enemies] - Start combat (e.g. /combat Ogre, 2 Skeletons)\n"
    "/round <N> <players|enemies> - Set specific round and phase\n"
    "/next - Advance to next phase (players→enemies→next round)\n"
    "/endcombat - End combat (shows log summary)\n"
    "/enemies [list] - View or set enemy roster\n"
    "/clog <event> - Add combat log entry\n"
    "/pause [reason] - Pause inactivity tracking (planned breaks)\n"
    "/resume - Resume inactivity tracking\n"
    "/kick @player - Remove a player from tracking\n"
    "/addplayer @user Name - Add a player to roster before they post\n"
    "/scene <name> - Mark a scene boundary in the transcript\n"
    "/note <text> - Add a persistent GM note to this campaign\n"
    "/delnote <N> - Delete a GM note by number\n"
    "/quest <text> - Add an active quest/objective\n"
    "/done <N> - Mark quest N as completed\n"
    "/delquest <N> - Delete quest N\n"
    "/pin <text> - Bookmark a story moment or key info\n"
    "/delpin <N> - Delete a pin\n"
    "/loot <text> - Add item to party loot tracker\n"
    "/delloot <N> - Remove item from loot\n"
    "/npc <name> — <desc> - Add an NPC to the tracker\n"
    "/delnpc <N> - Remove an NPC\n"
    "/condition <target> — <effect> [| duration] - Track a condition\n"
    "/endcondition <N> - Remove a condition\n"
    "/clearconditions - Clear all conditions\n"
    "/hp set <n> <cur>/<max> - Track enemy HP\n"
    "/hp d <n> <amount> - Deal damage\n"
    "/hp h <n> <amount> - Heal\n"
    "/hp remove <n> - Remove an entry\n"
    "/hp clear - Clear all HP entries\n"
    "/clock <n> <segments> - Create a progress clock\n"
    "/tick <n> [N] - Advance a clock\n"
    "/untick <n> [N] - Reverse a clock\n"
    "/delclock <n> - Delete a clock\n"
    "/vote <q> | <opt1> | <opt2> [| ...] - Start a vote\n"
    "/endvote - Close voting and show results\n"
    "/timer <duration> [reason] - Set a response deadline\n"
    "/canceltimer - Cancel the active timer\n"
    "/gm - GM dashboard: all campaigns at a glance\n"
    "\n"
    "Everyone:\n"
    "/help - Show this message\n"
    "/status - Campaign health snapshot\n"
    "/overview - All campaigns at a glance\n"
    "/campaign - Full scoreboard with roster and stats\n"
    "/mystats - Your personal stats (also: /me)\n"
    "/myhistory - 8-week posting sparkline\n"
    "/whosturn - Who has acted in combat and who hasn't\n"
    "/combatlog - View combat log entries\n"
    "/catchup - What happened since you last posted\n"
    "/party - In-fiction party composition\n"
    "/notes - View GM notes for this campaign\n"
    "/quests - View active and completed quests\n"
    "/pins - View bookmarked story moments\n"
    "/lootlist - View party loot\n"
    "/npcs - View tracked NPCs\n"
    "/conditions - View active conditions/buffs\n"
    "/hp - View enemy HP tracker\n"
    "/clocks - View progress clocks\n"
    "/dc <level> [difficulty] - PF2e DC lookup\n"
    "/pick <N> - Vote in an active poll\n"
    "/showvote - Show current vote status\n"
    "/showtimer - Show active timer\n"
    "/summary - Full campaign state at a glance\n"
    "/activity - Posting patterns: busiest hours and days\n"
    "/profile @player - Cross-campaign stats for a player\n"
    "/away [duration] [reason] - Declare an absence (skip warnings)\n"
    "/back - Return from absence\n"
    "/recap [N] - Show last N transcript entries (default 10)\n"
    "/roll <dice> [label] - Roll dice (e.g. 1d20+5 Stealth)"
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

    # Away players
    away_names = []
    for p in players:
        uid = p.get("user_id", "")
        record = helpers.is_away(state, pid, uid, now)
        if record:
            away_names.append(p["first_name"])
    if away_names:
        lines.append(f"✈️ Away: {', '.join(away_names)}")

    if combat_str:
        lines.append(combat_str)

    paused = state.get("paused_campaigns", {}).get(pid)
    if paused:
        lines.append(f"⏸️ PAUSED: {paused.get('reason', 'No reason')}")

    scene = state.get("current_scenes", {}).get(pid)
    if scene:
        lines.append(f"🎭 Scene: {scene}")

    active_quests = [q for q in state.get("quests", {}).get(pid, [])
                     if q.get("status") == "active"]
    if active_quests:
        lines.append(f"📋 {len(active_quests)} active quest{'s' if len(active_quests) != 1 else ''}")

    # HP tracker count
    hp_entries = state.get("hp_tracker", {}).get(pid, {})
    if hp_entries:
        alive = sum(1 for h in hp_entries.values() if h["current"] > 0)
        lines.append(f"❤️ {alive}/{len(hp_entries)} enemies standing (/hp)")

    # Conditions count
    conds = state.get("conditions", {}).get(pid, [])
    if conds:
        lines.append(f"⚡ {len(conds)} active condition{'s' if len(conds) != 1 else ''}")

    # Active clocks
    clocks = state.get("clocks", {}).get(pid, {})
    if clocks:
        incomplete = sum(1 for c in clocks.values() if c["filled"] < c["segments"])
        lines.append(f"⏱️ {incomplete}/{len(clocks)} clock{'s' if len(clocks) != 1 else ''} ticking")

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

    # Current scene
    scene = state.get("current_scenes", {}).get(pid)
    if scene:
        lines.append(f"\n🎭 Scene: {scene}")

    # GM notes
    notes = state.get("campaign_notes", {}).get(pid, [])
    if notes:
        lines.append(f"\n📝 Notes ({len(notes)}):")
        for i, note in enumerate(notes[-3:], start=max(1, len(notes) - 2)):
            lines.append(f"  {i}. {note['text']}")
        if len(notes) > 3:
            lines.append(f"  … and {len(notes) - 3} more (/notes to see all)")

    return "\n".join(lines)


def _build_mystats(pid: str, user_id: str, campaign_name: str,
                   state: dict, gm_ids: set, config: dict | None = None) -> str:
    """Build personal stats for a player's /mystats command."""
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    is_gm = user_id in gm_ids
    role = "GM" if is_gm else "Player"

    # Character name
    char_name = helpers.character_name(config, pid, user_id) if config else None

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

    header = f"Your stats in {campaign_name} ({role})"
    if char_name:
        header += f" — playing {char_name}"
    header += ":"

    lines = [
        header,
        f"Total: {posts_str(total_count)} ({len(sessions)} sessions)",
        f"This week: {posts_str(len(week_posts))}",
        f"Avg gap: {avg_gap}",
        f"Last post: {last_post_str}",
    ]

    # Word count stats
    total_words = state.get("word_counts", {}).get(pid, {}).get(user_id, 0)
    if total_words > 0 and total_count > 0:
        avg_words = total_words / total_count
        lines.append(f"Words written: {total_words:,} (~{avg_words:.0f}/post)")

    if streak > 1:
        lines.append(f"🔥 Streak: {streak} consecutive days")
    elif streak == 1:
        lines.append(f"Streak: 1 day (keep it going!)")

    return "\n".join(lines)


def _build_party(pid: str, campaign_name: str, config: dict, state: dict) -> str:
    """Build the in-fiction party composition for /party command."""
    characters = helpers.get_characters(config, pid)

    if not characters:
        return (f"No characters configured for {campaign_name}.\n"
                f"Ask your GM to add a 'characters' mapping in the bot config.")

    now = datetime.now(timezone.utc)
    players = [
        p for p in state.get("players", {}).values()
        if p.get("pbp_topic_id") == pid
    ]

    lines = [f"The party of {campaign_name}:", ""]

    # Map active players to their characters
    active_chars = []
    orphan_chars = []

    for uid, char_name in sorted(characters.items(), key=lambda x: x[1]):
        player = None
        for p in players:
            if p.get("user_id") == uid:
                player = p
                break

        if player:
            player_name = helpers.player_full_name(player)
            last_post = datetime.fromisoformat(player["last_post_time"])
            days_ago = helpers.days_since(now, last_post)
            away_record = helpers.is_away(state, pid, uid, now)
            if away_record:
                reason = away_record.get("reason", "Away")
                active_str = f"✈️ away ({reason})"
            elif days_ago < 1:
                active_str = "active today"
            elif days_ago < 7:
                active_str = f"active {int(days_ago)}d ago"
            else:
                active_str = f"last seen {int(days_ago)}d ago"
            active_chars.append(f"  ⚔️ {char_name} ({player_name}) — {active_str}")
        else:
            orphan_chars.append(f"  🔇 {char_name} — no recent posts")

    for line in active_chars:
        lines.append(line)
    for line in orphan_chars:
        lines.append(line)

    lines.append("")
    lines.append(f"{len(active_chars)} active, {len(orphan_chars)} inactive")

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


def _build_catchup(pid: str, user_id: str, campaign_name: str,
                   state: dict, gm_ids: set) -> str:
    """Build a catch-up summary: what happened since the player last posted."""
    now = datetime.now(timezone.utc)
    topic_ts = helpers.get_topic_timestamps(state, pid)
    my_ts = topic_ts.get(user_id, [])

    if not my_ts:
        return f"No posting history in {campaign_name}. Post something first!"

    last_post = max(datetime.fromisoformat(ts) for ts in my_ts)
    hours_ago = (now - last_post).total_seconds() / 3600

    if hours_ago < 1:
        return f"You posted in {campaign_name} less than an hour ago. You're caught up!"

    # Count messages from others since our last post
    poster_counts = {}
    total_since = 0
    for uid, timestamps in topic_ts.items():
        if uid == user_id:
            continue
        is_gm = uid in gm_ids
        count = len(timestamps_in_window(timestamps, last_post))
        if count > 0:
            player = helpers.get_player(state, pid, uid)
            if is_gm:
                name = "GM"
            elif player:
                name = player.get("first_name", "?")
            else:
                name = "?"
            poster_counts[name] = count
            total_since += count

    if total_since == 0:
        time_str = f"{hours_ago:.0f}h" if hours_ago < 48 else f"{hours_ago / 24:.0f}d"
        return (f"Nobody has posted in {campaign_name} since your last message "
                f"({time_str} ago). The floor is yours!")

    # Build summary
    time_str = f"{hours_ago:.0f}h" if hours_ago < 48 else f"{hours_ago / 24:.0f}d"

    lines = [
        f"Catch-up for {campaign_name}:",
        f"",
        f"Since your last post ({time_str} ago):",
        f"",
    ]

    # Sort by count descending
    for name, count in sorted(poster_counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {name}: {posts_str(count)}")

    lines.append(f"")
    lines.append(f"Total: {posts_str(total_since)} from {len(poster_counts)} people")

    # Combat state
    combat = state.get("combat", {}).get(pid, {})
    if combat.get("active"):
        round_num = combat.get("round", "?")
        phase = combat.get("phase", "?")
        lines.append(f"")
        lines.append(f"⚔️ Combat is active (Round {round_num}, {phase})")

    return "\n".join(lines)


def _build_overview(config: dict, state: dict) -> str:
    """Build a compact cross-campaign overview for /overview command."""
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    maps = build_topic_maps(config)

    lines = ["Path Wars — Campaign Overview:", ""]

    total_posts_all = 0
    total_players_all = 0
    campaigns_data = []

    for pid, name in maps.to_name.items():
        gm_ids = helpers.gm_ids_for_campaign(config, pid)
        topic_ts = helpers.get_topic_timestamps(state, pid)
        topic_state = state.get("topics", {}).get(pid)

        # Weekly posts
        gm_week = player_week = 0
        for uid, timestamps in topic_ts.items():
            count = len(timestamps_in_window(timestamps, week_ago))
            if uid in gm_ids:
                gm_week += count
            else:
                player_week += count
        total_week = gm_week + player_week
        total_posts_all += total_week

        # Last post age
        if topic_state:
            last_time = datetime.fromisoformat(topic_state["last_message_time"])
            hours = helpers.hours_since(now, last_time)
            if hours < 1:
                age = "<1h"
            elif hours < 24:
                age = f"{int(hours)}h"
            else:
                age = f"{int(hours / 24)}d"
        else:
            age = "—"

        # Player count
        players = [p for p in state.get("players", {}).values()
                    if p.get("pbp_topic_id") == pid]
        player_count = len(players)
        total_players_all += player_count

        # Combat
        combat = state.get("combat", {}).get(pid, {})
        combat_flag = " ⚔️" if combat.get("active") else ""

        # Paused
        paused = state.get("paused_campaigns", {}).get(pid)
        pause_flag = " ⏸️" if paused else ""

        # Health icon
        health = _health_icon(total_week)

        campaigns_data.append({
            "name": name, "total": total_week, "players": player_count,
            "age": age, "combat": combat_flag, "pause": pause_flag,
            "health": health,
        })

    for c in campaigns_data:
        line = f"{c['health']} {c['name']}: {posts_str(c['total'])} this week"
        line += f" | {c['players']} players | Last: {c['age']}"
        line += c["combat"] + c["pause"]
        lines.append(line)

    lines.append("")
    lines.append(f"Total: {posts_str(total_posts_all)} across {len(campaigns_data)} campaigns, {total_players_all} active players")

    return "\n".join(lines)


def _build_notes(pid: str, campaign_name: str, state: dict) -> str:
    """Build the notes list for /notes command."""
    notes = state.get("campaign_notes", {}).get(pid, [])
    if not notes:
        return f"No GM notes for {campaign_name}.\nGMs can add notes with /note <text>"

    lines = [f"📝 GM Notes — {campaign_name}:", ""]
    for i, note in enumerate(notes, 1):
        created = note.get("created_at", "")[:10]  # YYYY-MM-DD
        lines.append(f"{i}. {note['text']}")
        if created:
            lines.append(f"   ({created})")
    lines.append("")
    lines.append(f"{len(notes)}/20 notes. GMs: /note <text> to add, /delnote <N> to remove.")
    return "\n".join(lines)


_MAX_NOTES_PER_CAMPAIGN = 20
_MAX_QUESTS_PER_CAMPAIGN = 20


def _build_quests(pid: str, campaign_name: str, state: dict) -> str:
    """Build the quest list for /quests command."""
    quests = state.get("quests", {}).get(pid, [])
    if not quests:
        return f"No quests tracked for {campaign_name}.\nGMs can add quests with /quest <text>"

    active = [(i, q) for i, q in enumerate(quests, 1) if q.get("status") == "active"]
    completed = [(i, q) for i, q in enumerate(quests, 1) if q.get("status") == "completed"]

    lines = [f"📋 Quests — {campaign_name}:", ""]

    if active:
        lines.append("Active:")
        for i, q in active:
            lines.append(f"  {i}. {q['text']}")
    if completed:
        lines.append("")
        lines.append("Completed:")
        for i, q in completed:
            done_date = (q.get("completed_at") or "")[:10]
            lines.append(f"  ✅ {i}. {q['text']} ({done_date})")

    lines.append("")
    total = len(quests)
    lines.append(f"{len(active)} active, {len(completed)} completed ({total}/{_MAX_QUESTS_PER_CAMPAIGN}).")
    lines.append("GMs: /quest <text>, /done <N>, /delquest <N>")
    return "\n".join(lines)


_MAX_PINS_PER_CAMPAIGN = 30
_MAX_LOOT_PER_CAMPAIGN = 50


def _build_pins(pid: str, campaign_name: str, state: dict) -> str:
    """Build the pins list for /pins command."""
    pins = state.get("pins", {}).get(pid, [])
    if not pins:
        return f"No pins for {campaign_name}.\nGMs can bookmark moments with /pin <text>"

    lines = [f"📌 Pins — {campaign_name}:", ""]
    for i, pin in enumerate(pins, 1):
        created = pin.get("created_at", "")[:10]
        author = pin.get("author", "")
        author_tag = f" — {author}" if author else ""
        lines.append(f"{i}. {pin['text']}")
        if created:
            lines.append(f"   ({created}{author_tag})")
    lines.append("")
    lines.append(f"{len(pins)}/{_MAX_PINS_PER_CAMPAIGN} pins. GMs: /pin <text>, /delpin <N>")
    return "\n".join(lines)


def _build_lootlist(pid: str, campaign_name: str, state: dict) -> str:
    """Build the loot list for /lootlist command."""
    loot = state.get("loot", {}).get(pid, [])
    if not loot:
        return f"No loot tracked for {campaign_name}.\nGMs can add items with /loot <text>"

    lines = [f"💰 Party Loot — {campaign_name}:", ""]
    for i, item in enumerate(loot, 1):
        added = item.get("added_at", "")[:10]
        lines.append(f"  {i}. {item['text']}")
    lines.append("")
    lines.append(f"{len(loot)}/{_MAX_LOOT_PER_CAMPAIGN} items. GMs: /loot <text>, /delloot <N>")
    return "\n".join(lines)


_MAX_NPCS_PER_CAMPAIGN = 40


def _build_npcs(pid: str, campaign_name: str, state: dict) -> str:
    """Build the NPC list for /npcs command."""
    npcs = state.get("npcs", {}).get(pid, [])
    if not npcs:
        return f"No NPCs tracked for {campaign_name}.\nGMs can add NPCs with /npc <name> — <description>"

    lines = [f"🎭 NPCs — {campaign_name}:", ""]
    for i, npc in enumerate(npcs, 1):
        desc = npc.get("desc", "")
        desc_str = f" — {desc}" if desc else ""
        lines.append(f"  {i}. {npc['name']}{desc_str}")
    lines.append("")
    lines.append(f"{len(npcs)}/{_MAX_NPCS_PER_CAMPAIGN} NPCs. GMs: /npc <name> — <desc>, /delnpc <N>")
    return "\n".join(lines)


def _build_conditions(pid: str, campaign_name: str, state: dict, config: dict) -> str:
    """Build the active conditions list for /conditions command."""
    conds = state.get("conditions", {}).get(pid, [])
    if not conds:
        return f"No active conditions in {campaign_name}.\nGMs can add with /condition <target> — <effect>"

    lines = [f"⚡ Active Conditions — {campaign_name}:", ""]
    for i, c in enumerate(conds, 1):
        target = c.get("target", "Unknown")
        effect = c.get("effect", "")
        duration = c.get("duration", "")
        dur_str = f" ({duration})" if duration else ""
        lines.append(f"  {i}. {target}: {effect}{dur_str}")
    lines.append("")
    lines.append(f"{len(conds)} active. GMs: /condition, /endcondition <N>, /clearconditions")
    return "\n".join(lines)


def _build_vote(pid: str, campaign_name: str, state: dict) -> str:
    """Build the current vote display for /vote (no args) or /showvote."""
    vote = state.get("votes", {}).get(pid)
    if not vote or vote.get("closed"):
        return "No active vote. GMs can start one with /vote <question> | <option1> | <option2> [| ...]"

    lines = [f"🗳️ Vote — {campaign_name}:", ""]
    lines.append(f"❓ {vote['question']}")
    lines.append("")

    results = vote.get("results", {})
    total = sum(len(v) for v in results.values())

    for i, option in enumerate(vote["options"], 1):
        voters = results.get(str(i), [])
        count = len(voters)
        bar = "█" * count + "░" * max(0, 5 - count) if total > 0 else "░" * 5
        voter_names = ", ".join(voters) if voters else ""
        voter_str = f"  ({voter_names})" if voter_names else ""
        lines.append(f"  {i}. {option}  [{bar}] {count}{voter_str}")

    lines.append("")
    lines.append(f"{total} vote{'s' if total != 1 else ''} cast. Use /pick <N> to vote.")
    return "\n".join(lines)


def _build_timer(pid: str, campaign_name: str, state: dict) -> str:
    """Build the timer display for /showtimer."""
    timer = state.get("timers", {}).get(pid)
    if not timer:
        return "No active timer. GMs: /timer <duration> [reason]"

    now = datetime.now(timezone.utc)
    deadline = datetime.fromisoformat(timer["deadline"])
    remaining = deadline - now

    if remaining.total_seconds() <= 0:
        return f"⏰ Timer EXPIRED for {campaign_name}!\n{timer.get('reason', '')}\nGMs: /canceltimer to clear"

    # Format remaining time
    hours = int(remaining.total_seconds() // 3600)
    mins = int((remaining.total_seconds() % 3600) // 60)
    if hours >= 24:
        days = hours // 24
        time_str = f"{days}d {hours % 24}h"
    elif hours > 0:
        time_str = f"{hours}h {mins}m"
    else:
        time_str = f"{mins}m"

    reason = timer.get("reason", "")
    reason_str = f"\n📝 {reason}" if reason else ""

    return (
        f"⏳ Timer — {campaign_name}\n"
        f"⏰ {time_str} remaining (deadline: {deadline.strftime('%b %d %H:%M UTC')})"
        f"{reason_str}\n"
        f"GMs: /canceltimer to clear"
    )


def _build_summary(pid: str, campaign_name: str, state: dict, config: dict) -> str:
    """Build a one-stop campaign state summary."""
    lines = [f"📖 Summary — {campaign_name}", ""]

    # Current scene
    scene = state.get("current_scene", {}).get(pid)
    if scene:
        lines.append(f"🎬 Scene: {scene}")

    # Combat state
    combat = state.get("combat", {}).get(pid, {})
    if combat.get("active"):
        phase = combat.get("phase", "?")
        round_num = combat.get("round", "?")
        lines.append(f"⚔️ Combat: Round {round_num} — {phase}")

    # Timer
    timer = state.get("timers", {}).get(pid)
    if timer:
        now = datetime.now(timezone.utc)
        deadline = datetime.fromisoformat(timer["deadline"])
        remaining = deadline - now
        if remaining.total_seconds() > 0:
            hours = int(remaining.total_seconds() // 3600)
            mins = int((remaining.total_seconds() % 3600) // 60)
            time_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
            reason = timer.get("reason", "")
            lines.append(f"⏳ Timer: {time_str} left" + (f" — {reason}" if reason else ""))
        else:
            lines.append("⏰ Timer EXPIRED!")

    # Active vote
    vote = state.get("votes", {}).get(pid)
    if vote and not vote.get("closed"):
        total = sum(len(v) for v in vote.get("results", {}).values())
        lines.append(f"🗳️ Vote: {vote['question']} ({total} votes)")

    # Away players
    away_list = []
    for key, info in state.get("away", {}).items():
        if key.startswith(f"{pid}:"):
            if helpers.is_away(state, pid, key.split(":")[1]):
                reason = info.get("reason", "")
                away_list.append(reason if reason else "away")
    if away_list:
        lines.append(f"✈️ {len(away_list)} player{'s' if len(away_list) != 1 else ''} away")

    if len(lines) == 2:
        lines.append("Nothing special happening right now.")

    lines.append("")

    # Active quests
    quests = [q for q in state.get("quests", {}).get(pid, []) if q.get("status") == "active"]
    if quests:
        lines.append(f"📋 Quests ({len(quests)} active):")
        for i, q in enumerate(quests[:5], 1):
            lines.append(f"  {i}. {q['text']}")
        if len(quests) > 5:
            lines.append(f"  ... and {len(quests) - 5} more (/quests)")
        lines.append("")

    # Active conditions
    conds = state.get("conditions", {}).get(pid, [])
    if conds:
        lines.append(f"⚡ Conditions ({len(conds)}):")
        for c in conds[:5]:
            dur = f" ({c['duration']})" if c.get("duration") else ""
            lines.append(f"  • {c['target']}: {c['effect']}{dur}")
        if len(conds) > 5:
            lines.append(f"  ... and {len(conds) - 5} more (/conditions)")
        lines.append("")

    # NPC count
    npcs = state.get("npcs", {}).get(pid, [])
    if npcs:
        lines.append(f"🎭 {len(npcs)} NPC{'s' if len(npcs) != 1 else ''} tracked (/npcs)")

    # Loot count
    loot = state.get("loot", {}).get(pid, [])
    if loot:
        lines.append(f"💰 {len(loot)} loot item{'s' if len(loot) != 1 else ''} (/lootlist)")

    # Pins count
    pins = state.get("pins", {}).get(pid, [])
    if pins:
        lines.append(f"📌 {len(pins)} pin{'s' if len(pins) != 1 else ''} (/pins)")

    # HP tracker
    hp_entries = state.get("hp_tracker", {}).get(pid, {})
    if hp_entries:
        lines.append("")
        lines.append(f"❤️ HP Tracker ({len(hp_entries)}):")
        for name, hp in sorted(hp_entries.items()):
            icon = helpers.hp_status_icon(hp["current"], hp["max"])
            bar = helpers.hp_bar(hp["current"], hp["max"], 8)
            lines.append(f"  {icon} {name}: {bar}")

    # Progress clocks
    clocks = state.get("clocks", {}).get(pid, {})
    if clocks:
        lines.append("")
        lines.append(f"⏱️ Clocks ({len(clocks)}):")
        for name, clock in sorted(clocks.items()):
            display = helpers.clock_display(clock["filled"], clock["segments"])
            lines.append(f"  {name}: {display}")

    return "\n".join(lines)


_MAX_HP_ENTRIES = 20
_MAX_CLOCKS = 15


def _build_hp_tracker(pid: str, campaign_name: str, state: dict) -> str:
    """Build HP tracker display for /hp (no args)."""
    hp_entries = state.get("hp_tracker", {}).get(pid, {})
    if not hp_entries:
        return (f"No HP tracked in {campaign_name}.\n"
                "GMs: /hp set <name> <current>/<max>\n"
                "      /hp d <name> <amount>   (damage)\n"
                "      /hp h <name> <amount>   (heal)")

    lines = [f"❤️ HP Tracker — {campaign_name}:", ""]
    for name, hp in sorted(hp_entries.items()):
        icon = helpers.hp_status_icon(hp["current"], hp["max"])
        bar = helpers.hp_bar(hp["current"], hp["max"])
        lines.append(f"  {icon} {name}: {bar}")
    lines.append("")
    lines.append(f"{len(hp_entries)}/{_MAX_HP_ENTRIES} entries.")
    lines.append("GMs: /hp set, /hp d(amage), /hp h(eal), /hp remove, /hp clear")
    return "\n".join(lines)


def _build_clocks(pid: str, campaign_name: str, state: dict) -> str:
    """Build progress clocks display for /clocks."""
    clocks = state.get("clocks", {}).get(pid, {})
    if not clocks:
        return (f"No clocks in {campaign_name}.\n"
                "GMs: /clock <name> <segments>  (create)\n"
                "      /tick <name> [N]          (advance)\n"
                "      /untick <name> [N]        (reverse)")

    lines = [f"⏱️ Progress Clocks — {campaign_name}:", ""]
    for name, clock in sorted(clocks.items()):
        display = helpers.clock_display(clock["filled"], clock["segments"])
        complete = " ✅" if clock["filled"] >= clock["segments"] else ""
        lines.append(f"  {name}: {display}{complete}")
    lines.append("")
    lines.append(f"{len(clocks)}/{_MAX_CLOCKS} clocks.")
    lines.append("GMs: /clock, /tick, /untick, /delclock")
    return "\n".join(lines)


def _build_gm_dashboard(config: dict, state: dict) -> str:
    """Build a compact GM overview of all campaigns."""
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    maps = build_topic_maps(config)

    lines = ["📊 GM Dashboard:", ""]

    total_posts = 0
    total_players = 0

    for pid, name in sorted(maps.to_name.items(), key=lambda x: x[1]):
        gm_ids = helpers.gm_ids_for_campaign(config, pid)
        topic_ts = helpers.get_topic_timestamps(state, pid)
        players = [p for p in state.get("players", {}).values()
                   if p.get("pbp_topic_id") == pid]
        player_count = len(players)
        total_players += player_count

        # Posts this week
        week_posts = 0
        for uid, timestamps in topic_ts.items():
            week_posts += len(timestamps_in_window(timestamps, week_ago))
        total_posts += week_posts

        # Last post
        topic_state = state.get("topics", {}).get(pid)
        if topic_state:
            last_dt = datetime.fromisoformat(topic_state["last_message_time"])
            last_str, _ = helpers.fmt_brief_relative(now, last_dt)
        else:
            last_str = "never"

        # Health indicator
        if week_posts >= 20:
            icon = "🟢"
        elif week_posts >= 10:
            icon = "🟡"
        elif week_posts >= 5:
            icon = "🟠"
        else:
            icon = "🔴"

        # Flags
        flags = []
        if state.get("paused_campaigns", {}).get(pid):
            flags.append("⏸️")
        if state.get("combat", {}).get(pid, {}).get("active"):
            flags.append("⚔️")
        away_count = sum(1 for p in players
                         if helpers.is_away(state, pid, p.get("user_id", ""), now))
        if away_count:
            flags.append(f"✈️{away_count}")

        # At-risk count
        at_risk = sum(1 for p in players
                      if helpers.days_since(now, datetime.fromisoformat(p["last_post_time"])) >= 7)
        if at_risk:
            flags.append(f"⚠️{at_risk}")

        active_quests = len([q for q in state.get("quests", {}).get(pid, [])
                             if q.get("status") == "active"])
        if active_quests:
            flags.append(f"📋{active_quests}")

        flag_str = " " + " ".join(flags) if flags else ""

        lines.append(f"{icon} {name}: {week_posts}pw, {player_count}p, last {last_str}{flag_str}")

    lines.append("")
    lines.append(f"Total: {total_posts} posts/week across {len(maps.to_name)} campaigns, {total_players} players")

    return "\n".join(lines)


def _write_scene_marker(campaign_name: str, scene_name: str) -> None:
    """Write a scene boundary marker to the campaign's transcript file."""
    dir_name = _sanitize_dirname(campaign_name)
    campaign_dir = _LOGS_DIR / dir_name
    campaign_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    month_str = now.strftime("%Y-%m")
    log_file = campaign_dir / f"{month_str}.md"

    is_new = not log_file.exists()

    with open(log_file, "a", encoding="utf-8") as f:
        if is_new:
            f.write(f"# {campaign_name} — {month_str}\n\n")
            f.write("*PBP transcript archived by PathWarsNudge bot.*\n\n---\n\n")
        ts = now.strftime("%Y-%m-%d %H:%M")
        f.write(f"\n---\n\n### 🎭 Scene: {scene_name}\n*({ts})*\n\n---\n\n")


_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_HOUR_BLOCKS = {
    "Night (00-05)": range(0, 6),
    "Morning (06-11)": range(6, 12),
    "Afternoon (12-17)": range(12, 18),
    "Evening (18-23)": range(18, 24),
}


def _build_activity(pid: str, campaign_name: str, state: dict, gm_ids: set) -> str:
    """Build activity pattern report for /activity command."""
    hours_data = state.get("activity_hours", {}).get(pid, {})
    days_data = state.get("activity_days", {}).get(pid, {})

    if not hours_data and not days_data:
        return f"No activity data for {campaign_name} yet.\nPost some messages and check back!"

    # Aggregate across all users (excluding GM optionally — include everyone)
    hour_totals = {}
    day_totals = {}
    for uid, h in hours_data.items():
        for hour, count in h.items():
            hour_totals[int(hour)] = hour_totals.get(int(hour), 0) + count
    for uid, d in days_data.items():
        for day, count in d.items():
            day_totals[int(day)] = day_totals.get(int(day), 0) + count

    total_posts = sum(hour_totals.values())

    lines = [f"📊 Activity Patterns — {campaign_name}", f"({total_posts} tracked posts)", ""]

    # Best days
    lines.append("Busiest days:")
    sorted_days = sorted(day_totals.items(), key=lambda x: x[1], reverse=True)
    for day_num, count in sorted_days:
        pct = count / total_posts * 100 if total_posts else 0
        bar_len = int(pct / 5)  # Each block = 5%
        bar = "█" * bar_len
        lines.append(f"  {_DAY_NAMES[day_num]:3s}  {bar} {count} ({pct:.0f}%)")

    # Best time blocks
    lines.append("")
    lines.append("Busiest times (UTC):")
    block_totals = {}
    for block_name, hour_range in _HOUR_BLOCKS.items():
        block_totals[block_name] = sum(hour_totals.get(h, 0) for h in hour_range)
    sorted_blocks = sorted(block_totals.items(), key=lambda x: x[1], reverse=True)
    for block_name, count in sorted_blocks:
        pct = count / total_posts * 100 if total_posts else 0
        bar_len = int(pct / 5)
        bar = "█" * bar_len
        lines.append(f"  {block_name:20s} {bar} {count} ({pct:.0f}%)")

    # Peak hour
    if hour_totals:
        peak_hour = max(hour_totals, key=hour_totals.get)
        lines.append(f"\nPeak hour: {peak_hour:02d}:00 UTC ({hour_totals[peak_hour]} posts)")

    # Top 3 most active players
    player_totals = {}
    for uid, h in hours_data.items():
        player_totals[uid] = sum(h.values())
    sorted_players = sorted(player_totals.items(), key=lambda x: x[1], reverse=True)[:3]
    if sorted_players:
        lines.append("")
        lines.append("Most active posters:")
        players_map = {p["user_id"]: p for p in state.get("players", {}).values()
                       if p.get("pbp_topic_id") == pid}
        for uid, count in sorted_players:
            name = "GM" if uid in gm_ids else players_map.get(uid, {}).get("first_name", uid)
            lines.append(f"  {name}: {count} posts")

    return "\n".join(lines)


def _build_profile(target_name: str, config: dict, state: dict) -> str:
    """Build cross-campaign profile for /profile command."""
    # Find the target player across all campaigns
    target_name_lower = target_name.lower().lstrip("@")
    found_entries = []

    for key, player in state.get("players", {}).items():
        full_name = helpers.player_full_name(player).lower()
        username = (player.get("username") or "").lower()
        first_name = player.get("first_name", "").lower()

        if (target_name_lower == username or
                target_name_lower == first_name or
                target_name_lower in full_name):
            found_entries.append((key, player))

    if not found_entries:
        return f"No player matching '{target_name}' found across any campaign."

    # Determine display name from first match
    display_name = helpers.player_full_name(found_entries[0][1])
    user_id = found_entries[0][1]["user_id"]

    # Gather stats across all campaigns they're in
    lines = [f"👤 {display_name}", ""]
    total_posts = 0
    total_campaigns = 0
    total_words = 0

    for key, player in found_entries:
        pid = player["pbp_topic_id"]
        campaign_name = player["campaign_name"]
        counts = state.get("message_counts", {}).get(pid, {})
        post_count = counts.get(user_id, 0)
        total_posts += post_count
        total_campaigns += 1

        # Last post
        last_post = player.get("last_post_time", "")
        if last_post:
            last_dt = datetime.fromisoformat(last_post)
            elapsed_h = helpers.hours_since(datetime.now(timezone.utc), last_dt)
            if elapsed_h < 24:
                last_str = f"{int(elapsed_h)}h ago"
            else:
                last_str = f"{int(elapsed_h / 24)}d ago"
        else:
            last_str = "unknown"

        # Character name
        char_name = helpers.character_name(config, pid, user_id)
        char_tag = f" ({char_name})" if char_name else ""

        # Streak
        topic_ts = helpers.get_topic_timestamps(state, pid)
        raw_ts = topic_ts.get(user_id, [])
        streak = _calc_streak(raw_ts, datetime.now(timezone.utc))
        streak_str = f" | 🔥 {streak}d streak" if streak >= 3 else ""

        # Word count
        words = state.get("word_counts", {}).get(pid, {}).get(user_id, 0)
        words_str = f" | {words:,} words" if words > 0 else ""
        total_words += words

        lines.append(f"📖 {campaign_name}{char_tag}")
        lines.append(f"   {post_count} posts{words_str} | Last: {last_str}{streak_str}")

    lines.append("")
    words_summary = f" ({total_words:,} words)" if total_words > 0 else ""
    lines.append(f"Total: {total_posts} posts{words_summary} across {total_campaigns} campaign{'s' if total_campaigns != 1 else ''}")

    return "\n".join(lines)


def _build_recap(pid: str, campaign_name: str, config: dict, count: int = 10) -> str:
    """Build a compact recap of the last N transcript entries.

    Reads from the campaign's pbp_logs directory, pulling from the
    most recent month file(s) until we have enough entries.
    """
    import re
    count = max(1, min(count, 25))  # clamp 1-25

    dir_name = helpers.campaign_dir_name(campaign_name)
    campaign_dir = _LOGS_DIR / dir_name

    if not campaign_dir.exists():
        return f"No transcript archive found for {campaign_name}."

    # Get month files sorted newest first
    month_files = sorted(campaign_dir.glob("*.md"), reverse=True)
    if not month_files:
        return f"No transcript entries for {campaign_name}."

    # Parse entries from newest files until we have enough
    entries = []
    # Entry pattern: **Name** (optional char/GM) (timestamp):\ncontent\n
    entry_re = re.compile(
        r"^\*\*(.+?)\*\*(?:\s*\(.+?\))?\s*(?:\[GM\])?\s*\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\):\n(.*?)(?=\n\*\*|\n---|\Z)",
        re.MULTILINE | re.DOTALL,
    )

    for month_file in month_files:
        if len(entries) >= count:
            break
        try:
            text = month_file.read_text(encoding="utf-8")
        except OSError:
            continue

        file_entries = []
        for m in entry_re.finditer(text):
            name = m.group(1).strip()
            timestamp = m.group(2).strip()
            content = m.group(3).strip()
            # Truncate very long content
            if len(content) > 120:
                content = content[:117] + "..."
            file_entries.append((timestamp, name, content))

        # Add from this file (they're in chronological order within the file)
        entries = file_entries + entries
        if len(entries) > count:
            entries = entries[-count:]

    if not entries:
        return f"No transcript entries found for {campaign_name}."

    lines = [f"📜 Last {len(entries)} messages in {campaign_name}:", ""]
    for ts, name, content in entries:
        # Compact: just time (HH:MM), name, snippet
        time_str = ts[11:16]  # HH:MM
        date_str = ts[:10]     # YYYY-MM-DD
        content_line = content.replace("\n", " ").strip()
        lines.append(f"[{date_str} {time_str}] {name}: {content_line}")

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
    now = datetime.now(timezone.utc)
    elapsed = helpers.hours_since(now, phase_start)

    lines = [
        f"⚔️ {campaign_name} — Round {round_num}, {phase_label}' turn",
        f"Phase started: {_format_elapsed(elapsed)} ago",
    ]

    # Enemy roster
    enemies = combat.get("enemies", [])
    if enemies:
        lines.append(f"Enemies: {', '.join(enemies)}")

    lines.append("")

    if phase == "players":
        acted_dict = combat.get("players_acted", {})
        # Migrate old list format
        if isinstance(acted_dict, list):
            acted_dict = {uid: combat.get("phase_started_at", "") for uid in acted_dict}

        acted_ids = set(acted_dict.keys())
        players = [
            p for p in state.get("players", {}).values()
            if p.get("pbp_topic_id") == pid
        ]

        acted_list = []
        waiting_list = []
        for p in sorted(players, key=lambda x: x["first_name"]):
            uid = p["user_id"]
            if helpers.is_away(state, pid, uid, now):
                continue  # Skip away players entirely
            if uid in acted_ids:
                ts = acted_dict[uid]
                if ts:
                    acted_time = datetime.fromisoformat(ts)
                    ago = helpers.hours_since(now, acted_time)
                    acted_list.append(f"  ✅ {p['first_name']} ({_format_elapsed(ago)} ago)")
                else:
                    acted_list.append(f"  ✅ {p['first_name']}")
            else:
                # How long have they been holding things up?
                wait_h = helpers.hours_since(now, phase_start)
                wait_str = f" — waiting {_format_elapsed(wait_h)}" if wait_h >= 1 else ""
                waiting_list.append(f"  ⏳ {p['first_name']}{wait_str}")

        if waiting_list:
            lines.append("Waiting on:")
            lines.extend(waiting_list)
        if acted_list:
            lines.append("Acted:")
            lines.extend(acted_list)
        if not waiting_list and acted_list:
            lines.append("✅ Everyone has acted! GM can use /next")
    else:
        lines.append("Waiting for GM to resolve enemy turns.")
        lines.append("Use /next when done.")

    return "\n".join(lines)


def _format_elapsed(hours: float) -> str:
    """Format elapsed hours as a readable string."""
    if hours < 1:
        return f"{int(hours * 60)}m"
    elif hours < 24:
        return f"{int(hours)}h"
    else:
        days = int(hours / 24)
        remaining = int(hours % 24)
        return f"{days}d {remaining}h"


def _build_combatlog(pid: str, campaign_name: str, state: dict) -> str:
    """Build the combat log for /combatlog command."""
    combat = state.get("combat", {}).get(pid)
    if not combat or not combat.get("active"):
        return f"No active combat in {campaign_name}."

    log = combat.get("combat_log", [])
    if not log:
        return f"No combat log entries yet.\nGMs: /clog <event> to add entries."

    lines = [f"📝 Combat Log — {campaign_name} (Round {combat['round']}):", ""]
    for entry in log:
        lines.append(f"  R{entry['round']}: {entry['text']}")
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

    "💡 <b>Combat tracking</b> — Type <code>/combat Ogre, 2 Skeletons</code> to start. "
    "The bot tracks who posts their actions. When everyone's done, the GM gets auto-pinged! "
    "Use /whosturn to see who's still needed.",

    "💡 <b>/next</b> — Advance combat phases: players → enemies → next round. "
    "No more typing <code>/round 2 players</code> — just /next! "
    "Use <code>/clog The ogre crits Cardigan!</code> to log key moments, "
    "and /endcombat for a summary.",

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

    "💡 <b>/kick</b> (GM only) — Need to remove a player from tracking? "
    "Type <code>/kick @username</code> or <code>/kick PlayerName</code>. "
    "They can rejoin by posting in PBP again.",

    "💡 <b>/addplayer</b> (GM only) — Want someone on the roster before they've posted? "
    "Type <code>/addplayer @username Player Name</code> to pre-register them.",

    "💡 <b>/catchup</b> — Been away for a while? Type <code>/catchup</code> to see "
    "how many messages were posted since your last one, and who posted them.",

    "💡 <b>Message milestones</b> — The bot celebrates every 500th PBP message in each campaign, "
    "and every 5,000th message across all campaigns combined. Keep posting!",

    "💡 <b>/party</b> — See the in-fiction party composition: character names, "
    "who plays them, and when they were last active. Requires character config.",

    "💡 <b>Smart alerts</b> — The bot watches for campaigns that lose momentum. "
    "If weekly posts drop by 40%+, or if everyone goes silent for 2+ days, "
    "you'll get a gentle heads-up. Use /pause to silence during planned breaks.",

    "💡 <b>/overview</b> — See a compact summary of ALL campaigns at once: "
    "health status, weekly posts, player count, and last post time. "
    "Perfect for GMs juggling multiple games.",

    "💡 <b>/scene</b> (GM only) — Mark a scene boundary in the transcript. "
    "Type <code>/scene The Docks at Midnight</code> and it'll appear as a divider "
    "in the archived logs. Keeps your campaign history organised by narrative beats.",

    "💡 <b>/note</b> (GM only) — Keep persistent notes for any campaign. "
    "Type <code>/note Party agreed to meet the informant at dawn</code>. "
    "View with /notes, delete with /delnote. Notes also appear in /campaign output.",

    "💡 <b>/activity</b> — See when your campaign is most active: busiest days, "
    "peak hours, and time blocks. Great for knowing when to expect replies "
    "and when to post for maximum engagement.",

    "💡 <b>/profile</b> — Look up any player across all campaigns. "
    "Type <code>/profile @alice</code> to see their character, post counts, "
    "streaks, and last activity in every game they're in.",

    "💡 <b>Word Count Tracking</b> — The bot now tracks total words written per player. "
    "Check /mystats to see your word count and average words per post. "
    "Quality and quantity both matter in PBP!",

    "💡 <b>/away</b> — Going on holiday? Type <code>/away 5 days vacation</code> "
    "and the bot will skip you for inactivity warnings and combat pings. "
    "Use /back when you return, or the bot clears it automatically when you post.",

    "💡 <b>/recap</b> — Missed a few days? Type <code>/recap</code> to see the last "
    "10 transcript entries, or <code>/recap 20</code> for more. A quick way to "
    "catch up on the story without scrolling.",

    "💡 <b>/roll</b> — Roll dice right in chat! "
    "<code>/roll 1d20+5 Stealth</code> for a skill check, "
    "<code>/roll 2d6+3</code> for damage, or "
    "<code>/roll 4d6kh3</code> to keep the highest 3. "
    "Uses your character name if one is configured.",

    "💡 <b>/quests</b> — Your GM can track active quest objectives with "
    "<code>/quest Find the missing merchant</code>. View them with /quests. "
    "When you complete one, the GM uses /done to check it off. "
    "Never lose track of what you're supposed to be doing!",

    "💡 <b>/gm</b> (GM only) — A compact dashboard showing every campaign's health "
    "at a glance: weekly post count, player count, away/at-risk flags, "
    "active quests, and combat status. One command to check all your games.",

    "💡 <b>/dc</b> — Quick DC lookup for Pathfinder 2e! "
    "<code>/dc 5</code> shows all difficulty DCs for level 5. "
    "<code>/dc 5 hard</code> gives just the hard DC. "
    "<code>/dc trained</code> for proficiency DCs. Never flip through the CRB again.",

    "💡 <b>/pins</b> — The GM can bookmark key story moments with "
    "<code>/pin The party found the dragon's weakness</code>. "
    "View them with /pins. Great for tracking reveals, clues, and plot twists.",

    "💡 <b>/lootlist</b> — Track party loot with "
    "<code>/loot +1 striking longsword</code>. View everything with /lootlist. "
    "Remove claimed items with /delloot. Never forget what you picked up!",

    "💡 <b>/npcs</b> — Can't remember who that merchant was? "
    "GMs can add NPCs with <code>/npc Gorund — Dwarven blacksmith, owes party a favour</code>. "
    "View them all with /npcs. A living dramatis personae for your campaign.",

    "💡 <b>/conditions</b> — Track buffs, debuffs, and persistent effects. "
    "<code>/condition Cardigan — Frightened 2 | until end of next turn</code>. "
    "View active conditions with /conditions, end them with /endcondition, "
    "or /clearconditions to wipe the slate.",

    "💡 <b>/hp</b> — Track enemy HP in combat! "
    "<code>/hp set Ogre 45/45</code> to start, "
    "<code>/hp d Ogre 12</code> to deal damage, "
    "<code>/hp h Ogre 5</code> to heal. "
    "Visual HP bars show who's hurting. /hp clear when combat ends.",

    "💡 <b>/clocks</b> — Progress clocks for investigations, rituals, countdowns. "
    "<code>/clock Investigation 6</code> creates a 6-segment clock. "
    "<code>/tick Investigation</code> fills a segment. "
    "Great for tracking anything that builds over time. ◉◉◉○○○",

    "💡 <b>/vote</b> — Stuck on a group decision? GMs can start a vote: "
    "<code>/vote Where do we go? | North gate | Sewers | Rest first</code>. "
    "Players use /pick N to cast their vote. /endvote closes it and shows the winner.",

    "💡 <b>/timer</b> — Set a response deadline for the party. "
    "<code>/timer 24h Post your combat actions</code>. "
    "The bot will post a notification when time's up. "
    "Check with /showtimer, cancel with /canceltimer.",

    "💡 <b>/summary</b> — Everything at a glance: current scene, combat state, "
    "active quests, conditions, NPCs, loot, and pins. "
    "One command to see the full state of your campaign.",
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
    text: str, raw_text: str, user_id: str, user_name: str, gm_ids: set, pid: str, campaign_name: str,
    now_iso: str, group_id: int, thread_id: int, state: dict,
) -> None:
    """Process GM combat commands and track player actions.

    Combat state structure:
        active: bool
        campaign_name: str
        round: int
        current_phase: "players" | "enemies"
        phase_started_at: ISO timestamp
        players_acted: {user_id: timestamp}  (dict now, not list)
        last_ping_at: ISO timestamp or None
        enemies: [str]              — named enemy roster
        combat_log: [{round, text, at}]  — key moment log
        started_at: ISO timestamp
        all_players_notified: bool  — have we pinged GM that everyone's done?
    """
    if user_id in gm_ids:
        if text.startswith("/round"):
            _handle_round_command(text, pid, campaign_name, now_iso, group_id, thread_id, state)

        elif text.startswith("/next"):
            _handle_next_command(pid, campaign_name, now_iso, group_id, thread_id, state)

        elif text.startswith("/endcombat") or text == "/combat end":
            _handle_endcombat(pid, campaign_name, group_id, thread_id, state)

        elif text.startswith("/combat") and not text.startswith("/combatlog"):
            combat_args = raw_text[7:].strip()
            _handle_combat_start(combat_args, pid, campaign_name, now_iso, group_id, thread_id, state)

        elif text.startswith("/enemies"):
            enemy_args = raw_text[8:].strip()
            _handle_enemies_command(enemy_args, pid, campaign_name, now_iso, group_id, thread_id, state)

        elif text.startswith("/clog"):
            clog_args = raw_text[5:].strip()
            if clog_args:
                combat = state["combat"].get(pid)
                if combat and combat.get("active"):
                    log = combat.setdefault("combat_log", [])
                    log.append({"round": combat["round"], "text": clog_args, "at": now_iso})
                    tg.send_message(group_id, thread_id, f"📝 R{combat['round']}: {clog_args}")
                else:
                    tg.send_message(group_id, thread_id, "No active combat. Start with /combat")
            else:
                tg.send_message(group_id, thread_id,
                                "Usage: /clog <event>\ne.g. /clog The ogre crits Cardigan for 28 damage!")

    # Track player action during combat (with timestamp)
    combat = state["combat"].get(pid)
    if (combat and combat.get("active")
            and combat["current_phase"] == "players"
            and user_id not in gm_ids):
        acted = combat.get("players_acted", {})
        # Migrate old list format to dict
        if isinstance(acted, list):
            acted = {uid: now_iso for uid in acted}
            combat["players_acted"] = acted
        if user_id not in acted:
            acted[user_id] = now_iso
            # Check if all players have now acted
            if not combat.get("all_players_notified"):
                _check_all_acted(pid, campaign_name, group_id, thread_id, state, gm_ids)


def _check_all_acted(pid: str, campaign_name: str, group_id: int, thread_id: int,
                     state: dict, gm_ids: set) -> None:
    """If all non-away players have acted, notify the GM."""
    combat = state["combat"].get(pid)
    if not combat or not combat.get("active"):
        return
    acted = set(combat.get("players_acted", {}).keys())
    now = datetime.now(timezone.utc)
    players = [
        p for p in state.get("players", {}).values()
        if p.get("pbp_topic_id") == pid
    ]
    waiting = [
        p for p in players
        if p["user_id"] not in acted
        and not helpers.is_away(state, pid, p["user_id"], now)
    ]
    if not waiting and len(acted) > 0:
        combat["all_players_notified"] = True
        # Mention all GMs
        gm_mentions = []
        for p in state.get("players", {}).values():
            if p.get("user_id") in gm_ids and p.get("pbp_topic_id") == pid:
                gm_mentions.append(helpers.player_mention(p))
        gm_str = " ".join(gm_mentions) if gm_mentions else "GM"
        tg.send_message(group_id, thread_id,
                        f"✅ All players have posted their actions for Round {combat['round']}!\n"
                        f"{gm_str} — ready to resolve.")


def _handle_combat_start(args: str, pid: str, campaign_name: str,
                          now_iso: str, group_id: int, thread_id: int, state: dict) -> None:
    """Start combat with optional enemy list: /combat Ogre, 2 Skeletons"""
    enemies = [e.strip() for e in args.split(",") if e.strip()] if args else []

    state["combat"][pid] = {
        "active": True,
        "campaign_name": campaign_name,
        "round": 1,
        "current_phase": "players",
        "phase_started_at": now_iso,
        "players_acted": {},
        "last_ping_at": None,
        "enemies": enemies,
        "combat_log": [],
        "started_at": now_iso,
        "all_players_notified": False,
    }

    lines = [f"⚔️ Combat started in {campaign_name}!",
             f"Round 1 — Players' turn."]
    if enemies:
        lines.append("")
        lines.append("Enemies:")
        for e in enemies:
            lines.append(f"  • {e}")
    lines.append("")
    lines.append("Post your actions. Use /whosturn to see who's still needed.")

    print(f"Combat started in {campaign_name}: {enemies}")
    tg.send_message(group_id, thread_id, "\n".join(lines))


def _handle_next_command(pid: str, campaign_name: str, now_iso: str,
                         group_id: int, thread_id: int, state: dict) -> None:
    """Advance to next phase: players→enemies→next round players."""
    combat = state["combat"].get(pid)
    if not combat or not combat.get("active"):
        tg.send_message(group_id, thread_id, "No active combat. Start with /combat")
        return

    old_round = combat["round"]
    old_phase = combat["current_phase"]

    if old_phase == "players":
        # Advance to enemies
        combat["current_phase"] = "enemies"
        combat["phase_started_at"] = now_iso
        combat["last_ping_at"] = None
        tg.send_message(group_id, thread_id,
                        f"Round {old_round} — Enemies' turn.")
    else:
        # Advance to next round, players
        new_round = old_round + 1
        combat["round"] = new_round
        combat["current_phase"] = "players"
        combat["phase_started_at"] = now_iso
        combat["players_acted"] = {}
        combat["last_ping_at"] = None
        combat["all_players_notified"] = False
        tg.send_message(group_id, thread_id,
                        f"Round {new_round} — Players' turn.\n"
                        f"Post your actions!")

    print(f"Combat in {campaign_name}: Round {combat['round']}, {combat['current_phase']}")


def _handle_endcombat(pid: str, campaign_name: str,
                      group_id: int, thread_id: int, state: dict) -> None:
    """End combat with a summary."""
    combat = state["combat"].get(pid)
    if not combat:
        tg.send_message(group_id, thread_id, f"No active combat in {campaign_name}.")
        return

    # Build summary
    rounds = combat.get("round", 1)
    started = combat.get("started_at", "")
    log = combat.get("combat_log", [])

    lines = [f"⚔️ Combat ended in {campaign_name}.",
             f"Lasted {rounds} round{'s' if rounds != 1 else ''}."]

    if log:
        lines.append("")
        lines.append("Combat log:")
        for entry in log[-8:]:  # Last 8 entries
            lines.append(f"  R{entry['round']}: {entry['text']}")
        if len(log) > 8:
            lines.append(f"  ... and {len(log) - 8} earlier entries")

    del state["combat"][pid]
    print(f"Combat ended in {campaign_name}")
    tg.send_message(group_id, thread_id, "\n".join(lines))


def _handle_enemies_command(args: str, pid: str, campaign_name: str,
                             now_iso: str, group_id: int, thread_id: int, state: dict) -> None:
    """/enemies — view or set enemy roster."""
    combat = state["combat"].get(pid)
    if not combat or not combat.get("active"):
        tg.send_message(group_id, thread_id, "No active combat. Start with /combat")
        return

    if not args:
        # View enemies
        enemies = combat.get("enemies", [])
        if enemies:
            lines = [f"⚔️ Enemies in {campaign_name}:"]
            for e in enemies:
                lines.append(f"  • {e}")
            tg.send_message(group_id, thread_id, "\n".join(lines))
        else:
            tg.send_message(group_id, thread_id,
                            "No enemies listed. Use /enemies Ogre, Skeleton, etc.")
    else:
        # Set enemies
        enemies = [e.strip() for e in args.split(",") if e.strip()]
        combat["enemies"] = enemies
        lines = [f"⚔️ Updated enemies:"]
        for e in enemies:
            lines.append(f"  • {e}")
        tg.send_message(group_id, thread_id, "\n".join(lines))


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


def _format_log_entry(parsed: dict, gm_ids: set, char_name: str | None = None) -> str:
    """Format a single message as a markdown log line."""
    ts = parsed["msg_time_iso"][:19].replace("T", " ")  # 2026-02-26 14:30:05
    name = parsed["user_name"]
    last = parsed.get("user_last_name", "")
    if last:
        name = f"{name} {last}"

    is_gm = parsed["user_id"] in gm_ids
    role_tag = " [GM]" if is_gm else ""
    char_tag = f" ({char_name})" if char_name and not is_gm else ""

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

    return f"**{name}**{char_tag}{role_tag} ({ts}):\n{content}\n"


def _append_to_transcript(parsed: dict, gm_ids: set, config: dict | None = None) -> None:
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

    # Character name lookup
    char_name = None
    if config:
        char_name = helpers.character_name(config, parsed["pid"], parsed["user_id"])

    # Create header on first write
    is_new = not log_file.exists()

    with open(log_file, "a", encoding="utf-8") as f:
        if is_new:
            f.write(f"# {campaign_name} — {month_str}\n\n")
            f.write("*PBP transcript archived by PathWarsNudge bot.*\n\n---\n\n")

        entry = _format_log_entry(parsed, gm_ids, char_name)
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


def _handle_kick(pid: str, campaign_name: str, target: str,
                 state: dict, group_id: int, thread_id: int) -> None:
    """Remove a player from the campaign roster by username or name."""
    target_lower = target.lower()

    # Search for matching player in this campaign
    match_key = None
    match_player = None
    for key, player in state["players"].items():
        if not key.startswith(f"{pid}:"):
            continue
        username = player.get("username", "").lower()
        first = player.get("first_name", "").lower()
        full = f"{first} {player.get('last_name', '')}".strip().lower()

        if username == target_lower or first == target_lower or full == target_lower:
            match_key = key
            match_player = player
            break

    if not match_player:
        tg.send_message(group_id, thread_id,
                        f"No player matching '{target}' found in {campaign_name}.")
        return

    # Remove player
    removed = state["players"].pop(match_key)
    state["removed_players"][match_key] = {
        "removed_at": datetime.now(timezone.utc).isoformat(),
        "first_name": removed["first_name"],
        "username": removed.get("username", ""),
        "campaign_name": campaign_name,
        "kicked": True,
    }

    name = helpers.player_full_name(removed)
    tg.send_message(group_id, thread_id,
                    f"🚪 {name} has been removed from {campaign_name} tracking.\n"
                    f"They can rejoin by posting in PBP again.")
    print(f"Kicked {name} from {campaign_name}")


def _handle_addplayer(pid: str, campaign_name: str, raw_args: str,
                      now_iso: str, state: dict, group_id: int, thread_id: int) -> None:
    """Manually register a player who hasn't posted yet.

    Format: /addplayer @username FirstName [LastName]
    Creates a placeholder player entry. The username is stored as-is and
    updated with their real user_id when they first post.
    """
    parts = raw_args.split(None, 1)
    username = parts[0].lstrip("@") if parts else ""
    display_name = parts[1] if len(parts) > 1 else username

    if not username:
        tg.send_message(group_id, thread_id,
                        "Usage: /addplayer @username PlayerName")
        return

    # Check if player already exists in this campaign
    for key, player in state["players"].items():
        if not key.startswith(f"{pid}:"):
            continue
        if player.get("username", "").lower() == username.lower():
            tg.send_message(group_id, thread_id,
                            f"{display_name} (@{username}) is already tracked in {campaign_name}.")
            return

    # Use username as placeholder ID (will be replaced when they post)
    placeholder_id = f"pending_{username}"
    player_key = f"{pid}:{placeholder_id}"

    name_parts = display_name.split(None, 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    state["players"][player_key] = {
        "user_id": placeholder_id,
        "first_name": first_name,
        "last_name": last_name,
        "username": username,
        "campaign_name": campaign_name,
        "pbp_topic_id": pid,
        "last_post_time": now_iso,
        "last_warned_week": 0,
    }

    # Also clear from removed_players if they were previously removed
    for rkey in list(state["removed_players"].keys()):
        if rkey.startswith(f"{pid}:"):
            removed = state["removed_players"][rkey]
            if removed.get("username", "").lower() == username.lower():
                del state["removed_players"][rkey]
                break

    tg.send_message(group_id, thread_id,
                    f"✅ {display_name} (@{username}) added to {campaign_name} roster.\n"
                    f"Their tracking will update with full stats when they first post.")
    print(f"Added {display_name} (@{username}) to {campaign_name}")


def process_updates(updates: list, config: dict, state: dict) -> int:
    """Process new Telegram updates, tracking posts and handling commands. Returns new offset."""
    group_id = config["group_id"]

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

        # Per-campaign GM IDs (supports per-campaign overrides)
        gm_ids = helpers.gm_ids_for_campaign(config, pid)

        # ---- /help command ----
        if text in ("/help", "/pbphelp"):
            tg.send_message(group_id, thread_id, _HELP_TEXT)

        # ---- /status command ----
        if text == "/status":
            status = _build_status(pid, campaign_name, state, gm_ids)
            tg.send_message(group_id, thread_id, status)

        # ---- /overview command ----
        if text == "/overview":
            overview = _build_overview(config, state)
            tg.send_message(group_id, thread_id, overview)

        # ---- /campaign command ----
        if text == "/campaign":
            report = _build_campaign_report(pid, config, state, gm_ids)
            tg.send_message(group_id, thread_id, report)

        # ---- /mystats command ----
        if text in ("/mystats", "/me"):
            my_report = _build_mystats(pid, user_id, campaign_name, state, gm_ids, config)
            tg.send_message(group_id, thread_id, my_report)

        # ---- /whosturn command ----
        if text == "/whosturn":
            turn_report = _build_whosturn(pid, campaign_name, state)
            tg.send_message(group_id, thread_id, turn_report)

        # ---- /combatlog command (everyone) ----
        if text == "/combatlog":
            log_report = _build_combatlog(pid, campaign_name, state)
            tg.send_message(group_id, thread_id, log_report)

        # ---- /party command ----
        if text == "/party":
            party_report = _build_party(pid, campaign_name, config, state)
            tg.send_message(group_id, thread_id, party_report)

        # ---- /myhistory command ----
        if text == "/myhistory":
            history = _build_myhistory(pid, user_id, campaign_name, state, gm_ids)
            tg.send_message(group_id, thread_id, history)

        # ---- /catchup command ----
        if text == "/catchup":
            catchup = _build_catchup(pid, user_id, campaign_name, state, gm_ids)
            tg.send_message(group_id, thread_id, catchup)

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

        # ---- /kick command (GM only) ----
        if text.startswith("/kick") and user_id in gm_ids:
            target = parsed["raw_text"][5:].strip().lstrip("@")
            if not target:
                tg.send_message(group_id, thread_id,
                                "Usage: /kick @username or /kick PlayerName")
            else:
                _handle_kick(pid, campaign_name, target, state, group_id, thread_id)

        # ---- /addplayer command (GM only) ----
        if text.startswith("/addplayer") and user_id in gm_ids:
            raw_args = parsed["raw_text"][10:].strip()
            if not raw_args:
                tg.send_message(group_id, thread_id,
                                "Usage: /addplayer @username PlayerName\n"
                                "e.g. /addplayer @alice Alice Smith")
            else:
                _handle_addplayer(pid, campaign_name, raw_args, now_iso, state, group_id, thread_id)

        # ---- /scene command (GM only) ----
        if text.startswith("/scene") and user_id in gm_ids:
            scene_name = parsed["raw_text"][6:].strip()
            if not scene_name:
                tg.send_message(group_id, thread_id,
                                "Usage: /scene <name>\ne.g. /scene The Docks at Midnight")
            else:
                state.setdefault("current_scenes", {})[pid] = scene_name
                _write_scene_marker(campaign_name, scene_name)
                tg.send_message(group_id, thread_id,
                                f"🎭 Scene: {scene_name}\nMarked in transcript.")
                print(f"Scene marker in {campaign_name}: {scene_name}")

        # ---- /note command (GM only) ----
        if text.startswith("/note") and not text.startswith("/notes") and user_id in gm_ids:
            note_text = parsed["raw_text"][5:].strip()
            if not note_text:
                tg.send_message(group_id, thread_id,
                                "Usage: /note <text>\ne.g. /note Party agreed to meet the informant at dawn")
            else:
                notes = state.setdefault("campaign_notes", {}).setdefault(pid, [])
                if len(notes) >= _MAX_NOTES_PER_CAMPAIGN:
                    tg.send_message(group_id, thread_id,
                                    f"Maximum {_MAX_NOTES_PER_CAMPAIGN} notes reached. Use /delnote <N> to remove old ones.")
                else:
                    notes.append({"text": note_text, "created_at": now_iso})
                    tg.send_message(group_id, thread_id,
                                    f"📝 Note #{len(notes)} saved.")
                    print(f"Note added to {campaign_name}: {note_text[:50]}")

        # ---- /notes command (everyone) ----
        if text == "/notes":
            notes_report = _build_notes(pid, campaign_name, state)
            tg.send_message(group_id, thread_id, notes_report)

        # ---- /activity command (everyone) ----
        if text == "/activity":
            activity_report = _build_activity(pid, campaign_name, state, gm_ids)
            tg.send_message(group_id, thread_id, activity_report)

        # ---- /profile command (everyone) ----
        if text.startswith("/profile"):
            target = parsed["raw_text"][8:].strip()
            if not target:
                tg.send_message(group_id, thread_id,
                                "Usage: /profile @username or /profile PlayerName")
            else:
                profile = _build_profile(target, config, state)
                tg.send_message(group_id, thread_id, profile)

        # ---- /delnote command (GM only) ----
        if text.startswith("/delnote") and user_id in gm_ids:
            num_str = parsed["raw_text"][8:].strip()
            notes = state.get("campaign_notes", {}).get(pid, [])
            try:
                idx = int(num_str) - 1
                if 0 <= idx < len(notes):
                    removed = notes.pop(idx)
                    tg.send_message(group_id, thread_id,
                                    f"🗑️ Deleted note #{idx + 1}: {removed['text'][:60]}")
                    print(f"Note deleted from {campaign_name}: {removed['text'][:50]}")
                else:
                    tg.send_message(group_id, thread_id,
                                    f"Note #{num_str} not found. Use /notes to see current notes.")
            except (ValueError, TypeError):
                tg.send_message(group_id, thread_id,
                                "Usage: /delnote <number>\ne.g. /delnote 3")

        # ---- /quest command (GM only) ----
        if text.startswith("/quest") and not text.startswith("/quests") and user_id in gm_ids:
            quest_text = parsed["raw_text"][6:].strip()
            if not quest_text:
                tg.send_message(group_id, thread_id,
                                "Usage: /quest <text>\ne.g. /quest Find the missing merchant")
            else:
                quests = state.setdefault("quests", {}).setdefault(pid, [])
                if len(quests) >= _MAX_QUESTS_PER_CAMPAIGN:
                    tg.send_message(group_id, thread_id,
                                    f"Maximum {_MAX_QUESTS_PER_CAMPAIGN} quests reached. Use /delquest <N> to remove old ones.")
                else:
                    quests.append({"text": quest_text, "status": "active", "created_at": now_iso, "completed_at": None})
                    tg.send_message(group_id, thread_id,
                                    f"📋 Quest #{len(quests)} added: {quest_text}")
                    print(f"Quest added to {campaign_name}: {quest_text[:50]}")

        # ---- /quests command (everyone) ----
        if text == "/quests":
            quests_report = _build_quests(pid, campaign_name, state)
            tg.send_message(group_id, thread_id, quests_report)

        # ---- /done command (GM only) ----
        if text.startswith("/done") and user_id in gm_ids:
            num_str = parsed["raw_text"][5:].strip()
            quests = state.get("quests", {}).get(pid, [])
            try:
                idx = int(num_str) - 1
                if 0 <= idx < len(quests):
                    quests[idx]["status"] = "completed"
                    quests[idx]["completed_at"] = now_iso
                    tg.send_message(group_id, thread_id,
                                    f"✅ Quest #{idx + 1} completed: {quests[idx]['text']}")
                    print(f"Quest completed in {campaign_name}: {quests[idx]['text'][:50]}")
                else:
                    tg.send_message(group_id, thread_id,
                                    f"Quest #{num_str} not found. Use /quests to see current quests.")
            except (ValueError, TypeError):
                tg.send_message(group_id, thread_id,
                                "Usage: /done <number>\ne.g. /done 2")

        # ---- /delquest command (GM only) ----
        if text.startswith("/delquest") and user_id in gm_ids:
            num_str = parsed["raw_text"][9:].strip()
            quests = state.get("quests", {}).get(pid, [])
            try:
                idx = int(num_str) - 1
                if 0 <= idx < len(quests):
                    removed = quests.pop(idx)
                    tg.send_message(group_id, thread_id,
                                    f"🗑️ Deleted quest #{idx + 1}: {removed['text'][:60]}")
                    print(f"Quest deleted from {campaign_name}: {removed['text'][:50]}")
                else:
                    tg.send_message(group_id, thread_id,
                                    f"Quest #{num_str} not found. Use /quests to see current quests.")
            except (ValueError, TypeError):
                tg.send_message(group_id, thread_id,
                                "Usage: /delquest <number>\ne.g. /delquest 3")

        # ---- /gm command (GM only) ----
        if text == "/gm" and user_id in gm_ids:
            dashboard = _build_gm_dashboard(config, state)
            tg.send_message(group_id, thread_id, dashboard)

        # ---- /pin command (GM only) ----
        if text.startswith("/pin") and not text.startswith("/pins") and user_id in gm_ids:
            pin_text = parsed["raw_text"][4:].strip()
            if not pin_text:
                tg.send_message(group_id, thread_id,
                                "Usage: /pin <text>\ne.g. /pin The party discovered the hidden temple entrance")
            else:
                pins = state.setdefault("pins", {}).setdefault(pid, [])
                if len(pins) >= _MAX_PINS_PER_CAMPAIGN:
                    tg.send_message(group_id, thread_id,
                                    f"Maximum {_MAX_PINS_PER_CAMPAIGN} pins reached. Use /delpin <N> to remove old ones.")
                else:
                    pins.append({"text": pin_text, "created_at": now_iso, "author": user_name})
                    tg.send_message(group_id, thread_id,
                                    f"📌 Pin #{len(pins)} saved: {pin_text}")
                    print(f"Pin added to {campaign_name}: {pin_text[:50]}")

        # ---- /pins command (everyone) ----
        if text == "/pins":
            pins_report = _build_pins(pid, campaign_name, state)
            tg.send_message(group_id, thread_id, pins_report)

        # ---- /delpin command (GM only) ----
        if text.startswith("/delpin") and user_id in gm_ids:
            num_str = parsed["raw_text"][7:].strip()
            pins = state.get("pins", {}).get(pid, [])
            try:
                idx = int(num_str) - 1
                if 0 <= idx < len(pins):
                    removed = pins.pop(idx)
                    tg.send_message(group_id, thread_id,
                                    f"🗑️ Deleted pin #{idx + 1}: {removed['text'][:60]}")
                else:
                    tg.send_message(group_id, thread_id,
                                    f"Pin #{num_str} not found. Use /pins to see current pins.")
            except (ValueError, TypeError):
                tg.send_message(group_id, thread_id,
                                "Usage: /delpin <number>\ne.g. /delpin 3")

        # ---- /loot command (GM only) ----
        if text.startswith("/loot") and not text.startswith("/lootlist") and user_id in gm_ids:
            loot_text = parsed["raw_text"][5:].strip()
            if not loot_text:
                tg.send_message(group_id, thread_id,
                                "Usage: /loot <item>\ne.g. /loot +1 striking longsword")
            else:
                loot = state.setdefault("loot", {}).setdefault(pid, [])
                if len(loot) >= _MAX_LOOT_PER_CAMPAIGN:
                    tg.send_message(group_id, thread_id,
                                    f"Maximum {_MAX_LOOT_PER_CAMPAIGN} items. Use /delloot <N> to remove.")
                else:
                    loot.append({"text": loot_text, "added_at": now_iso})
                    tg.send_message(group_id, thread_id,
                                    f"💰 Loot #{len(loot)}: {loot_text}")
                    print(f"Loot added to {campaign_name}: {loot_text[:50]}")

        # ---- /lootlist command (everyone) ----
        if text == "/lootlist":
            loot_report = _build_lootlist(pid, campaign_name, state)
            tg.send_message(group_id, thread_id, loot_report)

        # ---- /delloot command (GM only) ----
        if text.startswith("/delloot") and user_id in gm_ids:
            num_str = parsed["raw_text"][8:].strip()
            loot = state.get("loot", {}).get(pid, [])
            try:
                idx = int(num_str) - 1
                if 0 <= idx < len(loot):
                    removed = loot.pop(idx)
                    tg.send_message(group_id, thread_id,
                                    f"🗑️ Removed loot #{idx + 1}: {removed['text'][:60]}")
                else:
                    tg.send_message(group_id, thread_id,
                                    f"Loot #{num_str} not found. Use /lootlist to see items.")
            except (ValueError, TypeError):
                tg.send_message(group_id, thread_id,
                                "Usage: /delloot <number>\ne.g. /delloot 3")

        # ---- /npc command (GM only) ----
        if text.startswith("/npc") and not text.startswith("/npcs") and user_id in gm_ids:
            raw_args = parsed["raw_text"][4:].strip()
            if not raw_args:
                tg.send_message(group_id, thread_id,
                                "Usage: /npc <name> — <description>\n"
                                "e.g. /npc Gorund — Dwarven blacksmith, owes party a favour")
            else:
                npcs = state.setdefault("npcs", {}).setdefault(pid, [])
                if len(npcs) >= _MAX_NPCS_PER_CAMPAIGN:
                    tg.send_message(group_id, thread_id,
                                    f"Maximum {_MAX_NPCS_PER_CAMPAIGN} NPCs. Use /delnpc <N> to remove.")
                else:
                    # Split on em-dash or double-hyphen
                    if " — " in raw_args:
                        name, desc = raw_args.split(" — ", 1)
                    elif " -- " in raw_args:
                        name, desc = raw_args.split(" -- ", 1)
                    elif " - " in raw_args:
                        name, desc = raw_args.split(" - ", 1)
                    else:
                        name, desc = raw_args, ""
                    npcs.append({"name": name.strip(), "desc": desc.strip(), "added_at": now_iso})
                    tg.send_message(group_id, thread_id,
                                    f"🎭 NPC #{len(npcs)}: {name.strip()}")
                    print(f"NPC added to {campaign_name}: {name.strip()[:50]}")

        # ---- /npcs command (everyone) ----
        if text == "/npcs":
            npcs_report = _build_npcs(pid, campaign_name, state)
            tg.send_message(group_id, thread_id, npcs_report)

        # ---- /delnpc command (GM only) ----
        if text.startswith("/delnpc") and user_id in gm_ids:
            num_str = parsed["raw_text"][7:].strip()
            npcs = state.get("npcs", {}).get(pid, [])
            try:
                idx = int(num_str) - 1
                if 0 <= idx < len(npcs):
                    removed = npcs.pop(idx)
                    tg.send_message(group_id, thread_id,
                                    f"🗑️ Removed NPC #{idx + 1}: {removed['name']}")
                else:
                    tg.send_message(group_id, thread_id,
                                    f"NPC #{num_str} not found. Use /npcs to see the list.")
            except (ValueError, TypeError):
                tg.send_message(group_id, thread_id,
                                "Usage: /delnpc <number>\ne.g. /delnpc 3")

        # ---- /condition command (GM only) ----
        if text.startswith("/condition") and not text.startswith("/conditions") and user_id in gm_ids:
            raw_args = parsed["raw_text"][10:].strip()
            if not raw_args:
                tg.send_message(group_id, thread_id,
                                "Usage: /condition <target> — <effect> [| duration]\n"
                                "e.g. /condition Cardigan — Frightened 2 | until end of next turn\n"
                                "e.g. /condition All — Inspired +1")
            else:
                # Parse: target — effect [| duration]
                if " — " in raw_args:
                    target, rest = raw_args.split(" — ", 1)
                elif " -- " in raw_args:
                    target, rest = raw_args.split(" -- ", 1)
                elif " - " in raw_args:
                    target, rest = raw_args.split(" - ", 1)
                else:
                    target, rest = raw_args, ""

                if "|" in rest:
                    effect, duration = rest.split("|", 1)
                else:
                    effect, duration = rest, ""

                conds = state.setdefault("conditions", {}).setdefault(pid, [])
                conds.append({
                    "target": target.strip(),
                    "effect": effect.strip(),
                    "duration": duration.strip(),
                    "added_at": now_iso,
                })
                tg.send_message(group_id, thread_id,
                                f"⚡ Condition on {target.strip()}: {effect.strip()}")
                print(f"Condition in {campaign_name}: {target.strip()} — {effect.strip()[:50]}")

        # ---- /conditions command (everyone) ----
        if text == "/conditions":
            conds_report = _build_conditions(pid, campaign_name, state, config)
            tg.send_message(group_id, thread_id, conds_report)

        # ---- /endcondition command (GM only) ----
        if text.startswith("/endcondition") and user_id in gm_ids:
            num_str = parsed["raw_text"][13:].strip()
            conds = state.get("conditions", {}).get(pid, [])
            try:
                idx = int(num_str) - 1
                if 0 <= idx < len(conds):
                    removed = conds.pop(idx)
                    tg.send_message(group_id, thread_id,
                                    f"✅ Ended: {removed['target']} — {removed['effect']}")
                else:
                    tg.send_message(group_id, thread_id,
                                    f"Condition #{num_str} not found. Use /conditions to see list.")
            except (ValueError, TypeError):
                tg.send_message(group_id, thread_id,
                                "Usage: /endcondition <number>\ne.g. /endcondition 2")

        # ---- /clearconditions command (GM only) ----
        if text == "/clearconditions" and user_id in gm_ids:
            old = state.get("conditions", {}).get(pid, [])
            count = len(old)
            state.setdefault("conditions", {})[pid] = []
            tg.send_message(group_id, thread_id,
                            f"✅ Cleared {count} condition{'s' if count != 1 else ''} from {campaign_name}.")

        # ---- /hp command (GM set/damage/heal/remove/clear, everyone view) ----
        if text.startswith("/hp"):
            hp_args = parsed["raw_text"][3:].strip()
            hp_tracker = state.setdefault("hp_tracker", {}).setdefault(pid, {})

            if not hp_args or hp_args == "show":
                # View HP tracker
                report = _build_hp_tracker(pid, campaign_name, state)
                tg.send_message(group_id, thread_id, report)

            elif user_id in gm_ids:
                parts = hp_args.split(None, 1)
                sub = parts[0].lower()
                rest = parts[1] if len(parts) > 1 else ""

                if sub == "set":
                    # /hp set <name> <current>/<max>
                    set_parts = rest.rsplit(None, 1)
                    if len(set_parts) == 2 and "/" in set_parts[1]:
                        name = set_parts[0].strip()
                        try:
                            cur, mx = set_parts[1].split("/", 1)
                            cur, mx = int(cur), int(mx)
                            if mx <= 0 or mx > 9999:
                                tg.send_message(group_id, thread_id, "Max HP must be 1–9999.")
                            elif len(hp_tracker) >= _MAX_HP_ENTRIES and name not in hp_tracker:
                                tg.send_message(group_id, thread_id,
                                                f"Max {_MAX_HP_ENTRIES} entries. Use /hp remove <name> first.")
                            else:
                                hp_tracker[name] = {"current": min(cur, mx), "max": mx}
                                icon = helpers.hp_status_icon(min(cur, mx), mx)
                                bar = helpers.hp_bar(min(cur, mx), mx)
                                tg.send_message(group_id, thread_id,
                                                f"{icon} {name}: {bar}")
                        except ValueError:
                            tg.send_message(group_id, thread_id,
                                            "Usage: /hp set <name> <current>/<max>\ne.g. /hp set Ogre 45/45")
                    else:
                        tg.send_message(group_id, thread_id,
                                        "Usage: /hp set <name> <current>/<max>\ne.g. /hp set Ogre 45/45")

                elif sub in ("d", "damage"):
                    # /hp d <name> <amount>
                    dmg_parts = rest.rsplit(None, 1)
                    if len(dmg_parts) == 2:
                        name = dmg_parts[0].strip()
                        try:
                            amount = int(dmg_parts[1])
                            if name in hp_tracker:
                                hp = hp_tracker[name]
                                hp["current"] = max(0, hp["current"] - amount)
                                icon = helpers.hp_status_icon(hp["current"], hp["max"])
                                bar = helpers.hp_bar(hp["current"], hp["max"])
                                status = " 💀 DOWN!" if hp["current"] == 0 else ""
                                tg.send_message(group_id, thread_id,
                                                f"{icon} {name} takes {amount} damage!\n{bar}{status}")
                            else:
                                tg.send_message(group_id, thread_id,
                                                f"No HP entry for '{name}'. Use /hp set {name} <hp>/<max> first.")
                        except ValueError:
                            tg.send_message(group_id, thread_id,
                                            "Usage: /hp d <name> <amount>\ne.g. /hp d Ogre 12")
                    else:
                        tg.send_message(group_id, thread_id,
                                        "Usage: /hp d <name> <amount>\ne.g. /hp d Ogre 12")

                elif sub in ("h", "heal"):
                    # /hp h <name> <amount>
                    heal_parts = rest.rsplit(None, 1)
                    if len(heal_parts) == 2:
                        name = heal_parts[0].strip()
                        try:
                            amount = int(heal_parts[1])
                            if name in hp_tracker:
                                hp = hp_tracker[name]
                                hp["current"] = min(hp["max"], hp["current"] + amount)
                                icon = helpers.hp_status_icon(hp["current"], hp["max"])
                                bar = helpers.hp_bar(hp["current"], hp["max"])
                                tg.send_message(group_id, thread_id,
                                                f"{icon} {name} healed {amount}!\n{bar}")
                            else:
                                tg.send_message(group_id, thread_id,
                                                f"No HP entry for '{name}'. Use /hp set {name} <hp>/<max> first.")
                        except ValueError:
                            tg.send_message(group_id, thread_id,
                                            "Usage: /hp h <name> <amount>\ne.g. /hp h Ogre 10")
                    else:
                        tg.send_message(group_id, thread_id,
                                        "Usage: /hp h <name> <amount>\ne.g. /hp h Ogre 10")

                elif sub == "remove":
                    name = rest.strip()
                    if name in hp_tracker:
                        del hp_tracker[name]
                        tg.send_message(group_id, thread_id, f"🗑️ Removed {name} from HP tracker.")
                    else:
                        tg.send_message(group_id, thread_id,
                                        f"No HP entry for '{name}'. Use /hp to see entries.")

                elif sub == "clear":
                    count = len(hp_tracker)
                    state["hp_tracker"][pid] = {}
                    tg.send_message(group_id, thread_id,
                                    f"✅ Cleared {count} HP entr{'ies' if count != 1 else 'y'}.")

                else:
                    tg.send_message(group_id, thread_id,
                                    "Usage: /hp set <n> <cur>/<max> | /hp d <n> <amt> | "
                                    "/hp h <n> <amt> | /hp remove <n> | /hp clear")

        # ---- /clock command (GM only) ----
        if text.startswith("/clock") and not text.startswith("/clocks") and user_id in gm_ids:
            clock_args = parsed["raw_text"][6:].strip()
            if not clock_args:
                tg.send_message(group_id, thread_id,
                                "Usage: /clock <name> <segments>\ne.g. /clock Investigation 6\ne.g. /clock Ritual 4")
            else:
                clock_parts = clock_args.rsplit(None, 1)
                if len(clock_parts) == 2:
                    name = clock_parts[0].strip()
                    try:
                        segments = int(clock_parts[1])
                        if segments < 2 or segments > 12:
                            tg.send_message(group_id, thread_id, "Segments must be 2–12.")
                        else:
                            clocks = state.setdefault("clocks", {}).setdefault(pid, {})
                            if len(clocks) >= _MAX_CLOCKS and name not in clocks:
                                tg.send_message(group_id, thread_id,
                                                f"Max {_MAX_CLOCKS} clocks. Use /delclock <name> first.")
                            else:
                                clocks[name] = {"filled": 0, "segments": segments}
                                display = helpers.clock_display(0, segments)
                                tg.send_message(group_id, thread_id,
                                                f"⏱️ Clock: {name}\n{display}")
                    except ValueError:
                        tg.send_message(group_id, thread_id,
                                        "Usage: /clock <name> <segments>\ne.g. /clock Investigation 6")
                else:
                    tg.send_message(group_id, thread_id,
                                    "Usage: /clock <name> <segments>\ne.g. /clock Investigation 6")

        # ---- /clocks command (everyone) ----
        if text == "/clocks":
            clocks_report = _build_clocks(pid, campaign_name, state)
            tg.send_message(group_id, thread_id, clocks_report)

        # ---- /tick command (GM only) ----
        if text.startswith("/tick") and not text.startswith("/ticker") and user_id in gm_ids:
            tick_args = parsed["raw_text"][5:].strip()
            clocks = state.get("clocks", {}).get(pid, {})
            if not tick_args:
                tg.send_message(group_id, thread_id,
                                "Usage: /tick <name> [N]\ne.g. /tick Investigation 2")
            else:
                tick_parts = tick_args.rsplit(None, 1)
                amount = 1
                name = tick_args
                if len(tick_parts) == 2:
                    try:
                        amount = int(tick_parts[1])
                        name = tick_parts[0]
                    except ValueError:
                        name = tick_args
                        amount = 1
                name = name.strip()
                if name in clocks:
                    clock = clocks[name]
                    clock["filled"] = min(clock["segments"], clock["filled"] + amount)
                    display = helpers.clock_display(clock["filled"], clock["segments"])
                    complete = " ✅ COMPLETE!" if clock["filled"] >= clock["segments"] else ""
                    tg.send_message(group_id, thread_id,
                                    f"⏱️ {name}\n{display}{complete}")
                else:
                    tg.send_message(group_id, thread_id,
                                    f"No clock named '{name}'. Use /clocks to see all.")

        # ---- /untick command (GM only) ----
        if text.startswith("/untick") and user_id in gm_ids:
            tick_args = parsed["raw_text"][7:].strip()
            clocks = state.get("clocks", {}).get(pid, {})
            if not tick_args:
                tg.send_message(group_id, thread_id,
                                "Usage: /untick <name> [N]\ne.g. /untick Investigation 1")
            else:
                tick_parts = tick_args.rsplit(None, 1)
                amount = 1
                name = tick_args
                if len(tick_parts) == 2:
                    try:
                        amount = int(tick_parts[1])
                        name = tick_parts[0]
                    except ValueError:
                        name = tick_args
                        amount = 1
                name = name.strip()
                if name in clocks:
                    clock = clocks[name]
                    clock["filled"] = max(0, clock["filled"] - amount)
                    display = helpers.clock_display(clock["filled"], clock["segments"])
                    tg.send_message(group_id, thread_id, f"⏱️ {name}\n{display}")
                else:
                    tg.send_message(group_id, thread_id,
                                    f"No clock named '{name}'. Use /clocks to see all.")

        # ---- /delclock command (GM only) ----
        if text.startswith("/delclock") and user_id in gm_ids:
            name = parsed["raw_text"][9:].strip()
            clocks = state.get("clocks", {}).get(pid, {})
            if name in clocks:
                del clocks[name]
                tg.send_message(group_id, thread_id, f"🗑️ Removed clock: {name}")
            else:
                tg.send_message(group_id, thread_id,
                                f"No clock named '{name}'. Use /clocks to see all.")

        # ---- /vote command (GM only) ----
        if text.startswith("/vote") and not text.startswith("/votes") and user_id in gm_ids:
            raw_args = parsed["raw_text"][5:].strip()
            if not raw_args:
                tg.send_message(group_id, thread_id,
                                "Usage: /vote <question> | <option1> | <option2> [| ...]\n"
                                "e.g. /vote Where do we go? | North gate | Sewers | Stay and rest")
            else:
                parts = [p.strip() for p in raw_args.split("|")]
                if len(parts) < 3:
                    tg.send_message(group_id, thread_id,
                                    "Need a question and at least 2 options, separated by |\n"
                                    "e.g. /vote Left or right? | Left | Right")
                else:
                    question = parts[0]
                    options = parts[1:]
                    if len(options) > 6:
                        tg.send_message(group_id, thread_id, "Maximum 6 options per vote.")
                    else:
                        state.setdefault("votes", {})[pid] = {
                            "question": question,
                            "options": options,
                            "results": {str(i): [] for i in range(1, len(options) + 1)},
                            "closed": False,
                            "created_at": now_iso,
                        }
                        # Build display
                        option_lines = "\n".join(f"  {i}. {opt}" for i, opt in enumerate(options, 1))
                        tg.send_message(group_id, thread_id,
                                        f"🗳️ Vote started!\n\n❓ {question}\n\n{option_lines}\n\n"
                                        f"Use /pick <N> to cast your vote.")
                        print(f"Vote started in {campaign_name}: {question}")

        # ---- /pick command (everyone) ----
        if text.startswith("/pick"):
            pick_str = parsed["raw_text"][5:].strip()
            vote = state.get("votes", {}).get(pid)
            if not vote or vote.get("closed"):
                tg.send_message(group_id, thread_id, "No active vote. GMs can start one with /vote")
            else:
                try:
                    choice = int(pick_str)
                    if 1 <= choice <= len(vote["options"]):
                        # Remove previous vote by this user
                        for key in vote["results"]:
                            vote["results"][key] = [n for n in vote["results"][key] if n != user_name]
                        # Add new vote
                        vote["results"][str(choice)].append(user_name)
                        tg.send_message(group_id, thread_id,
                                        f"✅ {user_name} voted for: {vote['options'][choice - 1]}")
                    else:
                        tg.send_message(group_id, thread_id,
                                        f"Pick a number 1–{len(vote['options'])}.")
                except (ValueError, TypeError):
                    tg.send_message(group_id, thread_id,
                                    f"Usage: /pick <number>\ne.g. /pick 2")

        # ---- /showvote command (everyone) ----
        if text == "/showvote":
            vote_report = _build_vote(pid, campaign_name, state)
            tg.send_message(group_id, thread_id, vote_report)

        # ---- /endvote command (GM only) ----
        if text == "/endvote" and user_id in gm_ids:
            vote = state.get("votes", {}).get(pid)
            if not vote or vote.get("closed"):
                tg.send_message(group_id, thread_id, "No active vote to close.")
            else:
                vote["closed"] = True
                # Find winner
                results = vote["results"]
                best_count = max(len(v) for v in results.values())
                total = sum(len(v) for v in results.values())
                winners = [vote["options"][int(k) - 1] for k, v in results.items() if len(v) == best_count]

                lines = [f"🗳️ Vote closed — {vote['question']}", ""]
                for i, option in enumerate(vote["options"], 1):
                    voters = results.get(str(i), [])
                    count = len(voters)
                    marker = " 👑" if count == best_count and count > 0 else ""
                    voter_names = ", ".join(voters) if voters else "—"
                    lines.append(f"  {i}. {option}: {count} ({voter_names}){marker}")
                lines.append("")
                if len(winners) == 1:
                    lines.append(f"Winner: {winners[0]} ({best_count}/{total} votes)")
                elif best_count > 0:
                    lines.append(f"Tied: {', '.join(winners)} ({best_count} each)")
                else:
                    lines.append("No votes were cast.")
                tg.send_message(group_id, thread_id, "\n".join(lines))

        # ---- /timer command (GM only) ----
        if text.startswith("/timer") and not text.startswith("/timers") and user_id in gm_ids:
            raw_args = parsed["raw_text"][6:].strip()
            if not raw_args:
                tg.send_message(group_id, thread_id,
                                "Usage: /timer <duration> [reason]\n"
                                "e.g. /timer 24h Post your combat actions\n"
                                "e.g. /timer 2d\n"
                                "Durations: Nh (hours), Nm (minutes), Nd (days)")
            else:
                now_dt = datetime.fromisoformat(now_iso)
                deadline, reason = helpers.parse_timer_duration(raw_args, now_dt)
                if deadline is None:
                    tg.send_message(group_id, thread_id,
                                    "Couldn't parse duration. Use Nh, Nm, or Nd.\n"
                                    "e.g. /timer 24h Post your actions")
                else:
                    state.setdefault("timers", {})[pid] = {
                        "deadline": deadline.isoformat(),
                        "reason": reason,
                        "set_at": now_iso,
                        "set_by": user_name,
                    }
                    time_fmt = deadline.strftime("%b %d %H:%M UTC")
                    reason_str = f"\n📝 {reason}" if reason else ""
                    tg.send_message(group_id, thread_id,
                                    f"⏳ Timer set! Deadline: {time_fmt}{reason_str}\n"
                                    f"Use /showtimer to check remaining time.")
                    print(f"Timer set in {campaign_name}: deadline {time_fmt}")

        # ---- /showtimer command (everyone) ----
        if text == "/showtimer":
            timer_report = _build_timer(pid, campaign_name, state)
            tg.send_message(group_id, thread_id, timer_report)

        # ---- /canceltimer command (GM only) ----
        if text == "/canceltimer" and user_id in gm_ids:
            if state.get("timers", {}).get(pid):
                del state["timers"][pid]
                tg.send_message(group_id, thread_id, f"⏳ Timer cancelled for {campaign_name}.")
            else:
                tg.send_message(group_id, thread_id, "No active timer to cancel.")

        # ---- /summary command (everyone) ----
        if text == "/summary":
            summary = _build_summary(pid, campaign_name, state, config)
            tg.send_message(group_id, thread_id, summary)

        # ---- /dc command (everyone) ----
        if text.startswith("/dc"):
            dc_query = parsed["raw_text"][3:].strip()
            result = helpers.dc_lookup(dc_query)
            tg.send_message(group_id, thread_id, result)

        # ---- /away command (everyone) ----
        if text.startswith("/away"):
            args = parsed["raw_text"][5:].strip()
            now_dt = datetime.fromisoformat(now_iso)
            until_dt, reason = helpers.parse_away_duration(args, now_dt)
            away_key = f"{pid}:{user_id}"
            state.setdefault("away", {})[away_key] = {
                "until": until_dt.isoformat() if until_dt else None,
                "reason": reason,
                "set_at": now_iso,
            }
            if until_dt:
                until_str = until_dt.strftime("%b %d")
                msg = f"✈️ {user_name} marked as away until {until_str}.\nReason: {reason}"
            else:
                msg = f"✈️ {user_name} marked as away (indefinite).\nReason: {reason}"
            msg += "\nUse /back when you return."
            print(f"Away: {user_name} in {campaign_name} — {reason}")
            tg.send_message(group_id, thread_id, msg)

        # ---- /back command (everyone) ----
        if text == "/back":
            away_key = f"{pid}:{user_id}"
            if away_key in state.get("away", {}):
                del state["away"][away_key]
                char_name = helpers.character_name(config, pid, user_id)
                char_tag = f" ({char_name})" if char_name else ""
                tg.send_message(group_id, thread_id,
                                f"👋 {user_name}{char_tag} is back!")
                print(f"Back: {user_name} in {campaign_name}")
            else:
                tg.send_message(group_id, thread_id,
                                f"You're not currently marked as away.")

        # ---- /recap command (everyone) ----
        if text.startswith("/recap"):
            args = parsed["raw_text"][6:].strip()
            try:
                count = int(args) if args else 10
            except ValueError:
                count = 10
            recap = _build_recap(pid, campaign_name, config, count)
            tg.send_message(group_id, thread_id, recap)

        # ---- /roll command (everyone) ----
        if text.startswith("/roll"):
            dice_expr = parsed["raw_text"][5:].strip()
            if not dice_expr:
                tg.send_message(group_id, thread_id,
                                "Usage: /roll <dice> [label]\n"
                                "e.g. /roll 1d20+5 Stealth\n"
                                "e.g. /roll 2d6+3\n"
                                "e.g. /roll 4d6kh3 (keep highest 3)")
            else:
                result = helpers.roll_dice(dice_expr)
                if result.get("error"):
                    tg.send_message(group_id, thread_id, result["error"])
                else:
                    char_name = helpers.character_name(config, pid, user_id)
                    roller = char_name or user_name
                    label = result["label"]

                    lines = []
                    grand_total = 0
                    for r in result["results"]:
                        grand_total += r["total"]
                        lines.append(f"  {r['expr']}: {r['detail']} = {r['total']}")

                    header = f"🎲 {roller}"
                    if label:
                        header += f" — {label}"
                    header += ":"

                    if len(result["results"]) == 1:
                        r = result["results"][0]
                        msg = f"{header}\n  {r['detail']} = {r['total']}"
                    else:
                        msg = header + "\n" + "\n".join(lines) + f"\n  Total: {grand_total}"

                    tg.send_message(group_id, thread_id, msg)

        # ---- Combat commands and tracking ----
        _handle_combat_message(
            text, parsed["raw_text"], user_id, user_name, gm_ids, pid, campaign_name,
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

        # Track word count (measures RP engagement depth, not just frequency)
        raw_text = parsed["raw_text"] or ""
        word_count = len(raw_text.split()) if raw_text.strip() else 0
        user_words = state.setdefault("word_counts", {}).setdefault(pid, {})
        user_words[user_id] = user_words.get(user_id, 0) + word_count

        # Track post timestamps for Player of the Week gap calculation
        state["post_timestamps"].setdefault(pid, {}).setdefault(user_id, []).append(msg_time_iso)

        # Track activity patterns (persistent hour/day counters)
        msg_dt = datetime.fromisoformat(msg_time_iso)
        hour_key = str(msg_dt.hour)
        day_key = str(msg_dt.weekday())  # 0=Mon, 6=Sun
        user_hours = state.setdefault("activity_hours", {}).setdefault(pid, {}).setdefault(user_id, {})
        user_hours[hour_key] = user_hours.get(hour_key, 0) + 1
        user_days = state.setdefault("activity_days", {}).setdefault(pid, {}).setdefault(user_id, {})
        user_days[day_key] = user_days.get(day_key, 0) + 1

        # Update player-level tracking (skip GM)
        if user_id and user_id not in gm_ids:
            # Auto-clear away status when player posts (non-command only)
            if not text.startswith("/"):
                away_key = f"{pid}:{user_id}"
                if away_key in state.get("away", {}):
                    del state["away"][away_key]
                    print(f"Auto-cleared away for {user_name} in {campaign_name} (posted)")

            player_key = f"{pid}:{user_id}"
            was_removed = player_key in state["removed_players"]
            old_player = state.get("players", {}).get(player_key, {})
            old_warn_level = old_player.get("last_warned_week", 0)

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
                # Welcome back notification
                char_name = helpers.character_name(config, pid, user_id)
                char_tag = f" ({char_name})" if char_name else ""
                tg.send_message(
                    group_id, chat_topic_id,
                    f"👋 {user_name}{char_tag} is back in {campaign_name}!"
                )
            elif old_warn_level >= 2:
                # Player was warned for 2+ weeks of inactivity — acknowledge return
                print(f"Warned player {user_name} returned to {campaign_name} (was week {old_warn_level})")
                char_name = helpers.character_name(config, pid, user_id)
                char_tag = f" as {char_name}" if char_name else ""
                tg.send_message(
                    group_id, chat_topic_id,
                    f"🎉 {user_name} is back{char_tag}! Good to see you."
                )

        # Log to persistent PBP transcript
        if not text.startswith("/"):
            _append_to_transcript(parsed, gm_ids, config)

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
        # Skip players who are marked as away
        user_id = player.get("user_id", "")
        if helpers.is_away(state, pbp_topic_id, user_id, now):
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

    for pid, chat_topic_id in maps.to_chat.items():
        if not helpers.feature_enabled(config, pid, "roster"):
            continue
        if not helpers.interval_elapsed(state["last_roster"].get(pid), helpers.ROSTER_INTERVAL_DAYS, now):
            continue

        gm_ids = helpers.gm_ids_for_campaign(config, pid)

        name = maps.to_name.get(pid, "Unknown")
        players = campaigns.get(pid, [])
        counts = state.get("message_counts", {}).get(pid, {})
        topic_timestamps = helpers.get_topic_timestamps(state, pid)

        if not players and not counts:
            continue

        lines = []
        characters = helpers.get_characters(config, pid)

        for player in sorted(players, key=lambda p: counts.get(p["user_id"], 0), reverse=True):
            uid = player["user_id"]
            raw_ts = topic_timestamps.get(uid, [])
            if not raw_ts:
                continue
            full = helpers.player_full_name(player)
            char_name = characters.get(uid)
            label = f"{full} ({char_name})" if char_name else full
            stats = _roster_user_stats(raw_ts, counts.get(uid, 0), now)
            lines.append(_roster_block(label, player.get("username", ""), stats))

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
        gm_ids = helpers.gm_ids_for_campaign(config, pid)

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
        acted_raw = combat.get("players_acted", {})
        acted = set(acted_raw.keys()) if isinstance(acted_raw, dict) else set(acted_raw)
        missing = [
            helpers.player_mention(p)
            for p in all_campaigns.get(pid, [])
            if p["user_id"] not in acted
            and not helpers.is_away(state, pid, p["user_id"], now)
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
        gm_ids = helpers.gm_ids_for_campaign(config, pid)

        gm_posts = 0
        player_posts = 0
        player_counts = {}
        player_post_times = []
        player_details = {}  # name -> {posts, sessions (unique days), timestamps}

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
                    # Collect per-player detail
                    unique_days = len({ts.date() for ts in user_sessions})
                    p_gap = helpers.avg_gap_hours(sorted(user_sessions))
                    player_details[p_name] = {
                        "posts": session_count,
                        "sessions": unique_days,
                        "avg_gap_h": round(p_gap, 1) if p_gap is not None else None,
                        "words": state.get("word_counts", {}).get(pid, {}).get(uid, 0),
                    }

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
            "total_words": sum(state.get("word_counts", {}).get(pid, {}).values()),
            "top_players": dict(sorted(
                player_counts.items(), key=lambda x: x[1], reverse=True
            )[:5]),
            "player_breakdown": player_details,
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
        gm_ids = helpers.gm_ids_for_campaign(config, pid)

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

        this_week_num = f"W{now.isocalendar()[1]:02d}"
        last_week_num = f"W{week_ago.isocalendar()[1]:02d}"

        message = (
            f"{icon} Weekly pace for {name}:\n"
            f"\n"
            f"This week {this_week_num} ({this_week_start} to {this_week_end}):\n"
            f"  GM: {gm_this} posts ({gm_this / 7.0:.1f}/day)\n"
            f"  Players: {player_this} posts ({player_this / 7.0:.1f}/day)\n"
            f"  Total: {this_week} posts ({this_avg:.1f}/day)\n"
            f"\n"
            f"Last week {last_week_num} ({last_week_start} to {last_week_end}):\n"
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

    maps = maps or build_topic_maps(config)
    celebrated = state.setdefault("celebrated_streaks", {})

    for pid, chat_topic_id in maps.to_chat.items():
        name = maps.to_name.get(pid, "Unknown")
        topic_ts = helpers.get_topic_timestamps(state, pid)
        gm_ids = helpers.gm_ids_for_campaign(config, pid)

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
#  Message milestones (every 500 per campaign, every 5000 global)
# ------------------------------------------------------------------ #
_CAMPAIGN_MILESTONE_STEP = 500
_GLOBAL_MILESTONE_STEP = 5000

_MILESTONE_ICONS = {
    500: "🎯", 1000: "🏅", 1500: "⚡", 2000: "🔥", 2500: "⭐",
    3000: "💎", 3500: "🌟", 4000: "👑", 4500: "🏆", 5000: "🎆",
}


def check_message_milestones(config: dict, state: dict, *, now: datetime | None = None, maps=None, **_kw) -> None:
    """Celebrate when a campaign or the global total crosses a message milestone."""
    group_id = config["group_id"]
    maps = maps or build_topic_maps(config)
    celebrated = state.setdefault("celebrated_milestones", {})

    global_total = 0

    for pid, name in maps.to_name.items():
        # Count total messages for this campaign
        counts = state.get("message_counts", {}).get(pid, {})
        campaign_total = sum(counts.values())
        global_total += campaign_total

        if campaign_total < _CAMPAIGN_MILESTONE_STEP:
            continue

        # Find highest milestone crossed
        milestone = (campaign_total // _CAMPAIGN_MILESTONE_STEP) * _CAMPAIGN_MILESTONE_STEP

        campaign_key = f"campaign:{pid}"
        last_celebrated = celebrated.get(campaign_key, 0)

        if milestone > last_celebrated:
            icon = _MILESTONE_ICONS.get(milestone, "🎯")
            chat_topic_id = maps.to_chat.get(pid)
            if chat_topic_id:
                message = (
                    f"{icon} {name} has hit {milestone:,} PBP messages!\n\n"
                    f"That's {milestone:,} posts of collaborative storytelling. "
                    f"Every single one moved the story forward."
                )
                if tg.send_message(group_id, chat_topic_id, message):
                    celebrated[campaign_key] = milestone
                    print(f"Milestone: {name} hit {milestone:,} messages")

    # Global milestone
    if global_total >= _GLOBAL_MILESTONE_STEP:
        global_milestone = (global_total // _GLOBAL_MILESTONE_STEP) * _GLOBAL_MILESTONE_STEP
        last_global = celebrated.get("global", 0)

        if global_milestone > last_global:
            leaderboard_topic = config.get("leaderboard_topic_id")
            if leaderboard_topic:
                message = (
                    f"🎆 Path Wars has hit {global_milestone:,} total PBP messages "
                    f"across all campaigns!\n\n"
                    f"That's {global_milestone:,} posts of adventure, intrigue, "
                    f"and terrible puns spread across {len(maps.to_name)} campaigns."
                )
                if tg.send_message(group_id, leaderboard_topic, message):
                    celebrated["global"] = global_milestone
                    print(f"Global milestone: {global_milestone:,} total messages")


# ------------------------------------------------------------------ #
#  Campaign Leaderboard (cross-campaign dashboard)
# ------------------------------------------------------------------ #
def _gather_leaderboard_stats(config: dict, state: dict, now: datetime) -> tuple[list, dict, list]:
    """Collect per-campaign stats, global player rankings, and top streaks for the leaderboard."""
    seven_days_ago = now - timedelta(days=7)
    three_days_ago = now - timedelta(days=3)
    six_days_ago = now - timedelta(days=6)

    campaign_stats = []
    global_player_posts = {}
    all_streaks = []

    maps = build_topic_maps(config)

    for pid, name in maps.to_name.items():
        topic_timestamps = helpers.get_topic_timestamps(state, pid)
        gm_ids = helpers.gm_ids_for_campaign(config, pid)

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
    maps = build_topic_maps(config)
    week_ago = now - timedelta(days=7)

    campaign_lines = []
    all_campaigns = helpers.players_by_campaign(state)

    for pid, name in maps.to_name.items():
        topic_ts = helpers.get_topic_timestamps(state, pid)
        gm_ids = helpers.gm_ids_for_campaign(config, pid)
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
#  Smart alerts: pace drop & conversation dying
# ------------------------------------------------------------------ #
def check_pace_drop(config: dict, state: dict, *, now: datetime | None = None, maps=None) -> None:
    """Alert when a campaign's weekly posts drop >40% vs the previous week.

    Checks once per week (tied to archive cadence). Sends a gentle nudge
    to the campaign's chat topic so the GM is aware without being pushy.
    """
    group_id = config["group_id"]
    now = now or datetime.now(timezone.utc)
    maps = maps or build_topic_maps(config)

    # Only run on archive day (weekly)
    if not helpers.interval_elapsed(state.get("last_pace_drop_check"), 7, now):
        return

    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    alerts_sent = False
    for pid, chat_topic_id in maps.to_chat.items():
        if not helpers.feature_enabled(config, pid, "smart_alerts"):
            continue

        name = maps.to_name.get(pid, "Unknown")
        topic_timestamps = helpers.get_topic_timestamps(state, pid)
        gm_ids = helpers.gm_ids_for_campaign(config, pid)

        if not topic_timestamps:
            continue

        pace = helpers.pace_split(topic_timestamps, gm_ids, now)
        this_week = pace["gm_this"] + pace["player_this"]
        last_week = pace["gm_last"] + pace["player_last"]

        # Skip if last week had very few posts (avoid noisy alerts)
        if last_week < 5:
            continue

        if this_week == 0 and last_week > 0:
            drop_pct = 100
        elif last_week > 0:
            drop_pct = ((last_week - this_week) / last_week) * 100
        else:
            continue

        if drop_pct > 40:
            message = (
                f"📉 Pace check for {name}:\n"
                f"\n"
                f"Posts dropped from {last_week} last week to {this_week} "
                f"this week ({drop_pct:.0f}% decrease).\n"
                f"\n"
                f"Just a heads-up — no action needed if the break is "
                f"intentional."
            )
            print(f"Pace drop alert for {name}: {last_week} -> {this_week} ({drop_pct:.0f}%)")
            tg.send_message(group_id, chat_topic_id, message)
            alerts_sent = True

    state["last_pace_drop_check"] = now.isoformat()
    if not alerts_sent:
        print("Pace drop check: no significant drops detected")


def check_conversation_dying(config: dict, state: dict, *, now: datetime | None = None, maps=None) -> None:
    """Warn when ALL participants (including GM) are silent for 48h+.

    Distinct from the 4-hour nudge (which just prompts the next post) — this
    fires once when a campaign crosses the 48h threshold, suggesting the
    campaign may need attention or a deliberate pause.
    """
    group_id = config["group_id"]
    now = now or datetime.now(timezone.utc)
    maps = maps or build_topic_maps(config)
    threshold = timedelta(hours=48)

    state.setdefault("dying_alerts_sent", {})

    for pid, chat_topic_id in maps.to_chat.items():
        if not helpers.feature_enabled(config, pid, "smart_alerts"):
            continue
        # Skip paused campaigns — they're intentionally quiet
        if state.get("paused", {}).get(pid):
            continue

        name = maps.to_name.get(pid, "Unknown")
        topic_timestamps = helpers.get_topic_timestamps(state, pid)

        if not topic_timestamps:
            continue

        # Find the most recent post from ANYONE
        latest = None
        for uid, timestamps in topic_timestamps.items():
            for ts in timestamps:
                if latest is None or ts > latest:
                    latest = ts

        if latest is None:
            continue

        try:
            latest_dt = datetime.fromisoformat(latest)
        except (TypeError, ValueError):
            continue

        silence_hours = (now - latest_dt).total_seconds() / 3600.0

        if silence_hours >= threshold.total_seconds() / 3600.0:
            # Only alert once per silence period
            if state["dying_alerts_sent"].get(pid) == "active":
                continue

            days_silent = silence_hours / 24.0
            message = (
                f"💤 {name} has been completely silent for "
                f"{days_silent:.1f} days.\n"
                f"\n"
                f"No posts from anyone — GM or players — since "
                f"{latest_dt.strftime('%b %d')}."
            )
            print(f"Conversation dying alert for {name}: {days_silent:.1f} days silent")
            if tg.send_message(group_id, chat_topic_id, message):
                state["dying_alerts_sent"][pid] = "active"
        else:
            # Reset the flag when activity resumes
            if state["dying_alerts_sent"].get(pid):
                del state["dying_alerts_sent"][pid]


def check_expired_timers(config: dict, state: dict, *, now: datetime | None = None, maps=None) -> None:
    """Check for expired timers and post notifications."""
    if not maps:
        maps = build_topic_maps(config)
    if not now:
        now = datetime.now(timezone.utc)

    group_id = config.get("group_id")
    for pid, timer in list(state.get("timers", {}).items()):
        deadline = datetime.fromisoformat(timer["deadline"])
        if now >= deadline:
            # Check if we already notified
            if timer.get("notified"):
                continue

            chat_topic_id = maps.to_chat.get(pid)
            if not chat_topic_id:
                continue
            campaign_name = maps.to_name.get(pid, pid)
            reason = timer.get("reason", "")
            reason_str = f"\n📝 {reason}" if reason else ""

            tg.send_message(group_id, chat_topic_id,
                            f"⏰ Timer expired for {campaign_name}!{reason_str}\n"
                            f"GMs: /canceltimer to clear.")
            timer["notified"] = True
            print(f"Timer expired in {campaign_name}")


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
        ("Message milestones", check_message_milestones),
        ("Combat pings", check_combat_turns),
        ("Leaderboard", post_campaign_leaderboard),
        ("Weekly digest", post_weekly_digest),
        ("Recruitment", check_recruitment_needs),
        ("Archive", archive_weekly_data),
        ("Pace drop", check_pace_drop),
        ("Conversation dying", check_conversation_dying),
        ("Timer expiry", check_expired_timers),
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
