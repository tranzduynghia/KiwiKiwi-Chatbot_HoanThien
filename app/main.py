"""
Composition Root của ứng dụng: khởi tạo toàn bộ kho dữ liệu, dịch vụ tính năng,
kết nối phụ thuộc theo hướng tiêm phụ thuộc (Dependency Injection) và chạy bot.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import psutil
from google import genai
from google.genai import types

from app.bot.client import AskBot
from app.core.cloudflare_tunnel import CloudflareTunnelService
from app.core.concurrency import DynamicSemaphore, UserLockManager
from app.core.database import MongoDatabaseManager
from app.core.logging import configure_logging
from app.core.memory_watchdog import DEFAULT_RESERVE_BYTES, MemoryWatchdog
from app.core.security import SessionTokenSigner
from app.core.settings import AppSettings
from app.core.sync import WriteBehindSyncCoordinator
from app.core.warp_proxy import WarpProxyService
from app.features.audit.repository import AuditRepository
from app.features.audit.service import AuditService
from app.features.auth.dependencies import AuthDependencies
from app.features.auth.oauth import DiscordOAuthService
from app.features.auth.repository import UserRepository
from app.features.auth.service import AuthService
from app.features.chat.cooldown import UserCooldownTracker
from app.features.chat.gemini_service import GeminiChatService
from app.features.config.atlas_watcher import AtlasConfigWatcher
from app.features.config.repository import ConfigRepository
from app.features.config.service import ConfigService
from app.features.models.cooldown import ModelCooldownTracker
from app.features.models.registry import ModelRegistry
from app.features.monitoring.service import MonitoringService
from app.features.monitoring.stats import UsageStats
from app.features.permissions.repository import PermissionRepository
from app.features.permissions.service import PermissionService
from app.features.sessions.discord_lookup import DiscordUserLookupService
from app.features.sessions.repository import SessionRepository


@dataclass
class AppContainer:
    """Tập hợp toàn bộ đối tượng runtime được khởi tạo và chia sẻ giữa Discord Bot và Web Dashboard."""

    settings: AppSettings
    db_manager: MongoDatabaseManager
    sync_coordinator: WriteBehindSyncCoordinator
    user_locks: UserLockManager
    gemini_semaphore: DynamicSemaphore
    gemini_client: Any

    audit_repo: AuditRepository
    audit_service: AuditService
    config_repo: ConfigRepository
    config_service: ConfigService
    atlas_config_watcher: AtlasConfigWatcher
    permission_repo: PermissionRepository
    permission_service: PermissionService
    user_repo: UserRepository
    auth_service: AuthService
    oauth_service: DiscordOAuthService
    auth_deps: AuthDependencies
    model_cooldowns: ModelCooldownTracker
    model_registry: ModelRegistry
    session_repo: SessionRepository
    user_lookup: DiscordUserLookupService
    user_cooldowns: UserCooldownTracker
    gemini_service: GeminiChatService
    usage_stats: UsageStats
    memory_watchdog: MemoryWatchdog
    cloudflare_tunnel: CloudflareTunnelService
    warp_proxy: WarpProxyService
    monitoring_service: MonitoringService


def build_gemini_http_options(
    settings: AppSettings,
    *,
    proxy_url: Optional[str] = None,
) -> Optional[types.HttpOptions]:
    """Tạo `types.HttpOptions` cho Google GenAI SDK hỗ trợ `GEMINI_API_VERSION` và proxy WARP (tắt keep-alive cũ, giữ phân giải DNS phía WARP)."""
    kwargs: dict[str, Any] = {}
    if settings.gemini_api_version:
        kwargs["api_version"] = settings.gemini_api_version
    effective_proxy = (
        settings.outbound_proxy_url
        if proxy_url is None
        else (proxy_url or "").strip()
    )
    if effective_proxy:
        # Tắt keep-alive pooling để không tái sử dụng socket hết hạn qua WARP NAT;
        # truyền `transport` buộc google-genai dùng httpx AsyncClient (hỗ trợ socks5h:// + remote DNS) thay vì aiohttp mặc định.
        no_keepalive_limits = httpx.Limits(max_keepalive_connections=0, keepalive_expiry=0.0)
        async_transport = httpx.AsyncHTTPTransport(proxy=effective_proxy, limits=no_keepalive_limits)
        kwargs["client_args"] = {
            "proxy": effective_proxy,
            "limits": no_keepalive_limits,
        }
        kwargs["async_client_args"] = {
            "proxy": effective_proxy,
            "limits": no_keepalive_limits,
            "transport": async_transport,
        }
    return types.HttpOptions(**kwargs) if kwargs else None


def _create_gemini_client(settings: AppSettings, proxy_url: str = "") -> Any:
    """Khởi tạo `genai.Client` với proxy hiệu lực (WARP hoặc Direct) mà không ảnh hưởng MongoDB Atlas."""
    http_options = build_gemini_http_options(settings, proxy_url=proxy_url)
    return genai.Client(api_key=settings.gemini_api_key, http_options=http_options)


def build_container(
    settings: AppSettings,
    *,
    gemini_client: Any = None,
    gemini_client_factory: Optional[Any] = None,
    start_time: Optional[float] = None,
    reserve_buffer_bytes: int = DEFAULT_RESERVE_BYTES,
    cloudflare_tunnel: Optional[CloudflareTunnelService] = None,
    warp_proxy: Optional[WarpProxyService] = None,
) -> AppContainer:
    """Khởi tạo và lắp ghép toàn bộ thành phần ứng dụng mà không dùng biến toàn cục cấp module."""
    explicit_proxy = bool(settings.outbound_proxy_url)
    warp_service = warp_proxy or WarpProxyService(
        proxy_url=settings.outbound_proxy_url,
        warp_port=settings.warp_proxy_port,
        enabled=settings.warp_outbound_enabled,
        explicit_proxy_url=explicit_proxy,
    )
    if warp_proxy is None:
        warp_service.probe_initial_sync()

    initial_proxy_url = warp_service.effective_proxy_url()
    managed_gemini_factory = gemini_client_factory
    if gemini_client is None and managed_gemini_factory is None:
        managed_gemini_factory = lambda proxy: _create_gemini_client(settings, proxy_url=proxy or "")

    if gemini_client is None:
        gemini_client = managed_gemini_factory(initial_proxy_url or None)

    db_manager = MongoDatabaseManager(uri=settings.mongodb_uri, db_name=settings.mongodb_db)
    sync_coordinator = WriteBehindSyncCoordinator()
    user_locks = UserLockManager()

    # Khởi tạo các kho dữ liệu có cơ chế Write-Behind Sync Buffer
    config_repo = ConfigRepository(cache_ttl=settings.cache_ttl_seconds)
    permission_repo = PermissionRepository(cache_ttl=settings.cache_ttl_seconds)
    audit_repo = AuditRepository()
    audit_service = AuditService(audit_repo, sync_coordinator)

    async def _audit_initial_admin(username: str) -> None:
        await audit_service.record(
            actor="system (.env)",
            actor_type="system",
            action="user_create",
            section="dashboard_users",
            summary=f"Tự động khởi tạo tài khoản admin đầu tiên '{username}'",
            changes=[
                {"field": "username", "before": None, "after": username},
                {"field": "role", "before": None, "after": "admin"},
            ],
            flush=False,
        )

    user_repo = UserRepository(on_initial_admin_created=_audit_initial_admin)

    # Thứ tự xả đồng bộ: bot_config -> discord_permissions -> dashboard_users -> audit_logs
    sync_coordinator.register(config_repo)
    sync_coordinator.register(permission_repo)
    sync_coordinator.register(user_repo)
    sync_coordinator.register(audit_repo)

    # Khởi tạo các dịch vụ nghiệp vụ cho từng tính năng
    config_service = ConfigService(config_repo, audit_service, sync_coordinator)
    permission_service = PermissionService(permission_repo, audit_service, sync_coordinator)
    auth_service = AuthService(user_repo, permission_service, audit_service, sync_coordinator)
    oauth_service = DiscordOAuthService()
    token_signer = SessionTokenSigner(secret_key=settings.session_secret)
    auth_deps = AuthDependencies(signer=token_signer, permission_service=permission_service, user_repo=user_repo)

    # Điều phối concurrency động theo cấu hình `ask.max_concurrent`
    gemini_semaphore = DynamicSemaphore(lambda: config_repo.ask_cfg().get("max_concurrent", 2))
    config_service.on_update(lambda _cfg: gemini_semaphore.notify_limit_changed())

    # Model Gemini, phiên chat, dịch vụ hỏi đáp và giám sát
    model_cooldowns = ModelCooldownTracker()
    model_registry = ModelRegistry(gemini_client, config_repo, model_cooldowns)
    session_repo = SessionRepository(db_manager, config_repo, user_locks)
    user_lookup = DiscordUserLookupService()
    user_cooldowns = UserCooldownTracker()

    async def _on_gemini_proxy_error(exc: Exception) -> bool:
        return await warp_service.fallback_to_direct(f"{type(exc).__name__}: {exc}")

    gemini_service = GeminiChatService(
        gemini_client,
        config_repo,
        model_cooldowns,
        on_proxy_error=_on_gemini_proxy_error,
    )
    usage_stats = UsageStats()

    async def _sync_before_memory_purge() -> dict[str, int]:
        """Bước 2 Watchdog: đẩy lượt chat chờ (`bulk_write`), bộ đếm metrics và hàng đợi Write-Behind lên Atlas."""
        counts: dict[str, int] = {}
        synced_sessions = await session_repo.flush_pending_bulk()
        if synced_sessions:
            counts["sessions"] = synced_sessions
        synced_metrics = await usage_stats.flush_pending()
        if synced_metrics:
            counts["metrics"] = synced_metrics
        sync_counts = await sync_coordinator.flush_pending_with_counts()
        for key, val in sync_counts.items():
            counts[key] = counts.get(key, 0) + val
        return counts

    def _purge_non_essential_caches() -> dict[str, int]:
        """Bước 3 Watchdog: xóa các cache không thiết yếu trong RAM, giữ nguyên cấu hình bot & phân quyền."""
        return {
            "session_cache": session_repo.purge_cache(),
            "discord_user_cache": user_lookup.clear_cache(),
            "user_cooldowns": user_cooldowns.purge_stale(),
            "model_cooldowns": model_cooldowns.purge_expired(),
        }

    async def _record_memory_watchdog_audit(event: dict[str, Any]) -> None:
        """Bước 4 Watchdog: ghi Audit Log xác nhận số bản ghi đã đồng bộ an toàn trước khi dọn RAM."""
        synced_total = int(event.get("synced_total", 0))
        purged_total = int(event.get("purged_total", 0))
        timed_out = bool(event.get("sync_timed_out", False))
        is_simulated = bool(event.get("simulated", False))
        triggered_by = event.get("triggered_by") if isinstance(event.get("triggered_by"), dict) else {}

        prefix = "[Mô phỏng Test Watchdog] " if is_simulated else ""
        summary = (
            f"{prefix}Giải cứu bộ nhớ khẩn cấp: đồng bộ an toàn {synced_total} bản ghi lên MongoDB Atlas"
            + (" (quá hạn 5s)" if timed_out else "")
            + f", dọn {purged_total} mục cache và cấp phát lại vùng đệm dự phòng"
        )
        changes = [
            {
                "field": "memory.simulated",
                "before": False,
                "after": is_simulated,
            },
            {
                "field": "memory.rss_mb",
                "before": f"{event.get('peak_rss_mb')} MB",
                "after": f"{event.get('recovered_rss_mb')} MB",
            },
            {
                "field": "memory.synced_records",
                "before": "Đang chờ trong RAM",
                "after": f"{synced_total} bản ghi ({event.get('synced_details') or {}})",
            },
            {
                "field": "memory.purged_cache_entries",
                "before": f"{purged_total} mục cache tạm",
                "after": f"Đã dọn sạch (GC: {event.get('gc_collected', 0)} đối tượng)",
            },
            {
                "field": "memory.reserve_buffer",
                "before": "Đã giải phóng khẩn cấp",
                "after": "Đã cấp phát lại sẵn sàng (< 400 MB)",
            },
        ]
        if timed_out and event.get("sync_error"):
            changes.append({
                "field": "memory.sync_timeout",
                "before": "5.0s",
                "after": str(event.get("sync_error")),
            })
        await audit_service.record(
            actor=str(triggered_by.get("actor") or "system (memory-watchdog)"),
            actor_type=str(triggered_by.get("actor_type") or "system"),
            actor_meta=triggered_by.get("actor_meta"),
            action="memory_watchdog_simulation" if is_simulated else "memory_emergency_cleanup",
            section="system",
            summary=summary,
            changes=changes,
            flush=True,
        )

    memory_watchdog = MemoryWatchdog(
        reserve_bytes=reserve_buffer_bytes,
        sync_before_purge=_sync_before_memory_purge,
        purge_caches=_purge_non_essential_caches,
        record_audit_event=_record_memory_watchdog_audit,
    )

    tunnel_service = cloudflare_tunnel or CloudflareTunnelService(
        local_port=settings.port,
        outbound_proxy_url=settings.outbound_proxy_url,
        warp_proxy=warp_service,
    )
    if cloudflare_tunnel is not None:
        tunnel_service.attach_warp_proxy(warp_service)

    atlas_config_watcher = AtlasConfigWatcher(
        config_repo=config_repo,
        config_service=config_service,
        audit_service=audit_service,
        cloudflare_tunnel=tunnel_service,
        warp_proxy=warp_service,
    )
    config_service.set_external_sync_hook(atlas_config_watcher.poll_once)

    monitoring_service = MonitoringService(
        config_repo=config_repo,
        model_registry=model_registry,
        model_cooldowns=model_cooldowns,
        session_repo=session_repo,
        db_manager=db_manager,
        sync_coordinator=sync_coordinator,
        usage_stats=usage_stats,
        memory_watchdog=memory_watchdog,
        cloudflare_tunnel=tunnel_service,
        warp_proxy=warp_service,
        start_time=start_time if start_time is not None else time.time(),
    )

    container = AppContainer(
        settings=settings,
        db_manager=db_manager,
        sync_coordinator=sync_coordinator,
        user_locks=user_locks,
        gemini_semaphore=gemini_semaphore,
        gemini_client=gemini_client,
        audit_repo=audit_repo,
        audit_service=audit_service,
        config_repo=config_repo,
        config_service=config_service,
        atlas_config_watcher=atlas_config_watcher,
        permission_repo=permission_repo,
        permission_service=permission_service,
        user_repo=user_repo,
        auth_service=auth_service,
        oauth_service=oauth_service,
        auth_deps=auth_deps,
        model_cooldowns=model_cooldowns,
        model_registry=model_registry,
        session_repo=session_repo,
        user_lookup=user_lookup,
        user_cooldowns=user_cooldowns,
        gemini_service=gemini_service,
        usage_stats=usage_stats,
        memory_watchdog=memory_watchdog,
        cloudflare_tunnel=tunnel_service,
        warp_proxy=warp_service,
        monitoring_service=monitoring_service,
    )

    if managed_gemini_factory is not None:
        async def _swap_gemini_client_route(new_proxy: Optional[str]) -> None:
            old_client = container.gemini_client
            new_client = managed_gemini_factory(new_proxy)
            container.gemini_client = new_client
            gemini_service.set_client(new_client)
            model_registry.set_client(new_client)
            # Đóng nhẹ client cũ để tránh rò rỉ socket trên container 488MB
            try:
                aio_obj = getattr(old_client, "aio", None)
                aclose = getattr(aio_obj, "aclose", None)
                if callable(aclose):
                    await aclose()
            except Exception:
                pass
            try:
                close_fn = getattr(old_client, "close", None)
                if callable(close_fn):
                    close_fn()
            except Exception:
                pass

        warp_service.register_route_hook(_swap_gemini_client_route)

    return container


def run_bot() -> None:
    """Đọc biến môi trường, khởi tạo container và chạy tiến trình bot Discord + Web Dashboard."""
    configure_logging()
    settings = AppSettings.from_env()
    if not settings.discord_token or not settings.gemini_api_key:
        sys.exit("Thiếu biến môi trường DISCORD_TOKEN hoặc GEMINI_API_KEY")

    # Làm nóng bộ đo CPU của psutil để lần đọc đầu tiên chính xác hơn
    psutil.Process().cpu_percent(None)

    container = build_container(settings)
    bot = AskBot(container)
    bot.run(settings.discord_token, log_handler=None)
