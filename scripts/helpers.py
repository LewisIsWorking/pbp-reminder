"""Shared utilities, constants, and config loading."""

import json
from datetime import datetime
from pathlib import Path

# ------------------------------------------------------------------ #
#  Paths
# ------------------------------------------------------------------ #
CONFIG_PATH = Path(__file__).parent.parent / "config.json"
BOONS_PATH = Path(__file__).parent.parent / "boons.json"
ARCHIVE_PATH = Path(__file__).parent.parent / "data" / "weekly_archive.json"

# ------------------------------------------------------------------ #
#  Tunable settings (defaults, overridden by config.json settings block)
# ------------------------------------------------------------------ #
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
POST_SESSION_MINUTES = 10

MECHANICAL_BOONS = [
    "+1 circumstance bonus on your next skill check.",
    "Recover 1d6 extra HP during your next rest.",
    "Your next critical failure on a skill check is a regular failure instead.",
    "Gain a +1 circumstance bonus to initiative in your next combat.",
    "+1 circumstance bonus to your next saving throw.",
    "Your next successful Strike deals 1 extra damage.",
    "Gain 1 temporary HP at the start of your next combat.",
    "Your next Recall Knowledge check gains a +2 circumstance bonus.",
    "+10 feet to your Speed for your first turn of your next combat.",
    "The DC of your next skill check is reduced by 1.",
]


# ------------------------------------------------------------------ #
#  Config loading
# ------------------------------------------------------------------ #
def load_config() -> dict:
    """Load and return the config.json file."""
    with open(CONFIG_PATH) as f:
        return json.load(f)


_SETTINGS_MAP = {
    "player_warn_weeks": "PLAYER_WARN_WEEKS",
    "player_remove_weeks": "PLAYER_REMOVE_WEEKS",
    "roster_interval_days": "ROSTER_INTERVAL_DAYS",
    "potw_interval_days": "POTW_INTERVAL_DAYS",
    "potw_min_posts": "POTW_MIN_POSTS",
    "pace_interval_days": "PACE_INTERVAL_DAYS",
    "leaderboard_interval_days": "LEADERBOARD_INTERVAL_DAYS",
    "combat_ping_hours": "COMBAT_PING_HOURS",
    "recruitment_interval_days": "RECRUITMENT_INTERVAL_DAYS",
    "required_players": "REQUIRED_PLAYERS",
    "post_session_minutes": "POST_SESSION_MINUTES",
}


def load_settings(config: dict) -> None:
    """Load tunable settings from config, applying defaults for any missing keys."""
    g = globals()
    s = config.get("settings", {})
    for config_key, global_name in _SETTINGS_MAP.items():
        if config_key in s:
            g[global_name] = s[config_key]


def gm_id_set(config: dict) -> set:
    """Return GM user IDs as a set of strings."""
    return set(str(uid) for uid in config.get("gm_user_ids", []))


def interval_elapsed(last_iso: str | None, interval_days: float, now: datetime) -> bool:
    """Return True if enough time has passed since last_iso, or if last_iso is None."""
    if not last_iso:
        return True
    return days_since(now, datetime.fromisoformat(last_iso)) >= interval_days


def timestamps_in_window(raw_timestamps: list[str], after: datetime,
                         before: datetime | None = None) -> list[datetime]:
    """Parse ISO timestamp strings and return those within the time window.

    Returns datetimes where: after <= dt (and dt < before, if given).
    """
    results = []
    for ts in raw_timestamps:
        dt = datetime.fromisoformat(ts)
        if dt >= after and (before is None or dt < before):
            results.append(dt)
    return results


def avg_gap_hours(sorted_times: list[datetime]) -> float | None:
    """Return average gap in hours between sorted datetimes, or None if < 2 entries."""
    if len(sorted_times) < 2:
        return None
    gaps = [(sorted_times[i] - sorted_times[i - 1]).total_seconds() / 3600
            for i in range(1, len(sorted_times))]
    return sum(gaps) / len(gaps)


def fmt_brief_relative(now: datetime, then: datetime | None) -> tuple[str, float]:
    """Short relative time (no date). Returns (string, days_since).

    Used by leaderboard for compact display: 'today', '5h ago', 'yesterday', '3d ago', 'never'.
    """
    if not then:
        return "never", 999.0
    d = days_since(now, then)
    if d < 0.04:  # ~1 hour
        return "today", d
    elif d < 1:
        return f"{int(d * 24)}h ago", d
    elif d < 2:
        return "yesterday", d
    else:
        return f"{int(d)}d ago", d


def trend_icon(recent: int, previous: int) -> str:
    """Return trend emoji comparing recent vs previous period post counts."""
    if previous == 0 and recent == 0:
        return "💤"
    elif previous == 0:
        return "🆕"
    elif recent > previous * 1.15:
        return "📈"
    elif recent < previous * 0.85:
        return "📉"
    else:
        return "➡️"


def players_by_campaign(state: dict) -> dict:
    """Group active players by canonical topic ID. Returns {pid: [player_dict, ...]}."""
    campaigns = {}
    for player_key, player in state.get("players", {}).items():
        pid = player["pbp_topic_id"]
        campaigns.setdefault(pid, []).append(player)
    return campaigns


def get_topic_timestamps(state: dict, pid: str) -> dict:
    """Get per-user timestamp dict for a campaign. Returns {uid: [iso_str, ...]}."""
    return state.get("post_timestamps", {}).get(pid, {})


def get_player(state: dict, pid: str, uid: str) -> dict:
    """Look up a player dict by campaign and user ID. Returns {} if not found."""
    return state.get("players", {}).get(f"{pid}:{uid}", {})


RANK_ICONS = ["🥇", "🥈", "🥉"]


def rank_icon(index: int) -> str:
    """Return medal emoji for top 3, or 'N.' for the rest."""
    return RANK_ICONS[index] if index < 3 else f"{index + 1}."


# ------------------------------------------------------------------ #
#  Formatting helpers
# ------------------------------------------------------------------ #
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


def hours_since(now: datetime, then: datetime) -> float:
    """Return fractional hours between two datetimes."""
    return (now - then).total_seconds() / 3600


def days_since(now: datetime, then: datetime) -> float:
    """Return fractional days between two datetimes."""
    return (now - then).total_seconds() / 86400


def display_name(first_name: str, username: str = "", last_name: str = "") -> str:
    """Format a player name as 'First Last (@username)' or 'First Last' or 'First'."""
    full = f"{first_name} {last_name}".strip() if last_name else first_name
    if username:
        return f"{full} (@{username})"
    return full


def player_mention(player: dict) -> str:
    """Extract display_name from a player state dict."""
    return display_name(
        player.get("first_name", "Unknown"),
        player.get("username", ""),
        player.get("last_name", ""),
    )


def player_full_name(player: dict) -> str:
    """Extract 'First Last' from a player dict, without @username."""
    first = player.get("first_name", "Unknown")
    last = player.get("last_name", "")
    return f"{first} {last}".strip() if last else first


def posts_str(n: int) -> str:
    """Return '1 post' or 'N posts'."""
    return f"{n} post" if n == 1 else f"{n} posts"


def fmt_relative_date(now: datetime, then: datetime) -> str:
    """Format as relative + absolute, e.g. '5d ago (2026-02-10)'."""
    d = int(days_since(now, then))
    date_str = fmt_date(then)
    if d == 0:
        return f"today ({date_str})"
    elif d == 1:
        return f"yesterday ({date_str})"
    else:
        return f"{d}d ago ({date_str})"


# ------------------------------------------------------------------ #
#  Post deduplication and gap calculation
# ------------------------------------------------------------------ #
def deduplicate_posts(timestamps: list[datetime]) -> list[datetime]:
    """Collapse posts within POST_SESSION_MINUTES into single sessions.

    Returns the timestamp of the first post in each session.
    """
    if not timestamps:
        return []
    sorted_ts = sorted(timestamps)
    sessions = [sorted_ts[0]]
    for ts in sorted_ts[1:]:
        if (ts - sessions[-1]).total_seconds() > POST_SESSION_MINUTES * 60:
            sessions.append(ts)
    return sessions


def calc_avg_gap_str(timestamps_iso: list[str]) -> str:
    """Calculate deduped average gap from ISO timestamp strings. Returns formatted string."""
    all_posts = sorted(datetime.fromisoformat(ts) for ts in timestamps_iso)
    sessions = deduplicate_posts(all_posts)
    avg = avg_gap_hours(sessions)
    if avg is None:
        return "N/A"
    if avg < 1:
        return f"{avg * 60:.0f} minutes"
    return f"{avg:.1f} hours"


# ------------------------------------------------------------------ #
#  Topic mapping (multi-topic campaign support)
# ------------------------------------------------------------------ #
class TopicMaps:
    """Lookup container for campaign topic ID mappings."""
    __slots__ = ("to_canonical", "to_chat", "to_name", "all_pbp_ids")

    def __init__(self, to_canonical, to_chat, to_name, all_pbp_ids):
        self.to_canonical = to_canonical  # any pbp_topic_id (str) -> canonical pid
        self.to_chat = to_chat            # canonical pid -> chat_topic_id
        self.to_name = to_name            # canonical pid -> campaign name
        self.all_pbp_ids = all_pbp_ids    # set of all pbp topic id strings


_topic_maps_cache = (None, None)  # (config_id, TopicMaps)


def build_topic_maps(config: dict) -> TopicMaps:
    """Build lookup dicts from config's topic_pairs. Cached per config object."""
    global _topic_maps_cache
    if _topic_maps_cache[0] == id(config):
        return _topic_maps_cache[1]

    to_canonical = {}
    to_chat = {}
    to_name = {}
    all_pbp_ids = set()
    for pair in config["topic_pairs"]:
        ids = pair["pbp_topic_ids"]
        canonical = str(ids[0])
        to_chat[canonical] = pair["chat_topic_id"]
        to_name[canonical] = pair["name"]
        for tid in ids:
            tid_str = str(tid)
            to_canonical[tid_str] = canonical
            all_pbp_ids.add(tid_str)
    result = TopicMaps(to_canonical, to_chat, to_name, all_pbp_ids)
    _topic_maps_cache = (id(config), result)
    return result
