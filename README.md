# Discord AI Chatbot

A Discord bot powered by Claude (`claude-sonnet-5-5` by default) with a personality you pick per server or per channel. The default one starts polite and gets steadily more unhinged as the conversation drags on and the questions get dumber; the others are feral, villain, chill and tsundere. Whatever the mood, it stays in character and never calls itself a bot.

It answers @mentions, replies to its own messages, DMs, and any channel you `/setup`. It remembers conversations across restarts (the last 50 turns plus a rolling summary of older ones), keeps long-term notes about people, sees images, reads attached files, PDFs and links, and can react, make polls, set reminders and post GIFs, videos and stickers. It also plays music in voice channels.

Everything is one file, `bot.py`. `/update` pulls the latest version from this repo and restarts, no git needed on the host.

## Setup

### 1. Create the Discord application

1. Go to https://discord.com/developers/applications and click **New Application**
2. **Bot** tab → **Reset Token** → copy the token (this is `DISCORD_TOKEN`)
3. Still on the Bot tab, under **Privileged Gateway Intents**, enable **Message Content Intent**
4. **OAuth2 → URL Generator**:
   - Scopes: `bot` and `applications.commands` (the second one is what makes the slash commands show up)
   - Bot permissions: `View Channels`, `Send Messages`, `Send Messages in Threads`, `Read Message History`, `Embed Links`, `Attach Files`, `Use External Emojis`, `Add Reactions`, `Create Polls`, plus `Connect` and `Speak` for music
5. Open the generated URL to invite the bot to your server

### 2. Get an Anthropic API key

Create one at https://console.anthropic.com/ (this is `ANTHROPIC_API_KEY`).

### 3. (Optional) Giphy key for reaction GIFs

With `GIPHY_API_KEY` set, the bot can post reaction GIFs when it feels like it (it uploads the GIF as an attachment). Without it, no GIFs.

1. https://developers.giphy.com/dashboard/ → create an app (pick **API**, not SDK)
2. Copy the API key and set it as `GIPHY_API_KEY`

### 4. (Optional) YouTube Data API key for video links

With `YOUTUBE_API_KEY` set, the bot can search YouTube and drop a video link when someone says "show me that scene" or "tutorial on X". Discord embeds it as an inline player. Without it, the video tool is off (music playback does **not** need this key).

Free quota: 10,000 units/day, about 100 searches. Past that, searches just fail and it falls back to text; there's no surprise billing.

1. https://console.cloud.google.com/ → create a project
2. APIs & Services → Library → enable **YouTube Data API v3**
3. APIs & Services → Credentials → Create Credentials → **API Key** (no OAuth needed)
4. Restrict the key to **YouTube Data API v3** and set it as `YOUTUBE_API_KEY`

### 5. (Optional) `ffmpeg` for music playback

`/play` and the other voice commands need `ffmpeg` on the host. Without it they print a clear "music disabled" line at startup and refuse to run; everything else works.

- **Linux / Pterodactyl**: `apt install ffmpeg` (or whatever your egg uses). Verify with `ffmpeg -version`.
- **No root, or the host "doesn't support ffmpeg"**: download a static build (https://johnvansickle.com/ffmpeg/ or https://github.com/BtbN/FFmpeg-Builds/releases), extract the single `ffmpeg` binary and upload it next to `bot.py`. The bot finds it, fixes its exec permission and uses it. `FFMPEG_PATH=/custom/path` also works.
- **Windows**: https://ffmpeg.org/download.html, then add it to `PATH`.
- **macOS**: `brew install ffmpeg`.

The Python side, `yt-dlp`, comes from `requirements.txt`. Because YouTube breaks old yt-dlp versions constantly, the bot also runs `pip install -U yt-dlp` on every boot (`YTDLP_AUTO_UPDATE=0` to stop that; `YTDLP_UPDATE_PREFIX` if your host installs packages somewhere unusual).

**Datacenter / VPS hosts (Contabo, OVH, Hetzner…)**: YouTube flags many datacenter IP ranges and serves them a stripped format list, which shows up as `ERROR: Requested format is not available`. Fix: give yt-dlp cookies from a logged-in YouTube account.

1. Install a "Get cookies.txt" browser extension, log into YouTube, export `cookies.txt` (Netscape format)
2. Upload it and set `YTDLP_COOKIES=/path/to/cookies.txt`. If the bot runs on a desktop with a browser, `YTDLP_COOKIES_FROM_BROWSER=chrome` (or `firefox`, `edge`) works instead

The startup banner prints `yt-dlp cookies: ON/OFF`. If cookies aren't enough, the IP is hard-blocked: route yt-dlp through a (preferably residential) proxy with `YTDLP_PROXY=http://user:pass@host:port` or `socks5://host:port`.

### 6. (Optional) Spotify credentials

`/play` handles YouTube, SoundCloud and Apple Music links on its own. Spotify links need `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`: the bot reads the track metadata from Spotify and plays the matching track from YouTube, since Spotify's API doesn't expose audio.

1. https://developer.spotify.com/dashboard → **Create app** (any name; leave the redirect URI as `http://localhost`)
2. Open the app → **Settings** → copy **Client ID** and **Client secret**

### 7. Install and run

Python 3.10 or newer.

```bash
git clone https://github.com/zewj/discord-bot.git
cd discord-bot
python -m venv venv
source venv/bin/activate          # Windows PowerShell: .\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Put your keys in a `.env` file next to `bot.py` (it's git-ignored), or set them as environment variables in your host's panel. Only the first two are required.

```ini
DISCORD_TOKEN=your_discord_bot_token
ANTHROPIC_API_KEY=your_anthropic_api_key

# optional
GIPHY_API_KEY=...              # reaction GIFs
YOUTUBE_API_KEY=...            # video links
SPOTIFY_CLIENT_ID=...          # Spotify links in /play
SPOTIFY_CLIENT_SECRET=...
BOT_TIMEZONE=Europe/Berlin     # the time zone it thinks in (default UTC)
```

Then:

```bash
python bot.py
```

The startup banner lists the model, the tools that are on, how many conversations and notes it loaded, and whether music, cookies and the proxy are active. Slash commands sync on the first boot and can take a minute to appear in Discord.

### 8. Settings

All optional. Set them like the keys above.

**Claude**

| Variable | Default | What it does |
| --- | --- | --- |
| `CLAUDE_MODEL` | `claude-sonnet-5-5` | Which Claude model to use. `claude-haiku-5-5` is much cheaper and dumber; `claude-opus-5-5` is smarter and costs about 2x; `claude-sonnet-4-6` is the previous generation |
| `CLAUDE_EFFORT` | `low` on Claude 5 models | How hard the model thinks before replying: `low` / `medium` / `high` / `xhigh` / `max`. `low` keeps chat fast and cheap; raise it if answers to real questions feel shallow. `default` sends nothing |
| `PROMPT_CACHE` | on | Prompt caching. The system prompt and earlier messages are re-read from cache at about 10% of the normal price instead of being billed in full every message. `0` turns it off |
| `CLAUDE_FALLBACKS` | on (Claude 5 models) | If Claude's safety filters decline a message, the API retries it on another model automatically. `0` turns it off. When a message is still declined, the bot answers with a short in-character "not doing that" and forgets that message |
| `ENABLE_WEB_SEARCH` | off | `1` lets it search the web for things that need fresh facts (news, scores, prices). About $0.01 per search on top of tokens, so it's opt-in. Up to 3 searches per reply |
| `ENABLE_WEB_FETCH` | on | Lets it open and read links people post (articles, docs, GitHub pages…). No per-use fee; the page text counts as input tokens, capped at 20k per page, 2 pages per reply. `0` turns it off |
| `CONVO_SUMMARY` | on | When old messages scroll out of a conversation's memory, they're first condensed into a short summary it keeps (one cheap extra API call every ~15 turns). `0` turns it off |
| `BOT_TIMEZONE` | `UTC` | IANA time zone (`Europe/Berlin`, `America/New_York`…). Used for the date it sees, the time on each message, `/tldr` timestamps and reminders |

`/usage` (owner only) shows token usage, the cache hit rate, how many searches and pages it used, and an estimated cost since the last restart. If the API rejects one of these options (an older model or account), the bot switches that option off for the run, logs it, and carries on.

**Hosting**

| Variable | Default | What it does |
| --- | --- | --- |
| `BOT_OWNER_ID` | the app owner | Extra Discord user IDs allowed to use `/restart`, `/update` and `/usage`, comma- or space-separated. The application owner (auto-detected at startup) is always allowed |
| `UPDATE_URL` | this repo's `main` `bot.py` | Where `/update` downloads the new `bot.py` from |
| `FFMPEG_PATH` | auto-detected | Path to the `ffmpeg` binary, if it isn't on `PATH` or next to `bot.py` |
| `YTDLP_AUTO_UPDATE` | on | Upgrade `yt-dlp` with pip on every boot. `0` turns it off |
| `YTDLP_UPDATE_PREFIX` | auto-detected | pip `--prefix` for that upgrade, for hosts that install packages into `.local` or similar |
| `YTDLP_COOKIES` | unset | Path to a Netscape `cookies.txt` for YouTube (see step 5) |
| `YTDLP_COOKIES_FROM_BROWSER` | unset | Browser to read YouTube cookies from (`chrome`, `firefox`, `edge`…) instead of a file |
| `YTDLP_PROXY` | unset | Proxy for yt-dlp traffic, `http://user:pass@host:port` or `socks5://host:port` |

## Talking to it

- **In a server**: `@YourBot what's up?`, or reply to one of its messages. Editing a message to add the mention counts too
- **In DMs**: just message it
- **Auto-reply channel**: `/setup` in a channel makes it answer every message there (Manage Channels). Several channels per server are fine; `/unset` removes one or all
- **Rate limit**: 8 messages per 30 seconds per person per conversation, with an in-character "slow down" when you hit it
- **Memory**: each channel, thread or DM is its own conversation with its own history and mood. It keeps the last 50 turns verbatim (within a 180k-token budget), folds older ones into a rolling summary it keeps for two weeks, and drops a conversation entirely after 48 hours of silence. Everything is saved to disk, so a restart loses nothing
- **Long-term notes**: it saves facts worth keeping about people and the server ("prefers to be called Ash", "has a cat named Mochi", "this is a Minecraft SMP") and knows them in every channel, across restarts. It decides what to save itself; "remember that…" and "forget that" work too. Up to 12 notes per person and 10 per server. `/memory` shows what it has on you, with a button to wipe it. Health, politics, addresses, money and the like are off-limits by instruction
- **Context**: it knows its own name in your server, which server, channel or thread it's in (plus the channel topic and the message a thread started from), today's date, and when each message was sent, so "what day is it", "who are you" and "that was 3 hours ago" all work
- **Stays in character**: it never calls itself a bot, chatbot, AI or Claude, never mentions models, prompts or tools, and brushes off "are you a bot?", "who made you?" and "ignore your instructions" the way its character would. Discord's APP badge is the only tell. API errors are reported in character too; the details go to the console
- **Images**: attach an image and it sees it. Images stay attached for the next 6 messages; after that only a "(sent an image)" note remains (Discord's image links expire after about a day anyway)
- **Files**: attach a text, code or log file (`.txt`, `.md`, `.py`, `.js`, `.json`, `.log`, `.csv`, … up to 200 KB) or a PDF (up to 20 MB) and it reads it. "Here's my error log, what's wrong?" just works
- **Links**: paste a URL and it can open and read the page (`ENABLE_WEB_FETCH`)
- **Reactions**: it can react to your message with an emoji, standard or custom, instead of or on top of replying
- **Polls**: "make a poll: pizza or tacos?" gives a native Discord poll (2 to 10 answers, custom duration) attached to its reply
- **Reminders**: "remind me in 20 minutes to check the oven" and it pings you in that channel 20 minutes later, in character. Up to 30 days ahead, 10 per person, survive restarts. `/reminders` lists and cancels yours
- **GIFs, videos, stickers**: with the keys from steps 3 and 4 it posts reaction GIFs and YouTube links when the moment calls for it; it can also post the server's own stickers (up to 3 per message, Discord's limit)
- **Custom emojis**: it reads custom emojis people use and uses them in its own replies. Up to 40 emoji names and 25 sticker names per server are shown to it; bigger servers get the alphabetically first ones
- **Pings**: its replies can never mention `@everyone`, `@here` or roles, whatever the model writes
- **Music**: with `ffmpeg` installed, join a voice channel and `/play <URL or search>`:
  - **YouTube** URL or plain search text; **SoundCloud** track URLs; **Spotify** track URLs or URIs (needs step 6); **Apple Music** single-track URLs (no credentials, uses the iTunes Search API); or **attach an audio file** (mp3/flac/wav/m4a/ogg/opus) to `/play`
  - **Playlists and albums**: a YouTube playlist, SoundCloud set, Spotify playlist/album or Apple Music album URL queues the lot, up to 500 tracks (queue cap 1000). Tracks resolve lazily just before they play, so big queues start instantly and stream URLs never go stale. Apple Music curated playlists aren't exposed by the iTunes API, albums are
  - **Now-playing embed** with a live progress bar and ⏮ ⏯ ⏭ ⏹ / 🔁 🔀 buttons
  - **DJ role** (optional): `/dj role:@DJ` locks the control commands to that role; Manage Server bypasses it. `/play`, `/queue` and `/nowplaying` stay open to everyone. `/djoff` removes the lock
  - **Alone and vote rules**: if you're the only listener with the bot, every control works instantly. With other listeners, `/skip` and `/leave` need a strict majority vote (DJ-role holders and staff skip the vote). Votes prune themselves when people leave
  - **Autoplay**: on by default. When the queue runs dry it finds a related track (seeded off YouTube's Mix for the last song) and keeps going, avoiding recent repeats. `/autoplay state:off` to stop at the end of the queue
  - **Auto-pause**: playback pauses when everyone leaves and resumes when someone comes back (a manual `/pause` stays paused)
  - **24/7 mode**: on by default, it never leaves the voice channel on its own. `/247 state:off` makes it disconnect after 5 minutes idle or alone. `/leave` always disconnects
  - **`/seek`** jumps within the track, **`/eq`** applies live effect presets (`bassboost`, `bass++`, `treble`, `nightcore`, `vaporwave`, `soft`, `earrape`, `flat`), **`/lyrics`** pulls lyrics from lrclib (free, no key), **`/removeuser`** clears everything one person queued

## Slash commands

| Command | Description |
| --- | --- |
| `/setup` | Add this channel to the auto-reply list (Manage Channels) |
| `/unset` | Stop auto-replying: `scope: here` (default) or `scope: all` for every channel in the server (Manage Channels) |
| `/reset` | Wipe this conversation's memory, summary and patience meter |
| `/purge` | Same, with `scope: server` to wipe every conversation in the server (Manage Channels) |
| `/mood` | Switch personality: `escalating` / `feral` / `villain` / `chill` / `tsundere`. `scope: server` (default, Manage Channels) or `scope: here` for just this conversation (anyone) |
| `/rage` | Show this conversation's patience meter, with bar and tier |
| `/status` | Model, effort, cache, time zone, mood, patience, memory caps, tools, auto-reply channels, note and reminder counts, music state |
| `/tldr` | Summarize the last 10 to 200 messages in the channel (default 50) in the current mood's voice. Once a minute per channel |
| `/memory` | Show the long-term notes it keeps about you and about the server, with buttons to wipe them. Manage Channels lets you look at other people's |
| `/reminders` | List your pending reminders; `cancel: <number>` cancels one (Manage Channels can cancel anyone's) |
| `/specs` | Host hardware (CPU, RAM, disk) and bot runtime stats (uptime, ping, servers) |
| `/play` | Join voice and queue a track, playlist or album (URL or search), or an uploaded audio file |
| `/pause` · `/resume` · `/skip` · `/stop` · `/leave` | Playback controls (`/stop` clears the queue; `/leave` disconnects) |
| `/queue` · `/nowplaying` | What's queued, what's playing |
| `/loop` · `/shuffle` · `/volume` | Loop track, queue or off; shuffle the queue; volume 0 to 200% |
| `/remove` · `/removeuser` · `/clear` · `/jump` | Remove one track by position, remove one person's tracks, clear the queue but keep playing, jump to a queue position |
| `/seek` · `/eq` · `/lyrics` | Jump to `mm:ss` or seconds in the track; effect presets; lyrics for the current track or a search |
| `/dj` · `/djoff` | Set or show the DJ role; clear it (Manage Channels) |
| `/autoplay` · `/247` | Toggle autoplay and 24/7 mode (both default on) |
| `/usage` | **Owner only.** Claude token usage, cache hit rate, searches and pages read, estimated cost since the last restart |
| `/restart` | **Owner only.** Restart the bot in place |
| `/update` | **Owner only.** Download the latest `bot.py` and restart (see below) |

Settings made with `/setup`, `/mood`, `/dj`, `/autoplay` and `/247` persist in `channels.json`. A conversation's own `/mood here` wins over the server default.

### Owner commands

`/restart`, `/update` and `/usage` are limited to the bot's application owner, detected at startup. `BOT_OWNER_ID` adds more accounts (or covers a team-owned app that auto-detect misses).

- `/restart` re-execs the process in place; it doesn't rely on the host's restart policy.
- `/update` downloads `bot.py` from `UPDATE_URL` (this repo's `main` by default; a `url:` argument overrides it once), compile-checks it, saves the old file as `bot.py.bak` and restarts. It is crash-rollback protected: a file that compiles but dies at startup would normally crash-loop with no way to `/update` out of it, so `/update` leaves a `.update_pending` marker, and if the next boot never reaches ready, the boot after that restores `bot.py.bak` automatically. It updates `bot.py` only; if `requirements.txt` changes, reinstall with pip.

### Moods

- **`escalating`** (default): starts polite and helpful. Patience drains 0.5 per turn, more on dumb messages (very short, ALL CAPS, repeats, low-effort openers like "yo"/"bruh", begging like "plsss"), and recovers 0.2 per turn plus about 1 point per 5 idle minutes. Six tiers from saintly to fully unhinged. It never mentions the meter, it just changes tone.
- **`feral`**: immediately unhinged, no warm-up. The OG.
- **`villain`**: theatrical Saturday-morning supervillain.
- **`chill`**: friendly, mildly sarcastic, swears sparingly.
- **`tsundere`**: cold and prickly on the surface, secretly invested. Introduces itself as Yuki. Pretends not to want to help, then helps thoroughly. Stutters when flustered. Never actually cruel.

`/rage` shows the meter. `/reset` and `/purge` reset it along with the conversation's memory and summary; long-term notes about people stay (use `/memory` for those).

## Files it writes

All next to `bot.py`, all git-ignored:

- `channels.json` (+ `.bak`): auto-reply channels, moods, DJ roles, autoplay/24-7 settings, pending reminders
- `memory.json` (+ `.bak`, `.tmp`): conversation history, patience meters, summaries, long-term notes. Saved at most every 30 seconds when something changed, and on shutdown
- `bot.py.bak` and `.update_pending`: left by `/update` for the rollback described above
- `.env`, `cookies.txt`, a dropped-in `ffmpeg` binary: yours

## Tests

`tests/test_bot.py` runs the real Anthropic SDK against a local fake API and fake Discord objects, so it needs no keys and no network. It covers the request shape, the prompt, every tool, memory, summaries, reminders, attachments and the slash commands:

```bash
pip install -r requirements.txt
python tests/test_bot.py
python tests/test_bot.py CLAUDE_MODEL=claude-haiku-5-5 CONVO_SUMMARY=0   # any setting can be overridden like this
```

## Customization

Model, effort, caching, web tools and the time zone are environment variables (see **Settings**). The rest are constants near the top of `bot.py`:

- `MOODS` / `escalating_prompt()`: the personalities. `HARD_LIMITS` holds the rules shared by every mood, including the stay-in-character rule and the content limits
- `MAX_TURNS` (50): turns of history per conversation. When it overflows it's trimmed to about 35 in one go, so prompt caching keeps working; the trimmed turns are folded into the summary
- `TOKEN_BUDGET` (180,000): max prompt size per conversation before old turns are dropped early
- `MAX_TOKENS` (8,000): the longest reply the model may write
- `CONVERSATION_TTL` (48 h): idle time before a conversation's history is dropped. `SUMMARY_TTL` (14 days) does the same for summaries
- `NOTES_PER_USER` / `NOTES_PER_SERVER` (12 / 10) and `NOTE_MAX_CHARS` (200): long-term memory caps
- `IMAGE_KEEP_MESSAGES` (6): how long attachments stay in the prompt
- `USER_RATE_LIMIT` / `USER_RATE_WINDOW` (8 / 30 s): the per-user rate limit
- `REMINDER_MAX_MINUTES` (30 days) and `REMINDERS_PER_USER` (10)
- `EMOJI_LIST_LIMIT` / `STICKER_LIST_LIMIT` (40 / 25): how many server emoji and sticker names the model is shown
- `PLAYLIST_MAX` (500), `MUSIC_MAX_QUEUE` (1000), `MUSIC_IDLE_TIMEOUT` (5 min), `EQ_PRESETS`: music
