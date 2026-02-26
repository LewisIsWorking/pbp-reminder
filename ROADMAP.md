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

### 📋 GitHub Pages dashboard improvements
- Player-level drill-down (click a campaign to see individual stats)
- Historical streak tracking
- Filterable date range
- Mobile-responsive layout

### ✅ Personal history
`/myhistory` shows a player's posting pattern over time: weekly post
counts for the last 8 weeks as a text sparkline chart.

---

## v1.5.0 — GM Tools (partially complete)

### ✅ `/pause` and `/resume`
Temporarily disable inactivity tracking for planned breaks, holidays,
or between arcs. Pause reason displayed in `/status` and `/campaign`.

### 📋 `/kick <player>`
Manually remove a player from tracking without waiting for the 4-week
auto-removal.

### 📋 `/addplayer <@mention>`
Manually register a player who hasn't posted yet so they appear in
the roster and get tracked.

---

## Future Ideas (unscheduled)

### 💡 Smart alerts
- Detect when a campaign's pace drops significantly week-over-week and
  send a gentle nudge to the GM (privately or in chat)
- "Conversation dying" warning when no posts for 48h+ across ALL users
  (including GM), distinct from the per-player inactivity alerts

### 💡 Character awareness
- Optional config field per campaign: character names mapped to user IDs
- Tips and roster could reference character names alongside player names
- "/party" command showing the in-fiction party composition

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
