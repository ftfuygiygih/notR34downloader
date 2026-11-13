from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import secrets
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from r34 import (ANIM_EXT, IMAGE_EXT, UA, VIDEO_EXT, FileTooBig, Post, R34Client,
                 R34Error, banned_in_query, post_is_banned)

# ------------------------- КОНФИГ (всё из переменных окружения) -------------------------
def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_ID = _i("ALLOWED_USER_ID", 0)
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/tg/webhook")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip() or secrets.token_urlsafe(24)
PORT = _i("PORT", 10000)
FORCE_POLLING = os.getenv("FORCE_POLLING", "0") == "1"
USE_WEBHOOK = bool(PUBLIC_URL) and not FORCE_POLLING

R34_USER_ID = os.getenv("R34_USER_ID", "").strip()
R34_API_KEY = os.getenv("R34_API_KEY", "").strip()

DEFAULT_LIMIT = _i("DEFAULT_LIMIT", 25)
HARD_MAX_LIMIT = _i("HARD_MAX_LIMIT", 300)
SEND_DELAY = _f("SEND_DELAY", 1.3)
MAX_FILE_MB = _i("MAX_FILE_MB", 49)
MAX_PAGES = _i("MAX_PAGES", 40)
SELF_PING = os.getenv("SELF_PING", "0") == "1"
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
SEEN_MAX = 5000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")
START_TS = time.time()

# ------------------------- СОСТОЯНИЕ -------------------------
DEFAULTS = {
    "newbie": True, "limit": DEFAULT_LIMIT, "mode": "file", "video": False,
    "notify": True, "jobs": 0, "sent": 0, "last_tags": [], "last_pid": 0, "seen": [],
}


class State(dict):
    def load(self):
        self.update(DEFAULTS)
        if STATE_FILE.exists():
            with suppress(Exception):
                self.update(json.loads(STATE_FILE.read_text("utf-8")))
        self["limit"] = max(1, min(int(self["limit"]), HARD_MAX_LIMIT))
        return self

    def save(self):
        with suppress(Exception):
            STATE_FILE.write_text(json.dumps(self, ensure_ascii=False), "utf-8")


state = State().load()
seen_order: deque[int] = deque(state.get("seen", []), maxlen=SEEN_MAX)
seen: set[int] = set(seen_order)


def remember(pid: int):
    if pid in seen:
        return
    if len(seen_order) >= SEEN_MAX:
        seen.discard(seen_order[0])
    seen_order.append(pid)
    seen.add(pid)


# ------------------------- ТЕКСТЫ -------------------------
HELP = (
    "<b>🔎 Как искать</b>\n"
    "Просто пришли теги сообщением: <code>cat_ears -video</code>\n\n"
    "• <code>tag</code> или <code>+tag</code> — включить\n"
    "• <code>-tag</code> — исключить\n"
    "• теги английские, пробел внутри тега → <code>_</code>\n"
    "• метатеги: <code>rating:safe</code>, <code>sort:score</code>, "
    "<code>score:&gt;50</code>, <code>user:ник</code>\n\n"
    "<b>⚙️ Команды</b>\n"
    "/limit 30 — сколько постов за раз\n"
    "/mode file|photo — file = оригинал без сжатия, photo = картинкой в ленте\n"
    "/video on|off — качать mp4/webm (по умолчанию off)\n"
    "/notify on|off — звук уведомлений\n"
    "/random [теги] — случайный пост\n"
    "/stop — прервать закачку\n"
    "/settings — настройки, /stats — статистика\n"
    "/newbie — режим новичка вкл, /expert — выкл\n"
    "/help — эта справка\n\n"
    "⚠️ Теги loli/shota/child/teen и подобные отсекаются фильтром навсегда."
)
SHORT = "Кидай теги, например <code>cat_ears -video sort:score</code>. Справка: /help"


def kb_stop():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛔ Стоп", callback_data="stop")]])


def kb_after(can_more: bool):
    rows = []
    if can_more:
        rows.append([InlineKeyboardButton(text="➕ Ещё", callback_data="more")])
    rows.append([InlineKeyboardButton(text="🎲 Рандом", callback_data="rand"),
                 InlineKeyboardButton(text="❓ Помощь", callback_data="help")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_examples():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐱 cat_ears", callback_data="ex:cat_ears")],
        [InlineKeyboardButton(text="⭐ sort:score -video", callback_data="ex:sort:score -video")],
        [InlineKeyboardButton(text="🎲 Случайный пост", callback_data="rand")],
        [InlineKeyboardButton(text="✅ Понятно, выключить подсказки", callback_data="newbie_off")],
    ])


# ------------------------- ПАРСЕР ТЕГОВ -------------------------
def parse_tags(text: str) -> list[str]:
    out, uniq = [], set()
    for raw in (text or "").replace(",", " ").replace("\n", " ").split():
        t = raw.strip().strip(".,;")
        if t.startswith("+"):
            t = t[1:]
        if not t or "://" in t:
            continue
        t = t.lower()
        if t in uniq:
            continue
        uniq.add(t)
        out.append(t)
    return out[:12]


# ------------------------- ЗАДАЧА -------------------------
@dataclass
class Job:
    tags: list[str]
    limit: int
    pid: int
    chat_id: int
    progress_id: int = 0
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    sent: int = 0
    scanned: int = 0
    dup: int = 0
    filtered: int = 0
    skipped_video: int = 0
    too_big: int = 0
    failed: int = 0
    next_pid: int = 0
    exhausted: bool = False
    done: bool = False
    error: str = ""
    last_edit: float = 0.0
    t0: float = field(default_factory=time.time)


bot: Bot
session: aiohttp.ClientSession
client: R34Client
job: Job | None = None
job_task: asyncio.Task | None = None
ping_task: asyncio.Task | None = None

router = Router()
router.message.filter(F.from_user.id == ALLOWED_USER_ID)
router.callback_query.filter(F.from_user.id == ALLOWED_USER_ID)


def busy() -> bool:
    return job is not None and not job.done


def caption_of(p: Post) -> str:
    head = (f'🖼 <a href="{p.page_url}">{p.id}</a> · {p.width}×{p.height} '
            f'· ⭐{p.score} · {p.rating}')
    tags = ", ".join(p.tags[:8])
    return head + ("\n" + html.escape(tags) if tags else "")


async def _send(chat_id: int, data: bytes, filename: str, caption: str,
                silent: bool, as_photo: bool):
    for attempt in range(3):
        file = BufferedInputFile(data, filename=filename)
        try:
            if as_photo:
                return await bot.send_photo(chat_id, file, caption=caption,
                                            disable_notification=silent)
            return await bot.send_document(chat_id, file, caption=caption,
                                           disable_notification=silent)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramBadRequest:
            raise
        except TelegramAPIError:
            if attempt == 2:
                raise
            await asyncio.sleep(2)


async def send_post(j: Job, p: Post, data: bytes) -> bool:
    silent = not state["notify"]
    fname = f"r34_{p.id}.{p.ext or 'bin'}"
    cap = caption_of(p)
    as_photo = (state["mode"] == "photo" and p.ext in {"jpg", "jpeg", "png"}
                and len(data) < 9_500_000)
    try:
        await _send(j.chat_id, data, fname, cap, silent, as_photo)
        return True
    except TelegramBadRequest:
        if as_photo:
            with suppress(TelegramAPIError):
                await _send(j.chat_id, data, fname, cap, silent, False)
                return True
    except TelegramAPIError as e:
        log.warning("send failed %s: %s", p.id, e)
    return False


async def tick(j: Job, force: bool = False):
    if not j.progress_id:
        return
    now = time.monotonic()
    if not force and now - j.last_edit < 4.5:
        return
    j.last_edit = now
    text = (f"⬇️ <code>{html.escape(' '.join(j.tags))}</code>\n"
            f"Отправлено <b>{j.sent}</b>/{j.limit} · просмотрено {j.scanned}")
    with suppress(TelegramAPIError):
        await bot.edit_message_text(text, chat_id=j.chat_id, message_id=j.progress_id,
                                    reply_markup=kb_stop())


def tip_for(j: Job) -> str:
    if j.error:
        return "\n\n💡 Если API ругается — проверь ключи R34_USER_ID / R34_API_KEY в Render."
    if j.sent == 0:
        return ("\n\n💡 Ничего не нашлось. Проверь: теги английские, пробелы внутри тега "
                "заменены на <code>_</code>, минус-теги не выкинули всё подряд.")
    if j.dup > j.sent:
        return "\n\n💡 Много дублей — эти посты уже были. Добавь новый тег или жми «Ещё»."
    if j.skipped_video:
        return "\n\n💡 Пропущены видео. Хочешь их тоже — /video on"
    return "\n\n💡 Качество: /mode file — оригинал, /mode photo — картинкой в ленте."


async def finish(j: Job):
    lines = [f"<b>Итог</b> · <code>{html.escape(' '.join(j.tags))}</code>",
             f"Отправлено: <b>{j.sent}</b> из {j.limit} · {int(time.time() - j.t0)} c"]
    skipped = []
    if j.dup:
        skipped.append(f"дубли {j.dup}")
    if j.filtered:
        skipped.append(f"фильтр {j.filtered}")
    if j.skipped_video:
        skipped.append(f"видео {j.skipped_video}")
    if j.too_big:
        skipped.append(f">{MAX_FILE_MB}МБ {j.too_big}")
    if j.failed:
        skipped.append(f"ошибки {j.failed}")
    if skipped:
        lines.append("Пропущено: " + ", ".join(skipped))
    if j.cancel.is_set():
        lines.append("⏹ Остановлено вручную")
    if j.error:
        lines.append("⚠️ " + html.escape(j.error))
    if state["newbie"]:
        lines.append(tip_for(j).strip())
    can_more = not j.exhausted and not j.error
    with suppress(TelegramAPIError):
        if j.progress_id:
            await bot.edit_message_text("\n".join(lines), chat_id=j.chat_id,
                                        message_id=j.progress_id, reply_markup=kb_after(can_more))
        else:
            await bot.send_message(j.chat_id, "\n".join(lines), reply_markup=kb_after(can_more))


async def worker(j: Job):
    try:
        pid, pages = j.pid, 0
        while not j.cancel.is_set() and j.sent < j.limit and pages < MAX_PAGES:
            batch = await client.search(j.tags, pid=pid)
            pages += 1
            if not batch:
                j.exhausted = True
                break
            j.next_pid = pid  # продолжим с этой же страницы, дубли отсеет seen
            for p in batch:
                if j.cancel.is_set() or j.sent >= j.limit:
                    break
                j.scanned += 1
                if p.id in seen:
                    j.dup += 1
                    continue
                if post_is_banned(p.tags):
                    j.filtered += 1
                    continue
                if p.ext in VIDEO_EXT and not state["video"]:
                    j.skipped_video += 1
                    continue
                if p.ext not in (IMAGE_EXT | ANIM_EXT | VIDEO_EXT):
                    j.failed += 1
                    continue
                try:
                    data = await client.fetch(p.file_url)
                except FileTooBig:
                    j.too_big += 1
                    continue
                except R34Error:
                    j.failed += 1
                    continue
                if await send_post(j, p, data):
                    remember(p.id)
                    j.sent += 1
                    state["sent"] += 1
                else:
                    j.failed += 1
                await tick(j)
                await asyncio.sleep(SEND_DELAY)
            else:
                pid += 1
                j.next_pid = pid
                if len(batch) < 100:
                    j.exhausted = True
                    break
                continue
            break
    except R34Error as e:
        j.error = str(e)
    except Exception:
        log.exception("worker crash")
        j.error = "внутренняя ошибка, смотри логи Render"
    finally:
        j.done = True
        state["jobs"] += 1
        state["last_pid"] = j.next_pid
        state["seen"] = list(seen_order)[-3000:]
        if state["newbie"] and state["jobs"] >= 3 and j.sent > 0:
            state["newbie"] = False
        state.save()
        await finish(j)


async def launch(m: Message, tags: list[str], limit: int, start_pid: int):
    global job, job_task
    if busy():
        await m.answer("⏳ Уже качаю. /stop чтобы прервать.")
        return
    bad = banned_in_query(tags)
    if bad:
        await m.answer("⛔ Заблокировано фильтром: <code>" +
                       html.escape(", ".join(bad)) + "</code>")
        return
    limit = max(1, min(limit, HARD_MAX_LIMIT))
    state["last_tags"] = tags
    state.save()
    progress = await m.answer(
        f"🔎 <code>{html.escape(' '.join(tags)) or 'без тегов'}</code>\nЛимит: {limit}",
        reply_markup=kb_stop())
    job = Job(tags=tags, limit=limit, pid=start_pid, chat_id=m.chat.id,
              progress_id=progress.message_id, next_pid=start_pid)
    job_task = asyncio.create_task(worker(job))


# ------------------------- ХЕНДЛЕРЫ -------------------------
@router.message(CommandStart())
async def cmd_start(m: Message):
    if state["newbie"]:
        await m.answer("👋 Привет! Ты тут впервые — включаю <b>режим новичка</b>.\n\n" + HELP,
                       reply_markup=kb_examples())
    else:
        await m.answer("👋 Готов. " + SHORT)


@router.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(HELP, reply_markup=kb_examples() if state["newbie"] else None)


@router.message(Command("expert"))
async def cmd_expert(m: Message):
    state["newbie"] = False
    state.save()
    await m.answer("✅ Режим новичка выключен. Вернуть: /newbie")


@router.message(Command("newbie"))
async def cmd_newbie(m: Message):
    state["newbie"] = True
    state.save()
    await m.answer("🐣 Режим новичка включён.\n\n" + HELP, reply_markup=kb_examples())


@router.message(Command("limit"))
async def cmd_limit(m: Message, command: CommandObject):
    if not (command.args or "").strip().isdigit():
        await m.answer(f"Сейчас: <b>{state['limit']}</b>. Пример: <code>/limit 50</code> "
                       f"(максимум {HARD_MAX_LIMIT})")
        return
    state["limit"] = max(1, min(int(command.args.strip()), HARD_MAX_LIMIT))
    state.save()
    await m.answer(f"👌 Лимит: <b>{state['limit']}</b> постов за запрос")


@router.message(Command("mode"))
async def cmd_mode(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    if arg not in ("file", "photo"):
        await m.answer("Сейчас: <b>{}</b>\n<code>/mode file</code> — оригинал документом "
                       "(без потери качества)\n<code>/mode photo</code> — картинкой в ленте "
                       "(Telegram жмёт)".format(state["mode"]))
        return
    state["mode"] = arg
    state.save()
    await m.answer(f"👌 Режим отправки: <b>{arg}</b>")


@router.message(Command("video"))
async def cmd_video(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        await m.answer(f"Видео сейчас: <b>{'on' if state['video'] else 'off'}</b>. "
                       "<code>/video on</code> / <code>/video off</code>")
        return
    state["video"] = arg == "on"
    state.save()
    await m.answer(f"👌 Видео: <b>{arg}</b>")


@router.message(Command("notify"))
async def cmd_notify(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        await m.answer(f"Уведомления: <b>{'on' if state['notify'] else 'off'}</b>. "
                       "<code>/notify off</code> — тихая пачка без звука.")
        return
    state["notify"] = arg == "on"
    state.save()
    await m.answer(f"👌 Уведомления: <b>{arg}</b>")


@router.message(Command("settings"))
async def cmd_settings(m: Message):
    await m.answer(
        "<b>Настройки</b>\n"
        f"Лимит: <b>{state['limit']}</b>\n"
        f"Отправка: <b>{state['mode']}</b>\n"
        f"Видео: <b>{'on' if state['video'] else 'off'}</b>\n"
        f"Уведомления: <b>{'on' if state['notify'] else 'off'}</b>\n"
        f"Режим новичка: <b>{'on' if state['newbie'] else 'off'}</b>\n"
        f"Макс. размер файла: <b>{MAX_FILE_MB} МБ</b>")


@router.message(Command("stats"))
async def cmd_stats(m: Message):
    await m.answer(
        "<b>Статистика</b>\n"
        f"Задач: <b>{state['jobs']}</b>\n"
        f"Отправлено файлов: <b>{state['sent']}</b>\n"
        f"В памяти дедупа: <b>{len(seen)}</b>\n"
        f"Аптайм: <b>{int((time.time() - START_TS) / 60)} мин</b>")


@router.message(Command("stop"))
async def cmd_stop(m: Message):
    if busy():
        job.cancel.set()
        await m.answer("⏹ Останавливаю…")
    else:
        await m.answer("Сейчас ничего не качается.")


@router.message(Command("random"))
async def cmd_random(m: Message, command: CommandObject):
    await random_post(m, parse_tags(command.args or ""))


async def random_post(m: Message, tags: list[str]):
    if busy():
        await m.answer("⏳ Занят, /stop")
        return
    bad = banned_in_query(tags)
    if bad:
        await m.answer("⛔ Заблокировано фильтром: <code>" + html.escape(", ".join(bad)) + "</code>")
        return
    try:
        batch = await client.search(tags, pid=random.randint(0, 9))
    except R34Error as e:
        await m.answer("⚠️ " + html.escape(str(e)))
        return
    pool = [p for p in batch
            if not post_is_banned(p.tags)
            and (state["video"] or p.ext not in VIDEO_EXT)
            and p.ext in (IMAGE_EXT | ANIM_EXT | VIDEO_EXT)]
    if not pool:
        await m.answer("🎲 Ничего не попалось, попробуй ещё раз.")
        return
    p = random.choice(pool)
    j = Job(tags=tags, limit=1, pid=0, chat_id=m.chat.id)
    try:
        data = await client.fetch(p.file_url)
    except R34Error as e:
        await m.answer("⚠️ " + html.escape(str(e)))
        return
    if await send_post(j, p, data):
        remember(p.id)
        state["sent"] += 1
        state.save()
    else:
        await m.answer("⚠️ Не смог отправить файл.")


@router.message(F.text & ~F.text.startswith("/"))
async def on_tags(m: Message):
    tags = parse_tags(m.text)
    if not tags:
        await m.answer("Не вижу тегов. Пример: <code>cat_ears -video</code>")
        return
    await launch(m, tags, state["limit"], 0)


@router.callback_query(F.data == "stop")
async def cb_stop(cb: CallbackQuery):
    if busy():
        job.cancel.set()
        await cb.answer("Останавливаю…")
    else:
        await cb.answer("Уже не качается")


@router.callback_query(F.data == "more")
async def cb_more(cb: CallbackQuery):
    await cb.answer()
    if not state["last_tags"] and not state["last_pid"]:
        await cb.message.answer("Нечего продолжать — пришли теги.")
        return
    await launch(cb.message, list(state["last_tags"]), state["limit"], int(state["last_pid"]))


@router.callback_query(F.data == "rand")
async def cb_rand(cb: CallbackQuery):
    await cb.answer("🎲")
    await random_post(cb.message, list(state["last_tags"]))


@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(HELP)


@router.callback_query(F.data == "newbie_off")
async def cb_newbie_off(cb: CallbackQuery):
    state["newbie"] = False
    state.save()
    await cb.answer("Подсказки выключены")
    await cb.message.answer("✅ Готово. " + SHORT)


@router.callback_query(F.data.startswith("ex:"))
async def cb_example(cb: CallbackQuery):
    await cb.answer()
    await launch(cb.message, parse_tags(cb.data.split(":", 1)[1]), state["limit"], 0)


# ------------------------- ЖИЗНЕННЫЙ ЦИКЛ -------------------------
async def self_ping():
    while True:
        await asyncio.sleep(600)
        with suppress(Exception):
            async with session.get(PUBLIC_URL + "/health",
                                   timeout=aiohttp.ClientTimeout(total=20)) as r:
                await r.read()


async def on_startup(bot: Bot):
    global session, client, ping_task
    session = aiohttp.ClientSession(headers={"User-Agent": UA},
                                    timeout=aiohttp.ClientTimeout(total=90))
    client = R34Client(session, R34_USER_ID, R34_API_KEY, max_file_mb=MAX_FILE_MB)
    if USE_WEBHOOK:
        url = PUBLIC_URL + WEBHOOK_PATH
        await bot.set_webhook(url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True,
                              allowed_updates=["message", "callback_query"])
        log.info("webhook set: %s", url)
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("polling mode")
    if SELF_PING and PUBLIC_URL:
        ping_task = asyncio.create_task(self_ping())
    me = await bot.get_me()
    log.info("started as @%s, owner=%s", me.username, ALLOWED_USER_ID)


async def on_shutdown():
    global ping_task
    if ping_task:
        ping_task.cancel()
    if busy():
        job.cancel.set()
        if job_task:
            with suppress(Exception):
                await asyncio.wait_for(asyncio.shield(job_task), timeout=10)
    state["seen"] = list(seen_order)[-3000:]
    state.save()
    if session:
        await session.close()


async def health(_request: web.Request):
    return web.json_response({
        "ok": True,
        "uptime_sec": int(time.time() - START_TS),
        "busy": busy(),
        "sent_total": state["sent"],
    })


def main():
    global bot
    if not BOT_TOKEN:
        raise SystemExit("Нет BOT_TOKEN в переменных окружения")
    if not ALLOWED_USER_ID:
        raise SystemExit("Нет ALLOWED_USER_ID в переменных окружения")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(
        parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    if USE_WEBHOOK:
        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        SimpleRequestHandler(dispatcher=dp, bot=bot,
                             secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)
        web.run_app(app, host="0.0.0.0", port=PORT)
    else:
        asyncio.run(dp.start_polling(bot, allowed_updates=["message", "callback_query"]))


if __name__ == "__main__":
    main()
