# Discord AI Chatbot

A Discord bot powered by Claude (claude-sonnet-4-6) with an unhinged, evil, devious personality. Responds to @mentions in servers and to all messages in DMs. Keeps per-channel conversation memory (last 20 turns).

## Setup

### 1. Get a Discord bot token

1. Go to https://discord.com/developers/applications
2. Click **New Application**, give it a name
3. Go to **Bot** tab → **Reset Token** → copy the token
4. Under **Privileged Gateway Intents**, enable **MESSAGE CONTENT INTENT**
5. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `Send Messages`, `Read Message History`, `Use External Emojis`, `Attach Files`, `Embed Links`
6. Open the generated URL to invite the bot to your server

### 2. Get an Anthropic API key

Go to https://console.anthropic.com/ and create an API key.

### 3. (Optional) Get a Giphy API key for reaction GIFs

If set, the bot can post reaction GIFs of its own via the `send_gif` tool — Claude decides when a GIF fits the moment and the bot uploads it as an attachment. Without this key, the bot can't post GIFs.

1. Go to https://developers.giphy.com/dashboard/ and create an app (pick **API**, not SDK)
2. Copy the resulting API key
3. Set it as `GIPHY_API_KEY` (see below)

### 4. (Optional) Get a YouTube Data API key for video links

If set, the bot can search YouTube and post videos via the `send_video` tool — Claude calls it when someone says "play X", "show me that scene", "tutorial on Y", etc. Discord auto-embeds the URL as an inline player. Without this key, the video tool stays disabled.

Free quota: 10,000 units/day = ~100 video searches/day. After that, requests just fail (no surprise billing) and the bot falls back to text-only.

1. Go to https://console.cloud.google.com/ and create a project
2. APIs & Services → Library → enable **YouTube Data API v3**
3. APIs & Services → Credentials → Create Credentials → **API Key** (public data, no OAuth needed)
4. Edit the key → restrict it to **YouTube Data API v3** only (security)
5. Set it as `YOUTUBE_API_KEY` (see below)

### 5. Install and run

```powershell
cd C:\Users\Neko\Desktop\discord-bot
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:DISCORD_TOKEN = "your_discord_bot_token_here"
$env:ANTHROPIC_API_KEY = "your_anthropic_api_key_here"
$env:GIPHY_API_KEY = "your_giphy_api_key_here"   # optional — enables reaction GIFs
$env:YOUTUBE_API_KEY = "your_youtube_api_key_here"  # optional — enables video links

python bot.py
```

## Usage

- **In a server**: `@YourBot hello, what's up?`
- **In DMs**: just message it directly — no mention needed
- **Auto-reply channel**: run `/setup` in any channel to make the bot reply to every message there (no mention needed)
- **Images (in)**: attach an image and the bot will see it (vision support)
- **GIFs (out)**: with `GIPHY_API_KEY` set, the bot can post reaction GIFs back via the `send_gif` tool (Claude picks the moment and the search query)
- **Videos (out)**: with `YOUTUBE_API_KEY` set, the bot can search YouTube and post videos via the `send_video` tool — Discord auto-embeds the URL as an inline player
- **Custom emojis (in & out)**: the bot reads custom server emojis users send and uses them inline in its own replies (`:emoji_name:` syntax — auto-rewritten to the rendered form before sending). Up to 40 emojis per server are exposed to Claude; if your server has more, only the alphabetically-first 40 are listed
- **Server stickers (in & out)**: the bot can read user-sent stickers (notes them in conversation context) and post stickers itself via the `send_sticker` tool. Up to 25 stickers per server are exposed; max 3 per outgoing message (Discord's hard limit)

## Slash commands

| Command   | Description                                                     |
| --------- | --------------------------------------------------------------- |
| `/setup`  | Add the current channel to the auto-reply list (multiple channels per server supported, Manage Channels perm required) |
| `/unset`  | Stop auto-replying — `scope: here` (default, just this channel) or `scope: all` (every channel in the server) |
| `/reset`  | Wipe the bot's memory for the current conversation              |
| `/purge`  | Nuke memory; scope `here` (this convo) or `server` (every convo in the guild — Manage Channels required) |
| `/mood`   | Switch personality preset (`escalating` / `feral` / `villain` / `chill`) — scope `server` or `here` |
| `/rage`   | Show the current patience meter for this conversation (with bar + tier) |
| `/status` | Show model, mood, memory, scope, active conversation count      |

`/mood` accepts a `scope` arg:
- `server` (default) — applies to the whole guild (requires Manage Channels)
- `here` — applies only to the current channel/thread/DM (anyone can set)

Per-conversation overrides win over server defaults. All settings persist to `channels.json` across restarts.

### Moods

- **`escalating`** *(default)* — Starts polite and helpful. Patience drains every turn (+1) and faster on dumb messages (short, all-caps, repeats, low-effort openers like "yo"/"bruh", begging like "plsss"). Decays ~1 point per 5 minutes idle. At low patience you get a warm helper, at mid you get sarcasm, at high you get the unhinged cussing menace. The bot doesn't mention the meter to users — just calibrates tone.
- **`feral`** — Immediately unhinged, no warmup. The OG.
- **`villain`** — Theatrical Saturday-morning supervillain.
- **`chill`** — Friendly, mildly sarcastic, swears sparingly.

Use `/status` to see the current patience meter for a conversation. `/reset` and `/purge` both wipe it.

## Customization

Edit `bot.py`:
- `MODEL` — currently `claude-sonnet-4-6` (balanced); switch to `claude-haiku-4-5` for cheapest/fastest or `claude-opus-4-7` for max intelligence
- `SYSTEM_PROMPT` — change the bot's personality
- `MAX_TURNS` — how many turns of history to remember per channel (default 50)
- `MAX_TOKENS` — max length of each Claude response (default 2048)
