"""
Discord bot hỏi đáp AI với Gemini API + Web Dashboard quản trị thời gian thực (MongoDB Atlas)
- /ask    : hỏi đáp + OCR + suy nghĩ sâu + tra cứu web (tuỳ chọn)
- /refresh: làm mới phiên chat + lưu trữ VĨNH VIỄN cuộc trò chuyện (kèm Discord ID) vào MongoDB
- /session: xem thông tin phiên chat hiện tại
- /status : RAM, uptime, tình trạng Gemini API + MongoDB, thống kê sử dụng (phân quyền động từ MongoDB)
- Toàn bộ cấu hình động lưu trên MongoDB Atlas (`bot_config`, `discord_permissions`, `dashboard_users`)
- Web Dashboard tối giản (FastAPI + Uvicorn) chạy chung tiến trình bot trên cổng PORT (kèm /health)
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
import uvicorn
from discord import app_commands
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pymongo import AsyncMongoClient

from config_store import ConfigStore, DynamicSemaphore
from dashboard import create_dashboard_app

# --------------------------------------------------------------------------
# Biến môi trường cốt lõi (khởi động tiến trình)
# --------------------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", "10000"))
GUILD_ID = os.getenv("GUILD_ID")
MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB = os.getenv("MONGODB_DB", "gemini_bot")
GEMINI_API_VERSION = os.getenv("GEMINI_API_VERSION", "").strip()

BOT_VERSION = "2026.11.24-dashboard-atlas"
START_TIME = time.time()
NO_MENTIONS = discord.AllowedMentions.none()
PACIFIC = ZoneInfo("America/Los_Angeles")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("gemini-bot")

if not DISCORD_TOKEN or not GEMINI_API_KEY:
    sys.exit("Thiếu biến môi trường DISCORD_TOKEN hoặc GEMINI_API_KEY")

gemini = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(api_version=GEMINI_API_VERSION) if GEMINI_API_VERSION else None,
)

# Kho cấu hình động & phân quyền dùng chung giữa Bot và Web Dashboard
config_store = ConfigStore(cache_ttl=5.0)


class UserError(Exception):
    """Lỗi có thể hiển thị thẳng cho người dùng."""


# --------------------------------------------------------------------------
# Danh sách model: tự động lấy từ Gemini API + cấu hình động từ MongoDB Atlas
# --------------------------------------------------------------------------
NON_CHAT_KEYWORDS = (
    "embedding", "aqa", "tts", "live", "audio", "imagen", "veo", "lyria",
    "robotics", "computer-use", "gemma", "learnlm", "deep-research", "image",
)


def version_of(model_id: str) -> float:
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", model_id.lower())
    return float(m.group(1)) if m else 0.0


def default_badge_for(model_id: str) -> str:
    """Gợi ý mặc định (heuristic) khi chưa cấu hình tag riêng trên Dashboard."""
    low = model_id.lower()
    if "pro" in low and version_of(low) >= 3:
        return "💳"
    if "preview" in low or "exp" in low:
        return "🧪"
    return "🆓"


def badge_for(model_id: str) -> str:
    """Ưu tiên tag/is_free cấu hình trên MongoDB Dashboard, fallback sang heuristic."""
    ov = config_store.model_override(model_id)
    if ov.get("tag"):
        return str(ov["tag"])
    if ov.get("is_free") is True:
        return "🆓"
    if ov.get("is_free") is False:
        return "💳"
    return default_badge_for(model_id)


def display_name_for(model_id: str, api_display: str = "") -> str:
    ov = config_store.model_override(model_id)
    if ov.get("display_name"):
        return str(ov["display_name"])
    return api_display or model_id


def tier_of(model_id: str) -> Optional[str]:
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
                info = ModelInfo(mid, m.display_name or mid, default_badge_for(mid))
                text.append(info)
            text.sort(key=lambda i: (-version_of(i.id), i.id))
            if text:
                self.text = text
                self.updated_at = time.time()
                self.last_error = None
                log.info("Đã tải %d model chat", len(text))
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            log.warning("Không tải được danh sách model: %s", self.last_error)

    def auto_chain(self) -> list[str]:
        """Thứ tự model cho chế độ tự động đọc trực tiếp từ cấu hình động MongoDB."""
        mcfg = config_store.models_cfg()
        auto_model_chain = mcfg.get("auto_model_chain") or []
        fallback_text = mcfg.get("fallback_text") or ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-4-hydrogen"]
        auto_last = (mcfg.get("auto_last_model") or "").strip()
        default_text_model = (mcfg.get("default_text_model") or "").strip()

        if auto_model_chain:
            chain = list(auto_model_chain)
        elif not self.text:
            chain = list(fallback_text)
        else:
            ids = [m.id for m in self.text]
            chain = []
            for tier in ("lite", "flash"):
                pool = [i for i in ids if tier_of(i) == tier]
                if pool:
                    chain.append(max(pool, key=lambda i: (is_stable(i), version_of(i), -len(i))))
            if auto_last and auto_last not in chain:
                chain.append(auto_last)

        if default_text_model:
            chain = [default_text_model] + [m for m in chain if m != default_text_model]
        return chain or list(fallback_text)

    def default_text(self) -> str:
        return self.auto_chain()[0]

    def all_known_models(self) -> list[dict]:
        """Danh sách toàn bộ model (từ API + extra_models + overrides) phục vụ bảng cấu hình trên Dashboard."""
        mcfg = config_store.models_cfg()
        hide_pro = bool(mcfg.get("hide_pro_models", True))
        extra_models = mcfg.get("extra_models") or []
        fallback_text = mcfg.get("fallback_text") or ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-4-hydrogen"]
        overrides = mcfg.get("overrides") or {}
        auto_chain_list = self.auto_chain()

        base_map: dict[str, tuple[str, str]] = {}
        for m in self.text:
            base_map[m.id] = (m.display, "api")
        if not base_map:
            for mid in fallback_text:
                base_map[mid] = (mid, "fallback")
        for mid in extra_models:
            if mid not in base_map:
                base_map[mid] = (mid, "extra")
        for mid in overrides:
            if mid not in base_map:
                base_map[mid] = (mid, "override")

        out = []
        for mid, (api_disp, source) in base_map.items():
            ov = config_store.model_override(mid)
            is_extra = mid in extra_models
            hidden_by_rule = config_store.is_model_hidden(mid) or (hide_pro and tier_of(mid) == "pro")
            # Model trong extra_models chỉ ẩn nếu admin bật explicit hidden=True trong overrides
            if is_extra and ov.get("hidden") is None:
                effective_hidden = False
            else:
                effective_hidden = hidden_by_rule
            out.append({
                "id": mid,
                "api_display": api_disp,
                "display_name": display_name_for(mid, api_disp),
                "badge": badge_for(mid),
                "default_badge": default_badge_for(mid),
                "source": source,
                "is_extra": is_extra,
                "tier": tier_of(mid) or "other",
                "hidden": effective_hidden,
                "in_auto_chain": mid in auto_chain_list,
                "cooldown_left": round(cooldown_left(mid), 1),
                "override": ov,
            })
        out.sort(key=lambda x: (x["hidden"], -version_of(x["id"]), x["id"]))
        return out

    def choices(self) -> list[ModelInfo]:
        """Danh sách gợi ý cho option `model` trên Discord (đọc trực tiếp từ cấu hình động)."""
        mcfg = config_store.models_cfg()
        hide_pro = bool(mcfg.get("hide_pro_models", True))
        extra_models = mcfg.get("extra_models") or []
        fallback_text = mcfg.get("fallback_text") or ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-4-hydrogen"]

        base = self.text or [ModelInfo(i, i, default_badge_for(i)) for i in fallback_text]
        out: list[ModelInfo] = []
        for m in base:
            ov = config_store.model_override(m.id)
            if ov.get("hidden") is True:
                continue
            if ov.get("hidden") is not False:
                if config_store.is_model_hidden(m.id) or (hide_pro and tier_of(m.id) == "pro"):
                    continue
            out.append(ModelInfo(m.id, display_name_for(m.id, m.display), badge_for(m.id)))

        have = {m.id for m in out}
        for mid in extra_models:
            if mid in have:
                continue
            ov = config_store.model_override(mid)
            if ov.get("hidden") is True:
                continue
            out.append(ModelInfo(mid, display_name_for(mid, mid), badge_for(mid)))
        return out


registry = ModelRegistry()


# --------------------------------------------------------------------------
# Thống kê sử dụng
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
gemini_sem = DynamicSemaphore(lambda: config_store.ask_cfg().get("max_concurrent", 2))
config_store.on_update(lambda _cfg: gemini_sem.notify_limit_changed())


# --------------------------------------------------------------------------
# Phiên chat + lưu trữ vĩnh viễn (MongoDB Atlas)
# --------------------------------------------------------------------------
_user_locks: "weakref.WeakValueDictionary[int, asyncio.Lock]" = weakref.WeakValueDictionary()


def user_lock(uid: int) -> asyncio.Lock:
    lock = _user_locks.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[uid] = lock
    return lock


def _clip(text: str) -> str:
    limit = int(config_store.session_cfg().get("max_stored_chars", 4000))
    return text if len(text) <= limit else text[:limit] + "…"


class SessionStore:
    def __init__(self):
        self.client: Optional[AsyncMongoClient] = None
        self.col = None
        self.archives = None
        self.ok = False
        self.index_ok = False
        self.last_error: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(MONGODB_URI)

    async def _run(self, make_coro, default=None):
        if self.col is None:
            return default
        try:
            result = await make_coro()
            self.ok = True
            self.last_error = None
            return result
        except Exception as e:
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
                await self.col.drop_index("expire_at_1")
                log.warning("Đã gỡ TTL index cũ của sessions")
            await self.col.create_index("expire_at")
            await self.archives.create_index([("user_id", 1), ("created_at", -1)])
            await self.archives.create_index("user_id_str")
            return True

        self.index_ok = bool(await self._run(work))

    async def connect(self):
        if not self.enabled:
            log.warning("Chưa đặt MONGODB_URI → tắt bộ nhớ phiên & lưu cấu hình động trên MongoDB Atlas")
            return
        kwargs = {"serverSelectionTimeoutMS": 8000, "tz_aware": True}
        if MONGODB_URI.startswith("mongodb+srv://"):
            kwargs["tlsCAFile"] = certifi.where()
        self.client = AsyncMongoClient(MONGODB_URI, **kwargs)
        db = self.client[MONGODB_DB]
        self.col = db["sessions"]
        self.archives = db["archives"]
        await self._ensure_index()
        await config_store.attach_db(db)
        if self.ok:
            idle_h = config_store.session_cfg().get("session_idle_hours", 2)
            log.info("Đã kết nối MongoDB Atlas (db=%s), phiên tự lưu trữ sau %g giờ", MONGODB_DB, idle_h)

    async def close(self):
        if self.client is not None:
            try:
                await self.client.close()
            except Exception:
                pass

    async def end_session(self, uid: int, reason: str, only_if_expired: bool = False) -> Optional[dict]:
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
             "$setOnInsert": {"user_id": uid, "user_id_str": str(uid), "created_at": doc.get("created_at", now),
                              "turns": doc.get("turns", 0), "messages": doc.get("messages", [])}},
            upsert=True))
        if r is None:
            return None
        r = await self._run(lambda: self.col.delete_one({"_id": uid, "session_id": doc.get("session_id")}))
        if r is None:
            return None
        return {"session_id": sid, "turns": doc.get("turns", 0)}

    async def load(self, uid: int) -> Optional[dict]:
        doc = await self._run(lambda: self.col.find_one({"_id": uid}))
        if not doc:
            return None
        scfg = config_store.session_cfg()
        ttl = timedelta(hours=float(scfg.get("session_idle_hours", 2.0)))
        max_turns = int(scfg.get("session_max_turns", 500))
        last = doc.get("last_active")
        if last and datetime.now(timezone.utc) - last > ttl:
            await self.end_session(uid, "idle_timeout")
            return None
        if doc.get("turns", 0) >= max_turns:
            await self.end_session(uid, "max_turns")
            return None
        return doc

    async def archive_expired(self) -> int:
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
        await self._ensure_index()
        scfg = config_store.session_cfg()
        ttl = timedelta(hours=float(scfg.get("session_idle_hours", 2.0)))
        max_hist_msgs = int(scfg.get("max_history_messages", 20))

        now = datetime.now(timezone.utc)
        fresh = session_doc is None
        sid = uuid.uuid4().hex if fresh else (session_doc.get("session_id") or uuid.uuid4().hex)
        created = now if fresh else session_doc.get("created_at", now)

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

        expire = now + ttl
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
                {"$push": {"messages": {"$each": ctx, "$slice": -max_hist_msgs}},
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
        if self.client is None:
            return None
        try:
            st = await asyncio.wait_for(self.client[MONGODB_DB].command("dbStats"), timeout=10)
            return (st.get("storageSize", 0) + st.get("indexSize", 0)) / 1024 / 1024
        except Exception:
            return None

    async def ping(self) -> tuple[bool, float, str]:
        if not self.enabled or self.client is None:
            return False, 0.0, "tắt"
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(self.client.admin.command("ping"), timeout=10)
            return True, (time.perf_counter() - t0) * 1000, "OK"
        except Exception as e:
            return False, (time.perf_counter() - t0) * 1000, type(e).__name__


sessions = SessionStore()


def history_to_contents(doc: Optional[dict]) -> list:
    if not doc:
        return []
    scfg = config_store.session_cfg()
    max_hist_msgs = int(scfg.get("max_history_messages", 20))
    max_hist_chars = int(scfg.get("max_history_chars", 24000))

    msgs = (doc.get("messages") or [])[-max_hist_msgs:]
    kept, total = [], 0
    for m in reversed(msgs):
        total += len(m["text"])
        if total > max_hist_chars and kept:
            break
        kept.append(m)
    kept.reverse()
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    return [types.Content(role=m["role"], parts=[types.Part(text=m["text"])]) for m in kept]


# --------------------------------------------------------------------------
# Gọi Gemini
# --------------------------------------------------------------------------
model_cooldowns: dict[str, float] = {}
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
    if isinstance(e, asyncio.TimeoutError):
        return 60.0
    code = getattr(e, "code", None)
    low = (getattr(e, "message", "") or "").lower()
    if code == 404:
        return 6 * 3600.0
    if code == 429:
        if "limit: 0" in low:
            return 3600.0
        if "perday" in low or "per day" in low:
            return 1800.0
        m = re.search(r"retry in ([\d.]+)s", low)
        return min(float(m.group(1)) + 1, 600.0) if m else 60.0
    return 60.0


def mark_failure(model_id: str, e: Exception):
    if is_retryable(e):
        model_cooldowns[model_id] = time.monotonic() + cooldown_seconds(e)


def chain_status_text() -> str:
    parts = []
    for m in registry.auto_chain():
        left = cooldown_left(m)
        parts.append(f"`{m}`" + (f" ⏳{left:.0f}s" if left > 0 else ""))
    return " → ".join(parts)


def build_thinking_configs(model_id: str, level: str) -> list[tuple[str, types.ThinkingConfig]]:
    budgets = config_store.ask_cfg().get("thinking_budgets") or {"low": 1024, "medium": 8192, "high": 24576}
    v = version_of(model_id)
    out: list[tuple[str, types.ThinkingConfig]] = []
    try:
        if level == "auto":
            if v >= 3:
                out.append(("auto", types.ThinkingConfig(include_thoughts=True)))
            elif v >= 2.5:
                out.append(("auto", types.ThinkingConfig(thinking_budget=-1, include_thoughts=True)))
        elif v >= 3:
            for lv in {"medium": ["medium", "high"], "xhigh": ["xhigh", "high"]}.get(level, [level]):
                try:
                    out.append((lv, types.ThinkingConfig(thinking_level=lv, include_thoughts=True)))
                except Exception as e:
                    log.warning("SDK không nhận thinking_level=%s: %s", lv, e)
        elif v >= 2.5:
            lv = "high" if level == "xhigh" else level
            out.append((lv, types.ThinkingConfig(thinking_budget=int(budgets.get(lv, 8192)), include_thoughts=True)))
    except Exception as e:
        log.warning("Không tạo được ThinkingConfig: %s", e)
    return out


async def call_gemini(model: str, contents: list, *, system: Optional[str], thinking_level: Optional[str],
                      web_search: bool, max_retries: Optional[int] = None, timeout_sec: Optional[float] = None):
    acfg = config_store.ask_cfg()
    retries = max_retries if max_retries is not None else int(acfg.get("max_retries", 3))
    timeout = timeout_sec if timeout_sec is not None else float(acfg.get("gemini_timeout_seconds", 170.0))

    attempts: list[tuple[types.GenerateContentConfig, Optional[str]]] = []
    base: dict = {"system_instruction": system}
    if web_search:
        base["tools"] = [types.Tool(google_search=types.GoogleSearch())]
    if thinking_level:
        for used_level, tc in build_thinking_configs(model, thinking_level):
            attempts.append((types.GenerateContentConfig(**base, thinking_config=tc), used_level))
    attempts.append((types.GenerateContentConfig(**base), None))

    for idx, (cfg, thinking_used) in enumerate(attempts):
        for retry in range(retries):
            try:
                resp = await asyncio.wait_for(
                    gemini.aio.models.generate_content(model=model, contents=contents, config=cfg),
                    timeout=timeout,
                )
                return resp, thinking_used
            except genai_errors.APIError as e:
                if e.code in (500, 503) and retry < retries - 1:
                    await asyncio.sleep(2 * (retry + 1))
                    continue
                if e.code == 400 and idx < len(attempts) - 1:
                    log.info("400 với thinking config, thử lại không thinking: %s", e.message)
                    break
                raise
    raise RuntimeError("Không gọi được Gemini")


async def call_with_fallback(chain: list[str], contents: list, *, system: Optional[str],
                             thinking_level: Optional[str], web_search: bool):
    acfg = config_store.ask_cfg()
    max_retries = int(acfg.get("max_retries", 3))
    errors: dict[str, str] = {}
    ready = [m for m in chain if cooldown_left(m) <= 0]
    for m in chain:
        if m not in ready:
            errors[m] = f"đang chờ phục hồi ~{cooldown_left(m):.0f}s"
    if not ready:
        ready = [min(chain, key=cooldown_left)]
    last_exc: Optional[Exception] = None
    for i, m in enumerate(ready):
        is_last = i == len(ready) - 1
        try:
            resp, thinking_used = await call_gemini(
                m, contents, system=system, thinking_level=thinking_level, web_search=web_search,
                max_retries=max_retries if is_last else 1)
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
    max_file_mb = float(config_store.ask_cfg().get("max_file_mb", 10.0))
    if att.size > max_file_mb * 1024 * 1024:
        raise UserError(f"File quá lớn (tối đa {max_file_mb:g}MB).")
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
# Tiện ích gửi tin nhắn Discord & đo tài nguyên
# --------------------------------------------------------------------------
def chunk_text(text: str, limit: int = 1900) -> list[str]:
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


def memory_info() -> dict:
    proc = psutil.Process()
    rss = proc.memory_info().rss
    used = limit = None
    for used_p, limit_p in (
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
        ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    ):
        try:
            with open(used_p) as f:
                u = int(f.read().strip())
            with open(limit_p) as f:
                raw = f.read().strip()
            lim = None if raw == "max" else int(raw)
            if lim and lim > 1 << 45:
                lim = None
            used, limit = u, lim
            break
        except Exception:
            continue
    ram_limit_mb = config_store.session_cfg().get("ram_limit_mb")
    if ram_limit_mb:
        limit = int(ram_limit_mb) * 1024 * 1024
    if used is None:
        vm = psutil.virtual_memory()
        used = vm.total - vm.available
        limit = limit or vm.total
    limit = limit or psutil.virtual_memory().total
    return {"rss": rss, "used": used, "limit": limit}


async def check_gemini_api() -> tuple[bool, float, str]:
    model = registry.default_text()
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(gemini.aio.models.get(model=model), timeout=10)
        return True, (time.perf_counter() - t0) * 1000, "OK"
    except genai_errors.APIError as e:
        return False, (time.perf_counter() - t0) * 1000, f"{e.code} {(e.message or '')[:80]}"
    except Exception as e:
        return False, (time.perf_counter() - t0) * 1000, type(e).__name__


# --------------------------------------------------------------------------
# Bot + Web Dashboard tích hợp chung tiến trình
# --------------------------------------------------------------------------
class AskBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.ready_at: Optional[float] = None
        self._cleaned_guild_cmds = False
        self._web_server: Optional[uvicorn.Server] = None
        self._web_task: Optional[asyncio.Task] = None

    async def build_status_snapshot(self, check_gemini: bool = False) -> dict:
        mem = memory_info()
        proc = psutil.Process()
        ram_pct = round(mem["used"] / mem["limit"] * 100, 1) if mem["limit"] else 0.0
        uptime_sec = int(time.time() - START_TIME)

        db_ok, db_ms, db_msg = await sessions.ping()
        active = archived = storage = None
        if db_ok:
            active, archived, storage = await asyncio.gather(
                sessions.active_count(), sessions.archive_count(), sessions.storage_mb()
            )
        quota_mb = float(config_store.session_cfg().get("mongodb_quota_mb", 512.0))

        gemini_check = None
        if check_gemini:
            g_ok, g_ms, g_msg = await check_gemini_api()
            gemini_check = {"ok": g_ok, "latency_ms": round(g_ms, 1), "message": g_msg}

        chain_items = [
            {"id": m, "display": display_name_for(m), "cooldown_left": round(cooldown_left(m), 1)}
            for m in registry.auto_chain()
        ]
        guilds_list = [
            {"id": str(g.id), "name": g.name, "member_count": g.member_count or 0}
            for g in sorted(self.guilds, key=lambda x: x.name.lower())
        ]
        stats._roll_day()
        return {
            "bot": {
                "online": self.is_ready(),
                "username": str(self.user) if self.user else "Đang khởi động...",
                "bot_id": str(self.user.id) if self.user else "",
                "ping_ms": round(self.latency * 1000, 1) if self.is_ready() else None,
                "uptime_seconds": uptime_sec,
                "uptime_human": fmt_duration(uptime_sec),
                "ready_at": int(self.ready_at) if self.ready_at else None,
                "guild_count": len(self.guilds),
                "guilds": guilds_list,
                "version": BOT_VERSION,
                "python_version": platform.python_version(),
                "discord_py_version": discord.__version__,
            },
            "resources": {
                "ram_used_mb": round(mem["used"] / 1024 / 1024, 1),
                "ram_limit_mb": round(mem["limit"] / 1024 / 1024, 1),
                "ram_rss_mb": round(mem["rss"] / 1024 / 1024, 1),
                "ram_percent": ram_pct,
                "cpu_percent": round(proc.cpu_percent(None), 1),
                "threads": proc.num_threads(),
            },
            "gemini": {
                "discovered_count": len(registry.text),
                "updated_at": int(registry.updated_at) if registry.updated_at else None,
                "last_error": registry.last_error,
                "auto_chain": chain_items,
                "check": gemini_check,
            },
            "mongodb": {
                "enabled": sessions.enabled,
                "ok": db_ok,
                "latency_ms": round(db_ms, 1),
                "message": db_msg,
                "active_sessions": active,
                "archived_sessions": archived,
                "storage_mb": round(storage, 2) if storage is not None else None,
                "quota_mb": quota_mb,
                "last_error": sessions.last_error,
            },
            "stats": {
                "total": stats.total,
                "ok": stats.ok,
                "errors": stats.errors,
                "success_rate": round(stats.ok / stats.total * 100, 1) if stats.total else None,
                "today_total": stats.today_total(),
                "daily_models": dict(stats.daily_models.most_common(10)),
                "features": dict(stats.features.most_common()),
                "last_error": (
                    {"at": int(stats.last_error[0]), "message": stats.last_error[1]}
                    if stats.last_error else None
                ),
            },
        }

    async def build_models_snapshot(self) -> dict:
        return {
            "all_models": registry.all_known_models(),
            "auto_chain": registry.auto_chain(),
            "updated_at": int(registry.updated_at) if registry.updated_at else None,
            "last_error": registry.last_error,
        }

    async def setup_hook(self):
        await self.start_web()  # mở cổng sớm để nền tảng host (Northflank/Render) nhận diện service
        await self.init_sessions()  # kết nối MongoDB + nạp cấu hình động trước khi sync & chạy
        await registry.refresh()
        self.loop.create_task(self.model_refresher())
        self.loop.create_task(self.session_sweeper())
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            scope = f"CHỈ server có ID {GUILD_ID} (đang đặt GUILD_ID)"
        else:
            synced = await self.tree.sync()
            scope = "TOÀN CỤC (mọi server, có thể mất vài phút để Discord cập nhật)"
        log.info("Đã đồng bộ slash commands: %s — phạm vi: %s", ", ".join("/" + c.name for c in synced), scope)
        log.info(
            "Phiên bản %s · google-genai %s (API %s) · discord.py %s · pymongo %s · RAM tiến trình: %s",
            BOT_VERSION, getattr(genai, "__version__", "?"), GEMINI_API_VERSION or "mặc định SDK",
            discord.__version__, pymongo.version, fmt_bytes(psutil.Process().memory_info().rss),
        )

    async def start_web(self):
        def _clear_cooldowns() -> int:
            n = len(model_cooldowns)
            model_cooldowns.clear()
            return n

        web_app = create_dashboard_app(
            config_store=config_store,
            get_status_snapshot=self.build_status_snapshot,
            get_models_snapshot=self.build_models_snapshot,
            refresh_models_fn=registry.refresh,
            clear_cooldowns_fn=_clear_cooldowns,
            is_bot_ready_fn=self.is_ready,
            get_uptime_fn=lambda: time.time() - START_TIME,
        )
        uv_cfg = uvicorn.Config(
            web_app,
            host="0.0.0.0",
            port=PORT,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(uv_cfg)
        server.install_signal_handlers = lambda: None  # để discord.py quản lý tín hiệu dừng tiến trình
        self._web_server = server
        self._web_task = self.loop.create_task(server.serve())
        log.info("Web Dashboard + /health chạy tại cổng %d", PORT)

    async def model_refresher(self):
        while not self.is_closed():
            hours = max(0.25, float(config_store.models_cfg().get("model_refresh_hours", 6.0)))
            await asyncio.sleep(hours * 3600)
            await registry.refresh()

    async def init_sessions(self):
        await sessions.connect()
        await sessions.archive_expired()

    async def session_sweeper(self):
        while not self.is_closed():
            mins = max(1.0, float(config_store.session_cfg().get("sweep_minutes", 5.0)))
            await asyncio.sleep(mins * 60)
            await sessions.archive_expired()

    async def cleanup_guild_commands(self):
        if GUILD_ID:
            return
        for guild in list(self.guilds):
            try:
                if await self.tree.fetch_commands(guild=guild):
                    self.tree.clear_commands(guild=guild)
                    await self.tree.sync(guild=guild)
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
        if self._web_server is not None:
            self._web_server.should_exit = True
        if self._web_task is not None:
            try:
                await asyncio.wait_for(self._web_task, timeout=5)
            except Exception:
                pass
        await sessions.close()
        await super().close()


bot = AskBot()
psutil.Process().cpu_percent(None)


# --------------------------------------------------------------------------
# /ask (đọc toàn bộ cấu hình động từ MongoDB Atlas theo thời gian thực)
# --------------------------------------------------------------------------
@bot.tree.command(name="ask", description="Hỏi AI Gemini: OCR, suy nghĩ sâu, tra cứu web")
@app_commands.describe(
    prompt="Câu hỏi / yêu cầu của bạn",
    file="Ảnh hoặc PDF đính kèm để hỏi / OCR",
    model="Chọn model; bỏ trống = tự động (theo chuỗi cấu hình trên Dashboard)",
    web="Tra cứu thông tin mới trên web (Google Search)",
    thinking_level="Suy nghĩ sâu: auto / low / medium / high / xhigh",
    ocr="Chỉ trích xuất chữ từ file đính kèm (OCR)",
)
async def ask(
    interaction: discord.Interaction,
    prompt: app_commands.Range[str, 1, 4000],
    file: Optional[discord.Attachment] = None,
    model: Optional[str] = None,
    web: Optional[bool] = None,
    thinking_level: Optional[Literal["auto", "low", "medium", "high", "xhigh"]] = None,
    ocr: bool = False,
):
    cfg = await config_store.get_config()
    acfg = cfg.get("ask", {})

    uid = interaction.user.id
    now = time.monotonic()
    user_cooldown = float(acfg.get("user_cooldown", 5.0))
    wait = user_cooldown - (now - cooldowns.get(uid, -1e9))
    if wait > 0:
        return await interaction.response.send_message(
            f"⏳ Chậm lại chút nhé, thử lại sau {wait:.0f}s.", ephemeral=True)

    # Kiểm tra các công tắc tính năng cấu hình trên Dashboard
    use_web = bool(acfg.get("default_web_search", False)) if web is None else bool(web)
    eff_thinking = thinking_level if thinking_level is not None else (acfg.get("default_thinking_level") or None)

    if file and not acfg.get("allow_file", True):
        return await interaction.response.send_message("🚫 Tính năng đính kèm ảnh/PDF hiện đang tắt.", ephemeral=True)
    if ocr and not acfg.get("allow_ocr", True):
        return await interaction.response.send_message("🚫 Tính năng OCR hiện đang tắt.", ephemeral=True)
    if ocr and not file:
        return await interaction.response.send_message(
            "❌ OCR cần đính kèm file ảnh/PDF vào option `file`.", ephemeral=True)
    if use_web and not acfg.get("allow_web_search", True):
        if web is True:
            return await interaction.response.send_message("🚫 Tính năng tra cứu web hiện đang tắt.", ephemeral=True)
        use_web = False
    if eff_thinking and not acfg.get("allow_thinking", True):
        if thinking_level is not None:
            return await interaction.response.send_message("🚫 Tính năng suy nghĩ sâu hiện đang tắt.", ephemeral=True)
        eff_thinking = None

    cooldowns[uid] = now
    if len(cooldowns) > 2000:
        for k in [k for k, v in cooldowns.items() if now - v > 3600]:
            cooldowns.pop(k, None)

    await interaction.response.defer(thinking=True)
    started = time.perf_counter()
    notes: list[str] = []
    features = ["chat"] + [
        n for n, on in (("web", use_web), ("think", bool(eff_thinking)), ("ocr", ocr), ("file", bool(file))) if on
    ]

    # --- chọn model ---
    auto = not model or model.strip().lower() == "auto"
    chain = registry.auto_chain() if auto else [model.strip()]
    model_id = chain[0]

    lock = user_lock(uid)
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

        # --- system instruction động từ MongoDB ---
        system_prompt = cfg.get("system_prompt") or ""
        if cfg.get("append_current_date", True):
            system = f"{system_prompt}\nHôm nay là {datetime.now(PACIFIC):%d/%m/%Y}."
        else:
            system = system_prompt
        if ocr:
            ocr_ins = cfg.get("ocr_instruction") or ""
            system = f"{ocr_ins}\n\n{system}"

        errors: dict[str, str] = {}
        async with gemini_sem:
            if auto:
                resp, thinking_used, model_id, errors = await call_with_fallback(
                    chain, contents, system=system, thinking_level=eff_thinking, web_search=use_web)
            else:
                try:
                    resp, thinking_used = await call_gemini(
                        model_id, contents, system=system, thinking_level=eff_thinking, web_search=use_web)
                    model_cooldowns.pop(model_id, None)
                except (genai_errors.APIError, asyncio.TimeoutError) as e:
                    mark_failure(model_id, e)
                    raise
        out = parse_response(resp)
        if auto and model_id in chain:
            failed = [f"`{m}` ({errors[m]})" for m in chain[:chain.index(model_id)] if m in errors]
            if failed:
                notes.append("Tự động chuyển model: " + ", ".join(failed) + f" → `{model_id}`")
        if eff_thinking and not thinking_used:
            notes.append(f"Model `{model_id}` không hỗ trợ `thinking_level` ({eff_thinking}) nên đã bỏ qua")
        elif eff_thinking and thinking_used != eff_thinking:
            notes.append(f"Model `{model_id}` không có mức `{eff_thinking}`, đã dùng mức `{thinking_used}`")

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
        model_label = display_name_for(model_id)
        meta = [f"🤖 {model_label}", f"⏱ {elapsed:.1f}s"]
        meta += [lbl for lbl, on in (("🌐 web", use_web), (f"🧠 {thinking_used}", bool(thinking_used)), ("🔎 OCR", ocr)) if on]
        if out.tokens:
            meta.append(f"{out.tokens:,} tokens")
        if turns:
            meta.append("🆕 phiên mới" if turns == 1 else f"💬 phiên: {turns} lượt")
        footer = "-# " + " · ".join(meta)
        extras = []
        if acfg.get("show_sources", True):
            if out.queries:
                extras.append("-# 🔎 Đã tìm: " + "; ".join(q[:60] for q in out.queries[:3]))
            if out.sources:
                links = " · ".join(f"[{clean_title(t)}](<{u}>)" for t, u in out.sources[:4])
                extras.append("-# 📚 Nguồn: " + links)
        extras += [f"-# ⚠️ {n}" for n in notes]
        tail = "\n".join([footer] + extras)

        # --- gửi kết quả ---
        if out.thoughts and acfg.get("show_thoughts_embed", True):
            emb = discord.Embed(
                title="🧠 Tóm tắt quá trình suy nghĩ",
                description=out.thoughts[:3500] + ("…" if len(out.thoughts) > 3500 else ""),
                color=discord.Color.dark_grey(),
            )
            await interaction.followup.send(embed=emb, allowed_mentions=NO_MENTIONS)

        chunk_limit = int(acfg.get("chunk_char_limit", 1900))
        max_chunks = int(acfg.get("max_reply_chunks", 6))
        files = []
        chunks = chunk_text(out.text, limit=chunk_limit)
        if len(chunks) > max_chunks:
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

    except Exception as e:
        stats.record(model_id, False, features, f"{type(e).__name__}: {e}"[:200])
        if not isinstance(e, UserError):
            log.exception("Lỗi /ask")
        await interaction.followup.send(explain_error(e), allowed_mentions=NO_MENTIONS)
    finally:
        lock.release()


@ask.autocomplete("model")
async def model_autocomplete(interaction: discord.Interaction, current: str):
    await config_store.get_config()
    cur = current.lower().strip()
    out = []
    if not cur or any(cur in k for k in ("auto", "tự động", "tu dong")):
        chain_preview = " → ".join(registry.auto_chain()[:3])
        out.append(app_commands.Choice(name=f"🤖 Tự động ({chain_preview})"[:100], value="auto"))
    for m in registry.choices():
        if cur in m.id.lower() or cur in m.display.lower():
            label = f"{m.badge} {m.display}" if m.display != m.id else f"{m.badge} {m.id}"
            out.append(app_commands.Choice(name=label[:100], value=m.id))
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
    await config_store.get_config()
    max_hist_msgs = int(config_store.session_cfg().get("max_history_messages", 20))
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
    emb.add_field(name="Tin nhắn đang nhớ", value=f"**{len(doc.get('messages', []))}** / {max_hist_msgs}", inline=True)
    emb.add_field(name="Bắt đầu", value=f"<t:{ts('created_at')}:R>", inline=True)
    emb.add_field(name="Chat gần nhất", value=f"<t:{ts('last_active')}:R>", inline=True)
    emb.add_field(name="Tự lưu trữ + làm mới", value=f"<t:{ts('expire_at')}:R> (nếu không chat thêm)", inline=True)
    emb.set_footer(text="Dùng /refresh để lưu trữ và làm mới ngay")
    await interaction.followup.send(embed=emb, ephemeral=True)


# --------------------------------------------------------------------------
# /status (kiểm tra quyền trực tiếp từ MongoDB Atlas)
# --------------------------------------------------------------------------
@bot.tree.command(name="status", description="Xem tình trạng bot: RAM, uptime, Gemini API, thống kê")
async def status(interaction: discord.Interaction):
    allowed = await config_store.can_use_status(interaction.user.id)
    if not allowed:
        return await interaction.response.send_message(
            "🔒 Bạn không có quyền dùng lệnh này.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    await config_store.get_config()
    scfg = config_store.session_cfg()
    idle_hours = float(scfg.get("session_idle_hours", 2.0))
    quota_mb = float(scfg.get("mongodb_quota_mb", 512.0))

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
            f"Tự lưu trữ + làm mới sau **{idle_hours:g} giờ** không chat"
            + (f"\nDung lượng: **{storage:.1f} / {quota_mb:g} MB** `{bar(storage / quota_mb * 100)}`"
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
