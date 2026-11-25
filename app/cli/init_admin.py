"""
Triển khai CLI khởi tạo tài khoản Admin đầu tiên và Discord Admin ID trên MongoDB Atlas.

Cách dùng:
    python init_admin.py --username admin --password MatKhauManh123 --discord-id 1240503090185109536
Hoặc chạy tương tác:
    python init_admin.py
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from app.core.database import MongoDatabaseManager
from app.core.errors import AlreadyExistsError
from app.core.sync import WriteBehindSyncCoordinator
from app.features.audit.repository import AuditRepository
from app.features.audit.service import AuditService
from app.features.auth.repository import UserRepository
from app.features.auth.service import AuthService
from app.features.permissions.repository import PermissionRepository
from app.features.permissions.service import PermissionService


async def run_init_admin(args: argparse.Namespace) -> None:
    """Kết nối MongoDB Atlas và khởi tạo tài khoản Dashboard Admin / Discord Admin ID."""
    uri = os.getenv("MONGODB_URI", "").strip()
    db_name = os.getenv("MONGODB_DB", "gemini_bot").strip() or "gemini_bot"
    if not uri:
        sys.exit("❌ Thiếu biến môi trường MONGODB_URI. Hãy thiết lập trong .env hoặc shell trước khi chạy.")

    db_manager = MongoDatabaseManager(uri=uri, db_name=db_name, timeout_ms=10000)
    try:
        db = await db_manager.connect()
        ok, _, msg = await db_manager.ping()
        if not ok:
            sys.exit(f"❌ Không kết nối được MongoDB Atlas: {msg}")

        sync_coordinator = WriteBehindSyncCoordinator()
        audit_repo = AuditRepository()
        perm_repo = PermissionRepository()
        user_repo = UserRepository()

        sync_coordinator.register(perm_repo)
        sync_coordinator.register(user_repo)
        sync_coordinator.register(audit_repo)
        await sync_coordinator.attach_db(db)

        audit_service = AuditService(audit_repo, sync_coordinator)
        perm_service = PermissionService(perm_repo, audit_service, sync_coordinator)
        auth_service = AuthService(user_repo, perm_service, audit_service, sync_coordinator)

        username = (args.username or "").strip()
        password = args.password or ""
        discord_id = (args.discord_id or "").strip()

        if not username and not discord_id:
            print("=== Khởi tạo Admin cho KiwiKiwi Bot Dashboard ===")
            username = input("Tên đăng nhập Dashboard (bỏ trống để bỏ qua): ").strip()
            if username:
                password = getpass.getpass("Mật khẩu (tối thiểu 6 ký tự): ")
            discord_id = input("Discord User ID làm Admin (bỏ trống để bỏ qua): ").strip()

        if username:
            if not password:
                password = getpass.getpass(f"Mật khẩu cho '{username}': ")
            try:
                await auth_service.create_user(username, password, role="admin")
                print(f"✅ Đã tạo tài khoản Admin Dashboard: {username}")
            except AlreadyExistsError as exc:
                if args.reset_password:
                    await auth_service.change_password(username, password)
                    print(f"🔑 Đã cập nhật mật khẩu mới cho tài khoản: {username}")
                else:
                    print(f"⚠️ {exc} (dùng thêm --reset-password nếu muốn đặt lại mật khẩu)")
            except ValueError as exc:
                print(f"⚠️ {exc}")

        if discord_id:
            doc = await perm_service.upsert_permission(
                discord_id,
                label=args.label or "Admin khởi tạo",
                is_admin=True,
                can_dashboard=True,
                can_status=True,
            )
            print(f"✅ Đã cấp quyền Admin + Dashboard + /status cho Discord ID: {doc['user_id']}")

        print("🎉 Hoàn tất khởi tạo trên MongoDB Atlas!")
    finally:
        await db_manager.close()


def build_parser() -> argparse.ArgumentParser:
    """Tạo bộ phân tích tham số dòng lệnh cho công cụ khởi tạo Admin."""
    parser = argparse.ArgumentParser(description="Khởi tạo tài khoản Admin đầu tiên trên MongoDB Atlas")
    parser.add_argument("--username", help="Tên đăng nhập Dashboard (VD: admin)")
    parser.add_argument("--password", help="Mật khẩu Dashboard (tối thiểu 6 ký tự)")
    parser.add_argument("--discord-id", help="Discord User ID cấp quyền Admin + Dashboard + /status")
    parser.add_argument("--label", default="Admin khởi tạo", help="Tên gợi nhớ cho Discord User ID")
    parser.add_argument("--reset-password", action="store_true", help="Đặt lại mật khẩu nếu tài khoản đã tồn tại")
    return parser


def main() -> None:
    """Điểm vào chính của CLI khởi tạo Admin."""
    parser = build_parser()
    asyncio.run(run_init_admin(parser.parse_args()))
