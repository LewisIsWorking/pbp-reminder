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
    checker._handle_combat_message("I attack!", "42", {"999"}, "100", "Test", "now", -100, 100, state)
    assert "42" in state["combat"]["100"]["players_acted"]


def test_handle_combat_message_gm_not_tracked():
    _reset()
    state = _make_state()
    state["combat"]["100"] = {
        "active": True, "round": 1, "current_phase": "players",
        "players_acted": [], "last_ping_at": None,
        "campaign_name": "Test", "phase_started_at": "now",
    }
    checker._handle_combat_message("narrative text", "999", {"999"}, "100", "Test", "now", -100, 100, state)
    assert "999" not in state["combat"]["100"]["players_acted"]


def test_handle_combat_endcombat():
    _reset()
    state = _make_state()
    state["combat"]["100"] = {
        "active": True, "round": 1, "current_phase": "players",
        "players_acted": [], "last_ping_at": None,
        "campaign_name": "Test", "phase_started_at": "now",
    }
    checker._handle_combat_message("/endcombat", "999", {"999"}, "100", "Test", "now", -100, 100, state)
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

    stats, global_players = checker._gather_leaderboard_stats(config, state, now)
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

    stats, global_players = checker._gather_leaderboard_stats(config, state, now)
    assert len(stats) == 1  # Campaign exists but with no data
    assert stats[0]["total_7d"] == 0
    assert len(global_players) == 0


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
