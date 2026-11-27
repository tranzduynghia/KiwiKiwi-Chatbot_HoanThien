"""Dịch vụ gọi Gemini API hỗ trợ ThinkingConfig, Google Search, retry 500/503 và chuyển model dự phòng."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from google.genai import errors as genai_errors
from google.genai import types

from app.core.security import redact_secrets
from app.core.warp_proxy import is_proxy_transport_error, is_transient_disconnect_error
from app.features.config.repository import ConfigRepository
from app.features.models.cooldown import ModelCooldownTracker
from app.features.models.metadata import version_of

log = logging.getLogger("gemini-bot.chat")

# Độ trễ backoff (giây) cho tối đa 2 lần thử lại khi gặp lỗi ngắt kết nối tạm thời qua WARP NAT (ServerDisconnectedError / RemoteProtocolError)
TRANSIENT_DISCONNECT_BACKOFFS: tuple[float, ...] = (0.4, 0.8)


class GeminiChatService:
    """Đóng gói toàn bộ logic gọi Gemini API, cấu hình suy nghĩ sâu và chuỗi fallback tự động."""

    def __init__(
        self,
        gemini_client: Any,
        config_repo: ConfigRepository,
        model_cooldowns: ModelCooldownTracker,
        *,
        on_proxy_error: Optional[Callable[[Exception], Awaitable[bool]]] = None,
        disconnect_backoffs: tuple[float, ...] = TRANSIENT_DISCONNECT_BACKOFFS,
    ):
        self._gemini = gemini_client
        self._config = config_repo
        self._cooldowns = model_cooldowns
        self._on_proxy_error = on_proxy_error
        self._disconnect_backoffs = tuple(disconnect_backoffs)

    def set_client(self, gemini_client: Any) -> None:
        """Cập nhật nóng đối tượng `genai.Client` khi chuyển đổi định tuyến Outbound giữa Cloudflare WARP và Direct."""
        self._gemini = gemini_client

    def build_thinking_configs(self, model_id: str, level: str) -> list[tuple[str, types.ThinkingConfig]]:
        """Tạo danh sách ứng viên `ThinkingConfig` tương thích theo từng thế hệ model (2.5 so với 3+)."""
        budgets = self._config.ask_cfg().get("thinking_budgets") or {"low": 1024, "medium": 8192, "high": 24576}
        version = version_of(model_id)
        configs: list[tuple[str, types.ThinkingConfig]] = []
        try:
            if level == "auto":
                if version >= 3:
                    configs.append(("auto", types.ThinkingConfig(include_thoughts=True)))
                elif version >= 2.5:
                    configs.append(("auto", types.ThinkingConfig(thinking_budget=-1, include_thoughts=True)))
            elif version >= 3:
                for candidate_level in {"medium": ["medium", "high"], "xhigh": ["xhigh", "high"]}.get(level, [level]):
                    try:
                        configs.append(
                            (candidate_level, types.ThinkingConfig(thinking_level=candidate_level, include_thoughts=True))
                        )
                    except Exception as exc:
                        log.warning("SDK không nhận thinking_level=%s: %s", candidate_level, exc)
            elif version >= 2.5:
                mapped_level = "high" if level == "xhigh" else level
                budget_val = int(budgets.get(mapped_level, 8192))
                configs.append(
                    (mapped_level, types.ThinkingConfig(thinking_budget=budget_val, include_thoughts=True))
                )
        except Exception as exc:
            log.warning("Không tạo được ThinkingConfig: %s", exc)
        return configs

    async def call_model(
        self,
        model: str,
        contents: list,
        *,
        system: Optional[str],
        thinking_level: Optional[str],
        web_search: bool,
        max_retries: Optional[int] = None,
        timeout_sec: Optional[float] = None,
    ) -> tuple[Any, Optional[str]]:
        """Gọi trực tiếp một model Gemini với hạ bậc thinking (400), thử lại ngắt kết nối tạm thời (ServerDisconnectedError/RemoteProtocolError), fallback Direct khi proxy lỗi và retry 500/503."""
        acfg = self._config.ask_cfg()
        retries = max_retries if max_retries is not None else int(acfg.get("max_retries", 3))
        timeout = timeout_sec if timeout_sec is not None else float(acfg.get("gemini_timeout_seconds", 170.0))

        attempts: list[tuple[types.GenerateContentConfig, Optional[str]]] = []
        base_kwargs: dict[str, Any] = {"system_instruction": system}
        if web_search:
            base_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        if thinking_level:
            for used_level, thinking_cfg in self.build_thinking_configs(model, thinking_level):
                attempts.append((types.GenerateContentConfig(**base_kwargs, thinking_config=thinking_cfg), used_level))
        attempts.append((types.GenerateContentConfig(**base_kwargs), None))

        proxy_fallback_used = False
        for idx, (cfg, thinking_used) in enumerate(attempts):
            retry = 0
            disconnect_retry = 0
            while retry < retries:
                try:
                    resp = await asyncio.wait_for(
                        self._gemini.aio.models.generate_content(model=model, contents=contents, config=cfg),
                        timeout=timeout,
                    )
                    return resp, thinking_used
                except genai_errors.APIError as exc:
                    if exc.code in (500, 503) and retry < retries - 1:
                        await asyncio.sleep(2 * (retry + 1))
                        retry += 1
                        continue
                    if exc.code == 400 and idx < len(attempts) - 1:
                        log.info("400 với thinking config, thử lại không thinking: %s", exc.message)
                        break
                    raise
                except Exception as exc:
                    if is_transient_disconnect_error(exc) and disconnect_retry < len(self._disconnect_backoffs):
                        delay = self._disconnect_backoffs[disconnect_retry]
                        disconnect_retry += 1
                        log.warning(
                            "Gemini bị ngắt kết nối tạm thời (%s) — thử lại %d/%d sau %.1fs.",
                            redact_secrets(f"{type(exc).__name__}: {exc}", max_len=140),
                            disconnect_retry,
                            len(self._disconnect_backoffs),
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    if (
                        not proxy_fallback_used
                        and self._on_proxy_error is not None
                        and is_proxy_transport_error(exc)
                    ):
                        fell_back = bool(await self._on_proxy_error(exc))
                        if fell_back:
                            proxy_fallback_used = True
                            continue
                    raise
        raise RuntimeError("Không gọi được Gemini")

    async def call_with_fallback(
        self,
        chain: list[str],
        contents: list,
        *,
        system: Optional[str],
        thinking_level: Optional[str],
        web_search: bool,
    ) -> tuple[Any, Optional[str], str, dict[str, str]]:
        """Thử tuần tự từng model sẵn sàng trong chuỗi Auto; tự động chuyển model kế tiếp khi gặp lỗi tạm thời."""
        max_retries = int(self._config.ask_cfg().get("max_retries", 3))
        errors: dict[str, str] = {}
        ready_models = [mid for mid in chain if self._cooldowns.cooldown_left(mid) <= 0]
        for mid in chain:
            if mid not in ready_models:
                errors[mid] = f"đang chờ phục hồi ~{self._cooldowns.cooldown_left(mid):.0f}s"
        if not ready_models:
            ready_models = [min(chain, key=self._cooldowns.cooldown_left)]

        last_exc: Optional[Exception] = None
        for idx, mid in enumerate(ready_models):
            is_last = idx == len(ready_models) - 1
            try:
                resp, thinking_used = await self.call_model(
                    mid,
                    contents,
                    system=system,
                    thinking_level=thinking_level,
                    web_search=web_search,
                    max_retries=max_retries if is_last else 1,
                )
                self._cooldowns.clear_model(mid)
                return resp, thinking_used, mid, errors
            except (genai_errors.APIError, asyncio.TimeoutError) as exc:
                if not self._cooldowns.is_retryable(exc):
                    raise
                self._cooldowns.mark_failure(mid, exc)
                errors[mid] = self._cooldowns.failure_reason(exc)
                last_exc = exc
                log.warning("Model %s lỗi (%s), thử model kế tiếp", mid, errors[mid])
        raise last_exc  # type: ignore[misc]
