"""Offline tests for bot.py.

Runs the real `anthropic` SDK against a local fake Messages API and fake
Discord objects — no credentials, no network. Plain Python, no pytest:

    pip install -r requirements.txt   # discord.py, anthropic, aiohttp
    python tests/test_bot.py

Exit code 0 = all checks passed. Env vars like CLAUDE_MODEL can be passed as
extra arguments: `python tests/test_bot.py CLAUDE_MODEL=claude-sonnet-4-6`.
"""
import asyncio
import datetime as dt
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time

from aiohttp import web

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8765 + (os.getpid() % 500)

# Run from a scratch copy so memory.json / channels.json never touch the repo.
WORK = tempfile.mkdtemp(prefix="botdir_")
shutil.copy(os.path.join(REPO, "bot.py"), WORK)

os.environ.update({
    "DISCORD_TOKEN": "x",
    "ANTHROPIC_API_KEY": "sk-test",
    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{PORT}",
    "YTDLP_AUTO_UPDATE": "0",
    "GIPHY_API_KEY": "fake",
    "BOT_TIMEZONE": "UTC",
})
for k in ("CLAUDE_MODEL", "CLAUDE_EFFORT", "CLAUDE_FALLBACKS", "PROMPT_CACHE",
          "ENABLE_WEB_SEARCH", "ENABLE_WEB_FETCH", "CONVO_SUMMARY"):
    os.environ.pop(k, None)
for kv in sys.argv[1:]:
    k, v = kv.split("=", 1)
    os.environ[k] = v

spec = importlib.util.spec_from_file_location("bot", os.path.join(WORK, "bot.py"))
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
discord = bot.discord

# ---------------------------------------------------------------- fake API

requests: list = []
script: list = []


async def handler(request):
    body = await request.json()
    requests.append({"path": request.path_qs, "headers": dict(request.headers), "body": body})
    if not script:
        return web.json_response({"type": "error", "error": {"type": "api_error", "message": "test script exhausted"}}, status=500)
    status, payload = script.pop(0)
    return web.json_response(payload, status=status)


def msg(content, stop="end_turn", usage=None):
    return 200, {
        "id": "msg_1", "type": "message", "role": "assistant",
        "model": bot.MODEL, "content": content, "stop_reason": stop, "stop_sequence": None,
        "usage": usage or {"input_tokens": 12, "output_tokens": 7,
                           "cache_read_input_tokens": 900, "cache_creation_input_tokens": 50},
    }


def text(t, stop="end_turn"):
    return msg([{"type": "text", "text": t}], stop=stop)


def tool(name, inp, tid="toolu_1"):
    return msg([{"type": "tool_use", "id": tid, "name": name, "input": inp}], stop="tool_use")


def err(status, message):
    return status, {"type": "error", "error": {"type": "invalid_request_error", "message": message}}


def last_system() -> str:
    return requests[-1]["body"]["system"]


def tool_names(req=None) -> list:
    return [t["name"] for t in (req or requests[-1])["body"].get("tools", [])]


# ------------------------------------------------------------ fake Discord

class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeMember(Obj):
    pass


class FakeGuild:
    def __init__(self, gid=77, name="Test Server"):
        self.id, self.name, self.member_count = gid, name, 42
        self.me = Obj(display_name="Botty")
        self.emojis, self.stickers, self.channels, self.threads = [], [], [], []
        self._members = {}

    def get_member(self, uid):
        return self._members.get(uid)


class FakeChannel:
    def __init__(self, cid=1001, name="general", topic="Where we talk", guild=None):
        self.id, self.name, self.topic, self.guild = cid, name, topic, guild
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))
        return Obj(id=len(self.sent))

    def typing(self):
        class _Typing:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *a):
                return False
        return _Typing()

    async def fetch_message(self, mid):
        raise discord.NotFound(Obj(status=404, reason="nf"), "nf")


class FakeAttachment:
    def __init__(self, filename, content_type, data=b"", size=None, url=None):
        self.filename, self.content_type, self._data = filename, content_type, data
        self.size = size if size is not None else len(data)
        self.url = url or f"https://cdn.discordapp.com/attachments/1/2/{filename}"

    async def read(self, *, use_cached=False):
        return self._data


class FakeMessage:
    def __init__(self, content, author_id=1, author_name="Bob", guild=None, channel=None,
                 attachments=None, dm=False):
        self.content = content
        self.author = Obj(id=author_id, display_name=author_name, bot=False)
        self.guild = None if dm else (guild or FakeGuild())
        self.channel = channel or FakeChannel(guild=self.guild)
        self.attachments = attachments or []
        self.stickers = []
        self.reference = None
        self.mentions = []
        self.created_at = dt.datetime(2026, 10, 29, 18, 30, tzinfo=dt.timezone.utc)
        self.reactions_added = []
        self.replies = []

    async def add_reaction(self, emoji):
        if emoji == "💩bad":
            raise discord.HTTPException(Obj(status=400, reason="bad"), "bad emoji")
        self.reactions_added.append(emoji)

    async def reply(self, content=None, **kw):
        self.replies.append((content, kw))
        return Obj(id=1)


class FakeResponse:
    def __init__(self):
        self.sent, self.done = [], False

    async def send_message(self, content=None, **kw):
        assert not self.done, "responded twice"
        self.done = True
        self.sent.append((content, kw))

    async def defer(self, **kw):
        assert not self.done, "responded twice"
        self.done = True

    async def edit_message(self, **kw):
        self.sent.append(("edit", kw))


class FakeInteraction:
    def __init__(self, user_id=1, name="Bob", guild=None, channel=None, manage=True):
        self.user = Obj(id=user_id, display_name=name, name=name,
                        guild_permissions=Obj(manage_channels=manage))
        self.guild = guild
        self.guild_id = guild.id if guild else None
        self.channel = channel or FakeChannel(guild=guild)
        self.response = FakeResponse()
        self.followup = Obj(sent=[])

        async def _send(content=None, **kw):
            self.followup.sent.append((content, kw))
        self.followup.send = _send


failures: list = []


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


def ctx_for(message, key=None, mood="chill", rage=0.0, **kw):
    return bot.ChatContext(
        key=key or f"channel:{message.channel.id}", mood=mood, rage_level=rage, message=message,
        guild=message.guild, channel=message.channel,
        speaker_id=message.author.id, speaker_name=message.author.display_name,
        place=kw.pop("place", ""), **kw,
    )


# ------------------------------------------------------------------- tests

async def main():
    app = web.Application()
    app.router.add_post("/v1/messages", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()

    async def fake_search(q):
        return "http://gif"

    async def fake_fetch(url):
        return b"GIF89a-bytes"

    bot.search_giphy = fake_search
    bot.fetch_gif_bytes = fake_fetch

    print(f"model={bot.MODEL} effort={bot.EFFORT} fallbacks={bot.USE_FALLBACKS} "
          f"cache={bot.PROMPT_CACHE} web_fetch={bot.WEB_FETCH_ENABLED} summary={bot.SUMMARY_ENABLED}")

    # ---- 1. plain reply: request shape, identity, capabilities, timed speaker tag
    guild = FakeGuild()
    m = FakeMessage("hi there", guild=guild)
    content = await bot.build_user_content(m, m.content)
    check(content[0]["text"] == "[Bob (1) · 10-29 18:30]: hi there", f"speaker tag with time: {content[0]['text']!r}")
    ctx = ctx_for(m, mood="escalating", rage=37.0, emoji_map={"wave": (123, False)}, emoji_names=["wave"],
                  place=await bot.describe_place(m))
    script.append(msg([{"type": "thinking", "thinking": "", "signature": "sig"},
                       {"type": "text", "text": "hello :wave:"}]))
    reply, extras = await bot.ask_claude(ctx, content)
    r = requests[-1]
    b = r["body"]
    check(reply == "hello <:wave:123>", f"plain reply rendered: {reply!r}")
    check(b["model"] == bot.MODEL, "model sent")
    if bot.PROMPT_CACHE:
        check(b.get("cache_control") == {"type": "ephemeral"}, "top-level cache_control sent")
    if bot.EFFORT:
        check(b.get("output_config") == {"effort": bot.EFFORT}, "effort sent")
    if bot.USE_FALLBACKS:
        check(b.get("fallbacks") == "default", "fallbacks default sent")
        check(bot.FALLBACK_BETA in r["headers"].get("anthropic-beta", ""), "fallback beta header sent")
    sysp = b["system"]
    check("37" not in sysp.split("ABOUT YOU")[0] and "'snarky'" in sysp, "rage tier (not number) in prompt")
    check('Your Discord name here is "Botty"' in sysp, "bot knows its own name")
    check('server "Test Server" (~42 members), in the channel #general' in sysp and "Where we talk" in sysp,
          "bot knows server/channel/topic")
    check("Today is Wednesday, 2026-10-29" in sysp or "Today is " in sysp, "date in prompt")
    check("WHAT YOU CAN DO" in sysp and "create a Discord poll" in sysp, "capabilities block")
    names = tool_names()
    check(names[:1] == ["send_gif"] and {"react", "create_poll", "remind", "remember", "forget"} <= set(names),
          f"tools offered: {names}")
    if bot.WEB_FETCH_ENABLED:
        check("web_fetch" in names and b["tools"][-1]["type"].startswith("web_fetch_"), "web_fetch tool last")
    check("WHAT YOU REMEMBER" not in sysp, "no notes block when nothing remembered")
    check(bot.last_input_tokens[ctx.key] == 12 + 900 + 50, "last_input_tokens counts cached tokens")

    s1 = bot._build_system(ctx_for(m, mood="escalating", rage=31.0), [])
    s2 = bot._build_system(ctx_for(m, mood="escalating", rage=49.0), [])
    check(s1 == s2, "system prompt identical across the same rage tier")

    # ---- 2. DM: no speaker name, DM place line
    dm = FakeMessage("yo", dm=True)
    dmc = await bot.build_user_content(dm, dm.content)
    check(dmc[0]["text"] == "[10-29 18:30]: yo", f"DM tag is just the time: {dmc[0]['text']!r}")
    check("private DM" in await bot.describe_place(dm), "DM place line")

    # ---- 3. react tool (custom emoji rendered), then text
    m3 = FakeMessage("lol", guild=guild, channel=FakeChannel(cid=1003, guild=guild))
    ctx3 = ctx_for(m3, emoji_map={"kek": (555, True)}, emoji_names=["kek"])
    script.append(tool("react", {"emoji": "kek"}))
    script.append(text("heh"))
    reply, extras = await bot.ask_claude(ctx3, await bot.build_user_content(m3, "lol"))
    check(m3.reactions_added == ["<a:kek:555>"], f"custom reaction added: {m3.reactions_added}")
    check(reply == "heh" and extras.reactions == ["kek"], "react then text")
    check(list(bot.history[ctx3.key])[-1]["content"] == "heh\n\n*[reacted kek]*", "history notes the reaction")
    tr = requests[-1]["body"]["messages"][-1]["content"][0]
    check(tr["type"] == "tool_result" and "Reacted" in tr["content"], "react tool_result sent back")

    # react failure -> is_error, model still answers
    m3b = FakeMessage("ok", guild=guild, channel=FakeChannel(cid=1004, guild=guild))
    script.append(tool("react", {"emoji": "💩bad"}))
    script.append(text("fine"))
    reply, extras = await bot.ask_claude(ctx_for(m3b), await bot.build_user_content(m3b, "ok"))
    tr = requests[-1]["body"]["messages"][-1]["content"][0]
    check(tr.get("is_error") is True and reply == "fine", "failed reaction reported as error")

    # ---- 4. poll tool -> extras.poll is a discord.Poll attached to the reply
    m4 = FakeMessage("pizza or tacos?", guild=guild, channel=FakeChannel(cid=1005, guild=guild))
    script.append(tool("create_poll", {"question": "Dinner?", "answers": ["Pizza", "Tacos", "pizza", ""], "hours": 2}))
    script.append(text("vote!"))
    reply, extras = await bot.ask_claude(ctx_for(m4), await bot.build_user_content(m4, m4.content))
    poll = extras.poll
    check(isinstance(poll, discord.Poll) and poll.question == "Dinner?" and len(poll.answers) == 2,
          f"poll built with deduped answers: {poll and [a.text for a in poll.answers]}")
    check(poll.duration == dt.timedelta(hours=2), "poll duration")
    check("*[posted poll: Dinner?" in list(bot.history[ctx_for(m4).key])[-1]["content"], "history notes the poll")
    # handle_chat attaches it to the reply
    bot.user_is_rate_limited = lambda uid, key: False
    m4b = FakeMessage("poll pls", guild=guild, channel=FakeChannel(cid=1006, guild=guild))
    script.append(tool("create_poll", {"question": "Q?", "answers": ["a", "b"]}))
    script.append(text("here"))
    await bot.handle_chat(m4b, "poll pls")
    check(m4b.replies and m4b.replies[0][0] == "here" and isinstance(m4b.replies[0][1].get("poll"), discord.Poll),
          "handle_chat sends the poll with the reply")

    # ---- 5. remind tool -> stored, persisted, delivered
    m5 = FakeMessage("remind me in 20 min to stretch", guild=guild, channel=FakeChannel(cid=1007, guild=guild))
    script.append(tool("remind", {"minutes": 20, "text": "stretch"}))
    script.append(text("set"))
    before = time.time()
    reply, extras = await bot.ask_claude(ctx_for(m5), await bot.build_user_content(m5, m5.content))
    rem = [r for r in bot.reminders if r["text"] == "stretch"]
    check(len(rem) == 1 and rem[0]["user_id"] == 1 and abs(rem[0]["due"] - (before + 1200)) < 5
          and rem[0]["channel_id"] == 1007, f"reminder stored: {rem}")
    tr = requests[-1]["body"]["messages"][-1]["content"][0]
    check("Reminder #" in tr["content"] and "in 20m" in tr["content"], f"remind tool_result: {tr['content']}")
    saved = json.loads(open(bot.CONFIG_PATH).read())
    check(any(r["text"] == "stretch" for r in saved.get("reminders", [])), "reminder persisted to channels.json")
    # bad minutes -> error
    script.append(tool("remind", {"minutes": 0, "text": "x"}))
    script.append(text("nope"))
    await bot.ask_claude(ctx_for(m5), await bot.build_user_content(m5, "again"))
    tr = requests[-1]["body"]["messages"][-1]["content"][0]
    check(tr.get("is_error") is True, "remind rejects minutes=0")
    # delivery: make it due now and run one pass
    rem[0]["due"] = time.time() - 1
    ch = FakeChannel(cid=1007, guild=guild)
    bot.bot.get_channel = lambda cid: ch if cid == 1007 else None
    n = await bot.process_due_reminders()
    check(n == 1 and ch.sent and "<@1>" in ch.sent[0][0] and "stretch" in ch.sent[0][0], f"reminder delivered: {ch.sent}")
    am = ch.sent[0][1].get("allowed_mentions")
    check(isinstance(am, discord.AllowedMentions) and am.everyone is False, "reminder pings only the user")
    check(not any(r["text"] == "stretch" for r in bot.reminders), "delivered reminder removed")
    check(list(bot.history["channel:1007"])[-1]["content"].startswith("*[sent reminder to Bob"), "history notes delivery")

    # ---- 6. remember / forget -> notes in the next prompt
    m6 = FakeMessage("my cat is called Mochi", guild=guild, channel=FakeChannel(cid=1008, guild=guild))
    script.append(tool("remember", {"note": "has a cat named Mochi", "user_id": "1"}))
    script.append(text("noted"))
    await bot.ask_claude(ctx_for(m6), await bot.build_user_content(m6, m6.content))
    check(bot.user_notes.get(1) and bot.user_notes[1][0]["text"] == "has a cat named Mochi", "note saved")
    script.append(tool("remember", {"note": "this is a cooking server"}))   # no user_id -> server note
    script.append(text("ok"))
    await bot.ask_claude(ctx_for(m6), await bot.build_user_content(m6, "we cook here"))
    check(bot.server_notes.get(77) and bot.server_notes[77][0]["text"] == "this is a cooking server", "server note saved")
    script.append(text("sure"))
    await bot.ask_claude(ctx_for(m6), await bot.build_user_content(m6, "what do you know"))
    sysp = last_system()
    check("WHAT YOU REMEMBER" in sysp and "About Bob (1): 1. has a cat named Mochi" in sysp
          and "About this server: 1. this is a cooking server" in sysp, "notes injected into prompt")
    check(sysp.index("WHAT YOU REMEMBER") > sysp.index("WHAT YOU CAN DO"), "notes come after the stable blocks")
    # another speaker in the same conversation still gets Bob's notes (participant scan)
    m6b = FakeMessage("hey", author_id=2, author_name="Ann", guild=guild, channel=FakeChannel(cid=1008, guild=guild))
    script.append(text("hey ann"))
    await bot.ask_claude(ctx_for(m6b), await bot.build_user_content(m6b, "hey"))
    check("About Bob (1)" in last_system(), "notes for earlier participants included")
    # dedupe + cap
    ok, m_ = bot.add_note(1, "Has a cat named MOCHI", 1)
    check(ok and "already" in m_ and len(bot.user_notes[1]) == 1, "duplicate note not re-added")
    for i in range(bot.NOTES_PER_USER + 3):
        bot.add_note(1, f"fact {i}", 1)
    check(len(bot.user_notes[1]) == bot.NOTES_PER_USER and bot.user_notes[1][-1]["text"].startswith("fact"),
          "notes capped per user (oldest dropped)")
    # forget by number
    first = bot.user_notes[1][0]["text"]
    script.append(tool("forget", {"number": 1, "user_id": "1"}))
    script.append(text("gone"))
    await bot.ask_claude(ctx_for(m6), await bot.build_user_content(m6, "forget that"))
    check(bot.user_notes[1][0]["text"] != first and len(bot.user_notes[1]) == bot.NOTES_PER_USER - 1, "forget removed note #1")
    script.append(tool("forget", {"number": 99, "user_id": "1"}))
    script.append(text("hm"))
    await bot.ask_claude(ctx_for(m6), await bot.build_user_content(m6, "forget 99"))
    check(requests[-1]["body"]["messages"][-1]["content"][0].get("is_error") is True, "forget of missing note -> error")

    # ---- 7. rolling summary on trim
    key7 = "channel:1009"
    m7 = FakeMessage("new", guild=guild, channel=FakeChannel(cid=1009, guild=guild))
    for i in range(51):
        bot.history[key7].append({"role": "user", "content": [{"type": "text", "text": f"[Bob (1) · 10-29 10:{i:02d}]: u{i}"}]})
        bot.history[key7].append({"role": "assistant", "content": f"a{i}"})
    n_before = len(requests)
    if bot.SUMMARY_ENABLED:
        script.append(text("Bob and the bot counted to fifty."))   # summary call
    script.append(text("ok"))
    await bot.ask_claude(ctx_for(m7), await bot.build_user_content(m7, "new"))
    if bot.SUMMARY_ENABLED:
        check(len(requests) - n_before == 2, "trim triggered one summary call + the reply")
        sreq = requests[-2]["body"]
        check(sreq["system"] == bot.SUMMARY_SYSTEM and "cache_control" not in sreq and "u0" in sreq["messages"][0]["content"],
              "summary call has its own prompt, no cache, dropped turns inside")
        check(bot.summaries.get(key7) == "Bob and the bot counted to fifty.", "summary stored")
        check("EARLIER IN THIS CONVERSATION" in last_system() and "counted to fifty" in last_system(),
              "summary injected into the reply's prompt")
    h7 = bot.history[key7]
    check(len(h7) <= 72 and h7[0]["role"] == "user", f"history trimmed to {len(h7)}")
    n_before = len(requests)
    script.append(text("still"))
    await bot.ask_claude(ctx_for(m7), await bot.build_user_content(m7, "more"))
    check(len(requests) - n_before == 1, "no summary call when nothing was trimmed")
    bot.forget_conversation(key7)
    check(key7 not in bot.summaries and key7 not in bot.history, "forget_conversation clears summary too")

    # ---- 8. attachments: text file + pdf; aging + expiry retry
    att_txt = FakeAttachment("error.log", "text/plain", b"Traceback: boom\n")
    att_pdf = FakeAttachment("spec.pdf", "application/pdf", b"%PDF", size=1000)
    att_bin = FakeAttachment("thing.bin", "application/octet-stream", b"\x00\x01")
    m8 = FakeMessage("", guild=guild, channel=FakeChannel(cid=1010, guild=guild), attachments=[att_txt, att_pdf, att_bin])
    c8 = await bot.build_user_content(m8, "")
    types = [b_["type"] for b_ in c8]
    check(types == ["text", "text", "document"], f"attachment blocks: {types}")
    check(c8[1]["text"].startswith('<attachment name="error.log">') and "boom" in c8[1]["text"], "text attachment inlined")
    check(c8[2]["source"] == {"type": "url", "url": att_pdf.url}, "pdf as url document")
    check("(sent a file: error.log, spec.pdf)" in c8[0]["text"], "placeholder lists files")
    key8 = "channel:1010"
    for i in range(4):
        bot.history[key8].append({"role": "user", "content": list(c8)})
        bot.history[key8].append({"role": "assistant", "content": "ok"})
    bot.age_out_media(key8, keep_last=2)
    old = bot.history[key8][0]["content"]
    check(all(b_["type"] == "text" for b_ in old) and "no longer shown" in old[1]["text"], f"old attachments aged out: {old}")
    new = bot.history[key8][-2]["content"]
    check(any(b_["type"] == "document" for b_ in new), "recent attachments kept")
    script.append(err(400, "Could not process the document at the given URL"))
    script.append(text("fine"))
    reply, _ = await bot.ask_claude(ctx_for(m8), await bot.build_user_content(m8, "look"))
    check(reply == "fine" and not any(b_.get("type") == "document"
                                       for mm in requests[-1]["body"]["messages"] if isinstance(mm["content"], list)
                                       for b_ in mm["content"]), "document error -> retried without attachments")

    # ---- 9. refusal
    m9 = FakeMessage("bad", guild=guild, channel=FakeChannel(cid=1011, guild=guild))
    script.append(msg([], stop="refusal"))
    reply, _ = await bot.ask_claude(ctx_for(m9), await bot.build_user_content(m9, "bad"))
    check(reply == bot.REFUSAL_MESSAGES["chill"] and len(bot.history.get("channel:1011", [])) == 0, "refusal handled, turn dropped")

    # ---- 10. web fetch / pause_turn with server tool blocks (lenient parsing on old SDKs)
    m10 = FakeMessage("read https://example.com", guild=guild, channel=FakeChannel(cid=1012, guild=guild))
    script.append(msg([{"type": "text", "text": "Reading. "},
                       {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_fetch", "input": {"url": "https://example.com"}},
                       {"type": "web_fetch_tool_result", "tool_use_id": "srvtoolu_1",
                        "content": {"type": "web_fetch_result", "url": "https://example.com", "retrieved_at": None,
                                    "content": {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "Example"}}}}],
                      stop="pause_turn",
                      usage={"input_tokens": 5, "output_tokens": 5, "server_tool_use": {"web_fetch_requests": 1}}))
    script.append(text("It says Example."))
    reply, _ = await bot.ask_claude(ctx_for(m10), await bot.build_user_content(m10, m10.content))
    check(reply == "Reading. It says Example.", f"pause_turn continued: {reply!r}")
    check(requests[-1]["body"]["messages"][-1]["role"] == "assistant", "pause_turn re-sent assistant content")
    check(bot.usage_stats["web_fetches"] == 1, "web fetch counted in usage")

    # ---- 11. memory save/load roundtrip keeps notes + summaries
    bot.summaries["channel:1"] = "they said hi"
    bot.summary_touched["channel:1"] = time.time()
    payload = bot._snapshot_memory()
    check(bot._write_memory(payload), "memory written")
    data = json.loads(open(bot.MEMORY_PATH).read())
    check(data["user_notes"]["1"] and data["server_notes"]["77"] and data["summaries"]["channel:1"] == "they said hi",
          "notes + summaries in memory.json")
    saved_old = data["history"].get(key8, [])
    check(all(b_.get("type") != "document" for t in saved_old if isinstance(t["content"], list) for b_ in t["content"]),
          "documents stripped from saved memory")
    bot.user_notes.clear(); bot.server_notes.clear(); bot.summaries.clear(); bot.summary_touched.clear()
    bot._memory_loaded = False
    bot.load_memory()
    check(bot.user_notes.get(1) and bot.server_notes.get(77) and bot.summaries.get("channel:1") == "they said hi",
          "notes + summaries reloaded")
    bot.load_config()
    check(bot.reminder_seq >= 1, "reminder_seq restored from channels.json")

    # ---- 12. commands: /memory view + forget button, /reminders list + cancel
    names_ = sorted(c.name for c in bot.tree.get_commands())
    check({"memory", "reminders", "tldr", "usage"} <= set(names_), f"commands registered ({len(names_)})")
    it = FakeInteraction(guild=guild)
    await bot.memory_cmd.callback(it, None)
    emb = it.response.sent[0][1]["embed"]
    view = it.response.sent[0][1]["view"]
    check("What I remember about Bob" in emb.title and "Mochi" in (emb.description or "") or "fact" in (emb.description or ""),
          "/memory shows notes")
    check(it.response.sent[0][1].get("ephemeral") is True, "/memory is private")
    it2 = FakeInteraction(guild=guild)
    it2.user = it.user
    await view.forget_user.callback(it2)
    check(1 not in bot.user_notes and it2.response.sent and "Forgot" in it2.response.sent[0][1]["content"], "forget button clears notes")
    other = FakeInteraction(user_id=5, name="Eve", guild=guild, manage=False)
    await bot.memory_cmd.callback(other, Obj(id=1, display_name="Bob", name="Bob"))
    check("only look at your own" in other.response.sent[0][0], "/memory on others needs Manage Channels")

    bot.add_reminder(channel_id=1007, guild_id=77, key="channel:1007", user_id=1, user_name="Bob",
                     text="water plants", minutes=30, by=1)
    it3 = FakeInteraction(guild=guild)
    await bot.reminders_cmd.callback(it3, None)
    check("water plants" in it3.response.sent[0][0], "/reminders lists")
    rid = next(r["id"] for r in bot.reminders if r["text"] == "water plants")
    it4 = FakeInteraction(user_id=9, name="Zed", guild=guild, manage=False)
    await bot.reminders_cmd.callback(it4, rid)
    check("isn't yours" in it4.response.sent[0][0], "/reminders cancel blocked for others")
    it5 = FakeInteraction(guild=guild)
    await bot.reminders_cmd.callback(it5, rid)
    check("Cancelled" in it5.response.sent[0][0] and not any(r["id"] == rid for r in bot.reminders), "/reminders cancel works")

    # ---- 13. API rejects optional settings -> switched off and retried
    if bot.USE_FALLBACKS and bot.EFFORT and bot.WEB_FETCH_ENABLED:
        m13 = FakeMessage("hi", guild=guild, channel=FakeChannel(cid=1013, guild=guild))
        script.append(err(400, "fallbacks: unexpected value for this account"))
        script.append(err(400, "output_config.effort: Extra inputs are not permitted"))
        script.append(err(400, "tools.6: web_fetch_20260209 is not supported"))
        script.append(text("still alive"))
        reply, _ = await bot.ask_claude(ctx_for(m13), await bot.build_user_content(m13, "hi"))
        last = requests[-1]["body"]
        check(reply == "still alive" and "fallbacks" not in last and "output_config" not in last
              and "web_fetch" not in tool_names(), "recovered after rejected options")
        check(not bot.USE_FALLBACKS and bot.EFFORT is None and not bot.WEB_FETCH_ENABLED, "rejected options stay off")
        script.append(err(400, "messages: something else is wrong"))
        try:
            await bot.ask_claude(ctx_for(m13), await bot.build_user_content(m13, "hi"))
            check(False, "unrelated 400 raised")
        except bot.anthropic.BadRequestError:
            check(True, "unrelated 400 raised")

    # ---- 14. /mood in DMs answers once; /reset clears summary
    it6 = FakeInteraction()
    await bot.mood_cmd.callback(it6, Obj(value="feral"), None)
    check(len(it6.response.sent) == 1, "/mood in DM responds once")
    check(bot.MusicControls().is_persistent(), "MusicControls is persistent")

    check(not script, f"all scripted responses consumed ({len(script)} left)")
    await runner.cleanup()
    print("\nALL PASSED" if not failures else f"\nFAILURES ({len(failures)}): {failures}")


asyncio.run(main())
sys.exit(1 if failures else 0)
