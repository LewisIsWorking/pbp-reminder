#!/usr/bin/env python3
"""Tests for import_history.py"""

import json
import shutil
import tempfile
from pathlib import Path

import import_history


def _make_export(messages):
    """Create a minimal Telegram export dict."""
    return {"messages": messages}


def _make_msg(msg_id, thread_id, text="Hello", from_name="Alice",
              from_id="user42", date="2025-06-15T14:30:05", **extra):
    msg = {
        "id": msg_id,
        "type": "message",
        "message_thread_id": thread_id,
        "text": text,
        "from": from_name,
        "from_id": from_id,
        "date": date,
    }
    msg.update(extra)
    return msg


def test_extract_text_string():
    assert import_history.extract_text({"text": "hello"}) == "hello"


def test_extract_text_entity_list():
    msg = {"text": [
        "plain ",
        {"type": "bold", "text": "bold"},
        " more",
    ]}
    assert import_history.extract_text(msg) == "plain bold more"


def test_extract_text_empty():
    assert import_history.extract_text({}) == ""


def test_detect_media_photo():
    assert import_history.detect_media({"photo": "file.jpg"}) == "image"


def test_detect_media_sticker():
    assert import_history.detect_media({"sticker_emoji": "😂"}) == "sticker:😂"


def test_detect_media_gif():
    assert import_history.detect_media({"media_type": "animation"}) == "gif"


def test_detect_media_video():
    assert import_history.detect_media({"media_type": "video_file"}) == "video"


def test_detect_media_voice():
    assert import_history.detect_media({"media_type": "voice_message"}) == "voice message"


def test_detect_media_none():
    assert import_history.detect_media({"text": "just text"}) is None


def test_format_entry_player():
    msg = {
        "from": "Alice",
        "from_id": "user42",
        "date": "2025-06-15T14:30:05",
        "text": "I attack!",
    }
    result = import_history.format_entry(msg, is_gm=False)
    assert "**Alice**" in result
    assert "[GM]" not in result
    assert "I attack!" in result
    assert "2025-06-15 14:30:05" in result


def test_format_entry_gm():
    msg = {
        "from": "Lewis",
        "from_id": "user999",
        "date": "2025-06-15T14:30:05",
        "text": "The goblin attacks.",
    }
    result = import_history.format_entry(msg, is_gm=True)
    assert "[GM]" in result


def test_format_entry_media():
    msg = {
        "from": "Alice",
        "from_id": "user42",
        "date": "2025-06-15T14:30:05",
        "text": "battle map",
        "photo": "photo.jpg",
    }
    result = import_history.format_entry(msg, is_gm=False)
    assert "[image]" in result
    assert "battle map" in result


def test_import_messages_dry_run():
    """Test dry run mode parses messages without writing files."""
    tmp = tempfile.mkdtemp()
    export_path = Path(tmp) / "export.json"
    old_logs = import_history.LOGS_DIR
    old_config = import_history.CONFIG_PATH

    try:
        # Create test config
        config = {
            "gm_user_ids": [999],
            "topic_pairs": [
                {"name": "TestCampaign", "chat_topic_id": 10, "pbp_topic_ids": [100]},
            ],
        }
        config_path = Path(tmp) / "config.json"
        config_path.write_text(json.dumps(config))
        import_history.CONFIG_PATH = config_path
        import_history.LOGS_DIR = Path(tmp) / "logs"

        export = _make_export([
            _make_msg(1, 100, "First post"),
            _make_msg(2, 100, "Second post"),
            _make_msg(3, 999, "Wrong topic"),  # Not a PBP topic
        ])
        export_path.write_text(json.dumps(export))

        results = import_history.import_messages(str(export_path), dry_run=True)
        assert results.get("TestCampaign") == 2
        assert not (Path(tmp) / "logs").exists()  # No files written
    finally:
        import_history.LOGS_DIR = old_logs
        import_history.CONFIG_PATH = old_config
        shutil.rmtree(tmp)


def test_import_messages_writes_files():
    """Test actual import writes correct transcript files."""
    tmp = tempfile.mkdtemp()
    old_logs = import_history.LOGS_DIR
    old_config = import_history.CONFIG_PATH

    try:
        config = {
            "gm_user_ids": [999],
            "topic_pairs": [
                {"name": "TestCampaign", "chat_topic_id": 10, "pbp_topic_ids": [100]},
            ],
        }
        config_path = Path(tmp) / "config.json"
        config_path.write_text(json.dumps(config))
        import_history.CONFIG_PATH = config_path
        import_history.LOGS_DIR = Path(tmp) / "logs"

        export = _make_export([
            _make_msg(1, 100, "First post", date="2025-06-15T10:00:00"),
            _make_msg(2, 100, "GM narrates", from_name="Lewis", from_id="user999",
                      date="2025-06-15T10:05:00"),
            _make_msg(3, 100, "July post", date="2025-07-01T12:00:00"),
        ])
        export_path = Path(tmp) / "export.json"
        export_path.write_text(json.dumps(export))

        results = import_history.import_messages(str(export_path))
        assert results["TestCampaign"] == 3

        # Check files
        campaign_dir = Path(tmp) / "logs" / "TestCampaign"
        assert (campaign_dir / "2025-06.md").exists()
        assert (campaign_dir / "2025-07.md").exists()

        june = (campaign_dir / "2025-06.md").read_text()
        assert "First post" in june
        assert "[GM]" in june  # GM tagged
        assert "TestCampaign — 2025-06" in june

        july = (campaign_dir / "2025-07.md").read_text()
        assert "July post" in july

        # Verify idempotency — second run imports nothing
        results2 = import_history.import_messages(str(export_path))
        assert results2["TestCampaign"] == 0

    finally:
        import_history.LOGS_DIR = old_logs
        import_history.CONFIG_PATH = old_config
        shutil.rmtree(tmp)


def test_import_skips_service_messages():
    """Service messages (joins, pins) are excluded."""
    tmp = tempfile.mkdtemp()
    old_logs = import_history.LOGS_DIR
    old_config = import_history.CONFIG_PATH

    try:
        config = {
            "gm_user_ids": [],
            "topic_pairs": [
                {"name": "TestCampaign", "chat_topic_id": 10, "pbp_topic_ids": [100]},
            ],
        }
        config_path = Path(tmp) / "config.json"
        config_path.write_text(json.dumps(config))
        import_history.CONFIG_PATH = config_path
        import_history.LOGS_DIR = Path(tmp) / "logs"

        export = _make_export([
            _make_msg(1, 100, "Real post"),
            {**_make_msg(2, 100, ""), "action": "pin_message", "type": "message"},
        ])
        export_path = Path(tmp) / "export.json"
        export_path.write_text(json.dumps(export))

        results = import_history.import_messages(str(export_path))
        assert results["TestCampaign"] == 1
    finally:
        import_history.LOGS_DIR = old_logs
        import_history.CONFIG_PATH = old_config
        shutil.rmtree(tmp)


def test_build_thread_map():
    config = {
        "topic_pairs": [
            {"name": "A", "chat_topic_id": 10, "pbp_topic_ids": [100, 101]},
            {"name": "B", "chat_topic_id": 20, "pbp_topic_ids": [200]},
        ],
    }
    m = import_history.build_thread_map(config)
    assert m[100] == "A"
    assert m[101] == "A"
    assert m[200] == "B"
    assert 10 not in m  # Chat topics not included


# ------------------------------------------------------------------ #
#  Runner
# ------------------------------------------------------------------ #
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"FAIL {test.__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed out of {passed + failed}")
