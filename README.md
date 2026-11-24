# KiwiKiwi Gemini Discord Bot + Web Dashboard (MongoDB Atlas)

Bot Discord AI sử dụng Gemini API kết hợp **Web Dashboard quản trị thời gian thực** (FastAPI + Tailwind CDN) chạy chung tiến trình bot và đồng bộ hoàn toàn qua **MongoDB Atlas**.

## Tính năng nổi bật

- **Cấu hình động trên MongoDB Atlas (Zero-Restart)**: Toàn bộ tham số của `/ask`, phiên chat, System Instruction, OCR Instruction, danh sách/tên/tag/ẩn/chuỗi Auto của model Gemini và phân quyền đều lưu trong MongoDB Atlas. Lưu trên Web Dashboard là có hiệu lực ngay lập tức.
- **Xác thực kép (Dual Auth)**:
  1. Đăng nhập truyền thống bằng **Username & Mật khẩu** (băm an toàn bằng `scrypt` + random salt lưu trong `dashboard_users`).
  2. **Login with Discord (OAuth2)**: chỉ cho phép truy cập nếu Discord User ID nằm trong danh sách có quyền Admin hoặc Dashboard trên MongoDB (`discord_permissions`).
- **Phân quyền chi tiết trên Dashboard**:
  - Quy định từng Discord User ID có quyền **Admin toàn quyền**, **Vào Dashboard**, và **Dùng `/status` trên Discord**.
  - Lệnh `/status` kiểm tra quyền trực tiếp từ MongoDB trước khi thực thi.
- **Giám sát trạng thái trực tiếp**: Online/Offline, Ping Discord, Uptime, RAM container/RSS, CPU, số server, tình trạng Gemini API + chuỗi Auto + cooldown, MongoDB Atlas ping/storage/sessions và thống kê request.

## 1. Biến môi trường cần bổ sung (`.env`)

Sao chép `.env.example` thành `.env` (hoặc cấu hình trong Environment Variables của Northflank/Render):

| Biến môi trường | Bắt buộc | Mô tả |
|---|---|---|
| `DISCORD_TOKEN` | Có | Token của Discord Bot |
| `GEMINI_API_KEY` | Có | API Key của Google Gemini |
| `MONGODB_URI` | Có | Connection string MongoDB Atlas (`mongodb+srv://...`) |
| `MONGODB_DB` | Không | Tên database (mặc định `gemini_bot`) |
| `PORT` | Không | Cổng chạy Web Dashboard & `/health` (mặc định `10000`) |
| `SESSION_SECRET` | Nên đặt | Chuỗi bí mật ký cookie phiên đăng nhập. Tạo bằng: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DASHBOARD_PUBLIC_URL` | Khi dùng OAuth2 | URL công khai của Dashboard (VD: `https://bot.northflank.app`, không có `/` ở cuối) |
| `DISCORD_CLIENT_ID` | Khi dùng OAuth2 | Client ID trong Discord Developer Portal → **OAuth2** |
| `DISCORD_CLIENT_SECRET` | Khi dùng OAuth2 | Client Secret trong Discord Developer Portal → **OAuth2** |
| `DISCORD_REDIRECT_URI` | Tuỳ chọn | Mặc định tự ghép `${DASHBOARD_PUBLIC_URL}/auth/discord/callback` |
| `INITIAL_ADMIN_USERNAME` | Khởi tạo lần đầu | Tên đăng nhập Admin tự động tạo nếu `dashboard_users` đang trống |
| `INITIAL_ADMIN_PASSWORD` | Khởi tạo lần đầu | Mật khẩu Admin tự động tạo (tối thiểu 6 ký tự) |
| `INITIAL_ADMIN_DISCORD_IDS` | Khởi tạo lần đầu | Các Discord User ID làm Admin ban đầu (cách nhau bằng dấu phẩy) |

## 2. Cấu hình Login with Discord (OAuth2)

1. Mở [Discord Developer Portal](https://discord.com/developers/applications) → chọn Application của bot → tab **OAuth2**.
2. Copy **Client ID** và **Client Secret** vào `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`.
3. Ở mục **Redirects**, thêm đúng URL callback:
   - Chạy local: `http://localhost:10000/auth/discord/callback`
   - Deploy Northflank/Render: `https://<domain-cua-ban>/auth/discord/callback`
4. Lưu thay đổi.

## 3. Khởi tạo tài khoản Admin đầu tiên

Bạn có **2 cách** (chọn 1 trong 2):

### Cách 1: Tự động qua biến môi trường `.env` (khuyên dùng khi deploy Northflank/Render)
Đặt 3 biến sau trong `.env` / Secret Group rồi khởi động bot:
```env
INITIAL_ADMIN_USERNAME=admin
INITIAL_ADMIN_PASSWORD=MatKhauManhCuaBan123!
INITIAL_ADMIN_DISCORD_IDS=1240503090185109536
```
Khi bot kết nối MongoDB Atlas lần đầu và thấy chưa có tài khoản nào, bot sẽ tự tạo tài khoản `admin` và cấp quyền Admin + Dashboard + `/status` cho Discord ID trên. Sau khi đăng nhập được, bạn có thể xóa 2 biến `INITIAL_ADMIN_USERNAME/PASSWORD` khỏi `.env` hoặc đổi mật khẩu ngay trên Dashboard.

### Cách 2: Dùng script CLI `init_admin.py` (khi chạy local hoặc muốn reset mật khẩu)
```bash
pip install -r requirements.txt
export MONGODB_URI="mongodb+srv://..."
python init_admin.py --username admin --password "MatKhauManh123!" --discord-id 1240503090185109536
```
Nếu tài khoản đã tồn tại và bạn quên mật khẩu, thêm cờ `--reset-password`:
```bash
python init_admin.py --username admin --password "MatKhauMoi123!" --reset-password
```

## 4. Cấu trúc Collections trên MongoDB Atlas

Bot tự tạo và quản lý các collection sau trong database `MONGODB_DB`:
- `bot_config`: cấu hình động toàn cục (`_id: "global"`) cho `/ask`, phiên chat, System Instruction và Model Gemini.
- `dashboard_users`: tài khoản đăng nhập truyền thống (mật khẩu mã hóa `scrypt`).
- `discord_permissions`: phân quyền theo từng Discord User ID (`is_admin`, `can_dashboard`, `can_status`).
- `sessions` & `archives`: giữ nguyên hoàn toàn cấu trúc phiên chat và lưu trữ vĩnh viễn của bot cũ.

## 5. Chạy Local & Deploy

```bash
pip install -r requirements.txt
python bot.py
```
Truy cập Web Dashboard tại `http://localhost:10000` (endpoint `/health` vẫn giữ nguyên cho Northflank/Render/UptimeRobot).
