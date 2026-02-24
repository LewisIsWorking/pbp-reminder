"""Gist-based state persistence."""

import json
import requests

GIST_TOKEN = ""
GIST_API = ""
STATE_FILENAME = "pbp_state.json"

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
    "pending_potw_boons": {},
    "last_recruitment_check": {},
    "last_leaderboard": None,
}


def init(gist_token: str, gist_id: str):
    """Set gist credentials."""
    global GIST_TOKEN, GIST_API
    GIST_TOKEN = gist_token
    GIST_API = f"https://api.github.com/gists/{gist_id}"


def load() -> dict:
    """Load bot state from GitHub Gist, or return defaults if unavailable."""
    if not GIST_API or not GIST_TOKEN:
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


def save(state: dict):
    """Persist bot state to GitHub Gist."""
    if not GIST_API or not GIST_TOKEN:
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
