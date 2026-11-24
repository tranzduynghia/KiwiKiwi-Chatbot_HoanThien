"""
Tầng lưu trữ cấu hình động, xác thực, phân quyền và Audit Log trên MongoDB Atlas
kèm cơ chế Đệm RAM + Tự động đồng bộ liên tục & giải phóng bộ nhớ (Write-Behind Sync Buffer).

Luồng hoạt động khi thay đổi cấu hình / phân quyền / tài khoản:
1. Áp dụng ngay trên RAM để bot và Dashboard có hiệu lực tức thì.
2. Thử ghi lên MongoDB Atlas; nếu Atlas đang mất kết nối hoặc lỗi mạng, lưu tạm vào hàng đợi RAM
   (mật khẩu luôn được băm scrypt trước khi đưa vào hàng đợi, không lưu mật khẩu thô).
3. Vòng lặp nền kiểm tra kết nối MongoDB Atlas liên tục (mặc định mỗi 5 giây): ngay khi Atlas
   hoạt động trở lại, tự động ghi toàn bộ hàng đợi lên Atlas rồi GIẢI PHÓNG ngay khỏi RAM.
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

SENSITIVE_KEY_TOKENS = ("password", "secret", "token", "api_key", "apikey", "uri")


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
    return {
        "_id": "global",
        "system_prompt": os.getenv("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        "ocr_instruction": os.getenv("OCR_INSTRUCTION", DEFAULT_OCR_INSTRUCTION),
        "append_current_date": True,
        "ask": {
            "user_cooldown": _float_env("USER_COOLDOWN", 5.0),
            "max_concurrent": max(1, _int_env("MAX_CONCURRENT", 2)),
            "max_file_mb": _float_env("MAX_FILE_MB", 10.0),
            "allow_file": True,
            "allow_ocr": True,
            "allow_web_search": True,
            "allow_thinking": True,
            "default_web_search": False,
            "default_thinking_level": "",
            "thinking_budgets": {"low": 1024, "medium": 8192, "high": 24576},
            "gemini_timeout_seconds": _float_env("GEMINI_TIMEOUT_SECONDS", 170.0),
            "max_retries": max(1, _int_env("MAX_RETRIES", 3)),
            "chunk_char_limit": 1900,
            "max_reply_chunks": 6,
            "show_thoughts_embed": True,
            "show_sources": True,
        },
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
        "models": {
            "default_text_model": os.getenv("DEFAULT_TEXT_MODEL", "").strip(),
            "auto_model_chain": _csv_env("AUTO_MODEL_CHAIN", ""),
            "auto_last_model": os.getenv("AUTO_LAST_MODEL", "gemini-4-hydrogen").strip(),
            "fallback_text": ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-4-hydrogen"],
            "hidden_models": _csv_env("HIDDEN_MODELS", ",".join(DEFAULT_HIDDEN_MODELS)),
            "hide_pro_models": _bool_env("HIDE_PRO_MODELS", True),
            "extra_models": _csv_env("EXTRA_MODELS", "gemini-4-hydrogen"),
            "model_refresh_hours": max(0.25, _float_env("MODEL_REFRESH_HOURS", 6.0)),
            "overrides": {},
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": "system-default",
    }


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "overrides":
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _is_sensitive_field(field_path: str) -> bool:
    low = field_path.lower()
    return any(tok in low for tok in SENSITIVE_KEY_TOKENS)


def _sanitize_audit_value(val: Any) -> Any:
    if isinstance(val, str) and len(val) > 500:
        return val[:500] + f"… ({len(val)} ký tự)"
    return val


def compute_diff(old_obj: Any, new_obj: Any, prefix: str = "") -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    skip_keys = {"_id", "updated_at", "updated_by"}
    if isinstance(old_obj, dict) and isinstance(new_obj, dict):
        keys = sorted(set(old_obj.keys()) | set(new_obj.keys()))
        for k in keys:
            if not prefix and k in skip_keys:
                continue
            path = f"{prefix}.{k}" if prefix else str(k)
            if _is_sensitive_field(path):
                continue
            v_old = old_obj.get(k)
            v_new = new_obj.get(k)
            if v_old == v_new:
                continue
            if isinstance(v_old, dict) and isinstance(v_new, dict):
                changes.extend(compute_diff(v_old, v_new, path))
            else:
                changes.append({
                    "field": path,
                    "before": _sanitize_audit_value(v_old),
                    "after": _sanitize_audit_value(v_new),
                })
    elif old_obj != new_obj and not _is_sensitive_field(prefix):
        changes.append({
            "field": prefix or "value",
            "before": _sanitize_audit_value(old_obj),
            "after": _sanitize_audit_value(new_obj),
        })
    return changes


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


class ConfigStore:
    def __init__(self, cache_ttl: float = 5.0):
        self.db = None
        self.config_col = None
        self.users_col = None
        self.perms_col = None
        self.audit_col = None
        self.cache_ttl = cache_ttl

        self._config: dict[str, Any] = build_default_config()
        self._config_loaded_at: float = 0.0
        self._perms_cache: dict[str, dict[str, Any]] = {}
        self._perms_loaded_at: float = 0.0
        self._users_cache: dict[str, dict[str, Any]] = {}
        self._on_config_update_callbacks: list = []

        # Hàng đợi tạm trong RAM chờ đồng bộ lên MongoDB Atlas (tự giải phóng sau khi lưu xong)
        self._sync_lock = asyncio.Lock()
        self._pending_config: Optional[dict[str, Any]] = None
        self._pending_perms: dict[str, Optional[dict[str, Any]]] = {}  # uid -> doc (upsert) hoặc None (delete)
        self._pending_users: dict[str, dict[str, Any]] = {}            # uname -> {"op": "upsert"|"delete", "doc": ...}
        self._pending_audits: list[dict[str, Any]] = []
        self._last_flush_at: Optional[str] = None
        self._last_flush_error: Optional[str] = None

        self._seed_env_perms()
        self._seed_env_user_cache()

    def _seed_env_perms(self):
        raw_admins = os.getenv("INITIAL_ADMIN_DISCORD_IDS", "")
        raw_status = os.getenv("STATUS_ALLOWED_IDS", "1240503090185109536,1568530089035956284")
        admin_ids = {x.strip() for x in re.split(r"[,\s]+", raw_admins) if x.strip().isdigit()}
        status_ids = {x.strip() for x in re.split(r"[,\s]+", raw_status) if x.strip().isdigit()}
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

    def _seed_env_user_cache(self):
        init_user = (os.getenv("INITIAL_ADMIN_USERNAME") or "").strip().lower()
        init_pass = (os.getenv("INITIAL_ADMIN_PASSWORD") or "").strip()
        if init_user and init_pass:
            self._users_cache[init_user] = {
                "username": init_user,
                "password_hash": hash_password(init_pass),
                "role": "admin",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "last_login_at": None,
            }

    def on_update(self, callback):
        self._on_config_update_callbacks.append(callback)

    @property
    def connected(self) -> bool:
        return self.db is not None

    @property
    def pending_count(self) -> int:
        return (
            (1 if self._pending_config is not None else 0)
            + len(self._pending_perms)
            + len(self._pending_users)
            + len(self._pending_audits)
        )

    def sync_status(self) -> dict[str, Any]:
        return {
            "pending_count": self.pending_count,
            "pending_config": self._pending_config is not None,
            "pending_perms": len(self._pending_perms),
            "pending_users": len(self._pending_users),
            "pending_audits": len(self._pending_audits),
            "last_flush_at": self._last_flush_at,
            "last_flush_error": self._last_flush_error,
        }

    async def attach_db(self, db):
        """Gắn database MongoDB Atlas, khởi tạo index và xả ngay mọi dữ liệu đang đệm trong RAM."""
        self.db = db
        self.config_col = db["bot_config"]
        self.users_col = db["dashboard_users"]
        self.perms_col = db["discord_permissions"]
        self.audit_col = db["audit_logs"]

        try:
            await self.users_col.create_index("username", unique=True)
            await self.audit_col.create_index([("at", -1)])
            await self.audit_col.create_index([("section", 1), ("at", -1)])
        except Exception as e:
            log.warning("Không tạo được index ConfigStore: %s", e)

        await self._ensure_initial_data()
        await self.flush_pending()

    async def _ensure_initial_data(self):
        if not self.connected:
            return
        try:
            # 1) bot_config: chỉ nạp từ DB nếu không có bản mới hơn đang chờ flush trong RAM
            if self._pending_config is None:
                existing = await self.config_col.find_one({"_id": "global"})
                if existing is None:
                    default_doc = build_default_config()
                    await self.config_col.replace_one({"_id": "global"}, default_doc, upsert=True)
                    self._config = default_doc
                    log.info("Đã khởi tạo cấu hình mặc định trong MongoDB collection `bot_config`.")
                else:
                    self._config = deep_merge(build_default_config(), existing)
                self._config_loaded_at = time.monotonic()

            # 2) discord_permissions
            perm_count = await self.perms_col.count_documents({})
            if perm_count == 0 and self._perms_cache:
                for doc in self._perms_cache.values():
                    await self.perms_col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
                log.info("Đã đồng bộ %d Discord User ID khởi tạo vào `discord_permissions`.", len(self._perms_cache))
            await self.refresh_permissions(force=True)

            # 3) dashboard_users
            init_user = (os.getenv("INITIAL_ADMIN_USERNAME") or "").strip().lower()
            init_pass = (os.getenv("INITIAL_ADMIN_PASSWORD") or "").strip()
            user_count = await self.users_col.count_documents({})
            if user_count == 0 and init_user and init_pass:
                doc = self._users_cache.get(init_user) or {
                    "username": init_user,
                    "password_hash": hash_password(init_pass),
                    "role": "admin",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "last_login_at": None,
                }
                await self.users_col.insert_one(copy.deepcopy(doc))
                await self.record_audit(
                    actor="system (.env)",
                    actor_type="system",
                    action="user_create",
                    section="dashboard_users",
                    summary=f"Tự động khởi tạo tài khoản admin đầu tiên '{init_user}'",
                    changes=[{"field": "username", "before": None, "after": init_user}, {"field": "role", "before": None, "after": "admin"}],
                )
                log.info("Đã tự động tạo tài khoản admin đầu tiên `%s` trên MongoDB Atlas.", init_user)
            # Nạp cache users (để đăng nhập vẫn hoạt động nếu Atlas rớt mạng tạm thời)
            db_users = await self.users_col.find({}).to_list(200)
            for u in db_users:
                uname = u.get("username")
                if uname and uname not in self._pending_users:
                    self._users_cache[uname] = u
        except Exception as e:
            log.warning("Lỗi khởi tạo dữ liệu ConfigStore trên MongoDB: %s", e)

    # ------------------------------------------------------------------
    # Xả bộ đệm RAM lên MongoDB Atlas & giải phóng bộ nhớ sau khi lưu thành công
    # ------------------------------------------------------------------
    async def flush_pending(self) -> dict[str, Any]:
        """Kiểm tra MongoDB Atlas, đẩy toàn bộ dữ liệu đang đệm trong RAM lên Atlas rồi giải phóng RAM."""
        if not self.connected or self.pending_count == 0:
            return self.sync_status()

        async with self._sync_lock:
            flushed_items = 0
            try:
                # 1) Flush bot_config
                if self._pending_config is not None:
                    doc = copy.deepcopy(self._pending_config)
                    await self.config_col.replace_one({"_id": "global"}, doc, upsert=True)
                    self._pending_config = None  # giải phóng bản đệm RAM
                    flushed_items += 1

                # 2) Flush discord_permissions
                for uid in list(self._pending_perms.keys()):
                    val = self._pending_perms[uid]
                    if val is None:
                        await self.perms_col.delete_one({"_id": uid})
                    else:
                        await self.perms_col.replace_one({"_id": uid}, copy.deepcopy(val), upsert=True)
                    self._pending_perms.pop(uid, None)  # giải phóng khỏi hàng đợi RAM
                    flushed_items += 1

                # 3) Flush dashboard_users
                for uname in list(self._pending_users.keys()):
                    entry = self._pending_users[uname]
                    op = entry.get("op")
                    if op == "delete":
                        await self.users_col.delete_one({"username": uname})
                    elif op == "upsert":
                        doc = copy.deepcopy(entry["doc"])
                        doc.pop("_id", None)
                        await self.users_col.replace_one({"username": uname}, doc, upsert=True)
                    self._pending_users.pop(uname, None)  # giải phóng khỏi hàng đợi RAM
                    flushed_items += 1

                # 4) Flush audit_logs (từng bản ghi theo thứ tự thời gian, lưu xong xóa ngay khỏi RAM)
                while self._pending_audits:
                    audit_doc = copy.deepcopy(self._pending_audits[0])
                    audit_doc.pop("pending", None)
                    await self.audit_col.insert_one(audit_doc)
                    self._pending_audits.pop(0)  # giải phóng khỏi RAM
                    flushed_items += 1

                if flushed_items > 0:
                    self._last_flush_at = datetime.now(timezone.utc).isoformat()
                    self._last_flush_error = None
                    log.info("Đã đồng bộ %d mục từ bộ đệm RAM lên MongoDB Atlas và giải phóng RAM.", flushed_items)
            except Exception as e:
                self._last_flush_error = f"{type(e).__name__}: {e}"[:200]
                log.warning("Chưa đẩy được bộ đệm RAM lên MongoDB Atlas (còn %d mục, sẽ thử lại): %s",
                            self.pending_count, self._last_flush_error)

        return self.sync_status()

    # ------------------------------------------------------------------
    # Nhật ký chỉnh sửa (`audit_logs`: lưu đệm RAM -> đẩy Atlas -> giải phóng)
    # ------------------------------------------------------------------
    async def record_audit(
        self,
        *,
        actor: str,
        actor_type: str = "dashboard",
        action: str,
        section: str,
        summary: str,
        changes: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        safe_changes = []
        for ch in changes or []:
            field = str(ch.get("field", ""))
            if _is_sensitive_field(field):
                continue
            safe_changes.append({
                "field": field,
                "before": _sanitize_audit_value(ch.get("before")),
                "after": _sanitize_audit_value(ch.get("after")),
            })
        doc = {
            "at": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "actor_type": actor_type,
            "action": action,
            "section": section,
            "summary": summary,
            "changes": safe_changes,
        }
        # 1) Lưu tạm vào hàng đợi RAM
        self._pending_audits.append(copy.deepcopy(doc))
        # Giới hạn an toàn bộ đệm audit tối đa 500 bản ghi nếu Atlas rớt quá lâu
        if len(self._pending_audits) > 500:
            self._pending_audits = self._pending_audits[-500:]
        # 2) Cố gắng xả ngay lên MongoDB Atlas và giải phóng RAM
        await self.flush_pending()
        return doc

    async def list_audit_logs(self, limit: int = 100, section: Optional[str] = None) -> list[dict[str, Any]]:
        sec = (section or "").strip()
        # Lấy các bản ghi đang chờ đồng bộ trong RAM trước (mới nhất lên đầu)
        pending_items = []
        for d in reversed(self._pending_audits):
            if not sec or d.get("section") == sec:
                item = copy.deepcopy(d)
                item["pending"] = True
                pending_items.append(item)

        db_items: list[dict[str, Any]] = []
        if self.connected and self.audit_col is not None:
            try:
                flt: dict[str, Any] = {"section": sec} if sec else {}
                db_items = await self.audit_col.find(flt, {"_id": 0}).sort("at", -1).to_list(max(1, min(300, limit)))
            except Exception as e:
                log.warning("Không đọc được audit_logs từ MongoDB: %s", e)

        return (pending_items + db_items)[:max(1, min(300, limit))]

    # ------------------------------------------------------------------
    # Đọc / Ghi cấu hình động (`bot_config`)
    # ------------------------------------------------------------------
    @property
    def current(self) -> dict[str, Any]:
        return self._config

    async def get_config(self, force: bool = False) -> dict[str, Any]:
        # Nếu có thay đổi đang chờ đẩy lên Atlas trong RAM -> ưu tiên bản RAM, tránh đọc bản cũ đè lên
        if self._pending_config is not None or not self.connected:
            return copy.deepcopy(self._config)
        now = time.monotonic()
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

    async def update_config(
        self,
        patch: dict[str, Any],
        updated_by: str = "dashboard",
        *,
        actor_type: str = "dashboard",
        section: str = "config",
        summary: str = "Cập nhật cấu hình bot",
    ) -> dict[str, Any]:
        """Lưu tạm trong RAM -> áp dụng tức thì -> đẩy lên MongoDB Atlas & giải phóng bộ đệm RAM."""
        current = await self.get_config(force=True)
        merged = deep_merge(current, patch)
        changes = compute_diff(current, merged)

        merged["_id"] = "global"
        merged["updated_at"] = datetime.now(timezone.utc).isoformat()
        merged["updated_by"] = updated_by

        # 1) Áp dụng ngay trên RAM + đưa vào hàng đợi chờ đồng bộ Atlas
        self._config = merged
        self._config_loaded_at = time.monotonic()
        self._pending_config = copy.deepcopy(merged)

        # 2) Ghi Audit Log vào hàng đợi RAM (chưa lộ trường nhạy cảm)
        if changes:
            self._pending_audits.append({
                "at": merged["updated_at"],
                "actor": updated_by,
                "actor_type": actor_type,
                "action": "update_config",
                "section": section,
                "summary": f"{summary} ({len(changes)} thay đổi)",
                "changes": changes,
            })

        # 3) Thử xả ngay lên MongoDB Atlas và giải phóng RAM (nếu lỗi, worker nền sẽ thử lại liên tục)
        await self.flush_pending()

        for cb in self._on_config_update_callbacks:
            try:
                cb(self._config)
            except Exception as e:
                log.warning("Lỗi callback cập nhật cấu hình: %s", e)

        return copy.deepcopy(self._config)

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
            merged = {str(d["_id"]): d for d in docs}
            # Giữ nguyên các thay đổi đang chờ đồng bộ trong RAM
            for uid, pval in self._pending_perms.items():
                if pval is None:
                    merged.pop(uid, None)
                else:
                    merged[uid] = copy.deepcopy(pval)
            self._perms_cache = merged
            self._perms_loaded_at = now
        except Exception as e:
            log.warning("Không đọc được discord_permissions từ MongoDB (dùng cache): %s", e)
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
        actor: Optional[str] = None,
        actor_type: str = "dashboard",
    ) -> dict[str, Any]:
        uid = str(user_id).strip()
        if not uid.isdigit():
            raise ValueError("Discord User ID phải là chuỗi chữ số hợp lệ.")
        if is_admin:
            can_dashboard = True
            can_status = True

        before = copy.deepcopy(self._perms_cache.get(uid))
        doc = {
            "_id": uid,
            "user_id": uid,
            "label": label.strip()[:80],
            "is_admin": bool(is_admin),
            "can_dashboard": bool(can_dashboard),
            "can_status": bool(can_status),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        # 1) Lưu tạm RAM + đưa vào hàng đợi sync
        self._perms_cache[uid] = doc
        self._perms_loaded_at = time.monotonic()
        self._pending_perms[uid] = copy.deepcopy(doc)

        if actor:
            changes = compute_diff(before or {}, doc, prefix=f"discord:{uid}")
            self._pending_audits.append({
                "at": doc["updated_at"],
                "actor": actor,
                "actor_type": actor_type,
                "action": "discord_perm_upsert",
                "section": "discord_permissions",
                "summary": f"{'Cập nhật' if before else 'Thêm'} quyền cho Discord ID {uid}",
                "changes": changes,
            })

        # 2) Đẩy ngay lên MongoDB Atlas & giải phóng RAM nếu kết nối OK
        await self.flush_pending()
        return doc

    async def delete_discord_permission(
        self,
        user_id: str,
        *,
        actor: Optional[str] = None,
        actor_type: str = "dashboard",
    ) -> bool:
        uid = str(user_id).strip()
        before = copy.deepcopy(self._perms_cache.get(uid))
        if before is None:
            return False
        self._perms_cache.pop(uid, None)
        self._perms_loaded_at = time.monotonic()
        self._pending_perms[uid] = None

        if actor:
            self._pending_audits.append({
                "at": datetime.now(timezone.utc).isoformat(),
                "actor": actor,
                "actor_type": actor_type,
                "action": "discord_perm_delete",
                "section": "discord_permissions",
                "summary": f"Xóa phân quyền Discord ID {uid}",
                "changes": [{"field": f"discord:{uid}", "before": before, "after": None}],
            })

        await self.flush_pending()
        return True

    async def get_discord_permission(self, user_id: int | str, *, force: bool = True) -> Optional[dict[str, Any]]:
        uid = str(user_id).strip()
        perms = await self.refresh_permissions(force=force)
        return perms.get(uid)

    async def can_use_status(self, user_id: int | str) -> bool:
        doc = await self.get_discord_permission(user_id, force=True)
        if not doc:
            return False
        return bool(doc.get("is_admin") or doc.get("can_status"))

    async def can_access_dashboard_discord(self, user_id: int | str) -> tuple[bool, bool]:
        doc = await self.get_discord_permission(user_id, force=True)
        if not doc:
            return False, False
        is_admin = bool(doc.get("is_admin"))
        return bool(is_admin or doc.get("can_dashboard")), is_admin

    # ------------------------------------------------------------------
    # Tài khoản đăng nhập truyền thống (`dashboard_users`)
    # ------------------------------------------------------------------
    async def list_dashboard_users(self) -> list[dict[str, Any]]:
        if self.connected and self.users_col is not None:
            try:
                docs = await self.users_col.find({}).to_list(200)
                merged = {d["username"]: d for d in docs if d.get("username")}
                for uname, entry in self._pending_users.items():
                    if entry.get("op") == "delete":
                        merged.pop(uname, None)
                    elif entry.get("op") == "upsert":
                        merged[uname] = copy.deepcopy(entry["doc"])
                self._users_cache = merged
            except Exception as e:
                log.warning("Không đọc được dashboard_users từ MongoDB (dùng cache): %s", e)

        out = []
        for uname in sorted(self._users_cache.keys()):
            d = self._users_cache[uname]
            out.append({
                "username": d.get("username", ""),
                "role": d.get("role", "admin"),
                "created_at": d.get("created_at"),
                "last_login_at": d.get("last_login_at"),
            })
        return out

    async def create_dashboard_user(
        self,
        username: str,
        password: str,
        role: str = "admin",
        *,
        actor: Optional[str] = None,
        actor_type: str = "dashboard",
    ) -> dict[str, Any]:
        uname = username.strip().lower()
        if not re.fullmatch(r"[a-z0-9_.-]{3,32}", uname):
            raise ValueError("Tên đăng nhập phải dài 3–32 ký tự (chữ thường, số, _, ., -).")
        if len(password) < 6:
            raise ValueError("Mật khẩu phải có ít nhất 6 ký tự.")
        if role not in ("admin", "operator"):
            raise ValueError("Vai trò không hợp lệ (chỉ nhận admin hoặc operator).")

        await self.list_dashboard_users()
        if uname in self._users_cache:
            raise ValueError(f"Tài khoản '{uname}' đã tồn tại.")

        doc = {
            "username": uname,
            "password_hash": hash_password(password),  # luôn băm scrypt trước khi lưu đệm RAM
            "role": role,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_login_at": None,
        }
        self._users_cache[uname] = doc
        self._pending_users[uname] = {"op": "upsert", "doc": copy.deepcopy(doc)}

        if actor:
            self._pending_audits.append({
                "at": doc["created_at"],
                "actor": actor,
                "actor_type": actor_type,
                "action": "user_create",
                "section": "dashboard_users",
                "summary": f"Tạo tài khoản Dashboard '{uname}' (vai trò: {role})",
                "changes": [{"field": "username", "before": None, "after": uname}, {"field": "role", "before": None, "after": role}],
            })

        await self.flush_pending()
        return {"username": uname, "role": role, "created_at": doc["created_at"], "last_login_at": None}

    async def update_dashboard_user_password(
        self,
        username: str,
        new_password: str,
        *,
        actor: Optional[str] = None,
        actor_type: str = "dashboard",
    ) -> bool:
        uname = username.strip().lower()
        if len(new_password) < 6:
            raise ValueError("Mật khẩu mới phải có ít nhất 6 ký tự.")
        await self.list_dashboard_users()
        if uname not in self._users_cache:
            return False

        doc = copy.deepcopy(self._users_cache[uname])
        doc["password_hash"] = hash_password(new_password)
        self._users_cache[uname] = doc
        self._pending_users[uname] = {"op": "upsert", "doc": copy.deepcopy(doc)}

        if actor:
            self._pending_audits.append({
                "at": datetime.now(timezone.utc).isoformat(),
                "actor": actor,
                "actor_type": actor_type,
                "action": "user_password_change",
                "section": "dashboard_users",
                "summary": f"Đổi mật khẩu tài khoản '{uname}' (mật khẩu được ẩn)",
                "changes": [{"field": "account", "before": uname, "after": "password_updated"}],
            })

        await self.flush_pending()
        return True

    async def delete_dashboard_user(
        self,
        username: str,
        *,
        actor: Optional[str] = None,
        actor_type: str = "dashboard",
    ) -> bool:
        uname = username.strip().lower()
        await self.list_dashboard_users()
        if uname not in self._users_cache:
            return False

        self._users_cache.pop(uname, None)
        self._pending_users[uname] = {"op": "delete"}

        if actor:
            self._pending_audits.append({
                "at": datetime.now(timezone.utc).isoformat(),
                "actor": actor,
                "actor_type": actor_type,
                "action": "user_delete",
                "section": "dashboard_users",
                "summary": f"Xóa tài khoản Dashboard '{uname}'",
                "changes": [{"field": "username", "before": uname, "after": None}],
            })

        await self.flush_pending()
        return True

    async def authenticate_user(self, username: str, password: str) -> Optional[dict[str, Any]]:
        uname = username.strip().lower()
        doc = None
        if self.connected and self.users_col is not None and uname not in self._pending_users:
            try:
                doc = await self.users_col.find_one({"username": uname})
                if doc:
                    self._users_cache[uname] = doc
            except Exception:
                doc = self._users_cache.get(uname)
        else:
            doc = self._users_cache.get(uname)

        if not doc or not verify_password(password, doc.get("password_hash", "")):
            return None

        now = datetime.now(timezone.utc).isoformat()
        doc["last_login_at"] = now
        self._users_cache[uname] = doc
        if self.connected and self.users_col is not None:
            try:
                await self.users_col.update_one({"username": uname}, {"$set": {"last_login_at": now}})
            except Exception:
                pass
        return {
            "username": uname,
            "role": doc.get("role", "admin"),
            "is_admin": doc.get("role", "admin") == "admin",
        }
