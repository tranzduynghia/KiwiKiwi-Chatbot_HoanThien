"""Bộ giám sát bộ nhớ khẩn cấp (Memory Watchdog) kèm vùng đệm dự phòng 20MB chống OOMKilled trên container 488MB."""
from __future__ import annotations

import asyncio
import gc
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import psutil

log = logging.getLogger("gemini-bot.memory")

DEFAULT_CEILING_MB = 488.0
DEFAULT_RESERVE_BYTES = 20 * 1024 * 1024  # 20 MB vùng đệm vật lý dự phòng
DEFAULT_TRIGGER_RSS_MB = 464.0            # 488 MB - 20 MB dự phòng - 4 MB khoảng thực thi an toàn
DEFAULT_RECOVER_RSS_MB = 400.0            # Mức RSS mục tiêu để cấp phát lại vùng đệm 20 MB
DEFAULT_CHECK_INTERVAL_SECONDS = 1.5      # Kiểm tra nhẹ mỗi 1500ms
DEFAULT_SYNC_TIMEOUT_SECONDS = 5.0        # Giới hạn cứng 5s đồng bộ MongoDB Atlas tránh treo watchdog


def _default_read_rss_bytes() -> int:
    """Đọc RSS thực tế của tiến trình Python hiện tại từ hệ điều hành (bytes)."""
    return int(psutil.Process().memory_info().rss)


class MemoryWatchdog:
    """Quản lý vùng đệm dự phòng 20MB và quy trình 4 bước Sync-Before-Purge khi RSS vượt 464MB."""

    def __init__(
        self,
        *,
        reserve_bytes: int = DEFAULT_RESERVE_BYTES,
        ceiling_mb: float = DEFAULT_CEILING_MB,
        trigger_mb: float = DEFAULT_TRIGGER_RSS_MB,
        recover_mb: float = DEFAULT_RECOVER_RSS_MB,
        check_interval: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        sync_timeout: float = DEFAULT_SYNC_TIMEOUT_SECONDS,
        read_rss_bytes: Optional[Callable[[], int]] = None,
        sync_before_purge: Optional[Callable[[], Awaitable[dict[str, int]]]] = None,
        purge_caches: Optional[Callable[[], dict[str, int]]] = None,
        record_audit_event: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
        allocate_on_init: bool = True,
    ):
        self.reserve_bytes = max(0, int(reserve_bytes))
        self.ceiling_mb = float(ceiling_mb)
        self.trigger_mb = float(trigger_mb)
        self.recover_mb = float(recover_mb)
        self.check_interval = max(0.05, float(check_interval))
        self.sync_timeout = max(0.05, float(sync_timeout))

        self._read_rss_bytes = read_rss_bytes or _default_read_rss_bytes
        self._sync_before_purge = sync_before_purge
        self._purge_caches = purge_caches
        self._record_audit_event = record_audit_event
        self._bot: Any = None

        self._reserve_buffer: Optional[bytearray] = None
        self._state: str = "ready"  # "ready" | "cleaning" | "released_waiting_recovery"
        self._lock = asyncio.Lock()
        self._emergency_count: int = 0
        self._pending_recovery_event: Optional[dict[str, Any]] = None
        self._last_event: Optional[dict[str, Any]] = None

        if allocate_on_init:
            self.allocate_reserve()

    def attach_bot(self, bot: Any) -> None:
        """Gắn tham chiếu Discord client để có thể dọn bộ đệm tin nhắn nội bộ khi khẩn cấp."""
        self._bot = bot

    @property
    def reserve_ready(self) -> bool:
        """Cho biết vùng đệm dự phòng 20MB hiện có đang được giữ trong RAM hay không."""
        return self._reserve_buffer is not None and len(self._reserve_buffer) == self.reserve_bytes

    @property
    def state(self) -> str:
        """Trả về trạng thái máy trạng thái hiện hành của watchdog."""
        return self._state

    def allocate_reserve(self) -> bool:
        """Cấp phát vùng đệm vật lý bằng byte khác 0 để Linux commit trang RSS thực (giải phóng tức thì qua munmap)."""
        if self.reserve_bytes <= 0:
            self._reserve_buffer = bytearray()
            self._state = "ready"
            return True
        if self._reserve_buffer is None or len(self._reserve_buffer) != self.reserve_bytes:
            # Điền byte 0x01 giúp hệ điều hành cam kết trang vật lý thật thay vì lazy zero-page
            self._reserve_buffer = bytearray(b"\x01") * self.reserve_bytes
        self._state = "ready"
        return True

    def release_reserve(self) -> bool:
        """Bước 1: Hủy tham chiếu vùng đệm dự phòng ngay lập tức để lấy khoảng trống RAM cho I/O MongoDB."""
        had_buffer = self._reserve_buffer is not None
        self._reserve_buffer = None
        gc.collect(0)
        return had_buffer

    def current_rss_mb(self) -> float:
        """Đọc mức RSS hiện tại theo đơn vị MB (làm tròn 1 chữ số thập phân)."""
        return round(self._read_rss_bytes() / (1024 * 1024), 1)

    def _purge_discord_message_cache(self) -> int:
        """Dọn hàng đợi tin nhắn gần đây trong bộ nhớ nội bộ của discord.py (nếu có)."""
        bot = self._bot
        if bot is None:
            return 0
        conn = getattr(bot, "_connection", None)
        messages = getattr(conn, "_messages", None) if conn is not None else None
        if messages is None:
            return 0
        try:
            count = len(messages)
            messages.clear()
            return count
        except Exception:
            return 0

    async def _finalize_recovery(self, rss_mb: float) -> dict[str, Any]:
        """Bước 4: Cấp phát lại vùng đệm 20MB khi RSS < 400MB và ghi Audit/System Log."""
        self.allocate_reserve()
        event = dict(self._pending_recovery_event or {})
        event["recovered_at"] = datetime.now(timezone.utc).isoformat()
        event["recovered_rss_mb"] = rss_mb
        event["reserve_reallocated"] = True
        self._pending_recovery_event = None
        self._last_event = event

        synced_total = int(event.get("synced_total", 0))
        purged_total = int(event.get("purged_total", 0))
        timed_out = bool(event.get("sync_timed_out", False))
        log.warning(
            "MemoryWatchdog hoàn tất phục hồi (RSS %.1f MB < %.0f MB): đã cấp phát lại vùng đệm %.0f MB · "
            "đã đồng bộ %d bản ghi lên MongoDB Atlas%s · đã giải phóng %d mục cache.",
            rss_mb,
            self.recover_mb,
            self.reserve_bytes / (1024 * 1024),
            synced_total,
            " (hết hạn 5s)" if timed_out else "",
            purged_total,
        )

        if self._record_audit_event is not None:
            try:
                await self._record_audit_event(event)
            except Exception as exc:
                log.warning("Không ghi được Audit Log sự kiện MemoryWatchdog: %s", exc)
        return event

    async def _execute_emergency_cleanup(
        self,
        *,
        simulated: bool = False,
        triggered_by: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Thực thi trọn vẹn quy trình 4 bước Sync-Before-Purge (dùng chung cho cảnh báo RSS thật và mô phỏng Admin)."""
        self._state = "cleaning"
        self._emergency_count += 1
        triggered_at = datetime.now(timezone.utc).isoformat()
        peak_rss_mb = self.current_rss_mb()

        if simulated:
            log.info(
                "Bắt đầu MÔ PHỎNG quy trình 4 bước MemoryWatchdog (RSS hiện tại %.1f MB, ngưỡng thật %.0f MB).",
                peak_rss_mb,
                self.trigger_mb,
            )
        else:
            log.warning(
                "CẢNH BÁO BỘ NHỚ KHẨN CẤP: RSS đạt %.1f MB > %.0f MB (trần %.0f MB). "
                "Bắt đầu quy trình 4 bước (Giải phóng đệm -> Đồng bộ MongoDB -> Dọn cache & GC -> Phục hồi).",
                peak_rss_mb,
                self.trigger_mb,
                self.ceiling_mb,
            )

        # BƯỚC 1: Giải phóng ngay vùng đệm dự phòng 20MB lấy khoảng trống cho network I/O và serialization
        released = self.release_reserve()
        rss_after_release_mb = self.current_rss_mb()

        # BƯỚC 2: Bắt buộc đồng bộ dữ liệu đang chờ trong RAM lên MongoDB Atlas (timeout cứng 5 giây)
        synced_details: dict[str, int] = {}
        sync_timed_out = False
        sync_error: Optional[str] = None
        if self._sync_before_purge is not None:
            try:
                result = await asyncio.wait_for(self._sync_before_purge(), timeout=self.sync_timeout)
                if isinstance(result, dict):
                    synced_details = {str(k): int(v) for k, v in result.items()}
            except asyncio.TimeoutError:
                sync_timed_out = True
                sync_error = f"Quá thời gian đồng bộ MongoDB Atlas ({self.sync_timeout:g}s)"
                log.warning("MemoryWatchdog Bước 2 quá hạn %.1fs — tiếp tục dọn cache RAM để tránh OOM.", self.sync_timeout)
            except Exception as exc:
                sync_error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("MemoryWatchdog Bước 2 lỗi đồng bộ MongoDB Atlas: %s", sync_error)

        # BƯỚC 3: Chỉ sau khi đồng bộ xong (hoặc timeout) mới xóa cache RAM không thiết yếu & ép GC
        purged_details: dict[str, int] = {}
        if self._purge_caches is not None:
            try:
                purged = self._purge_caches()
                if isinstance(purged, dict):
                    purged_details.update({str(k): int(v) for k, v in purged.items()})
            except Exception as exc:
                log.warning("MemoryWatchdog Bước 3 lỗi khi dọn cache: %s", exc)

        discord_msgs_cleared = self._purge_discord_message_cache()
        if discord_msgs_cleared:
            purged_details["discord_messages"] = discord_msgs_cleared

        gc_collected = gc.collect()
        rss_after_purge_mb = self.current_rss_mb()

        synced_total = sum(v for k, v in synced_details.items() if k != "total")
        if "total" in synced_details and synced_total == 0:
            synced_total = synced_details["total"]
        purged_total = sum(purged_details.values())

        event: dict[str, Any] = {
            "simulated": bool(simulated),
            "triggered_by": dict(triggered_by) if triggered_by else None,
            "triggered_at": triggered_at,
            "peak_rss_mb": peak_rss_mb,
            "rss_after_release_mb": rss_after_release_mb,
            "rss_after_purge_mb": rss_after_purge_mb,
            "reserve_released": released,
            "synced_details": synced_details,
            "synced_total": synced_total,
            "sync_timed_out": sync_timed_out,
            "sync_error": sync_error,
            "purged_details": purged_details,
            "purged_total": purged_total,
            "gc_collected": gc_collected,
            "recovered_at": None,
            "recovered_rss_mb": None,
            "reserve_reallocated": False,
        }
        self._pending_recovery_event = event
        self._last_event = dict(event)

        # BƯỚC 4: Nếu RSS đã ở dưới ngưỡng 400MB -> cấp phát lại ngay vùng đệm 20MB và ghi Audit Log
        if rss_after_purge_mb < self.recover_mb:
            return await self._finalize_recovery(rss_after_purge_mb)

        self._state = "released_waiting_recovery"
        log.warning(
            "MemoryWatchdog sau dọn RAM: RSS %.1f MB (vẫn >= %.0f MB). "
            "Tạm giữ trạng thái giải phóng đệm cho tới khi RSS < %.0f MB.",
            rss_after_purge_mb,
            self.recover_mb,
            self.recover_mb,
        )
        return event

    async def check_once(self) -> Optional[dict[str, Any]]:
        """Thực hiện 1 nhịp kiểm tra RSS: kích hoạt quy trình 4 bước nếu > 464MB hoặc cấp phát lại khi < 400MB."""
        async with self._lock:
            rss_mb = self.current_rss_mb()

            # Đang chờ RSS giảm xuống dưới 400MB để cấp phát lại vùng đệm 20MB
            if self._state == "released_waiting_recovery":
                if rss_mb < self.recover_mb:
                    return await self._finalize_recovery(rss_mb)
                return None

            # Chưa chạm ngưỡng khẩn cấp 464MB
            if rss_mb <= self.trigger_mb:
                return None

            return await self._execute_emergency_cleanup(simulated=False)

    async def simulate_emergency_cleanup(
        self,
        *,
        triggered_by: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Chạy mô phỏng an toàn toàn bộ quy trình 4 bước Sync-Before-Purge theo yêu cầu Admin mà không cần ép tăng RAM."""
        async with self._lock:
            return await self._execute_emergency_cleanup(simulated=True, triggered_by=triggered_by)

    async def run_forever(self, is_closed: Callable[[], bool]) -> None:
        """Vòng lặp nền kiểm tra RSS mỗi 1500ms cho đến khi tiến trình đóng."""
        try:
            while not is_closed():
                await asyncio.sleep(self.check_interval)
                if is_closed():
                    break
                try:
                    await self.check_once()
                except Exception as exc:
                    log.warning("Lỗi không mong muốn trong vòng lặp MemoryWatchdog: %s", exc)
        except asyncio.CancelledError:
            return

    def status_snapshot(self) -> dict[str, Any]:
        """Xuất trạng thái vùng đệm dự phòng 20MB và lịch sử cứu OOM cho thẻ 'Tài nguyên RAM & CPU'."""
        reserve_mb_display = round(self.reserve_bytes / (1024 * 1024), 1)
        reserve_label_num = int(reserve_mb_display) if reserve_mb_display == int(reserve_mb_display) else reserve_mb_display
        if self._state == "cleaning":
            status_label = f"Dự phòng {reserve_label_num}MB: Đang xả & dọn RAM..."
        elif self._state == "released_waiting_recovery" or not self.reserve_ready:
            status_label = f"Dự phòng {reserve_label_num}MB: Đã giải phóng (chờ < {int(self.recover_mb)}MB)"
        else:
            status_label = f"Dự phòng {reserve_label_num}MB: Sẵn sàng"

        return {
            "reserve_mb": reserve_mb_display,
            "reserve_ready": self.reserve_ready,
            "state": self._state,
            "status_label": status_label,
            "trigger_threshold_mb": self.trigger_mb,
            "recover_threshold_mb": self.recover_mb,
            "ceiling_mb": self.ceiling_mb,
            "check_interval_ms": int(self.check_interval * 1000),
            "emergency_count": self._emergency_count,
            "last_event": self._last_event,
        }
