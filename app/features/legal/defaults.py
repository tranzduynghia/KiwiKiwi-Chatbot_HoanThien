"""Nội dung pháp lý Rich-Text mặc định cho 5 trang công khai và quy tắc xác thực phiên bản ToS."""
from __future__ import annotations

import copy
import re
from typing import Any, Optional

LEGAL_DOC_KEYS = ("toslinks", "legal", "terms", "policies", "guidelines")
DEFAULT_ACTIVE_TOS_VERSION = "v1.0"
TOS_VERSION_RE = re.compile(r"^v\d+\.\d+(?:\.\d+)?$")
PUBLIC_SITE_ORIGIN = "https://kiwikiwi.dpdns.org"

LEGAL_DOC_LABELS: dict[str, str] = {
    "toslinks": "Cổng danh mục Điều khoản (/toslinks)",
    "legal": "Pháp lý (/legal)",
    "terms": "Điều khoản sử dụng (/terms)",
    "policies": "Chính sách bảo mật (/policies)",
    "guidelines": "Chính sách cộng đồng (/guidelines)",
}

DEFAULT_LEGAL_DOCS: dict[str, dict[str, str]] = {
    "toslinks": {
        "title": "Cổng danh mục Điều khoản & Pháp lý",
        "subtitle": "Tổng hợp liên kết chính thức tới toàn bộ tài liệu pháp lý và chính sách vận hành KiwiKiwi Bot",
        "body": (
            "<h2>Danh mục tài liệu chính thức</h2>"
            "<p>Chào mừng bạn đến với Trung tâm Pháp lý của <strong>KiwiKiwi Bot</strong>. "
            "Vui lòng tham khảo các tài liệu dưới đây trước khi sử dụng lệnh <strong>/ask</strong> hoặc bảng điều khiển Web:</p>"
            "<ul>"
            '<li><a href="/legal">Pháp lý (/legal)</a> — Thông tin vận hành và tuyên bố miễn trừ trách nhiệm AI.</li>'
            '<li><a href="/terms">Điều khoản sử dụng (/terms)</a> — Quy định ràng buộc khi dùng bot và phiên chat AI.</li>'
            '<li><a href="/policies">Chính sách bảo mật (/policies)</a> — Cách thu thập, lưu trữ và bảo vệ dữ liệu phiên.</li>'
            '<li><a href="/guidelines">Chính sách cộng đồng (/guidelines)</a> — Chuẩn mực ứng xử văn minh và sử dụng AI có trách nhiệm.</li>'
            "</ul>"
            "<hr />"
            "<p>Khi phiên bản ToS được nâng cấp, người dùng cần xác nhận đồng ý phiên bản mới nhất trên Discord hoặc Web để tiếp tục sử dụng dịch vụ.</p>"
        ),
    },
    "legal": {
        "title": "Thông tin Pháp lý & Tuyên bố Miễn trừ",
        "subtitle": "Tổng quan pháp lý và thông tin vận hành hệ thống KiwiKiwi Bot",
        "body": (
            "<h2>1. Giới thiệu dịch vụ</h2>"
            "<p><strong>KiwiKiwi Bot</strong> là trợ lý trí tuệ nhân tạo chạy trên nền tảng Discord, sử dụng mô hình Google Gemini để hỗ trợ hỏi đáp, tóm tắt, phân tích tài liệu và tra cứu thông tin.</p>"
            "<h2>2. Tính chất nội dung AI</h2>"
            "<blockquote>Mọi câu trả lời do AI tạo ra chỉ mang tính tham khảo. Quản trị viên không chịu trách nhiệm pháp lý đối với các quyết định tài chính, y tế, pháp luật hoặc kỹ thuật dựa hoàn toàn vào nội dung do bot sinh ra.</blockquote>"
            "<h2>3. Quyền sở hữu &amp; Liên hệ</h2>"
            "<p>Tên gọi, giao diện quản trị và mã nguồn cấu hình thuộc quyền vận hành của đội ngũ quản trị KiwiKiwi. Mọi yêu cầu pháp lý hoặc khiếu nại bản quyền vui lòng liên hệ trực tiếp quản trị viên hệ thống.</p>"
        ),
    },
    "terms": {
        "title": "Điều khoản Sử dụng (Terms of Service)",
        "subtitle": "Quy định ràng buộc khi sử dụng lệnh /ask, phiên chat AI và Web Dashboard",
        "body": (
            "<h2>1. Chấp thuận điều khoản</h2>"
            "<p>Bằng việc nhấn <strong>\"Tôi đồng ý\"</strong> trên Discord hoặc Web, bạn xác nhận đã đọc, hiểu và đồng ý tuân thủ phiên bản Điều khoản Sử dụng hiện hành của KiwiKiwi Bot.</p>"
            "<h2>2. Phạm vi sử dụng hợp lệ</h2>"
            "<ul>"
            "<li>Không sử dụng bot để tạo nội dung vi phạm pháp luật, lừa đảo, mã độc, quấy rối hoặc xâm phạm quyền riêng tư của người khác.</li>"
            "<li>Không cố tình spam lệnh, lạm dụng hạn mức Gemini API hoặc tìm cách vượt qua cơ chế giới hạn tốc độ / bảo trì hệ thống.</li>"
            "</ul>"
            "<h2>3. Thay đổi điều khoản &amp; Bảo trì</h2>"
            "<p>Quản trị viên có quyền cập nhật Điều khoản Sử dụng và nâng phiên bản ToS bất cứ lúc nào. Khi phiên bản ToS thay đổi, người dùng cần xác nhận lại trước khi tiếp tục sử dụng bot.</p>"
        ),
    },
    "policies": {
        "title": "Chính sách Bảo mật (Privacy Policy)",
        "subtitle": "Cách KiwiKiwi Bot thu thập, lưu trữ và bảo vệ dữ liệu phiên hội thoại",
        "body": (
            "<h2>1. Dữ liệu được thu thập</h2>"
            "<ul>"
            "<li>Discord User ID, tên hiển thị và nội dung câu hỏi/phản hồi trong phiên chat AI để duy trì ngữ cảnh hội thoại.</li>"
            "<li>Bản ghi xác nhận đồng ý Điều khoản (Discord User ID, phiên bản ToS, thời điểm đồng ý theo chuẩn UTC và nguồn xác nhận).</li>"
            "</ul>"
            "<h2>2. Bảo mật &amp; Không lưu bí mật người dùng</h2>"
            "<ul>"
            "<li>Không gửi mật khẩu, mã khóa API, thông tin thẻ thanh toán hoặc giấy tờ tùy thân nhạy cảm vào lệnh <strong>/ask</strong>.</li>"
            "<li>Dữ liệu phiên được lưu trên MongoDB Atlas có kiểm soát truy cập chặt chẽ và tự động lưu trữ/dọn dẹp theo chính sách phiên.</li>"
            "</ul>"
            "<h2>3. Quyền xóa dữ liệu</h2>"
            "<p>Bạn có thể dùng lệnh <strong>/refresh</strong> để kết thúc phiên hiện tại bất kỳ lúc nào hoặc liên hệ quản trị viên để yêu cầu xóa lịch sử phiên chat.</p>"
        ),
    },
    "guidelines": {
        "title": "Chính sách Cộng đồng (Community Guidelines)",
        "subtitle": "Chuẩn mực ứng xử văn minh và sử dụng AI có trách nhiệm",
        "body": (
            "<h2>1. Tôn trọng cộng đồng</h2>"
            "<p>Sử dụng KiwiKiwi Bot một cách văn minh, tôn trọng thành viên khác trong máy chủ Discord và tuân thủ nội quy riêng của từng server.</p>"
            "<h2>2. Sử dụng AI có trách nhiệm</h2>"
            "<ul>"
            "<li>Kiểm chứng lại thông tin quan trọng trước khi chia sẻ rộng rãi.</li>"
            "<li>Không lạm dụng tệp đính kèm có dung lượng lớn hoặc nội dung nhạy cảm.</li>"
            "</ul>"
            "<h2>3. Xử lý vi phạm</h2>"
            "<p>Tài khoản vi phạm chính sách cộng đồng hoặc cố tình phá hoại hệ thống có thể bị tạm khóa hoặc thu hồi quyền sử dụng bot mà không cần báo trước.</p>"
        ),
    },
}


DEFAULT_TOS_UPDATED_AT: Optional[str] = None
DEFAULT_DISCORD_TOS_WELCOME_TEMPLATE = (
    "📜 **Chào mừng bạn đến với KiwiKiwi Bot!**\n"
    "Trước khi sử dụng lệnh `/ask` hoặc bắt đầu phiên AI, vui lòng đọc và đồng ý Điều khoản phiên bản `{version}`:\n"
    "• Điều khoản Dịch vụ: {base_url}/terms\n"
    "• Chính sách Quyền riêng tư: {base_url}/policies\n"
    "• Nguyên tắc Cộng đồng: {base_url}/guidelines"
)
DEFAULT_DISCORD_TOS_UPDATE_TEMPLATE = (
    "📜 **Điều khoản Dịch vụ đã được cập nhật lên phiên bản `{version}`** *(phiên bản cũ của bạn: `{previous_version}`)*\n"
    "Vui lòng xem lại và nhấn nút xác nhận trước khi tiếp tục sử dụng `/ask` hoặc phiên AI:\n"
    "• Điều khoản Dịch vụ: {base_url}/terms\n"
    "• Chính sách Quyền riêng tư: {base_url}/policies\n"
    "• Nguyên tắc Cộng đồng: {base_url}/guidelines"
)
DEFAULT_DISCORD_TOS_BUTTON_LABEL = "Tôi đồng ý (Accept Terms)"


def build_default_legal_config() -> dict[str, Any]:
    """Khởi tạo siêu dữ liệu nhánh `legal` gọn nhẹ trong `bot_config` (chỉ giữ phiên bản, mẫu thông báo Discord & mốc cập nhật tĩnh)."""
    return {
        "active_tos_version": DEFAULT_ACTIVE_TOS_VERSION,
        "tos_updated_at": DEFAULT_TOS_UPDATED_AT,
        "discord_tos_welcome_template": DEFAULT_DISCORD_TOS_WELCOME_TEMPLATE,
        "discord_tos_update_template": DEFAULT_DISCORD_TOS_UPDATE_TEMPLATE,
        "discord_tos_button_label": DEFAULT_DISCORD_TOS_BUTTON_LABEL,
    }


DEFAULT_LEGAL_AUTHOR_NAME = "Hệ thống KiwiKiwi"


def build_default_legal_document(doc_key: str) -> dict[str, Any]:
    """Trả về bản sao nội dung mặc định của một trong 5 trang pháp lý (mốc cập nhật tĩnh, chỉ đổi khi Admin lưu)."""
    key = str(doc_key or "").strip().lower()
    if key not in DEFAULT_LEGAL_DOCS:
        raise ValueError(f"Tài liệu pháp lý không hợp lệ: {doc_key}")
    base = copy.deepcopy(DEFAULT_LEGAL_DOCS[key])
    return {
        "_id": key,
        "key": key,
        "label": LEGAL_DOC_LABELS[key],
        "title": base["title"],
        "subtitle": base["subtitle"],
        "body": base["body"],
        "updated_at": DEFAULT_TOS_UPDATED_AT,
        "updated_by": "system-default",
        "updated_by_name": DEFAULT_LEGAL_AUTHOR_NAME,
        "updated_by_avatar_url": None,
    }


def validate_tos_version(version: str) -> str:
    """Xác thực chuỗi phiên bản ToS theo chuẩn `^v\\d+\\.\\d+(\\.\\d+)?$` (ví dụ `v1.0`, `v1.2.1`)."""
    cleaned = str(version or "").strip()
    if not TOS_VERSION_RE.fullmatch(cleaned):
        raise ValueError("Phiên bản ToS không hợp lệ. Phải theo định dạng vX.Y hoặc vX.Y.Z (ví dụ: v1.0, v1.1).")
    return cleaned
