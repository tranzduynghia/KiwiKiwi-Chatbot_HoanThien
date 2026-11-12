"""
Discord bot hỏi đáp AI với Gemini API (free tier)
- /ask    : hỏi đáp + OCR + suy nghĩ sâu + tra cứu web (tuỳ chọn)
- /status : RAM, uptime, tình trạng Gemini API, thống kê sử dụng
- Có web server nhỏ (/ và /health) để Render + UptimeRobot giữ bot luôn thức
"""
import asyncio
import io
import logging
import mimetypes
import os
import platform
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import discord
import psutil
from aiohttp import web
from discord import app_commands
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

# --------------------------------------------------------------------------
# Cấu hình (đặt trong Environment Variables của Render)
# --------------------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", "10000"))  # cổng web server /health
GUILD_ID = os.getenv("GUILD_ID")  # tuỳ chọn: sync lệnh tức thì cho 1 server khi test
DEFAULT_TEXT_MODEL = os.getenv("DEFAULT_TEXT_MODEL", "").strip()
USER_COOLDOWN = float(os.getenv("USER_COOLDOWN", "5"))  # giây giữa 2 lần /ask của 1 người
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))  # số request Gemini chạy song song
MODEL_REFRESH_HOURS = float(os.getenv("MODEL_REFRESH_HOURS", "6"))
RAM_LIMIT_MB = os.getenv("RAM_LIMIT_MB")  # tuỳ chọn: ép giới hạn RAM hiển thị ở /status
MAX_FILE_MB = 10

FALLBACK_TEXT = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"]

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Bạn là trợ lý AI trong Discord. Trả lời bằng đúng ngôn ngữ người dùng đang dùng "
    "(mặc định tiếng Việt), rõ ràng, súc tích. Dùng Markdown của Discord "
    "(in đậm, danh sách, code block); KHÔNG dùng bảng Markdown vì Discord không hiển thị được.",
)
OCR_INSTRUCTION = (
    "Bạn là công cụ OCR. Hãy chép lại CHÍNH XÁC toàn bộ chữ có trong tệp đính kèm, giữ nguyên "
    "thứ tự và bố cục (dùng danh sách/code block nếu cần, bảng thì trình bày dạng dòng chữ). "
    "Không tự thêm bình luận. Nếu người dùng có yêu cầu thêm, hãy làm sau khi chép xong."
)

START_TIME = time.time()
NO_MENTIONS = discord.AllowedMentions.none()  # chặn @everyone/@role do AI sinh ra
PACIFIC = ZoneInfo("America/Los_Angeles")  # quota Gemini reset lúc nửa đêm giờ Pacific

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("gemini-bot")

if not DISCORD_TOKEN or not GEMINI_API_KEY:
    sys.exit("Thiếu biến môi trường DISCORD_TOKEN hoặc GEMINI_API_KEY")

gemini = genai.Client(api_key=GEMINI_API_KEY)


class UserError(Exception):
    """Lỗi có thể hiển thị thẳng cho người dùng."""


# --------------------------------------------------------------------------
# Danh sách model: tự động lấy từ Gemini API (models.list)
# --------------------------------------------------------------------------
NON_CHAT_KEYWORDS = (
    "embedding", "aqa", "tts", "live", "audio", "imagen", "veo", "lyria",
    "robotics", "computer-use", "gemma", "learnlm", "deep-research", "image",
)


def version_of(model_id: str) -> float:
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", model_id.lower())
    return float(m.group(1)) if m else 0.0


def badge_for(model_id: str) -> str:
    """Gợi ý (heuristic) - Google không trả về thông tin free/paid qua API."""
    low = model_id.lower()
    if "pro" in low and version_of(low) >= 3:
        return "💳"
    if "preview" in low or "exp" in low:
        return "🧪"
    return "🆓"


@dataclass
class ModelInfo:
    id: str
    display: str
    badge: str


class ModelRegistry:
    def __init__(self):
        self.text: list[ModelInfo] = []
        self.updated_at: float = 0.0
        self.last_error: Optional[str] = None

    async def refresh(self):
        text = []
        try:
            pager = await gemini.aio.models.list(config={"page_size": 100})
            async for m in pager:
                if "generateContent" not in (m.supported_actions or []):
                    continue
                mid = (m.name or "").removeprefix("models/")
                low = mid.lower()
                if not mid.startswith("gemini") or any(k in low for k in NON_CHAT_KEYWORDS):
                    continue
                info = ModelInfo(mid, m.display_name or mid, badge_for(mid))
                text.append(info)
            key = lambda i: (-version_of(i.id), i.id)  # noqa: E731
            text.sort(key=key)
            if text:
                self.text = text
                self.updated_at = time.time()
                self.last_error = None
                log.info("Đã tải %d model chat", len(text))
        except Exception as e:  # giữ cache cũ nếu lỗi
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            log.warning("Không tải được danh sách model: %s", self.last_error)

    def default_text(self) -> str:
        if DEFAULT_TEXT_MODEL:
            return DEFAULT_TEXT_MODEL
        ids = [m.id for m in self.text]
        flash = [i for i in ids if "flash" in i and "lite" not in i and "latest" not in i]
        pool = flash or ids
        stable = [i for i in pool if "preview" not in i and "exp" not in i]
        pool = stable or pool
        if not pool:
            return FALLBACK_TEXT[0]
        return max(pool, key=lambda i: (version_of(i), -len(i)))

    def choices(self) -> list[ModelInfo]:
        if self.text:
            return self.text
        return [ModelInfo(i, i, badge_for(i)) for i in FALLBACK_TEXT]


registry = ModelRegistry()

# --------------------------------------------------------------------------
# Thống kê sử dụng (chỉ tính request đi qua bot này)
# --------------------------------------------------------------------------
class Stats:
    def __init__(self):
        self.total = 0
        self.ok = 0
        self.errors = 0
        self.features: Counter = Counter()
        self.daily_day = ""
        self.daily_models: Counter = Counter()
        self.last_error: Optional[tuple[float, str]] = None

    def _roll_day(self):
        today = datetime.now(PACIFIC).strftime("%Y-%m-%d")
        if today != self.daily_day:
            self.daily_day = today
            self.daily_models = Counter()

    def record(self, model: str, ok: bool, features: list[str], err: Optional[str] = None):
        self._roll_day()
        self.total += 1
        self.daily_models[model] += 1
        self.features.update(features)
        if ok:
            self.ok += 1
        else:
            self.errors += 1
            self.last_error = (time.time(), err or "?")

    def today_total(self) -> int:
        self._roll_day()
        return sum(self.daily_models.values())


stats = Stats()
cooldowns: dict[int, float] = {}
gemini_sem = asyncio.Semaphore(MAX_CONCURRENT)

# --------------------------------------------------------------------------
# Gọi Gemini
# --------------------------------------------------------------------------
def build_thinking_config(model_id: str) -> Optional[types.ThinkingConfig]:
    """Gemini 3.x dùng thinking_level, Gemini 2.5 dùng thinking_budget."""
    v = version_of(model_id)
    try:
        if v >= 3:
            return types.ThinkingConfig(thinking_level="high", include_thoughts=True)
        if v >= 2.5:
            return types.ThinkingConfig(thinking_budget=-1, include_thoughts=True)  # -1 = tự động
    except Exception as e:  # SDK cũ không biết thinking_level
        log.warning("Không tạo được ThinkingConfig: %s", e)
    return None


async def call_gemini(model: str, contents: list, *, system: Optional[str], think: bool,
                      web_search: bool):
    """Trả về (response, thinking_used). Tự retry khi 500/503 và bỏ thinking nếu model không hỗ trợ."""
    attempts: list[tuple[types.GenerateContentConfig, bool]] = []
    base: dict = {"system_instruction": system}
    if web_search:
        base["tools"] = [types.Tool(google_search=types.GoogleSearch())]
    if think:
        tc = build_thinking_config(model)
        if tc:
            attempts.append((types.GenerateContentConfig(**base, thinking_config=tc), True))
    attempts.append((types.GenerateContentConfig(**base), False))

    for idx, (cfg, thinking_used) in enumerate(attempts):
        for retry in range(3):
            try:
                resp = await asyncio.wait_for(
                    gemini.aio.models.generate_content(model=model, contents=contents, config=cfg),
                    timeout=170,
                )
                return resp, thinking_used
            except genai_errors.APIError as e:
                if e.code in (500, 503) and retry < 2:
                    await asyncio.sleep(2 * (retry + 1))
                    continue
                if e.code == 400 and idx < len(attempts) - 1:
                    log.info("400 với thinking config, thử lại không thinking: %s", e.message)
                    break
                raise
    raise RuntimeError("Không gọi được Gemini")


@dataclass
class Parsed:
    text: str
    thoughts: str
    sources: list[tuple[str, str]]
    queries: list[str]
    finish: str
    blocked: Optional[str]
    tokens: Optional[int]


def parse_response(resp) -> Parsed:
    texts, thoughts, sources, queries = [], [], [], []
    cand = resp.candidates[0] if resp.candidates else None
    if cand and cand.content and cand.content.parts:
        for p in cand.content.parts:
            if p.text and p.thought:
                thoughts.append(p.text)
            elif p.text:
                texts.append(p.text)
    gm = getattr(cand, "grounding_metadata", None) if cand else None
    if gm:
        seen = set()
        for ch in gm.grounding_chunks or []:
            if ch.web and ch.web.uri and ch.web.uri not in seen:
                seen.add(ch.web.uri)
                sources.append((ch.web.title or ch.web.uri, ch.web.uri))
        queries = list(gm.web_search_queries or [])
    finish = cand.finish_reason.name if cand and cand.finish_reason else "?"
    block = None
    pf = getattr(resp, "prompt_feedback", None)
    if pf and pf.block_reason:
        block = pf.block_reason.name
    um = getattr(resp, "usage_metadata", None)
    return Parsed(
        text="".join(texts).strip(), thoughts="\n".join(thoughts).strip(),
        sources=sources, queries=queries, finish=finish, blocked=block,
        tokens=getattr(um, "total_token_count", None),
    )


async def read_attachment(att: discord.Attachment) -> types.Part:
    if att.size > MAX_FILE_MB * 1024 * 1024:
        raise UserError(f"File quá lớn (tối đa {MAX_FILE_MB}MB).")
    mime = (att.content_type or "").split(";")[0] or mimetypes.guess_type(att.filename)[0] or ""
    if not (mime.startswith("image/") or mime == "application/pdf"):
        raise UserError("Chỉ hỗ trợ file ảnh (png/jpg/webp...) hoặc PDF.")
    data = await att.read()
    return types.Part.from_bytes(data=data, mime_type=mime)


def explain_error(e: Exception) -> str:
    if isinstance(e, UserError):
        return f"❌ {e}"
    if isinstance(e, asyncio.TimeoutError):
        return "⌛ Gemini phản hồi quá lâu, hãy thử lại hoặc chọn model nhẹ hơn."
    if isinstance(e, genai_errors.APIError):
        msg = e.message or str(e)
        if e.code == 429:
            if "limit: 0" in msg:
                return ("🚫 Model này **không có quota free** (limit: 0). "
                        "Hãy chọn model có nhãn 🆓 hoặc bật billing cho API key.")
            m = re.search(r"retry in ([\d.]+)s", msg, re.I)
            hint = f" Thử lại sau ~{float(m.group(1)):.0f}s." if m else ""
            return f"🚦 Hết giới hạn tốc độ/quota free của Gemini.{hint} Có thể đổi model khác."
        if e.code in (401, 403):
            return "🔑 API key không hợp lệ/không có quyền dùng model này (hoặc khu vực bị chặn)."
        if e.code == 404:
            return "❓ Model không tồn tại hoặc đã bị gỡ. Hãy chọn lại model từ gợi ý."
        if e.code in (500, 503):
            return "🔥 Gemini đang quá tải, hãy thử lại sau ít phút."
        return f"⚠️ Lỗi Gemini ({e.code}): {msg[:300]}"
    return f"⚠️ Lỗi không xác định: {type(e).__name__}"


# --------------------------------------------------------------------------
# Tiện ích gửi tin nhắn Discord
# --------------------------------------------------------------------------
def chunk_text(text: str, limit: int = 1900) -> list[str]:
    """Cắt theo dòng, giữ nguyên code block khi bị cắt giữa chừng."""
    chunks: list[str] = []
    reopen = False
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit * 0.5:
            cut = text.rfind(" ", 0, limit)
        if cut < limit * 0.5:
            cut = limit
        piece, text = text[:cut], text[cut:].lstrip("\n")
        if reopen:
            piece = "```\n" + piece
        reopen = piece.count("```") % 2 == 1
        if reopen:
            piece += "\n```"
        chunks.append(piece)
    if text.strip():
        chunks.append(("```\n" + text) if reopen else text)
    return chunks


def clean_title(title: str) -> str:
    """Bỏ ký tự làm hỏng link Markdown và cắt ngắn tiêu đề nguồn."""
    return re.sub(r"[\[\]\n]", "", title)[:40]


def fmt_bytes(n: float) -> str:
    return f"{n / 1024 / 1024:.0f} MB"


def fmt_duration(sec: float) -> str:
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    return " ".join(x for x in (f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else "", f"{s}s") if x)


def bar(pct: float, width: int = 10) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = round(width * pct / 100)
    return "█" * filled + "░" * (width - filled) + f" {pct:.0f}%"


# --------------------------------------------------------------------------
# Bot + web server giữ sống
# --------------------------------------------------------------------------
class AskBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.ready_at: Optional[float] = None

    async def setup_hook(self):
        await self.start_web()  # mở cổng trước để Render nhận diện service đã chạy
        await registry.refresh()
        self.loop.create_task(self.model_refresher())
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        log.info("Đã đồng bộ slash commands")

    async def start_web(self):
        async def health(_request):
            return web.json_response({
                "status": "ok",
                "discord_ready": self.is_ready(),
                "uptime_seconds": int(time.time() - START_TIME),
            })

        app = web.Application()
        app.router.add_get("/", health)  # tự hỗ trợ cả HEAD (UptimeRobot)
        app.router.add_get("/health", health)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        log.info("Web server chạy ở cổng %d", PORT)

    async def model_refresher(self):
        while not self.is_closed():
            await asyncio.sleep(MODEL_REFRESH_HOURS * 3600)
            await registry.refresh()

    async def on_ready(self):
        self.ready_at = self.ready_at or time.time()
        log.info("Đăng nhập: %s (%d server)", self.user, len(self.guilds))


bot = AskBot()
psutil.Process().cpu_percent(None)  # khởi tạo bộ đo CPU


# --------------------------------------------------------------------------
# /ask
# --------------------------------------------------------------------------
@bot.tree.command(name="ask", description="Hỏi AI Gemini: OCR, suy nghĩ sâu, tra cứu web")
@app_commands.describe(
    prompt="Câu hỏi / yêu cầu của bạn",
    file="Ảnh hoặc PDF đính kèm để hỏi / OCR (tối đa 10MB)",
    model="Chọn model Gemini (danh sách tự lấy từ API)",
    web="Tra cứu thông tin mới trên web (Google Search)",
    think="Suy nghĩ sâu và hiện tóm tắt quá trình suy nghĩ",
    ocr="Chỉ trích xuất chữ từ file đính kèm (OCR)",
)
async def ask(
    interaction: discord.Interaction,
    prompt: app_commands.Range[str, 1, 4000],
    file: Optional[discord.Attachment] = None,
    model: Optional[str] = None,
    web: bool = False,
    think: bool = False,
    ocr: bool = False,
):
    uid = interaction.user.id
    now = time.monotonic()
    wait = USER_COOLDOWN - (now - cooldowns.get(uid, -1e9))
    if wait > 0:
        return await interaction.response.send_message(
            f"⏳ Chậm lại chút nhé, thử lại sau {wait:.0f}s.", ephemeral=True)
    if ocr and not file:
        return await interaction.response.send_message(
            "❌ OCR cần đính kèm file ảnh/PDF vào option `file`.", ephemeral=True)
    cooldowns[uid] = now
    if len(cooldowns) > 2000:
        for k in [k for k, v in cooldowns.items() if now - v > 3600]:
            cooldowns.pop(k, None)

    await interaction.response.defer(thinking=True)
    started = time.perf_counter()
    notes: list[str] = []
    features = ["chat"] + [n for n, on in (("web", web), ("think", think), ("ocr", ocr), ("file", bool(file))) if on]

    # --- chọn model ---
    model_id = model or registry.default_text()

    try:
        parts: list = []
        if file:
            parts.append(await read_attachment(file))
        parts.append(prompt)

        system = f"{SYSTEM_PROMPT}\nHôm nay là {datetime.now(PACIFIC):%d/%m/%Y}."
        if ocr:
            system = f"{OCR_INSTRUCTION}\n\n{system}"

        async with gemini_sem:
            resp, thinking_used = await call_gemini(
                model_id, parts, system=system, think=think, web_search=web)
        out = parse_response(resp)
        if think and not thinking_used:
            notes.append("Model này không hỗ trợ/không trả về tóm tắt suy nghĩ")

        if not out.text:
            reason = f"bị chặn ({out.blocked})" if out.blocked else f"finish_reason={out.finish}"
            raise UserError(f"Model không trả về nội dung — {reason}. Hãy thử diễn đạt lại.")

        elapsed = time.perf_counter() - started
        meta = [f"🤖 {model_id}", f"⏱ {elapsed:.1f}s"]
        meta += [lbl for lbl, on in (("🌐 web", web), ("🧠 think", thinking_used), ("🔎 OCR", ocr)) if on]
        if out.tokens:
            meta.append(f"{out.tokens:,} tokens")
        footer = "-# " + " · ".join(meta)
        extras = []
        if out.queries:
            extras.append("-# 🔎 Đã tìm: " + "; ".join(q[:60] for q in out.queries[:3]))
        if out.sources:
            links = " · ".join(f"[{clean_title(t)}](<{u}>)" for t, u in out.sources[:4])
            extras.append("-# 📚 Nguồn: " + links)
        extras += [f"-# ⚠️ {n}" for n in notes]
        tail = "\n".join([footer] + extras)

        # --- gửi kết quả ---
        if out.thoughts:
            emb = discord.Embed(title="🧠 Tóm tắt quá trình suy nghĩ",
                                description=out.thoughts[:3500] + ("…" if len(out.thoughts) > 3500 else ""),
                                color=discord.Color.dark_grey())
            await interaction.followup.send(embed=emb, allowed_mentions=NO_MENTIONS)

        files = []
        chunks = chunk_text(out.text)
        if len(chunks) > 6:  # quá dài -> gửi dạng file
            files.append(discord.File(io.BytesIO(out.text.encode()), filename="answer.md"))
            chunks = [chunks[0] + "\n…\n*(Câu trả lời dài — xem đầy đủ trong file `answer.md`)*"]
        if chunks and len(chunks[-1]) + len(tail) + 1 <= 2000:
            chunks[-1] += "\n" + tail
        else:
            chunks.append(tail)

        for i, content in enumerate(chunks):
            last = i == len(chunks) - 1
            await interaction.followup.send(
                content=content, files=files if last else [], allowed_mentions=NO_MENTIONS)

        stats.record(model_id, True, features)

    except Exception as e:  # noqa: BLE001
        stats.record(model_id, False, features, f"{type(e).__name__}: {e}"[:200])
        if not isinstance(e, UserError):
            log.exception("Lỗi /ask")
        await interaction.followup.send(explain_error(e), allowed_mentions=NO_MENTIONS)


@ask.autocomplete("model")
async def model_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower().strip()
    out = []
    for m in registry.choices():
        if cur in m.id.lower() or cur in m.display.lower():
            out.append(app_commands.Choice(name=f"{m.badge} {m.id}"[:100], value=m.id))
        if len(out) >= 25:
            break
    return out


# --------------------------------------------------------------------------
# /status
# --------------------------------------------------------------------------
def memory_info() -> dict:
    proc = psutil.Process()
    rss = proc.memory_info().rss
    used = limit = None
    for used_p, limit_p in (
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),  # cgroup v2
        ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.limit_in_bytes"),  # v1
    ):
        try:
            with open(used_p) as f:
                u = int(f.read().strip())
            with open(limit_p) as f:
                raw = f.read().strip()
            lim = None if raw == "max" else int(raw)
            if lim and lim > 1 << 45:  # "không giới hạn"
                lim = None
            used, limit = u, lim
            break
        except Exception:  # noqa: BLE001
            continue
    if RAM_LIMIT_MB:
        limit = int(RAM_LIMIT_MB) * 1024 * 1024
    if used is None:
        vm = psutil.virtual_memory()
        used = vm.total - vm.available
        limit = limit or vm.total
    limit = limit or psutil.virtual_memory().total
    return {"rss": rss, "used": used, "limit": limit}


async def check_gemini_api() -> tuple[bool, float, str]:
    """Gọi models.get (không tốn quota sinh nội dung) để kiểm tra key + kết nối."""
    model = registry.default_text()
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(gemini.aio.models.get(model=model), timeout=10)
        return True, (time.perf_counter() - t0) * 1000, "OK"
    except genai_errors.APIError as e:
        return False, (time.perf_counter() - t0) * 1000, f"{e.code} {(e.message or '')[:80]}"
    except Exception as e:  # noqa: BLE001
        return False, (time.perf_counter() - t0) * 1000, type(e).__name__


@bot.tree.command(name="status", description="Xem tình trạng bot: RAM, uptime, Gemini API, thống kê")
async def status(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    api_ok, api_ms, api_msg = await check_gemini_api()
    mem = memory_info()
    proc = psutil.Process()
    pct = mem["used"] / mem["limit"] * 100
    ready_ts = int(bot.ready_at or START_TIME)

    color = discord.Color.green() if api_ok and pct < 85 else (
        discord.Color.orange() if api_ok else discord.Color.red())
    emb = discord.Embed(title="📊 Tình trạng bot", color=color, timestamp=discord.utils.utcnow())

    emb.add_field(name="🤖 Bot", inline=True, value=(
        f"Ping Discord: **{bot.latency * 1000:.0f} ms**\n"
        f"Uptime: **{fmt_duration(time.time() - START_TIME)}**\n"
        f"Online từ: <t:{ready_ts}:R>\n"
        f"Server: **{len(bot.guilds)}**"))
    emb.add_field(name="💾 RAM", inline=True, value=(
        f"Container: **{fmt_bytes(mem['used'])} / {fmt_bytes(mem['limit'])}**\n"
        f"`{bar(pct)}`\n"
        f"Còn dư: **{fmt_bytes(max(mem['limit'] - mem['used'], 0))}**\n"
        f"Tiến trình bot: {fmt_bytes(mem['rss'])}"))
    emb.add_field(name="⚙️ CPU", inline=True, value=(
        f"Tiến trình: **{proc.cpu_percent(None):.1f}%**\n"
        f"Luồng: {proc.num_threads()}"))

    cache_age = f"{fmt_duration(time.time() - registry.updated_at)} trước" if registry.updated_at else "chưa tải được"
    emb.add_field(name="🔌 Gemini API", inline=False, value=(
        f"Trạng thái: {'🟢 **Hoạt động**' if api_ok else '🔴 **Lỗi**'} ({api_msg}) · {api_ms:.0f} ms\n"
        f"Model mặc định: `{registry.default_text()}`\n"
        f"Danh sách model: **{len(registry.text)}** model chat (cập nhật {cache_age})"
        + (f"\n⚠️ Lỗi tải model: {registry.last_error}" if registry.last_error else "")))

    top = ", ".join(f"`{m}` ×{n}" for m, n in stats.daily_models.most_common(4)) or "chưa có"
    rate = f"{stats.ok / stats.total * 100:.0f}%" if stats.total else "—"
    feat = " · ".join(f"{k}: {v}" for k, v in stats.features.most_common()) or "—"
    last_err = (f"<t:{int(stats.last_error[0])}:R> — `{stats.last_error[1][:120]}`"
                if stats.last_error else "không có")
    emb.add_field(name="📈 Sử dụng (từ lúc bot khởi động)", inline=False, value=(
        f"Tổng request: **{stats.total}** · thành công **{rate}** · lỗi **{stats.errors}**\n"
        f"Hôm nay (giờ Pacific, mốc reset quota): **{stats.today_total()}** — {top}\n"
        f"Tính năng: {feat}\n"
        f"Lỗi gần nhất: {last_err}"))

    env_bits = [f"Python {platform.python_version()}", f"discord.py {discord.__version__}"]
    if os.getenv("RENDER_SERVICE_NAME"):
        env_bits.append(f"Render: {os.getenv('RENDER_SERVICE_NAME')}")
    if os.getenv("RENDER_GIT_COMMIT"):
        env_bits.append(f"commit {os.getenv('RENDER_GIT_COMMIT')[:7]}")
    emb.set_footer(text=" · ".join(env_bits) + "  |  Gemini không cung cấp API xem quota còn lại")
    await interaction.followup.send(embed=emb)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.error("Lỗi lệnh: %r", error)
    msg = "⚠️ Có lỗi khi xử lý lệnh, hãy thử lại."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
