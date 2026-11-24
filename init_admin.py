"""
Script CLI khởi tạo tài khoản Admin đầu tiên và Discord Admin ID trên MongoDB Atlas.

Cách dùng:
    python init_admin.py --username admin --password MatKhauManh123 --discord-id 1240503090185109536
Hoặc chạy tương tác:
    python init_admin.py
"""
import argparse
import asyncio
import getpass
import os
import sys

import certifi
from pymongo import AsyncMongoClient

from config_store import ConfigStore


async def run(args):
    uri = os.getenv("MONGODB_URI", "").strip()
    db_name = os.getenv("MONGODB_DB", "gemini_bot").strip()
    if not uri:
        sys.exit("❌ Thiếu biến môi trường MONGODB_URI. Hãy thiết lập trong .env hoặc shell trước khi chạy.")

    kwargs = {"serverSelectionTimeoutMS": 10000, "tz_aware": True}
    if uri.startswith("mongodb+srv://"):
        kwargs["tlsCAFile"] = certifi.where()

    client = AsyncMongoClient(uri, **kwargs)
    try:
        await client.admin.command("ping")
        store = ConfigStore()
        await store.attach_db(client[db_name])

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
                await store.create_dashboard_user(username, password, role="admin")
                print(f"✅ Đã tạo tài khoản Admin Dashboard: {username}")
            except ValueError as e:
                if "đã tồn tại" in str(e) and args.reset_password:
                    await store.update_dashboard_user_password(username, password)
                    print(f"🔑 Đã cập nhật mật khẩu mới cho tài khoản: {username}")
                else:
                    print(f"⚠️ {e} (dùng thêm --reset-password nếu muốn đặt lại mật khẩu)")

        if discord_id:
            doc = await store.upsert_discord_permission(
                discord_id,
                label=args.label or "Admin khởi tạo",
                is_admin=True,
                can_dashboard=True,
                can_status=True,
            )
            print(f"✅ Đã cấp quyền Admin + Dashboard + /status cho Discord ID: {doc['user_id']}")

        print("🎉 Hoàn tất khởi tạo trên MongoDB Atlas!")
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(description="Khởi tạo tài khoản Admin đầu tiên trên MongoDB Atlas")
    parser.add_argument("--username", help="Tên đăng nhập Dashboard (VD: admin)")
    parser.add_argument("--password", help="Mật khẩu Dashboard (tối thiểu 6 ký tự)")
    parser.add_argument("--discord-id", help="Discord User ID cấp quyền Admin + Dashboard + /status")
    parser.add_argument("--label", default="Admin khởi tạo", help="Tên gợi nhớ cho Discord User ID")
    parser.add_argument("--reset-password", action="store_true", help="Đặt lại mật khẩu nếu tài khoản đã tồn tại")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
