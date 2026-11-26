"""Kho lưu trữ tài khoản Dashboard (`dashboard_users`) kèm đệm RAM và hàng đợi đồng bộ Atlas."""
from __future__ import annotations

import copy
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.security import hash_password, redact_secrets

log = logging.getLogger("gemini-bot.auth")


def _safe_username_key(username: Any) -> str:
    """Chuẩn hóa username dạng chuỗi an toàn, chặn khóa bắt đầu bằng `$` hoặc chứa ký tự null."""
    if not isinstance(username, str):
        return ""
    cleaned = username.strip().lower()
    if not cleaned or len(cleaned) > 64 or "$" in cleaned or "\x00" in cleaned:
        return ""
    return cleaned


class UserRepository:
    """Quản lý tài khoản đăng nhập mật khẩu trên RAM cache và collection `dashboard_users`."""

    sync_key = "users"

    def __init__(self, on_initial_admin_created=None):
        self.collection: Any = None
        self._cache: dict[str, dict[str, Any]] = {}
        # Bản đồ chờ đồng bộ: username -> {"op": "upsert"|"delete", "doc": ...}
        self._pending: dict[str, dict[str, Any]] = {}
        self._on_initial_admin_created = on_initial_admin_created
        self._seed_from_env()

    def _seed_from_env(self) -> None:
        """Nạp sẵn tài khoản admin đầu tiên vào RAM từ `.env` để vẫn đăng nhập được khi Atlas tạm gián đoạn."""
        init_user = _safe_username_key(os.getenv("INITIAL_ADMIN_USERNAME") or "")
        init_pass = (os.getenv("INITIAL_ADMIN_PASSWORD") or "").strip()
        if init_user and init_pass:
            self._cache[init_user] = {
                "username": init_user,
                "password_hash": hash_password(init_pass),
                "session_version": secrets.token_hex(8),
                "role": "admin",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "last_login_at": None,
            }

    @property
    def connected(self) -> bool:
        """Cho biết collection `dashboard_users` đã sẵn sàng hay chưa."""
        return self.collection is not None

    @property
    def pending_count(self) -> int:
        """Số thao tác tài khoản đang nằm trong hàng đợi RAM chờ đồng bộ lên Atlas."""
        return len(self._pending)

    async def attach_db(self, db: Any) -> None:
        """Gắn collection `dashboard_users`, tạo unique index và đồng bộ cache đúng theo nguồn MongoDB."""
        self.collection = db["dashboard_users"]
        try:
            await self.collection.create_index("username", unique=True)
        except Exception as exc:
            log.warning("Không tạo được index cho dashboard_users: %s", redact_secrets(str(exc)))

        try:
            init_user = _safe_username_key(os.getenv("INITIAL_ADMIN_USERNAME") or "")
            init_pass = (os.getenv("INITIAL_ADMIN_PASSWORD") or "").strip()
            user_count = await self.collection.count_documents({})
            if user_count == 0 and init_user and init_pass:
                doc = self._cache.get(init_user) or {
                    "username": init_user,
                    "password_hash": hash_password(init_pass),
                    "session_version": secrets.token_hex(8),
                    "role": "admin",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "last_login_at": None,
                }
                await self.collection.insert_one(copy.deepcopy(doc))
                if self._on_initial_admin_created is not None:
                    await self._on_initial_admin_created(init_user)
                log.info("Đã tự động tạo tài khoản admin đầu tiên `%s` trên MongoDB Atlas.", init_user)

            # Thay thế cache theo dữ liệu MongoDB thực tế (giữ lại thao tác đang chờ đồng bộ) để loại bỏ tài khoản đã xóa
            db_users = await self.collection.find({}).to_list(200)
            merged: dict[str, dict[str, Any]] = {}
            for user_doc in db_users:
                uname = _safe_username_key(user_doc.get("username"))
                if uname:
                    merged[uname] = user_doc
            for uname, entry in self._pending.items():
                if entry.get("op") == "delete":
                    merged.pop(uname, None)
                elif entry.get("op") == "upsert":
                    merged[uname] = copy.deepcopy(entry["doc"])
            self._cache = merged
        except Exception as exc:
            log.warning("Lỗi khởi tạo dữ liệu dashboard_users trên MongoDB: %s", redact_secrets(str(exc)))

    async def flush_pending(self) -> int:
        """Đẩy các thao tác tạo/đổi mật khẩu/xóa tài khoản lên MongoDB Atlas rồi giải phóng hàng đợi."""
        if not self.connected or not self._pending:
            return 0
        flushed = 0
        for uname in list(self._pending.keys()):
            entry = self._pending[uname]
            op = entry.get("op")
            if op == "delete":
                await self.collection.delete_one({"username": uname})
            elif op == "upsert":
                doc = copy.deepcopy(entry["doc"])
                doc.pop("_id", None)
                await self.collection.replace_one({"username": uname}, doc, upsert=True)
            self._pending.pop(uname, None)
            flushed += 1
        return flushed

    async def refresh_all(self) -> dict[str, dict[str, Any]]:
        """Đồng bộ danh sách tài khoản từ MongoDB về RAM cache, bảo toàn các thay đổi đang chờ đẩy."""
        if self.connected:
            try:
                docs = await self.collection.find({}).to_list(200)
                merged = {doc["username"]: doc for doc in docs if doc.get("username")}
                for uname, entry in self._pending.items():
                    if entry.get("op") == "delete":
                        merged.pop(uname, None)
                    elif entry.get("op") == "upsert":
                        merged[uname] = copy.deepcopy(entry["doc"])
                self._cache = merged
            except Exception as exc:
                log.warning("Không đọc được dashboard_users từ MongoDB (dùng cache): %s", redact_secrets(str(exc)))
        return copy.deepcopy(self._cache)

    async def find_by_username(self, username: str) -> Optional[dict[str, Any]]:
        """Tìm tài khoản theo tên đăng nhập (ưu tiên RAM nếu có thay đổi chờ đồng bộ)."""
        uname = _safe_username_key(username)
        if not uname:
            return None
        if self.connected and uname not in self._pending:
            try:
                doc = await self.collection.find_one({"username": uname})
                if doc:
                    self._cache[uname] = doc
                    return copy.deepcopy(doc)
                self._cache.pop(uname, None)
                return None
            except Exception:
                return copy.deepcopy(self._cache.get(uname))
        doc = self._cache.get(uname)
        return copy.deepcopy(doc) if doc is not None else None

    async def record_last_login(self, username: str, timestamp_iso: str) -> None:
        """Cập nhật thời điểm đăng nhập gần nhất trên cache và MongoDB (nếu đang kết nối)."""
        uname = _safe_username_key(username)
        if not uname:
            return
        if uname in self._cache:
            self._cache[uname]["last_login_at"] = timestamp_iso
        if self.connected:
            try:
                await self.collection.update_one({"username": uname}, {"$set": {"last_login_at": timestamp_iso}})
            except Exception:
                pass

    def stage_upsert(self, doc: dict[str, Any]) -> None:
        """Lưu tài khoản đã băm scrypt vào RAM và hàng đợi đồng bộ Atlas."""
        uname = doc["username"]
        self._cache[uname] = copy.deepcopy(doc)
        self._pending[uname] = {"op": "upsert", "doc": copy.deepcopy(doc)}

    def stage_delete(self, username: str) -> None:
        """Xóa tài khoản khỏi RAM cache và xếp lệnh xóa vào hàng đợi đồng bộ Atlas."""
        uname = _safe_username_key(username)
        if not uname:
            return
        self._cache.pop(uname, None)
        self._pending[uname] = {"op": "delete"}
