"""Kho lưu trữ phiên chat (`sessions`) và kho lưu trữ vĩnh viễn (`archives`) trên MongoDB Atlas."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.core.concurrency import UserLockManager
from app.core.database import MongoDatabaseManager
from app.features.config.repository import ConfigRepository
from app.features.sessions.history import clip_stored_text

log = logging.getLogger("gemini-bot.sessions")


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

    @property
    def enabled(self) -> bool:
        """Kiểm tra bộ nhớ phiên đã được bật qua `MONGODB_URI` hay chưa."""
        return self._db_manager.enabled

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
            self.ok = False
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
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
        """Gắn collections `sessions` và `archives` từ database đã kết nối."""
        self.sessions_col = db["sessions"]
        self.archives_col = db["archives"]
        self.index_ok = False
        await self.ensure_indexes()
        if self.ok:
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
        return {"session_id": session_id, "turns": doc.get("turns", 0)}

    async def load(self, user_id: int) -> Optional[dict[str, Any]]:
        """Tải phiên hiện tại của người dùng; tự lưu trữ và làm mới nếu quá hạn nghỉ hoặc vượt số lượt."""
        doc = await self._run(lambda: self.sessions_col.find_one({"_id": user_id}))
        if not doc:
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
    ) -> Optional[int]:
        """Lưu lượt hỏi đáp vào cả kho lưu trữ vĩnh viễn (`archives`) và ngữ cảnh phiên (`sessions`)."""
        await self.ensure_indexes()
        scfg = self._config.session_cfg()
        ttl = timedelta(hours=float(scfg.get("session_idle_hours", 2.0)))
        max_hist_msgs = int(scfg.get("max_history_messages", 20))
        max_stored_chars = int(scfg.get("max_stored_chars", 4000))

        now = datetime.now(timezone.utc)
        is_fresh = session_doc is None
        session_id = uuid.uuid4().hex if is_fresh else (session_doc.get("session_id") or uuid.uuid4().hex)
        created_at = now if is_fresh else session_doc.get("created_at", now)

        archive_res = await self._run(
            lambda: self.archives_col.update_one(
                {"_id": session_id},
                {
                    "$push": {
                        "messages": {
                            "$each": [
                                {"role": "user", "text": user_text, "at": now},
                                {"role": "model", "text": model_text, "at": now, "model": model_id},
                            ]
                        }
                    },
                    "$set": {"last_active": now},
                    "$inc": {"turns": 1},
                    "$setOnInsert": {
                        "user_id": user_id,
                        "user_id_str": str(user_id),
                        "created_at": created_at,
                        "ended_at": None,
                        "end_reason": None,
                    },
                },
                upsert=True,
            )
        )
        if archive_res is None:
            return None

        expire_at = now + ttl
        ctx_messages = [
            {"role": "user", "text": clip_stored_text(user_text, max_stored_chars)},
            {"role": "model", "text": clip_stored_text(model_text, max_stored_chars)},
        ]
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
        else:
            turns = session_doc.get("turns", 0) + 1
            sess_res = await self._run(
                lambda: self.sessions_col.update_one(
                    {"_id": user_id},
                    {
                        "$push": {"messages": {"$each": ctx_messages, "$slice": -max_hist_msgs}},
                        "$set": {"last_active": now, "expire_at": expire_at, "session_id": session_id},
                        "$inc": {"turns": 1},
                        "$setOnInsert": {"created_at": created_at},
                    },
                    upsert=True,
                )
            )
        return turns if sess_res is not None else None

    async def active_count(self) -> Optional[int]:
        """Đếm số phiên còn hạn đang hoạt động trong collection `sessions`."""
        now = datetime.now(timezone.utc)
        return await self._run(lambda: self.sessions_col.count_documents({"expire_at": {"$gt": now}}))

    async def archive_count(self) -> Optional[int]:
        """Ước lượng tổng số phiên đã được lưu trữ trong collection `archives`."""
        return await self._run(lambda: self.archives_col.estimated_document_count())
