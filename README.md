# Gemini Discord Bot (Northflank + MongoDB Atlas)

Slash command: `/ask` (hỏi đáp, OCR, suy nghĩ sâu, tra web), `/refresh`, `/session` và `/status`.
Mỗi người dùng có **một phiên chat riêng**; khi phiên kết thúc, cuộc trò chuyện được **lưu trữ vĩnh viễn** trên MongoDB Atlas kèm **Discord ID** của người đó.

## 1. Chuẩn bị token

**Discord**
1. https://discord.com/developers/applications → *New Application* → tab **Bot** → *Reset Token* → copy (`DISCORD_TOKEN`).
2. Tab **OAuth2 → URL Generator**: scope `bot` + `applications.commands`; quyền *Send Messages*, *Attach Files*, *Embed Links*. Mở URL tạo ra để mời bot vào server.
3. Không cần bật Privileged Intents (bot chỉ dùng slash command).

**Gemini**: https://aistudio.google.com/apikey → *Create API key* (`GEMINI_API_KEY`).

## 2. Đưa code lên GitHub

Tạo repo **private**, đẩy ở **thư mục gốc**: `bot.py`, `requirements.txt`, `Dockerfile`, `.dockerignore`, `.gitignore`, `.env.example`, `README.md`. **Không** commit file `.env`.

## 3. Tạo database MongoDB Atlas

1. https://www.mongodb.com/cloud/atlas → đăng ký → tạo cluster **M0 (Free)**, chọn region gần service của bạn.
2. **Security → Database Access → Add New Database User**: đặt username và mật khẩu mạnh (nên chỉ dùng chữ và số, để khỏi phải mã hoá ký tự đặc biệt trong URI), quyền *Read and write to any database*.
3. **Security → Network Access → Add IP Address**: Northflank free không có IP cố định, nên chọn **Allow access from anywhere (`0.0.0.0/0`)**. Vì vậy mật khẩu ở bước 2 phải mạnh.
4. **Connect → Drivers → Python**: copy connection string, thay `<password>` bằng mật khẩu thật, rồi đặt vào biến môi trường `MONGODB_URI` (secret). Mật khẩu có ký tự đặc biệt như `@ : / ?` thì phải mã hoá URL.

Bot tự tạo database `gemini_bot` cùng hai collection `sessions` và `archives`, cùng các index cần thiết. Không cần tạo tay.

## 4. Deploy lên Northflank

1. Đăng ký https://northflank.com → tạo **Project** (gói *Developer Sandbox* miễn phí), chọn region.
2. Liên kết GitHub: *Account settings → Git* → cấp quyền cho repo.
3. Trong project: **Create new → Service → Combined service**:
   - Repository và branch: repo của bạn, `main`
   - Build type: **Dockerfile** (đường dẫn `/Dockerfile`, context `/`)
4. **Runtime variables** (đánh dấu là secret): `DISCORD_TOKEN`, `GEMINI_API_KEY`, `MONGODB_URI`. Nên tạo một *Secret group* chứa 3 biến này rồi gắn vào service.
5. **Networking → Ports**: thêm cổng `10000`, giao thức `HTTP`.
6. **Resources**: chọn gói compute nhỏ nhất (free), 1 instance.
7. Tạo service. Khi tab **Logs** hiện `Đã đồng bộ slash commands` là bot đã chạy.

> Khi test, thêm `GUILD_ID` (ID server của bạn) để lệnh cập nhật tức thì.
> Gói free của Northflank chạy liên tục nên không cần UptimeRobot.

## 5. Cách dùng

| Lệnh | Tác dụng |
|---|---|
| `/ask prompt:` | Hỏi đáp với AI |
| `/refresh` | Lưu trữ cuộc trò chuyện hiện tại và bắt đầu phiên mới |
| `/session` | Xem phiên hiện tại: số lượt, lúc bắt đầu, lần chat gần nhất, thời điểm tự làm mới (chỉ mình bạn thấy) |
| `/status` | Tình trạng bot: RAM, uptime, Gemini API, MongoDB, thống kê. **Chỉ các Discord ID trong `STATUS_ALLOWED_IDS` được dùng**, kết quả chỉ người gõ lệnh thấy |

Option của `/ask`:

| Option | Tác dụng |
|---|---|
| `file` | Đính kèm ảnh/PDF để hỏi về nội dung hoặc OCR |
| `ocr` | Chỉ trích xuất chữ từ `file` |
| `thinking_level` | Chọn mức suy nghĩ sâu `low` / `medium` / `high` + hiện tóm tắt suy nghĩ. Bỏ trống = mặc định của model |
| `web` | Tra cứu web (Google Search grounding), kèm nguồn |
| `model` | Chọn model; **bỏ trống = tự động** (xem mục 7) |

**Về `thinking_level`:**
- Model Gemini 3.x nhận thẳng mức `low`/`medium`/`high`. Model Gemini 2.5 dùng ngân sách token: low = 1.024, medium = 8.192, high = 24.576.
- Model đời cũ hơn 2.5 không hỗ trợ suy nghĩ; bot sẽ báo ở chân tin nhắn.
- Nếu model không có mức `medium` (một số model Pro), bot tự thử `high`, và ghi chú mức thực tế đã dùng.
- Mức cao suy nghĩ lâu hơn và tốn nhiều token hơn, dễ chạm giới hạn của gói free.

Danh sách gợi ý lấy từ Gemini API, trừ các model trong `HIDDEN_MODELS`. Nhãn trong danh sách: 🆓 nhiều khả năng có free tier · 🧪 preview (giới hạn chặt hơn) · 💳 thường cần billing. Đây chỉ là gợi ý của bot, Google không trả thông tin này qua API.

## 6. Phiên chat và lưu trữ

- Mỗi người dùng Discord có **1 phiên riêng** (dùng chung ở mọi server có bot). Bot nhớ **20 tin gần nhất** để trả lời tiếp mạch (`MAX_HISTORY_MESSAGES`).
- Phiên **kết thúc và được lưu trữ** khi: bạn gõ `/refresh`, hoặc **2 giờ không chat** (`SESSION_IDLE_HOURS`), hoặc phiên quá **500 lượt** (`SESSION_MAX_TURNS`). Lần `/ask` kế tiếp tự mở phiên mới.
- Phiên hết hạn được lưu trữ **kể cả khi người dùng không quay lại**: bot quét mỗi 5 phút (`SWEEP_MINUTES`).
- Chân tin nhắn hiện `🆕 phiên mới` hoặc `💬 phiên: N lượt`.

**Dữ liệu trong MongoDB**

- `sessions`: phiên đang hoạt động (mỗi người 1 document, `_id` = Discord ID). Chỉ chứa ngữ cảnh gần nhất, bị xoá khi phiên kết thúc.
- `archives`: **lưu trữ vĩnh viễn, không tự xoá.** Mỗi phiên 1 document:

```json
{
  "_id": "a1b2c3d4...",            // mã phiên
  "user_id": 123456789012345678,   // Discord ID (int64)
  "user_id_str": "123456789012345678", // cùng ID, dạng chữ để dễ tìm
  "created_at": "...", "last_active": "...",
  "ended_at": "...",               // null nếu phiên đang diễn ra
  "end_reason": "refresh",         // refresh | idle_timeout | max_turns
  "turns": 12,
  "messages": [ {"role": "user", "text": "...", "at": "..."},
                {"role": "model", "text": "...", "at": "...", "model": "gemini-2.5-flash"} ]
}
```

Mỗi lượt hỏi đáp được ghi vào `archives` ngay lập tức (đủ toàn văn), nên bot khởi động lại giữa chừng cũng không mất dữ liệu.

**Tìm lịch sử một người** (Atlas → Browse Collections → `archives` → ô Filter). Mỗi phiên có 2 trường chứa Discord ID:
- `user_id_str`: ID dạng **chữ**, dễ tìm nhất: `{ "user_id_str": "1240503090185109536" }`
- `user_id`: ID dạng số **int64** (Atlas hiển thị là `Long('...')`, đó chính là ID thật). Phải lọc bằng `{ "user_id": { "$numberLong": "1240503090185109536" } }`.

> **Bẫy:** gõ thẳng `{ "user_id": 1240503090185109536 }` sẽ **không ra kết quả**, vì ô lọc đọc số lớn như số thực và làm tròn thành `1240503090185109504`. Phiên lưu từ bản cũ chưa có `user_id_str`, hãy dùng cách `$numberLong`.

Khác biệt cần nhớ:
- Bản lưu trữ lưu **toàn văn**; ngữ cảnh gửi cho AI thì cắt mỗi tin còn 4000 ký tự.
- Bot **không lưu file đính kèm**, chỉ lưu tên file và câu trả lời. Hỏi tiếp về file cũ chỉ được khi câu trả lời đã chứa đủ nội dung.
- Nếu MongoDB lỗi hoặc không đặt `MONGODB_URI`, bot vẫn trả lời bình thường nhưng không có trí nhớ và không lưu trữ.
- Nâng cấp từ bản cũ: bot tự gỡ TTL index cũ của `sessions` (index này xoá phiên hết hạn mà không lưu trữ).

## 7. Model tự động và tự chuyển khi quá tải

Khi bạn **không chọn `model`** (hoặc chọn `🤖 Tự động`), bot dùng chuỗi **flash-lite → flash → pro**:
- Mỗi hạng chọn 1 model tốt nhất từ danh sách API: ưu tiên bản ổn định, rồi bản mới nhất. Tên model không thuộc ba hạng này (như `gemini-4-*`, `nano-banana`, `omni`) sẽ không vào chuỗi tự động.
- Nếu model đang dùng **quá tải (503/500), hết lượt (429), không còn tồn tại (404) hoặc phản hồi quá lâu**, bot chuyển ngay sang model kế tiếp cho chính câu hỏi đó, và ghi chú ở chân tin nhắn.
- Model vừa lỗi bị **tạm bỏ qua** một lúc nên các câu hỏi sau dùng luôn model kế tiếp (≈1 phút nếu quá tải, theo thời gian Google yêu cầu nếu hết lượt, 30 phút nếu hết lượt trong ngày, 1 giờ nếu model không có quota free). Hết thời gian chờ, bot tự quay lại model ưu tiên.
- Khi bạn **chọn model cụ thể**, bot không tự chuyển model, chỉ báo lỗi.
- `/status` hiện chuỗi hiện tại và model nào đang bị tạm bỏ qua (⏳).

**Ẩn model khỏi gợi ý:** đặt `HIDDEN_MODELS` (cách nhau bằng dấu phẩy, hỗ trợ `*`). Mặc định đã ẩn các model bạn chỉ định. Ẩn chỉ ảnh hưởng danh sách gợi ý: ai gõ tay đúng tên model vẫn dùng được. **Toàn bộ model Pro** (tên có `pro`) cũng bị ẩn khỏi gợi ý (đặt `HIDE_PRO_MODELS=0` để hiện lại); model Pro vẫn là bước cuối của chuỗi tự động. Dùng `EXTRA_MODELS` để thêm model vào danh sách, `AUTO_MODEL_CHAIN` để tự quy định thứ tự chuỗi tự động.

## Lưu ý

- **Dung lượng:** cluster M0 chỉ có 512MB và lưu trữ vĩnh viễn sẽ đầy dần. `/status` hiển thị dung lượng đã dùng. Khi gần đầy, hãy xuất dữ liệu cũ ra file rồi dọn bớt, hoặc nâng cấp cluster.
- **Quyền riêng tư:** toàn bộ nội dung chat được lưu vĩnh viễn gắn với Discord ID, và người quản lý database đọc được. Hãy thông báo rõ cho thành viên server (nên có chính sách riêng tư ngắn gọn) và cân nhắc cách xử lý khi có người yêu cầu xoá dữ liệu của họ.
- **Chỉ chạy một nơi duy nhất.** Nếu còn service cũ trên Render, hãy *Suspend/Delete* nó, vì hai bản chạy cùng một token sẽ khiến bot trả lời hai lần.
- Northflank ghi rõ gói free dùng để thử nghiệm/hobby, không dành cho production. Một nguồn bên thứ ba cho rằng gói này đòi thẻ tín dụng, trong khi trang giá của Northflank ghi "No card needed"; hãy xem lúc bạn đăng ký.
- Nếu `/status` hiển thị RAM bất thường, đặt biến `RAM_LIMIT_MB` bằng RAM thực của gói bạn chọn.
- Lịch sử dài làm mỗi lần hỏi tốn nhiều token hơn, dễ chạm giới hạn của gói free Gemini hơn.
- Free tier Gemini: nội dung bạn gửi có thể được Google dùng để cải thiện sản phẩm. Hạn mức từng model xem trong AI Studio.
- Nếu Gemini báo lỗi "location is not supported", thử đổi region của service trên Northflank.
- Web search dùng Google Search grounding; Google có yêu cầu hiển thị "Search Suggestions", hãy đọc điều khoản nếu bot công khai.
