"""Slash command Discord `/ask` và autocomplete gợi ý model Gemini theo cấu hình động."""
from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import datetime
from typing import Any, Literal, Optional

import discord
from discord import app_commands
from google.genai import errors as genai_errors
from google.genai import types

from app.core.concurrency import DynamicSemaphore, UserLockManager
from app.core.errors import UserError
from app.core.settings import PACIFIC_TZ
from app.core.utils import chunk_text, clean_title
from app.features.chat.attachments import read_attachment
from app.features.chat.cooldown import UserCooldownTracker
from app.features.chat.gemini_service import GeminiChatService
from app.features.chat.parser import ParsedResponse, explain_error, parse_response
from app.features.config.repository import ConfigRepository
from app.features.models.cooldown import ModelCooldownTracker
from app.features.models.registry import ModelRegistry
from app.features.monitoring.stats import UsageStats
from app.features.sessions.history import history_to_contents
from app.features.sessions.repository import SessionRepository

log = logging.getLogger("gemini-bot.chat")
NO_MENTIONS = discord.AllowedMentions.none()


def _build_system_instruction(cfg: dict[str, Any], *, ocr: bool) -> str:
    """Ghép System Instruction chính, ngày hiện tại và OCR Instruction theo cấu hình động."""
    system_prompt = cfg.get("system_prompt") or ""
    if cfg.get("append_current_date", True):
        system = f"{system_prompt}\nHôm nay là {datetime.now(PACIFIC_TZ):%d/%m/%Y}."
    else:
        system = system_prompt
    if ocr:
        ocr_instruction = cfg.get("ocr_instruction") or ""
        system = f"{ocr_instruction}\n\n{system}"
    return system


def _build_reply_tail(
    *,
    model_label: str,
    elapsed: float,
    use_web: bool,
    thinking_used: Optional[str],
    ocr: bool,
    parsed: ParsedResponse,
    turns: int,
    acfg: dict[str, Any],
    notes: list[str],
) -> str:
    """Tạo dòng chân trang chứa thông tin model, thời gian phản hồi, nguồn tra cứu và cảnh báo."""
    meta = [f"🤖 {model_label}", f"⏱ {elapsed:.1f}s"]
    meta += [
        label
        for label, active in (
            ("🌐 web", use_web),
            (f"🧠 {thinking_used}", bool(thinking_used)),
            ("🔎 OCR", ocr),
        )
        if active
    ]
    if parsed.tokens:
        meta.append(f"{parsed.tokens:,} tokens")
    if turns:
        meta.append("🆕 phiên mới" if turns == 1 else f"💬 phiên: {turns} lượt")

    footer = "-# " + " · ".join(meta)
    extras: list[str] = []
    if acfg.get("show_sources", True):
        if parsed.queries:
            extras.append("-# 🔎 Đã tìm: " + "; ".join(query[:60] for query in parsed.queries[:3]))
        if parsed.sources:
            links = " · ".join(f"[{clean_title(title)}](<{uri}>)" for title, uri in parsed.sources[:4])
            extras.append("-# 📚 Nguồn: " + links)
    extras += [f"-# ⚠️ {note}" for note in notes]
    return "\n".join([footer] + extras)


async def _send_reply(
    interaction: discord.Interaction,
    *,
    parsed: ParsedResponse,
    tail: str,
    acfg: dict[str, Any],
) -> None:
    """Gửi Embed suy nghĩ (nếu có) và chia đoạn văn bản trả lời (hoặc xuất file `answer.md` nếu quá dài)."""
    if parsed.thoughts and acfg.get("show_thoughts_embed", True):
        embed = discord.Embed(
            title="🧠 Tóm tắt quá trình suy nghĩ",
            description=parsed.thoughts[:3500] + ("…" if len(parsed.thoughts) > 3500 else ""),
            color=discord.Color.dark_grey(),
        )
        await interaction.followup.send(embed=embed, allowed_mentions=NO_MENTIONS)

    chunk_limit = int(acfg.get("chunk_char_limit", 1900))
    max_chunks = int(acfg.get("max_reply_chunks", 6))
    files: list[discord.File] = []
    chunks = chunk_text(parsed.text, limit=chunk_limit)
    if len(chunks) > max_chunks:
        files.append(discord.File(io.BytesIO(parsed.text.encode()), filename="answer.md"))
        chunks = [chunks[0] + "\n…\n*(Câu trả lời dài — xem đầy đủ trong file `answer.md`)*"]
    if chunks and len(chunks[-1]) + len(tail) + 1 <= 2000:
        chunks[-1] += "\n" + tail
    else:
        chunks.append(tail)

    for idx, content in enumerate(chunks):
        is_last = idx == len(chunks) - 1
        await interaction.followup.send(
            content=content,
            files=files if is_last else [],
            allowed_mentions=NO_MENTIONS,
        )


def register_chat_commands(
    tree: app_commands.CommandTree,
    *,
    config_repo: ConfigRepository,
    model_registry: ModelRegistry,
    model_cooldowns: ModelCooldownTracker,
    gemini_service: GeminiChatService,
    session_repo: SessionRepository,
    usage_stats: UsageStats,
    user_cooldowns: UserCooldownTracker,
    user_locks: UserLockManager,
    gemini_semaphore: DynamicSemaphore,
) -> None:
    """Đăng ký lệnh `/ask` cùng autocomplete cho tùy chọn `model` lên cây lệnh Discord."""

    @tree.command(name="ask", description="Hỏi AI Gemini: OCR, suy nghĩ sâu, tra cứu web")
    @app_commands.describe(
        prompt="Câu hỏi / yêu cầu của bạn",
        file="Ảnh hoặc PDF đính kèm để hỏi / OCR",
        model="Chọn model; bỏ trống = tự động (theo chuỗi cấu hình trên Dashboard)",
        web="Tra cứu thông tin mới trên web (Google Search)",
        thinking_level="Suy nghĩ sâu: auto / low / medium / high / xhigh",
        ocr="Chỉ trích xuất chữ từ file đính kèm (OCR)",
    )
    async def ask_command(
        interaction: discord.Interaction,
        prompt: app_commands.Range[str, 1, 4000],
        file: Optional[discord.Attachment] = None,
        model: Optional[str] = None,
        web: Optional[bool] = None,
        thinking_level: Optional[Literal["auto", "low", "medium", "high", "xhigh"]] = None,
        ocr: bool = False,
    ):
        cfg = await config_repo.get_config()
        acfg = cfg.get("ask", {})
        scfg = cfg.get("session", {})

        uid = interaction.user.id
        user_cooldown = float(acfg.get("user_cooldown", 5.0))
        wait = user_cooldowns.remaining_seconds(uid, user_cooldown)
        if wait > 0:
            return await interaction.response.send_message(
                f"⏳ Chậm lại chút nhé, thử lại sau {wait:.0f}s.",
                ephemeral=True,
            )

        # Kiểm tra các công tắc tính năng cấu hình trên Dashboard
        use_web = bool(acfg.get("default_web_search", False)) if web is None else bool(web)
        eff_thinking = thinking_level if thinking_level is not None else (acfg.get("default_thinking_level") or None)

        if file and not acfg.get("allow_file", True):
            return await interaction.response.send_message("🚫 Tính năng đính kèm ảnh/PDF hiện đang tắt.", ephemeral=True)
        if ocr and not acfg.get("allow_ocr", True):
            return await interaction.response.send_message("🚫 Tính năng OCR hiện đang tắt.", ephemeral=True)
        if ocr and not file:
            return await interaction.response.send_message(
                "❌ OCR cần đính kèm file ảnh/PDF vào option `file`.",
                ephemeral=True,
            )
        if use_web and not acfg.get("allow_web_search", True):
            if web is True:
                return await interaction.response.send_message("🚫 Tính năng tra cứu web hiện đang tắt.", ephemeral=True)
            use_web = False
        if eff_thinking and not acfg.get("allow_thinking", True):
            if thinking_level is not None:
                return await interaction.response.send_message("🚫 Tính năng suy nghĩ sâu hiện đang tắt.", ephemeral=True)
            eff_thinking = None

        user_cooldowns.mark_used(uid)
        await interaction.response.defer(thinking=True)
        started = time.perf_counter()
        notes: list[str] = []
        features = ["chat"] + [
            name
            for name, active in (("web", use_web), ("think", bool(eff_thinking)), ("ocr", ocr), ("file", bool(file)))
            if active
        ]

        # Chọn model hoặc chuỗi Auto
        is_auto = not model or model.strip().lower() == "auto"
        chain = model_registry.auto_chain() if is_auto else [model.strip()]
        model_id = chain[0]

        lock = user_locks.get(uid)
        await lock.acquire()
        try:
            user_parts: list = []
            if file:
                max_file_mb = float(acfg.get("max_file_mb", 10.0))
                user_parts.append(await read_attachment(file, max_file_mb))
            user_parts.append(types.Part(text=prompt))

            # Nạp ngữ cảnh phiên hội thoại của người dùng
            session_doc = await session_repo.load(uid) if session_repo.enabled else None
            can_save = session_repo.enabled and session_repo.ok
            if session_repo.enabled and not session_repo.ok:
                notes.append("Không kết nối được bộ nhớ phiên — lần trả lời này không có ngữ cảnh cũ")

            history = history_to_contents(
                session_doc,
                max_history_messages=int(scfg.get("max_history_messages", 20)),
                max_history_chars=int(scfg.get("max_history_chars", 24000)),
            )
            contents = history + [types.Content(role="user", parts=user_parts)]
            system_instruction = _build_system_instruction(cfg, ocr=ocr)

            errors: dict[str, str] = {}
            async with gemini_semaphore:
                if is_auto:
                    resp, thinking_used, model_id, errors = await gemini_service.call_with_fallback(
                        chain,
                        contents,
                        system=system_instruction,
                        thinking_level=eff_thinking,
                        web_search=use_web,
                    )
                else:
                    try:
                        resp, thinking_used = await gemini_service.call_model(
                            model_id,
                            contents,
                            system=system_instruction,
                            thinking_level=eff_thinking,
                            web_search=use_web,
                        )
                        model_cooldowns.clear_model(model_id)
                    except (genai_errors.APIError, asyncio.TimeoutError) as exc:
                        model_cooldowns.mark_failure(model_id, exc)
                        raise

            parsed = parse_response(resp)
            if is_auto and model_id in chain:
                failed = [f"`{mid}` ({errors[mid]})" for mid in chain[: chain.index(model_id)] if mid in errors]
                if failed:
                    notes.append("Tự động chuyển model: " + ", ".join(failed) + f" → `{model_id}`")
            if eff_thinking and not thinking_used:
                notes.append(f"Model `{model_id}` không hỗ trợ `thinking_level` ({eff_thinking}) nên đã bỏ qua")
            elif eff_thinking and thinking_used != eff_thinking:
                notes.append(f"Model `{model_id}` không có mức `{eff_thinking}`, đã dùng mức `{thinking_used}`")

            if not parsed.text:
                reason = f"bị chặn ({parsed.blocked})" if parsed.blocked else f"finish_reason={parsed.finish}"
                raise UserError(f"Model không trả về nội dung — {reason}. Hãy thử diễn đạt lại.")

            # Lưu lượt hội thoại vào phiên và kho lưu trữ vĩnh viễn
            turns = 0
            if can_save:
                stored_user = ("[OCR] " if ocr else "") + (f"[Đính kèm: {file.filename}] " if file else "") + prompt
                turns = await session_repo.save_turn(uid, stored_user, parsed.text, session_doc, model_id) or 0
                if not turns:
                    notes.append("Không lưu được phiên/lưu trữ (lỗi MongoDB) — tin nhắn này sẽ không được nhớ")

            tail = _build_reply_tail(
                model_label=model_registry.display_name_for(model_id),
                elapsed=time.perf_counter() - started,
                use_web=use_web,
                thinking_used=thinking_used,
                ocr=ocr,
                parsed=parsed,
                turns=turns,
                acfg=acfg,
                notes=notes,
            )
            await _send_reply(interaction, parsed=parsed, tail=tail, acfg=acfg)
            usage_stats.record(model_id, True, features)

        except Exception as exc:
            usage_stats.record(model_id, False, features, f"{type(exc).__name__}: {exc}"[:200])
            if not isinstance(exc, UserError):
                log.exception("Lỗi /ask")
            await interaction.followup.send(explain_error(exc), allowed_mentions=NO_MENTIONS)
        finally:
            lock.release()

    @ask_command.autocomplete("model")
    async def model_autocomplete(_interaction: discord.Interaction, current: str):
        await config_repo.get_config()
        query = current.lower().strip()
        choices: list[app_commands.Choice[str]] = []
        if not query or any(query in keyword for keyword in ("auto", "tự động", "tu dong")):
            chain_preview = " → ".join(model_registry.auto_chain()[:3])
            choices.append(app_commands.Choice(name=f"🤖 Tự động ({chain_preview})"[:100], value="auto"))
        for info in model_registry.choices():
            if query in info.id.lower() or query in info.display.lower():
                label = f"{info.badge} {info.display}" if info.display != info.id else f"{info.badge} {info.id}"
                choices.append(app_commands.Choice(name=label[:100], value=info.id))
            if len(choices) >= 25:
                break
        return choices
