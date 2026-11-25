"""
Điểm vào (entrypoint) CLI khởi tạo tài khoản Admin đầu tiên và Discord Admin ID trên MongoDB Atlas.
Giữ nguyên lệnh `python init_admin.py` để tương thích với tài liệu triển khai.
"""
from app.cli.init_admin import main


if __name__ == "__main__":
    main()
