"""
Tầng lưu trữ cấu hình động, xác thực và phân quyền trên MongoDB Atlas.
Cả Bot Discord và Web Dashboard dùng chung module này để mọi thay đổi có hiệu lực ngay lập tức
mà không cần sửa code hay restart bot.

Collections sử dụng trong MongoDB Atlas (database = MONGODB_DB):
- `bot_config`          : 1 document (_id="global") chứa toàn bộ cấu hình động của bot & lệnh /ask & model Gemini
- `dashboard_users`     : tài khoản đăng nhập truyền thống (username + mật khẩu băm scrypt + role)
- `discord_permissions` : phân quyền theo Discord User ID (_id = str(user_id)) gồm is_admin, can_dashboard, can_status
"""
from __future__ import annotations

import asyncio
import copy
import fnmatch
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("gemini-bot.config")

DEFAULT_SYSTEM_PROMPT = (
    "Bạn là trợ lý AI trong Discord. Trả lời bằng đúng ngôn ngữ người dùng đang dùng "
    "(mặc định tiếng Việt), rõ ràng, súc tích. Dùng Markdown của Discord "
    "(in đậm, danh sách, code block); KHÔNG dùng bảng Markdown vì Discord không hiển thị được."
)

DEFAULT_OCR_INSTRUCTION = (
    "Bạn là công cụ OCR. Hãy chép lại CHÍNH XÁC toàn bộ chữ có trong tệp đính kèm, giữ nguyên "
    "thứ tự và bố cục (dùng danh sách/code block nếu cần, bảng thì trình bày dạng dòng chữ). "
    "Không tự thêm bình luận. Nếu người dùng có yêu cầu thêm, hãy làm sau khi chép xong."
)

DEFAULT_HIDDEN_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
    "gemini-nano-banana-2.1",
    "gemini-omni-flash-preview",
    "gemini-4-argon",
    "gemini-4-carbon",
]


def _csv_env(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        raw = default
    return [x.strip() for x in re.split(r"[,\n]+", raw) if x.strip()]


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "")


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _opt_int_env(name: str) -> Optional[int]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        val = int(raw)
        return val if val > 0 else None
    except ValueError:
        return None


def build_default_config() -> dict[str, Any]:
    """Khởi tạo cấu hình mặc định (lấy seed từ biến môi trường nếu có)."""
    return {
        "_id": "global",
        # --- System Instruction ---
        "system_prompt": os.getenv("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        "ocr_instruction": os.getenv("OCR_INSTRUCTION", DEFAULT_OCR_INSTRUCTION),
        "append_current_date": True,
        # --- Cấu hình lệnh /ask ---
        "ask": {
            "user_cooldown": _float_env("USER_COOLDOWN", 5.0),
            "max_concurrent": max(1, _int_env("MAX_CONCURRENT", 2)),
            "max_file_mb": _float_env("MAX_FILE_MB", 10.0),
            "allow_file": True,
            "allow_ocr": True,
            "allow_web_search": True,
            "allow_thinking": True,
            "default_web_search": False,
            "default_thinking_level": "",  # "", "auto", "low", "medium", "high", "xhigh"
            "thinking_budgets": {
                "low": 1024,
                "medium": 8192,
                "high": 24576,
            },
            "gemini_timeout_seconds": _float_env("GEMINI_TIMEOUT_SECONDS", 170.0),
            "max_retries": max(1, _int_env("MAX_RETRIES", 3)),
            "chunk_char_limit": 1900,
            "max_reply_chunks": 6,
            "show_thoughts_embed": True,
            "show_sources": True,
        },
        # --- Cấu hình phiên chat & hệ thống ---
        "session": {
            "session_idle_hours": _float_env("SESSION_IDLE_HOURS", 2.0),
            "max_history_messages": max(2, _int_env("MAX_HISTORY_MESSAGES", 20)),
            "max_history_chars": max(1000, _int_env("MAX_HISTORY_CHARS", 24000)),
            "max_stored_chars": max(500, _int_env("MAX_STORED_CHARS", 4000)),
            "session_max_turns": max(1, _int_env("SESSION_MAX_TURNS", 500)),
            "sweep_minutes": max(1.0, _float_env("SWEEP_MINUTES", 5.0)),
            "mongodb_quota_mb": max(1.0, _float_env("MONGODB_QUOTA_MB", 512.0)),
            "ram_limit_mb": _opt_int_env("RAM_LIMIT_MB"),
        },
        # --- Cấu hình Model Gemini ---
        "models": {
            "default_text_model": os.getenv("DEFAULT_TEXT_MODEL", "").strip(),
            "auto_model_chain": _csv_env("AUTO_MODEL_CHAIN", ""),
            "auto_last_model": os.getenv("AUTO_LAST_MODEL", "gemini-4-hydrogen").strip(),
            "fallback_text": ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-4-hydrogen"],
            "hidden_models": _csv_env("HIDDEN_MODELS", ",".join(DEFAULT_HIDDEN_MODELS)),
            "hide_pro_models": _bool_env("HIDE_PRO_MODELS", True),
            "extra_models": _csv_env("EXTRA_MODELS", "gemini-4-hydrogen"),
            "model_refresh_hours": max(0.25, _float_env("MODEL_REFRESH_HOURS", 6.0)),
            # Tùy chỉnh chi tiết theo từng model_id:
            # { "gemini-2.5-flash": {"display_name": "Flash 2.5", "tag": "🆓", "is_free": True, "hidden": False} }
            "overrides": {},
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": "system-default",
    }


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Gộp dict đệ quy để đảm bảo các trường mới luôn có giá trị mặc định khi nâng cấp schema."""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "overrides":
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# --------------------------------------------------------------------------
# Mã hóa mật khẩu an toàn (scrypt từ thư viện chuẩn Python, không cần C compiler)
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        parts = stored_hash.split("$")
        if len(parts) != 6 or parts[0] != "scrypt":
            return False
        _, n_s, r_s, p_s, salt_hex, dk_hex = parts
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_s),
            r=int(r_s),
            p=int(p_s),
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


# --------------------------------------------------------------------------
# Semaphore động: cho phép đổi MAX_CONCURRENT theo thời gian thực không cần restart
# --------------------------------------------------------------------------
class DynamicSemaphore:
    def __init__(self, get_limit):
        self._get_limit = get_limit
        self._active = 0
        self._cond = asyncio.Condition()

    async def __aenter__(self):
        async with self._cond:
            while self._active >= max(1, int(self._get_limit())):
                await self._cond.wait()
            self._active += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        async with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()

    def notify_limit_changed(self):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._notify())
        except RuntimeError:
            pass

    async def _notify(self):
        async with self._cond:
            self._cond.notify_all()


# --------------------------------------------------------------------------
# ConfigStore: đọc/ghi trực tiếp MongoDB Atlas + cache TTL ngắn để đồng bộ tức thì
# --------------------------------------------------------------------------
class ConfigStore:
    def __init__(self, cache_ttl: float = 5.0):
        self.db = None
        self.config_col = None
        self.users_col = None
        self.perms_col = None
        self.cache_ttl = cache_ttl

        self._config: dict[str, Any] = build_default_config()
        self._config_loaded_at: float = 0.0
        self._perms_cache: dict[str, dict[str, Any]] = {}
        self._perms_loaded_at: float = 0.0
        self._on_config_update_callbacks: list = []

        # Seed phân quyền Discord ban đầu từ biến môi trường (fallback khi chưa có DB)
        self._seed_env_perms()

    def _seed_env_perms(self):
        raw_admins = os.getenv("INITIAL_ADMIN_DISCORD_IDS", "")
        raw_status = os.getenv("STATUS_ALLOWED_IDS", "1240503090185109536,1568530089035956284")
        admin_ids = {x.strip() for x in re.split(r"[,\s]+", raw_admins) if x.strip().isdigit()}
        status_ids = {x.strip() for x in re.split(r"[,\s]+", raw_status) if x.strip().isdigit()}
        # Nếu không đặt INITIAL_ADMIN_DISCORD_IDS, mặc định nâng các STATUS_ALLOWED_IDS thành Admin ban đầu
        if not admin_ids:
            admin_ids = set(status_ids)
        now = datetime.now(timezone.utc).isoformat()
        for uid in sorted(admin_ids | status_ids):
            is_adm = uid in admin_ids
            self._perms_cache[uid] = {
                "_id": uid,
                "user_id": uid,
                "label": "Admin khởi tạo (.env)" if is_adm else "Status (.env)",
                "is_admin": is_adm,
                "can_dashboard": is_adm,
                "can_status": is_adm or (uid in status_ids),
                "updated_at": now,
            }

    def on_update(self, callback):
        self._on_config_update_callbacks.append(callback)

    @property
    def connected(self) -> bool:
        return self.db is not None

    async def attach_db(self, db):
        """Gắn database MongoDB (dùng chung AsyncMongoClient với SessionStore) và khởi tạo dữ liệu ban đầu."""
        self.db = db
        self.config_col = db["bot_config"]
        self.users_col = db["dashboard_users"]
        self.perms_col = db["discord_permissions"]

        try:
            await self.users_col.create_index("username", unique=True)
        except Exception as e:
            log.warning("Không tạo được index dashboard_users: %s", e)

        await self._ensure_initial_data()

    async def _ensure_initial_data(self):
        if not self.connected:
            return
        try:
            # 1) Khởi tạo bot_config nếu chưa có
            existing = await self.config_col.find_one({"_id": "global"})
            if existing is None:
                default_doc = build_default_config()
                await self.config_col.replace_one({"_id": "global"}, default_doc, upsert=True)
                self._config = default_doc
                log.info("Đã khởi tạo cấu hình mặc định trong MongoDB collection `bot_config`.")
            else:
                self._config = deep_merge(build_default_config(), existing)
            self._config_loaded_at = time.monotonic()

            # 2) Khởi tạo discord_permissions nếu collection đang rỗng
            perm_count = await self.perms_col.count_documents({})
            if perm_count == 0 and self._perms_cache:
                for doc in self._perms_cache.values():
                    await self.perms_col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
                log.info("Đã đồng bộ %d Discord User ID khởi tạo vào `discord_permissions`.", len(self._perms_cache))
            await self.refresh_permissions(force=True)

            # 3) Khởi tạo tài khoản Dashboard đầu tiên từ biến môi trường nếu chưa có tài khoản nào
            init_user = (os.getenv("INITIAL_ADMIN_USERNAME") or "").strip().lower()
            init_pass = (os.getenv("INITIAL_ADMIN_PASSWORD") or "").strip()
            if init_user and init_pass:
                user_count = await self.users_col.count_documents({})
                if user_count == 0:
                    await self.create_dashboard_user(init_user, init_pass, role="admin")
                    log.info("Đã tự động tạo tài khoản admin đầu tiên `%s` từ biến môi trường.", init_user)
        except Exception as e:
            log.warning("Lỗi khởi tạo dữ liệu ConfigStore trên MongoDB: %s", e)

    # ------------------------------------------------------------------
    # Đọc / Ghi cấu hình động (`bot_config`)
    # ------------------------------------------------------------------
    @property
    def current(self) -> dict[str, Any]:
        """Trả về bản cấu hình đang cache trong RAM (dùng cho chỗ đồng bộ không await)."""
        return self._config

    async def get_config(self, force: bool = False) -> dict[str, Any]:
        """Đọc cấu hình mới nhất từ MongoDB Atlas (có cache ngắn để tối ưu hiệu năng)."""
        now = time.monotonic()
        if not self.connected:
            return copy.deepcopy(self._config)
        if not force and (now - self._config_loaded_at) < self.cache_ttl:
            return copy.deepcopy(self._config)
        try:
            doc = await self.config_col.find_one({"_id": "global"})
            if doc:
                self._config = deep_merge(build_default_config(), doc)
                self._config_loaded_at = now
        except Exception as e:
            log.warning("Không đọc được bot_config từ MongoDB (dùng cache): %s", e)
        return copy.deepcopy(self._config)

    async def update_config(self, patch: dict[str, Any], updated_by: str = "dashboard") -> dict[str, Any]:
        """Cập nhật một phần hoặc toàn bộ cấu hình lên MongoDB Atlas và áp dụng ngay lập tức."""
        current = await self.get_config(force=True)
        merged = deep_merge(current, patch)
        merged["_id"] = "global"
        merged["updated_at"] = datetime.now(timezone.utc).isoformat()
        merged["updated_by"] = updated_by

        if self.connected:
            await self.config_col.replace_one({"_id": "global"}, merged, upsert=True)

        self._config = merged
        self._config_loaded_at = time.monotonic()

        for cb in self._on_config_update_callbacks:
            try:
                cb(self._config)
            except Exception as e:
                log.warning("Lỗi callback cập nhật cấu hình: %s", e)

        return copy.deepcopy(self._config)

    # Tiện ích truy cập nhanh cho bot.py
    def ask_cfg(self) -> dict[str, Any]:
        return self._config.get("ask", {})

    def session_cfg(self) -> dict[str, Any]:
        return self._config.get("session", {})

    def models_cfg(self) -> dict[str, Any]:
        return self._config.get("models", {})

    def is_model_hidden(self, model_id: str) -> bool:
        mcfg = self.models_cfg()
        overrides = mcfg.get("overrides") or {}
        ov = overrides.get(model_id)
        if isinstance(ov, dict) and ov.get("hidden") is not None:
            return bool(ov["hidden"])
        patterns = mcfg.get("hidden_models") or []
        return any(fnmatch.fnmatch(model_id.lower(), pat.lower()) for pat in patterns if pat)

    def model_override(self, model_id: str) -> dict[str, Any]:
        overrides = self.models_cfg().get("overrides") or {}
        ov = overrides.get(model_id)
        return ov if isinstance(ov, dict) else {}

    # ------------------------------------------------------------------
    # Phân quyền theo Discord User ID (`discord_permissions`)
    # ------------------------------------------------------------------
    async def refresh_permissions(self, force: bool = False) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        if not self.connected:
            return copy.deepcopy(self._perms_cache)
        if not force and (now - self._perms_loaded_at) < self.cache_ttl:
            return copy.deepcopy(self._perms_cache)
        try:
            docs = await self.perms_col.find({}).to_list(500)
            self._perms_cache = {str(d["_id"]): d for d in docs}
            self._perms_loaded_at = now
        except Exception as e:
            log.warning("Không đọc được discord_permissions từ MongoDB: %s", e)
        return copy.deepcopy(self._perms_cache)

    async def list_discord_permissions(self) -> list[dict[str, Any]]:
        perms = await self.refresh_permissions(force=True)
        items = list(perms.values())
        items.sort(key=lambda x: (not x.get("is_admin", False), x.get("user_id", "")))
        return items

    async def upsert_discord_permission(
        self,
        user_id: str,
        *,
        label: str = "",
        is_admin: bool = False,
        can_dashboard: bool = False,
        can_status: bool = False,
    ) -> dict[str, Any]:
        uid = str(user_id).strip()
        if not uid.isdigit():
            raise ValueError("Discord User ID phải là chuỗi chữ số hợp lệ.")
        # Admin luôn đồng thời có quyền vào Dashboard và dùng /status
        if is_admin:
            can_dashboard = True
            can_status = True
        doc = {
            "_id": uid,
            "user_id": uid,
            "label": label.strip()[:80],
            "is_admin": bool(is_admin),
            "can_dashboard": bool(can_dashboard),
            "can_status": bool(can_status),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if self.connected:
            await self.perms_col.replace_one({"_id": uid}, doc, upsert=True)
        self._perms_cache[uid] = doc
        self._perms_loaded_at = time.monotonic()
        return doc

    async def delete_discord_permission(self, user_id: str) -> bool:
        uid = str(user_id).strip()
        if self.connected:
            res = await self.perms_col.delete_one({"_id": uid})
            deleted = res.deleted_count > 0
        else:
            deleted = uid in self._perms_cache
        self._perms_cache.pop(uid, None)
        self._perms_loaded_at = time.monotonic()
        return deleted

    async def get_discord_permission(self, user_id: int | str, *, force: bool = True) -> Optional[dict[str, Any]]:
        uid = str(user_id).strip()
        perms = await self.refresh_permissions(force=force)
        return perms.get(uid)

    async def can_use_status(self, user_id: int | str) -> bool:
        """Kiểm tra trực tiếp từ MongoDB xem Discord User ID có được dùng /status không."""
        doc = await self.get_discord_permission(user_id, force=True)
        if not doc:
            return False
        return bool(doc.get("is_admin") or doc.get("can_status"))

    async def can_access_dashboard_discord(self, user_id: int | str) -> tuple[bool, bool]:
        """Trả về (can_access, is_admin) cho đăng nhập Discord OAuth2."""
        doc = await self.get_discord_permission(user_id, force=True)
        if not doc:
            return False, False
        is_admin = bool(doc.get("is_admin"))
        return bool(is_admin or doc.get("can_dashboard")), is_admin

    # ------------------------------------------------------------------
    # Tài khoản đăng nhập truyền thống (`dashboard_users`)
    # ------------------------------------------------------------------
    async def list_dashboard_users(self) -> list[dict[str, Any]]:
        if not self.connected:
            return []
        docs = await self.users_col.find({}, {"password_hash": 0}).sort("username", 1).to_list(200)
        out = []
        for d in docs:
            out.append({
                "username": d.get("username", ""),
                "role": d.get("role", "admin"),
                "created_at": d.get("created_at"),
                "last_login_at": d.get("last_login_at"),
            })
        return out

    async def create_dashboard_user(self, username: str, password: str, role: str = "admin") -> dict[str, Any]:
        uname = username.strip().lower()
        if not re.fullmatch(r"[a-z0-9_.-]{3,32}", uname):
            raise ValueError("Tên đăng nhập phải dài 3–32 ký tự (chữ thường, số, _, ., -).")
        if len(password) < 6:
            raise ValueError("Mật khẩu phải có ít nhất 6 ký tự.")
        if role not in ("admin", "operator"):
            raise ValueError("Vai trò không hợp lệ (chỉ nhận admin hoặc operator).")
        if not self.connected:
            raise RuntimeError("Chưa kết nối MongoDB Atlas.")

        existing = await self.users_col.find_one({"username": uname})
        if existing:
            raise ValueError(f"Tài khoản '{uname}' đã tồn tại.")

        doc = {
            "username": uname,
            "password_hash": hash_password(password),
            "role": role,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_login_at": None,
        }
        await self.users_col.insert_one(doc)
        return {"username": uname, "role": role, "created_at": doc["created_at"], "last_login_at": None}

    async def update_dashboard_user_password(self, username: str, new_password: str) -> bool:
        uname = username.strip().lower()
        if len(new_password) < 6:
            raise ValueError("Mật khẩu mới phải có ít nhất 6 ký tự.")
        if not self.connected:
            raise RuntimeError("Chưa kết nối MongoDB Atlas.")
        res = await self.users_col.update_one(
            {"username": uname},
            {"$set": {"password_hash": hash_password(new_password)}},
        )
        return res.matched_count > 0

    async def delete_dashboard_user(self, username: str) -> bool:
        uname = username.strip().lower()
        if not self.connected:
            raise RuntimeError("Chưa kết nối MongoDB Atlas.")
        res = await self.users_col.delete_one({"username": uname})
        return res.deleted_count > 0

    async def authenticate_user(self, username: str, password: str) -> Optional[dict[str, Any]]:
        if not self.connected:
            return None
        uname = username.strip().lower()
        doc = await self.users_col.find_one({"username": uname})
        if not doc or not verify_password(password, doc.get("password_hash", "")):
            return None
        now = datetime.now(timezone.utc).isoformat()
        await self.users_col.update_one({"username": uname}, {"$set": {"last_login_at": now}})
        return {
            "username": uname,
            "role": doc.get("role", "admin"),
            "is_admin": doc.get("role", "admin") == "admin",
        }
