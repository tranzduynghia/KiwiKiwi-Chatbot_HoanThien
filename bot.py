"""
Điểm vào (entrypoint) chạy KiwiKiwi Gemini Discord Bot + Web Dashboard.
Giữ nguyên lệnh khởi chạy `python bot.py` để tương thích tuyệt đối với Dockerfile và cấu hình deploy.
"""
from app.main import run_bot


if __name__ == "__main__":
    run_bot()
