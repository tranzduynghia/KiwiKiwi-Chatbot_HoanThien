"""Kho đăng ký model Gemini: tự động khám phá từ API, tính chuỗi Auto và kiểm tra kết nối."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from google.genai import errors as genai_errors

from app.core.settings import DEFAULT_FALLBACK_MODELS
from app.features.config.repository import ConfigRepository
from app.features.models.cooldown import ModelCooldownTracker
from app.features.models.metadata import (
    ModelInfo,
    NON_CHAT_KEYWORDS,
    default_badge_for,
    is_stable,
    resolve_badge,
    resolve_display_name,
    tier_of,
    version_of,
)

log = logging.getLogger("gemini-bot.models")


class ModelRegistry:
    """Quản lý danh sách model tải từ Gemini API kết hợp cấu hình động từ MongoDB."""

    def __init__(
        self,
        gemini_client: Any,
        config_repo: ConfigRepository,
        cooldown_tracker: ModelCooldownTracker,
    ):
        self._gemini = gemini_client
        self._config = config_repo
        self._cooldowns = cooldown_tracker
        self.text: list[ModelInfo] = []
        self.updated_at: float = 0.0
        self.last_error: Optional[str] = None

    async def refresh(self) -> None:
        """Tải danh sách model hỗ trợ `generateContent` trực tiếp từ Gemini API."""
        discovered: list[ModelInfo] = []
        try:
            pager = await self._gemini.aio.models.list(config={"page_size": 100})
            async for model in pager:
                if "generateContent" not in (model.supported_actions or []):
                    continue
                model_id = (model.name or "").removeprefix("models/")
                lowered = model_id.lower()
                if not model_id.startswith("gemini") or any(kw in lowered for kw in NON_CHAT_KEYWORDS):
                    continue
                discovered.append(ModelInfo(model_id, model.display_name or model_id, default_badge_for(model_id)))
            discovered.sort(key=lambda info: (-version_of(info.id), info.id))
            if discovered:
                self.text = discovered
                self.updated_at = time.time()
                self.last_error = None
                log.info("Đã tải %d model chat", len(discovered))
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("Không tải được danh sách model: %s", self.last_error)

    def badge_for(self, model_id: str) -> str:
        """Lấy nhãn badge hiệu lực của một model theo cấu hình hiện hành."""
        return resolve_badge(model_id, self._config.model_override(model_id))

    def display_name_for(self, model_id: str, api_display: str = "") -> str:
        """Lấy tên hiển thị hiệu lực của một model theo cấu hình hiện hành."""
        return resolve_display_name(model_id, self._config.model_override(model_id), api_display)

    def auto_chain(self) -> list[str]:
        """Tính thứ tự model cho chế độ tự động (Auto) từ cấu hình động MongoDB."""
        mcfg = self._config.models_cfg()
        auto_model_chain = mcfg.get("auto_model_chain") or []
        fallback_text = mcfg.get("fallback_text") or list(DEFAULT_FALLBACK_MODELS)
        auto_last = (mcfg.get("auto_last_model") or "").strip()
        default_text_model = (mcfg.get("default_text_model") or "").strip()

        if auto_model_chain:
            chain = list(auto_model_chain)
        elif not self.text:
            chain = list(fallback_text)
        else:
            ids = [model.id for model in self.text]
            chain = []
            for tier in ("lite", "flash"):
                pool = [mid for mid in ids if tier_of(mid) == tier]
                if pool:
                    chain.append(max(pool, key=lambda mid: (is_stable(mid), version_of(mid), -len(mid))))
            if auto_last and auto_last not in chain:
                chain.append(auto_last)

        if default_text_model:
            chain = [default_text_model] + [mid for mid in chain if mid != default_text_model]
        return chain or list(fallback_text)

    def default_text(self) -> str:
        """Trả về model ưu tiên đầu tiên trong chuỗi Auto."""
        return self.auto_chain()[0]

    def chain_status_text(self) -> str:
        """Định dạng chuỗi Auto kèm thời gian cooldown còn lại cho lệnh `/status`."""
        parts = []
        for model_id in self.auto_chain():
            left = self._cooldowns.cooldown_left(model_id)
            parts.append(f"`{model_id}`" + (f" ⏳{left:.0f}s" if left > 0 else ""))
        return " → ".join(parts)

    def all_known_models(self) -> list[dict[str, Any]]:
        """Tổng hợp toàn bộ model (API + extra_models + overrides) cho bảng quản trị trên Dashboard."""
        mcfg = self._config.models_cfg()
        hide_pro = bool(mcfg.get("hide_pro_models", True))
        extra_models = mcfg.get("extra_models") or []
        fallback_text = mcfg.get("fallback_text") or list(DEFAULT_FALLBACK_MODELS)
        overrides = mcfg.get("overrides") or {}
        auto_chain_list = self.auto_chain()

        base_map: dict[str, tuple[str, str]] = {}
        for model in self.text:
            base_map[model.id] = (model.display, "api")
        if not base_map:
            for mid in fallback_text:
                base_map[mid] = (mid, "fallback")
        for mid in extra_models:
            if mid not in base_map:
                base_map[mid] = (mid, "extra")
        for mid in overrides:
            if mid not in base_map:
                base_map[mid] = (mid, "override")

        output = []
        for mid, (api_disp, source) in base_map.items():
            override = self._config.model_override(mid)
            is_extra = mid in extra_models
            hidden_by_rule = self._config.is_model_hidden(mid) or (hide_pro and tier_of(mid) == "pro")
            # Model trong extra_models chỉ ẩn nếu admin đặt tường minh hidden=True trong overrides
            if is_extra and override.get("hidden") is None:
                effective_hidden = False
            else:
                effective_hidden = hidden_by_rule
            output.append({
                "id": mid,
                "api_display": api_disp,
                "display_name": self.display_name_for(mid, api_disp),
                "badge": self.badge_for(mid),
                "default_badge": default_badge_for(mid),
                "source": source,
                "is_extra": is_extra,
                "tier": tier_of(mid) or "other",
                "hidden": effective_hidden,
                "in_auto_chain": mid in auto_chain_list,
                "cooldown_left": round(self._cooldowns.cooldown_left(mid), 1),
                "override": override,
            })
        output.sort(key=lambda item: (item["hidden"], -version_of(item["id"]), item["id"]))
        return output

    def choices(self) -> list[ModelInfo]:
        """Danh sách gợi ý hiển thị trong autocomplete của option `model` trên Discord."""
        mcfg = self._config.models_cfg()
        hide_pro = bool(mcfg.get("hide_pro_models", True))
        extra_models = mcfg.get("extra_models") or []
        fallback_text = mcfg.get("fallback_text") or list(DEFAULT_FALLBACK_MODELS)

        base = self.text or [ModelInfo(mid, mid, default_badge_for(mid)) for mid in fallback_text]
        output: list[ModelInfo] = []
        for model in base:
            override = self._config.model_override(model.id)
            if override.get("hidden") is True:
                continue
            if override.get("hidden") is not False:
                if self._config.is_model_hidden(model.id) or (hide_pro and tier_of(model.id) == "pro"):
                    continue
            output.append(ModelInfo(model.id, self.display_name_for(model.id, model.display), self.badge_for(model.id)))

        existing_ids = {model.id for model in output}
        for mid in extra_models:
            if mid in existing_ids:
                continue
            override = self._config.model_override(mid)
            if override.get("hidden") is True:
                continue
            output.append(ModelInfo(mid, self.display_name_for(mid, mid), self.badge_for(mid)))
        return output

    async def build_snapshot(self) -> dict[str, Any]:
        """Tạo ảnh chụp trạng thái runtime danh sách model trả về cho Web Dashboard."""
        return {
            "all_models": self.all_known_models(),
            "auto_chain": self.auto_chain(),
            "updated_at": int(self.updated_at) if self.updated_at else None,
            "last_error": self.last_error,
        }

    async def check_health(self) -> tuple[bool, float, str]:
        """Kiểm tra trực tiếp độ trễ và tình trạng kết nối tới Gemini API."""
        model = self.default_text()
        started = time.perf_counter()
        try:
            await asyncio.wait_for(self._gemini.aio.models.get(model=model), timeout=10)
            return True, (time.perf_counter() - started) * 1000, "OK"
        except genai_errors.APIError as exc:
            return False, (time.perf_counter() - started) * 1000, f"{exc.code} {(exc.message or '')[:80]}"
        except Exception as exc:
            return False, (time.perf_counter() - started) * 1000, type(exc).__name__
