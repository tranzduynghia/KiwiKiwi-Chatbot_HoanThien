"""Dịch vụ nghiệp vụ CMS Pháp lý / ToS và quản lý Cổng đồng thuận có phiên bản."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.core.timezone import format_exact_timestamp, parse_utc_datetime
from app.features.config.repository import ConfigRepository
from app.features.config.service import ConfigService
from app.features.legal.defaults import (
    DEFAULT_ACTIVE_TOS_VERSION,
    DEFAULT_LEGAL_DOCS,
    LEGAL_DOC_KEYS,
    LEGAL_DOC_LABELS,
    build_default_legal_config,
    validate_tos_version,
)
from app.features.legal.repository import TosAcceptanceRepository


class LegalService:
    """Quản lý 4 tài liệu pháp lý, phiên bản `active_tos_version` và trạng thái đồng thuận của người dùng."""

    def __init__(
        self,
        *,
        config_repo: ConfigRepository,
        config_service: ConfigService,
        acceptance_repo: TosAcceptanceRepository,
    ) -> None:
        self._config_repo = config_repo
        self._config_service = config_service
        self._acceptances = acceptance_repo

    def _legal_cfg(self) -> dict[str, Any]:
        """Đọc nhánh `legal` từ cấu hình RAM (tự động bù giá trị mặc định nếu thiếu tài liệu)."""
        cfg = self._config_repo.current_config
        raw_legal = cfg.get("legal") if isinstance(cfg.get("legal"), dict) else {}
        defaults = build_default_legal_config()
        raw_docs = raw_legal.get("docs") if isinstance(raw_legal.get("docs"), dict) else {}

        merged_docs: dict[str, dict[str, str]] = {}
        for key in LEGAL_DOC_KEYS:
            default_doc = defaults["docs"][key]
            stored = raw_docs.get(key) if isinstance(raw_docs.get(key), dict) else {}
            merged_docs[key] = {
                "key": key,
                "label": LEGAL_DOC_LABELS[key],
                "title": str(stored.get("title") or default_doc["title"]).strip(),
                "subtitle": str(stored.get("subtitle") if stored.get("subtitle") is not None else default_doc["subtitle"]).strip(),
                "body": str(stored.get("body") or default_doc["body"]).strip(),
            }

        version = str(raw_legal.get("active_tos_version") or DEFAULT_ACTIVE_TOS_VERSION).strip() or DEFAULT_ACTIVE_TOS_VERSION
        updated_at = str(raw_legal.get("tos_updated_at") or defaults["tos_updated_at"]).strip()
        return {
            "active_tos_version": version,
            "tos_updated_at": updated_at,
            "docs": merged_docs,
        }

    @property
    def active_tos_version(self) -> str:
        """Phiên bản Điều khoản hiện hành đang áp dụng trên toàn hệ thống."""
        return self._legal_cfg()["active_tos_version"]

    @property
    def tos_updated_at(self) -> str:
        """Mốc thời gian UTC cập nhật nội dung pháp lý gần nhất."""
        return self._legal_cfg()["tos_updated_at"]

    def get_all_docs(self, tz_info: Optional[ZoneInfo] = None) -> dict[str, Any]:
        """Trả về toàn bộ 4 tài liệu pháp lý kèm phiên bản và thời gian cập nhật."""
        legal = self._legal_cfg()
        formatted_updated = format_exact_timestamp(legal["tos_updated_at"], tz_info) if tz_info else legal["tos_updated_at"]
        return {
            "active_tos_version": legal["active_tos_version"],
            "tos_updated_at": legal["tos_updated_at"],
            "tos_updated_at_formatted": formatted_updated,
            "docs": legal["docs"],
        }

    def get_doc(self, doc_key: str, tz_info: Optional[ZoneInfo] = None) -> dict[str, Any]:
        """Lấy một tài liệu pháp lý cụ thể (`legal` | `terms` | `policies` | `guidelines`)."""
        key = str(doc_key or "").strip().lower()
        if key not in LEGAL_DOC_KEYS:
            raise ValueError(f"Tài liệu pháp lý không hợp lệ: {doc_key}")
        legal = self._legal_cfg()
        doc = copy.deepcopy(legal["docs"][key])
        doc["active_tos_version"] = legal["active_tos_version"]
        doc["tos_updated_at"] = legal["tos_updated_at"]
        doc["tos_updated_at_formatted"] = (
            format_exact_timestamp(legal["tos_updated_at"], tz_info) if tz_info else legal["tos_updated_at"]
        )
        return doc

    async def update_document(
        self,
        *,
        doc_key: str,
        title: str,
        subtitle: str = "",
        body: str,
        active_tos_version: Optional[str] = None,
        actor: str,
        actor_type: str = "dashboard",
        actor_meta: Optional[dict[str, Any]] = None,
        client_changes: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Cập nhật nội dung một tài liệu pháp lý và/hoặc nâng phiên bản ToS, lưu MongoDB & ghi Audit Log."""
        key = str(doc_key or "").strip().lower()
        if key not in LEGAL_DOC_KEYS:
            raise ValueError(f"Tài liệu pháp lý không hợp lệ: {doc_key}")

        clean_title = " ".join(str(title or "").split()).strip()[:160]
        clean_subtitle = " ".join(str(subtitle or "").split()).strip()[:240]
        clean_body = str(body or "").replace("\r\n", "\n").strip()[:24000]
        if len(clean_title) < 2:
            raise ValueError("Tiêu đề tài liệu phải có ít nhất 2 ký tự.")
        if len(clean_body) < 10:
            raise ValueError("Nội dung tài liệu phải có ít nhất 10 ký tự.")

        current_legal = self._legal_cfg()
        next_version = (
            validate_tos_version(active_tos_version)
            if active_tos_version is not None and str(active_tos_version).strip()
            else current_legal["active_tos_version"]
        )

        now_iso = datetime.now(timezone.utc).isoformat()
        updated_docs = {
            k: {
                "title": current_legal["docs"][k]["title"],
                "subtitle": current_legal["docs"][k]["subtitle"],
                "body": current_legal["docs"][k]["body"],
            }
            for k in LEGAL_DOC_KEYS
        }
        updated_docs[key] = {
            "title": clean_title,
            "subtitle": clean_subtitle,
            "body": clean_body,
        }

        version_bumped = next_version != current_legal["active_tos_version"]
        summary = (
            f"Cập nhật {LEGAL_DOC_LABELS[key]} và nâng phiên bản ToS {current_legal['active_tos_version']} → {next_version}"
            if version_bumped
            else f"Cập nhật nội dung {LEGAL_DOC_LABELS[key]} (phiên bản {next_version})"
        )

        await self._config_service.update_config(
            {
                "legal": {
                    "active_tos_version": next_version,
                    "tos_updated_at": now_iso,
                    "docs": updated_docs,
                }
            },
            updated_by=actor,
            actor_type=actor_type,
            actor_meta=actor_meta,
            section="legal",
            summary=summary,
            client_changes=client_changes,
        )
        return self.get_all_docs()

    async def get_user_consent_status(self, user_id: str | int) -> dict[str, Any]:
        """Kiểm tra trạng thái đồng thuận của người dùng so với `active_tos_version` hiện hành."""
        active_ver = self.active_tos_version
        accepted_ver = await self._acceptances.get_accepted_version(user_id)
        accepted = bool(accepted_ver and accepted_ver == active_ver)
        return {
            "user_id": str(user_id),
            "active_tos_version": active_ver,
            "accepted_version": accepted_ver,
            "accepted": accepted,
            "is_update": bool(accepted_ver and accepted_ver != active_ver),
        }

    def has_accepted_current_tos_fast(self, user_id: str | int) -> bool:
        """Kiểm tra nhanh O(1) từ bộ đệm RAM xem người dùng đã đồng ý `active_tos_version` chưa."""
        cached = self._acceptances.get_cached_version(user_id)
        return bool(cached and cached == self.active_tos_version)

    async def accept_current_tos(
        self,
        *,
        user_id: str | int,
        username: str,
        source: str = "discord",
    ) -> dict[str, Any]:
        """Ghi nhận người dùng đã đồng ý phiên bản ToS hiện hành."""
        return await self._acceptances.record_acceptance(
            discord_user_id=user_id,
            discord_username=username,
            tos_accepted_version=self.active_tos_version,
            consent_source=source,
        )

    async def list_acceptances(
        self,
        *,
        limit: int = 15,
        cursor: Optional[str] = None,
        search: str = "",
        tz_info: ZoneInfo,
    ) -> dict[str, Any]:
        """Xuất danh sách chấp thuận ToS kèm định dạng thời gian chính xác theo múi giờ IP truy cập."""
        active_ver = self.active_tos_version
        raw = await self._acceptances.list_acceptances(limit=limit, cursor=cursor, search=search)
        formatted_items = []
        for doc in raw["items"]:
            dt = parse_utc_datetime(doc.get("tos_accepted_at"))
            accepted_ver = str(doc.get("tos_accepted_version") or "")
            formatted_items.append(
                {
                    "discord_user_id": str(doc.get("discord_user_id") or ""),
                    "discord_username": str(doc.get("discord_username") or ""),
                    "tos_accepted_version": accepted_ver,
                    "is_current_version": accepted_ver == active_ver,
                    "consent_source": str(doc.get("consent_source") or "discord"),
                    "tos_accepted_at": dt.isoformat() if dt else None,
                    "tos_accepted_at_formatted": format_exact_timestamp(dt, tz_info),
                }
            )
        return {
            "items": formatted_items,
            "active_tos_version": active_ver,
            "total": raw["total"],
            "limit": raw["limit"],
            "cursor": raw["cursor"],
            "next_cursor": raw["next_cursor"],
            "prev_cursor": raw["prev_cursor"],
            "has_more": raw["has_more"],
        }
