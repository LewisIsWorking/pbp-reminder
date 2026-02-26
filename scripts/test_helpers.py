"""Tests for helpers.py utilities."""

import sys
from datetime import datetime, timezone, timedelta

import helpers


def _utc(*args):
    """Shorthand for timezone-aware UTC datetime."""
    return datetime(*args, tzinfo=timezone.utc)


# ------------------------------------------------------------------ #
#  Time math
# ------------------------------------------------------------------ #
def test_hours_since():
    now = _utc(2026, 1, 10, 12, 0)
    then = _utc(2026, 1, 10, 6, 30)
    assert abs(helpers.hours_since(now, then) - 5.5) < 0.001


def test_days_since():
    now = _utc(2026, 1, 10, 12, 0)
    then = _utc(2026, 1, 7, 12, 0)
    assert helpers.days_since(now, then) == 3.0


def test_interval_elapsed_none():
    assert helpers.interval_elapsed(None, 7, _utc(2026, 1, 10, 12, 0)) is True


def test_interval_elapsed_fresh():
    now = _utc(2026, 1, 10, 12, 0)
    last = _utc(2026, 1, 10, 10, 0).isoformat()
    assert helpers.interval_elapsed(last, 1, now) is False


def test_interval_elapsed_stale():
    now = _utc(2026, 1, 10, 12, 0)
    last = _utc(2026, 1, 8, 12, 0).isoformat()
    assert helpers.interval_elapsed(last, 1, now) is True


# ------------------------------------------------------------------ #
#  Timestamp filtering
# ------------------------------------------------------------------ #
def test_timestamps_in_window_after_only():
    now = _utc(2026, 1, 10, 12, 0)
    cutoff = now - timedelta(hours=24)
    timestamps = [
        (now - timedelta(hours=h)).isoformat()
        for h in [1, 5, 25, 50]
    ]
    result = helpers.timestamps_in_window(timestamps, cutoff)
    assert len(result) == 2


def test_timestamps_in_window_bounded():
    now = _utc(2026, 1, 10, 12, 0)
    after = now - timedelta(hours=48)
    before = now - timedelta(hours=24)
    timestamps = [
        (now - timedelta(hours=h)).isoformat()
        for h in [1, 30, 50]
    ]
    result = helpers.timestamps_in_window(timestamps, after, before)
    assert len(result) == 1


def test_timestamps_in_window_empty():
    assert helpers.timestamps_in_window([], _utc(2026, 1, 1, 0, 0)) == []


# ------------------------------------------------------------------ #
#  Gap calculation
# ------------------------------------------------------------------ #
def test_avg_gap_hours_basic():
    times = [
        _utc(2026, 1, 10, 0, 0),
        _utc(2026, 1, 10, 6, 0),
        _utc(2026, 1, 10, 12, 0),
    ]
    assert helpers.avg_gap_hours(times) == 6.0


def test_avg_gap_hours_insufficient():
    assert helpers.avg_gap_hours([_utc(2026, 1, 10, 0, 0)]) is None
    assert helpers.avg_gap_hours([]) is None


def test_calc_avg_gap_str():
    now = _utc(2026, 1, 10, 12, 0)
    timestamps = [
        (now - timedelta(hours=h)).isoformat()
        for h in [0, 6, 12]
    ]
    result = helpers.calc_avg_gap_str(timestamps)
    assert "6.0 hours" == result


def test_calc_avg_gap_str_insufficient():
    assert helpers.calc_avg_gap_str([]) == "N/A"
    assert helpers.calc_avg_gap_str([_utc(2026, 1, 1, 0, 0).isoformat()]) == "N/A"


# ------------------------------------------------------------------ #
#  Post deduplication
# ------------------------------------------------------------------ #
def test_deduplicate_posts_within_session():
    base = _utc(2026, 1, 10, 12, 0)
    posts = [
        base,
        base + timedelta(minutes=3),
        base + timedelta(minutes=8),
    ]
    sessions = helpers.deduplicate_posts(posts)
    assert len(sessions) == 1


def test_deduplicate_posts_across_sessions():
    base = _utc(2026, 1, 10, 12, 0)
    posts = [
        base,
        base + timedelta(minutes=5),
        base + timedelta(minutes=30),  # New session
        base + timedelta(hours=2),     # New session
    ]
    sessions = helpers.deduplicate_posts(posts)
    assert len(sessions) == 3


def test_deduplicate_posts_empty():
    assert helpers.deduplicate_posts([]) == []


# ------------------------------------------------------------------ #
#  Formatting
# ------------------------------------------------------------------ #
def test_fmt_date():
    assert helpers.fmt_date(_utc(2026, 2, 14, 0, 0)) == "2026-02-14"


def test_html_escape():
    assert helpers.html_escape("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_posts_str():
    assert helpers.posts_str(1) == "1 post"
    assert helpers.posts_str(0) == "0 posts"
    assert helpers.posts_str(5) == "5 posts"


def test_display_name():
    assert helpers.display_name("Alice") == "Alice"
    assert helpers.display_name("Alice", last_name="B") == "Alice B"
    assert helpers.display_name("Alice", "alice_b", "B") == "Alice B (@alice_b)"


def test_player_mention():
    p = {"first_name": "Bob", "last_name": "S", "username": "bobs"}
    assert helpers.player_mention(p) == "Bob S (@bobs)"
    assert helpers.player_mention({}) == "Unknown"


def test_player_full_name():
    p = {"first_name": "Bob", "last_name": "S", "username": "bobs"}
    assert helpers.player_full_name(p) == "Bob S"
    assert helpers.player_full_name({"first_name": "Bob"}) == "Bob"
    assert helpers.player_full_name({}) == "Unknown"


def test_rank_icon():
    assert helpers.rank_icon(0) == "🥇"
    assert helpers.rank_icon(2) == "🥉"
    assert helpers.rank_icon(3) == "4."
    assert helpers.rank_icon(9) == "10."


def test_trend_icon():
    assert helpers.trend_icon(0, 0) == "💤"
    assert helpers.trend_icon(10, 0) == "🆕"
    assert helpers.trend_icon(20, 10) == "📈"
    assert helpers.trend_icon(5, 10) == "📉"
    assert helpers.trend_icon(10, 10) == "➡️"


def test_fmt_relative_date():
    now = _utc(2026, 1, 10, 12, 0)
    assert "today" in helpers.fmt_relative_date(now, now - timedelta(hours=5))
    assert "yesterday" in helpers.fmt_relative_date(now, now - timedelta(hours=30))
    assert "3d ago" in helpers.fmt_relative_date(now, now - timedelta(days=3))


def test_fmt_brief_relative():
    now = _utc(2026, 1, 10, 12, 0)
    s, d = helpers.fmt_brief_relative(now, None)
    assert s == "never" and d == 999.0

    s, _ = helpers.fmt_brief_relative(now, now - timedelta(minutes=30))
    assert s == "today"

    s, _ = helpers.fmt_brief_relative(now, now - timedelta(hours=5))
    assert "h ago" in s

    s, _ = helpers.fmt_brief_relative(now, now - timedelta(hours=30))
    assert s == "yesterday"

    s, _ = helpers.fmt_brief_relative(now, now - timedelta(days=5))
    assert "5d ago" == s


# ------------------------------------------------------------------ #
#  State lookups
# ------------------------------------------------------------------ #
def test_gm_id_set():
    config = {"gm_user_ids": [123, 456]}
    result = helpers.gm_id_set(config)
    assert result == {"123", "456"}
    assert helpers.gm_id_set({}) == set()


def test_gm_ids_for_campaign_global_fallback():
    config = {
        "gm_user_ids": [111],
        "topic_pairs": [
            {"name": "A", "chat_topic_id": 10, "pbp_topic_ids": [100]},
        ],
    }
    # No per-campaign override, falls back to global
    assert helpers.gm_ids_for_campaign(config, "100") == {"111"}


def test_gm_ids_for_campaign_per_campaign_override():
    config = {
        "gm_user_ids": [111],
        "topic_pairs": [
            {"name": "A", "chat_topic_id": 10, "pbp_topic_ids": [100]},
            {"name": "B", "chat_topic_id": 20, "pbp_topic_ids": [200], "gm_user_ids": [222]},
        ],
    }
    # Campaign A uses global
    assert helpers.gm_ids_for_campaign(config, "100") == {"111"}
    # Campaign B uses its own override
    assert helpers.gm_ids_for_campaign(config, "200") == {"222"}
    # Global GM NOT in campaign B's set
    assert "111" not in helpers.gm_ids_for_campaign(config, "200")


def test_gm_ids_for_campaign_unknown_pid():
    config = {
        "gm_user_ids": [111],
        "topic_pairs": [
            {"name": "A", "chat_topic_id": 10, "pbp_topic_ids": [100]},
        ],
    }
    # Unknown pid falls back to global
    assert helpers.gm_ids_for_campaign(config, "999") == {"111"}


def test_players_by_campaign():
    state = {"players": {
        "A:1": {"pbp_topic_id": "A", "name": "p1"},
        "A:2": {"pbp_topic_id": "A", "name": "p2"},
        "B:3": {"pbp_topic_id": "B", "name": "p3"},
    }}
    result = helpers.players_by_campaign(state)
    assert len(result["A"]) == 2
    assert len(result["B"]) == 1


def test_get_topic_timestamps():
    state = {"post_timestamps": {"A": {"1": ["ts1", "ts2"]}}}
    assert helpers.get_topic_timestamps(state, "A") == {"1": ["ts1", "ts2"]}
    assert helpers.get_topic_timestamps(state, "Z") == {}
    assert helpers.get_topic_timestamps({}, "A") == {}


def test_get_player():
    state = {"players": {"A:1": {"first_name": "Test"}}}
    assert helpers.get_player(state, "A", "1")["first_name"] == "Test"
    assert helpers.get_player(state, "A", "9") == {}
    assert helpers.get_player({}, "A", "1") == {}


# ------------------------------------------------------------------ #
#  Topic maps
# ------------------------------------------------------------------ #
def test_build_topic_maps_basic():
    config = {"topic_pairs": [
        {"pbp_topic_ids": [100, 101], "chat_topic_id": 200, "name": "Campaign A"},
        {"pbp_topic_ids": [300], "chat_topic_id": 400, "name": "Campaign B"},
    ]}
    maps = helpers.build_topic_maps(config)
    assert maps.to_canonical["100"] == "100"
    assert maps.to_canonical["101"] == "100"
    assert maps.to_canonical["300"] == "300"
    assert maps.to_chat["100"] == 200
    assert maps.to_name["100"] == "Campaign A"
    assert "101" in maps.all_pbp_ids
    assert "300" in maps.all_pbp_ids


def test_build_topic_maps_caching():
    config = {"topic_pairs": [
        {"pbp_topic_ids": [1], "chat_topic_id": 2, "name": "Test"},
    ]}
    m1 = helpers.build_topic_maps(config)
    m2 = helpers.build_topic_maps(config)
    assert m1 is m2


def test_pace_split():
    now = datetime(2025, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
    topic_ts = {
        "42": [  # Player
            (now - timedelta(hours=h)).isoformat()
            for h in [2, 24, 48, 200]  # 3 this week, 1 last week
        ],
        "999": [  # GM
            (now - timedelta(hours=h)).isoformat()
            for h in [1, 12, 168 + 12]  # 2 this week, 1 last week
        ],
    }
    result = helpers.pace_split(topic_ts, {"999"}, now)
    assert result["player_this"] == 3
    assert result["player_last"] == 1
    assert result["gm_this"] == 2
    assert result["gm_last"] == 1


def test_changelog_read_latest_entry():
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from post_changelog import read_latest_entry, markdown_to_telegram

    # Create a minimal test changelog
    test_path = Path("/tmp/test_changelog.md")
    test_path.write_text(
        "# Changelog\n\n"
        "## [2.0.0] - 2026-03-01\n\n"
        "### Added\n- New feature\n\n"
        "## [1.0.0] - 2026-02-26\n\n"
        "### Added\n- Old feature\n"
    )
    header, body = read_latest_entry(test_path)
    assert "2.0.0" in header
    assert "New feature" in body
    assert "Old feature" not in body
    test_path.unlink()


def test_changelog_markdown_to_telegram():
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from post_changelog import markdown_to_telegram

    msg = markdown_to_telegram("## [1.0.0] - 2026-02-26", "### Added\n- **Bold item**: description\n- `code` thing")
    assert "<b>PBP Reminder Bot v1.0.0</b>" in msg
    assert "<b>Added</b>" in msg
    assert "<b>Bold item</b>" in msg
    assert "<code>code</code>" in msg


# ------------------------------------------------------------------ #
#  Runner
# ------------------------------------------------------------------ #
def _run_all():
    """Find and run all test_ functions, report results."""
    tests = [(name, obj) for name, obj in globals().items()
             if name.startswith("test_") and callable(obj)]
    passed = failed = 0
    for name, func in sorted(tests):
        try:
            func()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  FAIL: {name}: {e}")
    print(f"\n{passed} passed, {failed} failed out of {passed + failed}")
    return failed


if __name__ == "__main__":
    sys.exit(_run_all())
