"""
Lucky X Beats - All-in-One Telegram Voice-Chat Music Bot
Single-file consolidated distribution for easy deployment.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import functools
import html
import json
import logging
import os
from pathlib import Path
import platform
import re
import signal
import sys
import time
from typing import Any, Awaitable, Callable, Dict, FrozenSet, List, Mapping, NamedTuple, Optional, Set, Tuple
import uuid

from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.errors import (
    ChannelInvalid,
    ChannelPrivate,
    ChatAdminRequired,
    FloodWait,
    InviteRequestSent,
    MessageNotModified,
    PeerIdInvalid,
    RPCError,
    UserAlreadyParticipant,
    UserNotParticipant,
)
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pytgcalls import PyTgCalls, filters as call_filters
import yt_dlp

# ==============================================================================
# 0. METADATA & CONSTANTS
# ==============================================================================
__version__ = "1.0.0"
BOT_NAME = "Lucky X Beats"
CREATOR = "Swapnil OG"

WATCHDOG_INTERVAL = 30
WATCHDOG_MAX_MISSES = 4
SHUTDOWN_STEP_TIMEOUT = 10.0

SEARCH_PREFIXES: Dict[str, str] = {
    "ytsearch": "ytsearch",
    "scsearch": "scsearch",
    "spsearch": "ytsearch",
}
VALID_SEARCH_PREFIXES: FrozenSet[str] = frozenset(SEARCH_PREFIXES.keys())

# ==============================================================================
# 1. ERRORS & MODELS
# ==============================================================================
class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""

class MediaError(Exception):
    """A media lookup/validation failure whose message is safe to show to users."""

class PlaybackError(Exception):
    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal

class NothingPlayingError(PlaybackError):
    def __init__(self, message: str = "Nothing is playing right now.") -> None:
        super().__init__(message)

class QueueFullError(Exception):
    pass

class QueueIndexError(IndexError):
    pass

class LoopMode:
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"

@dataclass
class Track:
    title: str
    webpage_url: str
    duration: Optional[int]
    requested_by_id: int
    requested_by_name: str
    source: str = "search"
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:6])
    stream_url: Optional[str] = field(default=None, repr=False, compare=False)
    stream_headers: Optional[Dict[str, str]] = field(default=None, repr=False, compare=False)
    resolved_at: float = field(default=0.0, repr=False, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "webpage_url": self.webpage_url,
            "duration": self.duration,
            "requested_by_id": self.requested_by_id,
            "requested_by_name": self.requested_by_name,
            "source": self.source,
            "uid": self.uid,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Track:
        try:
            duration = data.get("duration")
            return cls(
                title=str(data["title"]),
                webpage_url=str(data["webpage_url"]),
                duration=int(duration) if duration is not None else None,
                requested_by_id=int(data["requested_by_id"]),
                requested_by_name=str(data.get("requested_by_name", "Unknown")),
                source=str(data.get("source", "search")),
                uid=str(data.get("uid") or uuid.uuid4().hex[:6]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid track record: {exc}") from exc

# ==============================================================================
# 2. CONFIGURATION & STATE STORE
# ==============================================================================
def _int_env(env: Mapping[str, str], key: str, default: int, *, minimum: int = 1) -> int:
    val = env.get(key)
    if val is None or not val.strip():
        return default
    try:
        parsed = int(val.strip())
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got: {val!r}") from exc
    if parsed < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got: {parsed}")
    return parsed

def _bool_env(env: Mapping[str, str], key: str, default: bool) -> bool:
    val = env.get(key)
    if val is None or not val.strip():
        return default
    cleaned = val.strip().lower()
    if cleaned in {"1", "true", "yes", "on"}:
        return True
    if cleaned in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be a boolean (1/0, true/false), got: {val!r}")

def _id_list_env(env: Mapping[str, str], key: str) -> FrozenSet[int]:
    val = env.get(key, "").strip()
    if not val:
        return frozenset()
    ids: Set[int] = set()
    for raw in val.replace(",", " ").split():
        try:
            ids.add(int(raw))
        except ValueError as exc:
            raise ConfigError(f"{key} contains invalid user ID: {raw!r}") from exc
    return frozenset(ids)

@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    session_string: str
    owner_id: int
    admin_ids: FrozenSet[int] = field(default_factory=frozenset)
    queue_limit: int = 50
    default_search: str = "ytsearch"
    max_duration_seconds: int = 3600
    cache_ttl_seconds: int = 1800
    state_file: str = "data/lucky_x_beats_state.json"
    log_level: str = "INFO"
    auto_leave_empty: bool = True
    empty_leave_delay: int = 60
    stream_preload_seconds: int = 20

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> Settings:
        src = os.environ if env is None else env
        missing = [k for k in ("API_ID", "API_HASH", "BOT_TOKEN", "SESSION_STRING", "OWNER_ID") if not (src.get(k) or "").strip()]
        if missing:
            raise ConfigError(f"Missing required environment variables: {', '.join(missing)}")
        try:
            api_id = int(src["API_ID"].strip())
            owner_id = int(src["OWNER_ID"].strip())
        except ValueError as exc:
            raise ConfigError(f"API_ID and OWNER_ID must be valid integers: {exc}") from exc

        default_search = src.get("DEFAULT_SEARCH", "ytsearch").strip().lower()
        if default_search not in VALID_SEARCH_PREFIXES:
            raise ConfigError(f"DEFAULT_SEARCH={default_search!r} invalid. Must be one of: {sorted(VALID_SEARCH_PREFIXES)}")

        return cls(
            api_id=api_id,
            api_hash=src["API_HASH"].strip(),
            bot_token=src["BOT_TOKEN"].strip(),
            session_string=src["SESSION_STRING"].strip(),
            owner_id=owner_id,
            admin_ids=_id_list_env(src, "ADMIN_IDS"),
            queue_limit=_int_env(src, "QUEUE_LIMIT", 50, minimum=1),
            default_search=default_search,
            max_duration_seconds=_int_env(src, "MAX_DURATION_SECONDS", 3600, minimum=60),
            cache_ttl_seconds=_int_env(src, "CACHE_TTL_SECONDS", 1800, minimum=60),
            state_file=src.get("STATE_FILE", "data/lucky_x_beats_state.json").strip(),
            log_level=src.get("LOG_LEVEL", "INFO").strip().upper(),
            auto_leave_empty=_bool_env(src, "AUTO_LEAVE_EMPTY", True),
            empty_leave_delay=_int_env(src, "EMPTY_LEAVE_DELAY", 60, minimum=5),
            stream_preload_seconds=_int_env(src, "STREAM_PRELOAD_SECONDS", 20, minimum=0),
        )

class StateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.log = logging.getLogger(self.__class__.__name__)

    def save(self, raw_state: Dict[int, Dict[str, Any]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump({str(k): v for k, v in raw_state.items()}, f, indent=2)
            tmp.replace(self.path)
        except Exception as exc:
            self.log.warning("Could not persist state to %s: %s", self.path, exc)

    def load(self) -> Dict[int, Dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return {int(k): v for k, v in data.items() if str(k).lstrip("-").isdigit()}
        except Exception as exc:
            self.log.warning("State file corrupt or unreadable (%s), resetting: %s", self.path, exc)
            return {}

# ==============================================================================
# 3. QUEUE & STREAM HELPERS
# ==============================================================================
class GroupQueue:
    def __init__(self, max_size: int = 50) -> None:
        self.max_size = max_size
        self._tracks: deque[Track] = deque()
        self.current: Optional[Track] = None
        self.loop_mode: str = LoopMode.OFF

    def push(self, track: Track) -> int:
        if len(self._tracks) >= self.max_size:
            raise QueueFullError(f"Queue is full (limit is {self.max_size}).")
        self._tracks.append(track)
        return len(self._tracks)

    def push_next(self, track: Track) -> None:
        if len(self._tracks) >= self.max_size:
            raise QueueFullError(f"Queue is full (limit is {self.max_size}).")
        self._tracks.appendleft(track)

    def advance(self) -> Optional[Track]:
        if self.current and self.loop_mode == LoopMode.TRACK:
            return self.current
        if self.current and self.loop_mode == LoopMode.QUEUE:
            self._tracks.append(self.current)
        self.current = self._tracks.popleft() if self._tracks else None
        return self.current

    def clear(self) -> int:
        count = len(self._tracks)
        self._tracks.clear()
        return count

    def remove(self, index: int) -> Track:
        if not (1 <= index <= len(self._tracks)):
            raise QueueIndexError(f"Position #{index} out of range (1..{len(self._tracks)}).")
        t = self._tracks[index - 1]
        del self._tracks[index - 1]
        return t

    def list_tracks(self) -> List[Track]:
        return list(self._tracks)

    def serialize(self) -> Dict[str, Any]:
        return {
            "current": self.current.to_dict() if self.current else None,
            "tracks": [t.to_dict() for t in self._tracks],
            "loop_mode": self.loop_mode,
        }

    def restore(self, data: Dict[str, Any]) -> None:
        self.loop_mode = data.get("loop_mode", LoopMode.OFF)
        cur = data.get("current")
        self.current = Track.from_dict(cur) if cur else None
        self._tracks.clear()
        for item in data.get("tracks", []):
            try:
                self._tracks.append(Track.from_dict(item))
            except Exception:
                pass

def build_media_stream(track: Track, seek: int = 0) -> Any:
    from pytgcalls.types import AudioQuality, MediaStream
    kwargs = {
        "audio_parameters": AudioQuality.HIGH,
        "video_flags": MediaStream.Flags.IGNORE,
        "ffmpeg_parameters": f"-ss {int(seek)}" if seek > 0 else None,
    }
    if track.stream_headers:
        try:
            return MediaStream(track.stream_url, headers=track.stream_headers, **kwargs)
        except TypeError:
            pass
    return MediaStream(track.stream_url, **kwargs)

# ==============================================================================
# 4. TEXT FORMATTING & NOTIFICATIONS
# ==============================================================================
def fmt_duration(seconds: Optional[int]) -> str:
    if seconds is None or seconds <= 0:
        return "Live stream"
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    return f"{hours:02d}:{mins:02d}:{secs:02d}" if hours else f"{mins:02d}:{secs:02d}"

def render_now_playing(track: Track, elapsed: int = 0, is_paused: bool = False, loop_mode: str = LoopMode.OFF) -> str:
    title = html.escape(track.title)
    requester = html.escape(track.requested_by_name)
    status_icon = "⏸ Paused" if is_paused else "▶ Now Playing"
    loop_tag = f" | 🔁 Loop: {loop_mode.upper()}" if loop_mode != LoopMode.OFF else ""
    return (
        f"<b>{status_icon}</b>{loop_tag}\n\n"
        f"🎵 <b><a href=\"{track.webpage_url}\">{title}</a></b>\n"
        f"⏱ <code>{fmt_duration(elapsed)} / {fmt_duration(track.duration)}</code>\n"
        f"👤 Requested by: <b>{requester}</b>"
    )

def render_queue(queue: GroupQueue, page: int = 1, page_size: int = 10) -> str:
    tracks = queue.list_tracks()
    total_pages = max(1, (len(tracks) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * page_size
    slice_tracks = tracks[start_idx : start_idx + page_size]

    lines = [f"📋 <b>Queue for this chat (Page {page}/{total_pages})</b>\n"]
    if queue.current:
        lines.append(f"<b>Now:</b> 🎵 {html.escape(queue.current.title)}\n")
    if not slice_tracks:
        lines.append("<i>Queue is empty. Use /play to queue tracks!</i>")
    else:
        for i, t in enumerate(slice_tracks, start=start_idx + 1):
            lines.append(f"<b>{i}.</b> {html.escape(t.title)} (<code>{fmt_duration(t.duration)}</code>)")
    return "\n".join(lines)

def make_cb(action: str, group_id: int, extra: str = "") -> str:
    return f"{action}:{group_id}:{extra}" if extra else f"{action}:{group_id}"

class TelegramNotifier:
    def __init__(self, bot: Client) -> None:
        self.bot = bot
        self.log = logging.getLogger(self.__class__.__name__)

    async def send_now_playing(self, chat_id: int, text: str, group_id: int) -> Optional[int]:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⏸ Pause", callback_data=make_cb("pause", group_id)),
                InlineKeyboardButton("▶ Resume", callback_data=make_cb("resume", group_id)),
                InlineKeyboardButton("⏭ Skip", callback_data=make_cb("skip", group_id)),
            ],
            [
                InlineKeyboardButton("🔁 Loop", callback_data=make_cb("loop", group_id)),
                InlineKeyboardButton("⏹ Stop", callback_data=make_cb("stop", group_id)),
                InlineKeyboardButton("📋 Queue", callback_data=make_cb("queue", group_id, "1")),
            ]
        ])
        try:
            m = await self.bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)
            return m.id
        except Exception as exc:
            self.log.warning("Could not send now playing card: %s", exc)
            return None

    async def edit_card(self, chat_id: int, msg_id: int, text: str, group_id: int) -> None:
        try:
            await self.bot.edit_message_text(chat_id, msg_id, text, disable_web_page_preview=True)
        except MessageNotModified:
            pass
        except Exception as exc:
            self.log.debug("Edit card failed: %s", exc)

# ==============================================================================
# 5. ASSISTANT & PERMISSIONS
# ==============================================================================
class AssistantGate:
    def __init__(self, assistant: Client) -> None:
        self.assistant = assistant
        self.log = logging.getLogger(self.__class__.__name__)
        self._joined_cache: Set[int] = set()

    async def ensure_in_chat(self, chat_id: int, bot: Client) -> None:
        if chat_id in self._joined_cache:
            return
        try:
            me = await self.assistant.get_me()
            member = await self.assistant.get_chat_member(chat_id, me.id)
            if member.status not in (ChatMemberStatus.BANNED, ChatMemberStatus.LEFT):
                self._joined_cache.add(chat_id)
                return
        except UserNotParticipant:
            pass
        except Exception as exc:
            self.log.debug("Assistant membership check failed: %s", exc)

        try:
            link = await bot.export_chat_invite_link(chat_id)
            await self.assistant.join_chat(link)
            self._joined_cache.add(chat_id)
        except Exception as exc:
            raise PlaybackError(f"Assistant account could not join chat: {exc}") from exc

class Permissions:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def is_admin(self, client: Client, chat_id: int, user_id: int) -> bool:
        if user_id == self.settings.owner_id or user_id in self.settings.admin_ids:
            return True
        try:
            member = await client.get_chat_member(chat_id, user_id)
            return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
        except Exception:
            return False

# ==============================================================================
# 6. MEDIA RESOLVER (YT-DLP)
# ==============================================================================
class MediaResolver:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.log = logging.getLogger(self.__class__.__name__)
        self._ydl_opts = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "default_search": settings.default_search,
            "extract_flat": False,
        }

    async def resolve(self, query: str, requester_id: int, requester_name: str) -> Track:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._extract, query, requester_id, requester_name)

    def _extract(self, query: str, requester_id: int, requester_name: str) -> Track:
        with yt_dlp.YoutubeDL(self._ydl_opts) as ydl:
            try:
                info = ydl.extract_info(query, download=False)
            except Exception as exc:
                raise MediaError(f"Could not extract media info: {exc}") from exc

        if not info:
            raise MediaError("No results found for your query.")

        if "entries" in info and info["entries"]:
            info = info["entries"][0]

        duration = info.get("duration")
        if duration and duration > self.settings.max_duration_seconds:
            raise MediaError(f"Track is too long (>{self.settings.max_duration_seconds // 60}m).")

        return Track(
            title=info.get("title", "Unknown Title"),
            webpage_url=info.get("webpage_url") or info.get("original_url") or query,
            duration=duration,
            requested_by_id=requester_id,
            requested_by_name=requester_name,
            stream_url=info.get("url"),
            stream_headers=info.get("http_headers"),
            resolved_at=time.time(),
        )

# ==============================================================================
# 7. PLAYBACK ENGINE
# ==============================================================================
class PlaybackEngine:
    def __init__(
        self,
        settings: Settings,
        calls: PyTgCalls,
        notifier: TelegramNotifier,
        assistant: AssistantGate,
        resolver: MediaResolver,
        store: StateStore,
    ) -> None:
        self.settings = settings
        self.calls = calls
        self.notifier = notifier
        self.assistant = assistant
        self.resolver = resolver
        self.store = store
        self.queues: Dict[int, GroupQueue] = {}
        self.locks: Dict[int, asyncio.Lock] = {}
        self.active_cards: Dict[int, int] = {}
        self.is_paused: Dict[int, bool] = {}
        self.start_times: Dict[int, float] = {}
        self.log = logging.getLogger(self.__class__.__name__)

    def _get_lock(self, group_id: int) -> asyncio.Lock:
        if group_id not in self.locks:
            self.locks[group_id] = asyncio.Lock()
        return self.locks[group_id]

    def _get_queue(self, group_id: int) -> GroupQueue:
        if group_id not in self.queues:
            self.queues[group_id] = GroupQueue(max_size=self.settings.queue_limit)
        return self.queues[group_id]

    async def play(self, chat_id: int, track: Track, bot: Client) -> Tuple[bool, int]:
        lock = self._get_lock(chat_id)
        async with lock:
            queue = self._get_queue(chat_id)
            if not queue.current:
                queue.current = track
                await self._start_playback(chat_id, track, bot)
                return True, 0
            else:
                pos = queue.push(track)
                return False, pos

    async def _start_playback(self, chat_id: int, track: Track, bot: Client) -> None:
        await self.assistant.ensure_in_chat(chat_id, bot)
        if not track.stream_url or (time.time() - track.resolved_at > self.settings.cache_ttl_seconds):
            fresh = await self.resolver.resolve(track.webpage_url, track.requested_by_id, track.requested_by_name)
            track.stream_url = fresh.stream_url
            track.stream_headers = fresh.stream_headers
            track.resolved_at = time.time()

        stream = build_media_stream(track)
        try:
            await self.calls.play(chat_id, stream)
        except Exception:
            try:
                await self.calls.change_stream(chat_id, stream)
            except Exception as exc:
                raise PlaybackError(f"Call streaming failed: {exc}") from exc

        self.start_times[chat_id] = time.time()
        self.is_paused[chat_id] = False
        text = render_now_playing(track, loop_mode=self._get_queue(chat_id).loop_mode)
        msg_id = await self.notifier.send_now_playing(chat_id, text, chat_id)
        if msg_id:
            self.active_cards[chat_id] = msg_id

    async def pause(self, chat_id: int) -> None:
        async with self._get_lock(chat_id):
            await self.calls.pause(chat_id)
            self.is_paused[chat_id] = True

    async def resume(self, chat_id: int) -> None:
        async with self._get_lock(chat_id):
            await self.calls.resume(chat_id)
            self.is_paused[chat_id] = False

    async def skip(self, chat_id: int, bot: Client) -> Optional[Track]:
        async with self._get_lock(chat_id):
            queue = self._get_queue(chat_id)
            nxt = queue.advance()
            if nxt:
                await self._start_playback(chat_id, nxt, bot)
            else:
                await self.calls.leave_call(chat_id)
                self.active_cards.pop(chat_id, None)
            return nxt

    async def stop(self, chat_id: int) -> None:
        async with self._get_lock(chat_id):
            queue = self._get_queue(chat_id)
            queue.clear()
            queue.current = None
            try:
                await self.calls.leave_call(chat_id)
            except Exception:
                pass
            self.active_cards.pop(chat_id, None)

# ==============================================================================
# 8. HANDLERS
# ==============================================================================
BOT_COMMANDS = [
    BotCommand("play", "Play audio or queue a search term / URL"),
    BotCommand("pause", "Pause current playback"),
    BotCommand("resume", "Resume paused playback"),
    BotCommand("skip", "Skip current track"),
    BotCommand("stop", "Stop playback and clear queue"),
    BotCommand("queue", "Show the current queue"),
    BotCommand("help", "Show help and commands"),
]

def register_handlers(bot: Client, engine: PlaybackEngine, permissions: Permissions) -> None:
    @bot.on_message(filters.command(["start", "help"]))
    async def cmd_start(_, message: Message):
        text = (
            f"🎵 <b>Welcome to {BOT_NAME}!</b>\n\n"
            f"Commands:\n"
            f"• <code>/play &lt;query/url&gt;</code> - Stream audio in voice chat\n"
            f"• <code>/pause</code> | <code>/resume</code> - Control playback\n"
            f"• <code>/skip</code> - Next track\n"
            f"• <code>/stop</code> - Stop and leave\n"
            f"• <code>/queue</code> - View track queue\n\n"
            f"<i>Powered by {CREATOR}</i>"
        )
        await message.reply(text, disable_web_page_preview=True)

    @bot.on_message(filters.command(["play"]) & filters.group)
    async def cmd_play(client: Client, message: Message):
        query = message.text.partition(" ")[2].strip()
        if not query and message.reply_to_message:
            query = message.reply_to_message.text or ""
        if not query:
            return await message.reply("❗ Please provide a search term or audio URL. Example: <code>/play Faded</code>")

        status_msg = await message.reply("🔍 Searching & resolving track...")
        try:
            req_name = message.from_user.first_name if message.from_user else "Someone"
            req_id = message.from_user.id if message.from_user else 0
            track = await engine.resolver.resolve(query, req_id, req_name)
            started, pos = await engine.play(message.chat.id, track, client)
            if started:
                await status_msg.delete()
            else:
                await status_msg.edit(f"➕ Queued at position #{pos}: <b>{html.escape(track.title)}</b>")
        except Exception as exc:
            await status_msg.edit(f"❌ Error: {exc}")

    @bot.on_message(filters.command(["pause"]) & filters.group)
    async def cmd_pause(_, message: Message):
        try:
            await engine.pause(message.chat.id)
            await message.reply("⏸ Playback paused.")
        except Exception as exc:
            await message.reply(f"❌ {exc}")

    @bot.on_message(filters.command(["resume"]) & filters.group)
    async def cmd_resume(_, message: Message):
        try:
            await engine.resume(message.chat.id)
            await message.reply("▶ Playback resumed.")
        except Exception as exc:
            await message.reply(f"❌ {exc}")

    @bot.on_message(filters.command(["skip"]) & filters.group)
    async def cmd_skip(client: Client, message: Message):
        try:
            nxt = await engine.skip(message.chat.id, client)
            if nxt:
                await message.reply(f"⏭ Skipped to: <b>{html.escape(nxt.title)}</b>")
            else:
                await message.reply("⏹ Queue finished. Left voice chat.")
        except Exception as exc:
            await message.reply(f"❌ {exc}")

    @bot.on_message(filters.command(["stop"]) & filters.group)
    async def cmd_stop(_, message: Message):
        try:
            await engine.stop(message.chat.id)
            await message.reply("⏹ Playback stopped, queue cleared.")
        except Exception as exc:
            await message.reply(f"❌ {exc}")

    @bot.on_message(filters.command(["queue"]) & filters.group)
    async def cmd_queue(_, message: Message):
        q = engine._get_queue(message.chat.id)
        await message.reply(render_queue(q, page=1), disable_web_page_preview=True)

    @bot.on_callback_query()
    async def on_callback(client: Client, query: CallbackQuery):
        data = query.data or ""
        parts = data.split(":")
        if len(parts) < 2:
            return await query.answer()

        action = parts[0]
        group_id = int(parts[1])

        if not await permissions.is_admin(client, group_id, query.from_user.id):
            return await query.answer("⚠️ Only admins can control playback buttons.", show_alert=True)

        try:
            if action == "pause":
                await engine.pause(group_id)
                await query.answer("Paused")
            elif action == "resume":
                await engine.resume(group_id)
                await query.answer("Resumed")
            elif action == "skip":
                await engine.skip(group_id, client)
                await query.answer("Skipped")
            elif action == "stop":
                await engine.stop(group_id)
                await query.answer("Stopped")
            elif action == "loop":
                q = engine._get_queue(group_id)
                q.loop_mode = LoopMode.TRACK if q.loop_mode == LoopMode.OFF else LoopMode.OFF
                await query.answer(f"Loop set to {q.loop_mode.upper()}")
            elif action == "queue":
                page = int(parts[2]) if len(parts) > 2 else 1
                q = engine._get_queue(group_id)
                await query.message.reply(render_queue(q, page=page), disable_web_page_preview=True)
                await query.answer()
        except Exception as exc:
            await query.answer(f"Error: {exc}", show_alert=True)

# ==============================================================================
# 9. RUNNER & WATCHDOG
# ==============================================================================
async def run(settings: Settings) -> int:
    bot = Client(
        "bot_session",
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        bot_token=settings.bot_token,
        parse_mode=ParseMode.HTML,
    )
    user = Client(
        "assistant_session",
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        session_string=settings.session_string,
        parse_mode=ParseMode.HTML,
    )
    calls = PyTgCalls(user)
    notifier = TelegramNotifier(bot)
    assistant = AssistantGate(user)
    resolver = MediaResolver(settings)
    store = StateStore(settings.state_file)
    permissions = Permissions(settings)

    engine = PlaybackEngine(settings, calls, notifier, assistant, resolver, store)
    register_handlers(bot, engine, permissions)

    @calls.on_update()
    async def on_call_update(_, update: Any):
        # Auto advance when track finishes streaming
        pass

    logging.info("Starting Pyrogram bot and user client...")
    await bot.start()
    await user.start()
    await calls.start()
    try:
        await bot.set_bot_commands(BOT_COMMANDS)
    except Exception as exc:
        logging.warning("Failed to set bot commands: %s", exc)

    logging.info(f"{BOT_NAME} v{__version__} successfully initialized and running!")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await stop_event.wait()

    logging.info("Shutting down bot safely...")
    for chat_id in list(engine.queues.keys()):
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
    await calls.stop()
    await bot.stop()
    await user.stop()
    return 0

def main() -> int:
    load_dotenv()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"[{BOT_NAME}] Configuration Error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
    )
    for noisy in ("pyrogram", "asyncio", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        return asyncio.run(run(settings))
    except KeyboardInterrupt:
        return 0

if __name__ == "__main__":
    sys.exit(main())
