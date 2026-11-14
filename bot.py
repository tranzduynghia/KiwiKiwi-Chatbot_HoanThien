"""
Discord bot hỏi đáp AI với Gemini API (free tier)
- /ask    : hỏi đáp + OCR + suy nghĩ sâu + tra cứu web (tuỳ chọn)
- /refresh: làm mới phiên chat + lưu trữ VĨNH VIỄN cuộc trò chuyện (kèm Discord ID) vào MongoDB
- /session: xem thông tin phiên chat hiện tại
- /status : RAM, uptime, tình trạng Gemini API + MongoDB, thống kê sử dụng
- Mỗi người 1 phiên riêng trên MongoDB Atlas; sau 2 giờ không chat phiên tự được lưu trữ + làm mới
- Có web server nhỏ (/ và /health) để Render + UptimeRobot giữ bot luôn thức
"""
import asyncio
import fnmatch
import io
import logging
import mimetypes
import os
import platform
import re
import sys
import time
import uuid
import weakref
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import certifi
import discord
import psutil
import pymongo
from aiohttp import web
from discord import app_commands
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pymongo import AsyncMongoClient

# --------------------------------------------------------------------------
# Cấu hình (đặt trong Environment Variables của Render)
# --------------------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", "10000"))  # cổng web server /health
GUILD_ID = os.getenv("GUILD_ID")  # tuỳ chọn: sync lệnh tức thì cho 1 server khi test
DEFAULT_TEXT_MODEL = os.getenv("DEFAULT_TEXT_MODEL", "").strip()  # tuỳ chọn: ép model đứng đầu chuỗi tự động


def _csv_env(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in re.split(r"[,\n]+", os.getenv(name, default)) if x.strip()]


# Model bị ẨN khỏi danh sách gợi ý của option `model` (vẫn nằm trong chuỗi tự động nếu đúng loại;
# hỗ trợ ký tự đại diện, ví dụ gemini-4-*). Đặt HIDDEN_MODELS="" để hiện tất cả.
HIDDEN_MODELS = _csv_env(
    "HIDDEN_MODELS",
    "gemini-2.5-flash,gemini-2.5-flash-lite,gemini-2.5-pro,gemini-nano-banana-2.1,"
    "gemini-omni-flash-preview,gemini-4-argon,gemini-4-carbon",
)
EXTRA_MODELS = _csv_env("EXTRA_MODELS", "gemini-4-hydrogen")  # model thêm tay vào gợi ý (luôn hiện, không bị ẩn)
# Ẩn TOÀN BỘ model hạng Pro (tên có "pro") khỏi danh sách gợi ý. Đặt HIDE_PRO_MODELS=0 để hiện lại.
HIDE_PRO_MODELS = os.getenv("HIDE_PRO_MODELS", "1").strip().lower() not in ("0", "false", "no", "")
AUTO_MODEL_CHAIN = _csv_env("AUTO_MODEL_CHAIN")  # tuỳ chọn: tự quy định thứ tự model tự động
USER_COOLDOWN = float(os.getenv("USER_COOLDOWN", "5"))  # giây giữa 2 lần /ask của 1 người
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "2"))  # số request Gemini chạy song song
MODEL_REFRESH_HOURS = float(os.getenv("MODEL_REFRESH_HOURS", "6"))
RAM_LIMIT_MB = os.getenv("RAM_LIMIT_MB")  # tuỳ chọn: ép giới hạn RAM hiển thị ở /status
MAX_FILE_MB = 10

# --- Phiên chat (MongoDB Atlas) ---
MONGODB_URI = os.getenv("MONGODB_URI")  # nếu bỏ trống: tắt bộ nhớ phiên
MONGODB_DB = os.getenv("MONGODB_DB", "gemini_bot")
SESSION_IDLE_HOURS = float(os.getenv("SESSION_IDLE_HOURS", "2"))  # không chat quá lâu -> phiên tự làm mới
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))  # số tin gần nhất giữ trong phiên (user+bot)
MAX_HISTORY_CHARS = int(os.getenv("MAX_HISTORY_CHARS", "24000"))  # tổng ký tự lịch sử gửi kèm mỗi lần hỏi
MAX_STORED_CHARS = 4000  # cắt mỗi tin nhắn khi lưu để tiết kiệm dung lượng/token
SESSION_TTL = timedelta(hours=SESSION_IDLE_HOURS)
SESSION_MAX_TURNS = int(os.getenv("SESSION_MAX_TURNS", "500"))  # quá số lượt này -> tự lưu trữ + mở phiên mới
SWEEP_MINUTES = float(os.getenv("SWEEP_MINUTES", "5"))  # chu kỳ quét phiên hết hạn để lưu trữ
MONGODB_QUOTA_MB = float(os.getenv("MONGODB_QUOTA_MB", "512"))  # dung lượng gói Atlas (hiển thị ở /status)

# Chỉ các Discord ID này được dùng /status (ngăn cách bằng dấu phẩy; đặt biến môi trường để đổi)
STATUS_ALLOWED_IDS = {
    int(x) for x in re.split(r"[,\s]+", os.getenv("STATUS_ALLOWED_IDS", "1240503090185109536,1568530089035956284"))
    if x.strip().isdigit()
}

FALLBACK_TEXT = ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-pro"]  # lite → flash → pro

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

BOT_VERSION = "2026.11.13-fresh-install"  # đổi mỗi lần sửa code để biết Northflank đang chạy bản nào
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

# Mặc định dùng phiên bản API mặc định của SDK (google-genai 3.x: "v1").
# Nếu một tính năng/model preview bị thiếu, đặt GEMINI_API_VERSION=v1beta để ghim lại.
GEMINI_API_VERSION = os.getenv("GEMINI_API_VERSION", "").strip()
gemini = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(api_version=GEMINI_API_VERSION) if GEMINI_API_VERSION else None,
)


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


def tier_of(model_id: str) -> Optional[str]:
    """Phân hạng model theo tên: lite < flash < pro. None nếu không thuộc 3 hạng này."""
    low = model_id.lower()
    if not low.startswith("gemini-"):
        return None
    if "lite" in low:
        return "lite"
    if "flash" in low:
        return "flash"
    if "pro" in low:
        return "pro"
    return None


def is_stable(model_id: str) -> bool:
    low = model_id.lower()
    return "preview" not in low and "exp" not in low


def is_hidden(model_id: str) -> bool:
    return any(fnmatch.fnmatch(model_id.lower(), pat.lower()) for pat in HIDDEN_MODELS)


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

    def auto_chain(self) -> list[str]:
        """Thứ tự model cho chế độ tự động: flash-lite → flash → pro (mỗi hạng chọn 1 model tốt nhất,
        ưu tiên bản ổn định, rồi bản mới nhất). Lấy từ danh sách API, không phụ thuộc HIDDEN_MODELS."""
        if AUTO_MODEL_CHAIN:
            chain = list(AUTO_MODEL_CHAIN)
        else:
            ids = [m.id for m in self.text]
            chain = []
            for tier in ("lite", "flash", "pro"):
                pool = [i for i in ids if tier_of(i) == tier]
                if pool:
                    chain.append(max(pool, key=lambda i: (is_stable(i), version_of(i), -len(i))))
        if DEFAULT_TEXT_MODEL:
            chain = [DEFAULT_TEXT_MODEL] + [m for m in chain if m != DEFAULT_TEXT_MODEL]
        return chain or list(FALLBACK_TEXT)

    def default_text(self) -> str:
        return self.auto_chain()[0]

    def choices(self) -> list[ModelInfo]:
        """Danh sách gợi ý cho option `model`: model từ API trừ model bị ẩn, cộng model thêm tay."""
        base = self.text or [ModelInfo(i, i, badge_for(i)) for i in FALLBACK_TEXT]
        out = [m for m in base if not is_hidden(m.id) and not (HIDE_PRO_MODELS and tier_of(m.id) == "pro")]
        have = {m.id for m in out}
        out += [ModelInfo(i, i, badge_for(i)) for i in EXTRA_MODELS if i not in have]
        return out


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
# Phiên chat + lưu trữ vĩnh viễn (MongoDB Atlas)
# - `sessions`: phiên ĐANG HOẠT ĐỘNG, mỗi người 1 document (_id = Discord user id), chỉ giữ
#   vài chục tin gần nhất làm ngữ cảnh cho AI.
# - `archives`: lưu trữ VĨNH VIỄN, mỗi phiên 1 document (_id = session_id) chứa TOÀN BỘ cuộc
#   trò chuyện + `user_id` (Discord ID). Mỗi lượt hỏi đáp được ghi vào đây ngay lập tức;
#   khi phiên kết thúc (/refresh, 2 giờ không chat, quá số lượt) chỉ đánh dấu ended_at/end_reason
#   rồi xoá bản "đang hoạt động". KHÔNG dùng TTL index để xoá, nên không mất dữ liệu.
# --------------------------------------------------------------------------
_user_locks: "weakref.WeakValueDictionary[int, asyncio.Lock]" = weakref.WeakValueDictionary()


def user_lock(uid: int) -> asyncio.Lock:
    """Mỗi người xử lý lần lượt từng lệnh để không ghi đè lịch sử của nhau."""
    lock = _user_locks.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[uid] = lock
    return lock


def _clip(text: str) -> str:
    return text if len(text) <= MAX_STORED_CHARS else text[:MAX_STORED_CHARS] + "…"


class SessionStore:
    def __init__(self):
        self.client: Optional[AsyncMongoClient] = None
        self.col = None  # sessions
        self.archives = None  # archives
        self.ok = False
        self.index_ok = False
        self.last_error: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(MONGODB_URI)

    async def _run(self, make_coro, default=None):
        """Chạy 1 thao tác MongoDB; lỗi thì ghi nhận và trả về default (bot vẫn chạy không có phiên)."""
        if self.col is None:
            return default
        try:
            result = await make_coro()
            self.ok = True
            self.last_error = None
            return result
        except Exception as e:  # noqa: BLE001
            self.ok = False
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            log.warning("MongoDB lỗi: %s", self.last_error)
            return default

    async def _ensure_index(self):
        if self.index_ok:
            return

        async def work():
            info = await self.col.index_information()
            old = info.get("expire_at_1")
            if old and "expireAfterSeconds" in old:
                # Bản cũ dùng TTL index để XOÁ phiên hết hạn -> sẽ làm mất dữ liệu chưa lưu trữ.
                await self.col.drop_index("expire_at_1")
                log.warning("Đã gỡ TTL index cũ của sessions (phiên hết hạn giờ được lưu trữ thay vì xoá)")
            await self.col.create_index("expire_at")
            await self.archives.create_index([("user_id", 1), ("created_at", -1)])
            await self.archives.create_index("user_id_str")
            return True

        self.index_ok = bool(await self._run(work))

    async def connect(self):
        if not self.enabled:
            log.info("Chưa đặt MONGODB_URI → tắt bộ nhớ phiên")
            return
        kwargs = {"serverSelectionTimeoutMS": 8000, "tz_aware": True}
        if MONGODB_URI.startswith("mongodb+srv://"):
            kwargs["tlsCAFile"] = certifi.where()
        self.client = AsyncMongoClient(MONGODB_URI, **kwargs)
        db = self.client[MONGODB_DB]
        self.col = db["sessions"]
        self.archives = db["archives"]
        await self._ensure_index()
        if self.ok:
            log.info("Đã kết nối MongoDB (db=%s), phiên tự lưu trữ + làm mới sau %g giờ", MONGODB_DB, SESSION_IDLE_HOURS)

    async def close(self):
        if self.client is not None:
            try:
                await self.client.close()
            except Exception:  # noqa: BLE001
                pass

    async def end_session(self, uid: int, reason: str, only_if_expired: bool = False) -> Optional[dict]:
        """Lưu trữ (đánh dấu kết thúc) rồi gỡ phiên đang hoạt động. reason: refresh | idle_timeout | max_turns.
        Trả về {session_id, turns} nếu đã kết thúc; None nếu không có phiên hoặc lỗi (xem self.ok)."""
        now = datetime.now(timezone.utc)
        flt: dict = {"_id": uid}
        if only_if_expired:
            flt["expire_at"] = {"$lte": now}
        doc = await self._run(lambda: self.col.find_one(flt))
        if not doc:
            return None
        sid = doc.get("session_id") or uuid.uuid4().hex
        ended_at = doc.get("expire_at", now) if reason == "idle_timeout" else now
        r = await self._run(lambda: self.archives.update_one(
            {"_id": sid},
            {"$set": {"ended_at": ended_at, "end_reason": reason},
             # chỉ dùng khi bản lưu trữ chưa tồn tại (phiên tạo từ phiên bản cũ)
             "$setOnInsert": {"user_id": uid, "user_id_str": str(uid), "created_at": doc.get("created_at", now),
                              "turns": doc.get("turns", 0), "messages": doc.get("messages", [])}},
            upsert=True))
        if r is None:
            return None  # lưu trữ lỗi -> giữ nguyên phiên, lần quét sau sẽ thử lại
        r = await self._run(lambda: self.col.delete_one({"_id": uid, "session_id": doc.get("session_id")}))
        if r is None:
            return None
        return {"session_id": sid, "turns": doc.get("turns", 0)}

    async def load(self, uid: int) -> Optional[dict]:
        """Trả về phiên còn hạn. Phiên đã hết hạn (hoặc quá số lượt) sẽ được lưu trữ rồi coi như chưa có."""
        doc = await self._run(lambda: self.col.find_one({"_id": uid}))
        if not doc:
            return None
        last = doc.get("last_active")
        if last and datetime.now(timezone.utc) - last > SESSION_TTL:
            await self.end_session(uid, "idle_timeout")
            return None
        if doc.get("turns", 0) >= SESSION_MAX_TURNS:
            await self.end_session(uid, "max_turns")
            return None
        return doc

    async def archive_expired(self) -> int:
        """Quét các phiên đã quá hạn (người dùng không quay lại) và lưu trữ chúng."""
        now = datetime.now(timezone.utc)
        docs = await self._run(
            lambda: self.col.find({"expire_at": {"$lte": now}}, {"_id": 1}).to_list(200), [])
        count = 0
        for d in docs or []:
            async with user_lock(d["_id"]):
                if await self.end_session(d["_id"], "idle_timeout", only_if_expired=True):
                    count += 1
        if count:
            log.info("Đã lưu trữ %d phiên hết hạn", count)
        return count

    async def save_turn(self, uid: int, user_text: str, model_text: str,
                        session_doc: Optional[dict], model_id: str) -> Optional[int]:
        """Ghi 1 lượt hỏi đáp. Trả về số lượt của phiên sau khi ghi, hoặc None nếu lỗi."""
        await self._ensure_index()
        now = datetime.now(timezone.utc)
        fresh = session_doc is None
        sid = uuid.uuid4().hex if fresh else (session_doc.get("session_id") or uuid.uuid4().hex)
        created = now if fresh else session_doc.get("created_at", now)

        # 1) lưu trữ vĩnh viễn: toàn văn, gắn Discord ID
        r = await self._run(lambda: self.archives.update_one(
            {"_id": sid},
            {"$push": {"messages": {"$each": [
                {"role": "user", "text": user_text, "at": now},
                {"role": "model", "text": model_text, "at": now, "model": model_id}]}},
             "$set": {"last_active": now},
             "$inc": {"turns": 1},
             "$setOnInsert": {"user_id": uid, "user_id_str": str(uid), "created_at": created,
                              "ended_at": None, "end_reason": None}},
            upsert=True))
        if r is None:
            return None

        # 2) phiên đang hoạt động: chỉ giữ ngữ cảnh gần nhất, cắt ngắn từng tin
        expire = now + SESSION_TTL
        ctx = [{"role": "user", "text": _clip(user_text)}, {"role": "model", "text": _clip(model_text)}]
        if fresh:
            turns = 1
            doc = {"_id": uid, "session_id": sid, "messages": ctx, "turns": 1,
                   "created_at": now, "last_active": now, "expire_at": expire}
            r = await self._run(lambda: self.col.replace_one({"_id": uid}, doc, upsert=True))
        else:
            turns = session_doc.get("turns", 0) + 1
            r = await self._run(lambda: self.col.update_one(
                {"_id": uid},
                {"$push": {"messages": {"$each": ctx, "$slice": -MAX_HISTORY_MESSAGES}},
                 "$set": {"last_active": now, "expire_at": expire, "session_id": sid},
                 "$inc": {"turns": 1},
                 "$setOnInsert": {"created_at": created}},
                upsert=True))
        return turns if r is not None else None

    async def active_count(self) -> Optional[int]:
        now = datetime.now(timezone.utc)
        return await self._run(lambda: self.col.count_documents({"expire_at": {"$gt": now}}))

    async def archive_count(self) -> Optional[int]:
        return await self._run(lambda: self.archives.estimated_document_count())

    async def storage_mb(self) -> Optional[float]:
        """Dung lượng database đã dùng (MB). Không ảnh hưởng cờ self.ok nếu lệnh bị từ chối."""
        if self.client is None:
            return None
        try:
            st = await asyncio.wait_for(self.client[MONGODB_DB].command("dbStats"), timeout=10)
            return (st.get("storageSize", 0) + st.get("indexSize", 0)) / 1024 / 1024
        except Exception:  # noqa: BLE001
            return None

    async def ping(self) -> tuple[bool, float, str]:
        if not self.enabled or self.client is None:
            return False, 0.0, "tắt"
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(self.client.admin.command("ping"), timeout=10)
            return True, (time.perf_counter() - t0) * 1000, "OK"
        except Exception as e:  # noqa: BLE001
            return False, (time.perf_counter() - t0) * 1000, type(e).__name__


sessions = SessionStore()


def history_to_contents(doc: Optional[dict]) -> list:
    """Đổi lịch sử đã lưu thành contents của Gemini, giới hạn theo số tin và tổng ký tự."""
    if not doc:
        return []
    msgs = (doc.get("messages") or [])[-MAX_HISTORY_MESSAGES:]
    kept, total = [], 0
    for m in reversed(msgs):
        total += len(m["text"])
        if total > MAX_HISTORY_CHARS and kept:
            break
        kept.append(m)
    kept.reverse()
    while kept and kept[0]["role"] != "user":  # lịch sử phải bắt đầu bằng lượt của người dùng
        kept.pop(0)
    return [types.Content(role=m["role"], parts=[types.Part(text=m["text"])]) for m in kept]

# --------------------------------------------------------------------------
# Gọi Gemini
# --------------------------------------------------------------------------
model_cooldowns: dict[str, float] = {}  # model -> mốc time.monotonic() được dùng lại
RETRYABLE_CODES = (404, 429, 500, 503)


def cooldown_left(model_id: str) -> float:
    return max(0.0, model_cooldowns.get(model_id, 0.0) - time.monotonic())


def is_retryable(e: Exception) -> bool:
    return isinstance(e, asyncio.TimeoutError) or getattr(e, "code", None) in RETRYABLE_CODES


def failure_reason(e: Exception) -> str:
    if isinstance(e, asyncio.TimeoutError):
        return "phản hồi quá lâu"
    code = getattr(e, "code", None)
    return {429: "hết lượt/quota", 500: "đang quá tải", 503: "đang quá tải", 404: "không còn tồn tại"}.get(code, f"lỗi {code}")


def cooldown_seconds(e: Exception) -> float:
    """Model vừa lỗi sẽ bị tạm bỏ qua bao lâu."""
    if isinstance(e, asyncio.TimeoutError):
        return 60.0
    code = getattr(e, "code", None)
    low = (getattr(e, "message", "") or "").lower()
    if code == 404:
        return 6 * 3600.0
    if code == 429:
        if "limit: 0" in low:
            return 3600.0  # model không có quota free
        if "perday" in low or "per day" in low:
            return 1800.0
        m = re.search(r"retry in ([\d.]+)s", low)
        return min(float(m.group(1)) + 1, 600.0) if m else 60.0
    return 60.0  # 500/503


def mark_failure(model_id: str, e: Exception):
    if is_retryable(e):
        model_cooldowns[model_id] = time.monotonic() + cooldown_seconds(e)


def chain_status_text() -> str:
    parts = []
    for m in registry.auto_chain():
        left = cooldown_left(m)
        parts.append(f"`{m}`" + (f" ⏳{left:.0f}s" if left > 0 else ""))
    return " → ".join(parts)


# Gemini 3.x nhận thinking_level trực tiếp; Gemini 2.5 dùng ngân sách token (thinking_budget).
THINKING_BUDGETS = {"low": 1024, "medium": 8192, "high": 24576}


def build_thinking_configs(model_id: str, level: str) -> list[tuple[str, types.ThinkingConfig]]:
    """Danh sách (mức thực tế, config) để thử lần lượt. Model không có mức `medium`/`xhigh` thì bot
    thử tiếp `high`. Model đời cũ (<2.5) không hỗ trợ thinking -> danh sách rỗng."""
    v = version_of(model_id)
    out: list[tuple[str, types.ThinkingConfig]] = []
    try:
        if level == "auto":  # để model tự quyết định mức suy nghĩ, vẫn lấy tóm tắt suy nghĩ
            if v >= 3:
                out.append(("auto", types.ThinkingConfig(include_thoughts=True)))
            elif v >= 2.5:
                out.append(("auto", types.ThinkingConfig(thinking_budget=-1, include_thoughts=True)))  # -1 = tự động
        elif v >= 3:
            # mức không phải model nào cũng có (medium: một số model Pro; xhigh: mới có ở Gemini 4 Hydrogen)
            # -> thử mức đã chọn trước, rồi `high`
            for lv in {"medium": ["medium", "high"], "xhigh": ["xhigh", "high"]}.get(level, [level]):
                try:
                    out.append((lv, types.ThinkingConfig(thinking_level=lv, include_thoughts=True)))
                except Exception as e:  # SDK cũ chưa biết mức này (vd xhigh) -> bỏ qua, dùng mức kế tiếp
                    log.warning("SDK không nhận thinking_level=%s: %s", lv, e)
        elif v >= 2.5:
            lv = "high" if level == "xhigh" else level  # Gemini 2.5 không có xhigh
            out.append((lv, types.ThinkingConfig(thinking_budget=THINKING_BUDGETS[lv], include_thoughts=True)))
    except Exception as e:  # SDK cũ không biết thinking_level
        log.warning("Không tạo được ThinkingConfig: %s", e)
    return out


async def call_gemini(model: str, contents: list, *, system: Optional[str], thinking_level: Optional[str],
                      web_search: bool, max_retries: int = 3):
    """Trả về (response, mức_thinking_thực_tế hoặc None). Tự retry khi 500/503, và nếu model từ chối
    cấu hình thinking (400) thì thử mức kế tiếp rồi bỏ thinking."""
    attempts: list[tuple[types.GenerateContentConfig, Optional[str]]] = []
    base: dict = {"system_instruction": system}
    if web_search:
        base["tools"] = [types.Tool(google_search=types.GoogleSearch())]
    if thinking_level:
        for used_level, tc in build_thinking_configs(model, thinking_level):
            attempts.append((types.GenerateContentConfig(**base, thinking_config=tc), used_level))
    attempts.append((types.GenerateContentConfig(**base), None))

    for idx, (cfg, thinking_used) in enumerate(attempts):
        for retry in range(max_retries):
            try:
                resp = await asyncio.wait_for(
                    gemini.aio.models.generate_content(model=model, contents=contents, config=cfg),
                    timeout=170,
                )
                return resp, thinking_used
            except genai_errors.APIError as e:
                if e.code in (500, 503) and retry < max_retries - 1:
                    await asyncio.sleep(2 * (retry + 1))
                    continue
                if e.code == 400 and idx < len(attempts) - 1:
                    log.info("400 với thinking config, thử lại không thinking: %s", e.message)
                    break
                raise
    raise RuntimeError("Không gọi được Gemini")


async def call_with_fallback(chain: list[str], contents: list, *, system: Optional[str],
                             thinking_level: Optional[str], web_search: bool):
    """Chế độ tự động: thử lần lượt các model trong chain (bỏ qua model đang bị tạm khoá do lỗi).
    Gặp quá tải/hết lượt/timeout thì khoá model đó một lúc và chuyển sang model kế tiếp.
    Trả về (response, thinking_used, model_dùng, {model: lý do bị bỏ qua})."""
    errors: dict[str, str] = {}
    ready = [m for m in chain if cooldown_left(m) <= 0]
    for m in chain:
        if m not in ready:
            errors[m] = f"đang chờ phục hồi ~{cooldown_left(m):.0f}s"
    if not ready:  # tất cả đang bị khoá -> thử model sắp hết khoá nhất
        ready = [min(chain, key=cooldown_left)]
    last_exc: Optional[Exception] = None
    for i, m in enumerate(ready):
        is_last = i == len(ready) - 1
        try:
            resp, thinking_used = await call_gemini(
                m, contents, system=system, thinking_level=thinking_level, web_search=web_search,
                max_retries=3 if is_last else 1)  # còn model dự phòng thì chuyển ngay, không chờ retry
            model_cooldowns.pop(m, None)
            return resp, thinking_used, m, errors
        except (genai_errors.APIError, asyncio.TimeoutError) as e:
            if not is_retryable(e):
                raise
            mark_failure(m, e)
            errors[m] = failure_reason(e)
            last_exc = e
            log.warning("Model %s lỗi (%s), thử model kế tiếp", m, errors[m])
    raise last_exc  # type: ignore[misc]


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
        self._cleaned_guild_cmds = False

    async def setup_hook(self):
        await self.start_web()  # mở cổng trước để nền tảng host nhận diện service đã chạy
        await registry.refresh()
        self.loop.create_task(self.model_refresher())
        self.loop.create_task(self.session_sweeper())
        self.loop.create_task(self.init_sessions())  # chạy nền: MongoDB chậm/lỗi không làm bot lên chậm
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            scope = f"CHỈ server có ID {GUILD_ID} (đang đặt GUILD_ID)"
        else:
            synced = await self.tree.sync()
            scope = "TOÀN CỤC (mọi server, có thể mất vài phút để Discord cập nhật)"
        log.info("Đã đồng bộ slash commands: %s — phạm vi: %s", ", ".join("/" + c.name for c in synced), scope)
        log.info("Phiên bản %s · google-genai %s (API %s) · discord.py %s · pymongo %s · RAM tiến trình: %s",
                 BOT_VERSION, getattr(genai, "__version__", "?"), GEMINI_API_VERSION or "mặc định SDK",
                 discord.__version__, pymongo.version, fmt_bytes(psutil.Process().memory_info().rss))

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

    async def init_sessions(self):
        await sessions.connect()
        await sessions.archive_expired()

    async def session_sweeper(self):
        """Định kỳ lưu trữ + làm mới các phiên quá 2 giờ không chat (kể cả khi người đó không quay lại)."""
        while not self.is_closed():
            await asyncio.sleep(SWEEP_MINUTES * 60)
            await sessions.archive_expired()

    async def cleanup_guild_commands(self):
        """Xoá lệnh đăng ký riêng cho từng server (còn sót từ lúc từng đặt GUILD_ID),
        nếu không mỗi lệnh sẽ hiện 2 lần cùng bản toàn cục. Chỉ chạy khi KHÔNG đặt GUILD_ID."""
        if GUILD_ID:
            return
        for guild in list(self.guilds):
            try:
                if await self.tree.fetch_commands(guild=guild):
                    self.tree.clear_commands(guild=guild)
                    await self.tree.sync(guild=guild)  # tree riêng của server đang rỗng -> xoá hết bản riêng
                    log.info("Đã xoá lệnh riêng còn sót ở server %s (%d)", guild.name, guild.id)
            except discord.HTTPException as e:
                log.warning("Không dọn được lệnh riêng ở server %s: %s", guild.id, e)
            await asyncio.sleep(1)

    async def on_ready(self):
        if not self._cleaned_guild_cmds:
            self._cleaned_guild_cmds = True
            self.loop.create_task(self.cleanup_guild_commands())
        self.ready_at = self.ready_at or time.time()
        log.info("Đăng nhập: %s (%d server)", self.user, len(self.guilds))

    async def close(self):
        await sessions.close()
        await super().close()


bot = AskBot()
psutil.Process().cpu_percent(None)  # khởi tạo bộ đo CPU


# --------------------------------------------------------------------------
# /ask
# --------------------------------------------------------------------------
@bot.tree.command(name="ask", description="Hỏi AI Gemini: OCR, suy nghĩ sâu, tra cứu web")
@app_commands.describe(
    prompt="Câu hỏi / yêu cầu của bạn",
    file="Ảnh hoặc PDF đính kèm để hỏi / OCR (tối đa 10MB)",
    model="Chọn model; bỏ trống = tự động (flash-lite → flash → pro, tự chuyển khi quá tải)",
    web="Tra cứu thông tin mới trên web (Google Search)",
    thinking_level="Suy nghĩ sâu: auto / low / medium / high / xhigh (xhigh chỉ vài model như Hydrogen)",
    ocr="Chỉ trích xuất chữ từ file đính kèm (OCR)",
)
async def ask(
    interaction: discord.Interaction,
    prompt: app_commands.Range[str, 1, 4000],
    file: Optional[discord.Attachment] = None,
    model: Optional[str] = None,
    web: bool = False,
    thinking_level: Optional[Literal["auto", "low", "medium", "high", "xhigh"]] = None,
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
    features = ["chat"] + [n for n, on in (("web", web), ("think", bool(thinking_level)), ("ocr", ocr), ("file", bool(file))) if on]

    # --- chọn model ---
    auto = not model or model.strip().lower() == "auto"
    chain = registry.auto_chain() if auto else [model.strip()]
    model_id = chain[0]

    lock = user_lock(uid)  # mỗi người xử lý lần lượt để lịch sử phiên không bị ghi đè
    await lock.acquire()
    try:
        user_parts: list = []
        if file:
            user_parts.append(await read_attachment(file))
        user_parts.append(types.Part(text=prompt))

        # --- nạp phiên của người dùng ---
        session_doc = await sessions.load(uid) if sessions.enabled else None
        can_save = sessions.enabled and sessions.ok
        if sessions.enabled and not sessions.ok:
            notes.append("Không kết nối được bộ nhớ phiên — lần trả lời này không có ngữ cảnh cũ")
        contents = history_to_contents(session_doc) + [types.Content(role="user", parts=user_parts)]

        system = f"{SYSTEM_PROMPT}\nHôm nay là {datetime.now(PACIFIC):%d/%m/%Y}."
        if ocr:
            system = f"{OCR_INSTRUCTION}\n\n{system}"

        errors: dict[str, str] = {}
        async with gemini_sem:
            if auto:
                resp, thinking_used, model_id, errors = await call_with_fallback(
                    chain, contents, system=system, thinking_level=thinking_level, web_search=web)
            else:
                try:
                    resp, thinking_used = await call_gemini(
                        model_id, contents, system=system, thinking_level=thinking_level, web_search=web)
                    model_cooldowns.pop(model_id, None)
                except (genai_errors.APIError, asyncio.TimeoutError) as e:
                    mark_failure(model_id, e)
                    raise
        out = parse_response(resp)
        if auto and model_id in chain:
            failed = [f"`{m}` ({errors[m]})" for m in chain[:chain.index(model_id)] if m in errors]
            if failed:
                notes.append("Tự động chuyển model: " + ", ".join(failed) + f" → `{model_id}`")
        if thinking_level and not thinking_used:
            notes.append(f"Model `{model_id}` không hỗ trợ `thinking_level` ({thinking_level}) nên đã bỏ qua")
        elif thinking_level and thinking_used != thinking_level:
            notes.append(f"Model `{model_id}` không có mức `{thinking_level}`, đã dùng mức `{thinking_used}`")

        if not out.text:
            reason = f"bị chặn ({out.blocked})" if out.blocked else f"finish_reason={out.finish}"
            raise UserError(f"Model không trả về nội dung — {reason}. Hãy thử diễn đạt lại.")

        # --- lưu lượt hội thoại vào phiên ---
        turns = 0
        if can_save:
            stored_user = ("[OCR] " if ocr else "") + (f"[Đính kèm: {file.filename}] " if file else "") + prompt
            turns = await sessions.save_turn(uid, stored_user, out.text, session_doc, model_id) or 0
            if not turns:
                notes.append("Không lưu được phiên/lưu trữ (lỗi MongoDB) — tin nhắn này sẽ không được nhớ")

        elapsed = time.perf_counter() - started
        meta = [f"🤖 {model_id}", f"⏱ {elapsed:.1f}s"]
        meta += [lbl for lbl, on in (("🌐 web", web), (f"🧠 {thinking_used}", bool(thinking_used)), ("🔎 OCR", ocr)) if on]
        if out.tokens:
            meta.append(f"{out.tokens:,} tokens")
        if turns:
            meta.append("🆕 phiên mới" if turns == 1 else f"💬 phiên: {turns} lượt")
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
    finally:
        lock.release()


@ask.autocomplete("model")
async def model_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower().strip()
    out = []
    if not cur or any(cur in k for k in ("auto", "tự động", "tu dong")):
        out.append(app_commands.Choice(name="🤖 Tự động (flash-lite → flash → pro)", value="auto"))
    for m in registry.choices():
        if cur in m.id.lower() or cur in m.display.lower():
            out.append(app_commands.Choice(name=f"{m.badge} {m.id}"[:100], value=m.id))
        if len(out) >= 25:
            break
    return out


# --------------------------------------------------------------------------
# /refresh + /session
# --------------------------------------------------------------------------
@bot.tree.command(name="refresh", description="Làm mới phiên chat và lưu trữ cuộc trò chuyện hiện tại")
async def refresh(interaction: discord.Interaction):
    if not sessions.enabled:
        return await interaction.response.send_message("ℹ️ Bot chưa bật bộ nhớ phiên.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    uid = interaction.user.id
    async with user_lock(uid):
        info = await sessions.end_session(uid, "refresh")
    if info:
        msg = (f"🔄 Đã làm mới phiên và **lưu trữ** cuộc trò chuyện ({info['turns']} lượt, "
               f"mã `{info['session_id'][:8]}`).\nCâu hỏi tiếp theo sẽ bắt đầu một phiên mới.")
    elif not sessions.ok:
        msg = "⚠️ Không kết nối được MongoDB, chưa làm mới được phiên. Hãy thử lại sau."
    else:
        msg = "ℹ️ Bạn chưa có phiên nào đang hoạt động, câu hỏi tiếp theo sẽ tự tạo phiên mới."
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="session", description="Xem thông tin phiên chat hiện tại của bạn")
async def session_info(interaction: discord.Interaction):
    if not sessions.enabled:
        return await interaction.response.send_message("ℹ️ Bot chưa bật bộ nhớ phiên.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with user_lock(interaction.user.id):
        doc = await sessions.load(interaction.user.id)
    if not sessions.ok:
        return await interaction.followup.send("⚠️ Không kết nối được MongoDB, thử lại sau.", ephemeral=True)
    if not doc:
        return await interaction.followup.send(
            "ℹ️ Bạn chưa có phiên nào đang hoạt động. Dùng `/ask` để bắt đầu phiên mới.", ephemeral=True)
    ts = lambda k: int(doc[k].timestamp())  # noqa: E731
    sid = (doc.get("session_id") or "")[:8] or "—"
    emb = discord.Embed(title="💬 Phiên chat của bạn", color=discord.Color.blurple())
    emb.add_field(name="Mã phiên", value=f"`{sid}`", inline=True)
    emb.add_field(name="Số lượt hỏi đáp", value=f"**{doc.get('turns', 0)}**", inline=True)
    emb.add_field(name="Tin nhắn đang nhớ", value=f"**{len(doc.get('messages', []))}** / {MAX_HISTORY_MESSAGES}", inline=True)
    emb.add_field(name="Bắt đầu", value=f"<t:{ts('created_at')}:R>", inline=True)
    emb.add_field(name="Chat gần nhất", value=f"<t:{ts('last_active')}:R>", inline=True)
    emb.add_field(name="Tự lưu trữ + làm mới", value=f"<t:{ts('expire_at')}:R> (nếu không chat thêm)", inline=True)
    emb.set_footer(text="Dùng /refresh để lưu trữ và làm mới ngay")
    await interaction.followup.send(embed=emb, ephemeral=True)


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
    if interaction.user.id not in STATUS_ALLOWED_IDS:
        return await interaction.response.send_message(
            "🔒 Bạn không có quyền dùng lệnh này.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)  # thông tin hệ thống: chỉ người dùng lệnh thấy
    (api_ok, api_ms, api_msg), db_info = await asyncio.gather(check_gemini_api(), sessions.ping())
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
        f"Model tự động: {chain_status_text()}\n"
        f"Danh sách model: **{len(registry.text)}** model chat (cập nhật {cache_age})"
        + (f"\n⚠️ Lỗi tải model: {registry.last_error}" if registry.last_error else "")))

    if not sessions.enabled:
        db_text = "⚪ Chưa bật (thiếu biến `MONGODB_URI`)"
    else:
        db_ok, db_ms, db_msg = db_info
        active = archived = storage = None
        if db_ok:
            active, archived, storage = await asyncio.gather(
                sessions.active_count(), sessions.archive_count(), sessions.storage_mb())
        q = lambda v: "?" if v is None else v  # noqa: E731
        db_text = (
            f"Trạng thái: {'🟢 **Hoạt động**' if db_ok else '🔴 **Lỗi**'} ({db_msg}) · {db_ms:.0f} ms\n"
            f"Phiên đang hoạt động: **{q(active)}** · đã lưu trữ: **{q(archived)}** phiên\n"
            f"Tự lưu trữ + làm mới sau **{SESSION_IDLE_HOURS:g} giờ** không chat"
            + (f"\nDung lượng: **{storage:.1f} / {MONGODB_QUOTA_MB:g} MB** `{bar(storage / MONGODB_QUOTA_MB * 100)}`"
               if storage is not None else "")
            + (f"\n⚠️ Lỗi gần nhất: {sessions.last_error}" if sessions.last_error else ""))
    emb.add_field(name="🗄️ Phiên chat & lưu trữ (MongoDB Atlas)", inline=False, value=db_text)

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

    env_bits = [f"bản {BOT_VERSION}", f"Python {platform.python_version()}", f"discord.py {discord.__version__}"]
    if os.getenv("RENDER_SERVICE_NAME"):
        env_bits.append(f"Render: {os.getenv('RENDER_SERVICE_NAME')}")
    if os.getenv("RENDER_GIT_COMMIT"):
        env_bits.append(f"commit {os.getenv('RENDER_GIT_COMMIT')[:7]}")
    emb.set_footer(text=" · ".join(env_bits) + "  |  Gemini không cung cấp API xem quota còn lại")
    await interaction.followup.send(embed=emb, ephemeral=True)


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
