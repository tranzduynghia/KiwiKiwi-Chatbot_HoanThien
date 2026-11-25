"""
Composition Root của ứng dụng: khởi tạo toàn bộ kho dữ liệu, dịch vụ tính năng,
kết nối phụ thuộc theo hướng tiêm phụ thuộc (Dependency Injection) và chạy bot.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

import psutil
from google import genai
from google.genai import types

from app.bot.client import AskBot
from app.core.concurrency import DynamicSemaphore, UserLockManager
from app.core.database import MongoDatabaseManager
from app.core.logging import configure_logging
from app.core.security import SessionTokenSigner
from app.core.settings import AppSettings
from app.core.sync import WriteBehindSyncCoordinator
from app.features.audit.repository import AuditRepository
from app.features.audit.service import AuditService
from app.features.auth.dependencies import AuthDependencies
from app.features.auth.oauth import DiscordOAuthService
from app.features.auth.repository import UserRepository
from app.features.auth.service import AuthService
from app.features.chat.cooldown import UserCooldownTracker
from app.features.chat.gemini_service import GeminiChatService
from app.features.config.repository import ConfigRepository
from app.features.config.service import ConfigService
from app.features.models.cooldown import ModelCooldownTracker
from app.features.models.registry import ModelRegistry
from app.features.monitoring.service import MonitoringService
from app.features.monitoring.stats import UsageStats
from app.features.permissions.repository import PermissionRepository
from app.features.permissions.service import PermissionService
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
    permission_repo: PermissionRepository
    permission_service: PermissionService
    user_repo: UserRepository
    auth_service: AuthService
    oauth_service: DiscordOAuthService
    auth_deps: AuthDependencies
    model_cooldowns: ModelCooldownTracker
    model_registry: ModelRegistry
    session_repo: SessionRepository
    user_cooldowns: UserCooldownTracker
    gemini_service: GeminiChatService
    usage_stats: UsageStats
    monitoring_service: MonitoringService


def build_container(
    settings: AppSettings,
    *,
    gemini_client: Any = None,
    start_time: Optional[float] = None,
) -> AppContainer:
    """Khởi tạo và lắp ghép toàn bộ thành phần ứng dụng mà không dùng biến toàn cục cấp module."""
    if gemini_client is None:
        http_options = (
            types.HttpOptions(api_version=settings.gemini_api_version)
            if settings.gemini_api_version
            else None
        )
        gemini_client = genai.Client(api_key=settings.gemini_api_key, http_options=http_options)

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
    auth_deps = AuthDependencies(signer=token_signer, permission_service=permission_service)

    # Điều phối concurrency động theo cấu hình `ask.max_concurrent`
    gemini_semaphore = DynamicSemaphore(lambda: config_repo.ask_cfg().get("max_concurrent", 2))
    config_service.on_update(lambda _cfg: gemini_semaphore.notify_limit_changed())

    # Model Gemini, phiên chat, dịch vụ hỏi đáp và giám sát
    model_cooldowns = ModelCooldownTracker()
    model_registry = ModelRegistry(gemini_client, config_repo, model_cooldowns)
    session_repo = SessionRepository(db_manager, config_repo, user_locks)
    user_cooldowns = UserCooldownTracker()
    gemini_service = GeminiChatService(gemini_client, config_repo, model_cooldowns)
    usage_stats = UsageStats()
    monitoring_service = MonitoringService(
        config_repo=config_repo,
        model_registry=model_registry,
        model_cooldowns=model_cooldowns,
        session_repo=session_repo,
        db_manager=db_manager,
        sync_coordinator=sync_coordinator,
        usage_stats=usage_stats,
        start_time=start_time if start_time is not None else time.time(),
    )

    return AppContainer(
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
        permission_repo=permission_repo,
        permission_service=permission_service,
        user_repo=user_repo,
        auth_service=auth_service,
        oauth_service=oauth_service,
        auth_deps=auth_deps,
        model_cooldowns=model_cooldowns,
        model_registry=model_registry,
        session_repo=session_repo,
        user_cooldowns=user_cooldowns,
        gemini_service=gemini_service,
        usage_stats=usage_stats,
        monitoring_service=monitoring_service,
    )


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
