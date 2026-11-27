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
    TEMP_DRAFT_DOC_KEY,
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
        atlas_watcher: Optional[Any] = None,
    ) -> None:
        self._config_repo = config_repo
        self._config_service = config_service
        self._documents = document_repo
        self._acceptances = acceptance_repo
        self._audit = audit_service
        self._atlas_watcher = atlas_watcher

    def set_atlas_watcher(self, watcher: Any) -> None:
        """Gắn `AtlasConfigWatcher` để đồng bộ ngay ảnh chụp RAM sau mỗi lần Admin lưu tài liệu pháp lý."""
        self._atlas_watcher = watcher

    def _legal_meta(self) -> dict[str, Optional[str]]:
        """Đọc thuần túy siêu dữ liệu (`active_tos_version`, `tos_updated_at`) từ `bot_config` RAM (không sinh timestamp động)."""
        cfg = self._config_repo.current_config
        raw_legal = cfg.get("legal") if isinstance(cfg.get("legal"), dict) else {}
        version = str(raw_legal.get("active_tos_version") or DEFAULT_ACTIVE_TOS_VERSION).strip() or DEFAULT_ACTIVE_TOS_VERSION
        raw_updated = raw_legal.get("tos_updated_at")
        updated_at = str(raw_updated).strip() if raw_updated else None
        return {
            "active_tos_version": version,
            "tos_updated_at": updated_at,
        }

    @property
    def active_tos_version(self) -> str:
        """Phiên bản Điều khoản hiện hành (chuỗi ngắn lưu đệm trong RAM)."""
        return str(self._legal_meta()["active_tos_version"] or DEFAULT_ACTIVE_TOS_VERSION)

    @property
    def tos_updated_at(self) -> Optional[str]:
        """Mốc thời gian UTC cập nhật nội dung pháp lý gần nhất (chỉ đổi khi Admin lưu)."""
        return self._legal_meta()["tos_updated_at"]

    async def get_doc(self, doc_key: str, tz_info: Optional[ZoneInfo] = None) -> dict[str, Any]:
        """Truy vấn theo yêu cầu 1 trang pháp lý từ MongoDB `legal_documents` (100% read-only, không đệm HTML trong RAM)."""
        from app.core.timezone import format_minute_timestamp

        meta = self._legal_meta()
        doc = await self._documents.get_document(doc_key)
        effective_updated = doc.get("updated_at") or meta["tos_updated_at"]
        formatted_updated = (
            (format_exact_timestamp(effective_updated, tz_info) if tz_info else effective_updated)
            if effective_updated
            else "—"
        )
        formatted_minute = (
            (format_minute_timestamp(effective_updated, tz_info) if tz_info else effective_updated)
            if effective_updated
            else "—"
        )
        return {
            **doc,
            "active_tos_version": meta["active_tos_version"],
            "tos_updated_at": effective_updated,
            "tos_updated_at_formatted": formatted_updated,
            "updated_at_utc": effective_updated,
            "updated_at_minute_formatted": formatted_minute,
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
        meta_updated = meta["tos_updated_at"]
        formatted_meta_updated = (
            (format_exact_timestamp(meta_updated, tz_info) if tz_info else meta_updated)
            if meta_updated
            else "—"
        )
        return {
            "active_tos_version": meta["active_tos_version"],
            "tos_updated_at": meta_updated,
            "tos_updated_at_formatted": formatted_meta_updated,
            "pages": pages_meta,
            "doc": active_doc,
            "discord_prompt": self.get_discord_prompt_config(),
        }

    def get_discord_prompt_config(self) -> dict[str, str]:
        """Đọc thuần túy (100% read-only) mẫu tin nhắn yêu cầu đồng ý ToS trên Discord và nhãn nút từ `bot_config` RAM."""
        from app.features.legal.defaults import (
            DEFAULT_DISCORD_TOS_BUTTON_LABEL,
            DEFAULT_DISCORD_TOS_UPDATE_TEMPLATE,
            DEFAULT_DISCORD_TOS_WELCOME_TEMPLATE,
            PUBLIC_SITE_ORIGIN,
        )

        cfg = self._config_repo.current_config
        raw_legal = cfg.get("legal") if isinstance(cfg.get("legal"), dict) else {}
        welcome = str(raw_legal.get("discord_tos_welcome_template") or "").strip() or DEFAULT_DISCORD_TOS_WELCOME_TEMPLATE
        update_tpl = str(raw_legal.get("discord_tos_update_template") or "").strip() or DEFAULT_DISCORD_TOS_UPDATE_TEMPLATE
        btn_label = str(raw_legal.get("discord_tos_button_label") or "").strip()[:60] or DEFAULT_DISCORD_TOS_BUTTON_LABEL
        return {
            "discord_tos_welcome_template": welcome,
            "discord_tos_update_template": update_tpl,
            "discord_tos_button_label": btn_label,
            "base_url": PUBLIC_SITE_ORIGIN,
        }

    async def update_discord_prompt_config(
        self,
        *,
        welcome_template: str,
        update_template: str,
        button_label: str,
        actor: str,
        actor_type: str = "dashboard",
        actor_meta: Optional[dict[str, Any]] = None,
        client_changes: Optional[dict[str, Any]] = None,
    ) -> dict[str, str]:
        """Lưu mẫu tin nhắn yêu cầu đồng ý ToS trên Discord, đồng bộ snapshot watcher và ghi Audit Log (không sửa `tos_updated_at`)."""
        clean_welcome = str(welcome_template or "").strip()[:1500]
        clean_update = str(update_template or "").strip()[:1500]
        clean_label = " ".join(str(button_label or "").split()).strip()[:60]
        if len(clean_welcome) < 10:
            raise ValueError("Mẫu tin nhắn lần đầu phải có ít nhất 10 ký tự.")
        if len(clean_update) < 10:
            raise ValueError("Mẫu tin nhắn cập nhật phiên bản phải có ít nhất 10 ký tự.")
        if not clean_label:
            raise ValueError("Nhãn nút đồng ý không được để trống.")

        old_cfg = self.get_discord_prompt_config()
        audit_changes: list[dict[str, Any]] = []
        if old_cfg["discord_tos_welcome_template"] != clean_welcome:
            audit_changes.append({
                "field": "legal.discord_tos_welcome_template",
                "before": old_cfg["discord_tos_welcome_template"],
                "after": clean_welcome,
            })
        if old_cfg["discord_tos_update_template"] != clean_update:
            audit_changes.append({
                "field": "legal.discord_tos_update_template",
                "before": old_cfg["discord_tos_update_template"],
                "after": clean_update,
            })
        if old_cfg["discord_tos_button_label"] != clean_label:
            audit_changes.append({
                "field": "legal.discord_tos_button_label",
                "before": old_cfg["discord_tos_button_label"],
                "after": clean_label,
            })

        summary = "Cập nhật mẫu tin nhắn yêu cầu đồng ý ToS trên Discord"
        await self._config_service.update_config(
            {
                "legal": {
                    "discord_tos_welcome_template": clean_welcome,
                    "discord_tos_update_template": clean_update,
                    "discord_tos_button_label": clean_label,
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
        if self._atlas_watcher is not None:
            self._atlas_watcher.sync_snapshot()

        if self._audit is not None and audit_changes:
            await self._audit.record(
                actor=actor,
                actor_type=actor_type,
                actor_meta=actor_meta,
                action="legal_discord_prompt_update",
                section="legal",
                summary=summary,
                changes=audit_changes,
                source="Dashboard Legal CMS",
                flush=True,
            )

        return self.get_discord_prompt_config()

    async def revoke_user_consent(
        self,
        *,
        discord_user_id: str | int,
        actor: str,
        actor_type: str = "dashboard",
        actor_meta: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Thu hồi chấp thuận ToS của 1 người dùng, xóa ngay khỏi cache RAM và ghi Audit Log `TOS_CONSENT_REVOKED`."""
        result = await self._acceptances.revoke_acceptance(discord_user_id)
        uid = result["discord_user_id"]
        prev_ver = result.get("previous_version")
        if self._audit is not None:
            await self._audit.record(
                actor=actor,
                actor_type=actor_type,
                actor_meta=actor_meta,
                action="TOS_CONSENT_REVOKED",
                section="legal",
                summary=f"Thu hồi chấp thuận ToS của người dùng {uid} (phiên bản trước: {prev_ver or 'không rõ'})",
                changes=[
                    {
                        "field": f"tos_acceptances.{uid}.tos_accepted_version",
                        "before": prev_ver,
                        "after": None,
                    }
                ],
                source="Dashboard Legal CMS",
                extra_fields={
                    "target_discord_user_id": uid,
                    "previous_accepted_version": prev_ver,
                },
                flush=True,
            )
        return result

    @staticmethod
    def _resolve_author_info(
        actor: str,
        actor_type: str,
        actor_meta: Optional[dict[str, Any]],
    ) -> tuple[str, Optional[str]]:
        """Trích xuất tên hiển thị và avatar Discord hợp lệ của tác giả lưu tài liệu."""
        from app.features.legal.repository import validate_discord_avatar_url

        meta_dict = actor_meta if isinstance(actor_meta, dict) else {}
        author_name = str(meta_dict.get("actor_display_name") or "").strip()
        if not author_name:
            raw_actor = str(actor or "Admin").strip()
            author_name = raw_actor.split(" (discord:")[0].strip() or raw_actor
        author_avatar_url: Optional[str] = None
        if str(actor_type or "").strip().lower() == "discord" or meta_dict.get("actor_discord_id"):
            author_avatar_url = validate_discord_avatar_url(
                meta_dict.get("actor_avatar_url"),
                discord_user_id=meta_dict.get("actor_discord_id"),
                discord_avatar=meta_dict.get("actor_discord_avatar"),
            )
        return author_name, author_avatar_url

    async def get_temp_document(self, tz_info: Optional[ZoneInfo] = None) -> dict[str, Any]:
        """Đọc trang nháp `/temp` theo yêu cầu từ MongoDB (không đệm HTML trong RAM, không lộ công khai)."""
        return await self.get_doc(TEMP_DRAFT_DOC_KEY, tz_info=tz_info)

    async def save_temp_document(
        self,
        *,
        title: str,
        subtitle: str = "",
        body: str,
        actor: str,
        actor_type: str = "dashboard",
        actor_meta: Optional[dict[str, Any]] = None,
        client_changes: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Lưu nội dung Rich-Text vào trang nháp `/temp` trên MongoDB (không đổi phiên bản ToS, không purge cache công khai)."""
        clean_title = " ".join(str(title or "").split()).strip()[:160]
        clean_subtitle = " ".join(str(subtitle or "").split()).strip()[:240]
        clean_body = sanitize_legal_html(body)
        if len(clean_title) < 2:
            raise ValueError("Tiêu đề trang nháp phải có ít nhất 2 ký tự.")
        if len(clean_body) < 10:
            raise ValueError("Nội dung trang nháp sau khi lọc an toàn phải có ít nhất 10 ký tự.")

        old_doc = await self._documents.get_document(TEMP_DRAFT_DOC_KEY)
        now_iso = datetime.now(timezone.utc).isoformat()
        author_name, author_avatar_url = self._resolve_author_info(actor, actor_type, actor_meta)

        await self._documents.save_document(
            doc_key=TEMP_DRAFT_DOC_KEY,
            title=clean_title,
            subtitle=clean_subtitle,
            body_html=clean_body,
            updated_by=actor,
            updated_by_name=author_name,
            updated_by_avatar_url=author_avatar_url,
            updated_at=now_iso,
        )

        audit_changes: list[dict[str, Any]] = []
        if old_doc.get("title") != clean_title:
            audit_changes.append({
                "field": "legal.docs.temp.title",
                "before": old_doc.get("title"),
                "after": clean_title,
            })
        if old_doc.get("subtitle") != clean_subtitle:
            audit_changes.append({
                "field": "legal.docs.temp.subtitle",
                "before": old_doc.get("subtitle"),
                "after": clean_subtitle,
            })
        if old_doc.get("body") != clean_body:
            audit_changes.append({
                "field": "legal.docs.temp.body",
                "before": f"{len(str(old_doc.get('body') or ''))} ký tự HTML",
                "after": f"{len(clean_body)} ký tự HTML",
            })

        if self._audit is not None:
            await self._audit.record(
                actor=actor,
                actor_type=actor_type,
                actor_meta=actor_meta,
                action="legal_temp_save",
                section="legal",
                summary="Lưu bản nháp Rich-Text nội bộ (/temp)",
                changes=audit_changes,
                timestamp=now_iso,
                source="Dashboard Legal CMS (/temp)",
                flush=True,
            )

        return await self.get_temp_document()

    async def push_temp_to_page(
        self,
        *,
        target_doc_key: str,
        title: Optional[str] = None,
        subtitle: Optional[str] = None,
        body: Optional[str] = None,
        active_tos_version: Optional[str] = None,
        actor: str,
        actor_type: str = "dashboard",
        actor_meta: Optional[dict[str, Any]] = None,
        client_changes: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Đẩy nội dung từ `/temp` ghi đè sang 1 trong 5 trang pháp lý công khai kèm Audit Log & purge edge cache."""
        target_key = str(target_doc_key or "").strip().lower()
        if target_key not in LEGAL_DOC_KEYS:
            raise ValueError(f"Trang đích không hợp lệ: {target_doc_key}")

        if title is not None and body is not None:
            source_title = title
            source_subtitle = subtitle or ""
            source_body = body
        else:
            temp_doc = await self._documents.get_document(TEMP_DRAFT_DOC_KEY)
            source_title = title if title is not None else temp_doc.get("title", "")
            source_subtitle = subtitle if subtitle is not None else temp_doc.get("subtitle", "")
            source_body = body if body is not None else temp_doc.get("body", "")

        return await self.update_document(
            doc_key=target_key,
            title=source_title,
            subtitle=source_subtitle,
            body=source_body,
            active_tos_version=active_tos_version,
            actor=actor,
            actor_type=actor_type,
            actor_meta=actor_meta,
            client_changes=client_changes,
        )

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
        from app.features.legal.repository import validate_discord_avatar_url

        now_iso = datetime.now(timezone.utc).isoformat()
        meta_dict = actor_meta if isinstance(actor_meta, dict) else {}
        author_name = str(meta_dict.get("actor_display_name") or "").strip()
        if not author_name:
            raw_actor = str(actor or "Admin").strip()
            author_name = raw_actor.split(" (discord:")[0].strip() or raw_actor
        author_avatar_url: Optional[str] = None
        if str(actor_type or "").strip().lower() == "discord" or meta_dict.get("actor_discord_id"):
            author_avatar_url = validate_discord_avatar_url(
                meta_dict.get("actor_avatar_url"),
                discord_user_id=meta_dict.get("actor_discord_id"),
                discord_avatar=meta_dict.get("actor_discord_avatar"),
            )

        # 1) Lưu nội dung Rich-Text đã làm sạch xuống collection riêng `legal_documents` (không phình bot_config RAM)
        await self._documents.save_document(
            doc_key=key,
            title=clean_title,
            subtitle=clean_subtitle,
            body_html=clean_body,
            updated_by=actor,
            updated_by_name=author_name,
            updated_by_avatar_url=author_avatar_url,
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

        # Đồng bộ ngay ảnh chụp RAM của AtlasConfigWatcher để không ghi nhầm lượt lưu Admin thành External Sync
        if self._atlas_watcher is not None:
            self._atlas_watcher.sync_snapshot()

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
