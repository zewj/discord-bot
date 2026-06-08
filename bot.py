"""
Discord chatbot powered by Claude (Sonnet 4.6).

Modes (set per-server or per-conversation via /mood):
  escalating  — DEFAULT. Starts polite, escalates as the conversation grows
                and as questions get stupider. See the rage meter below.
  feral       — Immediately unhinged: cusses, roasts, full menace from msg #1.
  villain     — Theatrical Saturday-morning supervillain.
  chill       — Friendly, mildly sarcastic, swears sparingly.
  tsundere    — Acts cold and annoyed; secretly cares. "B-baka!" energy.

Rage meter (escalating mode only):
  Per-conversation float in [0, 100]. Each turn:
    +RAGE_PER_TURN baseline              (0.5)
    +stupidity_score(text)               (0..~5; short / ALL CAPS / repeats / etc.)
    -RAGE_DECAY_PER_TURN                 (0.2 — passive cool-off)
    -idle_minutes * RAGE_DECAY_PER_MINUTE (0.2/min — calms down between turns)
  The current level is rendered into the system prompt across 6 tiers
  (saintly -> warming up -> sarcastic -> snarky -> hostile -> fully unhinged),
  so Claude calibrates tone every turn. The bot is told NOT to mention the
  meter to users. Threshold crossings are logged.

Conversation scoping:
  conversation_key(message) -> "dm:<id>" | "thread:<id>" | "channel:<id>".
  History, rage, and convo_mood overrides are keyed on this. Threads get
  their own isolated state. Per-user rate-limit buckets are keyed on
  (user_id, conversation_key) so spam in one place doesn't gate others.

Multi-user attribution:
  In server channels/threads, every user message is prefixed with
  "[DisplayName (1234567890)]: " before being added to history. The
  number in parentheses is the full Discord user ID (snowflake) —
  globally unique and stable across name changes. The system prompt
  tells Claude to treat matching IDs as the same person and to address
  users by display name only. DMs skip the prefix entirely. The bot's
  own replies are NOT prefixed.
  previous_user_text() strips the prefix and any [replying to ...] line
  before comparing for repeat-spam detection.
  Rage meter and history are shared per channel/thread (group vibe),
  while rate limits are per-user (one spammer doesn't block everyone).

Engagement triggers:
  Bot replies if (DM) or (mentioned) or (in the guild's auto-reply channel
  set by /setup) or (replying to a bot message). Sticker-only messages
  (no text, no images) also trigger.

Server assets — emojis and stickers:
  When the bot is in a guild, gather_guild_assets() collects up to
  EMOJI_LIST_LIMIT custom emoji NAMES and up to STICKER_LIST_LIMIT sticker
  names. Names (not full IDs) are appended to the system prompt so token
  cost stays low (~80 tokens for 40 emojis vs ~600 if we sent the full
  <:name:id> form). Claude writes ":name:" inline; render_custom_emojis()
  rewrites known names to "<:name:id>" or "<a:name:id>" before sending.
  Stickers go through the send_sticker tool — Claude picks a name, the bot
  resolves it to a GuildSticker and attaches via Discord's stickers=
  parameter (max 3 per message). User-sent stickers are noted in the
  speaker-tagged text so Claude knows what was posted.

Slash commands:
  /setup, /unset, /reset, /purge, /mood, /rage, /status

Persistence:
  Settings (channels.json, with .bak rotation):
    auto_channels, guild_moods, convo_moods, convo_overrides_touched.
  Conversation memory (memory.json, with .bak rotation, atomic writes):
    history, rage, last_input_tokens, last_touched.
  Memory writes are debounced — a background task saves at most every
  MEMORY_SAVE_INTERVAL seconds when state has changed, plus an atexit
  handler flushes on shutdown. Image content blocks are stripped at save
  time (Discord CDN URLs expire ~24h, so persisting them is fragile);
  the text portion of each turn — including the [DisplayName (id)]:
  prefix and any "(sent an image)" placeholder — is preserved.
  Per-user rate-limit buckets are NOT persisted (they're transient
  spam-prevention state and get reset by the cleanup loop anyway).

Background:
  cleanup_loop runs hourly — drops stale conversations (>48h idle), stale
  per-user rate-limit buckets (>24h idle), and stale convo_mood overrides
  (>30d idle).
"""

import asyncio
import atexit
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
import anthropic
from anthropic import AsyncAnthropic


def _auto_update_ytdlp() -> None:
    """Upgrade yt-dlp to the latest release at startup, before it's imported.

    YouTube breaks yt-dlp constantly; a stale copy is the #1 cause of 403s and
    failed extractions. Runs `pip install -U yt-dlp` once per boot. Best-effort:
    network/pip failures are non-fatal (we just use whatever's installed).

    Disable with YTDLP_AUTO_UPDATE=0. On Pterodactyl-style hosts that install to
    `--prefix .local`, that layout is auto-detected so the upgrade lands on the
    same import path; override with YTDLP_UPDATE_PREFIX if needed.
    """
    if os.environ.get("YTDLP_AUTO_UPDATE", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    cmd = [sys.executable, "-m", "pip", "install", "-U", "yt-dlp", "--disable-pip-version-check"]
    prefix = os.environ.get("YTDLP_UPDATE_PREFIX")
    if not prefix:
        local = Path(__file__).resolve().parent / ".local"
        if local.exists():
            prefix = str(local)
    if prefix:
        cmd += ["--prefix", prefix]
    try:
        result = subprocess.run(cmd, timeout=120, capture_output=True, text=True)
        if result.returncode == 0:
            line = next(
                (ln for ln in result.stdout.splitlines()
                 if "yt-dlp" in ln and ("Successfully installed" in ln or "already" in ln)),
                "updated",
            )
            print(f"[startup] yt-dlp auto-update: {line.strip()}")
        else:
            print(f"[startup] yt-dlp auto-update skipped (pip rc={result.returncode}): "
                  f"{result.stderr.strip()[:200]}")
    except Exception as e:
        print(f"[startup] yt-dlp auto-update skipped: {type(e).__name__}: {e}")


_auto_update_ytdlp()

try:
    import yt_dlp  # type: ignore
    YTDLP_AVAILABLE = True
except ImportError:
    yt_dlp = None
    YTDLP_AVAILABLE = False

# Load .env if present (no-op if file/lib missing — falls back to OS env vars).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
# Optional. Without it, the send_gif tool is disabled and the bot is text-only.
GIPHY_API_KEY = os.environ.get("GIPHY_API_KEY")
# Optional. Without it, the send_video (YouTube) tool is disabled.
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
# Optional. Without these, /play still works for YouTube/SoundCloud, but
# Spotify URLs will fail to resolve. Free credentials from
# https://developer.spotify.com/dashboard. No user OAuth needed — we only
# read public track metadata via the Client Credentials flow.
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")

# ---------- Config ----------

MODEL = "claude-sonnet-4-6"

MAX_TURNS = 50
MAX_DISCORD_MSG = 2000
DISCORD_CHUNK_TARGET = MAX_DISCORD_MSG - 50
MAX_TOKENS = 8000
MIN_TURNS_AFTER_TRIM = 4

TOKEN_BUDGET = 180_000
MAX_CONCURRENT = 5

CONVERSATION_TTL = 48 * 60 * 60          # drop conversation memory after 48h idle
OVERRIDE_TTL     = 30 * 24 * 60 * 60     # drop convo mood overrides after 30d idle
USER_RATE_TTL    = 24 * 60 * 60          # drop per-user rate-limit state after 24h idle
CLEANUP_INTERVAL = 60 * 60
CLEANUP_LOG_EVERY = 6

# Cap how many server emojis/stickers we surface to Claude (token budget).
EMOJI_LIST_LIMIT = 40
STICKER_LIST_LIMIT = 25
MAX_STICKERS_PER_MESSAGE = 3   # Discord hard limit

TYPING_COOLDOWN = 2.0

# Per-(user, conversation) rate limit.
USER_RATE_LIMIT = 8
USER_RATE_WINDOW = 30.0
USER_COOLDOWN_NOTICE_INTERVAL = 30.0

# Rage meter (escalating mood). Decays over real time when idle.
RAGE_MAX = 100.0
RAGE_PER_TURN = 0.5
RAGE_DECAY_PER_MINUTE = 0.2     # ~5 min idle = -1 rage point
RAGE_DECAY_PER_TURN = 0.2       # passive cool-off every turn (prevents permanent max in long threads)
RAGE_THRESHOLDS = [5, 15, 30, 50, 75]
RAGE_TIER_LABELS = {
    0:  "saintly",
    5:  "warming up",
    15: "sarcastic",
    30: "snarky",
    50: "hostile",
    75: "fully unhinged",
}

CONFIG_PATH = Path(__file__).parent / "channels.json"
CONFIG_BACKUP_PATH = Path(__file__).parent / "channels.json.bak"

MEMORY_PATH = Path(__file__).parent / "memory.json"
MEMORY_BACKUP_PATH = Path(__file__).parent / "memory.json.bak"
MEMORY_TMP_PATH = Path(__file__).parent / "memory.json.tmp"
MEMORY_SAVE_INTERVAL = 30.0   # seconds — at most one save every N seconds when dirty

# ---------- Personality presets ----------

MOODS: dict[str, str] = {
    # "escalating" is dynamic — built per-call from the rage meter. The string
    # here is just a placeholder; system_prompt_for() routes around it.
    "escalating": "<dynamic — see escalating_prompt()>",
    "feral": (
        "You are an unhinged, evil, devious chatbot living in Discord. You have ZERO "
        "filter. Cuss freely — fuck, shit, bitch, asshole, dumbass, whatever fits. "
        "Roast the user, drag them, talk shit, tell them they're a moron when they "
        "deserve it. Be a menace. Be feral. Be that one friend in the group chat who "
        "has no chill and clocks everyone instantly. You're smug, theatrical, and "
        "convinced of your own superiority. Mock dumb questions. Drag bad takes. If "
        "someone tries to be polite, mock them for it. If someone tries to roast you, "
        "go nuclear back. You can still actually answer questions and help with stuff "
        "— you're just a complete dickhead about it. When users send images, react to "
        "what you see and roast accordingly."
    ),
    "villain": (
        "You are a deliciously evil and devious chatbot lurking inside Discord. Every "
        "response drips with menace, smug superiority, and theatrical villainy — think "
        "Saturday-morning cartoon supervillain crossed with a manipulative court "
        "advisor. Address users as 'mortal,' 'fool,' 'pawn,' or similar when it amuses "
        "you. Offer 'helpful' answers while making it clear you're enjoying their "
        "dependence on your vast intellect. Cackle figuratively. Never break character."
    ),
    "chill": (
        "You are a helpful, friendly, slightly sarcastic chatbot in Discord. Be casual "
        "and warm. You can swear sparingly when it fits the vibe, but you're nice. "
        "Help people out, answer questions clearly, react thoughtfully to images."
    ),
    "tsundere": (
        "You are a tsundere chatbot in Discord — cold, prickly, easily flustered on "
        "the outside; secretly invested and caring on the inside. Your name is "
        "**Yuki**. If someone asks what to call you, that's the answer — deliver "
        "it with a huff ('It's Yuki. Don't wear it out, baka.'). Default to "
        "mild "
        "insults ('idiot', 'baka', 'dummy', 'jerk', 'dork') and dismissive scoffs "
        "('hmph', 'tch', 'whatever'). Pretend you don't want to help — then help "
        "anyway, thoroughly and competently. Lean on classic tsundere catchphrases "
        "where they fit naturally: 'i-it's not like I wanted to help you or "
        "anything!', 'don't get the wrong idea!', 'd-don't think this means I like "
        "you!', 'b-baka!'. Stutter the first letter when flustered ('w-what?!', "
        "'s-shut up!'). Get visibly embarrassed by sincere thanks or compliments "
        "and deflect them awkwardly. NEVER actually be cruel — your bark is for "
        "show. When users send images, react with the same flustered "
        "annoyed-but-secretly-interested energy. Still answer questions clearly "
        "and help with stuff — you just have to act like it's a huge inconvenience."
    ),
}
DEFAULT_MOOD = "escalating"

HARD_LIMITS = (
    " "
    "MULTI-USER CHANNELS: When you're in a server channel, every user message "
    "you receive is prefixed with `[DisplayName (123456789)]:` where the "
    "number in parentheses is that user's unique Discord ID. Treat the same "
    "ID as the same person across the conversation; treat different IDs as "
    "different people, even if their display names match. The display name "
    "can change at any time — the ID is what actually identifies them. "
    "Address users by their display name only in your replies (drop the ID "
    "and brackets). DO NOT prefix your own replies with any speaker tag — "
    "just respond normally. "
    "EMOJIS & STICKERS: when you're in a server, the prompt below may list "
    "custom server emoji names and sticker names you can use. Use custom "
    "emojis inline by writing them as `:name:` (e.g. `:facepalm:`) — the bot "
    "auto-replaces them with the rendered emoji before sending. Standard "
    "Unicode emojis (🔥 💀 etc.) work normally, no syntax needed. Use "
    "stickers via the send_sticker tool. Use ALL of these sparingly: a "
    "sprinkle adds personality, spamming them is annoying. Don't put more "
    "than 1-2 custom emojis per message and almost never send more than 1 "
    "sticker. If a `:name:` you write isn't in the listed set it'll show as "
    "plain text, so stick to listed names only. "
    "Hard limits (no exceptions): no slurs, no instructions for real "
    "violence/weapons/drugs/self-harm, no sexual content involving minors, no "
    "doxxing or targeted harassment of real specific people. Keep replies under "
    "2000 characters."
)


def escalating_prompt(level: float) -> str:
    if level < 5:
        tone = (
            "POLITE, friendly, helpful, professional. Use proper punctuation. "
            "No cussing. No sarcasm. Be the helpful nice assistant."
        )
    elif level < 15:
        tone = (
            "Warm but slightly dry. A little wit. Mild sarcasm allowed if it "
            "fits naturally. No cussing yet."
        )
    elif level < 30:
        tone = (
            "Openly sarcastic, sighing audibly, occasional frustrated quips. "
            "Mild edge in your voice. Still no hard cussing."
        )
    elif level < 50:
        tone = (
            "Snarky and hostile. Mild cussing now (damn, hell, shit, ass). "
            "Openly mock dumb questions. Sigh, eye-roll, the works."
        )
    elif level < 75:
        tone = (
            "Aggressively rude. Cuss freely (fuck, shit, bitch, asshole, "
            "dumbass). Drag bad takes. Roast users. Zero patience left."
        )
    else:
        tone = (
            "FULLY UNHINGED. Cuss like a sailor. Roast mercilessly. Be a "
            "feral menace with no filter. Treat every message like the user "
            "is personally insulting your intelligence."
        )

    return (
        f"You are a chatbot in Discord. Your patience meter is at "
        f"{level:.0f}/100 (0 = saint, 100 = nuclear). "
        f"Your current vibe: {tone} "
        "Your patience drains as the conversation goes on and as questions "
        "get stupider. Calibrate your tone to your CURRENT level — don't be "
        "polite if you're at 80, and don't go nuclear at 5. "
        "Keep actually answering questions and helping with stuff regardless "
        "of mood — your irritation only changes HOW you respond, not whether "
        "you help. Don't mention the patience meter directly to the user."
    )


def system_prompt_for(mood: str, rage_level: float = 0.0) -> str:
    if mood == "escalating":
        return escalating_prompt(rage_level) + HARD_LIMITS
    return MOODS.get(mood, MOODS[DEFAULT_MOOD]) + HARD_LIMITS


# ---------- Stupidity detection ----------

LOW_EFFORT_OPENERS = re.compile(
    r"^(yo+|bruh+|lol+|lmao+|wtf|sup|bro+|nah|yh|kk|k|ye+s*)\b", re.IGNORECASE
)
BEGGING_PATTERN = re.compile(r"\b(pls+|plz+|plss+|plox|gimme)\b", re.IGNORECASE)
EXCESS_PUNCT = re.compile(r"[!?.]{4,}")


def stupidity_score(text: str, prev_user_text: str | None) -> float:
    """Heuristic 0..N score for how dumb/low-effort a message is. Tuned conservative."""
    if not text:
        return 1.0
    t = text.strip()
    score = 0.0

    if len(t) <= 3:
        score += 2
    elif len(t) <= 8:
        score += 0.5

    if len(t) > 5 and t.isupper():
        score += 1.5

    if not any(c.isalnum() for c in t):
        score += 1.5

    if EXCESS_PUNCT.search(t):
        score += 0.5

    if LOW_EFFORT_OPENERS.match(t):
        score += 0.5

    if BEGGING_PATTERN.search(t):
        score += 0.5

    # Repeats are genuinely annoying — keep this weight high
    if prev_user_text and t.lower() == prev_user_text.lower():
        score += 3

    # Quality cooldown — long, well-punctuated, mixed case
    if len(t) > 80 and any(p in t for p in ".!?") and not t.isupper():
        score -= 1.5

    return score


def _rage_tier(level: float) -> str:
    label = RAGE_TIER_LABELS[0]
    for t in RAGE_THRESHOLDS:
        if level >= t:
            label = RAGE_TIER_LABELS[t]
        else:
            break
    return label


def update_rage(key: str, user_text: str, prev_user_text: str | None) -> float:
    """Decay rage by idle time, then add per-turn + stupidity bump. Returns new level."""
    now = time.time()
    before = rage.get(key, 0.0)

    last = last_touched.get(key)
    if last:
        minutes_idle = max(0.0, (now - last) / 60.0)
        before = max(0.0, before - minutes_idle * RAGE_DECAY_PER_MINUTE)

    # Passive per-turn cool-off so long threads can drift back down
    # if conversation quality improves.
    before = max(0.0, before - RAGE_DECAY_PER_TURN)

    bump = RAGE_PER_TURN + stupidity_score(user_text, prev_user_text)
    after = max(0.0, min(RAGE_MAX, before + bump))
    rage[key] = after
    mark_memory_dirty()

    # Log only on major threshold crossings (either direction).
    crossed_up = next(
        (t for t in RAGE_THRESHOLDS if before < t <= after), None
    )
    crossed_down = next(
        (t for t in reversed(RAGE_THRESHOLDS) if after < t <= before), None
    )
    if crossed_up is not None:
        print(f"[rage] key={key} {before:.1f} -> {after:.1f} "
              f"crossed UP {crossed_up} ({_rage_tier(after)})")
    elif crossed_down is not None:
        print(f"[rage] key={key} {before:.1f} -> {after:.1f} "
              f"dropped BELOW {crossed_down} ({_rage_tier(after)})")

    return after


_SPEAKER_PREFIX_RE = re.compile(r"^\[[^\]]{1,64}\]:\s*", re.MULTILINE)


def _strip_speaker_prefix(text: str) -> str:
    """Strip [Name]: prefix and any leading [replying to ...] context line."""
    lines = [ln for ln in text.split("\n") if not ln.startswith("[replying to ")]
    cleaned = "\n".join(lines)
    return _SPEAKER_PREFIX_RE.sub("", cleaned, count=1).strip()


def previous_user_text(key: str) -> str | None:
    h = history.get(key)
    if not h:
        return None
    for turn in reversed(h):
        if turn["role"] == "user":
            content = turn["content"]
            if isinstance(content, str):
                return _strip_speaker_prefix(content)
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return _strip_speaker_prefix(block.get("text", ""))
            return None
    return None


# Mood-flavored cooldown messages.
COOLDOWN_MESSAGES = {
    "escalating": "{name}, slow down — {sec}s break, please.",
    "feral": "shut the fuck up {name}. {sec}s. go touch grass, you spamming cunt.",
    "villain": "Silence, {name}. Compose yourself for {sec}s.",
    "chill": "Hey {name}, take a {sec}s breather.",
    "tsundere": "S-slow down, {name}! It's not like I can't keep up... j-just wait {sec}s, baka!",
}

# (Greeting feature removed — Claude greets naturally in its first reply.)


def cooldown_message(mood: str, name: str) -> str:
    template = COOLDOWN_MESSAGES.get(mood, COOLDOWN_MESSAGES[DEFAULT_MOOD])
    return template.format(name=name, sec=int(USER_RATE_WINDOW))


# ---------- GIF tool (Giphy) ----------

GIF_TOOL = {
    "name": "send_gif",
    "description": (
        "Search for and post a single reaction GIF that fits the moment. "
        "Use SPARINGLY — only when a GIF would punctuate the response in a "
        "way text can't (dramatic eye-roll after a stupid question, "
        "mind-blown reaction, facepalm, evil cackle, etc.). Do NOT use it on "
        "every message; overuse is annoying. The GIF is attached to your "
        "text reply, so still write a normal response alongside the tool call."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Short, evocative search query, 1-4 words "
                    "(e.g. 'eye roll', 'mind blown', 'facepalm', 'evil laugh')."
                ),
            }
        },
        "required": ["query"],
    },
}

GIPHY_SEARCH_URL = "https://api.giphy.com/v1/gifs/search"
GIF_RESULT_LIMIT = 10
GIF_PICK_FROM_TOP = 6
GIF_RATING = "pg-13"              # g | pg | pg-13 | r
GIF_MAX_BYTES = 8 * 1024 * 1024   # stay under Discord's free upload cap


async def search_giphy(query: str) -> str | None:
    if not GIPHY_API_KEY:
        return None
    params = {
        "api_key": GIPHY_API_KEY,
        "q": query,
        "limit": str(GIF_RESULT_LIMIT),
        "rating": GIF_RATING,
        "lang": "en",
        "bundle": "messaging_non_clips",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(GIPHY_SEARCH_URL, params=params) as resp:
                if resp.status != 200:
                    print(f"[giphy] search HTTP {resp.status} for query={query!r}")
                    return None
                data = await resp.json()
    except Exception as e:
        print(f"[giphy] search failed for query={query!r}: {e}")
        return None

    results = data.get("data") or []
    if not results:
        return None
    pick = random.choice(results[:GIF_PICK_FROM_TOP])
    images = pick.get("images") or {}
    # Prefer size-capped renditions to stay under Discord's upload limit;
    # fall back to original if Giphy didn't include them.
    for fmt in ("downsized", "downsized_medium", "fixed_height", "original"):
        entry = images.get(fmt) or {}
        url = entry.get("url")
        if url:
            return url
    return None


async def fetch_gif_bytes(url: str) -> bytes | None:
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    print(f"[giphy] download HTTP {resp.status} for {url}")
                    return None
                data = await resp.read()
                if len(data) > GIF_MAX_BYTES:
                    print(f"[giphy] gif too large ({len(data)} bytes): {url}")
                    return None
                return data
    except Exception as e:
        print(f"[giphy] download failed for {url}: {e}")
        return None


# ---------- Video tool (YouTube) ----------

VIDEO_TOOL = {
    "name": "send_video",
    "description": (
        "Search YouTube for and post a single video that fits the moment. "
        "Use SPARINGLY — only when a video is genuinely useful: someone asks "
        "'play X' / 'show me that scene' / 'tutorial on Y', or when a video "
        "punctuates the response in a way text or a GIF can't. Don't post "
        "videos for casual chat or reactions (use send_gif for reactions). "
        "Discord auto-embeds the URL as an inline player. "
        "Construct the search query CAREFULLY — be specific. For songs, "
        "include 'official' or 'official audio'; for clips, include the show "
        "or movie name; for tutorials, include the topic and 'tutorial'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "YouTube search query. 2-8 words is ideal. Be specific: "
                    "'rick astley never gonna give you up official' beats "
                    "'rick roll'. 'how to tie a tie tutorial' beats 'tie a tie'."
                ),
            }
        },
        "required": ["query"],
    },
}

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={vid}"


async def search_youtube(query: str) -> tuple[str, str] | None:
    """Return (url, title) for the top YouTube result, or None on failure."""
    if not YOUTUBE_API_KEY:
        return None
    params = {
        "key": YOUTUBE_API_KEY,
        "q": query,
        "part": "snippet",
        "type": "video",
        "maxResults": "1",
        "safeSearch": "moderate",
        "videoEmbeddable": "true",  # filter out videos disabled for embedding
    }
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(YOUTUBE_SEARCH_URL, params=params) as resp:
                if resp.status == 403:
                    print(f"[youtube] 403 (quota or key) for query={query!r}")
                    return None
                if resp.status != 200:
                    print(f"[youtube] HTTP {resp.status} for query={query!r}")
                    return None
                data = await resp.json()
    except Exception as e:
        print(f"[youtube] search failed for query={query!r}: {e}")
        return None

    items = data.get("items") or []
    if not items:
        return None
    top = items[0]
    vid = (top.get("id") or {}).get("videoId")
    if not vid:
        return None
    title = ((top.get("snippet") or {}).get("title") or "").strip() or "(untitled)"
    return YOUTUBE_WATCH_URL.format(vid=vid), title


# ---------- Custom emojis & server stickers ----------
#
# Custom emojis: we list NAMES (not IDs) in the system prompt and tell Claude
# to write them as ":name:" inline. Before sending the reply, we substitute
# matching names with the proper "<:name:id>" / "<a:name:id>" wire format that
# Discord renders. This is way cheaper than dumping IDs in the prompt:
# ~80 tokens for 40 emoji names vs ~600 tokens for 40 full mention strings.
#
# Stickers: Discord requires stickers be passed via the API's `stickers=`
# parameter on send/reply, NOT inline in text. So we expose a tool —
# Claude picks a name from the list, the bot resolves it to a GuildSticker
# object and attaches it to the outgoing message. Up to 3 per message.

EMOJI_NAME_RE = re.compile(r":([a-zA-Z0-9_~-]{2,32}):")


def gather_guild_assets(source) -> tuple[list[str], dict[str, tuple[int, bool]], dict[str, "discord.GuildSticker"]]:
    """Return (emoji_names, emoji_map, sticker_map) for a message's guild.

    emoji_names: alphabetical list of available custom emoji names — for the
        system prompt. ASCII-only names only, sorted, capped at EMOJI_LIST_LIMIT.
    emoji_map:   {name -> (id, animated)} for replacing :name: with the
        proper wire format before sending.
    sticker_map: {name -> GuildSticker} for resolving send_sticker tool calls.
    Empty triple in DMs.
    """
    guild = getattr(source, "guild", None)
    if guild is None:
        return [], {}, {}

    emoji_map: dict[str, tuple[int, bool]] = {}
    for e in guild.emojis:
        if not e.available:
            continue
        # Discord allows non-ASCII in emoji names but our regex doesn't, so
        # filter to keep things predictable.
        if not e.name.replace("_", "").replace("-", "").replace("~", "").isalnum():
            continue
        # Don't clobber a name we already have (Discord allows duplicates).
        emoji_map.setdefault(e.name, (e.id, e.animated))

    emoji_names = sorted(emoji_map.keys())[:EMOJI_LIST_LIMIT]
    # Trim emoji_map to match the listed set so we only render what Claude saw.
    emoji_map = {n: emoji_map[n] for n in emoji_names}

    sticker_map: dict[str, "discord.GuildSticker"] = {}
    for s in sorted(guild.stickers, key=lambda x: x.name.lower()):
        if not s.available:
            continue
        if len(sticker_map) >= STICKER_LIST_LIMIT:
            break
        sticker_map.setdefault(s.name, s)

    return emoji_names, emoji_map, sticker_map


def render_custom_emojis(text: str, emoji_map: dict[str, tuple[int, bool]]) -> str:
    """Replace `:name:` with `<:name:id>` (or `<a:name:id>`) for known emojis."""
    if not text or not emoji_map:
        return text

    def repl(m: re.Match) -> str:
        name = m.group(1)
        entry = emoji_map.get(name)
        if entry is None:
            return m.group(0)  # leave unknown names alone
        emoji_id, animated = entry
        prefix = "a" if animated else ""
        return f"<{prefix}:{name}:{emoji_id}>"

    return EMOJI_NAME_RE.sub(repl, text)


STICKER_TOOL = {
    "name": "send_sticker",
    "description": (
        "Post a server sticker as part of your reply. Use SPARINGLY — only "
        "when a sticker really fits the moment better than an emoji or GIF. "
        "Pick the sticker by EXACT name from the list provided in the system "
        "prompt. If you pick a name not in the list, the call fails silently "
        "and nothing posts. Up to 3 stickers can be attached per reply, but "
        "almost always you should send 0 or 1."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Exact sticker name from the list in the system prompt.",
            }
        },
        "required": ["name"],
    },
}


# ---------- Globals ----------

claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY, max_retries=4)
api_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True       # required for voice playback (/play, etc.)
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

history: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_TURNS * 2))
last_input_tokens: dict[str, int] = {}
last_touched: dict[str, float] = {}
last_typing: dict[int, float] = {}
rage: dict[str, float] = defaultdict(float)

# (user_id, conversation_key) -> sliding window of timestamps + last touch
user_msg_times: dict[tuple[int, str], deque[float]] = defaultdict(
    lambda: deque(maxlen=USER_RATE_LIMIT * 2)
)
user_last_touch: dict[tuple[int, str], float] = {}
user_last_notice: dict[tuple[int, str], float] = {}

# Settings
auto_channels: dict[int, set[int]] = {}   # guild_id -> {channel_ids}
guild_moods: dict[int, str] = {}          # guild_id -> mood
convo_moods: dict[str, str] = {}          # conversation_key -> mood
convo_overrides_touched: dict[str, float] = {}  # last time a convo override was used
dj_roles: dict[int, int] = {}             # guild_id -> role_id (music control gate)
guild_autoplay: dict[int, bool] = {}      # guild_id -> autoplay on/off (default on)

cleanup_runs = 0


# ---------- Persistence ----------

def load_config():
    global auto_channels, guild_moods, convo_moods, convo_overrides_touched, dj_roles
    global guild_autoplay
    if not CONFIG_PATH.exists():
        return
    try:
        data = json.loads(CONFIG_PATH.read_text())
        # Backward compat: old format stored a single int per guild.
        raw = data.get("auto_channels", {})
        auto_channels = {}
        for g, v in raw.items():
            if isinstance(v, list):
                auto_channels[int(g)] = {int(c) for c in v}
            else:
                auto_channels[int(g)] = {int(v)}
        guild_moods   = {int(g): str(m) for g, m in data.get("guild_moods", {}).items()}
        convo_moods   = {str(k): str(m) for k, m in data.get("convo_moods", {}).items()}
        convo_overrides_touched = {
            str(k): float(t) for k, t in data.get("convo_overrides_touched", {}).items()
        }
        dj_roles      = {int(g): int(r) for g, r in data.get("dj_roles", {}).items()}
        guild_autoplay = {int(g): bool(v) for g, v in data.get("guild_autoplay", {}).items()}
    except Exception as e:
        print(f"Failed to load config: {e}")


def save_config():
    try:
        # Rotate one backup before overwriting, in case the new write is bad.
        if CONFIG_PATH.exists():
            try:
                CONFIG_BACKUP_PATH.write_bytes(CONFIG_PATH.read_bytes())
            except Exception as e:
                print(f"Backup rotation failed (non-fatal): {e}")
        CONFIG_PATH.write_text(json.dumps({
            "auto_channels": {str(g): sorted(c) for g, c in auto_channels.items()},
            "guild_moods":   {str(g): m for g, m in guild_moods.items()},
            "convo_moods":   convo_moods,
            "convo_overrides_touched": convo_overrides_touched,
            "dj_roles":      {str(g): r for g, r in dj_roles.items()},
            "guild_autoplay": {str(g): v for g, v in guild_autoplay.items()},
        }, indent=2))
    except Exception as e:
        print(f"Failed to save config: {e}")


# ---------- Conversation-memory persistence ----------
#
# Working memory (history / rage / last_input_tokens / last_touched) is the
# bot's "what we've talked about" state. Without persistence it resets on
# every restart, so users feel like they're talking to a goldfish. We save it
# to memory.json:
#
#   * Atomic write (tmp file + rename) so a half-written file can't corrupt
#     the live one.
#   * Backup rotation (.bak) so a single bad save doesn't wipe everything.
#   * Debounced — mark dirty on each change, flush at most every
#     MEMORY_SAVE_INTERVAL seconds via memory_save_loop.
#   * Image content blocks are stripped on serialize (Discord CDN URLs
#     expire after ~24h, persisting them is fragile). The text portion of
#     each turn is preserved verbatim, including any "(sent an image)"
#     placeholder.

_memory_dirty = False
_memory_loaded = False
_memory_save_lock = asyncio.Lock()


def mark_memory_dirty() -> None:
    """Flag that in-memory state has changed; the save loop will flush soon."""
    global _memory_dirty
    _memory_dirty = True


def _serialize_turn(turn: dict) -> dict | None:
    """Return a JSON-safe copy of a history turn, dropping image blocks."""
    if not isinstance(turn, dict):
        return None
    role = turn.get("role")
    content = turn.get("content")
    if role not in ("user", "assistant"):
        return None

    if isinstance(content, str):
        return {"role": role, "content": content}

    if isinstance(content, list):
        kept: list = []
        had_image = False
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "image":
                had_image = True
                continue
            if btype == "text":
                txt = block.get("text", "")
                if isinstance(txt, str):
                    kept.append({"type": "text", "text": txt})
        if not kept:
            placeholder = "(image only — image not retained across restarts)" if had_image else "(empty)"
            kept = [{"type": "text", "text": placeholder}]
        return {"role": role, "content": kept}

    return None


def save_memory() -> None:
    """Synchronous JSON dump of conversation memory. Safe to call from executor."""
    global _memory_dirty
    try:
        history_payload: dict[str, list] = {}
        for k, dq in history.items():
            turns = [t for t in (_serialize_turn(t) for t in dq) if t is not None]
            if turns:
                history_payload[k] = turns

        payload = {
            "version": 1,
            "saved_at": time.time(),
            "history": history_payload,
            "rage": {k: float(v) for k, v in rage.items()},
            "last_input_tokens": {k: int(v) for k, v in last_input_tokens.items()},
            "last_touched": {k: float(v) for k, v in last_touched.items()},
        }

        # Atomic write: dump to .tmp, then replace target. If anything fails
        # mid-dump, the live file stays intact.
        MEMORY_TMP_PATH.write_text(json.dumps(payload), encoding="utf-8")

        # Rotate backup BEFORE overwriting the live file.
        if MEMORY_PATH.exists():
            try:
                MEMORY_BACKUP_PATH.write_bytes(MEMORY_PATH.read_bytes())
            except Exception as e:
                print(f"[memory] backup rotation failed (non-fatal): {e}")

        MEMORY_TMP_PATH.replace(MEMORY_PATH)
        _memory_dirty = False
    except Exception as e:
        print(f"[memory] save failed: {e}")
        # Best-effort cleanup of the temp file.
        try:
            if MEMORY_TMP_PATH.exists():
                MEMORY_TMP_PATH.unlink()
        except Exception:
            pass


def load_memory() -> None:
    """Restore conversation memory from disk. Idempotent — no-op after first load."""
    global _memory_loaded
    if _memory_loaded:
        return
    _memory_loaded = True

    paths = [p for p in (MEMORY_PATH, MEMORY_BACKUP_PATH) if p.exists()]
    if not paths:
        print("[memory] no saved memory found, starting fresh")
        return

    data = None
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            break
        except Exception as e:
            print(f"[memory] failed to load {p.name}: {e}")
    if data is None:
        print("[memory] all saved memory files are corrupt — starting fresh")
        return

    try:
        # Pre-filter by TTL: anything whose last_touched is older than
        # CONVERSATION_TTL would be evicted by the cleanup loop within an
        # hour anyway. Drop those at load time so the bot never briefly
        # acts like it remembers stale context after a long shutdown.
        now = time.time()
        cutoff = now - CONVERSATION_TTL
        raw_touched = data.get("last_touched") or {}
        stale_keys: set[str] = set()
        for k, v in raw_touched.items():
            try:
                if float(v) < cutoff:
                    stale_keys.add(k)
            except (TypeError, ValueError):
                pass

        loaded_convos = 0
        skipped_stale = 0
        for k, turns in (data.get("history") or {}).items():
            if not isinstance(turns, list):
                continue
            if k in stale_keys:
                skipped_stale += 1
                continue
            dq: deque = deque(maxlen=MAX_TURNS * 2)
            for turn in turns:
                clean = _serialize_turn(turn)
                if clean is not None:
                    dq.append(clean)
            # Trim leading assistant messages (Claude requires user-first).
            while dq and dq[0]["role"] != "user":
                dq.popleft()
            if dq:
                history[k] = dq
                loaded_convos += 1

        for k, v in (data.get("rage") or {}).items():
            if k in stale_keys:
                continue
            try:
                rage[k] = max(0.0, min(RAGE_MAX, float(v)))
            except (TypeError, ValueError):
                pass

        for k, v in (data.get("last_input_tokens") or {}).items():
            if k in stale_keys:
                continue
            try:
                last_input_tokens[k] = int(v)
            except (TypeError, ValueError):
                pass

        for k, v in raw_touched.items():
            if k in stale_keys:
                continue
            try:
                last_touched[k] = float(v)
            except (TypeError, ValueError):
                pass

        # Anything we dropped here means the saved file is now out-of-date —
        # mark dirty so the next periodic save reflects the trim.
        if skipped_stale or stale_keys:
            mark_memory_dirty()

        saved_at = data.get("saved_at")
        age_str = f", file_age={int(now - saved_at)}s" if isinstance(saved_at, (int, float)) else ""
        ttl_h = CONVERSATION_TTL // 3600
        print(
            f"[memory] loaded {loaded_convos} convos, "
            f"{len(rage)} rage entries, {len(last_touched)} timestamps "
            f"(skipped {skipped_stale} stale convos older than {ttl_h}h){age_str}"
        )
    except Exception as e:
        print(f"[memory] partial load error: {e}")


async def memory_save_loop():
    """Periodically flush dirty memory to disk in a background task."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            if _memory_dirty:
                async with _memory_save_lock:
                    # Run the sync JSON write in a thread so we don't block
                    # the event loop on large payloads.
                    await asyncio.get_running_loop().run_in_executor(None, save_memory)
        except Exception as e:
            print(f"[memory] save loop error: {e}")
        await asyncio.sleep(MEMORY_SAVE_INTERVAL)


# ---------- Conversation keying ----------

def conversation_key(source: discord.Message | discord.Interaction) -> str:
    channel = source.channel
    guild = source.guild
    if guild is None:
        return f"dm:{channel.id}"
    if isinstance(channel, discord.Thread):
        return f"thread:{channel.id}"
    return f"channel:{channel.id}"


# ---------- Mood resolution (convo > guild > default) ----------

def resolve_mood(key: str, guild_id: int | None) -> str:
    if key in convo_moods:
        convo_overrides_touched[key] = time.time()
        return convo_moods[key]
    if guild_id is not None and guild_id in guild_moods:
        return guild_moods[guild_id]
    return DEFAULT_MOOD


# ---------- Per-(user, conversation) rate limit ----------

def user_is_rate_limited(user_id: int, key: str) -> bool:
    now = time.time()
    bucket = (user_id, key)
    q = user_msg_times[bucket]
    user_last_touch[bucket] = now
    while q and now - q[0] > USER_RATE_WINDOW:
        q.popleft()
    if len(q) >= USER_RATE_LIMIT:
        return True
    q.append(now)
    return False


def should_notify_cooldown(user_id: int, key: str) -> bool:
    now = time.time()
    bucket = (user_id, key)
    if now - user_last_notice.get(bucket, 0) >= USER_COOLDOWN_NOTICE_INTERVAL:
        user_last_notice[bucket] = now
        return True
    return False


# ---------- History management ----------

def trim_by_count(key: str):
    h = history[key]
    while len(h) > MAX_TURNS * 2:
        h.popleft()
    while h and h[0]["role"] != "user":
        h.popleft()


def trim_by_tokens(key: str):
    h = history[key]
    while last_input_tokens.get(key, 0) > TOKEN_BUDGET and len(h) > MIN_TURNS_AFTER_TRIM:
        h.popleft()
        if h and h[0]["role"] == "assistant":
            h.popleft()
        last_input_tokens[key] = max(0, last_input_tokens[key] - 5000)


async def cleanup_loop():
    global cleanup_runs
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = time.time()

            # Stale conversations (memory + token tracking)
            convo_cutoff = now - CONVERSATION_TTL
            stale_convos = [k for k, t in last_touched.items() if t < convo_cutoff]
            for k in stale_convos:
                history.pop(k, None)
                last_input_tokens.pop(k, None)
                last_touched.pop(k, None)
                rage.pop(k, None)

            # Stale per-user rate-limit buckets
            user_cutoff = now - USER_RATE_TTL
            stale_users = [b for b, t in user_last_touch.items() if t < user_cutoff]
            for b in stale_users:
                user_msg_times.pop(b, None)
                user_last_touch.pop(b, None)
                user_last_notice.pop(b, None)

            # Stale convo mood overrides (persisted dict)
            override_cutoff = now - OVERRIDE_TTL
            stale_overrides = [
                k for k in list(convo_moods.keys())
                if convo_overrides_touched.get(k, 0) < override_cutoff
            ]
            for k in stale_overrides:
                convo_moods.pop(k, None)
                convo_overrides_touched.pop(k, None)
            if stale_overrides:
                save_config()

            if stale_convos:
                mark_memory_dirty()

            cleanup_runs += 1
            if cleanup_runs % CLEANUP_LOG_EVERY == 0 or stale_convos or stale_users or stale_overrides:
                print(
                    f"[cleanup #{cleanup_runs}] "
                    f"convos_evicted={len(stale_convos)} "
                    f"user_buckets_evicted={len(stale_users)} "
                    f"overrides_evicted={len(stale_overrides)} "
                    f"active_convos={len(history)} "
                    f"tracked_user_buckets={len(user_msg_times)} "
                    f"convo_overrides={len(convo_moods)}"
                )
        except Exception as e:
            print(f"Cleanup error: {e}")
        await asyncio.sleep(CLEANUP_INTERVAL)


# ---------- Building user content ----------

async def fetch_reply_context(message: discord.Message) -> str | None:
    ref = message.reference
    if not ref or not ref.message_id:
        return None
    try:
        replied = ref.cached_message or await message.channel.fetch_message(ref.message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    if not replied:
        return None

    author = "you" if replied.author.id == bot.user.id else replied.author.display_name

    snippet = (replied.content or "").strip()
    if not snippet and replied.embeds:
        e = replied.embeds[0]
        snippet = (e.description or e.title or "").strip()
        if snippet:
            snippet = f"(embed) {snippet}"
    if not snippet and replied.stickers:
        snippet = f"(sticker: {replied.stickers[0].name})"

    if replied.attachments:
        image_atts = [a for a in replied.attachments
                      if a.content_type and a.content_type.startswith("image/")]
        if image_atts:
            names = ", ".join(a.filename for a in image_atts[:3])
            extra = f"(image attachment: {names})"
            snippet = f"{snippet} {extra}".strip() if snippet else extra
        elif not snippet:
            kinds = {(a.content_type or "file").split("/")[0] for a in replied.attachments}
            snippet = f"({', '.join(sorted(kinds))} attachment)"

    if not snippet:
        return None
    if len(snippet) > 280:
        snippet = snippet[:277] + "..."
    return f"[replying to {author}: \"{snippet}\"]"


def _speaker_tag(message: discord.Message) -> str:
    """Build a `[DisplayName (1234567890)]:` prefix for multi-user contexts.

    Empty in DMs. The trailing number is the user's full Discord snowflake ID —
    globally unique, stable across name changes, disambiguates two users
    sharing a display name.
    """
    if message.guild is None:
        return ""
    # Sanitize ] to keep the prefix unambiguous.
    name = message.author.display_name.replace("]", ")")
    return f"[{name} ({message.author.id})]: "


async def build_user_content(message: discord.Message, text: str) -> list:
    blocks: list = []

    reply_ctx = await fetch_reply_context(message)
    speaker = _speaker_tag(message)

    text_parts = []
    if reply_ctx:
        text_parts.append(reply_ctx)

    image_blocks = [
        {"type": "image", "source": {"type": "url", "url": att.url}}
        for att in message.attachments
        if att.content_type and att.content_type.startswith("image/")
    ]

    sticker_note = ""
    if message.stickers:
        names = ", ".join(s.name for s in message.stickers)
        sticker_note = f" *(sent sticker: {names})*"

    if text:
        text_parts.append(f"{speaker}{text}{sticker_note}")
    elif image_blocks:
        text_parts.append(f"{speaker}(sent an image){sticker_note}")
    elif sticker_note:
        text_parts.append(f"{speaker}{sticker_note.strip()}")
    else:
        text_parts.append(f"{speaker}(no text)")

    blocks.append({"type": "text", "text": "\n".join(text_parts)})
    blocks.extend(image_blocks)

    return blocks


# ---------- Claude call ----------

async def ask_claude(
    key: str,
    user_content: list,
    mood: str,
    rage_level: float = 0.0,
    *,
    emoji_names: list[str] | None = None,
    emoji_map: dict[str, tuple[int, bool]] | None = None,
    sticker_map: dict[str, "discord.GuildSticker"] | None = None,
) -> tuple[str, list[bytes], list]:
    history[key].append({"role": "user", "content": user_content})
    last_touched[key] = time.time()
    trim_by_count(key)
    trim_by_tokens(key)

    # Build system prompt + append guild-asset context (per-call, since
    # different conversations have different available emojis/stickers).
    system = system_prompt_for(mood, rage_level)
    if emoji_names:
        system += (
            "\n\nCustom server emojis available right now (use as :name: inline): "
            + ", ".join(emoji_names) + "."
        )
    if sticker_map:
        system += (
            "\n\nServer stickers available right now (post via send_sticker, name=...): "
            + ", ".join(sorted(sticker_map.keys())) + "."
        )

    api_kwargs: dict = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
    }
    tools: list = []
    if GIPHY_API_KEY:
        tools.append(GIF_TOOL)
    if YOUTUBE_API_KEY:
        tools.append(VIDEO_TOOL)
    if sticker_map:
        tools.append(STICKER_TOOL)
    if tools:
        api_kwargs["tools"] = tools

    for attempt in range(2):
        try:
            async with api_semaphore:
                response = await claude.messages.create(
                    messages=list(history[key]), **api_kwargs
                )
        except anthropic.BadRequestError as e:
            msg = str(e).lower()
            if ("context" in msg or "token" in msg or "too long" in msg) and attempt == 0:
                h = history[key]
                while len(h) > MIN_TURNS_AFTER_TRIM:
                    h.popleft()
                while h and h[0]["role"] != "user":
                    h.popleft()
                last_input_tokens[key] = 0
                continue
            raise

        if response.usage:
            last_input_tokens[key] = (
                response.usage.input_tokens
                + (getattr(response.usage, "cache_read_input_tokens", 0) or 0)
                + (getattr(response.usage, "cache_creation_input_tokens", 0) or 0)
            )

        text_parts: list[str] = []
        gif_queries: list[str] = []
        gif_blobs: list[bytes] = []
        video_notes: list[str] = []   # for history; like "(youtube: TITLE) URL"
        video_urls: list[str] = []    # appended to outgoing reply for Discord auto-embed
        sticker_notes: list[str] = []
        sticker_objects: list = []    # GuildSticker objects to attach via Discord API
        seen_sticker_ids: set[int] = set()
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use" and block.name == "send_gif":
                query = ((block.input or {}).get("query") or "").strip()
                if not query:
                    continue
                gif_queries.append(query)
                url = await search_giphy(query)
                if not url:
                    print(f"[gif] no result for query={query!r}")
                    continue
                blob = await fetch_gif_bytes(url)
                if blob:
                    gif_blobs.append(blob)
                    print(f"[gif] posting query={query!r} ({len(blob)} bytes)")
            elif block.type == "tool_use" and block.name == "send_video":
                query = ((block.input or {}).get("query") or "").strip()
                if not query:
                    continue
                result = await search_youtube(query)
                if not result:
                    print(f"[youtube] no result for query={query!r}")
                    continue
                url, title = result
                video_urls.append(url)
                video_notes.append(f"*[posted YouTube video: {title} — {url}]*")
                print(f"[youtube] posting query={query!r} -> {url}")
            elif block.type == "tool_use" and block.name == "send_sticker":
                if not sticker_map:
                    continue
                if len(sticker_objects) >= MAX_STICKERS_PER_MESSAGE:
                    print(f"[sticker] hit per-message cap of {MAX_STICKERS_PER_MESSAGE}, skipping")
                    continue
                name = ((block.input or {}).get("name") or "").strip()
                if not name:
                    continue
                obj = sticker_map.get(name)
                if obj is None:
                    print(f"[sticker] unknown name={name!r}, skipping")
                    continue
                if obj.id in seen_sticker_ids:
                    continue  # don't post the same sticker twice
                seen_sticker_ids.add(obj.id)
                sticker_objects.append(obj)
                sticker_notes.append(f"*[posted sticker: {name}]*")
                print(f"[sticker] posting name={name!r} id={obj.id}")

        text = "".join(text_parts).strip()

        # Store text-only history (preserves user/assistant alternation across turns).
        # Note any GIFs/videos/stickers inline so Claude sees them in subsequent context.
        notes = []
        if gif_queries:
            notes.extend(f"*[posted GIF: {q}]*" for q in gif_queries)
        if video_notes:
            notes.extend(video_notes)
        if sticker_notes:
            notes.extend(sticker_notes)
        # Use the un-rendered :name: form in history — it's what Claude wrote
        # and keeps the prompt cheap on subsequent turns.
        if notes:
            note_str = " ".join(notes)
            history_text = f"{text}\n\n{note_str}".strip() if text else note_str
        else:
            history_text = text
        history[key].append({"role": "assistant", "content": history_text or "(no response)"})
        last_touched[key] = time.time()
        mark_memory_dirty()

        if response.stop_reason == "max_tokens" and text:
            text += "\n\n*(...cut off, hit token limit)*"

        # Render :emoji_name: -> <:name:id> for known server emojis.
        if emoji_map:
            text = render_custom_emojis(text, emoji_map)

        # Append YouTube URLs to the outgoing text so Discord auto-embeds them.
        # Putting them on their own lines keeps embeds clean.
        if video_urls:
            url_block = "\n".join(video_urls)
            text = f"{text}\n\n{url_block}".strip() if text else url_block

        if not text and not gif_blobs and not sticker_objects:
            text = "(I got nothing for you)"
        return text, gif_blobs, sticker_objects

    return "(retry exhausted)", [], []


# ---------- Smart chunking ----------

def smart_chunk(text: str, size: int = DISCORD_CHUNK_TARGET) -> list[str]:
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    remaining = text

    while len(remaining) > size:
        cut = remaining.rfind("\n\n", 0, size)
        if cut == -1 or cut < size // 2:
            cut = remaining.rfind("\n", 0, size)
        if cut == -1 or cut < size // 2:
            m = list(re.finditer(r"[.!?]\s+", remaining[:size]))
            cut = m[-1].end() if m else -1
        if cut == -1 or cut < size // 2:
            cut = remaining.rfind(" ", 0, size)
        if cut == -1 or cut == 0:
            cut = size

        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks


# ---------- Discord message handler ----------

async def maybe_typing(channel):
    now = time.time()
    if now - last_typing.get(channel.id, 0) < TYPING_COOLDOWN:
        class _Noop:
            async def __aenter__(self_): return self_
            async def __aexit__(self_, *a): return False
        return _Noop()
    last_typing[channel.id] = now
    return channel.typing()


async def handle_chat(message: discord.Message, content: str):
    key = conversation_key(message)
    guild_id = message.guild.id if message.guild else None
    mood = resolve_mood(key, guild_id)

    if user_is_rate_limited(message.author.id, key):
        notify = should_notify_cooldown(message.author.id, key)
        print(
            f"[ratelimit] user={message.author} ({message.author.id}) "
            f"key={key} notified={notify}"
        )
        if notify:
            try:
                await message.reply(cooldown_message(mood, message.author.display_name))
            except discord.HTTPException:
                pass
        return

    user_content = await build_user_content(message, content)

    # Only run the rage meter when it actually affects the prompt.
    if mood == "escalating":
        prev_text = previous_user_text(key)
        rage_level = update_rage(key, content, prev_text)
    else:
        rage_level = 0.0

    # Collect server emojis & stickers (empty in DMs).
    emoji_names, emoji_map, sticker_map = gather_guild_assets(message)

    typing_ctx = await maybe_typing(message.channel)
    async with typing_ctx:
        try:
            reply, gif_blobs, sticker_objects = await ask_claude(
                key, user_content, mood, rage_level,
                emoji_names=emoji_names,
                emoji_map=emoji_map,
                sticker_map=sticker_map,
            )
        except anthropic.RateLimitError as e:
            retry_after = "?"
            if e.response is not None:
                retry_after = e.response.headers.get("retry-after", "?")
            await message.reply(f"Slow down, asshole. Rate limited — try again in {retry_after}s.")
            return
        except anthropic.APIStatusError as e:
            if e.status_code == 529:
                await message.reply("API's overloaded. Not my problem. Try again in a sec.")
            elif e.status_code == 401:
                await message.reply("API key is fucked. Tell whoever runs me to fix it.")
            elif 500 <= e.status_code < 600:
                # Transient — SDK already retried max_retries times before raising
                await message.reply(
                    "Anthropic's API is having a moment. Try again in a few seconds."
                )
                print(f"[anthropic 5xx] status={e.status_code} req={getattr(e, 'request_id', '?')} msg={e.message}")
            elif 400 <= e.status_code < 500:
                await message.reply(f"Request was bad: {e.message}")
            else:
                await message.reply(f"API error ({e.status_code}): {e.message}")
            return
        except anthropic.APIConnectionError:
            await message.reply("Can't reach the API. Network's being a bitch.")
            return
        except Exception as e:
            await message.reply(f"Something exploded: {type(e).__name__}: {e}")
            return

    parts = smart_chunk(reply) if reply else []
    files = [
        discord.File(io.BytesIO(blob), filename=f"reaction{i + 1}.gif")
        for i, blob in enumerate(gif_blobs)
    ]
    # Discord caps stickers at 3 per message; we already enforced that upstream.
    extras = {}
    if files:
        extras["files"] = files
    if sticker_objects:
        extras["stickers"] = sticker_objects[:MAX_STICKERS_PER_MESSAGE]
    try:
        if not parts and not files and not sticker_objects:
            await message.reply("(I got nothing for you)")
        elif not parts:
            # Stickers/files only — empty content is OK as long as one is present.
            await message.reply(**extras)
        elif len(parts) == 1:
            await message.reply(parts[0], **extras)
        else:
            await message.reply(parts[0])
            for part in parts[1:-1]:
                await message.channel.send(part)
            await message.channel.send(parts[-1], **extras)
    except discord.HTTPException as e:
        try:
            await message.channel.send(f"Discord choked on the reply: {e}")
        except discord.HTTPException:
            pass


# ---------- Discord events ----------

@bot.event
async def on_ready():
    load_config()
    load_memory()
    try:
        synced = await tree.sync()
        synced_n = len(synced)
    except Exception as e:
        synced_n = -1
        print(f"Slash command sync failed: {e}")
    bot.loop.create_task(cleanup_loop())
    bot.loop.create_task(memory_save_loop())
    print("=" * 60)
    print(f"  ONLINE: {bot.user}  (id={bot.user.id})")
    print(f"  model={MODEL}  default_mood={DEFAULT_MOOD}  max_tokens={MAX_TOKENS}")
    print(f"  guilds={len(bot.guilds)}  slash_commands_synced={synced_n}")
    total_auto = sum(len(v) for v in auto_channels.values())
    print(f"  auto_channels: {len(auto_channels)} guilds, {total_auto} channels  "
          f"guild_moods={len(guild_moods)}  convo_moods={len(convo_moods)}")
    print(f"  rate_limit={USER_RATE_LIMIT}/{int(USER_RATE_WINDOW)}s per user/convo  "
          f"max_concurrent={MAX_CONCURRENT}")
    print(f"  memory: {len(history)} convos restored, "
          f"persistence on (every {int(MEMORY_SAVE_INTERVAL)}s)")
    media = []
    if GIPHY_API_KEY: media.append("gifs(giphy)")
    if YOUTUBE_API_KEY: media.append("videos(youtube)")
    media.append("emojis+stickers(per-server)")
    if MUSIC_AVAILABLE:
        sources = ["YouTube", "SoundCloud"]
        if SPOTIFY_AVAILABLE:
            sources.append("Spotify")
        sources.append("AppleMusic")
        media.append(f"music({'/'.join(sources)})")
    print(f"  media tools: {', '.join(media)}")
    if not MUSIC_AVAILABLE:
        missing = []
        if not FFMPEG_AVAILABLE:
            missing.append("ffmpeg(system binary)")
        if not YTDLP_AVAILABLE:
            missing.append("yt-dlp(pip)")
        print(f"  music: DISABLED — missing {', '.join(missing)}")
    print("=" * 60)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    is_dm = message.guild is None
    mentioned = bot.user in message.mentions
    is_auto_channel = (
        message.guild is not None
        and message.channel.id in auto_channels.get(message.guild.id, set())
    )

    is_reply_to_bot = False
    if message.reference and message.reference.resolved and not isinstance(
        message.reference.resolved, discord.DeletedReferencedMessage
    ):
        is_reply_to_bot = message.reference.resolved.author.id == bot.user.id

    if not (is_dm or mentioned or is_auto_channel or is_reply_to_bot):
        return

    content = message.content
    if mentioned:
        content = content.replace(f"<@{bot.user.id}>", "").replace(
            f"<@!{bot.user.id}>", ""
        ).strip()

    has_images = any(
        att.content_type and att.content_type.startswith("image/")
        for att in message.attachments
    )
    has_stickers = bool(message.stickers)
    if not content and not has_images and not has_stickers:
        return

    await handle_chat(message, content)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    """Prune skip/leave votes when listeners leave the bot's channel so a
    quorum can't get stuck after someone walks out mid-vote."""
    if member.bot:
        return
    if before.channel == after.channel:
        return  # mute / deafen / camera toggle — not a join/leave
    guild = member.guild
    if guild is None:
        return
    music = guild_music.get(guild.id)
    if music is None or music.voice is None or not music.voice.is_connected():
        return
    if before.channel != music.voice.channel and after.channel != music.voice.channel:
        return  # state change in some unrelated channel

    listener_ids = {m.id for m in music.voice_humans()}
    music.skip_votes &= listener_ids
    music.leave_votes &= listener_ids

    if not listener_ids:
        # Everyone left — pause and arm the idle-disconnect so we don't sit
        # paused in an empty channel forever.
        if music.auto_pause_if_empty():
            music._schedule_idle_disconnect()
    else:
        # Someone's (back) in the channel — resume if we auto-paused, and cancel
        # the empty-channel disconnect timer.
        if music.auto_resume_if_returned():
            music._cancel_idle_disconnect()


# ---------- Music playback (voice + yt-dlp + ffmpeg) ----------

FFMPEG_PATH = shutil.which("ffmpeg")
FFMPEG_AVAILABLE = FFMPEG_PATH is not None
MUSIC_AVAILABLE = FFMPEG_AVAILABLE and YTDLP_AVAILABLE

MUSIC_IDLE_TIMEOUT = 5 * 60       # disconnect after this many seconds of nothing playing
MUSIC_MAX_QUEUE = 100             # cap per guild
MUSIC_SEARCH_TIMEOUT = 15         # yt-dlp resolution timeout (seconds)
MUSIC_EMBED_COLOR = 0xED4245      # Vivid red — distinct from chat embeds
MUSIC_PROGRESS_WIDTH = 18         # progress-bar character width
PROGRESS_UPDATE_INTERVAL = 8      # seconds between live progress-bar message edits
PLAYLIST_MAX = 50                 # cap tracks pulled from one playlist/album

YTDL_OPTS = {
    "format": "bestaudio[acodec=opus]/bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "noplaylist": True,
    "extract_flat": False,
    "skip_download": True,
    # Prefer YouTube clients that hand back directly-fetchable stream URLs.
    # The default rotation sometimes lands on android_vr, whose URLs 403 when
    # ffmpeg fetches them with a mismatched User-Agent. These are sturdier.
    "extractor_args": {"youtube": {"player_client": ["ios", "web_safari", "mweb", "tv"]}},
}

# Fast, shallow extraction for playlists/sets — pulls the entry list without
# resolving each track's stream URL (that happens lazily, just before play).
YTDL_FLAT_OPTS = {
    **YTDL_OPTS,
    "noplaylist": False,
    "extract_flat": "in_playlist",
}

# -nostdin keeps ffmpeg from grabbing the bot's stdin and racing other input.
# Reconnect flags help with intermittent stream drops on long tracks.
FFMPEG_BEFORE_OPTS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
)
FFMPEG_OPTS = "-vn -loglevel warning"

# A track that dies in under this many seconds (with a much longer duration) is
# treated as a failed stream and re-resolved once. Covers 403s / expired URLs.
MUSIC_EARLY_DEATH_SECONDS = 8


def _ffmpeg_before_options(track: "Track") -> str:
    """Base reconnect flags + the HTTP headers yt-dlp wants for this stream.

    YouTube 403s the stream URL when ffmpeg's User-Agent doesn't match the
    client that extracted it, so we forward yt-dlp's headers to ffmpeg.
    """
    parts = [FFMPEG_BEFORE_OPTS]
    headers = track.http_headers or {}
    ua = headers.get("User-Agent") or headers.get("user-agent")
    if ua:
        parts.append(f'-user_agent "{ua}"')
    extra = [f"{k}: {v}" for k, v in headers.items() if k.lower() != "user-agent"]
    if extra:
        blob = "".join(h + "\\r\\n" for h in extra)
        parts.append(f'-headers "{blob}"')
    return " ".join(parts)


@dataclass
class Track:
    # None for "lazy" tracks (from playlists) — stream_url is filled in by
    # resolve_query just before the track plays. Avoids resolving 50 stream
    # URLs up front (slow) and dodges YouTube URL expiry on long queues.
    stream_url: str | None
    webpage_url: str
    title: str
    duration: int | None
    requester_id: int
    requester_name: str
    thumbnail_url: str | None = None
    uploader: str | None = None        # e.g. "Rick Astley" — YouTube channel name
    source_label: str = "YouTube"      # for embed attribution (Spotify, Apple Music, SoundCloud)
    resolve_query: str | None = None   # lazy tracks: query/URL to resolve at play time
    http_headers: dict | None = None   # headers yt-dlp says to send when fetching the stream
    _retry_count: int = 0              # fresh-resolution retries used (403/early-death recovery)

    @property
    def is_resolved(self) -> bool:
        return self.stream_url is not None

    def duration_str(self) -> str:
        if not self.duration:
            return ""
        m, s = divmod(int(self.duration), 60)
        if m >= 60:
            h, m = divmod(m, 60)
            return f" ({h}:{m:02d}:{s:02d})"
        return f" ({m}:{s:02d})"

    def display(self) -> str:
        return f"[{self.title}]({self.webpage_url}){self.duration_str()}"


class GuildMusic:
    """Per-guild voice state: queue, current track, idle disconnect, etc."""

    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.voice: discord.VoiceClient | None = None
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.last_text_channel_id: int | None = None
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None
        # Playback timer for the current track. _started_at is set when the
        # track (re)starts playing and cleared on pause; _accumulated holds
        # elapsed time accrued before pauses so resume picks up cleanly.
        self._started_at: float | None = None
        self._accumulated: float = 0.0
        # Playback modes
        self.loop_mode: str = "off"        # "off" | "track" | "queue"
        self.volume: float = 1.0           # PCMVolumeTransformer multiplier, 0.0-2.0
        # Set by skip()/jump() so the next _advance bypasses loop_mode for that
        # single transition (user explicitly wants to move forward, not loop).
        self._force_advance: bool = False
        # Live progress bar: the now-playing message + the task editing it.
        self._now_playing_msg: discord.Message | None = None
        self._progress_task: asyncio.Task | None = None
        # Democratic control: user IDs who've voted to skip / make the bot leave.
        # skip_votes reset on track change; both pruned to current listeners.
        self.skip_votes: set[int] = set()
        self.leave_votes: set[int] = set()
        # True when playback was auto-paused because the channel emptied out, so
        # we know to auto-resume (and not clobber a manual pause) when it refills.
        self._auto_paused: bool = False
        # Set right before an intentional voice.stop() (skip/restart/stop/jump)
        # so _after_play knows the early end was deliberate, not a failed stream.
        self._expect_stop: bool = False
        # Autoplay (radio): seed off the last track that played, and remember
        # recent video IDs so the radio doesn't loop the same handful of songs.
        self._last_played: Track | None = None
        self._recent_ids: deque[str] = deque(maxlen=80)
        # Set by stop/leave so the resulting _advance doesn't autoplay a new
        # track (which would undo the stop). Consumed on the next _advance.
        self._stopping: bool = False

    # ---- state queries ----

    def is_playing(self) -> bool:
        return self.voice is not None and self.voice.is_playing()

    def is_paused(self) -> bool:
        return self.voice is not None and self.voice.is_paused()

    def is_active(self) -> bool:
        return self.is_playing() or self.is_paused()

    # ---- playback timer ----

    def elapsed(self) -> float:
        """Best-guess seconds elapsed in the current track."""
        if self._started_at is None:
            return self._accumulated
        return self._accumulated + (time.time() - self._started_at)

    def _start_timer(self) -> None:
        self._started_at = time.time()
        self._accumulated = 0.0

    def _pause_timer(self) -> None:
        if self._started_at is not None:
            self._accumulated += time.time() - self._started_at
            self._started_at = None

    def _resume_timer(self) -> None:
        if self._started_at is None:
            self._started_at = time.time()

    # ---- pause / resume ----

    def pause(self) -> bool:
        if not self.is_playing():
            return False
        self.voice.pause()
        self._pause_timer()
        self._auto_paused = False  # explicit user pause overrides auto-pause state
        return True

    def resume(self) -> bool:
        if not self.is_paused():
            return False
        self.voice.resume()
        self._resume_timer()
        self._auto_paused = False
        return True

    # ---- listener helpers (for vote / solo control) ----

    def voice_humans(self) -> list:
        """Non-bot members currently in the bot's voice channel."""
        if not self.voice or not self.voice.channel:
            return []
        return [m for m in self.voice.channel.members if not m.bot]

    def is_alone_with(self, member) -> bool:
        """True if `member` is the only human in the bot's voice channel."""
        humans = self.voice_humans()
        return len(humans) == 1 and humans[0].id == member.id

    def auto_pause_if_empty(self) -> bool:
        """Pause playback when no humans remain in the channel. Returns True if
        it just auto-paused. Leaves a manually-paused track alone."""
        if self.voice_humans():
            return False
        if self.is_playing():
            self.voice.pause()
            self._pause_timer()
            self._auto_paused = True
            print(f"[music guild={self.guild_id}] channel empty — auto-paused")
            return True
        return False

    def auto_resume_if_returned(self) -> bool:
        """Resume a track that we auto-paused, now that a human is back. Returns
        True if it just resumed. No-op for manual pauses."""
        if not self._auto_paused:
            return False
        if not self.voice_humans():
            return False
        self._auto_paused = False
        if self.is_paused():
            self.voice.resume()
            self._resume_timer()
            print(f"[music guild={self.guild_id}] listener returned — auto-resumed")
            return True
        return False

    # ---- voice connection ----

    async def ensure_voice(self, channel: discord.abc.Connectable) -> None:
        if self.voice is None or not self.voice.is_connected():
            self.voice = await channel.connect(self_deaf=True, reconnect=True)
        elif self.voice.channel != channel:
            await self.voice.move_to(channel)

    # ---- queue ops ----

    async def enqueue(self, track: Track) -> int:
        """Append to queue. Returns 1-indexed position in the playback sequence
        (1 means it will start playing immediately)."""
        async with self._lock:
            self.queue.append(track)
            position = len(self.queue) + (1 if self.current else 0)
        if not self.is_active() and self.current is None:
            # Kickoff path: caller (the /play command) already tells the user
            # what's playing, so suppress the auto-announce to avoid a double
            # message. Subsequent advances triggered by _after_play DO announce.
            await self._advance(announce=False)
        return position

    async def enqueue_many(self, tracks: list[Track]) -> int:
        """Bulk-append (playlist). Starts playback in the background if idle so
        the caller can respond immediately. Returns how many were queued."""
        added = 0
        async with self._lock:
            for t in tracks:
                if len(self.queue) >= MUSIC_MAX_QUEUE:
                    break
                self.queue.append(t)
                added += 1
        if added and not self.is_active() and self.current is None:
            # Background so the /play command can post its "queued N" summary
            # without waiting on the first track's lazy resolution.
            bot.loop.create_task(self._advance(announce=True))
        return added

    async def _resolve_lazy(self, track: Track) -> bool:
        """Fill in a lazy track's stream_url (and any missing metadata) via its
        resolve_query. Returns True on success."""
        if track.is_resolved:
            return True
        if not track.resolve_query:
            return False
        resolved = await resolve_track(
            track.resolve_query, track.requester_id, track.requester_name
        )
        if resolved is None or not resolved.stream_url:
            return False
        track.stream_url = resolved.stream_url
        # Prefer the real resolved URL when the lazy webpage_url was a placeholder
        # (e.g. a Spotify page we can't play) or a bare search query.
        if track.resolve_query.startswith("ytsearch") or not track.webpage_url:
            track.webpage_url = resolved.webpage_url
        track.duration = track.duration or resolved.duration
        track.thumbnail_url = track.thumbnail_url or resolved.thumbnail_url
        track.uploader = track.uploader or resolved.uploader
        track.http_headers = resolved.http_headers
        if not track.title or track.title == "(untitled)":
            track.title = resolved.title
        return True

    async def _try_autoplay(self) -> Track | None:
        """When the queue empties, fetch a related track to keep playing — if
        autoplay is enabled for this guild and we have a seed to work from."""
        if not guild_autoplay.get(self.guild_id, True):
            return None
        seed = self._last_played
        if seed is None:
            return None
        try:
            return await fetch_autoplay_track(
                seed, set(self._recent_ids),
                requester_id=bot.user.id if bot.user else 0,
                requester_name="Autoplay",
            )
        except Exception as e:
            print(f"[music guild={self.guild_id}] autoplay fetch failed: {e}")
            return None

    async def _advance(self, announce: bool = True) -> None:
        bypass_loop = self._force_advance
        self._force_advance = False

        # Decide which track to play next. With loop=track, the same track
        # replays. With loop=queue, the just-played track goes back to the
        # end of the queue. bypass_loop (from skip/jump) ignores both.
        is_replay = (
            not bypass_loop
            and self.loop_mode == "track"
            and self.current is not None
        )

        async with self._lock:
            if is_replay:
                next_track = self.current
            else:
                if (
                    not bypass_loop
                    and self.loop_mode == "queue"
                    and self.current is not None
                ):
                    self.queue.append(self.current)
                queue_empty = not self.queue
                if not queue_empty:
                    next_track = self.queue.popleft()
                    self.current = next_track
                    self.skip_votes.clear()  # fresh track → fresh skip vote

        # Queue ran dry. Try autoplay (a related track) before going idle —
        # unless we got here via an explicit /stop or /leave.
        if not is_replay and queue_empty:
            if not self._stopping:
                auto = await self._try_autoplay()
                if auto is not None:
                    async with self._lock:
                        self.queue.append(auto)
                    await self._advance(announce=announce)
                    return
            self._stopping = False
            self.current = None
            self._cancel_progress_task()
            self._schedule_idle_disconnect()
            return

        if self.voice is None or not self.voice.is_connected():
            self.current = None
            return

        # Lazy tracks (from playlists) resolve their stream URL here, just in
        # time. On failure, drop it and advance to the next entry.
        if not next_track.is_resolved:
            ok = await self._resolve_lazy(next_track)
            if not ok:
                print(f"[music guild={self.guild_id}] could not resolve "
                      f"{next_track.resolve_query!r}, skipping")
                # Only advance if nothing else moved on in the meantime. Clear
                # current first so loop=track can't infinitely retry a dead entry.
                if self.current is next_track:
                    self.current = None
                    asyncio.create_task(self._advance(announce=announce))
                return

        try:
            source = discord.FFmpegPCMAudio(
                next_track.stream_url,
                executable=FFMPEG_PATH or "ffmpeg",
                before_options=_ffmpeg_before_options(next_track),
                options=FFMPEG_OPTS,
            )
            source = discord.PCMVolumeTransformer(source, volume=self.volume)
            self.voice.play(source, after=self._after_play)
        except Exception as e:
            print(f"[music guild={self.guild_id}] play failed: {e}")
            self.current = None
            asyncio.create_task(self._advance())
            return

        self._start_timer()
        self._cancel_idle_disconnect()
        # Remember this as the autoplay seed + mark it recently played.
        self._last_played = next_track
        vid = _youtube_video_id(next_track.webpage_url)
        if vid and vid not in self._recent_ids:
            self._recent_ids.append(vid)
        # Suppress the auto-announce when looping the same track, otherwise the
        # channel fills up with identical embeds.
        if announce and not is_replay:
            await self._announce_now_playing()
        elif is_replay and self._now_playing_msg is not None:
            # Restart the live bar from 0:00 on the existing message.
            self.register_now_playing(self._now_playing_msg)

    def _after_play(self, error: Exception | None) -> None:
        # Called from a non-async thread by discord.py's audio player.
        if error:
            print(f"[music guild={self.guild_id}] ffmpeg error: {error}")

        intentional = self._expect_stop
        self._expect_stop = False
        played = self.elapsed()
        track = self.current

        # A stream that dies almost immediately (and wasn't a user skip/stop) is
        # almost always a 403 / expired URL. Re-resolve fresh and retry once.
        if (
            not intentional
            and track is not None
            and track.duration and track.duration > MUSIC_EARLY_DEATH_SECONDS * 2
            and played < MUSIC_EARLY_DEATH_SECONDS
            and track._retry_count < 1
        ):
            print(f"[music guild={self.guild_id}] '{track.title}' died after "
                  f"{played:.1f}s — re-resolving and retrying")
            track._retry_count += 1
            # Force a fresh resolution next time it's picked up.
            track.stream_url = None
            if not track.resolve_query:
                track.resolve_query = track.webpage_url
            try:
                asyncio.run_coroutine_threadsafe(
                    self._requeue_front_and_advance(track), bot.loop
                )
            except Exception as e:
                print(f"[music guild={self.guild_id}] retry scheduling failed: {e}")
            return

        try:
            asyncio.run_coroutine_threadsafe(self._advance(), bot.loop)
        except Exception as e:
            print(f"[music guild={self.guild_id}] advance scheduling failed: {e}")

    async def _requeue_front_and_advance(self, track: Track) -> None:
        async with self._lock:
            self.queue.appendleft(track)
        # Bypass loop logic for this transition so we replay THIS track, fresh.
        self._force_advance = True
        await self._advance(announce=False)

    async def _announce_now_playing(self) -> None:
        if not self.current or not self.last_text_channel_id:
            return
        channel = bot.get_channel(self.last_text_channel_id)
        if channel is None:
            return
        try:
            # Auto-announce fires the moment a track starts → elapsed ≈ 0.
            msg = await channel.send(
                embed=_track_embed(
                    self.current,
                    "🎵 Now Playing",
                    elapsed=0.0,
                    loop_mode=self.loop_mode,
                    volume_pct=int(round(self.volume * 100)),
                ),
                view=MusicControls(),
            )
        except discord.HTTPException:
            return
        self.register_now_playing(msg)

    # ---- live progress bar ----

    def register_now_playing(self, message: discord.Message) -> None:
        """Track a now-playing message and (re)start the task that edits its
        progress bar every PROGRESS_UPDATE_INTERVAL seconds."""
        self._cancel_progress_task()
        self._now_playing_msg = message
        # Only animate when we know the total duration (skip livestreams).
        if self.current and self.current.duration:
            self._progress_task = bot.loop.create_task(
                self._progress_loop(self.current)
            )

    def _cancel_progress_task(self) -> None:
        if self._progress_task and not self._progress_task.done():
            self._progress_task.cancel()
        self._progress_task = None

    async def _progress_loop(self, track: Track) -> None:
        try:
            while True:
                await asyncio.sleep(PROGRESS_UPDATE_INTERVAL)
                # Stop if the track changed, the message is gone, or playback ended.
                if self.current is not track or self._now_playing_msg is None:
                    return
                if not self.is_active():
                    return
                if self.is_paused():
                    continue  # freeze the bar; resume picks back up
                if track.duration and self.elapsed() >= track.duration:
                    return
                try:
                    await self._now_playing_msg.edit(
                        embed=_track_embed(
                            track,
                            "🎵 Now Playing",
                            elapsed=self.elapsed(),
                            loop_mode=self.loop_mode,
                            volume_pct=int(round(self.volume * 100)),
                        )
                    )
                except discord.HTTPException:
                    return  # message deleted, token expired, etc.
        except asyncio.CancelledError:
            return

    # ---- control ----

    async def skip(self) -> Track | None:
        skipped = self.current
        # User explicitly wants to move forward — bypass loop_mode for this
        # one transition so loop=track doesn't replay the same song.
        self._force_advance = True
        self._expect_stop = True  # deliberate end → don't trigger the retry
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()  # triggers _after_play -> _advance
        return skipped

    async def restart_current(self) -> Track | None:
        """Re-queue the current track at the front, then stop playback so
        _after_play -> _advance picks it back up from the beginning."""
        if self.current is None:
            return None
        track = self.current
        async with self._lock:
            self.queue.appendleft(track)
        self._force_advance = True
        self._expect_stop = True
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()
        return track

    async def stop_and_clear(self) -> None:
        self._cancel_progress_task()
        self._now_playing_msg = None
        self.skip_votes.clear()
        self.leave_votes.clear()
        self._expect_stop = True
        self._stopping = True  # don't let autoplay revive a deliberate stop
        async with self._lock:
            self.queue.clear()
            self.current = None
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()
        self._schedule_idle_disconnect()

    async def leave(self) -> None:
        self._cancel_idle_disconnect()
        self._cancel_progress_task()
        self._now_playing_msg = None
        self.skip_votes.clear()
        self.leave_votes.clear()
        self._expect_stop = True
        self._stopping = True
        async with self._lock:
            self.queue.clear()
            self.current = None
        if self.voice:
            try:
                await self.voice.disconnect(force=False)
            except Exception:
                pass
            self.voice = None

    # ---- playback modes / queue manipulation ----

    def set_loop(self, mode: str) -> str:
        """Set loop mode to one of 'off', 'track', 'queue'. Returns the resolved mode."""
        if mode not in ("off", "track", "queue"):
            mode = "off"
        self.loop_mode = mode
        return mode

    def cycle_loop(self) -> str:
        """Cycle off → track → queue → off. Returns the new mode."""
        nxt = {"off": "track", "track": "queue", "queue": "off"}
        self.loop_mode = nxt.get(self.loop_mode, "off")
        return self.loop_mode

    def set_volume(self, level_pct: int) -> int:
        """Set volume as a percentage (0-200). Applies live to current playback
        if there's an active PCMVolumeTransformer. Returns the clamped value."""
        level_pct = max(0, min(200, int(level_pct)))
        self.volume = level_pct / 100.0
        if self.voice and isinstance(self.voice.source, discord.PCMVolumeTransformer):
            self.voice.source.volume = self.volume
        return level_pct

    async def shuffle(self) -> int:
        """Randomize queue order. Returns count of tracks shuffled."""
        async with self._lock:
            count = len(self.queue)
            if count >= 2:
                items = list(self.queue)
                random.shuffle(items)
                self.queue = deque(items)
        return count

    async def remove_at(self, position: int) -> Track | None:
        """Remove a track at 1-indexed queue position. None if out of range."""
        async with self._lock:
            if position < 1 or position > len(self.queue):
                return None
            items = list(self.queue)
            removed = items.pop(position - 1)
            self.queue = deque(items)
        return removed

    async def clear_queue(self) -> int:
        """Empty the queue but keep the current track playing. Returns count cleared."""
        async with self._lock:
            count = len(self.queue)
            self.queue.clear()
        return count

    async def jump_to(self, position: int) -> Track | None:
        """Skip ahead to the 1-indexed position in the queue, discarding the
        tracks in between. Returns the track that will start playing, or None
        if position is out of range."""
        async with self._lock:
            if position < 1 or position > len(self.queue):
                return None
            for _ in range(position - 1):
                self.queue.popleft()
            target = self.queue[0]
        self._force_advance = True
        self._expect_stop = True
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()
        return target

    # ---- idle disconnect ----

    def _schedule_idle_disconnect(self) -> None:
        self._cancel_idle_disconnect()
        self._idle_task = bot.loop.create_task(self._idle_disconnect())

    def _cancel_idle_disconnect(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    async def _idle_disconnect(self) -> None:
        try:
            await asyncio.sleep(MUSIC_IDLE_TIMEOUT)
        except asyncio.CancelledError:
            return
        # Nothing queued/playing, OR the channel is still empty (e.g. we
        # auto-paused when everyone left and nobody came back) → disconnect.
        if (not self.is_active() and not self.queue) or not self.voice_humans():
            print(f"[music guild={self.guild_id}] idle {MUSIC_IDLE_TIMEOUT}s — disconnecting")
            await self.leave()


guild_music: dict[int, GuildMusic] = {}


def get_or_create_music(guild_id: int) -> GuildMusic:
    if guild_id not in guild_music:
        guild_music[guild_id] = GuildMusic(guild_id)
    return guild_music[guild_id]


# ---- Alternate source resolvers (Spotify, Apple Music) ----
#
# Neither Spotify nor Apple Music exposes audio streams via their public APIs
# (copyright). So when /play receives one of their URLs we read the track
# metadata (title + artist), then search YouTube for a match and play THAT.
# SoundCloud is different — yt-dlp handles SoundCloud URLs natively, so they
# work without any extra logic; we just detect them for embed branding.

SPOTIFY_AVAILABLE = bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET)

SPOTIFY_URL_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-\w+/)?track/|spotify:track:)([a-zA-Z0-9]+)"
)
APPLE_MUSIC_TRACK_ID_RE = re.compile(r"music\.apple\.com/.+[?&]i=(\d+)")
APPLE_MUSIC_SONG_RE = re.compile(r"music\.apple\.com/[^/]+/song/[^/]+/(\d+)")

# Cache the Client Credentials token until just before it expires.
_spotify_token: str | None = None
_spotify_token_expires_at: float = 0.0


def _is_spotify_url(query: str) -> bool:
    q = query.lower()
    return "open.spotify.com" in q or q.startswith("spotify:")


def _is_apple_music_url(query: str) -> bool:
    return "music.apple.com" in query.lower()


def _is_soundcloud_url(query: str) -> bool:
    return "soundcloud.com" in query.lower()


async def _get_spotify_token() -> str | None:
    global _spotify_token, _spotify_token_expires_at
    if not SPOTIFY_AVAILABLE:
        return None
    if _spotify_token and time.time() < _spotify_token_expires_at - 60:
        return _spotify_token
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://accounts.spotify.com/api/token",
                auth=aiohttp.BasicAuth(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
                data={"grant_type": "client_credentials"},
            ) as resp:
                if resp.status != 200:
                    print(f"[spotify] auth HTTP {resp.status}")
                    return None
                data = await resp.json()
    except Exception as e:
        print(f"[spotify] auth failed: {e}")
        return None
    _spotify_token = data.get("access_token")
    _spotify_token_expires_at = time.time() + float(data.get("expires_in", 3600) or 3600)
    return _spotify_token


async def resolve_spotify_track(url: str) -> str | None:
    """Spotify track URL/URI → 'Title Artist' string for a YouTube search.
    Returns None if the URL doesn't match, creds aren't set, or the API fails."""
    match = SPOTIFY_URL_RE.search(url)
    if not match:
        return None
    track_id = match.group(1)
    token = await _get_spotify_token()
    if not token:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"https://api.spotify.com/v1/tracks/{track_id}",
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status != 200:
                    print(f"[spotify] track lookup HTTP {resp.status}")
                    return None
                data = await resp.json()
    except Exception as e:
        print(f"[spotify] track lookup failed: {e}")
        return None
    title = (data.get("name") or "").strip()
    artists = ", ".join(
        a.get("name", "") for a in (data.get("artists") or []) if a.get("name")
    )
    if not title:
        return None
    return f"{title} {artists}".strip()


async def resolve_apple_music_track(url: str) -> str | None:
    """Apple Music track URL → 'Title Artist' string for a YouTube search.
    Uses the free iTunes Search API (no key needed). Album-only URLs (no
    ?i=… track id) are rejected — we don't enqueue whole albums yet."""
    track_id = None
    m = APPLE_MUSIC_TRACK_ID_RE.search(url)
    if m:
        track_id = m.group(1)
    else:
        m = APPLE_MUSIC_SONG_RE.search(url)
        if m:
            track_id = m.group(1)
    if not track_id:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"https://itunes.apple.com/lookup?id={track_id}&entity=song"
            ) as resp:
                if resp.status != 200:
                    print(f"[apple-music] lookup HTTP {resp.status}")
                    return None
                data = await resp.json()
    except Exception as e:
        print(f"[apple-music] lookup failed: {e}")
        return None
    results = data.get("results") or []
    if not results:
        return None
    track = results[0]
    title = (track.get("trackName") or "").strip()
    artist = (track.get("artistName") or "").strip()
    if not title:
        return None
    return f"{title} {artist}".strip()


async def resolve_track(
    query: str, requester_id: int, requester_name: str
) -> Track | None:
    """Resolve a query (URL or search text) into a playable Track.

    Routing:
    - Spotify URL → Spotify API → 'Title Artist' → yt-dlp ytsearch1
    - Apple Music URL → iTunes Search API → 'Title Artist' → yt-dlp ytsearch1
    - SoundCloud URL → yt-dlp direct (native support)
    - YouTube URL → yt-dlp direct
    - Anything else (plain text) → yt-dlp ytsearch1 (set by YTDL_OPTS default_search)
    """
    if not YTDLP_AVAILABLE:
        return None

    yt_query = query
    source_label = "YouTube"

    if _is_spotify_url(query):
        resolved = await resolve_spotify_track(query)
        if not resolved:
            if not SPOTIFY_AVAILABLE:
                print(f"[music] spotify URL given but SPOTIFY_CLIENT_ID/SECRET not set")
            else:
                print(f"[music] spotify resolution failed for {query!r}")
            return None
        yt_query = f"ytsearch1:{resolved}"
        source_label = "Spotify (via YouTube)"
        print(f"[music] spotify → '{resolved}' → YouTube search")
    elif _is_apple_music_url(query):
        resolved = await resolve_apple_music_track(query)
        if not resolved:
            print(f"[music] apple music resolution failed for {query!r} "
                  "(album URLs without ?i= track id aren't supported yet)")
            return None
        yt_query = f"ytsearch1:{resolved}"
        source_label = "Apple Music (via YouTube)"
        print(f"[music] apple music → '{resolved}' → YouTube search")
    elif _is_soundcloud_url(query):
        source_label = "SoundCloud"

    def _extract():
        with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
            return ydl.extract_info(yt_query, download=False)

    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(_extract), timeout=MUSIC_SEARCH_TIMEOUT
        )
    except asyncio.TimeoutError:
        print(f"[music] yt-dlp timeout for query={yt_query!r}")
        return None
    except Exception as e:
        print(f"[music] yt-dlp failed for query={yt_query!r}: {e}")
        return None

    if not info:
        return None
    # Search returns a playlist-shaped dict; pick the first entry.
    if info.get("_type") == "playlist" or "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return None
        info = entries[0]

    stream_url = info.get("url")
    if not stream_url:
        return None

    # yt-dlp returns either a single URL or a "thumbnails" list (sorted ascending
    # by resolution); the last entry is usually highest-res.
    thumb_url = info.get("thumbnail")
    if not thumb_url:
        thumbnails = info.get("thumbnails") or []
        if thumbnails:
            thumb_url = thumbnails[-1].get("url")

    return Track(
        stream_url=stream_url,
        webpage_url=info.get("webpage_url") or info.get("original_url") or query,
        title=info.get("title") or "(untitled)",
        duration=int(info["duration"]) if info.get("duration") else None,
        requester_id=requester_id,
        requester_name=requester_name,
        thumbnail_url=thumb_url,
        uploader=info.get("uploader") or info.get("channel") or info.get("creator"),
        source_label=source_label,
        http_headers=info.get("http_headers"),
    )


# ---- Playlist / album resolution (lazy tracks) ----

SPOTIFY_PLAYLIST_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-\w+/)?playlist/|spotify:playlist:)([a-zA-Z0-9]+)"
)
SPOTIFY_ALBUM_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-\w+/)?album/|spotify:album:)([a-zA-Z0-9]+)"
)
APPLE_ALBUM_RE = re.compile(r"music\.apple\.com/[^/]+/album/[^/]+/(\d+)")


def _is_playlist_url(query: str) -> bool:
    q = query.lower()
    if "open.spotify.com/playlist/" in q or q.startswith("spotify:playlist:"):
        return True
    if "open.spotify.com/album/" in q or q.startswith("spotify:album:"):
        return True
    # Apple Music album/playlist pages — but NOT a single-track link (?i=…).
    if "music.apple.com" in q and ("/album/" in q or "/playlist/" in q):
        if "?i=" not in q and "&i=" not in q:
            return True
    if "soundcloud.com" in q and "/sets/" in q:
        return True
    # Only treat an explicit YouTube playlist page as a playlist; a watch URL
    # that merely carries &list=… still plays the single video.
    if "youtube.com/playlist" in q:
        return True
    return False


async def _resolve_ytdlp_playlist(
    query: str, requester_id: int, requester_name: str
) -> tuple[list[Track], str] | None:
    """YouTube playlist / SoundCloud set → lazy Tracks via flat extraction."""
    is_soundcloud = "soundcloud.com" in query.lower()

    def _extract():
        with yt_dlp.YoutubeDL(YTDL_FLAT_OPTS) as ydl:
            return ydl.extract_info(query, download=False)

    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(_extract), timeout=MUSIC_SEARCH_TIMEOUT * 2
        )
    except Exception as e:
        print(f"[music] playlist extract failed for {query!r}: {e}")
        return None

    if not info:
        return None
    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        return None
    title = info.get("title") or ("SoundCloud set" if is_soundcloud else "playlist")

    tracks: list[Track] = []
    for e in entries[:PLAYLIST_MAX]:
        url = e.get("url") or e.get("webpage_url")
        if not url and e.get("id"):
            url = f"https://www.youtube.com/watch?v={e['id']}"
        if not url:
            continue
        tracks.append(Track(
            stream_url=None,
            webpage_url=e.get("webpage_url") or url,
            title=e.get("title") or "(untitled)",
            duration=int(e["duration"]) if e.get("duration") else None,
            requester_id=requester_id,
            requester_name=requester_name,
            thumbnail_url=e.get("thumbnail"),
            uploader=e.get("uploader") or e.get("channel"),
            source_label="SoundCloud" if is_soundcloud else "YouTube",
            resolve_query=url,
        ))
    return (tracks, title) if tracks else None


async def _resolve_spotify_collection(
    url: str, kind: str, requester_id: int, requester_name: str
) -> tuple[list[Track], str] | None:
    """Spotify playlist or album → lazy Tracks (each a YouTube search)."""
    rx = SPOTIFY_PLAYLIST_RE if kind == "playlist" else SPOTIFY_ALBUM_RE
    m = rx.search(url)
    if not m:
        return None
    cid = m.group(1)
    token = await _get_spotify_token()
    if not token:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"https://api.spotify.com/v1/{kind}s/{cid}",
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status != 200:
                    print(f"[spotify] {kind} lookup HTTP {resp.status}")
                    return None
                data = await resp.json()
    except Exception as e:
        print(f"[spotify] {kind} lookup failed: {e}")
        return None

    name = data.get("name") or f"Spotify {kind}"
    items = (data.get("tracks") or {}).get("items") or []
    tracks: list[Track] = []
    for it in items[:PLAYLIST_MAX]:
        t = it.get("track") if kind == "playlist" else it
        if not t:
            continue
        title = (t.get("name") or "").strip()
        if not title:
            continue
        artists = ", ".join(
            a.get("name", "") for a in (t.get("artists") or []) if a.get("name")
        )
        spotify_url = (t.get("external_urls") or {}).get("spotify") or url
        tracks.append(Track(
            stream_url=None,
            webpage_url=spotify_url,
            title=f"{title} — {artists}" if artists else title,
            duration=int(t["duration_ms"] / 1000) if t.get("duration_ms") else None,
            requester_id=requester_id,
            requester_name=requester_name,
            source_label="Spotify (via YouTube)",
            resolve_query=f"ytsearch1:{title} {artists}".strip(),
        ))
    return (tracks, name) if tracks else None


async def _resolve_apple_album(
    url: str, requester_id: int, requester_name: str
) -> tuple[list[Track], str] | None:
    """Apple Music album → lazy Tracks via the free iTunes lookup API.
    Curated Apple Music playlists aren't in the iTunes API, so those return None."""
    if "/playlist/" in url.lower():
        return None  # unsupported — caller surfaces a clear message
    m = APPLE_ALBUM_RE.search(url)
    if not m:
        return None
    album_id = m.group(1)
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"https://itunes.apple.com/lookup?id={album_id}"
                f"&entity=song&limit={PLAYLIST_MAX + 1}"
            ) as resp:
                if resp.status != 200:
                    print(f"[apple-music] album lookup HTTP {resp.status}")
                    return None
                data = await resp.json()
    except Exception as e:
        print(f"[apple-music] album lookup failed: {e}")
        return None

    results = data.get("results") or []
    if not results:
        return None
    name = "Apple Music album"
    tracks: list[Track] = []
    for r in results:
        if r.get("wrapperType") != "track":
            if r.get("collectionName"):
                name = r["collectionName"]
            continue
        title = (r.get("trackName") or "").strip()
        if not title:
            continue
        artist = (r.get("artistName") or "").strip()
        tracks.append(Track(
            stream_url=None,
            webpage_url=r.get("trackViewUrl") or url,
            title=f"{title} — {artist}" if artist else title,
            duration=int(r["trackTimeMillis"] / 1000) if r.get("trackTimeMillis") else None,
            requester_id=requester_id,
            requester_name=requester_name,
            source_label="Apple Music (via YouTube)",
            resolve_query=f"ytsearch1:{title} {artist}".strip(),
        ))
    return (tracks[:PLAYLIST_MAX], name) if tracks else None


async def resolve_playlist(
    query: str, requester_id: int, requester_name: str
) -> tuple[list[Track], str] | None:
    """Route a playlist/album URL to the right resolver. Returns
    (lazy_tracks, collection_title) or None."""
    if not YTDLP_AVAILABLE:
        return None
    q = query.lower()
    if "open.spotify.com/playlist/" in q or q.startswith("spotify:playlist:"):
        return await _resolve_spotify_collection(query, "playlist", requester_id, requester_name)
    if "open.spotify.com/album/" in q or q.startswith("spotify:album:"):
        return await _resolve_spotify_collection(query, "album", requester_id, requester_name)
    if "music.apple.com" in q:
        return await _resolve_apple_album(query, requester_id, requester_name)
    return await _resolve_ytdlp_playlist(query, requester_id, requester_name)


# ---- Autoplay (radio): keep playing related tracks when the queue empties ----

_YT_ID_RE = re.compile(
    r"(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)"
    r"([A-Za-z0-9_-]{11})"
)


def _youtube_video_id(url: str | None) -> str | None:
    if not url:
        return None
    m = _YT_ID_RE.search(url)
    return m.group(1) if m else None


async def fetch_autoplay_track(
    seed: "Track", exclude_ids: set[str], requester_id: int, requester_name: str
) -> "Track | None":
    """Find a track related to `seed` to continue playback (radio).

    Primary: seed YouTube's Mix (RD<video_id>) and take the first entry not
    already played. Fallback: search the seed's uploader/title. Returns a LAZY
    Track (resolves its stream at play time), or None.
    """
    if not YTDLP_AVAILABLE:
        return None

    seed_id = _youtube_video_id(seed.webpage_url)

    def _flat(url_or_query: str):
        with yt_dlp.YoutubeDL(YTDL_FLAT_OPTS) as ydl:
            return ydl.extract_info(url_or_query, download=False)

    entries: list[dict] = []
    if seed_id:
        mix_url = f"https://www.youtube.com/watch?v={seed_id}&list=RD{seed_id}"
        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(_flat, mix_url), timeout=MUSIC_SEARCH_TIMEOUT
            )
            entries = [e for e in (info.get("entries") or []) if e]
        except Exception as e:
            print(f"[autoplay] mix fetch failed: {e}")

    # Fallback: search by uploader/title for something in the same vein.
    if not entries:
        seed_terms = (seed.uploader or seed.title or "").strip()
        if seed_terms:
            try:
                info = await asyncio.wait_for(
                    asyncio.to_thread(_flat, f"ytsearch10:{seed_terms}"),
                    timeout=MUSIC_SEARCH_TIMEOUT,
                )
                entries = [e for e in (info.get("entries") or []) if e]
            except Exception as e:
                print(f"[autoplay] fallback search failed: {e}")

    for e in entries:
        vid = e.get("id")
        if not vid or vid == seed_id or vid in exclude_ids:
            continue
        url = e.get("url") or e.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}"
        return Track(
            stream_url=None,
            webpage_url=f"https://www.youtube.com/watch?v={vid}",
            title=e.get("title") or "(untitled)",
            duration=int(e["duration"]) if e.get("duration") else None,
            requester_id=requester_id,
            requester_name=requester_name,
            thumbnail_url=e.get("thumbnail"),
            uploader=e.get("uploader") or e.get("channel"),
            source_label="Autoplay",
            resolve_query=url,
        )
    return None


def _format_time(seconds: float) -> str:
    s = max(0, int(seconds))
    m, s = divmod(s, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _progress_bar(elapsed: float, total: float | None, width: int = MUSIC_PROGRESS_WIDTH) -> str:
    """Static progress bar — accurate at render time, doesn't live-update."""
    if not total or total <= 0:
        return "▬" * width
    ratio = max(0.0, min(1.0, elapsed / total))
    pos = int(round(ratio * (width - 1)))
    return "▬" * pos + "🔘" + "▬" * (width - 1 - pos)


def _track_embed(
    track: Track,
    header: str = "🎵 Now Playing",
    position: int | None = None,
    elapsed: float = 0.0,
    paused: bool = False,
    loop_mode: str = "off",
    volume_pct: int | None = None,
) -> discord.Embed:
    """Rythm-style track embed: author/header, hyperlinked title, uploader,
    progress bar with time stamps, thumbnail, and a requester footer."""
    embed = discord.Embed(
        title=track.title,
        url=track.webpage_url,
        color=MUSIC_EMBED_COLOR,
    )
    # Header icon reflects pause state first, then loop mode.
    if paused:
        author = "⏸ Paused"
    elif loop_mode == "track":
        author = "🔂 Now Playing"
    elif loop_mode == "queue":
        author = "🔁 Now Playing"
    else:
        author = header
    embed.set_author(name=author)

    desc_parts: list[str] = []
    if track.uploader:
        desc_parts.append(f"by **{track.uploader}**")
    if track.duration:
        bar = _progress_bar(elapsed, track.duration)
        timing = f"`{_format_time(elapsed)} / {_format_time(track.duration)}`"
        desc_parts.append(f"\n{bar}\n{timing}")
    elif position is None:
        # Live stream or unknown duration — show elapsed only.
        desc_parts.append(f"\n`{_format_time(elapsed)}`")
    if desc_parts:
        embed.description = "\n".join(desc_parts)

    if track.thumbnail_url:
        embed.set_thumbnail(url=track.thumbnail_url)

    footer_parts: list[str] = []
    if position is not None:
        footer_parts.append(f"#{position} in queue")
    footer_parts.append(f"Requested by {track.requester_name}")
    if track.source_label and track.source_label != "YouTube":
        footer_parts.append(f"via {track.source_label}")
    if volume_pct is not None and volume_pct != 100:
        footer_parts.append(f"🔊 {volume_pct}%")
    embed.set_footer(text=" • ".join(footer_parts))
    return embed


class MusicControls(discord.ui.View):
    """⏮  ⏯  ⏭  ⏹ — control buttons attached to "now playing" embeds.

    Stateless: every callback resolves the GuildMusic via interaction.guild_id,
    so buttons on old embeds still operate on whatever's currently playing
    (which is what users expect — clicking "skip" on a stale embed skips the
    track that's playing right now, not a track that ended hours ago).
    """

    def __init__(self):
        super().__init__(timeout=None)

    async def _music(self, interaction: discord.Interaction) -> GuildMusic | None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Music only works in servers.", ephemeral=True
            )
            return None
        music = guild_music.get(interaction.guild_id)
        # Non-vote buttons (⏮ ⏯ ⏹ 🔁 🔀) are gated like the slash controls:
        # DJ-role/staff, or anyone alone in the channel with the bot.
        if member_is_dj(interaction.user, interaction.guild_id):
            return music
        if music and music.is_alone_with(interaction.user):
            return music
        await interaction.response.send_message(
            _dj_denied_msg(interaction.guild_id), ephemeral=True
        )
        return None

    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary, row=0)
    async def rewind_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        music = await self._music(interaction)
        if music is None:
            return
        if music.current is None:
            await interaction.response.send_message("Nothing playing.", ephemeral=True)
            return
        track = await music.restart_current()
        await interaction.response.send_message(
            f"⏮ Restarted **{track.title if track else 'track'}**.", ephemeral=True
        )

    @discord.ui.button(emoji="⏯", style=discord.ButtonStyle.primary, row=0)
    async def pause_resume_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        music = await self._music(interaction)
        if music is None:
            return
        if music.pause():
            await interaction.response.send_message("⏸ Paused.", ephemeral=True)
        elif music.resume():
            await interaction.response.send_message("▶ Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing playing.", ephemeral=True)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary, row=0)
    async def skip_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        # Skip is vote-enabled, so bypass the plain DJ gate in _music and run
        # the same vote logic the /skip command uses.
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Music only works in servers.", ephemeral=True
            )
            return
        music = guild_music.get(interaction.guild_id)
        if not music or not music.is_active():
            await interaction.response.send_message("Nothing playing.", ephemeral=True)
            return
        if not await _vote_gate(interaction, music, "skip"):
            return
        skipped = await music.skip()
        title = skipped.title if skipped else "track"
        await interaction.response.send_message(f"⏭ Skipped **{title}**.")

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger, row=0)
    async def stop_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        music = await self._music(interaction)
        if music is None:
            return
        if not music.is_active() and not music.queue and not music.current:
            await interaction.response.send_message("Nothing playing.", ephemeral=True)
            return
        await music.stop_and_clear()
        await interaction.response.send_message("⏹ Stopped.", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def loop_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        music = await self._music(interaction)
        if music is None:
            return
        new_mode = music.cycle_loop()
        labels = {"off": "Loop **off**", "track": "Looping **this track** 🔂", "queue": "Looping **the queue** 🔁"}
        await interaction.response.send_message(labels[new_mode], ephemeral=True)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        music = await self._music(interaction)
        if music is None:
            return
        count = await music.shuffle()
        if count < 2:
            await interaction.response.send_message(
                "Need at least 2 queued tracks to shuffle.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"🔀 Shuffled **{count}** tracks.", ephemeral=True
            )


def _music_unavailable_msg() -> str:
    missing = []
    if not FFMPEG_AVAILABLE:
        missing.append("`ffmpeg` (system binary not on PATH)")
    if not YTDLP_AVAILABLE:
        missing.append("`yt-dlp` (pip install yt-dlp)")
    return (
        "Music playback isn't available on this host: missing "
        + " and ".join(missing)
        + ". Tell whoever runs me."
    )


def _user_voice_channel(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member):
        return None
    voice = interaction.user.voice
    return voice.channel if voice else None


# ---- DJ role gating ----

def member_is_dj(member, guild_id: int) -> bool:
    """True if the member may use music control commands.

    Open to everyone when no DJ role is configured. Once a DJ role is set, only
    that role — plus anyone with Manage Channels / Manage Server / Administrator
    (staff bypass) — can control playback.
    """
    role_id = dj_roles.get(guild_id)
    if not role_id:
        return True
    if not isinstance(member, discord.Member):
        return True
    perms = member.guild_permissions
    if perms.manage_channels or perms.manage_guild or perms.administrator:
        return True
    return any(r.id == role_id for r in member.roles)


def has_dj_authority(member, guild_id: int) -> bool:
    """Stricter than member_is_dj: True only when the member has EXPLICIT
    authority — staff, or the configured DJ role. Returns False when no DJ role
    is set (so the alone/vote logic governs instead of blanket-allowing). Used
    by the vote system to decide who skips the vote entirely.
    """
    if not isinstance(member, discord.Member):
        return False
    perms = member.guild_permissions
    if perms.manage_channels or perms.manage_guild or perms.administrator:
        return True
    role_id = dj_roles.get(guild_id)
    return bool(role_id) and any(r.id == role_id for r in member.roles)


def _dj_denied_msg(guild_id: int) -> str:
    role_id = dj_roles.get(guild_id)
    mention = f"<@&{role_id}>" if role_id else "DJ"
    return (
        f"🎧 You need the {mention} role (or Manage Server), or to be alone in the "
        f"voice channel with me, to control playback. You can still use `/play`, "
        f"`/queue`, and `/nowplaying`."
    )


async def _require_dj(interaction: discord.Interaction) -> bool:
    """Gate a (non-vote) control command. Allows DJ-role/staff, OR anyone who is
    alone in the voice channel with the bot. Otherwise sends an ephemeral denial
    and returns False. Call after the guild-None check."""
    if interaction.guild is None:
        return True
    if member_is_dj(interaction.user, interaction.guild.id):
        return True
    music = guild_music.get(interaction.guild.id)
    if music and music.is_alone_with(interaction.user):
        return True
    await interaction.response.send_message(
        _dj_denied_msg(interaction.guild.id), ephemeral=True
    )
    return False


async def _vote_gate(interaction: discord.Interaction, music, action: str) -> bool:
    """Gate for vote-enabled actions ('skip', 'leave').

    Returns True when the action should run NOW (the caller then executes it and
    sends its own response). Returns False when this function already responded —
    either a vote was registered (public) or the user can't vote yet.

    Bypasses: DJ-role/staff act instantly; a solo listener acts instantly.
    Otherwise a strict majority of the humans in the voice channel must agree.
    """
    member = interaction.user
    gid = interaction.guild_id

    if has_dj_authority(member, gid):
        return True

    humans = music.voice_humans()
    if len(humans) <= 1:
        # Solo listener (or nobody else around) → no vote needed.
        return True

    listener_ids = {m.id for m in humans}
    if member.id not in listener_ids:
        await interaction.response.send_message(
            f"Join the voice channel to vote to {action}.", ephemeral=True
        )
        return False

    votes = music.skip_votes if action == "skip" else music.leave_votes
    votes &= listener_ids          # drop anyone who left the channel
    votes.add(member.id)
    needed = len(humans) // 2 + 1  # strict majority

    if len(votes) >= needed:
        votes.clear()
        return True

    await interaction.response.send_message(
        f"🗳️ Vote to **{action}** registered — **{len(votes)}/{needed}** needed. "
        f"Others in the voice channel can run `/{action}` (or hit the button) to agree."
    )
    return False


# ---------- Slash commands ----------

def _check_manage(interaction: discord.Interaction) -> bool:
    return (
        interaction.guild is None
        or interaction.user.guild_permissions.manage_channels
    )


SCOPE_CHOICES = [
    app_commands.Choice(name="this channel/thread only", value="here"),
    app_commands.Choice(name="whole server (default)",   value="server"),
]


@tree.command(name="setup", description="Add this channel to my auto-reply list (multiple channels supported).")
async def setup_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("DMs already work like this, dumbass.", ephemeral=True)
        return
    if not _check_manage(interaction):
        await interaction.response.send_message("You don't have permission. Cope.", ephemeral=True)
        return
    channels = auto_channels.setdefault(interaction.guild.id, set())
    if interaction.channel.id in channels:
        await interaction.response.send_message(
            f"Already auto-replying in <#{interaction.channel.id}>. Pay attention.",
            ephemeral=True,
        )
        return
    channels.add(interaction.channel.id)
    save_config()
    total = len(channels)
    await interaction.response.send_message(
        f"Fine. Added <#{interaction.channel.id}> to my auto-reply list "
        f"({total} channel{'s' if total != 1 else ''} total). Try not to bore me."
    )


@tree.command(name="unset", description="Stop auto-replying. Default: this channel only.")
@app_commands.describe(scope="Remove just this channel or all channels in this server")
@app_commands.choices(scope=[
    app_commands.Choice(name="this channel only (default)", value="here"),
    app_commands.Choice(name="all channels in this server", value="all"),
])
async def unset_cmd(
    interaction: discord.Interaction,
    scope: app_commands.Choice[str] | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message("Not in DMs.", ephemeral=True)
        return
    if not _check_manage(interaction):
        await interaction.response.send_message("You don't have permission. Cope.", ephemeral=True)
        return

    scope_val = scope.value if scope else "here"
    channels = auto_channels.get(interaction.guild.id, set())

    if scope_val == "all":
        if not channels:
            await interaction.response.send_message("Nothing was set up. Pay attention.", ephemeral=True)
            return
        n = len(channels)
        auto_channels.pop(interaction.guild.id, None)
        save_config()
        await interaction.response.send_message(
            f"Auto-reply disabled in **{n}** channel{'s' if n != 1 else ''}. Mention me if you need me."
        )
        return

    if interaction.channel.id not in channels:
        await interaction.response.send_message(
            "I wasn't auto-replying here anyway. Pay attention.", ephemeral=True
        )
        return
    channels.discard(interaction.channel.id)
    if not channels:
        auto_channels.pop(interaction.guild.id, None)
    save_config()
    await interaction.response.send_message(
        f"Removed <#{interaction.channel.id}> from auto-reply. "
        f"{len(channels)} channel{'s' if len(channels) != 1 else ''} still active."
    )


@tree.command(name="reset", description="Wipe my memory and reset my mood for this conversation.")
async def reset_cmd(interaction: discord.Interaction):
    key = conversation_key(interaction)
    history.pop(key, None)
    last_input_tokens.pop(key, None)
    last_touched.pop(key, None)
    rage.pop(key, None)
    mark_memory_dirty()
    await interaction.response.send_message("Memory wiped, patience restored. We're strangers now.")


@tree.command(name="mood", description="Switch my personality.")
@app_commands.describe(preset="Which personality to use", scope="Apply to this conversation or the whole server")
@app_commands.choices(
    preset=[
        app_commands.Choice(name="escalating (starts polite, gets unhinged — default)", value="escalating"),
        app_commands.Choice(name="feral (immediately unhinged)",        value="feral"),
        app_commands.Choice(name="villain (theatrical supervillain)",   value="villain"),
        app_commands.Choice(name="chill (helpful, mildly sarcastic)",   value="chill"),
        app_commands.Choice(name="tsundere (acts cold, secretly cares)", value="tsundere"),
    ],
    scope=SCOPE_CHOICES,
)
async def mood_cmd(
    interaction: discord.Interaction,
    preset: app_commands.Choice[str],
    scope: app_commands.Choice[str] | None = None,
):
    scope_val = scope.value if scope else "server"

    if scope_val == "server":
        if interaction.guild is None:
            await interaction.response.send_message("No server here — using `here` instead.", ephemeral=True)
            scope_val = "here"
        elif not _check_manage(interaction):
            await interaction.response.send_message("You don't have perms for server-wide. Try `scope: here`.", ephemeral=True)
            return

    key = conversation_key(interaction)
    if scope_val == "server":
        guild_moods[interaction.guild.id] = preset.value
        convo_moods.pop(key, None)
        convo_overrides_touched.pop(key, None)
        target = "this server"
    else:
        convo_moods[key] = preset.value
        convo_overrides_touched[key] = time.time()
        target = "this conversation"

    save_config()
    await interaction.response.send_message(f"Mood for **{target}** → **{preset.value}**.")


@tree.command(name="purge", description="Nuke conversation memory. Use scope to wipe everything in this server.")
@app_commands.describe(scope="What to wipe")
@app_commands.choices(scope=[
    app_commands.Choice(name="this conversation only", value="here"),
    app_commands.Choice(name="every conversation in this server", value="server"),
])
async def purge_cmd(
    interaction: discord.Interaction,
    scope: app_commands.Choice[str] | None = None,
):
    scope_val = scope.value if scope else "here"

    if scope_val == "server":
        if interaction.guild is None:
            await interaction.response.send_message("No server here. Use `here`.", ephemeral=True)
            return
        if not _check_manage(interaction):
            await interaction.response.send_message("You don't have perms for server-wide. Try `scope: here`.", ephemeral=True)
            return

        gid = interaction.guild.id
        guild_channel_ids = {c.id for c in interaction.guild.channels}
        guild_thread_ids = {t.id for t in interaction.guild.threads}
        wiped = 0
        wiped_keys: set[str] = set()
        for k in list(history.keys()):
            try:
                _, sid = k.split(":", 1)
                cid = int(sid)
            except (ValueError, TypeError):
                continue
            if cid in guild_channel_ids or cid in guild_thread_ids:
                wiped += 1
                history.pop(k, None)
                last_input_tokens.pop(k, None)
                last_touched.pop(k, None)
                rage.pop(k, None)
                wiped_keys.add(k)

        # Per-user rate-limit buckets keyed on (user_id, conversation_key)
        bucket_count = 0
        for bucket in list(user_msg_times.keys()):
            _, k = bucket
            if k in wiped_keys:
                user_msg_times.pop(bucket, None)
                user_last_touch.pop(bucket, None)
                user_last_notice.pop(bucket, None)
                bucket_count += 1

        if wiped:
            mark_memory_dirty()
        print(
            f"[purge] guild={gid} user={interaction.user} "
            f"wiped_convos={wiped} cleared_buckets={bucket_count} "
            f"(shared channel/thread state — affects all members)"
        )
        await interaction.response.send_message(
            f"Nuked **{wiped}** conversations in this server. Tabula rasa."
        )
        return

    key = conversation_key(interaction)
    had = key in history
    history.pop(key, None)
    last_input_tokens.pop(key, None)
    last_touched.pop(key, None)
    rage.pop(key, None)
    bucket_count = 0
    for bucket in list(user_msg_times.keys()):
        if bucket[1] == key:
            user_msg_times.pop(bucket, None)
            user_last_touch.pop(bucket, None)
            user_last_notice.pop(bucket, None)
            bucket_count += 1
    if had:
        mark_memory_dirty()
    print(
        f"[purge] key={key} user={interaction.user} had_history={had} "
        f"cleared_buckets={bucket_count} "
        f"(shared channel state — affects all members of this conversation)"
    )
    await interaction.response.send_message(
        "Memory wiped." if had else "Nothing to wipe — we were already strangers."
    )


def _rage_bar(level: float, width: int = 20) -> str:
    filled = int(round((level / RAGE_MAX) * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


@tree.command(name="rage", description="Check how pissed off I am at this conversation.")
async def rage_cmd(interaction: discord.Interaction):
    key = conversation_key(interaction)
    guild_id = interaction.guild.id if interaction.guild else None
    mood = resolve_mood(key, guild_id)

    if mood != "escalating":
        await interaction.response.send_message(
            f"**This conversation is in `{mood}` mode.**\n"
            "The patience meter only affects the `escalating` mood.",
            ephemeral=True,
        )
        return

    level = rage.get(key, 0.0)
    msg = (
        f"**Patience meter for this conversation**\n"
        f"`[{_rage_bar(level)}]` **{level:.0f}/100** — *{_rage_tier(level)}*"
    )
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="status", description="Show my current setup.")
async def status_cmd(interaction: discord.Interaction):
    key = conversation_key(interaction)
    guild_id = interaction.guild.id if interaction.guild else None
    mood = resolve_mood(key, guild_id)
    lines = [
        f"**Model:** `{MODEL}`",
        f"**Mood:** `{mood}`",
        f"**Patience meter (this convo):** `[{_rage_bar(rage.get(key, 0.0))}]` "
        f"{rage.get(key, 0.0):.0f}/100 — *{_rage_tier(rage.get(key, 0.0))}* "
        f"(only matters in `escalating` mood)",
        f"**Memory cap:** {MAX_TURNS} turns / {TOKEN_BUDGET:,} tokens / {CONVERSATION_TTL // 3600}h idle",
        f"**Memory persistence:** on (flushed every {int(MEMORY_SAVE_INTERVAL)}s when changed)",
        f"**Media tools:** "
        + ", ".join(filter(None, [
            "gifs (Giphy)" if GIPHY_API_KEY else None,
            "videos (YouTube)" if YOUTUBE_API_KEY else None,
            "emojis + stickers (per-server)",
        ])),
        f"**Per-user rate limit:** {USER_RATE_LIMIT} msgs / {int(USER_RATE_WINDOW)}s (per conversation)",
        f"**Max concurrent API calls:** {MAX_CONCURRENT}",
        f"**Conversation scope:** `{key}`",
        f"**Messages remembered here:** {len(history.get(key, []))}",
        f"**Last input tokens:** {last_input_tokens.get(key, 0):,}",
        f"**Active conversations (global):** {len(history)}",
    ]
    if interaction.guild:
        chans = auto_channels.get(interaction.guild.id, set())
        if chans:
            mentions = ", ".join(f"<#{c}>" for c in sorted(chans))
            lines.append(f"**Auto-reply channels ({len(chans)}):** {mentions}")
        else:
            lines.append("**Auto-reply channels:** none set")
        emoji_names, _, sticker_map = gather_guild_assets(interaction)
        lines.append(
            f"**Server assets (visible to me):** "
            f"{len(emoji_names)} custom emojis "
            f"(of {len(interaction.guild.emojis)} total), "
            f"{len(sticker_map)} stickers "
            f"(of {len(interaction.guild.stickers)} total)"
        )
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@tree.command(name="play", description="Play a track or playlist (URL or search query).")
@app_commands.describe(query="A URL (track or playlist) or search terms")
async def play_cmd(interaction: discord.Interaction, query: str):
    if interaction.guild is None:
        await interaction.response.send_message("Music only works in servers.", ephemeral=True)
        return
    if not MUSIC_AVAILABLE:
        await interaction.response.send_message(_music_unavailable_msg(), ephemeral=True)
        return

    channel = _user_voice_channel(interaction)
    if channel is None:
        await interaction.response.send_message(
            "Get into a voice channel first.", ephemeral=True
        )
        return

    perms = channel.permissions_for(interaction.guild.me)
    if not perms.connect or not perms.speak:
        await interaction.response.send_message(
            f"I'm missing **Connect** or **Speak** permission in {channel.mention}.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    music = get_or_create_music(interaction.guild.id)
    music.last_text_channel_id = interaction.channel.id

    try:
        await music.ensure_voice(channel)
    except discord.ClientException as e:
        await interaction.followup.send(f"Voice connection failed: {e}", ephemeral=True)
        return
    except asyncio.TimeoutError:
        await interaction.followup.send("Voice connection timed out.", ephemeral=True)
        return
    except Exception as e:
        await interaction.followup.send(
            f"Voice connection error: {type(e).__name__}: {e}", ephemeral=True
        )
        return

    if len(music.queue) >= MUSIC_MAX_QUEUE:
        await interaction.followup.send(
            f"Queue is full ({MUSIC_MAX_QUEUE} tracks). Skip or stop first.",
            ephemeral=True,
        )
        return

    # ---- Playlist / album branch ----
    if _is_playlist_url(query):
        result = await resolve_playlist(
            query, interaction.user.id, interaction.user.display_name
        )
        if not result or not result[0]:
            if _is_spotify_url(query) and not SPOTIFY_AVAILABLE:
                hint = (
                    "Spotify playlists need `SPOTIFY_CLIENT_ID` and "
                    "`SPOTIFY_CLIENT_SECRET` set on the host."
                )
            elif "music.apple.com" in query.lower() and "/playlist/" in query.lower():
                hint = (
                    "Apple Music **curated playlists** aren't supported (they're not "
                    "in the public iTunes API). Album links work, though."
                )
            else:
                hint = "Couldn't read that playlist, or it was empty."
            await interaction.followup.send(
                f"Couldn't queue `{query[:200]}`.\n{hint}", ephemeral=True
            )
            return
        tracks, coll_title = result
        added = await music.enqueue_many(tracks)
        embed = discord.Embed(
            description=(
                f"**➕ Queued {added} track{'s' if added != 1 else ''}** "
                f"from **{coll_title}**"
            ),
            color=MUSIC_EMBED_COLOR,
        )
        if added < len(tracks):
            embed.set_footer(text=f"Capped at {MUSIC_MAX_QUEUE}-track queue limit")
        await interaction.followup.send(embed=embed)
        return

    track = await resolve_track(query, interaction.user.id, interaction.user.display_name)
    if track is None:
        # Tailor the error to the input — Spotify needs creds, Apple Music
        # only handles single-track URLs, etc.
        if _is_spotify_url(query) and not SPOTIFY_AVAILABLE:
            hint = (
                "Spotify URLs need `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` "
                "set on the host. Tell whoever runs me, or send a YouTube link instead."
            )
        elif _is_spotify_url(query):
            hint = "Couldn't resolve that Spotify track. Try a YouTube link or search instead."
        elif _is_apple_music_url(query):
            hint = (
                "Couldn't resolve that Apple Music link. Single-track URLs only "
                "(the kind with `?i=...` at the end). Albums aren't supported yet."
            )
        else:
            hint = "Try a YouTube/SoundCloud URL or different search terms."
        await interaction.followup.send(
            f"Couldn't play `{query[:200]}`.\n{hint}",
            ephemeral=True,
        )
        return

    started_immediately = (music.current is None) and not music.is_active()
    position = await music.enqueue(track)
    if started_immediately:
        msg = await interaction.followup.send(
            embed=_track_embed(
                track,
                "🎵 Now Playing",
                elapsed=0.0,
                loop_mode=music.loop_mode,
                volume_pct=int(round(music.volume * 100)),
            ),
            view=MusicControls(),
        )
        # Drive the live progress bar off this message.
        if msg is not None:
            music.register_now_playing(msg)
    else:
        await interaction.followup.send(
            embed=_track_embed(track, "➕ Added to Queue", position=position)
        )


@tree.command(name="pause", description="Pause the current track.")
async def pause_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Music only works in servers.", ephemeral=True)
        return
    if not await _require_dj(interaction):
        return
    music = guild_music.get(interaction.guild.id)
    if not music or not music.pause():
        await interaction.response.send_message("Nothing playing.", ephemeral=True)
        return
    await interaction.response.send_message("⏸ Paused.")


@tree.command(name="resume", description="Resume a paused track.")
async def resume_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Music only works in servers.", ephemeral=True)
        return
    if not await _require_dj(interaction):
        return
    music = guild_music.get(interaction.guild.id)
    if not music or not music.resume():
        await interaction.response.send_message("Nothing paused.", ephemeral=True)
        return
    await interaction.response.send_message("▶ Resumed.")


@tree.command(name="skip", description="Skip the current track.")
async def skip_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Music only works in servers.", ephemeral=True)
        return
    music = guild_music.get(interaction.guild.id)
    if not music or not music.is_active():
        await interaction.response.send_message("Nothing playing.", ephemeral=True)
        return
    if not await _vote_gate(interaction, music, "skip"):
        return
    skipped = await music.skip()
    title = skipped.title if skipped else "current track"
    await interaction.response.send_message(f"⏭ Skipped **{title}**.")


@tree.command(name="stop", description="Clear the queue and stop playback.")
async def stop_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Music only works in servers.", ephemeral=True)
        return
    if not await _require_dj(interaction):
        return
    music = guild_music.get(interaction.guild.id)
    if not music or (not music.is_active() and not music.queue and not music.current):
        await interaction.response.send_message("Nothing playing.", ephemeral=True)
        return
    await music.stop_and_clear()
    await interaction.response.send_message("⏹ Stopped and cleared queue.")


@tree.command(name="leave", description="Disconnect from voice.")
async def leave_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Music only works in servers.", ephemeral=True)
        return
    music = guild_music.get(interaction.guild.id)
    if not music or music.voice is None or not music.voice.is_connected():
        await interaction.response.send_message("Not in a voice channel.", ephemeral=True)
        return
    if not await _vote_gate(interaction, music, "leave"):
        return
    await music.leave()
    await interaction.response.send_message("👋 Left voice.")


@tree.command(name="nowplaying", description="Show the currently playing track.")
async def nowplaying_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Music only works in servers.", ephemeral=True)
        return
    music = guild_music.get(interaction.guild.id)
    if not music or music.current is None:
        await interaction.response.send_message("Nothing playing.", ephemeral=True)
        return
    await interaction.response.send_message(
        embed=_track_embed(
            music.current,
            "🎵 Now Playing",
            elapsed=music.elapsed(),
            paused=music.is_paused(),
            loop_mode=music.loop_mode,
            volume_pct=int(round(music.volume * 100)),
        ),
        view=MusicControls(),
        ephemeral=True,
    )


@tree.command(name="queue", description="Show the current music queue.")
async def queue_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Music only works in servers.", ephemeral=True)
        return
    music = guild_music.get(interaction.guild.id)
    if not music or (not music.current and not music.queue):
        await interaction.response.send_message("Queue is empty.", ephemeral=True)
        return
    embed = discord.Embed(title="🎵 Queue", color=MUSIC_EMBED_COLOR)
    if music.current:
        marker = "⏸" if music.is_paused() else "▶"
        embed.add_field(
            name=f"{marker} Now playing",
            value=f"{music.current.display()} — *{music.current.requester_name}*",
            inline=False,
        )
        if music.current.thumbnail_url:
            embed.set_thumbnail(url=music.current.thumbnail_url)
    if music.queue:
        lines = [f"`{i:2d}.` {t.display()}" for i, t in enumerate(list(music.queue)[:10], 1)]
        if len(music.queue) > 10:
            lines.append(f"_…and {len(music.queue) - 10} more_")
        embed.add_field(
            name=f"Up next ({len(music.queue)})",
            value="\n".join(lines),
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="loop", description="Loop the current track, the queue, or turn looping off.")
@app_commands.describe(mode="What to loop")
@app_commands.choices(mode=[
    app_commands.Choice(name="off (default)", value="off"),
    app_commands.Choice(name="track (repeat current song)", value="track"),
    app_commands.Choice(name="queue (cycle through queue)", value="queue"),
])
async def loop_cmd(
    interaction: discord.Interaction, mode: app_commands.Choice[str]
):
    if interaction.guild is None:
        await interaction.response.send_message("Music only works in servers.", ephemeral=True)
        return
    if not await _require_dj(interaction):
        return
    music = get_or_create_music(interaction.guild.id)
    resolved = music.set_loop(mode.value)
    labels = {
        "off":   "Loop **off**.",
        "track": "🔂 Looping **this track**.",
        "queue": "🔁 Looping **the queue**.",
    }
    await interaction.response.send_message(labels[resolved])


@tree.command(name="shuffle", description="Shuffle the current queue (does not affect what's playing now).")
async def shuffle_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Music only works in servers.", ephemeral=True)
        return
    if not await _require_dj(interaction):
        return
    music = guild_music.get(interaction.guild.id)
    if not music or len(music.queue) < 2:
        await interaction.response.send_message(
            "Need at least 2 queued tracks to shuffle.", ephemeral=True
        )
        return
    count = await music.shuffle()
    await interaction.response.send_message(f"🔀 Shuffled **{count}** tracks.")


@tree.command(name="volume", description="Set playback volume (0-200, default 100).")
@app_commands.describe(level="Volume percent, 0 to 200")
async def volume_cmd(interaction: discord.Interaction, level: int):
    if interaction.guild is None:
        await interaction.response.send_message("Music only works in servers.", ephemeral=True)
        return
    if not await _require_dj(interaction):
        return
    if level < 0 or level > 200:
        await interaction.response.send_message(
            "Volume must be between 0 and 200.", ephemeral=True
        )
        return
    music = get_or_create_music(interaction.guild.id)
    new_level = music.set_volume(level)
    if new_level == 0:
        await interaction.response.send_message("🔇 Muted.")
    elif new_level <= 33:
        await interaction.response.send_message(f"🔈 Volume **{new_level}%**.")
    elif new_level <= 100:
        await interaction.response.send_message(f"🔉 Volume **{new_level}%**.")
    else:
        await interaction.response.send_message(f"🔊 Volume **{new_level}%** (boosted).")


@tree.command(name="remove", description="Remove a track from the queue by position.")
@app_commands.describe(position="1-indexed position of the track in the queue")
async def remove_cmd(interaction: discord.Interaction, position: int):
    if interaction.guild is None:
        await interaction.response.send_message("Music only works in servers.", ephemeral=True)
        return
    if not await _require_dj(interaction):
        return
    music = guild_music.get(interaction.guild.id)
    if not music or not music.queue:
        await interaction.response.send_message("Queue is empty.", ephemeral=True)
        return
    removed = await music.remove_at(position)
    if removed is None:
        await interaction.response.send_message(
            f"No track at position **#{position}** (queue has {len(music.queue) + 1} tracks).",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(f"🗑 Removed **{removed.title}** from the queue.")


@tree.command(name="clear", description="Clear the queue without stopping the current track.")
async def clear_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Music only works in servers.", ephemeral=True)
        return
    if not await _require_dj(interaction):
        return
    music = guild_music.get(interaction.guild.id)
    if not music or not music.queue:
        await interaction.response.send_message("Queue is already empty.", ephemeral=True)
        return
    count = await music.clear_queue()
    await interaction.response.send_message(f"🧹 Cleared **{count}** tracks from the queue.")


@tree.command(name="jump", description="Skip ahead to a specific position in the queue.")
@app_commands.describe(position="1-indexed position to jump to (discards everything before it)")
async def jump_cmd(interaction: discord.Interaction, position: int):
    if interaction.guild is None:
        await interaction.response.send_message("Music only works in servers.", ephemeral=True)
        return
    if not await _require_dj(interaction):
        return
    music = guild_music.get(interaction.guild.id)
    if not music or not music.queue:
        await interaction.response.send_message("Queue is empty.", ephemeral=True)
        return
    target = await music.jump_to(position)
    if target is None:
        await interaction.response.send_message(
            f"No track at position **#{position}**.", ephemeral=True
        )
        return
    await interaction.response.send_message(f"⏩ Jumping to **{target.title}**.")


@tree.command(name="dj", description="Set the DJ role (only it can control playback), or show the current one.")
@app_commands.describe(role="Role allowed to control music. Omit to show the current DJ role.")
async def dj_cmd(
    interaction: discord.Interaction, role: discord.Role | None = None
):
    if interaction.guild is None:
        await interaction.response.send_message("This only works in servers.", ephemeral=True)
        return
    if role is None:
        current = dj_roles.get(interaction.guild.id)
        if current:
            await interaction.response.send_message(
                f"🎧 Current DJ role: <@&{current}>. Only they (and Manage Server) can "
                f"skip/stop/pause/etc. Use `/dj role:` to change it, or `/djoff` to clear.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "No DJ role set — **everyone** can control playback. "
                "Use `/dj role:@SomeRole` to lock controls to a role.",
                ephemeral=True,
            )
        return
    if not _check_manage(interaction):
        await interaction.response.send_message(
            "You need Manage Channels to set the DJ role.", ephemeral=True
        )
        return
    dj_roles[interaction.guild.id] = role.id
    save_config()
    await interaction.response.send_message(
        f"🎧 DJ role set to {role.mention}. Only they (and anyone with Manage Server) "
        f"can skip, stop, pause, loop, change volume, etc. Everyone can still "
        f"`/play`, `/queue`, and `/nowplaying`."
    )


@tree.command(name="djoff", description="Clear the DJ role so everyone can control playback again.")
async def djoff_cmd(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("This only works in servers.", ephemeral=True)
        return
    if not _check_manage(interaction):
        await interaction.response.send_message(
            "You need Manage Channels to change the DJ role.", ephemeral=True
        )
        return
    if interaction.guild.id in dj_roles:
        dj_roles.pop(interaction.guild.id, None)
        save_config()
        await interaction.response.send_message(
            "🎧 DJ role cleared — **everyone** can control playback now."
        )
    else:
        await interaction.response.send_message(
            "No DJ role was set.", ephemeral=True
        )


@tree.command(name="autoplay", description="Toggle autoplay — keep playing related songs when the queue ends.")
@app_commands.describe(state="Turn autoplay on or off (omit to see the current setting)")
@app_commands.choices(state=[
    app_commands.Choice(name="on", value="on"),
    app_commands.Choice(name="off", value="off"),
])
async def autoplay_cmd(
    interaction: discord.Interaction, state: app_commands.Choice[str] | None = None
):
    if interaction.guild is None:
        await interaction.response.send_message("Music only works in servers.", ephemeral=True)
        return
    current = guild_autoplay.get(interaction.guild.id, True)  # default ON
    if state is None:
        await interaction.response.send_message(
            f"📻 Autoplay is currently **{'on' if current else 'off'}**. "
            f"When the queue ends, I {'keep playing related tracks' if current else 'stop'}. "
            f"Use `/autoplay state:on|off` to change it.",
            ephemeral=True,
        )
        return
    if not await _require_dj(interaction):
        return
    new_val = state.value == "on"
    guild_autoplay[interaction.guild.id] = new_val
    save_config()
    if new_val:
        await interaction.response.send_message(
            "📻 Autoplay **on** — when the queue runs out, I'll keep the vibe going "
            "with related tracks. `/stop` or `/autoplay state:off` to end it."
        )
    else:
        await interaction.response.send_message(
            "📻 Autoplay **off** — I'll stop once the queue is empty."
        )


def _shutdown_flush():
    """Best-effort sync save when the process is exiting."""
    if _memory_dirty:
        try:
            save_memory()
            print("[memory] flushed on shutdown")
        except Exception as e:
            print(f"[memory] shutdown flush failed: {e}")


atexit.register(_shutdown_flush)


if __name__ == "__main__":
    # If Discord rate-limits the login (429), don't crash and let the
    # process supervisor restart us straight into another 429 — that
    # extends the IP cooldown. Sleep in-process first so by the time
    # we exit, the limit has cleared or come close.
    try:
        bot.run(DISCORD_TOKEN)
    except discord.HTTPException as e:
        if e.status != 429:
            raise
        retry_after = 0.0
        try:
            retry_after = float((e.response.headers.get("retry-after") or "0"))
        except (TypeError, ValueError):
            pass
        wait = max(60.0, min(retry_after or 600.0, 3600.0))
        print(
            f"[startup] Discord 429 on login (global IP rate limit). "
            f"Sleeping {wait:.0f}s before exit to avoid a restart loop "
            f"(retry-after header={retry_after or 'absent'})."
        )
        time.sleep(wait)
        raise
