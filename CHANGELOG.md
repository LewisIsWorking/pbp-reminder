# Changelog

All notable changes to the PBP Reminder Bot are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/).

- **MAJOR** (x.0.0): Breaking config changes, workflow restructuring.
- **MINOR** (0.x.0): New commands, new features, new bot behaviours.
- **PATCH** (0.0.x): Bug fixes, test additions, refactors, documentation.

---

## [1.1.0] - 2026-02-26

### Summary
Player self-service update. Three new commands let players check their own stats,
inspect combat status, and discover features through daily tips. Plus a roadmap,
versioning pipeline, and 20 new tests.

### Added — New Commands
- **/mystats** (alias: **/me**): Players type `/mystats` in any PBP topic to see
  their personal stats: total posts, posting sessions, average gap between posts,
  weekly activity count, last post time, and current posting streak. Works for both
  players and GMs. No need to wait for roster day — check any time.
- **/whosturn**: Anyone can check combat status on demand. Shows: current round,
  whose phase it is (players/enemies), who has already acted (✅), and who the party
  is waiting on (⏳). During enemy phase, shows "Waiting for GM." Works outside the
  ping timer schedule so players can check without waiting for the automatic ping.

### Added — Daily Tips
- The bot now posts one random tip per day to a randomly chosen PBP chat topic.
  Each tip explains a bot feature (commands, combat tracking, POTW, streaks, etc).
  Tips rotate through all 12 entries before repeating, so every feature gets explained.
  This helps players who don't read GitHub or the issues topic discover what the bot
  can do. Tips are posted with HTML formatting for readability.

### Added — Posting Streaks
- The bot now tracks consecutive days with posts and displays the streak in `/mystats`.
  A "streak" means posting at least once per day with no gaps. Posts yesterday count
  as maintaining the streak. Streak resets if you miss a day. Shows 🔥 emoji for
  streaks of 2+ days.

### Added — Infrastructure
- **ROADMAP.md**: Full feature roadmap through v1.4.0+ with planned features
  (streaks leaderboard, weekly digest, campaign health scoring, dashboard improvements,
  GM tools, smart alerts, character awareness, AI summaries) and status tracking.
- **Changelog notifications**: When CHANGELOG.md is pushed, the `changelog-notify.yml`
  workflow posts the latest entry (formatted as Telegram HTML) to the Foundry & GitHub
  topic (https://t.me/Path_Wars/71537). Uses `post_changelog.py` which parses markdown,
  converts bold/italic/code/headers to HTML tags, and splits messages if they exceed
  Telegram's 4096 char limit.
- **VERSION file**: Semver-based version tracking. MAJOR = breaking config changes,
  MINOR = new features/commands, PATCH = fixes/tests/docs.

### Changed
- `telegram.py`: `send_message()` now accepts optional `parse_mode` parameter for
  HTML-formatted messages (used by daily tips).
- Help text updated with `/mystats`, `/me`, `/whosturn`, and daily tips.

### Tests
- 20 new tests: _build_mystats (4), _calc_streak (5), _build_whosturn (4),
  /whosturn command (1), /mystats command (2), daily tips (4).
- Total: 107 tests (34 helpers + 73 checker).

---

## [1.0.0] - 2026-02-26

### Summary
First versioned release. Consolidates all prior refactoring work (sessions 1–4)
plus today's new features into a stable, tested baseline.

### Added — New Features
- **/campaign command**: Type `/campaign` in any PBP topic to get a full scoreboard:
  campaign age, party size, weekly pace with trend arrows, complete roster with
  per-player stats (total posts, sessions, weekly count, average gap, last post),
  at-risk player warnings, and active combat state. This replaces the need to wait
  for scheduled roster/pace reports — players can check on demand.
- **/status command**: Quick health snapshot — party size, last post time, posts
  this week, at-risk players, combat state.
- **/help command**: Lists all bot features and GM commands in-chat.
- **Per-campaign feature toggles**: Add `"disabled_features": ["potw", "recruitment"]`
  to any campaign in config to turn off specific features per campaign. Valid toggles:
  alerts, warnings, roster, potw, pace, recruitment, combat, anniversary.
- **Config validation on startup**: Bot checks config structure before running —
  catches bad group_id, duplicate topic IDs, unknown feature names, malformed dates.
  Errors prevent the run; warnings are logged but continue.
- **Archive dashboard** (docs/index.html): Interactive web dashboard for
  weekly_archive.json. Line charts for posts per week, GM vs player splits, response
  gap trends, and a sortable campaign comparison table. Dark RPG-themed design.
  Works on GitHub Pages or locally.
- **Changelog notifications**: Bot posts release notes to the Foundry & GitHub
  topic automatically after each push.
- **Versioning**: Semver-based VERSION file and CHANGELOG.md.

### Added — Code Quality
- **87 tests** (32 helpers, 55 checker) covering: message parsing, combat state
  machine, boon selection/expiry, player warnings and removal, leaderboard stats,
  anniversary detection, recruitment checks, feature toggles, config validation,
  pace calculations, roster formatting, and all helper utilities.
- **CI test gate**: Tests run before the checker in GitHub Actions. If tests fail,
  the checker doesn't execute.
- **Extracted `_parse_message`**: Message validation and field extraction pulled out
  of `process_updates`, reducing the main loop from 111 to 75 lines.
- **Extracted `pace_split` helper**: Deduplicated GM/player weekly post split logic
  used by both `/campaign` and pace reports.
- **Shared timestamps**: All 11 per-run features now receive identical `now` and
  `maps` objects, eliminating 13 redundant `datetime.now()` calls per run.

### Removed
- `pbp_summary_feature.py`: Unused 206-line AI summary prototype. The `/campaign`
  command now fills this role without requiring an API key.

### Architecture (for reference — pre-v1.0.0 refactoring)
The codebase was restructured across 4 sessions from a single 1,200-line file into:
- `checker.py` (1,468 lines, 27 functions): All bot features and orchestration.
- `helpers.py` (418 lines, 28 functions): Pure utilities, constants, config loading.
- `telegram.py` (105 lines, 7 functions): Telegram Bot API wrapper.
- `state.py` (103 lines, 3 functions): Gist-backed state persistence.
- `test_helpers.py` (314 lines, 32 tests): Helper function test suite.
- `test_checker.py` (1,069 lines, 55 tests): Checker integration and unit tests.
- `docs/index.html` (411 lines): Archive dashboard.

Every function has docstrings and return type hints. Max nesting: 4 levels.
All settings are configurable via `config.json` with sensible defaults.
