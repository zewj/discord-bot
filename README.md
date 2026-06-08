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
   - Bot Permissions: `Send Messages`, `Read Message History`, `Use External Emojis`, `Attach Files`, `Embed Links`, `Connect`, `Speak` (last two for music)
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

### 5. (Optional) Install `ffmpeg` for music playback

Required by the `/play` voice commands. Without it, the music commands log a clear "disabled" message at startup and refuse to run; everything else still works.

- **Linux/Pterodactyl**: `apt install ffmpeg` (or whatever your egg's package manager uses). Verify with `ffmpeg -version`.
- **Windows**: download from https://ffmpeg.org/download.html and add to PATH.
- **macOS**: `brew install ffmpeg`.

The Python side (`yt-dlp`) is installed automatically via `requirements.txt`.

### 6. (Optional) Spotify credentials for Spotify-link parsing

Without these, `/play` still works for YouTube, SoundCloud, and Apple Music URLs — but pasting a Spotify link returns "couldn't resolve". With them, Spotify URLs are converted to a `"Title Artist"` YouTube search and played from there (Spotify's API doesn't expose audio streams, so this is the only legal route).

1. Go to https://developer.spotify.com/dashboard, log in, **Create app**
2. Any name/description; redirect URI doesn't matter — leave it as `http://localhost`
3. Open the app → **Settings** → copy **Client ID** and **Client secret**
4. Set them as `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`

Apple Music link parsing needs no credentials — uses the free iTunes Search API.

### 7. Install and run

```powershell
cd C:\Users\Neko\Desktop\discord-bot
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:DISCORD_TOKEN = "your_discord_bot_token_here"
$env:ANTHROPIC_API_KEY = "your_anthropic_api_key_here"
$env:GIPHY_API_KEY = "your_giphy_api_key_here"   # optional — enables reaction GIFs
$env:YOUTUBE_API_KEY = "your_youtube_api_key_here"  # optional — enables video links
$env:SPOTIFY_CLIENT_ID = "your_spotify_client_id"          # optional — Spotify link parsing
$env:SPOTIFY_CLIENT_SECRET = "your_spotify_client_secret"  # optional — Spotify link parsing

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
- **Music playback**: if `ffmpeg` is installed on the host, the bot joins your voice channel and streams audio. Join a voice channel, then run `/play <URL or search>`. Sources:
  - **YouTube** — URL or plain search text (default)
  - **SoundCloud** — paste a track URL (handled natively by `yt-dlp`)
  - **Spotify** — paste a track URL/URI (`open.spotify.com/track/…` or `spotify:track:…`); requires `SPOTIFY_CLIENT_ID` + `SPOTIFY_CLIENT_SECRET`. Spotify doesn't stream audio via its API, so the bot reads track metadata and plays the equivalent from YouTube
  - **Apple Music** — paste a single-track URL (`music.apple.com/…?i=…`); no credentials needed (uses iTunes Search API)
  - **Playlists & albums** — paste a YouTube playlist, SoundCloud set, Spotify playlist/album, or Apple Music album URL to queue all of it at once (capped at 50 tracks). Playlist tracks resolve lazily — each one's stream is fetched just before it plays, so big queues start instantly and stream URLs never go stale. (Apple Music *curated playlists* aren't supported — not exposed by the iTunes API — but albums are.)
  - **Now-playing embed** has a **live progress bar** that updates as the track plays, plus ⏮ ⏯ ⏭ ⏹ / 🔁 🔀 buttons
  - **DJ role** (optional): run `/dj role:@DJ` to lock control actions to that role. Anyone with Manage Server bypasses it. `/play`, `/queue`, and `/nowplaying` stay open to everyone. `/djoff` removes the lock. With no DJ role set, controls are open to all (default)
  - **Alone & vote rules** (always on, layered on top of the DJ system): if you're the **only listener** in the voice channel with the bot, every control works instantly — no DJ role and no vote required. With other people in the channel, `/skip` and `/leave` need a **strict majority vote** from the listeners (DJ-role holders and Manage-Server staff still bypass the vote). Votes auto-prune when listeners leave, so a quorum can't get stuck
  - Auto-disconnects after 5 minutes idle

## Slash commands

| Command   | Description                                                     |
| --------- | --------------------------------------------------------------- |
| `/setup`  | Add the current channel to the auto-reply list (multiple channels per server supported, Manage Channels perm required) |
| `/unset`  | Stop auto-replying — `scope: here` (default, just this channel) or `scope: all` (every channel in the server) |
| `/reset`  | Wipe the bot's memory for the current conversation              |
| `/purge`  | Nuke memory; scope `here` (this convo) or `server` (every convo in the guild — Manage Channels required) |
| `/mood`   | Switch personality preset (`escalating` / `feral` / `villain` / `chill` / `tsundere`) — scope `server` or `here` |
| `/rage`   | Show the current patience meter for this conversation (with bar + tier) |
| `/status` | Show model, mood, memory, scope, active conversation count      |
| `/play`   | Join voice and queue a track **or playlist/album** from YouTube/SoundCloud/Spotify/Apple Music (URL or search) |
| `/pause`  | Pause the current track                                         |
| `/resume` | Resume a paused track                                           |
| `/skip`   | Skip to the next track in the queue                             |
| `/stop`   | Clear the queue and stop playback                               |
| `/queue`  | Show what's queued                                              |
| `/nowplaying` | Show what's playing right now                               |
| `/leave`  | Disconnect from voice                                           |
| `/loop`   | Loop the current track, the queue, or turn looping off          |
| `/shuffle`| Shuffle the queue                                               |
| `/volume` | Set playback volume 0-200% (default 100)                        |
| `/remove` | Remove a track from the queue by position                       |
| `/clear`  | Clear the queue but keep the current track playing              |
| `/jump`   | Skip ahead to a specific queue position                         |
| `/dj`     | Set the DJ role (only it can control playback), or show the current one (Manage Channels to set) |
| `/djoff`  | Clear the DJ role so everyone can control playback again (Manage Channels) |

`/mood` accepts a `scope` arg:
- `server` (default) — applies to the whole guild (requires Manage Channels)
- `here` — applies only to the current channel/thread/DM (anyone can set)

Per-conversation overrides win over server defaults. All settings persist to `channels.json` across restarts.

### Moods

- **`escalating`** *(default)* — Starts polite and helpful. Patience drains every turn (+1) and faster on dumb messages (short, all-caps, repeats, low-effort openers like "yo"/"bruh", begging like "plsss"). Decays ~1 point per 5 minutes idle. At low patience you get a warm helper, at mid you get sarcasm, at high you get the unhinged cussing menace. The bot doesn't mention the meter to users — just calibrates tone.
- **`feral`** — Immediately unhinged, no warmup. The OG.
- **`villain`** — Theatrical Saturday-morning supervillain.
- **`chill`** — Friendly, mildly sarcastic, swears sparingly.
- **`tsundere`** — Cold and prickly on the surface, secretly invested. Pretends not to want to help, then helps thoroughly. Stutters when flustered. "B-baka!" energy. Never actually cruel — the bark is for show.

Use `/status` to see the current patience meter for a conversation. `/reset` and `/purge` both wipe it.

## Customization

Edit `bot.py`:
- `MODEL` — currently `claude-sonnet-4-6` (balanced); switch to `claude-haiku-4-5` for cheapest/fastest or `claude-opus-4-7` for max intelligence
- `SYSTEM_PROMPT` — change the bot's personality
- `MAX_TURNS` — how many turns of history to remember per channel (default 50)
- `MAX_TOKENS` — max length of each Claude response (default 2048)
