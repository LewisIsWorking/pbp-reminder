# PBP Bot Enhancement Plan

## 1. Config Validation on Startup
**Why:** Typos in topic IDs silently break features. A wrong `chat_topic_id` means rosters never post, with no error logged.

**Implementation:**
- Add `validate_config(config)` to helpers.py
- Check: `group_id` is negative int, `gm_user_ids` non-empty, `topic_pairs` non-empty
- Check: every pair has `name`, `chat_topic_id`, `pbp_topic_ids` (non-empty list)
- Check: no duplicate topic IDs across campaigns
- Check: `leaderboard_topic_id` doesn't collide with any campaign topic
- Check: settings values are correct types (ints positive, lists non-empty)
- Call from main() before any processing; print warnings, exit on fatal errors
- **Status: DONE**

## 2. /help Command in Telegram
**Why:** Players and GMs have no way to discover what the bot does or what commands exist.

**Implementation:**
- Detect `/help` or `/pbphelp` in process_updates
- Reply in the same topic with a summary of bot features and GM commands
- Respond to `/help` from anyone, `/round` and `/endcombat` from GMs only (already the case)
- Keep message concise: feature list + command reference + intervals
- **Status: DONE**

## 3. Per-Campaign Feature Toggles
**Why:** Some campaigns may not want recruitment notices or POTW (e.g. Hopeful End-Times with 1 player).

**Implementation:**
- Add optional `disabled_features` list per topic_pair in config
- Valid values: `"roster"`, `"potw"`, `"pace"`, `"recruitment"`, `"combat"`, `"anniversary"`
- Add `helpers.feature_enabled(config, pid, feature_name) -> bool`
- Check at the top of each per-campaign loop iteration
- Document in README config reference
- **Status: DONE**

## 4. Tests for checker.py
**Why:** Only helpers.py is tested (31 tests). Core logic in checker.py has zero coverage.

**Testable pure functions (no Telegram calls):**
- `_format_boon_result` - HTML formatting
- `_roster_user_stats` - stat computation from timestamps
- `_roster_block` - string formatting
- `_gather_potw_candidates` - candidate filtering
- `_format_leaderboard` - leaderboard message formatting
- `cleanup_timestamps` - timestamp pruning
- `validate_config` (once built)
- `feature_enabled` (once built)

**Integration tests (mock tg.send_message):**
- `process_updates` - offset tracking, player state updates
- `check_and_alert` - alert timing logic
- `check_player_activity` - warning escalation

**Approach:** Simple mock for tg module, real state dicts, test pure functions first.
- **Status: DONE**

## 5. Archive Viewer (HTML Dashboard)
**Why:** weekly_archive.json has great data but is unreadable raw. A dashboard makes trends visible.

**Implementation:**
- Static HTML file with embedded JS (no build step, no server)
- Reads weekly_archive.json via fetch (works on GitHub Pages or local file)
- Charts: posts/week per campaign (line), GM vs player split (stacked bar), avg gap trend
- Table: sortable campaign comparison for latest week
- Use Chart.js from CDN (lightweight, no build)
- Place in `docs/index.html` for GitHub Pages compatibility
- **Status: DONE**

---

## Execution Order
1. Config validation (foundation - catches bugs early, tests need it)
2. Feature toggles (small, needed before tests can cover the skip logic)  
3. /help command (small, self-contained)
4. Tests for checker.py (covers everything built so far)
5. Archive viewer (standalone, no bot changes)
