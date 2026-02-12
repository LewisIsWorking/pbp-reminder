# Path Wars PBP Inactivity Reminder

A GitHub Actions bot that monitors your Telegram PBP topics and sends
reminders to OOC chat topics when nobody has posted for 4+ hours.

**No server needed.** Runs free on GitHub Actions.

## Example alerts

After 4 hours:
```
No new posts in Grand Explorers PBP for 4h.
Last post was from Tyler Link.
```

After a day and a half:
```
No new posts in Grand Explorers PBP for 1d 14h.
Last post was from Cannon McMahon.
```

Alerts repeat every 4 hours until someone posts.

## How it works

1. GitHub Actions runs the script every hour
2. Script fetches new messages from Telegram via Bot API
3. Tracks the last message time per PBP topic
4. If a topic has been quiet for 4+ hours, sends a reminder to the CHAT topic
5. State is stored in a GitHub Gist (persists between runs)

## Setup (one-time, ~15 minutes)

### Step 1: Create a Telegram bot

1. Message **@BotFather** on Telegram
2. Send `/newbot`, follow the prompts, copy the **bot token**
3. Send `/setprivacy`, select your bot, set to **Disable**
   (so it can read all messages, not just commands)
4. Add the bot to your **Path Wars Main Chat** supergroup
5. Make it an **admin** (needs: Read Messages, Send Messages)

### Step 2: Find your topic thread IDs

Send this in your browser (replace YOUR_TOKEN):
```
https://api.telegram.org/botYOUR_TOKEN/getUpdates
```

Then send one message in each PBP topic and one in each CHAT topic.
Refresh the URL. You'll see JSON with entries like:
```json
{
  "message": {
    "chat": {
      "id": -1001234567890
    },
    "message_thread_id": 12345,
    "text": "test"
  }
}
```

- `chat.id` is your **group_id** (same for all topics)
- `message_thread_id` is the **topic ID** (different per topic)

Note down the thread ID for each PBP topic and each CHAT topic.

### Step 3: Create a GitHub Gist for state storage

1. Go to https://gist.github.com
2. Create a new gist with filename `pbp_state.json` and content `{}`
3. Save it. Copy the **Gist ID** from the URL:
   `https://gist.github.com/yourname/THIS_IS_THE_GIST_ID`

### Step 4: Create a GitHub Personal Access Token

1. Go to https://github.com/settings/tokens
2. Generate a new token (classic) with the **gist** scope only
3. Copy the token

### Step 5: Create the repo

1. Create a new GitHub repo (can be private, Actions are free)
2. Upload all the files from this folder keeping the structure:
   ```
   .github/workflows/pbp-reminder.yml
   scripts/checker.py
   config.json
   README.md
   ```

### Step 6: Add secrets

In your repo, go to **Settings > Secrets and variables > Actions** and add:

| Secret Name          | Value                          |
|----------------------|--------------------------------|
| `TELEGRAM_BOT_TOKEN` | Your bot token from BotFather  |
| `GIST_TOKEN`         | Your GitHub PAT with gist scope|
| `GIST_ID`            | The Gist ID from step 3        |

### Step 7: Fill in config.json

Edit `config.json` with your actual group ID and topic thread IDs:

```json
{
    "group_id": -1001234567890,
    "alert_after_hours": 4,
    "topic_pairs": [
        {
            "name": "Grand Explorers",
            "pbp_topic_id": 11111,
            "chat_topic_id": 22222
        }
    ]
}
```

### Step 8: Test it

Go to **Actions** tab in your repo, find the workflow, click **Run workflow**.
Check the logs to see if it connects and processes messages.

## Configuration

| Setting            | Default | Description                                    |
|--------------------|---------|------------------------------------------------|
| `alert_after_hours`| 4       | Hours of silence before first alert            |
| Cron schedule      | hourly  | Edit `.github/workflows/pbp-reminder.yml`      |

Alerts won't repeat more often than every `alert_after_hours` per topic.

## Troubleshooting

**Bot not seeing messages:**
- Make sure privacy mode is disabled via @BotFather (`/setprivacy` > Disable)
- Make sure the bot is an admin in the group

**No updates showing:**
- Send a message in a monitored topic after the bot is added
- Check the getUpdates URL manually first

**Wrong topic IDs:**
- Topic IDs are the `message_thread_id` field, not the message ID
- Each topic in a supergroup has a unique thread ID

**GitHub Actions not running:**
- Make sure the workflow file is in `.github/workflows/`
- Check the Actions tab for errors
- Free tier allows 2000 minutes/month, this uses ~30 min/month
