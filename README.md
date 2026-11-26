# KiwiKiwi Gemini Discord Bot + Web Dashboard (MongoDB Atlas)

Bot Discord AI sử dụng Gemini API kết hợp **Web Dashboard quản trị thời gian thực** (FastAPI + Tailwind CDN) chạy chung tiến trình bot và đồng bộ hoàn toàn qua **MongoDB Atlas**.

## Tính năng nổi bật

- **Cấu hình động trên MongoDB Atlas (Zero-Restart)**: Toàn bộ tham số của `/ask`, phiên chat, System Instruction, OCR Instruction, Bot Presence, Cloudflare Tunnel, danh sách/tên/tag/ẩn/chuỗi Auto của model Gemini và phân quyền đều lưu trong MongoDB Atlas. Lưu trên Web Dashboard hoặc đồng bộ qua `AtlasConfigWatcher` (5 giây) là có hiệu lực ngay lập tức.
- **Xác thực kép (Dual Auth) & Thu hồi phiên tức thì**:
  1. Đăng nhập truyền thống bằng **Username & Mật khẩu** (băm bất đồng bộ bằng `scrypt` + random salt, giới hạn đồng thời tránh nghẽn event loop / tràn RAM, hỗ trợ `session_version` thu hồi cookie ngay khi đổi mật khẩu hoặc xóa tài khoản).
  2. **Login with Discord (OAuth2)**: kiểm tra CSRF `state` qua cookie đã ký HMAC-SHA256, chống Host Header Poisoning, và chỉ cho phép truy cập nếu Discord User ID nằm trong danh sách có quyền Admin hoặc Dashboard trên MongoDB (`discord_permissions`).
- **Cloudflare Tunnel (`cloudflared`) tích hợp sẵn**:
  - Hỗ trợ cả **Named Tunnel** (truyền token qua biến môi trường cô lập `TUNNEL_TOKEN`, không lộ trên CLI `argv`) và **Quick Tunnel** (`trycloudflare.com`).
  - Tùy chọn `tunnel_only_inbound`: khóa truy cập trực tiếp bằng kiểm tra biên tin cậy reverse proxy (`TRUSTED_PROXY_IPS`) kết hợp header Cloudflare (`Cf-Ray` / `CF-Connecting-IP`) và tùy chọn header bí mật nội bộ (luôn giữ mở `/health`).
- **Memory Watchdog (Container 488MB RAM)**: Duy trì vùng đệm dự phòng 20MB và tự động thực thi quy trình 4 bước *Sync-Before-Purge* khi RAM chạm ngưỡng 464MB.
- **Bảo mật & Zero-Secret / Zero-Hash Policy**:
  - Mọi API `/api/*` và `/auth/*` đều được bảo vệ bởi RBAC (`require_user` / `require_admin`), giới hạn tốc độ đăng nhập (5 lần/phút -> `429`), giới hạn kích thước payload (`256 KB`), tắt `/docs`, `/redoc`, `/openapi.json`, và gắn đầy đủ HTTP Security Headers (`CSP`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `HSTS`).
  - Không bao giờ trả về bí mật (`cf_tunnel_token`, `GEMINI_API_KEY`, `DISCORD_TOKEN`, `MONGODB_URI`), tiền tố token hay chuỗi băm (`password_hash`, `token_hash`, `salt`) qua API/HTML/Audit Log — chỉ trả cờ boolean (VD: `has_cf_tunnel_token: true`) và hiển thị giữ chỗ tĩnh `••••••••••••`.
- **Giám sát trạng thái trực tiếp & Audit Log**: Online/Offline, Ping Discord, Uptime, RAM container/RSS, CPU, số server, tình trạng Gemini API + chuỗi Auto + cooldown, MongoDB Atlas ping/storage/sessions, tra cứu phiên chat và nhật ký chỉnh sửa `audit_logs`.

## 1. Biến môi trường cần bổ sung (`.env`)

Sao chép `_.env.example` thành `.env` (hoặc cấu hình trong Environment Variables của Northflank/Render):

| Biến môi trường | Bắt buộc | Mô tả |
|---|---|---|
| `DISCORD_TOKEN` | Có | Token của Discord Bot |
| `GEMINI_API_KEY` | Có | API Key của Google Gemini |
| `MONGODB_URI` | Có | Connection string MongoDB Atlas (`mongodb+srv://...`) |
| `MONGODB_DB` | Không | Tên database (mặc định `gemini_bot`) |
| `PORT` | Không | Cổng chạy Web Dashboard & `/health` (mặc định `10000`) |
| `SESSION_SECRET` | Nên đặt | Chuỗi bí mật >= 32 ký tự để ký HMAC-SHA256 cookie phiên. Tạo bằng: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `PASSWORD_PEPPER` | Nên đặt | Khóa bí mật tiền băm mật khẩu bằng HMAC-SHA256 trước khi đưa vào `scrypt` (tự động nâng cấp mượt hash cũ khi đăng nhập, không lưu vào MongoDB). Tạo bằng: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `SESSION_MAX_AGE_SECONDS` | Không | Thời hạn cookie phiên tính bằng giây (mặc định `86400` = 24 giờ, kẹp trong `300..604800`) |
| `DASHBOARD_PUBLIC_URL` | Khi dùng OAuth2 | URL công khai của Dashboard (VD: `https://bot.northflank.app`, không có `/` ở cuối) |
| `DISCORD_CLIENT_ID` | Khi dùng OAuth2 | Client ID trong Discord Developer Portal → **OAuth2** |
| `DISCORD_CLIENT_SECRET` | Khi dùng OAuth2 | Client Secret trong Discord Developer Portal → **OAuth2** |
| `DISCORD_REDIRECT_URI` | Tuỳ chọn | Mặc định tự ghép `${DASHBOARD_PUBLIC_URL}/auth/discord/callback` |
| `CF_TUNNEL_TOKEN` / `CLOUDFLARE_TUNNEL_TOKEN` | Không | Token Cloudflare Named Tunnel nạp sẵn lúc khởi động container (khi MongoDB chưa có token đã lưu) |
| `CF_TUNNEL_MODE` | Không | Chế độ Tunnel mặc định: `named` hoặc `quick` (mặc định `named`) |
| `CF_TUNNEL_ONLY` | Không | `true` = bật sẵn khóa `tunnel_only_inbound` (chỉ nhận truy cập qua Cloudflare Tunnel, giữ mở `/health`) |
| `TRUST_PROXY_HEADERS` | Không | `true` = bật sẵn đọc `X-Forwarded-Proto` / `X-Forwarded-For` / `CF-Connecting-IP` từ proxy tin cậy |
| `TRUSTED_PROXY_IPS` | Không | Danh sách IP/CIDR reverse proxy tin cậy cách nhau dấu phẩy (mặc định chỉ loopback `127.0.0.1/8`, `::1/128` cho `cloudflared` nội bộ) |
| `CLOUDFLARE_TUNNEL_SECRET_HEADER` / `CLOUDFLARE_TUNNEL_SECRET_VALUE` | Không | Cặp tên/giá trị header bí mật bổ sung do Cloudflare Transform Rule chèn khi bật `tunnel_only_inbound` |
| `OUTBOUND_PROXY_URL` | Không | Ghi đè URL proxy outbound cho Discord & Gemini SDK (mặc định tự phát hiện WARP tại `http://127.0.0.1:40000`) |
| `WARP_ENABLED` / `WARP_OUTBOUND_ENABLED` | Không | Bật/tắt Cloudflare WARP User-Space Proxy (`127.0.0.1:40000`) khi khởi động container và mặc định trên Dashboard (mặc định `true`, tự động fallback Direct nếu không kết nối được; MongoDB Atlas luôn giữ TLS trực tiếp) |
| `WARP_PROXY_PORT` | Không | Cổng cục bộ của WARP user-space proxy (mặc định `40000`) |
| `WARP_LICENSE_KEY` / `WARP_CONNECTOR_TOKEN` | Tuỳ chọn | License key WARP+ hoặc Zero Trust WARP Connector token (chỉ đọc lúc container khởi động, không bao giờ lưu DB hay lộ qua API) |
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
- `bot_config`: cấu hình động toàn cục (`_id: "global"`) cho `/ask`, phiên chat, System Instruction, Presence, Cloudflare Tunnel và Model Gemini.
- `dashboard_users`: tài khoản đăng nhập truyền thống (mật khẩu mã hóa `scrypt` + `session_version`).
- `discord_permissions`: phân quyền theo từng Discord User ID (`is_admin`, `can_dashboard`, `can_status`).
- `audit_logs`: nhật ký kiểm toán thay đổi cấu hình, phân quyền và sự kiện hệ thống (tự che tuyệt đối mọi trường bí mật).
- `bot_metrics`: ảnh chụp thống kê sử dụng và nhận diện bot (`runtime_snapshot`).
- `sessions` & `archives`: giữ nguyên cấu trúc phiên chat và lưu trữ vĩnh viễn.

## 5. Chạy Local, Kiểm thử & Deploy

```bash
pip install -r requirements.txt
python bot.py
```
Truy cập Web Dashboard tại `http://localhost:10000` (endpoint `/health` vẫn giữ nguyên cho Northflank/Render/UptimeRobot).

Chạy bộ kiểm thử tự động (bao gồm kiểm thử bảo mật):
```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -v
```
