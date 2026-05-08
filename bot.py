"""
Discord chatbot powered by Claude (Sonnet 4.6).

Modes (set per-server or per-conversation via /mood):
  escalating  — DEFAULT. Starts polite, escalates as the conversation grows
                and as questions get stupider. See the rage meter below.
  feral       — Immediately unhinged: cusses, roasts, full menace from msg #1.
  villain     — Theatrical Saturday-morning supervillain.
  chill       — Friendly, mildly sarcastic, swears sparingly.

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
  set by /setup) or (replying to a bot message).

Slash commands:
  /setup, /unset, /reset, /purge, /mood, /rage, /status

Persistence (channels.json, with channels.json.bak rotation):
  auto_channels, guild_moods, convo_moods, convo_overrides_touched.
  History / rage / rate-limit state are all in-memory and reset on restart
  (deliberate: clean slate).

Background:
  cleanup_loop runs hourly — drops stale conversations (>48h idle), stale
  per-user rate-limit buckets (>24h idle), and stale convo_mood overrides
  (>30d idle).
"""

import asyncio
import io
import json
import os
import random
import re
import time
from collections import defaultdict, deque
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
import anthropic
from anthropic import AsyncAnthropic

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


# ---------- Globals ----------

claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY, max_retries=4)
api_semaphore = asyncio.Semaphore(MAX_CONCURRENT)

intents = discord.Intents.default()
intents.message_content = True
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

cleanup_runs = 0


# ---------- Persistence ----------

def load_config():
    global auto_channels, guild_moods, convo_moods, convo_overrides_touched
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
        }, indent=2))
    except Exception as e:
        print(f"Failed to save config: {e}")


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

    if text:
        text_parts.append(f"{speaker}{text}")
    elif image_blocks:
        text_parts.append(f"{speaker}(sent an image)")
    else:
        text_parts.append(f"{speaker}(no text)")

    blocks.append({"type": "text", "text": "\n".join(text_parts)})
    blocks.extend(image_blocks)

    return blocks


# ---------- Claude call ----------

async def ask_claude(
    key: str, user_content: list, mood: str, rage_level: float = 0.0
) -> tuple[str, list[bytes]]:
    history[key].append({"role": "user", "content": user_content})
    last_touched[key] = time.time()
    trim_by_count(key)
    trim_by_tokens(key)

    api_kwargs: dict = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system_prompt_for(mood, rage_level),
    }
    if GIPHY_API_KEY:
        api_kwargs["tools"] = [GIF_TOOL]

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

        text = "".join(text_parts).strip()

        # Store text-only history (preserves user/assistant alternation across turns).
        # Note any GIFs inline so Claude sees them in subsequent context.
        if gif_queries:
            note = " ".join(f"*[posted GIF: {q}]*" for q in gif_queries)
            history_text = f"{text}\n\n{note}".strip() if text else note
        else:
            history_text = text
        history[key].append({"role": "assistant", "content": history_text or "(no response)"})
        last_touched[key] = time.time()

        if response.stop_reason == "max_tokens" and text:
            text += "\n\n*(...cut off, hit token limit)*"

        if not text and not gif_blobs:
            text = "(I got nothing for you)"
        return text, gif_blobs

    return "(retry exhausted)", []


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

    typing_ctx = await maybe_typing(message.channel)
    async with typing_ctx:
        try:
            reply, gif_blobs = await ask_claude(key, user_content, mood, rage_level)
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
    try:
        if not parts and not files:
            await message.reply("(I got nothing for you)")
        elif not parts:
            await message.reply(files=files)
        elif len(parts) == 1:
            await message.reply(parts[0], files=files)
        else:
            await message.reply(parts[0])
            for part in parts[1:-1]:
                await message.channel.send(part)
            await message.channel.send(parts[-1], files=files)
    except discord.HTTPException as e:
        try:
            await message.channel.send(f"Discord choked on the reply: {e}")
        except discord.HTTPException:
            pass


# ---------- Discord events ----------

@bot.event
async def on_ready():
    load_config()
    try:
        synced = await tree.sync()
        synced_n = len(synced)
    except Exception as e:
        synced_n = -1
        print(f"Slash command sync failed: {e}")
    bot.loop.create_task(cleanup_loop())
    print("=" * 60)
    print(f"  ONLINE: {bot.user}  (id={bot.user.id})")
    print(f"  model={MODEL}  default_mood={DEFAULT_MOOD}  max_tokens={MAX_TOKENS}")
    print(f"  guilds={len(bot.guilds)}  slash_commands_synced={synced_n}")
    total_auto = sum(len(v) for v in auto_channels.values())
    print(f"  auto_channels: {len(auto_channels)} guilds, {total_auto} channels  "
          f"guild_moods={len(guild_moods)}  convo_moods={len(convo_moods)}")
    print(f"  rate_limit={USER_RATE_LIMIT}/{int(USER_RATE_WINDOW)}s per user/convo  "
          f"max_concurrent={MAX_CONCURRENT}")
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
    if not content and not has_images:
        return

    await handle_chat(message, content)


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
    await interaction.response.send_message("Memory wiped, patience restored. We're strangers now.")


@tree.command(name="mood", description="Switch my personality.")
@app_commands.describe(preset="Which personality to use", scope="Apply to this conversation or the whole server")
@app_commands.choices(
    preset=[
        app_commands.Choice(name="escalating (starts polite, gets unhinged — default)", value="escalating"),
        app_commands.Choice(name="feral (immediately unhinged)",       value="feral"),
        app_commands.Choice(name="villain (theatrical supervillain)",  value="villain"),
        app_commands.Choice(name="chill (helpful, mildly sarcastic)",  value="chill"),
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
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
