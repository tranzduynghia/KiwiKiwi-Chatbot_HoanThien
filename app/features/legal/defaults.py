"""Nội dung pháp lý mặc định bằng tiếng Việt và quy tắc xác thực phiên bản ToS."""
from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any

LEGAL_DOC_KEYS = ("legal", "terms", "policies", "guidelines")
DEFAULT_ACTIVE_TOS_VERSION = "v1.0"
TOS_VERSION_RE = re.compile(r"^v\d+\.\d+(?:\.\d+)?$")
PUBLIC_SITE_ORIGIN = "https://kiwikiwi.dpdns.org"

LEGAL_DOC_LABELS: dict[str, str] = {
    "legal": "Thông tin Pháp lý (/legal)",
    "terms": "Điều khoản Dịch vụ (/terms)",
    "policies": "Chính sách Quyền riêng tư (/policies)",
    "guidelines": "Nguyên tắc Cộng đồng (/guidelines)",
}

DEFAULT_LEGAL_DOCS: dict[str, dict[str, str]] = {
    "legal": {
        "title": "Thông tin Pháp lý & Tuyên bố Miễn trừ",
        "subtitle": "Tổng quan pháp lý và thông tin vận hành hệ thống KiwiKiwi Bot",
        "body": (
            "1. Giới thiệu dịch vụ\n"
            "KiwiKiwi Bot là trợ lý trí tuệ nhân tạo chạy trên nền tảng Discord, sử dụng mô hình Google Gemini để hỗ trợ hỏi đáp, tóm tắt, phân tích tài liệu và tra cứu thông tin.\n\n"
            "2. Tính chất nội dung AI\n"
            "Mọi câu trả lời do AI tạo ra chỉ mang tính tham khảo. Quản trị viên không chịu trách nhiệm pháp lý đối với các quyết định tài chính, y tế, pháp luật hoặc kỹ thuật dựa hoàn toàn vào nội dung do bot sinh ra.\n\n"
            "3. Quyền sở hữu & Liên hệ\n"
            "Tên gọi, giao diện quản trị và mã nguồn cấu hình thuộc quyền vận hành của đội ngũ quản trị KiwiKiwi. Mọi yêu cầu pháp lý hoặc khiếu nại bản quyền vui lòng liên hệ trực tiếp quản trị viên hệ thống."
        ),
    },
    "terms": {
        "title": "Điều khoản Dịch vụ (Terms of Service)",
        "subtitle": "Quy định ràng buộc khi sử dụng lệnh /ask, phiên chat AI và Web Dashboard",
        "body": (
            "1. Chấp thuận điều khoản\n"
            "Bằng việc nhấn \"Tôi đồng ý\" trên Discord hoặc Web, bạn xác nhận đã đọc, hiểu và đồng ý tuân thủ phiên bản Điều khoản Dịch vụ hiện hành của KiwiKiwi Bot.\n\n"
            "2. Phạm vi sử dụng hợp lệ\n"
            "- Không sử dụng bot để tạo nội dung vi phạm pháp luật, lừa đảo, mã độc, quấy rối hoặc xâm phạm quyền riêng tư của người khác.\n"
            "- Không cố tình spam lệnh, lạm dụng hạn mức Gemini API hoặc tìm cách vượt qua cơ chế giới hạn tốc độ / bảo trì hệ thống.\n\n"
            "3. Thay đổi điều khoản & Bảo trì\n"
            "Quản trị viên có quyền cập nhật Điều khoản Dịch vụ và nâng phiên bản ToS bất cứ lúc nào. Khi phiên bản ToS thay đổi, người dùng cần xác nhận lại trước khi tiếp tục sử dụng bot."
        ),
    },
    "policies": {
        "title": "Chính sách Quyền riêng tư (Privacy Policy)",
        "subtitle": "Cách KiwiKiwi Bot thu thập, lưu trữ và bảo vệ dữ liệu phiên hội thoại",
        "body": (
            "1. Dữ liệu được thu thập\n"
            "- Discord User ID, tên hiển thị và nội dung câu hỏi/phản hồi trong phiên chat AI để duy trì ngữ cảnh hội thoại.\n"
            "- Bản ghi xác nhận đồng ý Điều khoản (Discord User ID, phiên bản ToS, thời điểm đồng ý theo chuẩn UTC và nguồn xác nhận).\n\n"
            "2. Bảo mật & Không lưu bí mật người dùng\n"
            "- Không gửi mật khẩu, mã khóa API, thông tin thẻ thanh toán hoặc giấy tờ tùy thân nhạy cảm vào lệnh /ask.\n"
            "- Dữ liệu phiên được lưu trên MongoDB Atlas có kiểm soát truy cập chặt chẽ và tự động lưu trữ/dọn dẹp theo chính sách phiên.\n\n"
            "3. Quyền xóa dữ liệu\n"
            "Bạn có thể dùng lệnh /refresh để kết thúc phiên hiện tại bất kỳ lúc nào hoặc liên hệ quản trị viên để yêu cầu xóa lịch sử phiên chat."
        ),
    },
    "guidelines": {
        "title": "Nguyên tắc Cộng đồng (Community Guidelines)",
        "subtitle": "Chuẩn mực ứng xử văn minh và sử dụng AI có trách nhiệm",
        "body": (
            "1. Tôn trọng cộng đồng\n"
            "Sử dụng KiwiKiwi Bot một cách văn minh, tôn trọng thành viên khác trong máy chủ Discord và tuân thủ nội quy riêng của từng server.\n\n"
            "2. Sử dụng AI có trách nhiệm\n"
            "- Kiểm chứng lại thông tin quan trọng trước khi chia sẻ rộng rãi.\n"
            "- Không lạm dụng tệp đính kèm có dung lượng lớn hoặc nội dung nhạy cảm.\n\n"
            "3. Xử lý vi phạm\n"
            "Tài khoản vi phạm nguyên tắc cộng đồng hoặc cố tình phá hoại hệ thống có thể bị tạm khóa hoặc thu hồi quyền sử dụng bot mà không cần báo trước."
        ),
    },
}


def build_default_legal_config() -> dict[str, Any]:
    """Khởi tạo cấu trúc nhánh `legal` mặc định trong `bot_config`."""
    return {
        "active_tos_version": DEFAULT_ACTIVE_TOS_VERSION,
        "tos_updated_at": datetime.now(timezone.utc).isoformat(),
        "docs": copy.deepcopy(DEFAULT_LEGAL_DOCS),
    }


def validate_tos_version(version: str) -> str:
    """Xác thực chuỗi phiên bản ToS theo chuẩn `^v\\d+\\.\\d+(\\.\\d+)?$` (ví dụ `v1.0`, `v1.2.1`)."""
    cleaned = str(version or "").strip()
    if not TOS_VERSION_RE.fullmatch(cleaned):
        raise ValueError("Phiên bản ToS không hợp lệ. Phải theo định dạng vX.Y hoặc vX.Y.Z (ví dụ: v1.0, v1.1).")
    return cleaned
