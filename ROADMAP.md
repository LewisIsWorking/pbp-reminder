# Roadmap

Planned features and improvements for the PBP Reminder Bot.
Status: ✅ Done | 🔧 In Progress | 📋 Planned | 💡 Idea

---

## v1.1.0 — Player Self-Service & Awareness

### ✅ `/mystats` command
Players type `/mystats` in any PBP topic to see their own stats:
total posts, posting sessions, average gap, last post, current streak,
and weekly activity. No waiting for roster day.

### ✅ `/whosturn` command
Anyone can check combat status on demand: current round, whose phase it is,
who has acted, who hasn't. Works outside the ping timer schedule.

### ✅ Daily tips
The bot posts a random tip about one of its features once per day to a
randomly chosen PBP chat topic. Helps players discover commands without
needing to read GitHub or the issues topic. Tips rotate through all
features so each one gets explained eventually.

---

## v1.2.0 — Streaks & Celebrations

### ✅ Posting streaks
Track consecutive days each player posts. Display in `/mystats` and roster.
Milestone celebrations at 7, 14, 30, 60, 90 days ("🔥 Alice is on a 30-day
streak!"). Could feed into POTW weighting or be a standalone shout-out.

### ✅ Streak leaderboard
Add a "longest active streak" section to the weekly leaderboard. Show top 5
across all campaigns. Encourage consistent engagement over burst posting.

---

## v1.3.0 — Cross-Campaign Intelligence

### ✅ Weekly digest
A compact newsletter posted to the leaderboard topic once per week.
One-line summary per campaign: name, post count, trend, top contributor,
notable events (new player joined, combat started, anniversary). Designed
to be scannable in 10 seconds.

### ✅ Campaign health scoring
Assign each campaign a simple health score (traffic light)
based on weekly post volume.
Show in digest. Helps the GM spot campaigns that need
attention without reading every stat.

---

## v1.4.0 — Archive & History

### ✅ PBP transcript archive
Every message in every PBP topic is logged to persistent markdown files
in the repo. Monthly files per campaign at `data/pbp_logs/`. Media is
tagged with type markers. Auto-generated README index. A complete
disaster-recovery backup of every campaign's story.

### ✅ GitHub Pages dashboard v2
- Player-level drill-down (click a campaign to see individual stats)
- Summary cards, week filter, sortable columns
- Health indicator dots (green/yellow/orange/red)
- Trend arrows (week-over-week change)
- Mobile-responsive layout

### ✅ Personal history
`/myhistory` shows a player's posting pattern over time: weekly post
counts for the last 8 weeks as a text sparkline chart.

---

## v1.5.0 — History Import & Per-Campaign GMs

### ✅ Historical transcript backfill
`scripts/import_history.py` imports past PBP messages from Telegram Desktop
JSON exports into `data/pbp_logs/`. Idempotent, supports `--dry-run`.

### ✅ Per-campaign GM overrides
Optional `gm_user_ids` on individual topic_pairs replaces the global list
for that campaign. Allows different GMs per campaign (e.g. Theria).

### ✅ `/pause` and `/resume`
Temporarily disable inactivity tracking for planned breaks, holidays,
or between arcs. Pause reason displayed in `/status` and `/campaign`.

---

## v1.6.0 — GM Roster Management

### ✅ `/kick @player`
Manually remove a player from tracking without waiting for the 4-week
auto-removal.

### ✅ `/addplayer @username Name`
Manually register a player who hasn't posted yet so they appear in
the roster and get tracked.

---

## v1.9.0 — Character Awareness

### ✅ Character name mapping
Optional `characters` field per campaign maps user IDs to character names.
Names appear in rosters, `/mystats`, transcripts, and the new `/party` command.

### ✅ `/party` command
Shows the in-fiction party composition: character names, players, activity.

---

## v1.8.0 — Message Milestones

### ✅ Campaign milestones (every 500 messages)
Celebrates in the campaign's chat topic with escalating icons.

### ✅ Global milestones (every 5,000 messages)
Celebrates across all campaigns in the leaderboard topic.

---

## v1.7.0 — Player Catch-Up

### ✅ `/catchup`
Shows what happened since you last posted: message counts by person,
time since last post, and combat state. Essential for returning PBP players.

---

## v2.0.0 — Dashboard v2 & Smart Alerts

### ✅ Dashboard v2
Summary cards, week filter, sortable columns, player drill-down,
health indicators, trend arrows, mobile-responsive. Powered by
`player_breakdown` data in the weekly archive.

### ✅ Smart alerts
Pace drop detection (>40% week-over-week) and conversation dying
warning (48h+ total silence). Both gated behind `smart_alerts` feature
flag (enabled by default). Use `/pause` to silence during planned breaks.

---

## v2.1.0 — Scene Markers & GM Notes

### ✅ Scene markers
`/scene <n>` marks a narrative scene boundary in the campaign's
transcript file. Creates a styled divider with timestamp. Scene name
displayed in `/status` and `/campaign` output.

### ✅ GM notes
`/note <text>` adds persistent notes per campaign (max 20). View
with `/notes`, delete with `/delnote <N>`. Latest notes shown in
`/campaign` output. All players can view, only GMs can add/remove.

---

## v2.2.0 — Activity Insights

### ✅ Activity tracking
Every message records hour-of-day and day-of-week counters.
Permanent lightweight data (24 + 7 buckets per user per campaign).

### ✅ `/activity` command
Campaign-level posting patterns: busiest days, peak time blocks,
peak hour, and top posters. Available to everyone.

### ✅ `/profile` command
Cross-campaign player lookup. Shows every campaign a player is in
with post counts, character names, last activity, and streaks.
Matches by username, first name, or full name.

---

## Future Ideas (unscheduled)

### 💡 AI summaries (revisited)
- Optional AI-generated "story so far" recap using Anthropic API
- Posts to chat topic on a configurable schedule
- Requires ANTHROPIC_API_KEY secret (see removed pbp_summary_feature.py
  for prior implementation)

### 💡 Timezone-aware scheduling
- Allow per-campaign timezone config
- Schedule posts for reasonable local times instead of UTC cron
- Display "last post" times in local timezone

---

## Contributing

Ideas and feedback welcome in the
[Foundry & GitHub topic](https://t.me/Path_Wars/71537) or via GitHub issues.
