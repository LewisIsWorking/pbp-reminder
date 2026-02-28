"""Tests for checker.py logic.

Uses a lightweight mock for the telegram module so no real API calls are made.
"""

import sys
import types
from datetime import datetime, timezone, timedelta

# ------------------------------------------------------------------ #
#  Mock telegram module before importing checker
# ------------------------------------------------------------------ #
_sent_messages = []
_mock_tg = types.ModuleType("telegram")
_mock_tg.TELEGRAM_API = ""


def _mock_init(token):
    pass


def _mock_send(group_id, topic_id, text, parse_mode=None):
    _sent_messages.append({"group_id": group_id, "topic_id": topic_id, "text": text})
    return True


def _mock_send_buttons(group_id, topic_id, text, buttons):
    _sent_messages.append({"group_id": group_id, "topic_id": topic_id, "text": text, "buttons": buttons})
    return 99999


def _mock_edit(chat_id, message_id, text, parse_mode=None):
    _sent_messages.append({"chat_id": chat_id, "message_id": message_id, "text": text})
    return True


def _mock_answer(cb_id, text):
    _sent_messages.append({"cb_id": cb_id, "text": text})
    return True


def _mock_get_updates(offset):
    return []


_mock_tg.init = _mock_init
_mock_tg.send_message = _mock_send
_mock_tg.send_message_with_buttons = _mock_send_buttons
_mock_tg.edit_message = _mock_edit
_mock_tg.answer_callback = _mock_answer
_mock_tg.get_updates = _mock_get_updates
sys.modules["telegram"] = _mock_tg

import checker
import helpers


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def _reset():
    _sent_messages.clear()


# Redirect transcript logging to temp dir (so tests don't write to repo)
import tempfile as _tempfile
_test_log_dir = _tempfile.mkdtemp()
checker._LOGS_DIR = __import__("pathlib").Path(_test_log_dir)

# Redirect archive to temp file so tests don't write to repo
helpers.ARCHIVE_PATH = __import__("pathlib").Path(_test_log_dir) / "weekly_archive.json"


def _make_config(pairs=None, gm_ids=None):
    return {
        "group_id": -100,
        "alert_after_hours": 4,
        "gm_user_ids": gm_ids or [999],
        "leaderboard_topic_id": None,
        "topic_pairs": pairs or [
            {"name": "TestCampaign", "chat_topic_id": 200, "pbp_topic_ids": [100]},
        ],
    }


def _make_state():
    return {
        "offset": 0,
        "topics": {},
        "players": {},
        "message_counts": {},
        "post_timestamps": {},
        "last_alerts": {},
        "last_roster": {},
        "last_potw": {},
        "last_pace": {},
        "last_leaderboard": None,
        "last_recruitment_check": {},
        "last_anniversary": {},
        "combat": {},
        "removed_players": {},
        "pending_potw_boons": {},
    }


def _make_msg(update_id, topic_id, text, user_id=42, first_name="TestPlayer",
              username="tp", last_name="", group_id=-100, date_ts=None):
    """Convenience factory for a Telegram update dict."""
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": group_id},
            "message_thread_id": topic_id,
            "from": {
                "id": user_id,
                "first_name": first_name,
                "last_name": last_name,
                "username": username,
            },
            "date": date_ts or int(datetime.now(timezone.utc).timestamp()),
            "text": text,
        },
    }


# ------------------------------------------------------------------ #
#  Pure function tests
# ------------------------------------------------------------------ #
def test_format_boon_result():
    _reset()
    boons = ["Boon A", "Boon B", "Boon C"]
    result = checker._format_boon_result(boons, 1, "Winner!", "Chosen boon")
    assert "✓" in result
    assert "<s>" in result
    assert "Boon B" in result.split("✓")[0]  # Chosen boon before checkmark
    assert "Chosen boon:" in result


def test_format_boon_result_html_escapes():
    _reset()
    boons = ["<script>", "Normal"]
    result = checker._format_boon_result(boons, 0, "Test & Win", "Label")
    assert "&lt;script&gt;" in result
    assert "Test &amp; Win" in result


def test_roster_user_stats():
    now = _utc(2026, 2, 20, 12, 0)
    # 4 posts: now, 6h ago, 2d ago, 10d ago
    timestamps = [
        now.isoformat(),
        (now - timedelta(hours=6)).isoformat(),
        (now - timedelta(days=2)).isoformat(),
        (now - timedelta(days=10)).isoformat(),
    ]
    stats = checker._roster_user_stats(timestamps, 20, now)
    assert stats["total"] == 20
    assert stats["sessions"] >= 3  # 3+ sessions after dedup
    assert stats["week_count"] >= 2  # At least 2 posts in the last week
    assert "hours" in stats["avg_gap_str"] or "minutes" in stats["avg_gap_str"]
    assert "today" in stats["last_post_str"]


def test_roster_block():
    stats = {
        "total": 15,
        "sessions": 5,
        "week_count": 3,
        "avg_gap_str": "24.0 hours",
        "last_post_str": "today (2026-02-20)",
    }
    block = checker._roster_block("Alice", "alice123", stats)
    assert "Alice" in block
    assert "@alice123" in block
    assert "15 posts total" in block
    assert "5 posting sessions" in block
    assert "3 posts in the last week" in block
    assert "24.0 hours" in block


def test_roster_block_no_username():
    stats = {"total": 1, "sessions": 1, "week_count": 0, "avg_gap_str": "N/A", "last_post_str": "N/A"}
    block = checker._roster_block("Bob", "", stats)
    assert "Bob" in block
    assert "@" not in block
    assert "1 posting session." in block  # Singular


def test_gather_potw_candidates():
    now = _utc(2026, 2, 20, 12, 0)
    week_ago = now - timedelta(days=7)
    # Player with 6 sessions this week
    timestamps = {
        "player1": [(now - timedelta(hours=h)).isoformat() for h in [2, 14, 26, 38, 50, 62]],
        "gm999": [(now - timedelta(hours=h)).isoformat() for h in [1, 3, 5]],  # GM
    }
    state = _make_state()
    state["players"]["100:player1"] = {
        "first_name": "Alice", "last_name": "B", "username": "alice",
        "pbp_topic_id": "100", "user_id": "player1", "campaign_name": "Test",
        "last_post_time": now.isoformat(), "last_warned_week": 0,
    }
    candidates = checker._gather_potw_candidates(timestamps, {"gm999"}, week_ago, "100", state)
    assert len(candidates) == 1
    assert candidates[0]["user_id"] == "player1"
    assert candidates[0]["first_name"] == "Alice"
    assert candidates[0]["avg_gap_hours"] > 0


def test_gather_potw_excludes_low_posts():
    now = _utc(2026, 2, 20, 12, 0)
    week_ago = now - timedelta(days=7)
    # Only 2 posts (below default POTW_MIN_POSTS of 5)
    timestamps = {
        "player1": [(now - timedelta(hours=h)).isoformat() for h in [2, 50]],
    }
    state = _make_state()
    state["players"]["100:player1"] = {
        "first_name": "Bob", "last_name": "", "username": "",
        "pbp_topic_id": "100", "user_id": "player1", "campaign_name": "Test",
        "last_post_time": now.isoformat(), "last_warned_week": 0,
    }
    candidates = checker._gather_potw_candidates(timestamps, set(), week_ago, "100", state)
    assert len(candidates) == 0


def test_cleanup_timestamps_prunes_old():
    now = datetime.now(timezone.utc)
    state = _make_state()
    state["post_timestamps"] = {
        "100": {
            "user1": [
                (now - timedelta(days=1)).isoformat(),   # Keep
                (now - timedelta(days=20)).isoformat(),  # Prune
            ],
            "user2": [
                (now - timedelta(days=30)).isoformat(),  # Prune (user removed entirely)
            ],
        }
    }
    checker.cleanup_timestamps(state)
    assert len(state["post_timestamps"]["100"]["user1"]) == 1
    assert "user2" not in state["post_timestamps"]["100"]


def test_cleanup_timestamps_empty_state():
    state = _make_state()
    checker.cleanup_timestamps(state)  # Should not crash


def test_format_leaderboard():
    now = _utc(2026, 2, 20, 12, 0)
    campaign_stats = [
        {
            "name": "Alpha",
            "total_7d": 30,
            "player_7d": 20,
            "gm_7d": 10,
            "trend_icon": "📈",
            "avg_gap_str": "4.0h",
            "player_avg_gap": 5.0,
            "player_avg_gap_str": "5.0h",
            "last_post_str": "today",
            "days_since_last": 0.1,
            "top_players": [{"full_name": "Alice B", "username": "alice", "count": 12}],
        },
        {
            "name": "Bravo",
            "total_7d": 0,
            "player_7d": 0,
            "gm_7d": 0,
            "trend_icon": "💤",
            "avg_gap_str": "N/A",
            "player_avg_gap": None,
            "player_avg_gap_str": "N/A",
            "last_post_str": "5d ago",
            "days_since_last": 5.0,
            "top_players": [],
        },
    ]
    global_players = {
        "u1": {"full_name": "Alice B", "username": "alice", "count": 12, "campaigns": 1},
    }
    result = checker._format_leaderboard(campaign_stats, global_players, now)
    assert "Weekly Campaign Leaderboard" in result
    assert "Alpha" in result
    assert "Dead campaigns" in result
    assert "Bravo" in result
    assert "Alice B" in result
    assert "Fastest player response gaps" in result


# ------------------------------------------------------------------ #
#  Integration tests (mock telegram)
# ------------------------------------------------------------------ #
def test_process_updates_tracks_messages():
    _reset()
    config = _make_config()
    state = _make_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 1001,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 42, "first_name": "TestPlayer", "last_name": "X", "username": "tp"},
            "date": now_ts,
            "text": "I attack the goblin",
        },
    }]

    new_offset = checker.process_updates(updates, config, state)
    assert new_offset == 1002
    assert "100" in state["topics"]
    assert state["topics"]["100"]["last_user"] == "TestPlayer"
    assert "100:42" in state["players"]
    assert state["players"]["100:42"]["first_name"] == "TestPlayer"
    assert state["message_counts"]["100"]["42"] == 1
    assert len(state["post_timestamps"]["100"]["42"]) == 1


def test_process_updates_ignores_other_groups():
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [{
        "update_id": 2001,
        "message": {
            "chat": {"id": -999},  # Wrong group
            "message_thread_id": 100,
            "from": {"id": 42, "first_name": "Test"},
            "text": "hello",
        },
    }]

    new_offset = checker.process_updates(updates, config, state)
    assert new_offset == 2002
    assert "100" not in state["topics"]


def test_process_updates_skips_gm_player_tracking():
    _reset()
    config = _make_config(gm_ids=[42])
    state = _make_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 3001,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 42, "first_name": "GM"},
            "date": now_ts,
            "text": "The goblin attacks!",
        },
    }]

    checker.process_updates(updates, config, state)
    assert "100:42" not in state["players"]  # GM not tracked as player
    assert state["message_counts"]["100"]["42"] == 1  # But counts are tracked


def test_process_updates_help_command():
    _reset()
    config = _make_config()
    state = _make_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 4001,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 42, "first_name": "Test"},
            "date": now_ts,
            "text": "/help",
        },
    }]

    checker.process_updates(updates, config, state)
    help_msgs = [m for m in _sent_messages if "PBP Reminder Bot" in m.get("text", "")]
    assert len(help_msgs) == 1


def test_check_and_alert_fires_after_threshold():
    _reset()
    config = _make_config()
    state = _make_state()
    five_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()

    state["topics"]["100"] = {
        "last_message_time": five_hours_ago,
        "last_user": "Alice",
        "last_user_id": "42",
        "campaign_name": "TestCampaign",
    }

    checker.check_and_alert(config, state)
    assert len(_sent_messages) == 1
    assert "No new posts" in _sent_messages[0]["text"]
    assert "TestCampaign" in _sent_messages[0]["text"]


def test_check_and_alert_skips_recent():
    _reset()
    config = _make_config()
    state = _make_state()
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    state["topics"]["100"] = {
        "last_message_time": one_hour_ago,
        "last_user": "Bob",
        "last_user_id": "42",
        "campaign_name": "TestCampaign",
    }

    checker.check_and_alert(config, state)
    assert len(_sent_messages) == 0


def test_check_and_alert_respects_feature_toggle():
    _reset()
    config = _make_config(pairs=[
        {"name": "Quiet", "chat_topic_id": 200, "pbp_topic_ids": [100], "disabled_features": ["alerts"]},
    ])
    state = _make_state()
    old = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    state["topics"]["100"] = {
        "last_message_time": old,
        "last_user": "Alice",
        "last_user_id": "42",
        "campaign_name": "Quiet",
    }

    checker.check_and_alert(config, state)
    assert len(_sent_messages) == 0  # Feature disabled, no alert


def test_build_status_basic():
    _reset()
    state = _make_state()
    now = datetime.now(timezone.utc)

    state["topics"]["100"] = {
        "last_message_time": (now - timedelta(hours=3)).isoformat(),
        "last_user": "Alice",
        "last_user_id": "42",
        "campaign_name": "TestCampaign",
    }
    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": (now - timedelta(hours=3)).isoformat(),
        "last_warned_week": 0,
    }
    state["post_timestamps"]["100"] = {
        "42": [(now - timedelta(hours=h)).isoformat() for h in [3, 24, 48]],
    }

    result = checker._build_status("100", "TestCampaign", state, {"999"})
    assert "Status for TestCampaign" in result
    assert "1/6" in result  # 1 player
    assert "3h ago" in result
    assert "player" in result


def test_build_status_at_risk():
    _reset()
    state = _make_state()
    now = datetime.now(timezone.utc)

    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Bob", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": (now - timedelta(days=10)).isoformat(),
        "last_warned_week": 0,
    }

    result = checker._build_status("100", "TestCampaign", state, {"999"})
    assert "At risk" in result
    assert "Bob" in result
    assert "10d" in result


def test_process_updates_status_command():
    _reset()
    config = _make_config()
    state = _make_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 5001,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 42, "first_name": "Test"},
            "date": now_ts,
            "text": "/status",
        },
    }]

    checker.process_updates(updates, config, state)
    status_msgs = [m for m in _sent_messages if "Status for" in m.get("text", "")]
    assert len(status_msgs) == 1


def test_build_campaign_report_basic():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config(pairs=[
        {"name": "TestCampaign", "chat_topic_id": 200, "pbp_topic_ids": [100], "created": "2025-01-15"},
    ])
    state = _make_state()

    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "B",
        "username": "alice", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": (now - timedelta(hours=5)).isoformat(),
        "last_warned_week": 0,
    }
    state["message_counts"]["100"] = {"42": 20, "999": 30}
    state["post_timestamps"]["100"] = {
        "42": [(now - timedelta(hours=h)).isoformat() for h in [5, 24, 48, 72, 120]],
        "999": [(now - timedelta(hours=h)).isoformat() for h in [1, 6, 12, 30, 60]],
    }

    result = checker._build_campaign_report("100", config, state, {"999"})
    assert "TestCampaign" in result
    assert "1/6" in result
    assert "Roster" in result
    assert "Alice B" in result
    assert "@alice" in result
    assert "GM" in result
    assert "Running since" in result


def test_build_campaign_report_at_risk():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()

    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Bob", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": (now - timedelta(days=12)).isoformat(),
        "last_warned_week": 0,
    }
    state["message_counts"]["100"] = {"42": 5}
    state["post_timestamps"]["100"] = {
        "42": [(now - timedelta(days=d)).isoformat() for d in [12, 13, 14]],
    }

    result = checker._build_campaign_report("100", config, state, {"999"})
    assert "At Risk" in result
    assert "Bob" in result


def test_process_updates_campaign_command():
    _reset()
    config = _make_config()
    state = _make_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 6001,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 42, "first_name": "Test"},
            "date": now_ts,
            "text": "/campaign",
        },
    }]

    checker.process_updates(updates, config, state)
    campaign_msgs = [m for m in _sent_messages if "TestCampaign" in m.get("text", "")]
    assert len(campaign_msgs) >= 1


# ------------------------------------------------------------------ #
#  Config validation tests
# ------------------------------------------------------------------ #
def test_validate_config_valid():
    config = _make_config()
    issues = helpers.validate_config(config)
    assert not any(i.startswith("ERROR:") for i in issues)


def test_validate_config_bad_group_id():
    config = _make_config()
    config["group_id"] = 12345
    issues = helpers.validate_config(config)
    assert any("group_id" in i for i in issues)


def test_validate_config_duplicate_pbp_ids():
    config = _make_config(pairs=[
        {"name": "A", "chat_topic_id": 1, "pbp_topic_ids": [100]},
        {"name": "B", "chat_topic_id": 2, "pbp_topic_ids": [100]},
    ])
    issues = helpers.validate_config(config)
    assert any("ERROR:" in i and "100" in i for i in issues)


def test_validate_config_unknown_feature():
    config = _make_config(pairs=[
        {"name": "A", "chat_topic_id": 1, "pbp_topic_ids": [100], "disabled_features": ["bogus"]},
    ])
    issues = helpers.validate_config(config)
    assert any("bogus" in i for i in issues)


def test_validate_config_bad_created_date():
    config = _make_config(pairs=[
        {"name": "A", "chat_topic_id": 1, "pbp_topic_ids": [100], "created": "15-01-2025"},
    ])
    issues = helpers.validate_config(config)
    assert any("YYYY-MM-DD" in i for i in issues)


def test_feature_enabled():
    config = _make_config(pairs=[
        {"name": "A", "chat_topic_id": 1, "pbp_topic_ids": [100], "disabled_features": ["roster"]},
    ])
    assert helpers.feature_enabled(config, "100", "roster") is False
    assert helpers.feature_enabled(config, "100", "alerts") is True
    assert helpers.feature_enabled(config, "999", "roster") is True


# ------------------------------------------------------------------ #
#  _parse_message tests
# ------------------------------------------------------------------ #
def test_parse_message_valid():
    maps = helpers.build_topic_maps({"topic_pairs": [
        {"name": "Test", "chat_topic_id": 200, "pbp_topic_ids": [100]},
    ]})
    msg = {
        "chat": {"id": -100},
        "message_thread_id": 100,
        "from": {"id": 42, "first_name": "Alice", "last_name": "B", "username": "alice"},
        "date": int(datetime.now(timezone.utc).timestamp()),
        "text": "Hello world",
    }
    result = checker._parse_message(msg, -100, maps)
    assert result is not None
    assert result["pid"] == "100"
    assert result["user_id"] == "42"
    assert result["user_name"] == "Alice"
    assert result["text"] == "hello world"


def test_parse_message_wrong_group():
    maps = helpers.build_topic_maps({"topic_pairs": [
        {"name": "Test", "chat_topic_id": 200, "pbp_topic_ids": [100]},
    ]})
    msg = {"chat": {"id": -999}, "message_thread_id": 100, "from": {"id": 42}}
    assert checker._parse_message(msg, -100, maps) is None


def test_parse_message_unknown_topic():
    maps = helpers.build_topic_maps({"topic_pairs": [
        {"name": "Test", "chat_topic_id": 200, "pbp_topic_ids": [100]},
    ]})
    msg = {"chat": {"id": -100}, "message_thread_id": 999, "from": {"id": 42}}
    assert checker._parse_message(msg, -100, maps) is None


def test_parse_message_bot_skipped():
    maps = helpers.build_topic_maps({"topic_pairs": [
        {"name": "Test", "chat_topic_id": 200, "pbp_topic_ids": [100]},
    ]})
    msg = {"chat": {"id": -100}, "message_thread_id": 100, "from": {"id": 42, "is_bot": True}}
    assert checker._parse_message(msg, -100, maps) is None


# ------------------------------------------------------------------ #
#  Combat tests
# ------------------------------------------------------------------ #
def test_handle_round_command():
    _reset()
    state = _make_state()
    checker._handle_round_command("/round 1 players", "100", "Test", "now", -100, 100, state)
    assert "100" in state["combat"]
    assert state["combat"]["100"]["round"] == 1
    assert state["combat"]["100"]["current_phase"] == "players"
    assert len(_sent_messages) == 1
    assert "Round 1" in _sent_messages[0]["text"]


def test_handle_round_command_enemies():
    _reset()
    state = _make_state()
    checker._handle_round_command("/round 2 enemies", "100", "Test", "now", -100, 100, state)
    assert state["combat"]["100"]["current_phase"] == "enemies"


def test_handle_round_command_resets_players_acted():
    _reset()
    state = _make_state()
    state["combat"]["100"] = {
        "active": True, "round": 1, "current_phase": "enemies",
        "players_acted": ["42"], "last_ping_at": None,
        "campaign_name": "Test", "phase_started_at": "now",
    }
    checker._handle_round_command("/round 2 players", "100", "Test", "now", -100, 100, state)
    assert state["combat"]["100"]["players_acted"] == []
    assert state["combat"]["100"]["round"] == 2


def test_handle_combat_message_tracks_player():
    _reset()
    state = _make_state()
    state["combat"]["100"] = {
        "active": True, "round": 1, "current_phase": "players",
        "players_acted": [], "last_ping_at": None,
        "campaign_name": "Test", "phase_started_at": "now",
    }
    checker._handle_combat_message("I attack!", "I attack!", "42", "Player", {"999"}, "100", "Test", "now", -100, 100, state)
    assert "42" in state["combat"]["100"]["players_acted"]


def test_handle_combat_message_gm_not_tracked():
    _reset()
    state = _make_state()
    state["combat"]["100"] = {
        "active": True, "round": 1, "current_phase": "players",
        "players_acted": [], "last_ping_at": None,
        "campaign_name": "Test", "phase_started_at": "now",
    }
    checker._handle_combat_message("narrative text", "narrative text", "999", "GM", {"999"}, "100", "Test", "now", -100, 100, state)
    assert "999" not in state["combat"]["100"]["players_acted"]


def test_handle_combat_endcombat():
    _reset()
    state = _make_state()
    state["combat"]["100"] = {
        "active": True, "round": 1, "current_phase": "players",
        "players_acted": [], "last_ping_at": None,
        "campaign_name": "Test", "phase_started_at": "now",
    }
    checker._handle_combat_message("/endcombat", "/endcombat", "999", "GM", {"999"}, "100", "Test", "now", -100, 100, state)
    assert "100" not in state["combat"]


# ------------------------------------------------------------------ #
#  Boon tests
# ------------------------------------------------------------------ #
def test_process_boon_callback_valid():
    _reset()
    state = _make_state()
    state["pending_potw_boons"]["100"] = {
        "message_id": 555,
        "winner_user_id": "42",
        "boons": ["Boon A", "Boon B", "Boon C"],
        "base_message": "Winner!",
        "posted_at": datetime.now(timezone.utc).isoformat(),
    }
    cb = {
        "id": "cb1", "data": "boon:100:1",
        "from": {"id": 42},
        "message": {"chat": {"id": -100}, "message_id": 555},
    }
    checker.process_boon_callback(cb, _make_config(), state)
    assert "100" not in state["pending_potw_boons"]  # Cleaned up
    edit_msgs = [m for m in _sent_messages if "message_id" in m]
    assert len(edit_msgs) == 1


def test_process_boon_callback_wrong_user():
    _reset()
    state = _make_state()
    state["pending_potw_boons"]["100"] = {
        "message_id": 555,
        "winner_user_id": "42",
        "boons": ["Boon A"],
        "base_message": "Winner!",
        "posted_at": datetime.now(timezone.utc).isoformat(),
    }
    cb = {
        "id": "cb1", "data": "boon:100:0",
        "from": {"id": 99},  # Wrong user
        "message": {"chat": {"id": -100}, "message_id": 555},
    }
    checker.process_boon_callback(cb, _make_config(), state)
    assert "100" in state["pending_potw_boons"]  # Not cleaned up
    reject_msgs = [m for m in _sent_messages if "Only the Player" in m.get("text", "")]
    assert len(reject_msgs) == 1


def test_expire_pending_boons():
    _reset()
    state = _make_state()
    old_time = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
    state["pending_potw_boons"]["100"] = {
        "message_id": 555,
        "winner_user_id": "42",
        "boons": ["Boon A", "Boon B"],
        "base_message": "Winner!",
        "posted_at": old_time,
    }
    checker.expire_pending_boons(_make_config(), state)
    assert "100" not in state["pending_potw_boons"]
    edit_msgs = [m for m in _sent_messages if "auto-selected" in m.get("text", "")]
    assert len(edit_msgs) == 1


# ------------------------------------------------------------------ #
#  Player activity tests
# ------------------------------------------------------------------ #
def test_check_player_activity_warns_at_1_week():
    _reset()
    config = _make_config()
    state = _make_state()
    now = datetime.now(timezone.utc)

    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "alice", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": (now - timedelta(days=8)).isoformat(),
        "last_warned_week": 0,
    }

    checker.check_player_activity(config, state)
    warn_msgs = [m for m in _sent_messages if "hasn't posted" in m.get("text", "")]
    assert len(warn_msgs) == 1
    assert state["players"]["100:42"]["last_warned_week"] == 1


def test_check_player_activity_removes_at_4_weeks():
    _reset()
    config = _make_config()
    state = _make_state()
    now = datetime.now(timezone.utc)

    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Bob", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": (now - timedelta(days=30)).isoformat(),
        "last_warned_week": 3,
    }

    checker.check_player_activity(config, state)
    assert "100:42" not in state["players"]
    assert "100:42" in state["removed_players"]


def test_check_player_activity_respects_toggle():
    _reset()
    config = _make_config(pairs=[
        {"name": "NoWarn", "chat_topic_id": 200, "pbp_topic_ids": [100], "disabled_features": ["warnings"]},
    ])
    state = _make_state()
    now = datetime.now(timezone.utc)

    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "NoWarn",
        "pbp_topic_id": "100", "last_post_time": (now - timedelta(days=15)).isoformat(),
        "last_warned_week": 0,
    }

    checker.check_player_activity(config, state)
    assert len(_sent_messages) == 0  # No warning sent


# ------------------------------------------------------------------ #
#  _gather_leaderboard_stats tests
# ------------------------------------------------------------------ #
def test_gather_leaderboard_stats_basic():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()

    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "B",
        "username": "alice", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": (now - timedelta(hours=2)).isoformat(),
        "last_warned_week": 0,
    }
    state["message_counts"]["100"] = {"42": 10, "999": 20}
    state["post_timestamps"]["100"] = {
        "42": [(now - timedelta(hours=h)).isoformat() for h in [2, 24, 48, 72, 120]],
        "999": [(now - timedelta(hours=h)).isoformat() for h in [1, 12, 36, 60, 96]],
    }

    stats, global_players, streaks = checker._gather_leaderboard_stats(config, state, now)
    assert len(stats) == 1
    assert stats[0]["name"] == "TestCampaign"
    assert stats[0]["total_7d"] > 0
    assert stats[0]["gm_7d"] > 0
    assert stats[0]["player_7d"] > 0
    assert "42" in global_players
    assert global_players["42"]["full_name"] == "Alice B"


def test_gather_leaderboard_stats_empty():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()

    stats, global_players, streaks = checker._gather_leaderboard_stats(config, state, now)
    assert len(stats) == 1  # Campaign exists but with no data
    assert stats[0]["total_7d"] == 0
    assert len(global_players) == 0
    assert len(streaks) == 0


# ------------------------------------------------------------------ #
#  check_combat_turns tests
# ------------------------------------------------------------------ #
def test_check_combat_turns_pings_missing():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()

    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "alice", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }
    state["combat"]["100"] = {
        "active": True, "round": 1, "current_phase": "players",
        "players_acted": [], "last_ping_at": None,
        "campaign_name": "TestCampaign",
        "phase_started_at": (now - timedelta(hours=5)).isoformat(),
    }

    checker.check_combat_turns(config, state, now=now)
    ping_msgs = [m for m in _sent_messages if "waiting on" in m.get("text", "")]
    assert len(ping_msgs) == 1
    assert "alice" in ping_msgs[0]["text"].lower() or "Alice" in ping_msgs[0]["text"]


def test_check_combat_turns_skips_enemies_phase():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()

    state["combat"]["100"] = {
        "active": True, "round": 1, "current_phase": "enemies",
        "players_acted": [], "last_ping_at": None,
        "campaign_name": "TestCampaign",
        "phase_started_at": (now - timedelta(hours=5)).isoformat(),
    }

    checker.check_combat_turns(config, state, now=now)
    assert len(_sent_messages) == 0


def test_check_combat_turns_no_reping_too_soon():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()

    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }
    state["combat"]["100"] = {
        "active": True, "round": 1, "current_phase": "players",
        "players_acted": [], "campaign_name": "TestCampaign",
        "phase_started_at": (now - timedelta(hours=5)).isoformat(),
        "last_ping_at": (now - timedelta(hours=1)).isoformat(),
    }

    checker.check_combat_turns(config, state, now=now)
    assert len(_sent_messages) == 0  # Too soon to reping


# ------------------------------------------------------------------ #
#  check_anniversaries tests
# ------------------------------------------------------------------ #
def test_check_anniversaries_fires_on_date():
    _reset()
    now = datetime.now(timezone.utc)
    # Construct a "created" date exactly 2 years ago today
    two_years_ago = now.replace(year=now.year - 2)
    created_str = two_years_ago.strftime("%Y-%m-%d")

    config = _make_config(pairs=[
        {"name": "OldCampaign", "chat_topic_id": 200, "pbp_topic_ids": [100], "created": created_str},
    ])
    state = _make_state()

    checker.check_anniversaries(config, state, now=now)
    anniv_msgs = [m for m in _sent_messages if "2 years" in m.get("text", "")]
    assert len(anniv_msgs) == 1
    assert "100:2" in state["last_anniversary"]


def test_check_anniversaries_no_duplicate():
    _reset()
    now = datetime.now(timezone.utc)
    two_years_ago = now.replace(year=now.year - 2)
    created_str = two_years_ago.strftime("%Y-%m-%d")

    config = _make_config(pairs=[
        {"name": "OldCampaign", "chat_topic_id": 200, "pbp_topic_ids": [100], "created": created_str},
    ])
    state = _make_state()
    state["last_anniversary"]["100:2"] = now.isoformat()  # Already posted

    checker.check_anniversaries(config, state, now=now)
    assert len(_sent_messages) == 0


def test_check_anniversaries_wrong_day():
    _reset()
    now = datetime.now(timezone.utc)
    # Use a date that's NOT today
    wrong_date = now.replace(year=now.year - 1, month=(now.month % 12) + 1)
    created_str = wrong_date.strftime("%Y-%m-%d")

    config = _make_config(pairs=[
        {"name": "Campaign", "chat_topic_id": 200, "pbp_topic_ids": [100], "created": created_str},
    ])
    state = _make_state()

    checker.check_anniversaries(config, state, now=now)
    assert len(_sent_messages) == 0


# ------------------------------------------------------------------ #
#  check_recruitment_needs tests
# ------------------------------------------------------------------ #
def test_check_recruitment_fires_when_short():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()

    # Only 1 player, needs 6
    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }

    checker.check_recruitment_needs(config, state, now=now)
    recruit_msgs = [m for m in _sent_messages if "needs" in m.get("text", "") and "more player" in m.get("text", "")]
    assert len(recruit_msgs) == 1
    assert "5 more players" in recruit_msgs[0]["text"]


def test_check_recruitment_skips_full_roster():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()

    # Add 6 players (full roster)
    for i in range(6):
        state["players"][f"100:{i}"] = {
            "user_id": str(i), "first_name": f"Player{i}", "last_name": "",
            "username": "", "campaign_name": "TestCampaign",
            "pbp_topic_id": "100", "last_post_time": now.isoformat(),
            "last_warned_week": 0,
        }

    checker.check_recruitment_needs(config, state, now=now)
    recruit_msgs = [m for m in _sent_messages if "needs" in m.get("text", "") and "more player" in m.get("text", "")]
    assert len(recruit_msgs) == 0


# ------------------------------------------------------------------ #
#  /mystats tests
# ------------------------------------------------------------------ #
def test_build_mystats_basic():
    _reset()
    now = datetime.now(timezone.utc)
    state = _make_state()

    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "B",
        "username": "alice", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": (now - timedelta(hours=2)).isoformat(),
        "last_warned_week": 0,
    }
    state["message_counts"]["100"] = {"42": 15}
    state["post_timestamps"]["100"] = {
        "42": [(now - timedelta(hours=h)).isoformat() for h in [2, 24, 48, 72, 96, 120]],
    }

    result = checker._build_mystats("100", "42", "TestCampaign", state, {"999"})
    assert "TestCampaign" in result
    assert "Player" in result
    assert "15 posts" in result
    assert "Avg gap" in result


def test_build_mystats_gm():
    _reset()
    now = datetime.now(timezone.utc)
    state = _make_state()
    state["message_counts"]["100"] = {"999": 30}
    state["post_timestamps"]["100"] = {
        "999": [(now - timedelta(hours=h)).isoformat() for h in [1, 12, 24]],
    }

    result = checker._build_mystats("100", "999", "TestCampaign", state, {"999"})
    assert "GM" in result


def test_build_mystats_no_posts():
    _reset()
    state = _make_state()
    result = checker._build_mystats("100", "42", "TestCampaign", state, {"999"})
    assert "No posts tracked" in result


def test_process_updates_mystats_command():
    _reset()
    config = _make_config()
    state = _make_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 7001,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 42, "first_name": "Test"},
            "date": now_ts,
            "text": "/mystats",
        },
    }]

    checker.process_updates(updates, config, state)
    stats_msgs = [m for m in _sent_messages if "No posts tracked" in m.get("text", "") or "TestCampaign" in m.get("text", "")]
    assert len(stats_msgs) >= 1


def test_process_updates_me_alias():
    _reset()
    config = _make_config()
    state = _make_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 7002,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 42, "first_name": "Test"},
            "date": now_ts,
            "text": "/me",
        },
    }]

    checker.process_updates(updates, config, state)
    stats_msgs = [m for m in _sent_messages if "No posts tracked" in m.get("text", "") or "TestCampaign" in m.get("text", "")]
    assert len(stats_msgs) >= 1


# ------------------------------------------------------------------ #
#  _calc_streak tests
# ------------------------------------------------------------------ #
def test_calc_streak_consecutive_days():
    now = datetime(2025, 3, 15, 14, 0, 0, tzinfo=timezone.utc)
    timestamps = [
        (now - timedelta(days=d, hours=h)).isoformat()
        for d, h in [(0, 2), (1, 5), (2, 3), (3, 8)]  # 4 consecutive days
    ]
    assert checker._calc_streak(timestamps, now) == 4


def test_calc_streak_gap_breaks():
    now = datetime(2025, 3, 15, 14, 0, 0, tzinfo=timezone.utc)
    timestamps = [
        (now - timedelta(days=0, hours=2)).isoformat(),
        (now - timedelta(days=1, hours=5)).isoformat(),
        # Day 2 missing
        (now - timedelta(days=3, hours=3)).isoformat(),
    ]
    assert checker._calc_streak(timestamps, now) == 2


def test_calc_streak_no_recent_posts():
    now = datetime(2025, 3, 15, 14, 0, 0, tzinfo=timezone.utc)
    timestamps = [
        (now - timedelta(days=5)).isoformat(),  # Too old
    ]
    assert checker._calc_streak(timestamps, now) == 0


def test_calc_streak_multiple_posts_same_day():
    now = datetime(2025, 3, 15, 14, 0, 0, tzinfo=timezone.utc)
    timestamps = [
        (now - timedelta(hours=1)).isoformat(),
        (now - timedelta(hours=3)).isoformat(),
        (now - timedelta(hours=5)).isoformat(),
        (now - timedelta(days=1, hours=2)).isoformat(),
    ]
    assert checker._calc_streak(timestamps, now) == 2


def test_calc_streak_empty():
    now = datetime(2025, 3, 15, 14, 0, 0, tzinfo=timezone.utc)
    assert checker._calc_streak([], now) == 0


# ------------------------------------------------------------------ #
#  /whosturn tests
# ------------------------------------------------------------------ #
def test_build_whosturn_no_combat():
    _reset()
    state = _make_state()
    result = checker._build_whosturn("100", "TestCampaign", state)
    assert "No active combat" in result


def test_build_whosturn_players_phase():
    _reset()
    now = datetime.now(timezone.utc)
    state = _make_state()

    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }
    state["players"]["100:43"] = {
        "user_id": "43", "first_name": "Bob", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }
    state["combat"]["100"] = {
        "active": True, "round": 2, "current_phase": "players",
        "players_acted": ["42"], "last_ping_at": None,
        "campaign_name": "TestCampaign",
        "phase_started_at": (now - timedelta(hours=1)).isoformat(),
    }

    result = checker._build_whosturn("100", "TestCampaign", state)
    assert "Round 2" in result
    assert "Alice" in result
    assert "Bob" in result
    assert "Acted" in result
    assert "Waiting" in result


def test_build_whosturn_enemies_phase():
    _reset()
    now = datetime.now(timezone.utc)
    state = _make_state()

    state["combat"]["100"] = {
        "active": True, "round": 1, "current_phase": "enemies",
        "players_acted": [], "last_ping_at": None,
        "campaign_name": "TestCampaign",
        "phase_started_at": (now - timedelta(hours=1)).isoformat(),
    }

    result = checker._build_whosturn("100", "TestCampaign", state)
    assert "Enemies" in result
    assert "GM" in result


def test_process_updates_whosturn_command():
    _reset()
    config = _make_config()
    state = _make_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 7003,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 42, "first_name": "Test"},
            "date": now_ts,
            "text": "/whosturn",
        },
    }]

    checker.process_updates(updates, config, state)
    turn_msgs = [m for m in _sent_messages if "No active combat" in m.get("text", "") or "Round" in m.get("text", "")]
    assert len(turn_msgs) >= 1


# ------------------------------------------------------------------ #
#  Daily tip tests
# ------------------------------------------------------------------ #
def test_post_daily_tip_sends():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()

    checker.post_daily_tip(config, state, now=now)
    assert len(_sent_messages) == 1
    assert "💡" in _sent_messages[0].get("text", "")
    assert state.get("last_daily_tip") is not None
    assert len(state.get("used_tip_indices", [])) == 1


def test_post_daily_tip_respects_cooldown():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()
    state["last_daily_tip"] = (now - timedelta(hours=10)).isoformat()

    checker.post_daily_tip(config, state, now=now)
    assert len(_sent_messages) == 0  # Too soon


def test_post_daily_tip_rotates():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()

    # Exhaust all but one tip
    state["used_tip_indices"] = list(range(len(checker._TIPS) - 1))

    checker.post_daily_tip(config, state, now=now)
    assert len(_sent_messages) == 1
    # The only remaining index should be the one not in the used list
    last_idx = state["used_tip_indices"][-1]
    assert last_idx == len(checker._TIPS) - 1


def test_post_daily_tip_resets_cycle():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()

    # All tips used
    state["used_tip_indices"] = list(range(len(checker._TIPS)))

    checker.post_daily_tip(config, state, now=now)
    assert len(_sent_messages) == 1
    # Cycle should have reset - used_tip_indices should have exactly 1 entry
    assert len(state["used_tip_indices"]) == 1


# ------------------------------------------------------------------ #
#  Streak milestone tests
# ------------------------------------------------------------------ #
def test_streak_milestone_fires_at_7():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()

    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }
    # 8 consecutive days of posts
    state["post_timestamps"]["100"] = {
        "42": [(now - timedelta(days=d, hours=3)).isoformat() for d in range(8)],
    }

    checker.check_streak_milestones(config, state, now=now)
    streak_msgs = [m for m in _sent_messages if "7-day" in m.get("text", "")]
    assert len(streak_msgs) == 1
    assert state["celebrated_streaks"]["100:42"] == 7


def test_streak_milestone_no_duplicate():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()

    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }
    state["post_timestamps"]["100"] = {
        "42": [(now - timedelta(days=d, hours=3)).isoformat() for d in range(8)],
    }
    state["celebrated_streaks"] = {"100:42": 7}  # Already celebrated

    checker.check_streak_milestones(config, state, now=now)
    assert len(_sent_messages) == 0


def test_streak_milestone_escalates():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()

    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }
    # 15 consecutive days
    state["post_timestamps"]["100"] = {
        "42": [(now - timedelta(days=d, hours=3)).isoformat() for d in range(15)],
    }
    state["celebrated_streaks"] = {"100:42": 7}

    checker.check_streak_milestones(config, state, now=now)
    streak_msgs = [m for m in _sent_messages if "14-day" in m.get("text", "")]
    assert len(streak_msgs) == 1
    assert state["celebrated_streaks"]["100:42"] == 14


# ------------------------------------------------------------------ #
#  Weekly digest tests
# ------------------------------------------------------------------ #
def test_build_weekly_digest_basic():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()

    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }
    state["message_counts"]["100"] = {"42": 15, "999": 10}
    state["post_timestamps"]["100"] = {
        "42": [(now - timedelta(hours=h)).isoformat() for h in range(1, 16)],
        "999": [(now - timedelta(hours=h)).isoformat() for h in range(1, 11)],
    }

    result = checker._build_weekly_digest(config, state, now)
    assert "Weekly Digest" in result
    assert "TestCampaign" in result
    assert "MVP" in result
    assert "Alice" in result


def test_build_weekly_digest_health_icons():
    assert checker._health_icon(25) == "🟢"
    assert checker._health_icon(15) == "🟡"
    assert checker._health_icon(7) == "🟠"
    assert checker._health_icon(2) == "🔴"
    assert checker._health_icon(0) == "🔴"


def test_leaderboard_includes_streaks():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()

    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "B",
        "username": "alice", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }
    state["message_counts"]["100"] = {"42": 10, "999": 20}
    # 5 consecutive days of posts
    state["post_timestamps"]["100"] = {
        "42": [(now - timedelta(days=d, hours=3)).isoformat() for d in range(5)],
        "999": [(now - timedelta(hours=h)).isoformat() for h in [1, 12, 36, 60, 96]],
    }

    stats, global_players, streaks = checker._gather_leaderboard_stats(config, state, now)
    assert len(streaks) >= 1
    assert streaks[0]["name"] == "Alice B"
    assert streaks[0]["streak"] >= 2

    result = checker._format_leaderboard(stats, global_players, now, streaks)
    assert "Longest Active Streaks" in result
    assert "Alice B" in result


def test_roster_block_shows_streak():
    now = datetime.now(timezone.utc)
    stats = {
        "total": 20, "sessions": 15, "week_count": 5,
        "avg_gap_str": "4.2h", "last_post_str": "2h ago", "streak": 8,
    }
    result = checker._roster_block("Alice", "alice", stats)
    assert "8-day streak" in result
    assert "🔥" in result


def test_roster_block_hides_short_streak():
    stats = {
        "total": 20, "sessions": 15, "week_count": 5,
        "avg_gap_str": "4.2h", "last_post_str": "2h ago", "streak": 1,
    }
    result = checker._roster_block("Alice", "alice", stats)
    assert "streak" not in result


# ------------------------------------------------------------------ #
#  Sparkline and /myhistory tests
# ------------------------------------------------------------------ #
def test_sparkline_basic():
    result = checker._sparkline([0, 2, 4, 8, 4, 2, 0, 1])
    assert len(result) == 8
    assert result[3] == "█"  # Peak
    assert result[0] == " "  # Zero


def test_sparkline_all_zeros():
    result = checker._sparkline([0, 0, 0])
    assert result == "▁▁▁"


def test_sparkline_uniform():
    result = checker._sparkline([5, 5, 5])
    assert all(c == "█" for c in result)


def test_build_myhistory_basic():
    _reset()
    now = datetime.now(timezone.utc)
    state = _make_state()

    state["message_counts"]["100"] = {"42": 30}
    state["post_timestamps"]["100"] = {
        "42": [
            (now - timedelta(weeks=w, hours=h)).isoformat()
            for w in range(4)
            for h in [2, 24, 48]
        ],
    }

    result = checker._build_myhistory("100", "42", "TestCampaign", state, {"999"})
    assert "Posting history" in result
    assert "Player" in result
    assert "8 weeks" in result
    assert "Peak week" in result


def test_build_myhistory_no_posts():
    _reset()
    state = _make_state()
    result = checker._build_myhistory("100", "42", "TestCampaign", state, {"999"})
    assert "No posting history" in result


def test_process_updates_myhistory_command():
    _reset()
    config = _make_config()
    state = _make_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 8001,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 42, "first_name": "Test"},
            "date": now_ts,
            "text": "/myhistory",
        },
    }]

    checker.process_updates(updates, config, state)
    history_msgs = [m for m in _sent_messages if "No posting history" in m.get("text", "") or "Posting history" in m.get("text", "")]
    assert len(history_msgs) >= 1


# ------------------------------------------------------------------ #
#  /pause and /resume tests
# ------------------------------------------------------------------ #
def test_pause_command():
    _reset()
    config = _make_config()
    state = _make_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 9001,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 999, "first_name": "GM"},
            "date": now_ts,
            "text": "/pause Holiday break",
        },
    }]

    checker.process_updates(updates, config, state)
    assert "100" in state.get("paused_campaigns", {})
    assert state["paused_campaigns"]["100"]["reason"] == "Holiday break"
    pause_msgs = [m for m in _sent_messages if "paused" in m.get("text", "").lower()]
    assert len(pause_msgs) == 1


def test_pause_non_gm_ignored():
    _reset()
    config = _make_config()
    state = _make_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 9002,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 42, "first_name": "Player"},
            "date": now_ts,
            "text": "/pause trying to pause",
        },
    }]

    checker.process_updates(updates, config, state)
    assert "100" not in state.get("paused_campaigns", {})


def test_resume_command():
    _reset()
    config = _make_config()
    state = _make_state()
    state["paused_campaigns"] = {"100": {"paused_at": "now", "reason": "test"}}
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 9003,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 999, "first_name": "GM"},
            "date": now_ts,
            "text": "/resume",
        },
    }]

    checker.process_updates(updates, config, state)
    assert "100" not in state.get("paused_campaigns", {})
    resume_msgs = [m for m in _sent_messages if "resumed" in m.get("text", "").lower()]
    assert len(resume_msgs) == 1


def test_pause_stops_alerts():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()

    state["topics"]["100"] = {
        "last_message_time": (now - timedelta(hours=10)).isoformat(),
        "last_user": "Alice",
        "last_user_id": "42",
        "campaign_name": "TestCampaign",
    }
    state["paused_campaigns"] = {"100": {"paused_at": now.isoformat(), "reason": "break"}}

    checker.check_and_alert(config, state, now=now)
    alert_msgs = [m for m in _sent_messages if "No new posts" in m.get("text", "")]
    assert len(alert_msgs) == 0


def test_pause_stops_player_warnings():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()

    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": (now - timedelta(days=10)).isoformat(),
        "last_warned_week": 0,
    }
    state["paused_campaigns"] = {"100": {"paused_at": now.isoformat(), "reason": "break"}}

    checker.check_player_activity(config, state, now=now)
    assert len(_sent_messages) == 0


def test_pause_shows_in_status():
    _reset()
    now = datetime.now(timezone.utc)
    state = _make_state()
    state["paused_campaigns"] = {"100": {"paused_at": now.isoformat(), "reason": "Holiday"}}

    result = checker._build_status("100", "TestCampaign", state, {"999"})
    assert "PAUSED" in result
    assert "Holiday" in result


def test_pause_shows_in_campaign():
    _reset()
    now = datetime.now(timezone.utc)
    config = _make_config()
    state = _make_state()
    state["paused_campaigns"] = {"100": {"paused_at": now.isoformat(), "reason": "Between arcs"}}

    result = checker._build_campaign_report("100", config, state, {"999"})
    assert "PAUSED" in result
    assert "Between arcs" in result


# ------------------------------------------------------------------ #
#  Transcript logging tests
# ------------------------------------------------------------------ #
def test_sanitize_dirname():
    assert checker._sanitize_dirname("Doomsday Funtime") == "Doomsday_Funtime"
    assert checker._sanitize_dirname("Test/Bad:Name!") == "TestBadName"
    assert checker._sanitize_dirname("  Spaces  ") == "Spaces"


def test_format_log_entry_text():
    parsed = {
        "user_name": "Alice", "user_last_name": "B", "user_id": "42",
        "msg_time_iso": "2026-02-26T14:30:05+00:00",
        "raw_text": "I attack the goblin!", "media_type": None, "caption": "",
    }
    result = checker._format_log_entry(parsed, {"999"})
    assert "**Alice B**" in result
    assert "I attack the goblin!" in result
    assert "[GM]" not in result
    assert "2026-02-26 14:30:05" in result


def test_format_log_entry_gm():
    parsed = {
        "user_name": "Lewis", "user_last_name": "", "user_id": "999",
        "msg_time_iso": "2026-02-26T14:30:05+00:00",
        "raw_text": "The goblin snarls.", "media_type": None, "caption": "",
    }
    result = checker._format_log_entry(parsed, {"999"})
    assert "[GM]" in result


def test_format_log_entry_image():
    parsed = {
        "user_name": "Alice", "user_last_name": "", "user_id": "42",
        "msg_time_iso": "2026-02-26T14:30:05+00:00",
        "raw_text": "", "media_type": "image", "caption": "battle map",
    }
    result = checker._format_log_entry(parsed, {"999"})
    assert "[image]" in result
    assert "battle map" in result


def test_format_log_entry_sticker():
    parsed = {
        "user_name": "Bob", "user_last_name": "", "user_id": "42",
        "msg_time_iso": "2026-02-26T14:30:05+00:00",
        "raw_text": "", "media_type": "sticker:😂", "caption": "",
    }
    result = checker._format_log_entry(parsed, {"999"})
    assert "[sticker 😂]" in result


def test_append_to_transcript():
    import shutil
    test_dir = checker._LOGS_DIR / "transcript_test"
    if test_dir.exists():
        shutil.rmtree(test_dir)

    parsed = {
        "campaign_name": "transcript_test",
        "user_name": "Alice", "user_last_name": "", "user_id": "42",
        "msg_time_iso": "2026-02-26T14:30:05+00:00",
        "raw_text": "Hello world!", "media_type": None, "caption": "",
    }
    checker._append_to_transcript(parsed, {"999"})

    log_dir = checker._LOGS_DIR / "transcript_test"
    assert log_dir.exists()
    log_file = log_dir / "2026-02.md"
    assert log_file.exists()
    content = log_file.read_text()
    assert "transcript_test — 2026-02" in content
    assert "**Alice**" in content
    assert "Hello world!" in content

    # Second write appends
    parsed["raw_text"] = "Second message"
    checker._append_to_transcript(parsed, {"999"})
    content = log_file.read_text()
    assert "Second message" in content
    assert content.count("transcript_test — 2026-02") == 1  # Header only once

    shutil.rmtree(test_dir)


def test_parse_message_captures_media():
    maps = helpers.build_topic_maps({"topic_pairs": [
        {"name": "Test", "chat_topic_id": 200, "pbp_topic_ids": [100]},
    ]})
    msg = {
        "chat": {"id": -100},
        "message_thread_id": 100,
        "from": {"id": 42, "first_name": "Alice"},
        "date": int(datetime.now(timezone.utc).timestamp()),
        "photo": [{"file_id": "abc"}],
        "caption": "battle map",
    }
    result = checker._parse_message(msg, -100, maps)
    assert result["media_type"] == "image"
    assert result["caption"] == "battle map"
    assert result["text"] == "battle map"  # Falls back to caption


# ------------------------------------------------------------------ #
#  /kick and /addplayer tests
# ------------------------------------------------------------------ #
def test_kick_by_username():
    _reset()
    state = _make_state()
    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "alice99", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": "2026-01-01T00:00:00",
        "last_warned_week": 0,
    }
    checker._handle_kick("100", "TestCampaign", "alice99", state, -100, 200)
    assert "100:42" not in state["players"]
    assert "100:42" in state["removed_players"]
    assert state["removed_players"]["100:42"]["kicked"] is True
    assert any("removed" in m.get("text", "").lower() for m in _sent_messages)


def test_kick_by_first_name():
    _reset()
    state = _make_state()
    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "Smith",
        "username": "alice99", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": "2026-01-01T00:00:00",
        "last_warned_week": 0,
    }
    checker._handle_kick("100", "TestCampaign", "Alice Smith", state, -100, 200)
    assert "100:42" not in state["players"]


def test_kick_no_match():
    _reset()
    state = _make_state()
    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "alice99", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": "2026-01-01T00:00:00",
        "last_warned_week": 0,
    }
    checker._handle_kick("100", "TestCampaign", "nobody", state, -100, 200)
    assert "100:42" in state["players"]  # Not removed
    assert any("no player" in m.get("text", "").lower() for m in _sent_messages)


def test_addplayer():
    _reset()
    state = _make_state()
    now_iso = datetime.now(timezone.utc).isoformat()
    checker._handle_addplayer("100", "TestCampaign", "@bob Bob Jones",
                              now_iso, state, -100, 200)
    key = "100:pending_bob"
    assert key in state["players"]
    assert state["players"][key]["first_name"] == "Bob"
    assert state["players"][key]["last_name"] == "Jones"
    assert state["players"][key]["username"] == "bob"
    assert any("added" in m.get("text", "").lower() for m in _sent_messages)


def test_addplayer_duplicate():
    _reset()
    state = _make_state()
    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Bob", "last_name": "",
        "username": "bob", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": "2026-01-01T00:00:00",
        "last_warned_week": 0,
    }
    now_iso = datetime.now(timezone.utc).isoformat()
    checker._handle_addplayer("100", "TestCampaign", "@bob Bob",
                              now_iso, state, -100, 200)
    assert "100:pending_bob" not in state["players"]  # Not added
    assert any("already tracked" in m.get("text", "").lower() for m in _sent_messages)


def test_addplayer_clears_removed():
    _reset()
    state = _make_state()
    state["removed_players"]["100:42"] = {
        "removed_at": "2026-01-01T00:00:00",
        "first_name": "Bob", "username": "bob",
        "campaign_name": "TestCampaign",
    }
    now_iso = datetime.now(timezone.utc).isoformat()
    checker._handle_addplayer("100", "TestCampaign", "@bob Bob",
                              now_iso, state, -100, 200)
    assert "100:42" not in state["removed_players"]
    assert "100:pending_bob" in state["players"]


# ------------------------------------------------------------------ #
#  /catchup tests
# ------------------------------------------------------------------ #
def test_catchup_no_history():
    _reset()
    state = _make_state()
    result = checker._build_catchup("100", "42", "TestCampaign", state, {"999"})
    assert "no posting history" in result.lower()


def test_catchup_caught_up():
    _reset()
    now = datetime.now(timezone.utc)
    state = _make_state()
    # Player posted just now
    state["post_timestamps"]["100"] = {
        "42": [now.isoformat()],
    }
    result = checker._build_catchup("100", "42", "TestCampaign", state, {"999"})
    assert "caught up" in result.lower()


def test_catchup_nobody_posted():
    _reset()
    now = datetime.now(timezone.utc)
    state = _make_state()
    # Player posted 5 hours ago, nobody else has posted since
    state["post_timestamps"]["100"] = {
        "42": [(now - timedelta(hours=5)).isoformat()],
    }
    result = checker._build_catchup("100", "42", "TestCampaign", state, {"999"})
    assert "nobody" in result.lower()
    assert "floor is yours" in result.lower()


def test_catchup_with_messages():
    _reset()
    now = datetime.now(timezone.utc)
    state = _make_state()
    # Player posted 24 hours ago, others posted since
    my_post = (now - timedelta(hours=24)).isoformat()
    gm_post = (now - timedelta(hours=12)).isoformat()
    other_post1 = (now - timedelta(hours=6)).isoformat()
    other_post2 = (now - timedelta(hours=3)).isoformat()

    state["post_timestamps"]["100"] = {
        "42": [my_post],
        "999": [gm_post],
        "50": [other_post1, other_post2],
    }
    state["players"]["100:50"] = {
        "user_id": "50", "first_name": "Bob", "last_name": "",
        "username": "bob", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": other_post2,
        "last_warned_week": 0,
    }

    result = checker._build_catchup("100", "42", "TestCampaign", state, {"999"})
    assert "GM" in result
    assert "Bob" in result
    assert "3 posts" in result  # 1 GM + 2 Bob


def test_catchup_with_combat():
    _reset()
    now = datetime.now(timezone.utc)
    state = _make_state()
    state["post_timestamps"]["100"] = {
        "42": [(now - timedelta(hours=5)).isoformat()],
        "999": [(now - timedelta(hours=2)).isoformat()],
    }
    state["combat"]["100"] = {
        "active": True, "round": 3, "current_phase": "players",
        "players_acted": {},
    }

    result = checker._build_catchup("100", "42", "TestCampaign", state, {"999"})
    assert "combat" in result.lower()
    assert "Round 3" in result
    assert "haven't acted" in result


# ------------------------------------------------------------------ #
#  /overview tests
# ------------------------------------------------------------------ #
def test_overview_multi_campaign():
    _reset()
    now = datetime.now(timezone.utc)
    config = {
        "group_id": -100,
        "gm_user_ids": [999],
        "topic_pairs": [
            {"name": "Campaign A", "chat_topic_id": 200, "pbp_topic_ids": [100]},
            {"name": "Campaign B", "chat_topic_id": 400, "pbp_topic_ids": [300]},
        ],
    }
    state = _make_state()
    state["topics"]["100"] = {
        "last_message_time": (now - timedelta(hours=2)).isoformat(),
        "last_user": "Alice", "last_user_id": "42",
        "campaign_name": "Campaign A",
    }
    state["topics"]["300"] = {
        "last_message_time": (now - timedelta(days=3)).isoformat(),
        "last_user": "Bob", "last_user_id": "50",
        "campaign_name": "Campaign B",
    }
    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "Campaign A",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }

    result = checker._build_overview(config, state)
    assert "Campaign A" in result
    assert "Campaign B" in result
    assert "2 campaigns" in result
    assert "1 active players" in result


# ------------------------------------------------------------------ #
#  Message milestone tests
# ------------------------------------------------------------------ #
def test_milestone_campaign_500():
    _reset()
    config = _make_config()
    state = _make_state()
    # Give the campaign 500 messages
    state["message_counts"]["100"] = {"42": 300, "50": 200}
    state["celebrated_milestones"] = {}

    checker.check_message_milestones(config, state)
    assert state["celebrated_milestones"].get("campaign:100") == 500
    assert any("500" in m.get("text", "") for m in _sent_messages)


def test_milestone_campaign_not_repeated():
    _reset()
    config = _make_config()
    state = _make_state()
    state["message_counts"]["100"] = {"42": 300, "50": 200}
    state["celebrated_milestones"] = {"campaign:100": 500}

    checker.check_message_milestones(config, state)
    # No new messages sent — already celebrated
    milestone_msgs = [m for m in _sent_messages if "500" in m.get("text", "")]
    assert len(milestone_msgs) == 0


def test_milestone_campaign_1000():
    _reset()
    config = _make_config()
    state = _make_state()
    state["message_counts"]["100"] = {"42": 600, "50": 400}
    state["celebrated_milestones"] = {"campaign:100": 500}

    checker.check_message_milestones(config, state)
    assert state["celebrated_milestones"]["campaign:100"] == 1000
    assert any("1,000" in m.get("text", "") for m in _sent_messages)


def test_milestone_global():
    _reset()
    config = {
        "group_id": -100,
        "gm_user_ids": [999],
        "leaderboard_topic_id": 9999,
        "topic_pairs": [
            {"name": "A", "chat_topic_id": 200, "pbp_topic_ids": [100]},
            {"name": "B", "chat_topic_id": 400, "pbp_topic_ids": [300]},
        ],
    }
    state = _make_state()
    state["message_counts"]["100"] = {"42": 3000}
    state["message_counts"]["300"] = {"50": 2000}
    state["celebrated_milestones"] = {}

    checker.check_message_milestones(config, state)
    assert state["celebrated_milestones"].get("global") == 5000
    assert any("5,000" in m.get("text", "") and "Path Wars" in m.get("text", "")
               for m in _sent_messages)


# ------------------------------------------------------------------ #
#  Character awareness and /party tests
# ------------------------------------------------------------------ #
def test_character_name_helper():
    config = {
        "topic_pairs": [
            {"name": "A", "chat_topic_id": 10, "pbp_topic_ids": [100],
             "characters": {"42": "Cardigan", "50": "Amar"}},
        ],
    }
    assert helpers.character_name(config, "100", "42") == "Cardigan"
    assert helpers.character_name(config, "100", "50") == "Amar"
    assert helpers.character_name(config, "100", "999") is None
    assert helpers.character_name(config, "999", "42") is None


def test_get_characters():
    config = {
        "topic_pairs": [
            {"name": "A", "chat_topic_id": 10, "pbp_topic_ids": [100],
             "characters": {42: "Cardigan"}},
        ],
    }
    chars = helpers.get_characters(config, "100")
    assert chars == {"42": "Cardigan"}
    assert helpers.get_characters(config, "999") == {}


def test_party_with_characters():
    _reset()
    now = datetime.now(timezone.utc)
    config = {
        "group_id": -100,
        "gm_user_ids": [999],
        "topic_pairs": [
            {"name": "TestCampaign", "chat_topic_id": 200, "pbp_topic_ids": [100],
             "characters": {"42": "Cardigan", "50": "Amar"}},
        ],
    }
    state = _make_state()
    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }

    result = checker._build_party("100", "TestCampaign", config, state)
    assert "Cardigan" in result
    assert "Alice" in result
    assert "Amar" in result
    assert "1 active" in result
    assert "1 inactive" in result


def test_party_no_characters():
    _reset()
    config = _make_config()
    state = _make_state()
    result = checker._build_party("100", "TestCampaign", config, state)
    assert "no characters" in result.lower()


def test_mystats_with_character():
    _reset()
    now = datetime.now(timezone.utc)
    config = {
        "group_id": -100,
        "gm_user_ids": [999],
        "topic_pairs": [
            {"name": "Test", "chat_topic_id": 200, "pbp_topic_ids": [100],
             "characters": {"42": "Cardigan"}},
        ],
    }
    state = _make_state()
    state["post_timestamps"]["100"] = {
        "42": [(now - timedelta(hours=h)).isoformat() for h in range(10)],
    }
    state["message_counts"]["100"] = {"42": 10}

    result = checker._build_mystats("100", "42", "Test", state, {"999"}, config)
    assert "Cardigan" in result


def test_word_count_tracking():
    """Word counts are accumulated per-user per-campaign during message processing."""
    _reset()
    config = _make_config()
    state = _make_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [
        {
            "update_id": 2001,
            "message": {
                "chat": {"id": -100},
                "message_thread_id": 100,
                "from": {"id": 42, "first_name": "Alice", "last_name": "", "username": "alice"},
                "date": now_ts,
                "text": "Cardigan draws her blade and charges forward",
            },
        },
        {
            "update_id": 2002,
            "message": {
                "chat": {"id": -100},
                "message_thread_id": 100,
                "from": {"id": 42, "first_name": "Alice", "last_name": "", "username": "alice"},
                "date": now_ts + 60,
                "text": "She strikes true",
            },
        },
    ]
    checker.process_updates(updates, config, state)
    # 7 words + 3 words = 10
    assert state["word_counts"]["100"]["42"] == 10


def test_mystats_shows_word_count():
    """The /mystats output includes word count when available."""
    _reset()
    now = datetime.now(timezone.utc)
    state = _make_state()
    state["post_timestamps"]["100"] = {
        "42": [(now - timedelta(hours=h)).isoformat() for h in range(5)],
    }
    state["message_counts"]["100"] = {"42": 5}
    state["word_counts"] = {"100": {"42": 250}}

    result = checker._build_mystats("100", "42", "Test", state, {"999"})
    assert "250" in result
    assert "50/post" in result


def test_profile_shows_word_count():
    """The /profile output includes word count when available."""
    _reset()
    now = datetime.now(timezone.utc)
    config = {
        "group_id": -100,
        "gm_user_ids": [999],
        "topic_pairs": [
            {"name": "Test", "chat_topic_id": 200, "pbp_topic_ids": [100]},
        ],
    }
    state = _make_state()
    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "alice", "campaign_name": "Test",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }
    state["message_counts"]["100"] = {"42": 20}
    state["post_timestamps"]["100"] = {
        "42": [(now - timedelta(hours=h)).isoformat() for h in range(20)],
    }
    state["word_counts"] = {"100": {"42": 1500}}

    result = checker._build_profile("alice", config, state)
    assert "1,500 words" in result


def test_transcript_with_character():
    _reset()
    import shutil
    test_dir = checker._LOGS_DIR / "char_test"
    if test_dir.exists():
        shutil.rmtree(test_dir)

    config = {
        "topic_pairs": [
            {"name": "char_test", "chat_topic_id": 10, "pbp_topic_ids": [100],
             "characters": {"42": "Cardigan"}},
        ],
    }
    parsed = {
        "campaign_name": "char_test", "pid": "100",
        "user_name": "Alice", "user_last_name": "", "user_id": "42",
        "msg_time_iso": "2026-02-26T14:30:05+00:00",
        "raw_text": "I rage!", "media_type": None, "caption": "",
    }
    checker._append_to_transcript(parsed, {"999"}, config)

    log_file = checker._LOGS_DIR / "char_test" / "2026-02.md"
    content = log_file.read_text()
    assert "(Cardigan)" in content
    assert "I rage!" in content

    shutil.rmtree(test_dir)


# ------------------------------------------------------------------ #
#  Archive player_breakdown
# ------------------------------------------------------------------ #
def test_archive_includes_player_breakdown():
    _reset()
    config = _make_config()
    now = datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc)  # Friday

    state = _make_state()
    # Plant timestamps for player 42 (not GM 999) in last week
    week_start = now - timedelta(days=now.weekday() + 7)
    ts1 = (week_start + timedelta(hours=2)).isoformat()
    ts2 = (week_start + timedelta(days=1, hours=3)).isoformat()
    ts3 = (week_start + timedelta(days=2, hours=4)).isoformat()
    state["post_timestamps"]["100"] = {
        "42": [ts1, ts2, ts3],
        "999": [(week_start + timedelta(hours=5)).isoformat()],
    }
    state["players"]["100:42"] = {
        "first_name": "Alice",
        "last_post_time": ts3,
        "pbp_topic_id": "100",
        "campaign_name": "TestCampaign",
    }

    # Ensure archive file doesn't exist yet
    import pathlib
    archive_path = helpers.ARCHIVE_PATH
    if archive_path.exists():
        archive_path.unlink()

    checker.archive_weekly_data(config, state, now=now)

    # Read the archive
    import json
    with open(archive_path) as f:
        archive = json.load(f)

    # Find our entry
    entries = [v for v in archive.values() if v["campaign"] == "TestCampaign"]
    assert len(entries) == 1
    entry = entries[0]

    assert "player_breakdown" in entry
    pb = entry["player_breakdown"]
    # Alice should be in the breakdown
    alice_entries = [v for k, v in pb.items() if "Alice" in k]
    assert len(alice_entries) == 1
    assert alice_entries[0]["posts"] == 3
    assert alice_entries[0]["sessions"] >= 1
    assert alice_entries[0]["avg_gap_h"] is not None


# ------------------------------------------------------------------ #
#  Smart alerts: pace drop
# ------------------------------------------------------------------ #
def test_pace_drop_detected():
    _reset()
    config = _make_config()
    now = datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc)
    state = _make_state()

    # Last week had 20 posts, this week has 5 -> 75% drop
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    last_week_times = [(two_weeks_ago + timedelta(hours=i * 6)).isoformat() for i in range(20)]
    this_week_times = [(week_ago + timedelta(hours=i * 24)).isoformat() for i in range(5)]

    state["post_timestamps"]["100"] = {
        "42": last_week_times + this_week_times,
    }
    state["players"]["100"] = {
        "42": {"first_name": "Alice", "last_post": this_week_times[-1]},
    }

    checker.check_pace_drop(config, state, now=now)
    assert any("Pace check" in m.get("text", "") or "📉" in m.get("text", "") for m in _sent_messages)
    assert "last_pace_drop_check" in state


def test_pace_drop_skips_low_activity():
    _reset()
    config = _make_config()
    now = datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc)
    state = _make_state()

    # Last week had only 3 posts (below threshold of 5) — should not alert
    two_weeks_ago = now - timedelta(days=14)
    last_week_times = [(two_weeks_ago + timedelta(hours=i * 24)).isoformat() for i in range(3)]

    state["post_timestamps"]["100"] = {
        "42": last_week_times,
    }
    state["players"]["100"] = {
        "42": {"first_name": "Alice", "last_post": last_week_times[-1]},
    }

    checker.check_pace_drop(config, state, now=now)
    pace_msgs = [m for m in _sent_messages if "Pace check" in m.get("text", "") or "📉" in m.get("text", "")]
    assert len(pace_msgs) == 0


def test_pace_drop_weekly_gating():
    _reset()
    config = _make_config()
    now = datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc)
    state = _make_state()
    # Already checked recently
    state["last_pace_drop_check"] = (now - timedelta(days=1)).isoformat()

    checker.check_pace_drop(config, state, now=now)
    assert len(_sent_messages) == 0  # Should not run


# ------------------------------------------------------------------ #
#  Smart alerts: conversation dying
# ------------------------------------------------------------------ #
def test_conversation_dying_48h():
    _reset()
    config = _make_config()
    now = datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc)
    state = _make_state()

    # Last post was 60h ago
    last_post = (now - timedelta(hours=60)).isoformat()
    state["post_timestamps"]["100"] = {
        "42": [last_post],
        "999": [(now - timedelta(hours=55)).isoformat()],
    }

    checker.check_conversation_dying(config, state, now=now)
    dying_msgs = [m for m in _sent_messages if "💤" in m.get("text", "") or "silent" in m.get("text", "")]
    assert len(dying_msgs) == 1
    assert state.get("dying_alerts_sent", {}).get("100") == "active"


def test_conversation_dying_not_repeated():
    _reset()
    config = _make_config()
    now = datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc)
    state = _make_state()

    last_post = (now - timedelta(hours=60)).isoformat()
    state["post_timestamps"]["100"] = {"42": [last_post]}
    state["dying_alerts_sent"] = {"100": "active"}

    checker.check_conversation_dying(config, state, now=now)
    # Should NOT send again — already flagged
    assert len(_sent_messages) == 0


def test_conversation_dying_resets_on_activity():
    _reset()
    config = _make_config()
    now = datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc)
    state = _make_state()

    # Recent post (1h ago) — should clear the flag
    recent = (now - timedelta(hours=1)).isoformat()
    state["post_timestamps"]["100"] = {"42": [recent]}
    state["dying_alerts_sent"] = {"100": "active"}

    checker.check_conversation_dying(config, state, now=now)
    assert "100" not in state.get("dying_alerts_sent", {})
    assert len(_sent_messages) == 0


def test_conversation_dying_skips_paused():
    _reset()
    config = _make_config()
    now = datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc)
    state = _make_state()

    last_post = (now - timedelta(hours=72)).isoformat()
    state["post_timestamps"]["100"] = {"42": [last_post]}
    state["paused"] = {"100": "on holiday"}

    checker.check_conversation_dying(config, state, now=now)
    assert len(_sent_messages) == 0


# ------------------------------------------------------------------ #
#  v2.1.0: Scene markers & GM notes
# ------------------------------------------------------------------ #
def test_scene_command():
    """GM /scene sets current scene and writes to transcript."""
    _reset()
    config = _make_config()
    state = _make_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 9100,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 999, "first_name": "GM"},
            "date": now_ts,
            "text": "/scene The Docks at Midnight",
        },
    }]

    checker.process_updates(updates, config, state)
    assert state.get("current_scenes", {}).get("100") == "The Docks at Midnight"
    scene_msgs = [m for m in _sent_messages if "Scene" in m.get("text", "")]
    assert len(scene_msgs) >= 1


def test_scene_no_name():
    """GM /scene with no name shows usage."""
    _reset()
    config = _make_config()
    state = _make_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 9101,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 999, "first_name": "GM"},
            "date": now_ts,
            "text": "/scene",
        },
    }]

    checker.process_updates(updates, config, state)
    assert "100" not in state.get("current_scenes", {})
    usage_msgs = [m for m in _sent_messages if "Usage" in m.get("text", "")]
    assert len(usage_msgs) >= 1


def test_scene_non_gm_ignored():
    """Non-GM /scene is ignored."""
    _reset()
    config = _make_config()
    state = _make_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 9102,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 42, "first_name": "Player"},
            "date": now_ts,
            "text": "/scene Sneaky Scene",
        },
    }]

    checker.process_updates(updates, config, state)
    assert "100" not in state.get("current_scenes", {})


def test_scene_shows_in_status():
    """Scene name appears in /status output."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["current_scenes"] = {"100": "The Haunted Chapel"}
    result = checker._build_status("100", "TestCampaign", state, {999})
    assert "The Haunted Chapel" in result


def test_note_command():
    """GM /note adds a persistent note."""
    _reset()
    config = _make_config()
    state = _make_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 9110,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 999, "first_name": "GM"},
            "date": now_ts,
            "text": "/note Party agreed to meet the informant at dawn",
        },
    }]

    checker.process_updates(updates, config, state)
    notes = state.get("campaign_notes", {}).get("100", [])
    assert len(notes) == 1
    assert notes[0]["text"] == "Party agreed to meet the informant at dawn"
    saved_msgs = [m for m in _sent_messages if "saved" in m.get("text", "").lower()]
    assert len(saved_msgs) >= 1


def test_note_no_text():
    """GM /note with no text shows usage."""
    _reset()
    config = _make_config()
    state = _make_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 9111,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 999, "first_name": "GM"},
            "date": now_ts,
            "text": "/note",
        },
    }]

    checker.process_updates(updates, config, state)
    assert len(state.get("campaign_notes", {}).get("100", [])) == 0


def test_note_max_limit():
    """Notes capped at 20 per campaign."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["campaign_notes"] = {"100": [
        {"text": f"Note {i}", "created_at": "2026-01-01T00:00:00+00:00"}
        for i in range(20)
    ]}
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 9112,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 999, "first_name": "GM"},
            "date": now_ts,
            "text": "/note One too many",
        },
    }]

    checker.process_updates(updates, config, state)
    assert len(state["campaign_notes"]["100"]) == 20
    max_msgs = [m for m in _sent_messages if "Maximum" in m.get("text", "")]
    assert len(max_msgs) >= 1


def test_notes_command():
    """Anyone can view notes with /notes."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["campaign_notes"] = {"100": [
        {"text": "First note", "created_at": "2026-01-15T10:00:00+00:00"},
        {"text": "Second note", "created_at": "2026-01-16T10:00:00+00:00"},
    ]}
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 9113,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 42, "first_name": "Player"},
            "date": now_ts,
            "text": "/notes",
        },
    }]

    checker.process_updates(updates, config, state)
    notes_msgs = [m for m in _sent_messages if "First note" in m.get("text", "")]
    assert len(notes_msgs) >= 1


def test_notes_empty():
    """/notes with no notes shows helpful message."""
    _reset()
    result = checker._build_notes("100", "TestCampaign", {})
    assert "No GM notes" in result


def test_delnote_command():
    """GM /delnote removes a note by number."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["campaign_notes"] = {"100": [
        {"text": "Keep this", "created_at": "2026-01-15T10:00:00+00:00"},
        {"text": "Delete this", "created_at": "2026-01-16T10:00:00+00:00"},
    ]}
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 9114,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 999, "first_name": "GM"},
            "date": now_ts,
            "text": "/delnote 2",
        },
    }]

    checker.process_updates(updates, config, state)
    notes = state["campaign_notes"]["100"]
    assert len(notes) == 1
    assert notes[0]["text"] == "Keep this"
    del_msgs = [m for m in _sent_messages if "Deleted" in m.get("text", "")]
    assert len(del_msgs) >= 1


def test_delnote_invalid_number():
    """GM /delnote with invalid number shows error."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["campaign_notes"] = {"100": [
        {"text": "A note", "created_at": "2026-01-15T10:00:00+00:00"},
    ]}
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 9115,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 999, "first_name": "GM"},
            "date": now_ts,
            "text": "/delnote 5",
        },
    }]

    checker.process_updates(updates, config, state)
    assert len(state["campaign_notes"]["100"]) == 1
    err_msgs = [m for m in _sent_messages if "not found" in m.get("text", "")]
    assert len(err_msgs) >= 1


def test_scene_shows_in_campaign():
    """Scene name appears in /campaign output."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["current_scenes"] = {"100": "The Grand Library"}
    result = checker._build_campaign_report("100", config, state, {999})
    assert "The Grand Library" in result


def test_notes_show_in_campaign():
    """Notes appear in /campaign output."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["campaign_notes"] = {"100": [
        {"text": "Remember the artifact", "created_at": "2026-01-15T10:00:00+00:00"},
    ]}
    result = checker._build_campaign_report("100", config, state, {999})
    assert "Remember the artifact" in result


def test_write_scene_marker():
    """Scene marker writes correct markdown to transcript."""
    import tempfile, pathlib
    original_dir = checker._LOGS_DIR
    with tempfile.TemporaryDirectory() as tmp:
        checker._LOGS_DIR = pathlib.Path(tmp)
        try:
            checker._write_scene_marker("Test Campaign", "The Final Battle")
            campaign_dir = pathlib.Path(tmp) / "Test_Campaign"
            assert campaign_dir.exists()
            md_files = list(campaign_dir.glob("*.md"))
            assert len(md_files) == 1
            content = md_files[0].read_text()
            assert "### 🎭 Scene: The Final Battle" in content
        finally:
            checker._LOGS_DIR = original_dir


# ------------------------------------------------------------------ #
#  v2.2.0: Activity insights
# ------------------------------------------------------------------ #
def test_activity_tracking():
    """Messages record hour and day counters in state."""
    _reset()
    config = _make_config()
    state = _make_state()
    # Use a known time: Wednesday (weekday=2) at 14:30 UTC
    from datetime import datetime as dt
    wed_14 = int(dt(2026, 2, 25, 14, 30, tzinfo=timezone.utc).timestamp())

    updates = [{
        "update_id": 9200,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 42, "first_name": "Alice"},
            "date": wed_14,
            "text": "I search the room carefully.",
        },
    }]

    checker.process_updates(updates, config, state)
    hours = state.get("activity_hours", {}).get("100", {}).get("42", {})
    days = state.get("activity_days", {}).get("100", {}).get("42", {})
    assert hours.get("14", 0) == 1
    assert days.get("2", 0) == 1  # Wednesday = 2


def test_activity_command():
    """/activity shows pattern report when data exists."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["activity_hours"] = {"100": {
        "42": {"14": 10, "15": 5, "20": 3},
        "999": {"10": 8, "14": 4},
    }}
    state["activity_days"] = {"100": {
        "42": {"0": 5, "2": 8, "4": 5},
        "999": {"1": 4, "3": 8},
    }}

    result = checker._build_activity("100", "TestCampaign", state, {999})
    assert "Activity Patterns" in result
    assert "Busiest days" in result
    assert "Busiest times" in result
    assert "Peak hour" in result


def test_activity_empty():
    """/activity with no data shows helpful message."""
    _reset()
    result = checker._build_activity("100", "TestCampaign", {}, {999})
    assert "No activity data" in result


def test_activity_command_via_message():
    """/activity sent as a message produces a response."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["activity_hours"] = {"100": {"42": {"14": 5}}}
    state["activity_days"] = {"100": {"42": {"2": 5}}}
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 9201,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 42, "first_name": "Alice"},
            "date": now_ts,
            "text": "/activity",
        },
    }]

    checker.process_updates(updates, config, state)
    activity_msgs = [m for m in _sent_messages if "Activity" in m.get("text", "")]
    assert len(activity_msgs) >= 1


def test_profile_command():
    """/profile shows cross-campaign stats for a player."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["players"] = {
        "100:42": {
            "user_id": "42", "first_name": "Alice", "last_name": "",
            "username": "alice", "campaign_name": "TestCampaign",
            "pbp_topic_id": "100", "last_post_time": datetime.now(timezone.utc).isoformat(),
            "last_warned_week": 0,
        },
    }
    state["message_counts"] = {"100": {"42": 25}}
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 9202,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 42, "first_name": "Alice"},
            "date": now_ts,
            "text": "/profile alice",
        },
    }]

    checker.process_updates(updates, config, state)
    profile_msgs = [m for m in _sent_messages if "Alice" in m.get("text", "")]
    assert len(profile_msgs) >= 1


def test_profile_not_found():
    """/profile with unknown player shows error."""
    _reset()
    result = checker._build_profile("nonexistent", _make_config(), _make_state())
    assert "No player matching" in result


def test_profile_no_target():
    """/profile with no name shows usage."""
    _reset()
    config = _make_config()
    state = _make_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())

    updates = [{
        "update_id": 9203,
        "message": {
            "chat": {"id": -100},
            "message_thread_id": 100,
            "from": {"id": 42, "first_name": "Alice"},
            "date": now_ts,
            "text": "/profile",
        },
    }]

    checker.process_updates(updates, config, state)
    usage_msgs = [m for m in _sent_messages if "Usage" in m.get("text", "")]
    assert len(usage_msgs) >= 1


def test_profile_cross_campaign():
    """/profile shows stats across multiple campaigns."""
    _reset()
    config = _make_config(pairs=[
        {"name": "Campaign A", "chat_topic_id": 200, "pbp_topic_ids": [100]},
        {"name": "Campaign B", "chat_topic_id": 400, "pbp_topic_ids": [300]},
    ])
    state = _make_state()
    now = datetime.now(timezone.utc).isoformat()
    state["players"] = {
        "100:42": {
            "user_id": "42", "first_name": "Alice", "last_name": "",
            "username": "alice", "campaign_name": "Campaign A",
            "pbp_topic_id": "100", "last_post_time": now,
            "last_warned_week": 0,
        },
        "300:42": {
            "user_id": "42", "first_name": "Alice", "last_name": "",
            "username": "alice", "campaign_name": "Campaign B",
            "pbp_topic_id": "300", "last_post_time": now,
            "last_warned_week": 0,
        },
    }
    state["message_counts"] = {"100": {"42": 15}, "300": {"42": 10}}

    result = checker._build_profile("alice", config, state)
    assert "Campaign A" in result
    assert "Campaign B" in result
    assert "25 posts across 2 campaigns" in result


# ------------------------------------------------------------------ #
#  /away and /back command tests
# ------------------------------------------------------------------ #
def test_away_command():
    """/away marks player as away and skips warnings."""
    _reset()
    config = _make_config()
    state = _make_state()
    now = datetime.now(timezone.utc)
    state["players"] = {
        "100:42": {
            "user_id": "42", "first_name": "Alice", "last_name": "",
            "username": "alice", "campaign_name": "TestCampaign",
            "pbp_topic_id": "100", "last_post_time": now.isoformat(),
            "last_warned_week": 0,
        },
    }

    updates = [_make_msg(1, 100, "/away 3 days vacation", user_id=42, first_name="Alice")]
    checker.process_updates(updates, config, state)

    assert "100:42" in state.get("away", {}), "Away record should be created"
    record = state["away"]["100:42"]
    assert record["reason"] == "vacation"
    assert record["until"] is not None
    assert "✈️" in _sent_messages[-1]["text"]


def test_away_indefinite():
    """/away without duration is indefinite."""
    _reset()
    config = _make_config()
    state = _make_state()
    now = datetime.now(timezone.utc)
    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }

    updates = [_make_msg(1, 100, "/away busy with work", user_id=42, first_name="Alice")]
    checker.process_updates(updates, config, state)

    record = state["away"]["100:42"]
    assert record["until"] is None
    assert record["reason"] == "busy with work"


def test_back_command():
    """/back clears away status."""
    _reset()
    config = _make_config()
    state = _make_state()
    now = datetime.now(timezone.utc)
    state["away"] = {
        "100:42": {"until": None, "reason": "holiday", "set_at": now.isoformat()}
    }
    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }

    updates = [_make_msg(1, 100, "/back", user_id=42, first_name="Alice")]
    checker.process_updates(updates, config, state)

    assert "100:42" not in state.get("away", {}), "Away record should be cleared"
    assert "👋" in _sent_messages[-1]["text"]


def test_away_auto_clear_on_post():
    """Posting a non-command message auto-clears away status."""
    _reset()
    config = _make_config()
    state = _make_state()
    now = datetime.now(timezone.utc)
    state["away"] = {
        "100:42": {"until": None, "reason": "holiday", "set_at": now.isoformat()}
    }
    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }

    updates = [_make_msg(1, 100, "I check the chest for traps.", user_id=42, first_name="Alice")]
    checker.process_updates(updates, config, state)

    assert "100:42" not in state.get("away", {}), "Away should auto-clear on post"


def test_away_skips_warnings():
    """Away players should be skipped in inactivity warnings."""
    _reset()
    config = _make_config()
    state = _make_state()
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=10)).isoformat()
    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": old,
        "last_warned_week": 0,
    }
    # Mark as away
    state["away"] = {
        "100:42": {"until": None, "reason": "holiday", "set_at": now.isoformat()}
    }

    _sent_messages.clear()
    checker.check_player_activity(config, state, now=now)

    # Should NOT have sent any warning
    warning_msgs = [m for m in _sent_messages if "Alice" in m["text"] and "not posted" in m["text"]]
    assert len(warning_msgs) == 0, f"Away player should not get warned, got: {_sent_messages}"


def test_away_skips_combat_ping():
    """Away players should be excluded from combat ping missing list."""
    _reset()
    config = _make_config()
    state = _make_state()
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=5)).isoformat()

    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "alice", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": old,
        "last_warned_week": 0,
    }
    state["combat"]["100"] = {
        "active": True, "round": 1, "current_phase": "players",
        "phase_started_at": old, "last_ping_at": None,
        "players_acted": [], "campaign_name": "TestCampaign",
    }
    # Mark as away
    state["away"] = {
        "100:42": {"until": None, "reason": "holiday", "set_at": now.isoformat()}
    }

    _sent_messages.clear()
    checker.check_combat_turns(config, state, now=now)

    # Should NOT ping Alice
    pings = [m for m in _sent_messages if "Alice" in m["text"]]
    assert len(pings) == 0, f"Away player should not be pinged, got: {_sent_messages}"


def test_away_shows_in_status():
    """Away players should appear in /status output."""
    _reset()
    config = _make_config()
    state = _make_state()
    now = datetime.now(timezone.utc)
    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }
    state["away"] = {
        "100:42": {"until": None, "reason": "holiday", "set_at": now.isoformat()}
    }

    result = checker._build_status("100", "TestCampaign", state, {"999"})
    assert "✈️ Away:" in result
    assert "Alice" in result


def test_away_expiry():
    """Away records with passed 'until' date should auto-expire."""
    state = {"away": {
        "100:42": {
            "until": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            "reason": "short break",
            "set_at": datetime.now(timezone.utc).isoformat(),
        }
    }}
    result = helpers.is_away(state, "100", "42", datetime.now(timezone.utc))
    assert result is None, "Expired away should return None"
    assert "100:42" not in state["away"], "Expired record should be cleaned up"


def test_away_shows_in_party():
    """Away players should be marked in /party output."""
    _reset()
    config = _make_config(pairs=[{
        "name": "TestCampaign", "chat_topic_id": 200, "pbp_topic_ids": [100],
        "characters": {"42": "Cardigan"},
    }])
    state = _make_state()
    now = datetime.now(timezone.utc)
    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }
    state["away"] = {
        "100:42": {"until": None, "reason": "vacation", "set_at": now.isoformat()}
    }

    result = checker._build_party("100", "TestCampaign", config, state)
    assert "✈️ away" in result
    assert "vacation" in result


# ------------------------------------------------------------------ #
#  /recap command tests
# ------------------------------------------------------------------ #
def test_recap_basic():
    """_build_recap returns recent transcript entries."""
    import pathlib
    campaign_dir = pathlib.Path(checker._LOGS_DIR) / "TestCampaign"
    campaign_dir.mkdir(parents=True, exist_ok=True)

    # Write a test transcript file
    content = (
        "# TestCampaign — 2026-02\n\n"
        "*PBP transcript archived by PathWarsNudge bot.*\n\n---\n\n"
        "**Alice** (2026-02-26 10:00:00):\nI search the room.\n\n"
        "**Bob** [GM] (2026-02-26 10:05:00):\nYou find a hidden door.\n\n"
        "**Alice** (2026-02-26 10:10:00):\nI open the door cautiously.\n\n"
    )
    (campaign_dir / "2026-02.md").write_text(content, encoding="utf-8")

    config = _make_config()
    result = checker._build_recap("100", "TestCampaign", config, 10)

    assert "📜 Recap" in result
    assert "Alice" in result
    assert "Bob" in result
    assert "I search the room" in result


def test_recap_no_transcript():
    """_build_recap handles missing transcripts gracefully."""
    config = _make_config()
    result = checker._build_recap("100", "NoCampaign", config, 10)
    assert "No transcript archive" in result


def test_recap_command():
    """/recap command sends transcript entries."""
    import pathlib
    _reset()
    campaign_dir = pathlib.Path(checker._LOGS_DIR) / "TestCampaign"
    campaign_dir.mkdir(parents=True, exist_ok=True)
    content = (
        "# TestCampaign — 2026-02\n\n"
        "*PBP transcript archived by PathWarsNudge bot.*\n\n---\n\n"
        "**Alice** (2026-02-26 10:00:00):\nHello world.\n\n"
    )
    (campaign_dir / "2026-02.md").write_text(content, encoding="utf-8")

    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/recap", user_id=42, first_name="Alice")]
    checker.process_updates(updates, config, state)

    recap_msgs = [m for m in _sent_messages if "📜" in m["text"]]
    assert len(recap_msgs) >= 1, "Should send recap message"


def test_recap_with_count():
    """/recap 5 limits to 5 entries."""
    import pathlib
    campaign_dir = pathlib.Path(checker._LOGS_DIR) / "TestCampaign"
    campaign_dir.mkdir(parents=True, exist_ok=True)
    entries = ""
    for i in range(20):
        entries += f"**Alice** (2026-02-26 {10+i//60:02d}:{i%60:02d}:00):\nEntry {i+1}.\n\n"
    content = (
        "# TestCampaign — 2026-02\n\n"
        "*PBP transcript archived by PathWarsNudge bot.*\n\n---\n\n"
        + entries
    )
    (campaign_dir / "2026-02.md").write_text(content, encoding="utf-8")

    config = _make_config()
    result = checker._build_recap("100", "TestCampaign", config, 5)
    # Should show exactly 5 entries
    assert "last 5" in result


def test_recap_gm_tag():
    """Recap shows 🎲 for GM posts."""
    import pathlib
    campaign_dir = pathlib.Path(checker._LOGS_DIR) / "TestCampaign"
    campaign_dir.mkdir(parents=True, exist_ok=True)
    content = (
        "# TestCampaign — 2026-02\n\n"
        "*PBP transcript archived by PathWarsNudge bot.*\n\n---\n\n"
        "**Lewis** [GM] (2026-02-26 10:00:00):\nThe ogre swings at you.\n\n"
        "**Alice** (Cardigan) (2026-02-26 10:05:00):\nI dodge!\n\n"
    )
    (campaign_dir / "2026-02.md").write_text(content, encoding="utf-8")

    config = _make_config()
    result = checker._build_recap("100", "TestCampaign", config, 10)
    assert "🎲 Lewis" in result
    assert "Cardigan" in result
    # Alice's real name shouldn't show when char name exists
    # (Cardigan should be the display name)


def test_recap_scene_boundary():
    """Recap shows scene markers."""
    import pathlib
    campaign_dir = pathlib.Path(checker._LOGS_DIR) / "TestCampaign"
    campaign_dir.mkdir(parents=True, exist_ok=True)
    content = (
        "# TestCampaign — 2026-02\n\n"
        "*PBP transcript archived by PathWarsNudge bot.*\n\n---\n\n"
        "**Alice** (2026-02-26 10:00:00):\nOld scene post.\n\n"
        "\n---\n\n### 🎭 Scene: The Dark Cave\n*(2026-02-26 10:30)*\n\n---\n\n"
        "**Alice** (2026-02-26 10:35:00):\nI enter the cave.\n\n"
    )
    (campaign_dir / "2026-02.md").write_text(content, encoding="utf-8")

    config = _make_config()
    result = checker._build_recap("100", "TestCampaign", config, 10)
    assert "The Dark Cave" in result
    assert "━━━" in result


def test_recap_time_gap():
    """Recap shows time gaps between posts."""
    import pathlib
    campaign_dir = pathlib.Path(checker._LOGS_DIR) / "TestCampaign"
    campaign_dir.mkdir(parents=True, exist_ok=True)
    content = (
        "# TestCampaign — 2026-02\n\n"
        "*PBP transcript archived by PathWarsNudge bot.*\n\n---\n\n"
        "**Alice** (2026-02-26 08:00:00):\nMorning post.\n\n"
        "**Bob** (2026-02-26 20:00:00):\nEvening post.\n\n"
    )
    (campaign_dir / "2026-02.md").write_text(content, encoding="utf-8")

    config = _make_config()
    result = checker._build_recap("100", "TestCampaign", config, 10)
    assert "later" in result  # "12h later" gap indicator


def test_catchup_shows_combat_acted():
    """Catchup tells player if they've already acted in combat."""
    _reset()
    now = datetime.now(timezone.utc)
    state = _make_state()
    state["post_timestamps"]["100"] = {
        "42": [(now - timedelta(hours=5)).isoformat()],
        "999": [(now - timedelta(hours=2)).isoformat()],
    }
    state["combat"]["100"] = {
        "active": True, "round": 1, "current_phase": "players",
        "players_acted": {"42": now.isoformat()},
    }

    result = checker._build_catchup("100", "42", "TestCampaign", state, {"999"})
    assert "already acted" in result


# ------------------------------------------------------------------ #
#  helpers.parse_away_duration tests
# ------------------------------------------------------------------ #
def test_parse_away_duration_days():
    """Parse '3 days reason'."""
    now = datetime(2026, 2, 27, 12, 0, tzinfo=timezone.utc)
    until, reason = helpers.parse_away_duration("3 days vacation", now)
    assert until is not None
    assert (until - now).days == 3
    assert reason == "vacation"


def test_parse_away_duration_weeks():
    """Parse '2 weeks'."""
    now = datetime(2026, 2, 27, 12, 0, tzinfo=timezone.utc)
    until, reason = helpers.parse_away_duration("2 weeks", now)
    assert until is not None
    assert (until - now).days == 14
    assert reason == "Away"


def test_parse_away_duration_indefinite():
    """Parse plain text as indefinite."""
    now = datetime(2026, 2, 27, 12, 0, tzinfo=timezone.utc)
    until, reason = helpers.parse_away_duration("busy with real life stuff", now)
    assert until is None
    assert reason == "busy with real life stuff"


def test_parse_away_duration_empty():
    """Empty text gives indefinite with default reason."""
    now = datetime(2026, 2, 27, 12, 0, tzinfo=timezone.utc)
    until, reason = helpers.parse_away_duration("", now)
    assert until is None
    assert reason == "No reason given"


# ------------------------------------------------------------------ #
#  Dice roller tests
# ------------------------------------------------------------------ #
def test_roll_basic():
    """Basic 1d20 roll."""
    result = helpers.roll_dice("1d20")
    assert result["error"] is None
    assert len(result["results"]) == 1
    r = result["results"][0]
    assert 1 <= r["total"] <= 20
    assert len(r["rolls"]) == 1


def test_roll_with_modifier():
    """1d20+5 adds modifier to total."""
    result = helpers.roll_dice("1d20+5")
    assert result["error"] is None
    r = result["results"][0]
    assert r["modifier"] == 5
    assert r["total"] == r["rolls"][0] + 5


def test_roll_negative_modifier():
    """1d20-3 subtracts modifier."""
    result = helpers.roll_dice("1d20-3")
    assert result["error"] is None
    r = result["results"][0]
    assert r["modifier"] == -3
    assert r["total"] == r["rolls"][0] - 3


def test_roll_multiple_dice():
    """2d6 rolls two dice."""
    result = helpers.roll_dice("2d6")
    assert result["error"] is None
    r = result["results"][0]
    assert len(r["rolls"]) == 2
    assert all(1 <= x <= 6 for x in r["rolls"])
    assert r["total"] == sum(r["rolls"])


def test_roll_keep_highest():
    """4d6kh3 keeps highest 3."""
    result = helpers.roll_dice("4d6kh3")
    assert result["error"] is None
    r = result["results"][0]
    assert len(r["rolls"]) == 4
    assert len(r["kept"]) == 3
    assert r["total"] == sum(r["kept"])
    # Kept should be the 3 highest
    assert sorted(r["kept"], reverse=True) == r["kept"]


def test_roll_keep_lowest():
    """2d20kl1 keeps lowest."""
    result = helpers.roll_dice("2d20kl1")
    assert result["error"] is None
    r = result["results"][0]
    assert len(r["rolls"]) == 2
    assert len(r["kept"]) == 1
    assert r["total"] == min(r["rolls"])


def test_roll_with_label():
    """1d20+12 Stealth extracts label."""
    result = helpers.roll_dice("1d20+12 Stealth")
    assert result["error"] is None
    assert result["label"] == "Stealth"
    assert len(result["results"]) == 1


def test_roll_multiple_expressions():
    """1d20+5 2d6+3 rolls both."""
    result = helpers.roll_dice("1d20+5 2d6+3")
    assert result["error"] is None
    assert len(result["results"]) == 2


def test_roll_no_dice():
    """Invalid expression returns error."""
    result = helpers.roll_dice("hello")
    assert result["error"] is not None


def test_roll_empty():
    """Empty expression returns error."""
    result = helpers.roll_dice("")
    assert result["error"] is not None


def test_roll_command():
    """/roll processes dice and sends result."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/roll 1d20+5 Stealth", user_id=42, first_name="Alice")]
    checker.process_updates(updates, config, state)

    roll_msgs = [m for m in _sent_messages if "🎲" in m.get("text", "")]
    assert len(roll_msgs) >= 1, f"Should send dice result, got: {_sent_messages}"
    assert "Stealth" in roll_msgs[0]["text"]


def test_roll_command_no_args():
    """/roll with no args shows usage."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/roll", user_id=42, first_name="Alice")]
    checker.process_updates(updates, config, state)

    assert any("Usage" in m.get("text", "") for m in _sent_messages)


# ------------------------------------------------------------------ #
#  Quest tracker tests
# ------------------------------------------------------------------ #
def test_quest_add():
    """/quest adds a quest to the campaign."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/quest Find the missing merchant", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    quests = state.get("quests", {}).get("100", [])
    assert len(quests) == 1
    assert quests[0]["text"] == "Find the missing merchant"
    assert quests[0]["status"] == "active"
    assert "📋" in _sent_messages[-1]["text"]


def test_quest_non_gm():
    """/quest from non-GM should be ignored."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/quest Hack the system", user_id=42, first_name="Player")]
    checker.process_updates(updates, config, state)

    quests = state.get("quests", {}).get("100", [])
    assert len(quests) == 0


def test_quests_list():
    """/quests shows active and completed quests."""
    _reset()
    config = _make_config()
    state = _make_state()
    now = datetime.now(timezone.utc).isoformat()
    state["quests"] = {
        "100": [
            {"text": "Find the gem", "status": "active", "created_at": now, "completed_at": None},
            {"text": "Save the prince", "status": "completed", "created_at": now, "completed_at": now},
        ]
    }

    result = checker._build_quests("100", "TestCampaign", state)
    assert "Find the gem" in result
    assert "Save the prince" in result
    assert "1 active" in result
    assert "1 completed" in result


def test_quest_done():
    """/done marks a quest as completed."""
    _reset()
    config = _make_config()
    state = _make_state()
    now = datetime.now(timezone.utc).isoformat()
    state["quests"] = {
        "100": [{"text": "Find the gem", "status": "active", "created_at": now, "completed_at": None}]
    }

    updates = [_make_msg(1, 100, "/done 1", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert state["quests"]["100"][0]["status"] == "completed"
    assert state["quests"]["100"][0]["completed_at"] is not None
    assert "✅" in _sent_messages[-1]["text"]


def test_quest_delete():
    """/delquest removes a quest entirely."""
    _reset()
    config = _make_config()
    state = _make_state()
    now = datetime.now(timezone.utc).isoformat()
    state["quests"] = {
        "100": [{"text": "Find the gem", "status": "active", "created_at": now, "completed_at": None}]
    }

    updates = [_make_msg(1, 100, "/delquest 1", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert len(state["quests"]["100"]) == 0
    assert "🗑️" in _sent_messages[-1]["text"]


def test_quests_empty():
    """/quests with no quests shows helpful message."""
    result = checker._build_quests("100", "TestCampaign", {"quests": {}})
    assert "No quests" in result


# ------------------------------------------------------------------ #
#  GM dashboard tests
# ------------------------------------------------------------------ #
def test_gm_dashboard():
    """/gm shows all campaigns with health info."""
    _reset()
    config = _make_config(pairs=[
        {"name": "Campaign A", "chat_topic_id": 200, "pbp_topic_ids": [100]},
        {"name": "Campaign B", "chat_topic_id": 400, "pbp_topic_ids": [300]},
    ])
    state = _make_state()
    now = datetime.now(timezone.utc)
    state["players"] = {
        "100:42": {
            "user_id": "42", "first_name": "Alice", "last_name": "",
            "username": "", "campaign_name": "Campaign A",
            "pbp_topic_id": "100", "last_post_time": now.isoformat(),
            "last_warned_week": 0,
        },
    }
    state["topics"]["100"] = {
        "last_message_time": now.isoformat(),
        "last_user": "Alice", "last_user_id": "42",
        "campaign_name": "Campaign A",
    }

    result = checker._build_gm_dashboard(config, state)
    assert "📊 GM Dashboard" in result
    assert "Campaign A" in result
    assert "Campaign B" in result


def test_gm_command_requires_gm():
    """/gm only works for GMs."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/gm", user_id=42, first_name="Player")]
    checker.process_updates(updates, config, state)

    gm_msgs = [m for m in _sent_messages if "GM Dashboard" in m.get("text", "")]
    assert len(gm_msgs) == 0, "Non-GM should not see dashboard"


def test_gm_command_works_for_gm():
    """/gm works for GMs."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/gm", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    gm_msgs = [m for m in _sent_messages if "GM Dashboard" in m.get("text", "")]
    assert len(gm_msgs) >= 1


# ------------------------------------------------------------------ #
#  Pin tests
# ------------------------------------------------------------------ #
def test_pin_add():
    """/pin adds a bookmark."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/pin The dragon revealed its weakness", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    pins = state.get("pins", {}).get("100", [])
    assert len(pins) == 1
    assert pins[0]["text"] == "The dragon revealed its weakness"
    assert pins[0]["author"] == "GM"
    assert "📌" in _sent_messages[-1]["text"]


def test_pin_non_gm():
    """/pin from non-GM is ignored."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/pin some note", user_id=42, first_name="Player")]
    checker.process_updates(updates, config, state)

    pins = state.get("pins", {}).get("100", [])
    assert len(pins) == 0


def test_pins_list():
    """/pins shows all bookmarks."""
    state = {"pins": {"100": [
        {"text": "Found the key", "created_at": "2026-02-27T10:00:00+00:00", "author": "GM"},
        {"text": "Met the dragon", "created_at": "2026-02-28T10:00:00+00:00", "author": "GM"},
    ]}}
    result = checker._build_pins("100", "TestCampaign", state)
    assert "Found the key" in result
    assert "Met the dragon" in result
    assert "2/30 pins" in result


def test_delpin():
    """/delpin removes a pin."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["pins"] = {"100": [
        {"text": "Pin one", "created_at": "2026-02-27T10:00:00+00:00", "author": "GM"},
    ]}

    updates = [_make_msg(1, 100, "/delpin 1", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert len(state["pins"]["100"]) == 0
    assert "🗑️" in _sent_messages[-1]["text"]


# ------------------------------------------------------------------ #
#  Loot tests
# ------------------------------------------------------------------ #
def test_loot_add():
    """/loot adds an item."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/loot +1 striking longsword", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    loot = state.get("loot", {}).get("100", [])
    assert len(loot) == 1
    assert loot[0]["text"] == "+1 striking longsword"
    assert "💰" in _sent_messages[-1]["text"]


def test_loot_non_gm():
    """/loot from non-GM is ignored."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/loot stolen gem", user_id=42, first_name="Player")]
    checker.process_updates(updates, config, state)

    loot = state.get("loot", {}).get("100", [])
    assert len(loot) == 0


def test_lootlist():
    """/lootlist shows all items."""
    state = {"loot": {"100": [
        {"text": "+1 longsword", "added_at": "2026-02-27T10:00:00+00:00"},
        {"text": "500 gp", "added_at": "2026-02-28T10:00:00+00:00"},
    ]}}
    result = checker._build_lootlist("100", "TestCampaign", state)
    assert "+1 longsword" in result
    assert "500 gp" in result
    assert "2/50 items" in result


def test_delloot():
    """/delloot removes an item."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["loot"] = {"100": [
        {"text": "+1 longsword", "added_at": "2026-02-27T10:00:00+00:00"},
    ]}

    updates = [_make_msg(1, 100, "/delloot 1", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert len(state["loot"]["100"]) == 0
    assert "🗑️" in _sent_messages[-1]["text"]


# ------------------------------------------------------------------ #
#  DC lookup tests
# ------------------------------------------------------------------ #
def test_dc_level():
    """DC lookup for level 5."""
    result = helpers.dc_lookup("5")
    assert "Level 5" in result
    assert "DC 20" in result  # Standard DC for level 5


def test_dc_level_hard():
    """DC lookup for level 5 hard."""
    result = helpers.dc_lookup("5 hard")
    assert "DC 22" in result  # 20 + 2


def test_dc_proficiency():
    """Proficiency DC lookup."""
    result = helpers.dc_lookup("trained")
    assert "15" in result


def test_dc_legendary():
    """Legendary proficiency DC."""
    result = helpers.dc_lookup("legendary")
    assert "40" in result


def test_dc_alias():
    """Short alias works."""
    result = helpers.dc_lookup("vh")
    assert "Very Hard" in result


def test_dc_empty():
    """Empty query shows help."""
    result = helpers.dc_lookup("")
    assert "Usage" in result


def test_dc_command():
    """/dc command sends result."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/dc 10", user_id=42, first_name="Alice")]
    checker.process_updates(updates, config, state)

    dc_msgs = [m for m in _sent_messages if "Level 10" in m.get("text", "")]
    assert len(dc_msgs) >= 1


def test_dc_out_of_range():
    """Level out of range gives error."""
    result = helpers.dc_lookup("25")
    assert "0–20" in result


# ------------------------------------------------------------------ #
#  NPC tracker tests
# ------------------------------------------------------------------ #
def test_npc_add():
    """/npc adds an NPC with name and description."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/npc Gorund — Dwarven blacksmith", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    npcs = state.get("npcs", {}).get("100", [])
    assert len(npcs) == 1
    assert npcs[0]["name"] == "Gorund"
    assert npcs[0]["desc"] == "Dwarven blacksmith"
    assert "🎭" in _sent_messages[-1]["text"]


def test_npc_name_only():
    """/npc with just a name (no description)."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/npc Mysterious Stranger", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    npcs = state.get("npcs", {}).get("100", [])
    assert len(npcs) == 1
    assert npcs[0]["name"] == "Mysterious Stranger"
    assert npcs[0]["desc"] == ""


def test_npcs_list():
    """/npcs shows all NPCs."""
    state = {"npcs": {"100": [
        {"name": "Gorund", "desc": "Blacksmith", "added_at": "2026-02-27T10:00:00+00:00"},
        {"name": "Elara", "desc": "Temple priestess", "added_at": "2026-02-28T10:00:00+00:00"},
    ]}}
    result = checker._build_npcs("100", "TestCampaign", state)
    assert "Gorund" in result
    assert "Elara" in result
    assert "2/40 NPCs" in result


def test_delnpc():
    """/delnpc removes an NPC."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["npcs"] = {"100": [
        {"name": "Gorund", "desc": "Blacksmith", "added_at": "2026-02-27T10:00:00+00:00"},
    ]}

    updates = [_make_msg(1, 100, "/delnpc 1", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert len(state["npcs"]["100"]) == 0


def test_npc_non_gm():
    """/npc from non-GM is ignored."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/npc Bad Guy", user_id=42, first_name="Player")]
    checker.process_updates(updates, config, state)

    assert len(state.get("npcs", {}).get("100", [])) == 0


# ------------------------------------------------------------------ #
#  Condition tracker tests
# ------------------------------------------------------------------ #
def test_condition_add():
    """/condition adds a condition with target and effect."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/condition Cardigan — Frightened 2 | end of next turn", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    conds = state.get("conditions", {}).get("100", [])
    assert len(conds) == 1
    assert conds[0]["target"] == "Cardigan"
    assert conds[0]["effect"] == "Frightened 2"
    assert conds[0]["duration"] == "end of next turn"
    assert "⚡" in _sent_messages[-1]["text"]


def test_condition_no_duration():
    """/condition without duration."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/condition All — Inspired +1", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    conds = state.get("conditions", {}).get("100", [])
    assert len(conds) == 1
    assert conds[0]["duration"] == ""


def test_conditions_list():
    """/conditions shows all active conditions."""
    state = {"conditions": {"100": [
        {"target": "Cardigan", "effect": "Frightened 2", "duration": "1 round", "added_at": "2026-02-27T10:00:00+00:00"},
        {"target": "Rax", "effect": "Flat-footed", "duration": "", "added_at": "2026-02-27T10:00:00+00:00"},
    ]}}
    config = _make_config()
    result = checker._build_conditions("100", "TestCampaign", state, config)
    assert "Cardigan" in result
    assert "Frightened 2" in result
    assert "(1 round)" in result
    assert "Rax" in result
    assert "2 active" in result


def test_endcondition():
    """/endcondition removes a condition."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["conditions"] = {"100": [
        {"target": "Cardigan", "effect": "Frightened 2", "duration": "", "added_at": "2026-02-27T10:00:00+00:00"},
    ]}

    updates = [_make_msg(1, 100, "/endcondition 1", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert len(state["conditions"]["100"]) == 0
    assert "✅ Ended" in _sent_messages[-1]["text"]


def test_clearconditions():
    """/clearconditions removes all conditions."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["conditions"] = {"100": [
        {"target": "A", "effect": "X", "duration": "", "added_at": ""},
        {"target": "B", "effect": "Y", "duration": "", "added_at": ""},
    ]}

    updates = [_make_msg(1, 100, "/clearconditions", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert len(state["conditions"]["100"]) == 0
    assert "Cleared 2" in _sent_messages[-1]["text"]


def test_condition_non_gm():
    """/condition from non-GM is ignored."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/condition Me — Invincible", user_id=42, first_name="Player")]
    checker.process_updates(updates, config, state)

    assert len(state.get("conditions", {}).get("100", [])) == 0


# ------------------------------------------------------------------ #
#  Combat system v2 tests
# ------------------------------------------------------------------ #
def test_combat_start():
    """/combat starts combat with enemy roster."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/combat Ogre, 2 Skeletons", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    combat = state["combat"].get("100")
    assert combat is not None
    assert combat["active"] is True
    assert combat["round"] == 1
    assert combat["current_phase"] == "players"
    assert combat["enemies"] == ["Ogre", "2 Skeletons"]
    assert "⚔️" in _sent_messages[-1]["text"]
    assert "Ogre" in _sent_messages[-1]["text"]


def test_combat_start_no_enemies():
    """/combat works without enemy list."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/combat", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    combat = state["combat"].get("100")
    assert combat is not None
    assert combat["enemies"] == []


def test_next_players_to_enemies():
    """/next advances from players to enemies phase."""
    _reset()
    config = _make_config()
    state = _make_state()
    now = datetime.now(timezone.utc)
    state["combat"]["100"] = {
        "active": True, "round": 1, "current_phase": "players",
        "players_acted": {}, "last_ping_at": None, "enemies": [],
        "combat_log": [], "campaign_name": "TestCampaign",
        "phase_started_at": now.isoformat(), "started_at": now.isoformat(),
        "all_players_notified": False,
    }

    updates = [_make_msg(1, 100, "/next", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert state["combat"]["100"]["current_phase"] == "enemies"
    assert "Enemies" in _sent_messages[-1]["text"]


def test_next_enemies_to_new_round():
    """/next advances from enemies to next round players."""
    _reset()
    config = _make_config()
    state = _make_state()
    now = datetime.now(timezone.utc)
    state["combat"]["100"] = {
        "active": True, "round": 1, "current_phase": "enemies",
        "players_acted": {}, "last_ping_at": None, "enemies": [],
        "combat_log": [], "campaign_name": "TestCampaign",
        "phase_started_at": now.isoformat(), "started_at": now.isoformat(),
        "all_players_notified": False,
    }

    updates = [_make_msg(1, 100, "/next", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert state["combat"]["100"]["round"] == 2
    assert state["combat"]["100"]["current_phase"] == "players"
    assert state["combat"]["100"]["players_acted"] == {}
    assert "Round 2" in _sent_messages[-1]["text"]


def test_combat_auto_notify():
    """GM gets pinged when all players have acted."""
    _reset()
    config = _make_config()
    state = _make_state()
    now = datetime.now(timezone.utc)

    # Register two players
    state["players"]["100:42"] = {
        "user_id": "42", "first_name": "Alice", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }
    state["players"]["100:43"] = {
        "user_id": "43", "first_name": "Bob", "last_name": "",
        "username": "", "campaign_name": "TestCampaign",
        "pbp_topic_id": "100", "last_post_time": now.isoformat(),
        "last_warned_week": 0,
    }
    state["combat"]["100"] = {
        "active": True, "round": 1, "current_phase": "players",
        "players_acted": {"42": now.isoformat()}, "last_ping_at": None,
        "enemies": [], "combat_log": [], "campaign_name": "TestCampaign",
        "phase_started_at": now.isoformat(), "started_at": now.isoformat(),
        "all_players_notified": False,
    }

    # Bob posts — now everyone has acted
    updates = [_make_msg(1, 100, "I swing my axe!", user_id=43, first_name="Bob")]
    checker.process_updates(updates, config, state)

    # Should see auto-notify
    notify_msgs = [m for m in _sent_messages if "All players have posted" in m.get("text", "")]
    assert len(notify_msgs) >= 1
    assert state["combat"]["100"]["all_players_notified"] is True


def test_clog():
    """/clog adds a combat log entry."""
    _reset()
    config = _make_config()
    state = _make_state()
    now = datetime.now(timezone.utc)
    state["combat"]["100"] = {
        "active": True, "round": 2, "current_phase": "players",
        "players_acted": {}, "last_ping_at": None, "enemies": [],
        "combat_log": [], "campaign_name": "TestCampaign",
        "phase_started_at": now.isoformat(), "started_at": now.isoformat(),
        "all_players_notified": False,
    }

    updates = [_make_msg(1, 100, "/clog The ogre crits Cardigan for 28!", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    log = state["combat"]["100"]["combat_log"]
    assert len(log) == 1
    assert log[0]["round"] == 2
    assert "ogre crits" in log[0]["text"]


def test_combatlog_view():
    """/combatlog shows the log."""
    state = {"combat": {"100": {
        "active": True, "round": 3, "current_phase": "players",
        "combat_log": [
            {"round": 1, "text": "Combat begins!", "at": "2026-02-28T10:00:00+00:00"},
            {"round": 2, "text": "Ogre drops to 0 HP", "at": "2026-02-28T11:00:00+00:00"},
        ],
        "phase_started_at": "2026-02-28T12:00:00+00:00",
    }}}
    result = checker._build_combatlog("100", "TestCampaign", state)
    assert "Combat begins!" in result
    assert "Ogre drops" in result
    assert "R1:" in result
    assert "R2:" in result


def test_enemies_set():
    """/enemies sets enemy roster mid-combat."""
    _reset()
    config = _make_config()
    state = _make_state()
    now = datetime.now(timezone.utc)
    state["combat"]["100"] = {
        "active": True, "round": 1, "current_phase": "players",
        "players_acted": {}, "last_ping_at": None, "enemies": [],
        "combat_log": [], "campaign_name": "TestCampaign",
        "phase_started_at": now.isoformat(), "started_at": now.isoformat(),
        "all_players_notified": False,
    }

    updates = [_make_msg(1, 100, "/enemies Dragon, 3 Kobolds", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert state["combat"]["100"]["enemies"] == ["Dragon", "3 Kobolds"]


def test_endcombat_summary():
    """/endcombat shows combat log summary."""
    _reset()
    config = _make_config()
    state = _make_state()
    now = datetime.now(timezone.utc)
    state["combat"]["100"] = {
        "active": True, "round": 3, "current_phase": "enemies",
        "players_acted": {}, "last_ping_at": None, "enemies": ["Ogre"],
        "combat_log": [
            {"round": 1, "text": "Combat begins!", "at": now.isoformat()},
            {"round": 3, "text": "Ogre falls!", "at": now.isoformat()},
        ],
        "campaign_name": "TestCampaign",
        "phase_started_at": now.isoformat(), "started_at": now.isoformat(),
        "all_players_notified": False,
    }

    updates = [_make_msg(1, 100, "/endcombat", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    end_msgs = [m for m in _sent_messages if "Combat ended" in m.get("text", "")]
    assert len(end_msgs) >= 1
    assert "3 rounds" in end_msgs[0]["text"]
    assert "Ogre falls!" in end_msgs[0]["text"]
    assert "100" not in state["combat"]


def test_whosturn_with_enemies():
    """/whosturn shows enemy roster."""
    now = datetime.now(timezone.utc)
    state = _make_state()
    state["combat"]["100"] = {
        "active": True, "round": 1, "current_phase": "players",
        "players_acted": {}, "last_ping_at": None,
        "enemies": ["Ogre", "2 Skeletons"],
        "combat_log": [], "campaign_name": "TestCampaign",
        "phase_started_at": (now - timedelta(hours=1)).isoformat(),
        "started_at": now.isoformat(), "all_players_notified": False,
    }
    result = checker._build_whosturn("100", "TestCampaign", state)
    assert "Ogre" in result
    assert "2 Skeletons" in result


def test_format_elapsed():
    """_format_elapsed formats times correctly."""
    assert "30m" in checker._format_elapsed(0.5)
    assert "3h" in checker._format_elapsed(3.2)
    assert "1d" in checker._format_elapsed(26.5)


# ------------------------------------------------------------------ #
#  HP Tracker tests
# ------------------------------------------------------------------ #
def test_hp_set():
    """/hp set creates an HP entry."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/hp set Ogre 45/45", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    hp = state.get("hp_tracker", {}).get("100", {})
    assert "Ogre" in hp
    assert hp["Ogre"]["current"] == 45
    assert hp["Ogre"]["max"] == 45
    assert "█" in _sent_messages[-1]["text"]


def test_hp_damage():
    """/hp d deals damage."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["hp_tracker"] = {"100": {"Ogre": {"current": 45, "max": 45}}}

    updates = [_make_msg(1, 100, "/hp d Ogre 12", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert state["hp_tracker"]["100"]["Ogre"]["current"] == 33
    assert "12 damage" in _sent_messages[-1]["text"]


def test_hp_heal():
    """/hp h heals."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["hp_tracker"] = {"100": {"Ogre": {"current": 20, "max": 45}}}

    updates = [_make_msg(1, 100, "/hp h Ogre 10", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert state["hp_tracker"]["100"]["Ogre"]["current"] == 30
    assert "healed" in _sent_messages[-1]["text"]


def test_hp_kill():
    """/hp d that kills shows DOWN."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["hp_tracker"] = {"100": {"Ogre": {"current": 5, "max": 45}}}

    updates = [_make_msg(1, 100, "/hp d Ogre 20", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert state["hp_tracker"]["100"]["Ogre"]["current"] == 0
    assert "DOWN" in _sent_messages[-1]["text"]


def test_hp_remove():
    """/hp remove removes an entry."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["hp_tracker"] = {"100": {"Ogre": {"current": 45, "max": 45}}}

    updates = [_make_msg(1, 100, "/hp remove Ogre", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert "Ogre" not in state["hp_tracker"]["100"]


def test_hp_clear():
    """/hp clear removes all entries."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["hp_tracker"] = {"100": {
        "Ogre": {"current": 45, "max": 45},
        "Goblin": {"current": 10, "max": 10},
    }}

    updates = [_make_msg(1, 100, "/hp clear", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert len(state["hp_tracker"]["100"]) == 0


def test_hp_view():
    """/hp shows HP tracker."""
    state = {"hp_tracker": {"100": {
        "Ogre": {"current": 30, "max": 45},
        "Goblin": {"current": 0, "max": 10},
    }}}
    result = checker._build_hp_tracker("100", "TestCampaign", state)
    assert "Ogre" in result
    assert "Goblin" in result
    assert "█" in result
    assert "💀" in result  # Goblin at 0 HP


def test_hp_non_gm_view():
    """/hp from non-GM shows tracker (read-only)."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["hp_tracker"] = {"100": {"Ogre": {"current": 45, "max": 45}}}

    updates = [_make_msg(1, 100, "/hp", user_id=42, first_name="Player")]
    checker.process_updates(updates, config, state)

    hp_msgs = [m for m in _sent_messages if "Ogre" in m.get("text", "")]
    assert len(hp_msgs) >= 1


def test_hp_no_heal_over_max():
    """/hp h doesn't overheal past max."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["hp_tracker"] = {"100": {"Ogre": {"current": 40, "max": 45}}}

    updates = [_make_msg(1, 100, "/hp h Ogre 100", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert state["hp_tracker"]["100"]["Ogre"]["current"] == 45


# ------------------------------------------------------------------ #
#  Progress Clock tests
# ------------------------------------------------------------------ #
def test_clock_create():
    """/clock creates a progress clock."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/clock Investigation 6", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    clocks = state.get("clocks", {}).get("100", {})
    assert "Investigation" in clocks
    assert clocks["Investigation"]["segments"] == 6
    assert clocks["Investigation"]["filled"] == 0
    assert "○" in _sent_messages[-1]["text"]


def test_tick():
    """/tick advances a clock."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["clocks"] = {"100": {"Investigation": {"filled": 2, "segments": 6}}}

    updates = [_make_msg(1, 100, "/tick Investigation", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert state["clocks"]["100"]["Investigation"]["filled"] == 3


def test_tick_amount():
    """/tick with amount advances multiple segments."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["clocks"] = {"100": {"Investigation": {"filled": 1, "segments": 6}}}

    updates = [_make_msg(1, 100, "/tick Investigation 3", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert state["clocks"]["100"]["Investigation"]["filled"] == 4


def test_tick_complete():
    """/tick that completes a clock shows COMPLETE."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["clocks"] = {"100": {"Investigation": {"filled": 5, "segments": 6}}}

    updates = [_make_msg(1, 100, "/tick Investigation", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert state["clocks"]["100"]["Investigation"]["filled"] == 6
    assert "COMPLETE" in _sent_messages[-1]["text"]


def test_untick():
    """/untick reverses a clock."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["clocks"] = {"100": {"Investigation": {"filled": 3, "segments": 6}}}

    updates = [_make_msg(1, 100, "/untick Investigation", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert state["clocks"]["100"]["Investigation"]["filled"] == 2


def test_delclock():
    """/delclock removes a clock."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["clocks"] = {"100": {"Investigation": {"filled": 3, "segments": 6}}}

    updates = [_make_msg(1, 100, "/delclock Investigation", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert "Investigation" not in state["clocks"]["100"]


def test_clocks_list():
    """/clocks shows all clocks."""
    state = {"clocks": {"100": {
        "Investigation": {"filled": 3, "segments": 6},
        "Ritual": {"filled": 4, "segments": 4},
    }}}
    result = checker._build_clocks("100", "TestCampaign", state)
    assert "Investigation" in result
    assert "Ritual" in result
    assert "◉" in result
    assert "✅" in result  # Ritual is complete


def test_clock_non_gm():
    """/clock from non-GM is ignored."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/clock Cheat 6", user_id=42, first_name="Player")]
    checker.process_updates(updates, config, state)

    assert len(state.get("clocks", {}).get("100", {})) == 0


# ------------------------------------------------------------------ #
#  HP helper tests
# ------------------------------------------------------------------ #
def test_hp_bar():
    """HP bar renders correctly."""
    import helpers
    result = helpers.hp_bar(30, 45, 10)
    assert "30/45" in result
    assert "█" in result
    assert "░" in result


def test_hp_bar_full():
    """Full HP bar is all filled."""
    import helpers
    result = helpers.hp_bar(100, 100, 10)
    assert "100/100" in result
    assert "░" not in result


def test_hp_bar_empty():
    """Empty HP bar is all empty."""
    import helpers
    result = helpers.hp_bar(0, 100, 10)
    assert "0/100" in result
    assert "█" not in result


def test_clock_display():
    """Clock display renders correctly."""
    import helpers
    result = helpers.clock_display(3, 6)
    assert "◉◉◉○○○" in result
    assert "3/6" in result


def test_clock_display_full():
    """Full clock is all filled."""
    import helpers
    result = helpers.clock_display(6, 6)
    assert "◉◉◉◉◉◉" in result
    assert "○" not in result


# ------------------------------------------------------------------ #
#  Vote tests
# ------------------------------------------------------------------ #
def test_vote_start():
    """/vote creates a vote with options."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/vote Where next? | North | South | Stay", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    vote = state.get("votes", {}).get("100")
    assert vote is not None
    assert vote["question"] == "Where next?"
    assert vote["options"] == ["North", "South", "Stay"]
    assert not vote["closed"]
    assert "🗳️" in _sent_messages[-1]["text"]


def test_vote_too_few_options():
    """/vote with only 1 option rejected."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/vote Bad vote | Only one", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert "100" not in state.get("votes", {})


def test_pick_vote():
    """/pick casts a vote."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["votes"] = {"100": {
        "question": "Left or right?",
        "options": ["Left", "Right"],
        "results": {"1": [], "2": []},
        "closed": False,
        "created_at": "2026-02-28T10:00:00+00:00",
    }}

    updates = [_make_msg(1, 100, "/pick 2", user_id=42, first_name="Alice")]
    checker.process_updates(updates, config, state)

    assert "Alice" in state["votes"]["100"]["results"]["2"]
    assert "✅" in _sent_messages[-1]["text"]


def test_pick_changes_vote():
    """/pick changes previous vote."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["votes"] = {"100": {
        "question": "A or B?",
        "options": ["A", "B"],
        "results": {"1": ["Alice"], "2": []},
        "closed": False,
        "created_at": "2026-02-28T10:00:00+00:00",
    }}

    updates = [_make_msg(1, 100, "/pick 2", user_id=42, first_name="Alice")]
    checker.process_updates(updates, config, state)

    assert "Alice" not in state["votes"]["100"]["results"]["1"]
    assert "Alice" in state["votes"]["100"]["results"]["2"]


def test_endvote():
    """/endvote closes and shows results."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["votes"] = {"100": {
        "question": "A or B?",
        "options": ["A", "B"],
        "results": {"1": ["Alice", "Bob"], "2": ["Charlie"]},
        "closed": False,
        "created_at": "2026-02-28T10:00:00+00:00",
    }}

    updates = [_make_msg(1, 100, "/endvote", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert state["votes"]["100"]["closed"]
    last = _sent_messages[-1]["text"]
    assert "Winner" in last or "Tied" in last
    assert "A" in last


def test_showvote():
    """/showvote displays current vote."""
    state = {"votes": {"100": {
        "question": "Go where?",
        "options": ["Left", "Right"],
        "results": {"1": ["Alice"], "2": []},
        "closed": False,
    }}}
    result = checker._build_vote("100", "TestCampaign", state)
    assert "Go where?" in result
    assert "Left" in result
    assert "Alice" in result


def test_vote_non_gm():
    """/vote from non-GM is ignored."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/vote Cheat? | Yes | No", user_id=42, first_name="Player")]
    checker.process_updates(updates, config, state)

    assert "100" not in state.get("votes", {})


# ------------------------------------------------------------------ #
#  Timer tests
# ------------------------------------------------------------------ #
def test_timer_set():
    """/timer sets a deadline."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/timer 24h Post your actions", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    timer = state.get("timers", {}).get("100")
    assert timer is not None
    assert timer["reason"] == "Post your actions"
    assert "⏳" in _sent_messages[-1]["text"]


def test_timer_bad_duration():
    """/timer with bad duration gives error."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/timer blah", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert "100" not in state.get("timers", {})
    assert "parse" in _sent_messages[-1]["text"].lower() or "Nh" in _sent_messages[-1]["text"]


def test_showtimer():
    """/showtimer displays timer."""
    from datetime import timezone
    state = {"timers": {"100": {
        "deadline": (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat(),
        "reason": "Act now!",
        "set_at": datetime.now(timezone.utc).isoformat(),
    }}}
    result = checker._build_timer("100", "TestCampaign", state)
    assert "remaining" in result
    assert "Act now!" in result


def test_canceltimer():
    """/canceltimer removes the timer."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["timers"] = {"100": {
        "deadline": (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat(),
        "reason": "test",
        "set_at": datetime.now(timezone.utc).isoformat(),
    }}

    updates = [_make_msg(1, 100, "/canceltimer", user_id=999, first_name="GM")]
    checker.process_updates(updates, config, state)

    assert "100" not in state.get("timers", {})


def test_timer_expiry_notification():
    """check_expired_timers posts notification."""
    _reset()
    config = _make_config()
    state = _make_state()
    state["timers"] = {"100": {
        "deadline": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "reason": "Time's up!",
        "set_at": datetime.now(timezone.utc).isoformat(),
    }}

    checker.check_expired_timers(config, state)

    expired_msgs = [m for m in _sent_messages if "expired" in m.get("text", "").lower()]
    assert len(expired_msgs) >= 1
    assert state["timers"]["100"].get("notified")


def test_timer_non_gm():
    """/timer from non-GM is ignored."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/timer 24h hack", user_id=42, first_name="Player")]
    checker.process_updates(updates, config, state)

    assert "100" not in state.get("timers", {})


# ------------------------------------------------------------------ #
#  Summary tests
# ------------------------------------------------------------------ #
def test_summary_basic():
    """/summary shows campaign state."""
    state = {
        "quests": {"100": [{"text": "Find the key", "status": "active", "created_at": ""}]},
        "npcs": {"100": [{"name": "Gorund", "desc": "Smith", "added_at": ""}]},
        "loot": {"100": [{"text": "Sword", "added_at": ""}]},
        "pins": {"100": [{"text": "Clue", "created_at": "", "author": ""}]},
        "conditions": {"100": [{"target": "Bob", "effect": "Stunned", "duration": "", "added_at": ""}]},
    }
    config = _make_config()
    result = checker._build_summary("100", "TestCampaign", state, config)
    assert "Find the key" in result
    assert "1 NPC" in result
    assert "1 loot" in result
    assert "1 pin" in result
    assert "Stunned" in result


def test_summary_empty():
    """/summary with nothing tracked."""
    state = {}
    config = _make_config()
    result = checker._build_summary("100", "TestCampaign", state, config)
    assert "Nothing special" in result


def test_summary_command():
    """/summary command sends result."""
    _reset()
    config = _make_config()
    state = _make_state()

    updates = [_make_msg(1, 100, "/summary", user_id=42, first_name="Alice")]
    checker.process_updates(updates, config, state)

    summary_msgs = [m for m in _sent_messages if "Summary" in m.get("text", "")]
    assert len(summary_msgs) >= 1


# ------------------------------------------------------------------ #
#  Timer duration parsing tests
# ------------------------------------------------------------------ #
def test_parse_timer_hours():
    """Parse '24h' duration."""
    now = datetime(2026, 2, 28, 12, 0, 0, tzinfo=timezone.utc)
    deadline, reason = helpers.parse_timer_duration("24h", now)
    assert deadline == datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert reason == ""


def test_parse_timer_minutes():
    """Parse '30m' duration."""
    now = datetime(2026, 2, 28, 12, 0, 0, tzinfo=timezone.utc)
    deadline, reason = helpers.parse_timer_duration("30m", now)
    assert deadline == datetime(2026, 2, 28, 12, 30, 0, tzinfo=timezone.utc)


def test_parse_timer_days():
    """Parse '2d' duration."""
    now = datetime(2026, 2, 28, 12, 0, 0, tzinfo=timezone.utc)
    deadline, reason = helpers.parse_timer_duration("2d", now)
    assert deadline == datetime(2026, 3, 2, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_timer_with_reason():
    """Parse '24h Post your actions'."""
    now = datetime(2026, 2, 28, 12, 0, 0, tzinfo=timezone.utc)
    deadline, reason = helpers.parse_timer_duration("24h Post your actions", now)
    assert deadline is not None
    assert reason == "Post your actions"


def test_parse_timer_invalid():
    """Invalid duration returns None."""
    now = datetime(2026, 2, 28, 12, 0, 0, tzinfo=timezone.utc)
    deadline, reason = helpers.parse_timer_duration("blah", now)
    assert deadline is None


# ------------------------------------------------------------------ #
#  Runner
# ------------------------------------------------------------------ #
def _run_all():
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
