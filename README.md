# Gemini Discord Bot (Northflank)

Slash command: `/ask` (hỏi đáp, OCR, suy nghĩ sâu, tra web) và `/status`.

## 1. Chuẩn bị token

**Discord**
1. https://discord.com/developers/applications → *New Application* → tab **Bot** → *Reset Token* → copy (`DISCORD_TOKEN`).
2. Tab **OAuth2 → URL Generator**: scope `bot` + `applications.commands`; quyền *Send Messages*, *Attach Files*, *Embed Links*. Mở URL tạo ra để mời bot vào server.
3. Không cần bật Privileged Intents (bot chỉ dùng slash command).

**Gemini**: https://aistudio.google.com/apikey → *Create API key* (`GEMINI_API_KEY`).

## 2. Đưa code lên GitHub

Tạo repo **private**, đẩy: `bot.py`, `requirements.txt`, `Dockerfile`, `.dockerignore`, `.gitignore`, `.env.example`, `README.md`. **Không** commit file `.env`.

## 3. Deploy lên Northflank

1. Đăng ký https://northflank.com → tạo **Project** (gói *Developer Sandbox* miễn phí), chọn region.
2. Liên kết GitHub: *Account settings → Git* → cấp quyền cho repo vừa tạo.
3. Trong project: **Create new → Service → Combined service** (build + deploy từ repo):
   - Repository và branch: repo của bạn, `main`
   - Build type: **Dockerfile** (đường dẫn `/Dockerfile`, context `/`)
4. **Runtime variables** (đánh dấu là secret): thêm `DISCORD_TOKEN` và `GEMINI_API_KEY`.
   Nên tạo một *Secret group* chứa 2 biến này rồi gắn vào service.
5. **Networking → Ports**: thêm cổng `10000`, giao thức `HTTP`. Có thể để không public, chỉ cần để bot có `/health`.
6. **Resources**: chọn gói compute nhỏ nhất (free), 1 instance.
7. Bấm tạo service. Khi tab **Logs** hiện `Đã đồng bộ slash commands` là bot đã chạy.

> Khi test, thêm `GUILD_ID` (ID server của bạn) để lệnh cập nhật tức thì.

## 4. Không cần UptimeRobot

Gói Developer Sandbox của Northflank chạy liên tục, không ngủ như Render Free. Bot vẫn có `/health` nếu bạn muốn gắn monitor để nhận cảnh báo khi bot sập (chỉ cần bật public cho cổng 10000).

## 5. Cách dùng `/ask`

| Option | Tác dụng |
|---|---|
| `prompt` | Câu hỏi (bắt buộc) |
| `file` | Đính kèm ảnh/PDF để hỏi về nội dung hoặc OCR |
| `ocr` | Chỉ trích xuất chữ từ `file` |
| `think` | Suy nghĩ sâu + hiện tóm tắt suy nghĩ |
| `web` | Tra cứu web (Google Search grounding), kèm nguồn |
| `model` | Chọn model; danh sách tự lấy từ Gemini API |

Nhãn trong danh sách model: 🆓 nhiều khả năng có free tier · 🧪 preview (giới hạn chặt hơn) · 💳 thường cần billing. Đây chỉ là gợi ý của bot, Google không trả thông tin này qua API.

## Lưu ý

- **Chỉ chạy một nơi duy nhất.** Nếu còn service cũ trên Render, hãy *Suspend/Delete* nó, vì hai bản chạy cùng một token sẽ khiến bot trả lời hai lần.
- Northflank ghi rõ gói free dùng để thử nghiệm/hobby, không dành cho production. Một nguồn bên thứ ba cho rằng gói này đòi thẻ tín dụng khi đăng ký, trong khi trang giá của Northflank ghi "No card needed"; hãy xem lúc bạn đăng ký.
- Nếu `/status` hiển thị RAM bất thường, đặt biến `RAM_LIMIT_MB` bằng RAM thực của gói bạn chọn.
- Free tier Gemini: nội dung bạn gửi có thể được Google dùng để cải thiện sản phẩm. Hạn mức từng model xem trong AI Studio.
- Nếu Gemini báo lỗi "location is not supported", thử đổi region của service trên Northflank.
- Web search dùng Google Search grounding; Google có yêu cầu hiển thị "Search Suggestions", hãy đọc điều khoản nếu bot công khai.
