"""Dịch vụ nghiệp vụ CMS Pháp lý 5 trang (đọc theo yêu cầu từ MongoDB, không đệm HTML trong RAM) & Cổng đồng thuận ToS."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.core.timezone import format_exact_timestamp, parse_utc_datetime
from app.features.audit.service import AuditService
from app.features.config.repository import ConfigRepository
from app.features.config.service import ConfigService
from app.features.legal.defaults import (
    DEFAULT_ACTIVE_TOS_VERSION,
    LEGAL_DOC_KEYS,
    LEGAL_DOC_LABELS,
    validate_tos_version,
)
from app.features.legal.repository import LegalDocumentRepository, TosAcceptanceRepository
from app.features.legal.sanitizer import sanitize_legal_html


class LegalService:
    """Giữ duy nhất `active_tos_version` và cache đồng thuận trong RAM; đọc HTML 5 trang pháp lý theo yêu cầu từ MongoDB."""

    def __init__(
        self,
        *,
        config_repo: ConfigRepository,
        config_service: ConfigService,
        document_repo: LegalDocumentRepository,
        acceptance_repo: TosAcceptanceRepository,
        audit_service: Optional[AuditService] = None,
    ) -> None:
        self._config_repo = config_repo
        self._config_service = config_service
        self._documents = document_repo
        self._acceptances = acceptance_repo
        self._audit = audit_service

    def _legal_meta(self) -> dict[str, str]:
        """Đọc siêu dữ liệu gọn nhẹ (`active_tos_version`, `tos_updated_at`) từ `bot_config` RAM."""
        cfg = self._config_repo.current_config
        raw_legal = cfg.get("legal") if isinstance(cfg.get("legal"), dict) else {}
        version = str(raw_legal.get("active_tos_version") or DEFAULT_ACTIVE_TOS_VERSION).strip() or DEFAULT_ACTIVE_TOS_VERSION
        updated_at = str(raw_legal.get("tos_updated_at") or datetime.now(timezone.utc).isoformat()).strip()
        return {
            "active_tos_version": version,
            "tos_updated_at": updated_at,
        }

    @property
    def active_tos_version(self) -> str:
        """Phiên bản Điều khoản hiện hành (chuỗi ngắn lưu đệm trong RAM)."""
        return self._legal_meta()["active_tos_version"]

    @property
    def tos_updated_at(self) -> str:
        """Mốc thời gian UTC cập nhật nội dung pháp lý gần nhất."""
        return self._legal_meta()["tos_updated_at"]

    async def get_doc(self, doc_key: str, tz_info: Optional[ZoneInfo] = None) -> dict[str, Any]:
        """Truy vấn theo yêu cầu 1 trang pháp lý từ MongoDB `legal_documents` (không đệm toàn bộ HTML trong RAM)."""
        meta = self._legal_meta()
        doc = await self._documents.get_document(doc_key)
        effective_updated = doc.get("updated_at") or meta["tos_updated_at"]
        return {
            **doc,
            "active_tos_version": meta["active_tos_version"],
            "tos_updated_at": effective_updated,
            "tos_updated_at_formatted": (
                format_exact_timestamp(effective_updated, tz_info) if tz_info else effective_updated
            ),
        }

    async def get_cms_overview(
        self,
        *,
        doc_key: str = "toslinks",
        tz_info: Optional[ZoneInfo] = None,
    ) -> dict[str, Any]:
        """Tải danh sách siêu dữ liệu 5 trang + nội dung HTML theo yêu cầu của đúng 1 trang đang chọn sửa."""
        meta = self._legal_meta()
        safe_key = doc_key if doc_key in LEGAL_DOC_KEYS else "toslinks"
        active_doc = await self.get_doc(safe_key, tz_info=tz_info)
        pages_meta = [
            {"key": k, "path": f"/{k}", "label": LEGAL_DOC_LABELS[k]}
            for k in LEGAL_DOC_KEYS
        ]
        return {
            "active_tos_version": meta["active_tos_version"],
            "tos_updated_at": meta["tos_updated_at"],
            "tos_updated_at_formatted": (
                format_exact_timestamp(meta["tos_updated_at"], tz_info) if tz_info else meta["tos_updated_at"]
            ),
            "pages": pages_meta,
            "doc": active_doc,
        }

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
        """Làm sạch HTML bằng stdlib `html.parser`, lưu `legal_documents`, cập nhật `active_tos_version` và ghi Audit Log."""
        key = str(doc_key or "").strip().lower()
        if key not in LEGAL_DOC_KEYS:
            raise ValueError(f"Tài liệu pháp lý không hợp lệ: {doc_key}")

        clean_title = " ".join(str(title or "").split()).strip()[:160]
        clean_subtitle = " ".join(str(subtitle or "").split()).strip()[:240]
        clean_body = sanitize_legal_html(body)
        if len(clean_title) < 2:
            raise ValueError("Tiêu đề tài liệu phải có ít nhất 2 ký tự.")
        if len(clean_body) < 10:
            raise ValueError("Nội dung tài liệu sau khi lọc an toàn phải có ít nhất 10 ký tự.")

        meta = self._legal_meta()
        old_doc = await self._documents.get_document(key)
        next_version = (
            validate_tos_version(active_tos_version)
            if active_tos_version is not None and str(active_tos_version).strip()
            else meta["active_tos_version"]
        )
        now_iso = datetime.now(timezone.utc).isoformat()

        # 1) Lưu nội dung Rich-Text đã làm sạch xuống collection riêng `legal_documents` (không phình bot_config RAM)
        await self._documents.save_document(
            doc_key=key,
            title=clean_title,
            subtitle=clean_subtitle,
            body_html=clean_body,
            updated_by=actor,
            updated_at=now_iso,
        )

        # 2) Cập nhật siêu dữ liệu gọn nhẹ `legal.active_tos_version` + `legal.tos_updated_at` trong `bot_config`
        version_bumped = next_version != meta["active_tos_version"]
        summary = (
            f"Cập nhật {LEGAL_DOC_LABELS[key]} và nâng phiên bản ToS {meta['active_tos_version']} → {next_version}"
            if version_bumped
            else f"Cập nhật nội dung Rich-Text {LEGAL_DOC_LABELS[key]} (phiên bản {next_version})"
        )

        # Tổng hợp diff cụ thể cho Audit Log
        audit_changes: list[dict[str, Any]] = []
        if version_bumped:
            audit_changes.append({
                "field": "legal.active_tos_version",
                "before": meta["active_tos_version"],
                "after": next_version,
            })
        if old_doc.get("title") != clean_title:
            audit_changes.append({
                "field": f"legal.docs.{key}.title",
                "before": old_doc.get("title"),
                "after": clean_title,
            })
        if old_doc.get("subtitle") != clean_subtitle:
            audit_changes.append({
                "field": f"legal.docs.{key}.subtitle",
                "before": old_doc.get("subtitle"),
                "after": clean_subtitle,
            })
        if old_doc.get("body") != clean_body:
            audit_changes.append({
                "field": f"legal.docs.{key}.body",
                "before": f"{len(str(old_doc.get('body') or ''))} ký tự HTML",
                "after": f"{len(clean_body)} ký tự HTML",
            })

        await self._config_service.update_config(
            {
                "legal": {
                    "active_tos_version": next_version,
                    "tos_updated_at": now_iso,
                }
            },
            updated_by=actor,
            actor_type=actor_type,
            actor_meta=actor_meta,
            section="legal",
            summary=summary,
            client_changes=client_changes,
            record_audit=False if self._audit is not None else True,
        )

        if self._audit is not None:
            await self._audit.record(
                actor=actor,
                actor_type=actor_type,
                actor_meta=actor_meta,
                action="legal_cms_update",
                section="legal",
                summary=summary,
                changes=audit_changes,
                timestamp=now_iso,
                source="Dashboard Legal CMS",
                flush=True,
            )

        # Xóa bộ đệm Cloudflare Edge bất đồng bộ (non-blocking) cho cả 5 URL pháp lý
        from app.features.legal.cloudflare_purge import schedule_legal_cache_purge

        schedule_legal_cache_purge()

        return await self.get_cms_overview(doc_key=key)

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
