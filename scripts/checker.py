"""
PBP Inactivity Checker for GitHub Actions

Runs hourly via cron. Processes Telegram updates to track last message
times in PBP topics. Sends alerts to OOC chat topics when a PBP topic
has been inactive for the configured threshold.

State is persisted between runs using a GitHub Gist.
"""

import os
import sys
import json
import requests
from datetime import datetime, timezone
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


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ------------------------------------------------------------------ #
#  Gist state storage
# ------------------------------------------------------------------ #
def load_state_from_gist() -> dict:
    """Load state from GitHub Gist."""
    if not GIST_ID or not GIST_TOKEN:
        print("Warning: No GIST_ID or GIST_TOKEN set, starting with empty state")
        return {"offset": 0, "topics": {}, "last_alerts": {}}

    resp = requests.get(
        GIST_API,
        headers={
            "Authorization": f"token {GIST_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        },
    )

    if resp.status_code != 200:
        print(f"Warning: Could not load gist (HTTP {resp.status_code}), starting fresh")
        return {"offset": 0, "topics": {}, "last_alerts": {}}

    gist_data = resp.json()
    files = gist_data.get("files", {})

    if STATE_FILENAME in files:
        content = files[STATE_FILENAME]["content"]
        return json.loads(content)

    return {"offset": 0, "topics": {}, "last_alerts": {}}


def save_state_to_gist(state: dict):
    """Save state to GitHub Gist."""
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
    """Fetch new updates from Telegram."""
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
    """Send a message to a specific topic thread."""
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
#  Main logic
# ------------------------------------------------------------------ #
def process_updates(updates: list, config: dict, state: dict) -> int:
    """Process updates and track last message times. Returns new offset."""
    group_id = config["group_id"]
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

        if thread_id_str in pbp_topic_ids:
            # Ignore bot's own messages
            from_user = msg.get("from", {})
            if from_user.get("is_bot", False):
                continue

            user_name = from_user.get("first_name", "Someone")
            campaign_name = pbp_topic_ids[thread_id_str]

            state["topics"][thread_id_str] = {
                "last_message_time": datetime.now(timezone.utc).isoformat(),
                "last_user": user_name,
                "campaign_name": campaign_name,
            }

            print(f"Tracked message in {campaign_name} from {user_name}")

    return new_offset


def check_and_alert(config: dict, state: dict):
    """Check for inactive topics and send alerts."""
    group_id = config["group_id"]
    alert_hours = config.get("alert_after_hours", 4)
    now = datetime.now(timezone.utc)

    if "last_alerts" not in state:
        state["last_alerts"] = {}

    for pair in config["topic_pairs"]:
        pbp_id_str = str(pair["pbp_topic_id"])
        chat_topic_id = pair["chat_topic_id"]
        name = pair["name"]

        if pbp_id_str not in state.get("topics", {}):
            print(f"No messages tracked yet for {name}, skipping")
            continue

        topic_state = state["topics"][pbp_id_str]
        last_time = datetime.fromisoformat(topic_state["last_message_time"])
        elapsed = now - last_time
        elapsed_hours = elapsed.total_seconds() / 3600

        if elapsed_hours < alert_hours:
            # Not inactive long enough
            continue

        # Check if we already alerted recently (within alert_hours)
        last_alert_str = state["last_alerts"].get(pbp_id_str)
        if last_alert_str:
            last_alert = datetime.fromisoformat(last_alert_str)
            since_last_alert = (now - last_alert).total_seconds() / 3600
            if since_last_alert < alert_hours:
                print(f"{name}: Already alerted {since_last_alert:.1f}h ago, skipping")
                continue

        # Build the message
        hours_int = int(elapsed_hours)
        days = hours_int // 24
        remaining_hours = hours_int % 24
        last_user = topic_state.get("last_user", "someone")

        if days > 0:
            time_str = f"{days}d {remaining_hours}h"
        else:
            time_str = f"{hours_int}h"

        message = (
            f"No new posts in {name} PBP for {time_str}.\n"
            f"Last post was from {last_user}."
        )

        print(f"Sending alert for {name}: {time_str} inactive")

        if send_message(group_id, chat_topic_id, message):
            state["last_alerts"][pbp_id_str] = now.isoformat()
            print(f"Alert sent for {name}")
        else:
            print(f"Failed to send alert for {name}")


def main():
    if not TELEGRAM_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)

    config = load_config()
    state = load_state_from_gist()

    print(f"Loaded state. Current offset: {state.get('offset', 0)}")
    print(f"Tracking {len(state.get('topics', {}))} topics")

    # Fetch and process new messages
    offset = state.get("offset", 0)
    updates = get_updates(offset)
    print(f"Received {len(updates)} new updates")

    if updates:
        new_offset = process_updates(updates, config, state)
        state["offset"] = new_offset

    # Check for inactivity and send alerts
    check_and_alert(config, state)

    # Save state
    save_state_to_gist(state)

    print("Done")


if __name__ == "__main__":
    main()
