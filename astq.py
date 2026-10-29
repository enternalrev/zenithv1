#!/usr/bin/env python3
"""
Telegram multi-bot control panel — operator console.

    ┌──────────────────────────────────────────────────────────┐
    │  main bot        polls Telegram, owns the command surface│
    │  worker bots     driven by main, never poll independently│
    │  rotation        flooded workers cool, others take over  │
    │  flood control   chat-kind gate + 8s cooldown cap        │
    │  multi-GC        chats isolated end to end               │
    │  health          HTTP on 0.0.0.0:$PORT for Render        │
    │  persistence     sons.json beside the script             │
    └──────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterable

from telegram import Bot, ReactionTypeEmoji, Update
from telegram.error import (
    BadRequest,
    Forbidden,
    RetryAfter,
    TelegramError,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ═════════════════════════════════════════════════════════════════════════════
# Secrets — fill these in
# ═════════════════════════════════════════════════════════════════════════════

OWNER_ID: int = 8815953523

MAIN_BOT_TOKEN: str = "8725085217:AAGEwRDvuWTF5o6R1L-vHL0dPtobor4AubY"

WORKER_BOT_TOKENS: list[str] = [
    "8983949431:AAGDfgGNcLeFjTtCx0YMpFEDBXIP5kpKeiM",
    "8777168647:AAGXwsjZ5HeHEVuiFyK934gyTWxOUidZvDI",
    "8920890264:AAFlZcsdPkLnIf9HsDs0q6YEZ4MLYAI9CS4",
    "9036368499:AAGN-tMUSizARstoEx11LDkXCkMfIAa82K4",
    "9185845966:AAG6W7BgPgiI55JtjYTgKIsHugdmwckYTws",
    "9246972548:AAGve1ia_stSftEThtNRIGLLFLQiZx0i5Xw",
    "9210068789:AAHIerATpEgrAEYpO7LgeBytmSUkW7e296k",
    "9292310503:AAEsjnustCM_IakqsLUZBy45bRezL8AmYFw",
    "9286146038:AAFWBOMlbgxaGkyh_PQau_3CZ2Ai_hsW2xA",
]


# ═════════════════════════════════════════════════════════════════════════════
# Logging
# ═════════════════════════════════════════════════════════════════════════════

class _Formatter(logging.Formatter):
    COLORS = {
        logging.DEBUG:    "\x1b[38;5;244m",
        logging.INFO:     "\x1b[38;5;81m",
        logging.WARNING:  "\x1b[38;5;214m",
        logging.ERROR:    "\x1b[38;5;203m",
        logging.CRITICAL: "\x1b[38;5;199m",
    }
    RESET = "\x1b[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelno, "")
        tag = f"{color}{record.levelname:<8}{self.RESET}"
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        return f"{ts}  {tag}  {record.getMessage()}"


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_Formatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("lunar.console")


# ═════════════════════════════════════════════════════════════════════════════
# Delays
# ═════════════════════════════════════════════════════════════════════════════

MIN_DELAY = 0.1
MAX_DELAY = 60.0
DEF_DELAY = 0.1


@dataclass
class Delays:
    replyraid: float = DEF_DELAY
    swipe: float = DEF_DELAY
    gcnc: float = DEF_DELAY
    ns: float = 1.2          # NS is heavy — slower per-worker floor by default

    def get(self, kind: str) -> float:
        return getattr(self, kind, DEF_DELAY)

    def set(self, kind: str, value: float) -> float:
        clamped = max(MIN_DELAY, min(MAX_DELAY, value))
        setattr(self, kind, clamped)
        return clamped

    def all_fastest(self) -> None:
        for k in ("replyraid", "swipe", "gcnc", "ns"):
            setattr(self, k, MIN_DELAY)


# ═════════════════════════════════════════════════════════════════════════════
# Config
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Config:
    owner_id: int
    main_bot_token: str
    worker_tokens: tuple[str, ...]
    sons_path: Path = Path("sons.json")
    delays: Delays = field(default_factory=Delays)
    max_backoff: float = 30.0
    flood_cap: float = 8.0           # hard cap on any 429 cooldown (recovery)
    chat_flood_pause: float = 1.0    # pause per chat-kind after a 429
    msg_per_minute: int = 3000
    title_per_minute: int = 600
    react_cooldown: float = 0.4
    use_paid_broadcast: bool = False

    @classmethod
    def load(cls) -> "Config":
        if not MAIN_BOT_TOKEN:
            raise SystemExit("MAIN_BOT_TOKEN is empty")
        if not WORKER_BOT_TOKENS:
            raise SystemExit("WORKER_BOT_TOKENS is empty")
        if not OWNER_ID:
            raise SystemExit("OWNER_ID is 0")

        delays = Delays()
        msg_pm = 3000
        title_pm = 600
        react_cd = 0.4
        paid = False

        cfg_file = Path("config.json")
        if cfg_file.exists():
            try:
                with cfg_file.open() as fh:
                    raw = json.load(fh)
                for k in ("replyraid", "swipe", "gcnc", "ns"):
                    if k in raw.get("delays", {}):
                        delays.set(k, float(raw["delays"][k]))
                msg_pm = int(raw.get("msg_per_minute", msg_pm))
                title_pm = int(raw.get("title_per_minute", title_pm))
                react_cd = float(raw.get("react_cooldown", react_cd))
                paid = bool(raw.get("use_paid_broadcast", paid))
            except Exception as exc:                   # noqa: BLE001
                log.warning("config.json ignored: %s", exc)

        return cls(
            owner_id=OWNER_ID,
            main_bot_token=MAIN_BOT_TOKEN,
            worker_tokens=tuple(WORKER_BOT_TOKENS),
            delays=delays,
            msg_per_minute=msg_pm,
            title_per_minute=title_pm,
            react_cooldown=react_cd,
            use_paid_broadcast=paid,
        )


# ═════════════════════════════════════════════════════════════════════════════
# Font styling — applied only to names/targets
# ═════════════════════════════════════════════════════════════════════════════

_FONT: dict[str, str] = {
    'a': 'ꪖ', 'b': '᥇', 'c': 'ᥴ', 'd': 'ᦔ', 'e': 'ꫀ', 'f': 'ᠻ',
    'g': 'ᧁ', 'h': 'ꫝ', 'i': '꠸', 'j': '꠹', 'k': 'ᛕ', 'l': 'ꪶ',
    'm': 'ꪑ', 'n': 'ꪀ', 'o': 'ꪮ', 'p': 'ρ', 'q': 'ꪇ', 'r': '᥅',
    's': 'ᦓ', 't': 'ꪻ', 'u': 'ꪊ', 'v': 'ꪜ', 'w': '᭙', 'x': '᥊',
    'y': 'ꪗ', 'z': 'ƺ',
}


def S(text: str) -> str:
    return ''.join(_FONT.get(c.lower(), c) for c in text)


_PAYLOADS: tuple[str, ...] = (
    "AGAR ME APKI माँ KO NAMAZ PADHTE WAQT CHODUN TO CYA VO SUNNAT HOGA?🤪",
    "Teri mummy ko subah 5 baje uthake morning walk pe lejake break fast karake nahLake dholake lunch karake din me sulake raat me utha ke candle light date dinner krke phir Ghar pe lake dheere dheere pom pom krte krte chodunga 🤪🤏🏿 entry 🥵🥵🥵",
    "𝙏𝙚𝙧𝙞 𝙢𝙖 𝙠𝙞 𝙘𝙝𝙪𝙩 𝙆𝙖 𝙗𝙞𝙧𝙮𝙖𝙣𝙞 𝙥𝙠𝙖 𝙠𝙚 𝙠𝙝𝙖 𝙟𝙖𝙪𝙣𝙜𝙖😁",
    "𝙏𝙐 𝙏𝙊𝙃 𝙈𝙀𝙍𝘼 𝙒𝙊𝙃𝙄 𝙏𝘼𝙏𝙏𝘼 𝙃𝘼𝙄 𝙉𝘼 ? 𝙅𝙄𝙎𝙈𝙀 𝙍𝙊𝙕 𝙆𝙃𝙐𝙅𝙇𝙄 𝙃𝙊𝙏𝙄🤣",
    "𝙆𝙞𝙩𝙣𝙖 𝙘𝙝𝙪𝙙𝙚𝙜𝙖 𝘼𝙗 𝙩𝙚𝙧𝙞 𝙢𝙖 𝙠𝙤 𝙗𝙝𝙞 𝙢𝙤𝙠𝙖 𝙙𝙚 𝙘𝙝𝙪𝙙𝙣𝙚 𝙠𝙖🤗",
    "Oye dost Teri randi maa ka 👙 aur 300 rupey reh gaye ake le ja Aur teri randi maa v yahi he Konsi maa Dharti maa Fatima Teri Ghar wali maa Ya teri dadi ki maa?",
    "Ye dekh Tmkc me camel 🐪",
    "Oho funny 🤣😂🤣😂🤣😂🤣😂🤣😂🤣😂🤣🤣😂🤣😂🤣😂🤣😂🤣😂🤣😂🤣😂🤣😂🤣😂🤣😂🤣😂🤣😂🤣 Ab trima chudegi😱",
    "Baune trima cute RND he tri behen ko to slave banake rkha he mene itna attitude kese aara tujhme😂😂😂😂😂💔😂😂💔😂💔😂💔😂💔😂💔💔😂🥀🥀🥀",
    "Apki mom itni RND kese he bina pese lie chud jaati😏🥀💔",
    "Trima pe atomic bomb gir jae jo tre sarr pe pankha he na vo tut ke tre goto pe gire jo tre ghar pe cylinder he na tre ma jab uspe khana banari ho tb vi fatt jae jo tra nall he na vo leak hojae or tra ghar bhass jae mc",
    "ＴＲＩＭＡ　ＣＨＵＤＧＹＩ　ＲＥ😜💜        ほソがん       ˖ ᡭ𐭩 ⊹ ࣪  ౨ৎ˚₊",
    "Beta lund chus bada ho pese duga 😏🥀",
    "sun aukat anusar chud tu",
    "chl mc apni mom ki kali chut ki mms send kr",
    "𝐁𝐀𝐍𝐍𝐄𝐑 𝐌𝐎𝐃𝐄 𝐎𝐍 𝐓𝟑𝐑𝐈 𝐌𝟒𝟒 𝐊𝐈 𝐂𝐇𝟎𝟎𝐓",
)

_REVNC_TEXTS: tuple[str, ...] = (
    "Aᴘɴɪ Mᴏᴍ Gɪғᴛ Kʀ Dᴊ Aʟᴏᴋ Kɪ Jᴀɢᴀʜ",
    "𝙏𝙢𝙠𝙘 𝙢𝙚𝙮 🤖",
    "𝐓𝐄𝐑𝐘 𝐌𝐀 𝐊𝐎 𝐊𝐈𝐒𝐒 𝐊𝐑𝐊𝐄 𝐅𝐈𝐑 𝐔𝐒𝐊𝐎 𝐃𝐇𝐄𝐄𝐑𝐄 𝐒𝐄 𝐏𝐀𝐊𝐀𝐃 𝐊𝐑 𝐂𝐇𝐎𝐃𝐔𝐆𝐀",
    "𝙏𝙚𝙧𝙮 𝙈𝙖 𝙏𝙤𝙥 𝘾𝙡𝙖𝙨𝙨 𝙆𝙞 𝙒𝙝𝙤𝙧𝙚",
    "Tᴇʀʏ Mᴀ Tʜᴏᴅɪ Sɪ Cʜᴜᴅᴀᴋᴋᴀᴅ Hᴀ Kʏᴀ",
    "Bɪʟᴋᴜʟ Aᴘɴɪ Mᴀ Pᴇ Gᴀʏᴀ Hᴀɪ Cʜᴜᴅɴᴇ Mᴇʏ",
    "Tᴇʀʏ Mᴀ Kᴏ Cʜᴏᴅ Cʜᴏᴅ Kᴀʀ Mᴀʀ Dᴀʟᴜɴɢᴀ",
    "𝙏𝙚𝙧𝙮 𝙈𝙠𝙘 𝙈𝙚𝙮 𝘾𝙝𝙪𝙝𝙖 𝘾𝙝𝙝𝙤𝙙 𝘿𝙪𝙣𝙜𝙖 𝙏𝙚𝙧𝙮 𝙈𝙖 𝙆𝙞 𝘾𝙝𝙪𝙩 𝙈𝙚 𝙂𝙝𝙪𝙨𝙠𝙧 𝙂𝙪𝙙 𝙂𝙪𝙙𝙞 𝙆𝙖𝙧𝙚𝙜𝙖",
)

_HEARTS: tuple[str, ...] = ("🩵", "🩷", "🤍")


# ── /ns payloads ──────────────────────────────────────────────────────────────

_NS_BLOCK_1 = (
    "{name}       Tꫀʀɪ Mꪮᴍ ᭙ꫝꪮRꫀ🩷🩵🤍\n\n"
    "☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽☽"
)
_NS_TEMPLATE_1 = "\n\n\n\n".join([_NS_BLOCK_1] * 13)

_NS_BLOCK_2 = (
    "{name} TꫀRɪ Mꪖ ᛕꪮ ᥴʜᴏD ᥴꫝꪮD ᛕꪖR ꪑꪖꪖR DꪖꪶꪊꪀGꪖ Rꫀ 🩷🩵🤍"
    "          彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡\n\n"
    "彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡"
)
_NS_TEMPLATE_2 = "\n\n\n\n".join([_NS_BLOCK_2] * 12)

_NS_BLOCK_3 = (
    "{name} LꪊꪀD Sꫀꪗ ꪊꪻꪖR ꪑᦔᥴ ꪖB ᦔꫝꫀꫀRꫀ ᦔꫝꫀꫀRꫀ ᦔꪖRᴅ ꫝꪮꪀꫀ Lꪖɢɪ Hꪖ 😡"
    " 彡✯✯✯✯✯✯✯✯✯✯✯✯\n\n"
    "彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡彡"
)
_NS_TEMPLATE_3 = "\n\n\n\n".join([_NS_BLOCK_3] * 11)

_NS_TEMPLATES: tuple[str, ...] = (_NS_TEMPLATE_1, _NS_TEMPLATE_2, _NS_TEMPLATE_3)


def ns_payload(name: str) -> str:
    tpl = random.choice(_NS_TEMPLATES)
    return tpl.replace("{name}", name)


# ═════════════════════════════════════════════════════════════════════════════
# Shared per-chat pacing limiter
# ═════════════════════════════════════════════════════════════════════════════

class ChatRateLimiter:
    """Soft overall pacing. 429s do NOT throttle this — they cool the
    offending worker only, so rotation can hand off to another bot."""

    def __init__(
        self,
        per_minute_fn: Callable[[], int],
    ) -> None:
        self._per_minute_fn = per_minute_fn
        self._next_by_chat: dict[int, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, chat_id: int) -> None:
        async with self._lock:
            now = time.monotonic()
            next_at = self._next_by_chat.get(chat_id, 0.0)
            slot = max(now, next_at)
            interval = 60.0 / max(1, self._per_minute_fn())
            self._next_by_chat[chat_id] = slot + interval

        wait = slot - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)


# ═════════════════════════════════════════════════════════════════════════════
# Worker bot
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class WorkerStats:
    sent: int = 0
    failed: int = 0
    reacted: int = 0
    last_error: str = ""
    online: bool = True


class WorkerBot:
    """A Bot + per-(chat, kind) floor + 429 cooldown (capped) + counters."""

    KINDS = ("replyraid", "swipe", "gcnc", "ns")

    def __init__(
        self,
        token: str,
        cfg: Config,
        msg_limiter: ChatRateLimiter,
        title_limiter: ChatRateLimiter,
    ) -> None:
        self.bot = Bot(token=token)
        self.cfg = cfg
        self.msg_limiter = msg_limiter
        self.title_limiter = title_limiter
        self.stats = WorkerStats()
        self._id: int | None = None
        self._username: str | None = None
        self._min_gates: dict[tuple[int, str], float] = {}

    @property
    def label(self) -> str:
        return f"@{self._username}" if self._username else f"bot:{self._id or '?'}"

    @property
    def ident(self) -> int | None:
        return self._id

    async def probe(self) -> bool:
        try:
            me = await self.bot.get_me()
            self._id = me.id
            self._username = me.username or me.first_name
            self.stats.online = True
            return True
        except TelegramError as exc:
            self.stats.online = False
            self.stats.last_error = type(exc).__name__
            log.warning("worker probe failed: %s", self.stats.last_error)
            return False

    async def preflight(self, chat_id: int, need_admin: bool) -> tuple[bool, str]:
        if self._id is None:
            return False, "not probed"
        try:
            member = await self.bot.get_chat_member(chat_id, self._id)
        except Forbidden:
            return False, "kicked"
        except BadRequest as exc:
            return False, f"err:{type(exc).__name__}"
        except TelegramError as exc:
            return False, f"err:{type(exc).__name__}"

        status = getattr(member, "status", "unknown")
        if status in ("left", "kicked"):
            return False, status

        if need_admin:
            if status not in ("administrator", "creator"):
                return False, "not admin"
            if status == "administrator" and not getattr(member, "can_change_info", False):
                return False, "no change_info"
        else:
            if status == "restricted" and not getattr(member, "can_send_messages", True):
                return False, "muted"

        return True, "ok"

    def available_at(self, chat_id: int, kind: str) -> float:
        return self._min_gates.get((chat_id, kind), 0.0)

    def is_available(self, chat_id: int, kind: str) -> bool:
        return time.monotonic() >= self.available_at(chat_id, kind)

    def cool(self, chat_id: int, kind: str, seconds: float) -> None:
        """Cool this worker for min(retry_after, flood_cap).  Capped so a
        stuck 429 for 30s doesn't lock the worker out for the full window."""
        key = (chat_id, kind)
        capped = min(seconds, self.cfg.flood_cap)
        self._min_gates[key] = max(
            self._min_gates.get(key, 0.0),
            time.monotonic() + capped,
        )

    async def _worker_floor(self, chat_id: int, kind: str) -> None:
        key = (chat_id, kind)
        delay = max(MIN_DELAY, self.cfg.delays.get(kind))
        now = time.monotonic()
        next_at = self._min_gates.get(key, 0.0)
        if now < next_at:
            await asyncio.sleep(next_at - now)
        self._min_gates[key] = time.monotonic() + delay

    async def send(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        kind: str = "replyraid",
    ) -> str:
        await self.msg_limiter.acquire(chat_id)
        await self._worker_floor(chat_id, kind)

        kwargs = {"chat_id": chat_id, "text": text}
        if reply_to is not None:
            kwargs["reply_to_message_id"] = reply_to
            kwargs["allow_sending_without_reply"] = True
        if self.cfg.use_paid_broadcast:
            kwargs["allow_paid_broadcast"] = True

        try:
            await self.bot.send_message(**kwargs)
            self.stats.sent += 1
            return "ok"
        except RetryAfter as exc:
            self.cool(chat_id, kind, float(exc.retry_after) + 1.0)
            return "retry"
        except Forbidden:
            self.stats.failed += 1
            self.stats.last_error = "Forbidden"
            return "dead"
        except BadRequest as exc:
            self.stats.failed += 1
            self.stats.last_error = "BadRequest"
            msg_lower = str(exc).lower()
            if "rights" in msg_lower or "admin" in msg_lower:
                return "dead"
            return "failed"
        except TelegramError as exc:
            self.stats.failed += 1
            self.stats.last_error = type(exc).__name__
            return "failed"
        except Exception as exc:                       # noqa: BLE001
            self.stats.failed += 1
            self.stats.last_error = type(exc).__name__
            return "failed"

    async def set_title(
        self,
        chat_id: int,
        title: str,
        kind: str = "gcnc",
    ) -> str:
        await self.title_limiter.acquire(chat_id)
        await self._worker_floor(chat_id, kind)

        try:
            await self.bot.set_chat_title(chat_id=chat_id, title=title)
            return "ok"
        except RetryAfter as exc:
            self.cool(chat_id, kind, float(exc.retry_after) + 1.0)
            return "retry"
        except Forbidden:
            self.stats.failed += 1
            self.stats.last_error = "Forbidden"
            return "dead"
        except BadRequest as exc:
            self.stats.failed += 1
            self.stats.last_error = "BadRequest"
            msg_lower = str(exc).lower()
            if "rights" in msg_lower or "admin" in msg_lower:
                return "dead"
            return "failed"
        except TelegramError as exc:
            self.stats.failed += 1
            self.stats.last_error = type(exc).__name__
            return "failed"
        except Exception as exc:                       # noqa: BLE001
            self.stats.failed += 1
            self.stats.last_error = type(exc).__name__
            return "failed"

    async def react(self, chat_id: int, message_id: int, emoji: str) -> bool:
        key = (chat_id, "react")
        now = time.monotonic()
        if now < self._min_gates.get(key, 0.0):
            return False
        self._min_gates[key] = now + self.cfg.react_cooldown

        try:
            await self.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )
            self.stats.reacted += 1
            return True
        except TelegramError:
            return False
        except Exception:                              # noqa: BLE001
            return False

    async def leave(self, chat_id: int) -> None:
        with contextlib.suppress(TelegramError):
            await self.bot.leave_chat(chat_id)


# ═════════════════════════════════════════════════════════════════════════════
# Task registry
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class TaskHandle:
    task_id: int
    kind: str
    chat_id: int
    worker: WorkerBot | None
    task: asyncio.Task


class TaskRegistry:
    def __init__(self) -> None:
        self._next_id = 1
        self._by_chat: dict[int, list[TaskHandle]] = {}

    def add(
        self,
        chat_id: int,
        kind: str,
        worker: WorkerBot | None,
        coro: Awaitable[None],
    ) -> TaskHandle:
        handle = TaskHandle(
            task_id=self._next_id,
            kind=kind,
            chat_id=chat_id,
            worker=worker,
            task=asyncio.create_task(coro),
        )
        self._next_id += 1
        self._by_chat.setdefault(chat_id, []).append(handle)
        return handle

    def list(self, chat_id: int) -> list[TaskHandle]:
        bucket = self._by_chat.get(chat_id, [])
        alive = [h for h in bucket if not h.task.done()]
        self._by_chat[chat_id] = alive
        return alive

    def cancel_chat(self, chat_id: int) -> int:
        handles = self.list(chat_id)
        for h in handles:
            if not h.task.done():
                h.task.cancel()
        self._by_chat[chat_id] = []
        return len(handles)

    def cancel_all(self) -> int:
        total = 0
        for chat_id in list(self._by_chat.keys()):
            total += self.cancel_chat(chat_id)
        return total

    def active_chats(self) -> list[int]:
        return [cid for cid, hs in self._by_chat.items() if hs]


# ═════════════════════════════════════════════════════════════════════════════
# Son store
# ═════════════════════════════════════════════════════════════════════════════

class SonStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._ids: set[int] = set()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._ids = set(json.loads(self._path.read_text()))
        except Exception:                              # noqa: BLE001
            log.warning("sons file malformed; starting empty")

    def _flush(self) -> None:
        try:
            self._path.write_text(json.dumps(sorted(self._ids)))
        except OSError as exc:
            log.warning("failed to persist sons: %s", exc)

    def add(self, user_id: int) -> None:
        self._ids.add(user_id)
        self._flush()

    def remove(self, user_id: int) -> None:
        self._ids.discard(user_id)
        self._flush()

    def __contains__(self, user_id: int) -> bool:
        return user_id in self._ids

    def snapshot(self) -> list[int]:
        return sorted(self._ids)


# ═════════════════════════════════════════════════════════════════════════════
# Console
# ═════════════════════════════════════════════════════════════════════════════

class Console:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.sons = SonStore(cfg.sons_path)
        self.tasks = TaskRegistry()
        self.workers: list[WorkerBot] = []
        self.app: Application | None = None
        self._shutdown = asyncio.Event()
        self.react_modes: dict[int, tuple[str, str]] = {}
        # (chat_id, kind) -> flood resume monotonic time
        self._chat_flood: dict[tuple[int, str], float] = {}

        self.msg_limiter = ChatRateLimiter(
            per_minute_fn=lambda: self.cfg.msg_per_minute,
        )
        self.title_limiter = ChatRateLimiter(
            per_minute_fn=lambda: self.cfg.title_per_minute,
        )

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def boot(self) -> None:
        self._banner()

        self.workers = [
            WorkerBot(tok, self.cfg, self.msg_limiter, self.title_limiter)
            for tok in self.cfg.worker_tokens
        ]
        results = await asyncio.gather(*(w.probe() for w in self.workers))
        online = sum(results)
        log.info("workers: %d online / %d total", online, len(self.workers))

        self.app = (
            ApplicationBuilder()
            .token(self.cfg.main_bot_token)
            .concurrent_updates(True)
            .build()
        )

        self._register_commands(self.app)
        self._install_signal_handlers()

    def _banner(self) -> None:
        d = self.cfg.delays
        log.info("┌─ control console ────────────────────────────")
        log.info("│  owner        %d", self.cfg.owner_id)
        log.info("│  workers      %d", len(self.cfg.worker_tokens))
        log.info("│  replyraid    %.2fs", d.replyraid)
        log.info("│  swipe        %.2fs", d.swipe)
        log.info("│  gcnc         %.2fs", d.gcnc)
        log.info("│  ns           %.2fs", d.ns)
        log.info("│  msg cap      %d/min", self.cfg.msg_per_minute)
        log.info("│  title cap    %d/min", self.cfg.title_per_minute)
        log.info("│  flood cap    %.1fs", self.cfg.flood_cap)
        log.info("└──────────────────────────────────────────────")

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._shutdown.set)

    def _register_commands(self, app: Application) -> None:
        table: dict[str, Callable] = {
            "start":      self.cmd_start,
            "help":       self.cmd_start,
            "menu":       self.cmd_start,
            "ping":       self.cmd_ping,
            "whoami":     self.cmd_whoami,
            "addson":     self.cmd_addson,
            "delson":     self.cmd_delson,
            "sons":       self.cmd_sons,
            "replyraid":  self.cmd_replyraid,
            "swipe":      self.cmd_swipe,
            "gcnc":       self.cmd_gcnc,
            "revnc":      self.cmd_revnc,
            "ns":         self.cmd_ns,
            "reall":      self.cmd_reall,
            "sre":        self.cmd_sre,
            "reactstop":  self.cmd_reactstop,
            "stop":       self.cmd_stop,
            "stopall":    self.cmd_stopall,
            "status":     self.cmd_status,
            "delay":      self.cmd_delay,
            "delays":     self.cmd_delays,
            "turbo":      self.cmd_turbo,
            "preflight":  self.cmd_preflight,
            "promoteall": self.cmd_promoteall,
            "leave":      self.cmd_leave,
        }
        for name, fn in table.items():
            app.add_handler(CommandHandler(name, fn))

        app.add_handler(MessageHandler(
            filters.ALL & ~filters.COMMAND,
            self._react_handler,
        ))

    async def run(self) -> None:
        assert self.app is not None
        async with self.app:
            await self.app.start()
            await self.app.updater.start_polling(drop_pending_updates=True)
            log.info("polling started — waiting for commands")
            try:
                await self._shutdown.wait()
                log.info("shutdown requested")
            finally:
                self.tasks.cancel_all()
                with contextlib.suppress(Exception):
                    await self.app.updater.stop()
                with contextlib.suppress(Exception):
                    await self.app.stop()
                log.info("console offline")

    # ── access ────────────────────────────────────────────────────────────

    def _authorized(self, user_id: int) -> bool:
        return user_id == self.cfg.owner_id or user_id in self.sons

    def _is_owner(self, user_id: int) -> bool:
        return user_id == self.cfg.owner_id

    async def _gate(
        self,
        update: Update,
        *,
        owner_only: bool = False,
    ) -> bool:
        user = update.effective_user
        msg = update.effective_message
        if not user or not msg:
            return False

        is_owner = self._is_owner(user.id)
        is_son = user.id in self.sons

        if not is_owner and not is_son:
            log.info(
                "unrecognized user: id=%d username=@%s name=%s",
                user.id, user.username or "-", user.first_name or "-",
            )

        if owner_only and not is_owner:
            await msg.reply_text(S("CHAL TMKC") + " 👎🏿")
            return False
        if not owner_only and not (is_owner or is_son):
            await msg.reply_text(S("CHAL TMKC") + " 👎🏿")
            return False
        return True

    # ── formatting ────────────────────────────────────────────────────────

    @staticmethod
    def _section(
        title: str,
        pairs: Iterable[tuple[str, str]],
        width: int = 14,
    ) -> str:
        out = [f"▸ {title}", ""]
        for k, v in pairs:
            out.append(f"  {k:<{width}} {v}")
        return "\n".join(out)

    @staticmethod
    def _eligible_split(
        workers: list[WorkerBot],
        results: list[tuple[bool, str]],
    ) -> tuple[list[WorkerBot], list[str]]:
        eligible: list[WorkerBot] = []
        skipped: list[str] = []
        for w, (ok, reason) in zip(workers, results):
            if ok:
                eligible.append(w)
            else:
                skipped.append(f"{w.label}:{reason}")
        return eligible, skipped

    # ── commands ──────────────────────────────────────────────────────────

    async def cmd_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update):
            return

        user = update.effective_user
        role = "owner" if self._is_owner(user.id) else "son"

        header = (
            "╭──────────────────────────────╮\n"
            f"│  {'𝙇𝙐𝙉𝘼𝙍 𝘾𝙊𝙉𝙎𝙊𝙇𝙀':<28}│\n"
            "╰──────────────────────────────╯"
        )

        identity = self._section(
            "identity",
            [("role", role), ("user", user.first_name or "-")],
        )

        raids = self._section(
            "𝙎𝙘",
            [
                ("/replyraid", "(𝙍𝙚𝙥𝙡𝙮)"),
                ("/swipe",     "(𝙍𝙚𝙥𝙡𝙮)"),
                ("/gcnc",      "𝙏𝙚𝙭𝙩𝙣𝙘"),
                ("/revnc",     "𝙉𝙘"),
                ("/ns",        "𝙉𝙘+𝙎𝙥𝙢"),
                ("/stop",      "𝙎𝙩𝙤𝙥 𝙖𝙡𝙡 𝙞𝙣 𝙩𝙝𝙞𝙨 𝙘𝙝𝙖𝙩"),
                ("/stopall",   "𝙨𝙩𝙤𝙥 𝙖𝙡𝙡 𝙍𝙪𝙣𝙣𝙞𝙣𝙜 𝘾𝙤𝙢𝙢𝙖𝙣𝙙𝙨"),
            ],
        )

        reactions = self._section(
            "𝙍𝙚𝙖𝙘𝙩𝙞𝙤𝙣𝙨",
            [
                ("/reall",     "𝙍𝙚𝙖𝙘𝙩 𝘼𝙡𝙡 (𝙀𝙢𝙤𝙟𝙞)"),
                ("/sre",       "𝙎𝙚𝙡𝙛 𝙍𝙚𝙖𝙘𝙩 (𝙀𝙢𝙤𝙟𝙞)"),
                ("/reactstop", "𝘿𝙞𝙨𝙖𝙗𝙡𝙚"),
            ],
        )

        tuning = self._section(
            "𝙏𝙪𝙣𝙞𝙣𝙜",
            [
                ("/delay",     "𝙎𝙚𝙩 𝘿𝙚𝙡𝙖𝙮 <kind> <seconds>"),
                ("/delays",    "show all current delays"),
                ("/turbo",     f"𝙎𝙚𝙩 𝘼𝙡𝙡 𝘿𝙚𝙡𝙖𝙮 𝙏𝙤 {MIN_DELAY:.1f}s"),
                ("/status",    "𝙎𝙩𝙖𝙩𝙪𝙨"),
                ("/preflight", "𝘾𝙝𝙚𝙘𝙠"),
            ],
        )

        access = self._section(
            "𝘼𝙘𝙘𝙚𝙨𝙨",
            [
                ("/addson", "𝙍𝙚𝙥𝙡𝙮"),
                ("/delson", "𝙍𝙚𝙥𝙡𝙮 𝙩𝙤 𝙧𝙚𝙢𝙤𝙫𝙚"),
                ("/sons",   "𝙇𝙞𝙨𝙩"),
                ("/whoami", "𝙄𝙙+𝙍𝙤𝙡𝙚"),
                ("/ping",   "𝙇𝙖𝙩𝙚𝙣𝙘𝙮"),
            ],
        )

        control = self._section(
            "control",
            [
                ("/promoteall", "promote every worker to admin"),
                ("/leave",      "all bots leave this chat"),
            ],
        )

        body = "\n\n".join([
            header, identity, raids, reactions, tuning, access, control,
        ])
        await update.message.reply_text(body)

    async def cmd_ping(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update):
            return
        t0 = time.monotonic()
        sent = await update.message.reply_text("pong")
        rtt = (time.monotonic() - t0) * 1000
        age = (
            datetime.now(timezone.utc) - update.message.date
        ).total_seconds() * 1000
        online = sum(1 for w in self.workers if w.stats.online)
        await sent.edit_text(
            self._section(
                "ping",
                [
                    ("round trip", f"{rtt:.0f} ms"),
                    ("msg age",    f"{age:.0f} ms"),
                    ("workers",    f"{online}/{len(self.workers)} online"),
                ],
            )
        )

    async def cmd_whoami(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        user = update.effective_user
        msg = update.effective_message
        if not user or not msg:
            return

        is_owner = self._is_owner(user.id)
        is_son = user.id in self.sons
        role = "owner" if is_owner else ("son" if is_son else "unknown")

        pairs = [
            ("your id",  str(user.id)),
            ("username", f"@{user.username}" if user.username else "-"),
            ("name",     user.first_name or "-"),
            ("role",     role),
            ("owner id", str(self.cfg.owner_id)),
            ("match",    "yes" if is_owner else "no"),
        ]
        await msg.reply_text(self._section("you", pairs))

    async def cmd_preflight(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update):
            return
        chat_id = update.message.chat_id

        send_results = await asyncio.gather(
            *(w.preflight(chat_id, need_admin=False) for w in self.workers)
        )
        title_results = await asyncio.gather(
            *(w.preflight(chat_id, need_admin=True) for w in self.workers)
        )

        send_ok = sum(1 for ok, _ in send_results if ok)
        title_ok = sum(1 for ok, _ in title_results if ok)

        send_skip = [
            f"{w.label}:{r}"
            for w, (ok, r) in zip(self.workers, send_results) if not ok
        ]
        title_skip = [
            f"{w.label}:{r}"
            for w, (ok, r) in zip(self.workers, title_results) if not ok
        ]

        pairs = [
            ("send",  f"{send_ok}/{len(self.workers)}"),
            ("title", f"{title_ok}/{len(self.workers)}"),
        ]
        body = self._section("preflight", pairs)
        if send_skip:
            body += "\n\n" + self._section(
                "send skipped", [(s, "") for s in send_skip[:12]], width=0
            )
        if title_skip:
            body += "\n\n" + self._section(
                "title skipped", [(s, "") for s in title_skip[:12]], width=0
            )

        await update.message.reply_text(body)

    async def cmd_addson(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update, owner_only=True):
            return
        msg = update.message
        target = msg.reply_to_message.from_user if msg.reply_to_message else None
        if not target:
            await msg.reply_text("Reply to a user with /addson")
            return
        if target.is_bot:
            await msg.reply_text("Bots cannot be sons")
            return
        self.sons.add(target.id)
        await msg.reply_text(
            self._section(
                "access granted",
                [
                    ("user", target.first_name or "-"),
                    ("id",   str(target.id)),
                    ("role", "son"),
                ],
            )
        )

    async def cmd_delson(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update, owner_only=True):
            return
        msg = update.message
        target = msg.reply_to_message.from_user if msg.reply_to_message else None
        if not target:
            await msg.reply_text("Reply to a user with /delson")
            return
        self.sons.remove(target.id)
        await msg.reply_text(
            self._section(
                "access revoked",
                [
                    ("user", target.first_name or "-"),
                    ("id",   str(target.id)),
                ],
            )
        )

    async def cmd_sons(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update, owner_only=True):
            return
        ids = self.sons.snapshot()
        if not ids:
            await update.message.reply_text("no sons yet")
            return
        lines = [f"{i+1:>2}.  {uid}" for i, uid in enumerate(ids)]
        await update.message.reply_text(
            f"sons — {len(ids)}\n──────────────\n  " + "\n  ".join(lines)
        )

    # ── raids ─────────────────────────────────────────────────────────────

    async def cmd_replyraid(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update):
            return
        msg = update.message
        if not msg.reply_to_message:
            await msg.reply_text("Reply to a target with /replyraid <name>")
            return
        if not context.args:
            await msg.reply_text("Usage: /replyraid <name>")
            return
        if not self.workers:
            await msg.reply_text("No workers configured")
            return

        styled_name = S(" ".join(context.args))
        target_id = msg.reply_to_message.message_id
        chat_id = msg.chat_id

        results = await asyncio.gather(
            *(w.preflight(chat_id, need_admin=False) for w in self.workers)
        )
        eligible, skipped = self._eligible_split(self.workers, results)

        if not eligible:
            await msg.reply_text(
                self._section("replyraid blocked", [
                    ("eligible", "0"),
                    ("reason",   ", ".join(skipped) or "no workers"),
                ])
            )
            return

        def fire(w: WorkerBot):
            body = random.choice(_PAYLOADS)
            return w.send(chat_id, f"{styled_name} {body}", target_id, kind="replyraid")

        self.tasks.add(
            chat_id, "replyraid", eligible[0],
            self._rotation_loop(chat_id, "replyraid", eligible, fire),
        )

        pairs = [
            ("name",     styled_name),
            ("workers",  f"{len(eligible)} of {len(self.workers)}"),
            ("rotation", "on"),
            ("delay",    f"{self.cfg.delays.replyraid:.2f}s"),
        ]
        if skipped:
            pairs.append(("skipped", ", ".join(skipped)[:200]))

        await msg.reply_text(self._section("replyraid deployed", pairs))

    async def cmd_swipe(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update):
            return
        msg = update.message
        if not msg.reply_to_message:
            await msg.reply_text("Reply to a target with /swipe <text>")
            return
        if not context.args:
            await msg.reply_text("Usage: /swipe <text>")
            return
        if not self.workers:
            await msg.reply_text("No workers configured")
            return

        text = " ".join(context.args)
        target_id = msg.reply_to_message.message_id
        chat_id = msg.chat_id

        results = await asyncio.gather(
            *(w.preflight(chat_id, need_admin=False) for w in self.workers)
        )
        eligible, skipped = self._eligible_split(self.workers, results)

        if not eligible:
            await msg.reply_text(
                self._section("swipe blocked", [
                    ("eligible", "0"),
                    ("reason",   ", ".join(skipped) or "no workers"),
                ])
            )
            return

        def fire(w: WorkerBot):
            return w.send(chat_id, text, target_id, kind="swipe")

        self.tasks.add(
            chat_id, "swipe", eligible[0],
            self._rotation_loop(chat_id, "swipe", eligible, fire),
        )

        pairs = [
            ("payload",  text[:40]),
            ("workers",  f"{len(eligible)} of {len(self.workers)}"),
            ("rotation", "on"),
            ("delay",    f"{self.cfg.delays.swipe:.2f}s"),
        ]
        if skipped:
            pairs.append(("skipped", ", ".join(skipped)[:200]))

        await msg.reply_text(self._section("swipe deployed", pairs))

    async def cmd_gcnc(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update):
            return
        msg = update.message
        if not context.args:
            await msg.reply_text("Usage: /gcnc <name>")
            return
        if not self.workers:
            await msg.reply_text("No workers configured")
            return

        styled_name = S(" ".join(context.args))
        chat_id = msg.chat_id

        results = await asyncio.gather(
            *(w.preflight(chat_id, need_admin=True) for w in self.workers)
        )
        eligible, skipped = self._eligible_split(self.workers, results)

        if not eligible:
            await msg.reply_text(
                self._section("gcnc blocked", [
                    ("eligible", "0"),
                    ("reason",   ", ".join(skipped) or "no workers"),
                ])
            )
            return

        def fire(w: WorkerBot):
            title = f"{styled_name} {random.choice(_HEARTS)}"
            return w.set_title(chat_id, title, kind="gcnc")

        self.tasks.add(
            chat_id, "gcnc", eligible[0],
            self._rotation_loop(chat_id, "gcnc", eligible, fire),
        )

        pairs = [
            ("name",     styled_name),
            ("workers",  f"{len(eligible)} of {len(self.workers)}"),
            ("rotation", "on"),
            ("delay",    f"{self.cfg.delays.gcnc:.2f}s"),
        ]
        if skipped:
            pairs.append(("skipped", ", ".join(skipped)[:200]))

        await msg.reply_text(self._section("gcnc deployed", pairs))

    async def cmd_revnc(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update):
            return
        msg = update.message
        if not context.args:
            await msg.reply_text("Usage: /revnc <name>")
            return
        if not self.workers:
            await msg.reply_text("No workers configured")
            return

        styled_name = S(" ".join(context.args))
        chat_id = msg.chat_id

        results = await asyncio.gather(
            *(w.preflight(chat_id, need_admin=True) for w in self.workers)
        )
        eligible, skipped = self._eligible_split(self.workers, results)

        if not eligible:
            await msg.reply_text(
                self._section("revnc blocked", [
                    ("eligible", "0"),
                    ("reason",   ", ".join(skipped) or "no workers"),
                ])
            )
            return

        def fire(w: WorkerBot):
            taunt = random.choice(_REVNC_TEXTS)
            heart = random.choice(_HEARTS)
            return w.set_title(chat_id, f"{styled_name} {taunt} {heart}", kind="gcnc")

        self.tasks.add(
            chat_id, "revnc", eligible[0],
            self._rotation_loop(chat_id, "gcnc", eligible, fire),
        )

        pairs = [
            ("name",     styled_name),
            ("workers",  f"{len(eligible)} of {len(self.workers)}"),
            ("rotation", "on"),
            ("delay",    f"{self.cfg.delays.gcnc:.2f}s"),
        ]
        if skipped:
            pairs.append(("skipped", ", ".join(skipped)[:200]))

        await msg.reply_text(self._section("revnc deployed", pairs))

    async def cmd_ns(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update):
            return
        msg = update.message
        if not context.args:
            await msg.reply_text("Usage: /ns <name>")
            return
        if not self.workers:
            await msg.reply_text("No workers configured")
            return

        styled_name = S(" ".join(context.args))
        chat_id = msg.chat_id

        title_results = await asyncio.gather(
            *(w.preflight(chat_id, need_admin=True) for w in self.workers)
        )
        title_eligible, title_skipped = self._eligible_split(self.workers, title_results)

        msg_results = await asyncio.gather(
            *(w.preflight(chat_id, need_admin=False) for w in self.workers)
        )
        msg_eligible, msg_skipped = self._eligible_split(self.workers, msg_results)

        if not title_eligible and not msg_eligible:
            await msg.reply_text(
                self._section("ns blocked", [
                    ("title eligible", str(len(title_eligible))),
                    ("spam eligible",  str(len(msg_eligible))),
                    ("reason",         ", ".join(title_skipped + msg_skipped)[:200]),
                ])
            )
            return

        if title_eligible:
            def title_fire(w: WorkerBot):
                taunt = random.choice(_REVNC_TEXTS)
                heart = random.choice(_HEARTS)
                return w.set_title(chat_id, f"{styled_name} {taunt} {heart}", kind="gcnc")

            self.tasks.add(
                chat_id, "ns-title", title_eligible[0],
                self._rotation_loop(chat_id, "gcnc", title_eligible, title_fire),
            )

        if msg_eligible:
            def spam_fire(w: WorkerBot):
                return w.send(chat_id, ns_payload(styled_name), kind="ns")

            self.tasks.add(
                chat_id, "ns-spam", msg_eligible[0],
                self._rotation_loop(chat_id, "ns", msg_eligible, spam_fire),
            )

        pairs = [
            ("name",     styled_name),
            ("title",    f"{len(title_eligible)} of {len(self.workers)}"),
            ("spam",     f"{len(msg_eligible)} of {len(self.workers)}"),
            ("rotation", "on"),
            ("ns delay", f"{self.cfg.delays.ns:.2f}s"),
        ]
        await msg.reply_text(self._section("ns deployed", pairs))

    # ── reactions ─────────────────────────────────────────────────────────

    async def cmd_reall(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        await self._set_react(update, context, mode="all", label="reall")

    async def cmd_sre(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        await self._set_react(update, context, mode="sre", label="sre")

    async def _set_react(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        mode: str,
        label: str,
    ) -> None:
        if not await self._gate(update):
            return
        msg = update.message
        chat_id = msg.chat_id

        if not context.args:
            await msg.reply_text(
                self._section(f"{label} usage", [
                    (f"/{label}", "<emoji>   enable"),
                    (f"/{label}", "off       disable"),
                ])
            )
            return

        arg = context.args[0].strip()
        if arg.lower() in ("off", "stop", "disable"):
            self.react_modes.pop(chat_id, None)
            await msg.reply_text(
                self._section(f"{label} off", [("state", "disabled")])
            )
            return

        if not self.workers:
            await msg.reply_text("No workers configured")
            return

        self.react_modes[chat_id] = (mode, arg)
        await msg.reply_text(
            self._section(f"{label} on", [
                ("mode",    "every message" if mode == "all" else "owner only"),
                ("emoji",   arg),
                ("workers", str(len(self.workers))),
                ("cooldown", f"{self.cfg.react_cooldown:.2f}s"),
            ])
        )

    async def cmd_reactstop(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update):
            return
        had = self.react_modes.pop(update.message.chat_id, None) is not None
        await update.message.reply_text(
            self._section("reactions halted", [("was active", "yes" if had else "no")])
        )

    async def cmd_stop(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update):
            return
        chat_id = update.message.chat_id
        n_tasks = self.tasks.cancel_chat(chat_id)
        had_react = self.react_modes.pop(chat_id, None) is not None
        pairs = [("loops", str(n_tasks))]
        if had_react:
            pairs.append(("reactions", "off"))
        await update.message.reply_text(self._section("loops halted", pairs))

    async def cmd_stopall(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update, owner_only=True):
            return
        n = self.tasks.cancel_all()
        react_count = len(self.react_modes)
        self.react_modes.clear()
        await update.message.reply_text(
            self._section("all halted", [
                ("loops",     str(n)),
                ("reactions", str(react_count)),
            ])
        )

    async def cmd_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update):
            return

        chat_id = update.message.chat_id
        live = self.tasks.list(chat_id)
        by_kind: dict[str, int] = {}
        for h in live:
            by_kind[h.kind] = by_kind.get(h.kind, 0) + 1
        loop_pairs = [(k, f"×{v}") for k, v in by_kind.items()] or [("state", "idle")]

        react_data = self.react_modes.get(chat_id)
        if react_data:
            mode, emoji = react_data
            react_pairs = [("mode", mode), ("emoji", emoji)]
        else:
            react_pairs = [("state", "off")]

        others = [c for c in self.tasks.active_chats() if c != chat_id]
        if others:
            cross_pairs = [(str(c), f"×{len(self.tasks.list(c))}") for c in others[:10]]
        else:
            cross_pairs = [("state", "none")]

        worker_pairs = [
            (w.label, f"sent {w.stats.sent:>6}  react {w.stats.reacted:>5}  err {w.stats.failed:>4}")
            for w in self.workers
        ]

        d = self.cfg.delays
        delay_pairs = [
            ("replyraid", f"{d.replyraid:>5.2f}s"),
            ("swipe",     f"{d.swipe:>5.2f}s"),
            ("gcnc",      f"{d.gcnc:>5.2f}s"),
            ("ns",        f"{d.ns:>5.2f}s"),
            ("msg cap",   f"{self.cfg.msg_per_minute}/min"),
            ("title cap", f"{self.cfg.title_per_minute}/min"),
            ("flood cap", f"{self.cfg.flood_cap:.1f}s"),
            ("react cd",  f"{self.cfg.react_cooldown:.2f}s"),
        ]

        body = "\n\n".join([
            self._section("this chat", loop_pairs),
            self._section("reactions", react_pairs),
            self._section("other chats", cross_pairs),
            self._section("workers", worker_pairs, width=24),
            self._section("delays", delay_pairs),
        ])
        await update.message.reply_text(body)

    async def cmd_delay(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update, owner_only=True):
            return
        msg = update.message
        if len(context.args) != 2:
            await msg.reply_text(
                self._section("usage", [
                    ("/delay", "<kind> <seconds>"),
                    ("kinds",  "replyraid | swipe | gcnc | ns"),
                    ("range",  f"{MIN_DELAY:.1f} – {MAX_DELAY:.1f}"),
                ])
            )
            return

        kind, raw = context.args
        if kind not in WorkerBot.KINDS:
            await msg.reply_text(f"Unknown kind: {kind}")
            return

        try:
            value = float(raw)
        except ValueError:
            await msg.reply_text("Seconds must be a number")
            return

        applied = self.cfg.delays.set(kind, value)
        clamped_note = ""
        if applied != value:
            clamped_note = f"  (clamped to {applied:.2f}s)"

        await msg.reply_text(
            self._section("delay updated", [
                ("kind",  kind),
                ("value", f"{applied:.2f}s{clamped_note}"),
            ])
        )

    async def cmd_delays(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update):
            return
        d = self.cfg.delays
        await update.message.reply_text(
            self._section("delays", [
                ("replyraid", f"{d.replyraid:>5.2f}s"),
                ("swipe",     f"{d.swipe:>5.2f}s"),
                ("gcnc",      f"{d.gcnc:>5.2f}s"),
                ("ns",        f"{d.ns:>5.2f}s"),
                ("floor",     f"{MIN_DELAY:.2f}s"),
                ("ceiling",   f"{MAX_DELAY:.2f}s"),
                ("msg cap",   f"{self.cfg.msg_per_minute}/min"),
                ("title cap", f"{self.cfg.title_per_minute}/min"),
                ("flood cap", f"{self.cfg.flood_cap:.1f}s"),
                ("react cd",  f"{self.cfg.react_cooldown:.2f}s"),
            ])
        )

    async def cmd_turbo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update, owner_only=True):
            return
        self.cfg.delays.all_fastest()
        await update.message.reply_text(
            self._section("turbo engaged", [
                ("replyraid", f"{self.cfg.delays.replyraid:.2f}s"),
                ("swipe",     f"{self.cfg.delays.swipe:.2f}s"),
                ("gcnc",      f"{self.cfg.delays.gcnc:.2f}s"),
                ("ns",        f"{self.cfg.delays.ns:.2f}s"),
            ])
        )

    async def cmd_promoteall(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update, owner_only=True):
            return
        msg = update.message
        chat_id = msg.chat_id
        pairs: list[tuple[str, str]] = []

        for w in self.workers:
            if w.ident is None:
                pairs.append((w.label, "no id"))
                continue
            try:
                await context.bot.promote_chat_member(
                    chat_id=chat_id,
                    user_id=w.ident,
                    can_manage_chat=True,
                    can_delete_messages=True,
                    can_restrict_members=True,
                    can_invite_users=True,
                    can_pin_messages=True,
                    can_promote_members=True,
                    can_change_info=True,
                )
                pairs.append((w.label, "ok"))
            except TelegramError as exc:
                pairs.append((w.label, type(exc).__name__))

        await msg.reply_text(self._section("promotion", pairs, width=24))

    async def cmd_leave(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._gate(update, owner_only=True):
            return
        chat_id = update.message.chat_id
        self.tasks.cancel_chat(chat_id)
        self.react_modes.pop(chat_id, None)
        await update.message.reply_text("all bots leaving")
        await asyncio.gather(*(w.leave(chat_id) for w in self.workers))
        await asyncio.sleep(1.0)
        with contextlib.suppress(TelegramError):
            await context.bot.leave_chat(chat_id)

    # ── reaction handler ──────────────────────────────────────────────────

    async def _react_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        msg = update.effective_message
        if not msg or not msg.chat_id:
            return
        data = self.react_modes.get(msg.chat_id)
        if not data:
            return

        mode, emoji = data

        if msg.from_user and msg.from_user.is_bot:
            return

        if mode == "sre":
            if not msg.from_user or msg.from_user.id != self.cfg.owner_id:
                return

        await asyncio.gather(
            *(w.react(msg.chat_id, msg.message_id, emoji) for w in self.workers),
            return_exceptions=True,
        )

    # ── rotation loop ─────────────────────────────────────────────────────

    async def _rotation_loop(
        self,
        chat_id: int,
        kind: str,
        workers: list[WorkerBot],
        fire_fn: Callable[[WorkerBot], Awaitable[str]],
    ) -> None:
        """One loop per (chat, kind).

        Rotation rules:
          1. Chat-kind flood gate — after any 429, wait chat_flood_pause
             before the next attempt so workers cool one at a time,
             not all at once.
          2. Pick next worker whose per-(chat, kind) floor has expired.
          3. If all workers are cooling, sleep until the earliest one
             frees up instead of tight-looping.
          4. A "dead" result permanently drops the worker from this loop.
        """
        dead: set[int] = set()
        idx = 0
        while True:
            n = len(workers)
            alive = n - sum(1 for w in workers if w.ident in dead)
            if alive <= 0:
                log.info("rotation empty in %d (%s)", chat_id, kind)
                return

            now = time.monotonic()
            flood_until = self._chat_flood.get((chat_id, kind), 0.0)
            if now < flood_until:
                await asyncio.sleep(min(flood_until - now, 1.0))
                continue

            picked: WorkerBot | None = None
            earliest = float("inf")
            for _ in range(n):
                w = workers[idx % n]
                idx += 1
                if w.ident in dead:
                    continue
                until = w.available_at(chat_id, kind)
                if until <= now:
                    picked = w
                    break
                if until < earliest:
                    earliest = until

            if picked is None:
                wait = max(0.05, earliest - time.monotonic())
                await asyncio.sleep(min(wait, 1.0))
                continue

            try:
                result = await fire_fn(picked)
            except asyncio.CancelledError:
                raise
            except Exception:                          # noqa: BLE001
                result = "failed"

            if result == "retry":
                self._chat_flood[(chat_id, kind)] = (
                    time.monotonic() + self.cfg.chat_flood_pause
                )
            elif result == "dead":
                if picked.ident is not None:
                    dead.add(picked.ident)
                    log.info(
                        "rotation dropped %s in %d (%s)",
                        picked.label, chat_id, kind,
                    )


# ═════════════════════════════════════════════════════════════════════════════
# Health server — Render Web Service needs this on 0.0.0.0:$PORT
# ═════════════════════════════════════════════════════════════════════════════

async def _health_server() -> asyncio.AbstractServer:
    port = int(os.environ.get("PORT", "8080"))
    host = "0.0.0.0"

    async def _handle(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            try:
                await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"),
                    timeout=2.0,
                )
            except (
                asyncio.TimeoutError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
            ):
                pass
            body = b"ok"
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(_handle, host, port)
    log.info("health server listening on %s:%d", host, port)
    return server


# ═════════════════════════════════════════════════════════════════════════════
# Entry
# ═════════════════════════════════════════════════════════════════════════════

async def _amain() -> None:
    cfg = Config.load()
    console = Console(cfg)
    await console.boot()

    health = await _health_server()

    try:
        await console.run()
    finally:
        health.close()
        try:
            await health.wait_closed()
        except Exception:                              # noqa: BLE001
            pass


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
    except SystemExit as exc:
        log.error("%s", exc)
        raise


if __name__ == "__main__":
    main()