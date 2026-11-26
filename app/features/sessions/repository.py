"""Kho lưu trữ phiên chat (`sessions`) và kho lưu trữ vĩnh viễn (`archives`) trên MongoDB Atlas."""
from __future__ import annotations

import copy
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pymongo import UpdateOne

from app.core.concurrency import UserLockManager
from app.core.database import MongoDatabaseManager
from app.features.config.repository import ConfigRepository
from app.features.sessions.history import clip_stored_text

log = logging.getLogger("gemini-bot.sessions")

MAX_SESSION_CACHE_ENTRIES = 200
MAX_PENDING_TURNS = 200


class SessionRepository:
    """Quản lý đọc, ghi, lưu trữ vĩnh viễn và quét hết hạn các phiên chat theo Discord User ID."""

    def __init__(
        self,
        db_manager: MongoDatabaseManager,
        config_repo: ConfigRepository,
        user_locks: UserLockManager,
    ):
        self._db_manager = db_manager
        self._config = config_repo
        self._locks = user_locks
        self.sessions_col: Any = None
        self.archives_col: Any = None
        self.ok: bool = False
        self.index_ok: bool = False
        self.last_error: Optional[str] = None
        # Bộ đệm đọc lịch sử phiên gần đây (có thể dọn an toàn ở Bước 3) và hàng đợi lượt chưa ghi được lên Atlas
        self._session_cache: dict[int, dict[str, Any]] = {}
        self._pending_turns: list[dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        """Kiểm tra bộ nhớ phiên đã được bật qua `MONGODB_URI` hay chưa."""
        return self._db_manager.enabled

    @property
    def pending_turn_count(self) -> int:
        """Số lượt hỏi đáp chưa đồng bộ xong đang nằm trong bộ đệm RAM."""
        return len(self._pending_turns)

    def _remember_session_cache(self, user_id: int, doc: dict[str, Any]) -> None:
        """Lưu bản sao phiên gần nhất vào bộ đệm RAM có giới hạn kích thước."""
        self._session_cache[user_id] = copy.deepcopy(doc)
        if len(self._session_cache) > MAX_SESSION_CACHE_ENTRIES:
            oldest_key = next(iter(self._session_cache))
            self._session_cache.pop(oldest_key, None)

    def purge_cache(self) -> int:
        """Bước 3 Watchdog: xóa toàn bộ bộ đệm lịch sử phiên trong RAM (không làm mất cấu hình bot)."""
        count = len(self._session_cache)
        self._session_cache.clear()
        return count

    def _enqueue_pending_turn(self, entry: dict[str, Any]) -> None:
        """Đưa lượt hỏi đáp chưa ghi được lên Atlas vào hàng đợi RAM để đồng bộ bù qua `bulk_write`."""
        self._pending_turns.append(copy.deepcopy(entry))
        if len(self._pending_turns) > MAX_PENDING_TURNS:
            self._pending_turns = self._pending_turns[-MAX_PENDING_TURNS:]

    async def flush_pending_bulk(self) -> int:
        """Bước 2 Watchdog: đồng bộ hàng loạt (`bulk_write`) mọi lượt chat đang nằm trong bộ đệm RAM lên Atlas."""
        if self.sessions_col is None or self.archives_col is None or not self._pending_turns:
            return 0

        pending = list(self._pending_turns)
        archive_ops = [
            UpdateOne(item["archive_filter"], item["archive_update"], upsert=True)
            for item in pending
        ]
        session_ops = [
            UpdateOne(item["session_filter"], item["session_update"], upsert=True)
            for item in pending
        ]

        archive_bulk = getattr(self.archives_col, "bulk_write", None)
        session_bulk = getattr(self.sessions_col, "bulk_write", None)
        if callable(archive_bulk) and callable(session_bulk):
            await archive_bulk(archive_ops, ordered=False)
            await session_bulk(session_ops, ordered=False)
        else:
            for item in pending:
                await self.archives_col.update_one(item["archive_filter"], item["archive_update"], upsert=True)
                await self.sessions_col.update_one(item["session_filter"], item["session_update"], upsert=True)

        self._pending_turns = self._pending_turns[len(pending):]
        self.ok = True
        self.last_error = None
        return len(pending)

    async def _run(self, make_coro, default=None):
        """Thực thi thao tác MongoDB có bắt lỗi an toàn để bot vẫn trả lời ngay cả khi DB gặp sự cố."""
        if self.sessions_col is None:
            return default
        try:
            result = await make_coro()
            self.ok = True
            self.last_error = None
            return result
        except Exception as exc:
            from app.core.security import redact_secrets

            self.ok = False
            self.last_error = redact_secrets(f"{type(exc).__name__}: {exc}", max_len=200)
            log.warning("MongoDB lỗi: %s", self.last_error)
            return default

    async def ensure_indexes(self) -> None:
        """Gỡ TTL index cũ (nếu có) và đảm bảo các index cần thiết cho `sessions` và `archives`."""
        if self.index_ok:
            return

        async def _create_indexes():
            info = await self.sessions_col.index_information()
            old_ttl = info.get("expire_at_1")
            if old_ttl and "expireAfterSeconds" in old_ttl:
                await self.sessions_col.drop_index("expire_at_1")
                log.warning("Đã gỡ TTL index cũ của sessions")
            await self.sessions_col.create_index("expire_at")
            await self.archives_col.create_index([("user_id", 1), ("created_at", -1)])
            await self.archives_col.create_index("user_id_str")
            return True

        self.index_ok = bool(await self._run(_create_indexes))

    async def attach_db(self, db: Any) -> None:
        """Gắn collections `sessions` và `archives` từ database đã kết nối và đồng bộ bù các lượt đang chờ."""
        self.sessions_col = db["sessions"]
        self.archives_col = db["archives"]
        self.index_ok = False
        await self.ensure_indexes()
        if self.ok:
            if self._pending_turns:
                try:
                    flushed = await self.flush_pending_bulk()
                    if flushed:
                        log.info("Đã đồng bộ bù %d lượt chat từ bộ đệm RAM lên MongoDB Atlas.", flushed)
                except Exception as exc:
                    log.warning("Chưa đồng bộ bù được lượt chat chờ: %s", exc)
            idle_hours = self._config.session_cfg().get("session_idle_hours", 2)
            log.info(
                "Đã kết nối MongoDB Atlas (db=%s), phiên tự lưu trữ sau %g giờ",
                self._db_manager.db_name,
                idle_hours,
            )

    async def end_session(
        self,
        user_id: int,
        reason: str,
        only_if_expired: bool = False,
    ) -> Optional[dict[str, Any]]:
        """Đóng phiên đang hoạt động và chốt bản ghi lưu trữ vĩnh viễn vào collection `archives`."""
        now = datetime.now(timezone.utc)
        query: dict[str, Any] = {"_id": user_id}
        if only_if_expired:
            query["expire_at"] = {"$lte": now}
        doc = await self._run(lambda: self.sessions_col.find_one(query))
        if not doc:
            self._session_cache.pop(user_id, None)
            return None

        session_id = doc.get("session_id") or uuid.uuid4().hex
        ended_at = doc.get("expire_at", now) if reason == "idle_timeout" else now
        res = await self._run(
            lambda: self.archives_col.update_one(
                {"_id": session_id},
                {
                    "$set": {"ended_at": ended_at, "end_reason": reason},
                    "$setOnInsert": {
                        "user_id": user_id,
                        "user_id_str": str(user_id),
                        "created_at": doc.get("created_at", now),
                        "turns": doc.get("turns", 0),
                        "messages": doc.get("messages", []),
                    },
                },
                upsert=True,
            )
        )
        if res is None:
            return None

        res = await self._run(
            lambda: self.sessions_col.delete_one({"_id": user_id, "session_id": doc.get("session_id")})
        )
        if res is None:
            return None
        self._session_cache.pop(user_id, None)
        return {"session_id": session_id, "turns": doc.get("turns", 0)}

    async def load(self, user_id: int) -> Optional[dict[str, Any]]:
        """Tải phiên hiện tại của người dùng; tự lưu trữ và làm mới nếu quá hạn nghỉ hoặc vượt số lượt."""
        doc = await self._run(lambda: self.sessions_col.find_one({"_id": user_id}))
        if not doc:
            self._session_cache.pop(user_id, None)
            return None
        scfg = self._config.session_cfg()
        ttl = timedelta(hours=float(scfg.get("session_idle_hours", 2.0)))
        max_turns = int(scfg.get("session_max_turns", 500))
        last_active = doc.get("last_active")
        if last_active and datetime.now(timezone.utc) - last_active > ttl:
            await self.end_session(user_id, "idle_timeout")
            return None
        if doc.get("turns", 0) >= max_turns:
            await self.end_session(user_id, "max_turns")
            return None
        self._remember_session_cache(user_id, doc)
        return doc

    async def archive_expired(self) -> int:
        """Quét toàn bộ phiên đã hết hạn để chuyển sang lưu trữ vĩnh viễn."""
        now = datetime.now(timezone.utc)
        docs = await self._run(
            lambda: self.sessions_col.find({"expire_at": {"$lte": now}}, {"_id": 1}).to_list(200),
            [],
        )
        archived_count = 0
        for item in docs or []:
            async with self._locks.get(item["_id"]):
                if await self.end_session(item["_id"], "idle_timeout", only_if_expired=True):
                    archived_count += 1
        if archived_count:
            log.info("Đã lưu trữ %d phiên hết hạn", archived_count)
        return archived_count

    async def save_turn(
        self,
        user_id: int,
        user_text: str,
        model_text: str,
        session_doc: Optional[dict[str, Any]],
        model_id: str,
        user_meta: Optional[dict[str, Any]] = None,
    ) -> Optional[int]:
        """Lưu lượt hỏi đáp vào `archives` và `sessions`; nếu Atlas tạm lỗi thì đệm RAM chờ `bulk_write`."""
        await self.ensure_indexes()
        scfg = self._config.session_cfg()
        ttl = timedelta(hours=float(scfg.get("session_idle_hours", 2.0)))
        max_hist_msgs = int(scfg.get("max_history_messages", 20))
        max_stored_chars = int(scfg.get("max_stored_chars", 4000))

        now = datetime.now(timezone.utc)
        is_fresh = session_doc is None
        session_id = uuid.uuid4().hex if is_fresh else (session_doc.get("session_id") or uuid.uuid4().hex)
        created_at = now if is_fresh else session_doc.get("created_at", now)

        archive_set: dict[str, Any] = {"last_active": now}
        if user_meta:
            for field in ("username", "display_name", "avatar_url", "guild_id", "guild_name", "server_roles"):
                if field in user_meta and user_meta[field] is not None:
                    archive_set[field] = user_meta[field]

        archive_filter = {"_id": session_id}
        archive_update = {
            "$push": {
                "messages": {
                    "$each": [
                        {"role": "user", "text": user_text, "at": now},
                        {"role": "model", "text": model_text, "at": now, "model": model_id},
                    ]
                }
            },
            "$set": archive_set,
            "$inc": {"turns": 1},
            "$setOnInsert": {
                "user_id": user_id,
                "user_id_str": str(user_id),
                "created_at": created_at,
                "ended_at": None,
                "end_reason": None,
            },
        }

        expire_at = now + ttl
        ctx_messages = [
            {"role": "user", "text": clip_stored_text(user_text, max_stored_chars)},
            {"role": "model", "text": clip_stored_text(model_text, max_stored_chars)},
        ]
        session_filter = {"_id": user_id}
        session_update = {
            "$push": {"messages": {"$each": ctx_messages, "$slice": -max_hist_msgs}},
            "$set": {"last_active": now, "expire_at": expire_at, "session_id": session_id},
            "$inc": {"turns": 1},
            "$setOnInsert": {"created_at": created_at},
        }

        archive_res = await self._run(
            lambda: self.archives_col.update_one(archive_filter, archive_update, upsert=True)
        )
        if archive_res is None:
            # Atlas tạm lỗi -> đưa vào hàng đợi RAM để MemoryWatchdog Bước 2 xả hàng loạt bằng bulk_write
            self._enqueue_pending_turn({
                "archive_filter": archive_filter,
                "archive_update": archive_update,
                "session_filter": session_filter,
                "session_update": session_update,
            })
            return None

        if is_fresh:
            turns = 1
            new_doc = {
                "_id": user_id,
                "session_id": session_id,
                "messages": ctx_messages,
                "turns": 1,
                "created_at": now,
                "last_active": now,
                "expire_at": expire_at,
            }
            sess_res = await self._run(lambda: self.sessions_col.replace_one({"_id": user_id}, new_doc, upsert=True))
            if sess_res is not None:
                self._remember_session_cache(user_id, new_doc)
        else:
            turns = session_doc.get("turns", 0) + 1
            sess_res = await self._run(
                lambda: self.sessions_col.update_one(session_filter, session_update, upsert=True)
            )
            if sess_res is not None:
                updated_msgs = (list(session_doc.get("messages") or []) + ctx_messages)[-max_hist_msgs:]
                self._remember_session_cache(
                    user_id,
                    {
                        "_id": user_id,
                        "session_id": session_id,
                        "messages": updated_msgs,
                        "turns": turns,
                        "created_at": created_at,
                        "last_active": now,
                        "expire_at": expire_at,
                    },
                )
        if sess_res is None:
            self._enqueue_pending_turn({
                "archive_filter": archive_filter,
                "archive_update": archive_update,
                "session_filter": session_filter,
                "session_update": session_update,
            })
            return None
        return turns

    async def active_count(self) -> Optional[int]:
        """Đếm số phiên còn hạn đang hoạt động trong collection `sessions`."""
        now = datetime.now(timezone.utc)
        return await self._run(lambda: self.sessions_col.count_documents({"expire_at": {"$gt": now}}))

    async def archive_count(self) -> Optional[int]:
        """Ước lượng tổng số phiên đã được lưu trữ trong collection `archives`."""
        return await self._run(lambda: self.archives_col.estimated_document_count())

    async def list_stored_server_roles(self) -> list[dict[str, str]]:
        """Thu thập danh sách vai trò server đã từng lưu trong collection `archives`."""
        if self.archives_col is None:
            return []
        docs = await self._run(
            lambda: self.archives_col.find({}, {"server_roles": 1, "guild_name": 1}).sort("last_active", -1).to_list(300),
            [],
        )
        roles_by_id: dict[str, dict[str, str]] = {}
        for doc in docs or []:
            guild_name = str(doc.get("guild_name") or "")
            for role in doc.get("server_roles") or []:
                if not isinstance(role, dict):
                    continue
                rid = str(role.get("id") or role.get("name") or "")
                name = str(role.get("name") or "")
                if not rid or not name or name == "@everyone" or rid in roles_by_id:
                    continue
                roles_by_id[rid] = {
                    "id": rid,
                    "name": name,
                    "color": str(role.get("color") or "#64748b"),
                    "guild_name": guild_name,
                }
        return sorted(roles_by_id.values(), key=lambda item: item["name"].lower())

    async def search_messages(
        self,
        *,
        query: str = "",
        user_id: str = "",
        username: str = "",
        server_role: str = "",
        role: str = "",
        limit: int = 50,
        offset: int = 0,
        user_resolver: Optional[Any] = None,
    ) -> dict[str, Any]:
        """Tìm kiếm và duyệt toàn bộ tin nhắn phiên chat từ MongoDB Atlas (`archives`) kèm bộ lọc đa chiều."""
        if self.archives_col is None:
            return {"items": [], "total": 0, "has_more": False}

        capped_limit = max(1, min(100, int(limit)))
        safe_offset = max(0, min(1000, int(offset)))
        clean_query = (query if isinstance(query, str) else "").strip()[:200]
        clean_uid = (user_id if isinstance(user_id, str) else "").strip()[:40]
        clean_username = (username if isinstance(username, str) else "").strip().lower()[:64]
        clean_server_role = (server_role if isinstance(server_role, str) else "").strip().lower()[:80]
        clean_role = (role if isinstance(role, str) else "").strip().lower()
        if clean_role not in ("user", "model"):
            clean_role = ""

        # Xây dựng bộ lọc MongoDB an toàn (re.escape chống ReDoS & chặn hoàn toàn toán tử NoSQL)
        mongo_filter: dict[str, Any] = {}
        if clean_uid:
            mongo_filter["user_id_str"] = {"$regex": re.escape(clean_uid), "$options": "i"}
        if clean_query:
            escaped = re.escape(clean_query)
            mongo_filter["$or"] = [
                {"messages.text": {"$regex": escaped, "$options": "i"}},
                {"_id": {"$regex": escaped, "$options": "i"}},
                {"user_id_str": {"$regex": escaped, "$options": "i"}},
                {"username": {"$regex": escaped, "$options": "i"}},
                {"display_name": {"$regex": escaped, "$options": "i"}},
            ]

        docs = await self._run(
            lambda: self.archives_col.find(mongo_filter).sort("last_active", -1).to_list(200),
            [],
        )

        from app.core.security import sanitize_https_url

        query_lower = clean_query.lower()
        seen_session_ids: set[str] = set()
        matched_sessions: list[dict[str, Any]] = []

        for doc in docs or []:
            chat_id = str(doc.get("_id") or "")
            if not chat_id or chat_id in seen_session_ids:
                continue
            seen_session_ids.add(chat_id)

            uid_str = str(doc.get("user_id_str") or doc.get("user_id") or "")
            session_status = "active" if doc.get("ended_at") is None else "archived"
            fallback_dt = doc.get("last_active") or doc.get("created_at")

            doc_username = str(doc.get("username") or "")
            doc_display = str(doc.get("display_name") or "")
            doc_avatar = sanitize_https_url(str(doc.get("avatar_url") or ""))
            doc_roles = doc.get("server_roles") if isinstance(doc.get("server_roles"), list) else []

            # Bổ sung hồ sơ & vai trò từ Discord client trực tiếp cho các bản ghi cũ chưa lưu metadata
            if user_resolver is not None and uid_str and (not doc_username or not doc_avatar or not doc_roles):
                resolved = await user_resolver(uid_str)
                if resolved:
                    doc_username = doc_username or str(resolved.get("username") or "")
                    doc_display = doc_display or str(resolved.get("display_name") or "")
                    doc_avatar = doc_avatar or sanitize_https_url(str(resolved.get("avatar_url") or ""))
                    if not doc_roles and resolved.get("server_roles"):
                        doc_roles = resolved["server_roles"]

            # Áp dụng bộ lọc theo username / display_name
            if clean_username:
                if clean_username not in doc_username.lower() and clean_username not in doc_display.lower():
                    continue

            # Áp dụng bộ lọc theo vai trò server (khớp theo Role ID hoặc tên vai trò)
            if clean_server_role:
                role_hit = any(
                    clean_server_role == str(r.get("id") or "").lower()
                    or clean_server_role in str(r.get("name") or "").lower()
                    for r in doc_roles
                    if isinstance(r, dict)
                )
                if not role_hit:
                    continue

            messages = doc.get("messages") or []
            meta_matched = bool(
                query_lower
                and (
                    query_lower in chat_id.lower()
                    or query_lower in uid_str.lower()
                    or query_lower in doc_username.lower()
                    or query_lower in doc_display.lower()
                )
            )

            # Kiểm tra phiên có khớp bộ lọc loại tin (user/model) và từ khóa nội dung hay không
            if clean_role or (query_lower and not meta_matched):
                msg_matched = False
                for msg in messages:
                    msg_role = str(msg.get("role") or "")
                    if clean_role and msg_role != clean_role:
                        continue
                    msg_text = str(msg.get("text") or "")
                    if not query_lower or meta_matched or query_lower in msg_text.lower():
                        msg_matched = True
                        break
                if not msg_matched:
                    continue

            # Lấy câu hỏi đầu tiên của người dùng làm bản xem trước ngắn gọn cho bảng tổng hợp
            first_user_msg = next(
                (str(m.get("text") or "").strip() for m in messages if m.get("role") == "user" and m.get("text")),
                "",
            )
            if not first_user_msg and messages:
                first_user_msg = str(messages[0].get("text") or "").strip()
            preview_single_line = " ".join(first_user_msg.split())
            preview = (
                preview_single_line
                if len(preview_single_line) <= 140
                else preview_single_line[:140] + "…"
            )

            if isinstance(fallback_dt, datetime):
                ts_iso = fallback_dt.isoformat()
            else:
                ts_iso = str(fallback_dt) if fallback_dt else ""

            created_dt = doc.get("created_at")
            created_iso = created_dt.isoformat() if isinstance(created_dt, datetime) else (str(created_dt) if created_dt else "")

            matched_sessions.append({
                "chat_id": chat_id,
                "chat_id_short": chat_id[:8],
                "user_id": uid_str,
                "username": doc_username,
                "display_name": doc_display or doc_username or uid_str,
                "avatar_url": doc_avatar,
                "server_roles": doc_roles,
                "preview": preview or "(Phiên chat trống)",
                "content": preview or "(Phiên chat trống)",
                "turns": int(doc.get("turns") or 0),
                "message_count": len(messages),
                "timestamp": ts_iso,
                "created_at": created_iso,
                "session_status": session_status,
            })

        # Sắp xếp phiên mới nhất lên đầu rồi phân trang
        matched_sessions.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
        total = len(matched_sessions)
        page_items = matched_sessions[safe_offset : safe_offset + capped_limit]
        return {
            "items": page_items,
            "total": total,
            "has_more": (safe_offset + capped_limit) < total,
        }

    async def get_session_detail(
        self,
        chat_id: str,
        *,
        user_resolver: Optional[Any] = None,
    ) -> Optional[dict[str, Any]]:
        """Lấy chi tiết toàn bộ hội thoại và thông tin người dùng của một phiên chat theo `chat_id`."""
        if self.archives_col is None or not isinstance(chat_id, str):
            return None
        clean_id = chat_id.strip()
        if not clean_id or len(clean_id) > 128 or "$" in clean_id or "\x00" in clean_id:
            return None

        doc = await self._run(lambda: self.archives_col.find_one({"_id": clean_id}))
        if not doc:
            return None

        from app.core.security import sanitize_https_url

        uid_str = str(doc.get("user_id_str") or doc.get("user_id") or "")
        doc_username = str(doc.get("username") or "")
        doc_display = str(doc.get("display_name") or "")
        doc_avatar = sanitize_https_url(str(doc.get("avatar_url") or ""))
        doc_roles = doc.get("server_roles") if isinstance(doc.get("server_roles"), list) else []

        if user_resolver is not None and uid_str and (not doc_username or not doc_avatar or not doc_roles):
            resolved = await user_resolver(uid_str)
            if resolved:
                doc_username = doc_username or str(resolved.get("username") or "")
                doc_display = doc_display or str(resolved.get("display_name") or "")
                doc_avatar = doc_avatar or sanitize_https_url(str(resolved.get("avatar_url") or ""))
                if not doc_roles and resolved.get("server_roles"):
                    doc_roles = resolved["server_roles"]

        def _fmt_dt(val: Any) -> str:
            if isinstance(val, datetime):
                return val.isoformat()
            return str(val) if val else ""

        messages = []
        for msg in doc.get("messages") or []:
            messages.append({
                "role": str(msg.get("role") or "user"),
                "content": str(msg.get("text") or ""),
                "model": msg.get("model"),
                "timestamp": _fmt_dt(msg.get("at") or doc.get("last_active") or doc.get("created_at")),
            })

        return {
            "chat_id": str(doc.get("_id") or ""),
            "chat_id_short": str(doc.get("_id") or "")[:8],
            "user_id": uid_str,
            "username": doc_username,
            "display_name": doc_display or doc_username or uid_str,
            "avatar_url": doc_avatar,
            "server_roles": doc_roles,
            "guild_name": str(doc.get("guild_name") or ""),
            "turns": int(doc.get("turns") or 0),
            "created_at": _fmt_dt(doc.get("created_at")),
            "last_active": _fmt_dt(doc.get("last_active")),
            "ended_at": _fmt_dt(doc.get("ended_at")),
            "end_reason": doc.get("end_reason"),
            "session_status": "active" if doc.get("ended_at") is None else "archived",
            "messages": messages,
        }


