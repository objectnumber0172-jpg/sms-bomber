import asyncio
import io
import logging
import re
import time
from logging.handlers import RotatingFileHandler
from typing import Optional

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)

# ==================== КОНФИГ ====================
BOT_TOKEN = "8914080279:AAEecpnCmwui0s20RSqdsBJ6JF7GM-0W8P8"
DB_PATH = "dating.db"

SCAN_USER = "797431055"
SCAN_SECRET = "wyBCXQdVQcfZGa4HKCcr262XneW6bcBt"
SE_IMAGE_URL = "https://api.sightengine.com/1.0/check.json"
SE_TEXT_URL = "https://api.sightengine.com/1.0/text/check.json"
IMAGE_MODELS = "nudity-2.1,gore-2.0,offensive"
THRESHOLD = 0.7
SE_CONCURRENCY = 8          # максимум одновременных запросов к Sightengine
SE_TIMEOUT = 25

OWNER_ID = 8544445592

# Антифлуд: не больше N сообщений за WINDOW секунд
FLOOD_LIMIT = 15
FLOOD_WINDOW = 10

# ==================== ЛОГИ ====================
log = logging.getLogger("dating")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
_fh = RotatingFileHandler("bot.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
log.addHandler(_fh)
log.addHandler(_sh)

# ==================== ИНИЦИАЛИЗАЦИЯ ====================
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

db: Optional[aiosqlite.Connection] = None
http: Optional[aiohttp.ClientSession] = None
se_sem = asyncio.Semaphore(SE_CONCURRENCY)
chat_partners: dict[int, int] = {}
flood_state: dict[int, list[float]] = {}
photo_hashes: dict[str, int] = {}   # file_unique_id -> reason, кэш вердиктов

# ==================== БД ====================
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;
PRAGMA mmap_size=268435456;

CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    age INTEGER NOT NULL,
    gender TEXT NOT NULL,
    city TEXT NOT NULL,
    target TEXT NOT NULL,
    bio TEXT DEFAULT '',
    photo_id TEXT DEFAULT '',
    username TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at INTEGER DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS likes (
    from_id INTEGER, to_id INTEGER,
    ts INTEGER DEFAULT (strftime('%s','now')),
    PRIMARY KEY (from_id, to_id)
);
CREATE TABLE IF NOT EXISTS matches (
    user1 INTEGER, user2 INTEGER,
    ts INTEGER DEFAULT (strftime('%s','now')),
    PRIMARY KEY (user1, user2)
);
CREATE TABLE IF NOT EXISTS blocks (
    from_id INTEGER, to_id INTEGER,
    PRIMARY KEY (from_id, to_id)
);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(active);
CREATE INDEX IF NOT EXISTS idx_likes_to ON likes(to_id);
CREATE INDEX IF NOT EXISTS idx_matches_u1 ON matches(user1);
CREATE INDEX IF NOT EXISTS idx_matches_u2 ON matches(user2);
"""

async def open_db() -> None:
    global db
    db = await aiosqlite.connect(DB_PATH, isolation_level=None)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)

async def get_user(uid: int):
    cur = await db.execute("SELECT * FROM users WHERE tg_id=?", (uid,))
    return await cur.fetchone()

async def save_user(uid: int, data: dict) -> None:
    await db.execute(
        """INSERT OR REPLACE INTO users
           (tg_id, name, age, gender, city, target, bio, photo_id, username, active)
           VALUES (?,?,?,?,?,?,?,?,?,1)""",
        (uid, data["name"], data["age"], data["gender"], data["city"],
         data["target"], data.get("bio", ""), data.get("photo_id", ""),
         data.get("username", "")),
    )

# ==================== SIGHTENGINE ====================
async def se_image(image_bytes: bytes) -> Optional[str]:
    form = aiohttp.FormData()
    form.add_field("models", IMAGE_MODELS)
    form.add_field("api_user", SCAN_USER)
    form.add_field("api_secret", SCAN_SECRET)
    form.add_field("media", image_bytes, filename="photo.jpg", content_type="image/jpeg")
    try:
        async with se_sem:
            async with http.post(SE_IMAGE_URL, data=form,
                                 timeout=aiohttp.ClientTimeout(total=SE_TIMEOUT)) as r:
                res = await r.json()
    except Exception as e:
        log.warning("SE image: %s", e)
        return None
    if res.get("status") != "success":
        return None

    nudity = res.get("nudity", {})
    for cls in ("sexual_activity", "sexual_display", "erotica", "very_suggestive"):
        if nudity.get(cls, 0) >= THRESHOLD:
            return f"nudity:{cls}"

    gore = res.get("gore", {}).get("classes", {})
    for cls in ("very_bloody", "body_organ", "serious_injury", "corpse"):
        if gore.get(cls, 0) >= THRESHOLD:
            return f"gore:{cls}"

    if res.get("offensive", {}).get("prob", 0) >= THRESHOLD:
        return "offensive"
    return None

async def se_text(text: str) -> Optional[str]:
    if not text:
        return None
    if not re.search(r"(https?://|www\.|t\.me/|@[A-Za-z0-9_]{3,})", text):
        return None
    payload = {
        "text": text, "mode": "rules", "lang": "ru",
        "api_user": SCAN_USER, "api_secret": SCAN_SECRET,
    }
    try:
        async with se_sem:
            async with http.post(SE_TEXT_URL, data=payload,
                                 timeout=aiohttp.ClientTimeout(total=SE_TIMEOUT)) as r:
                res = await r.json()
    except Exception as e:
        log.warning("SE text: %s", e)
        return None
    if res.get("status") != "success":
        return None
    if res.get("link", {}).get("matches"):
        return "link"
    return None

# ==================== БЛОКИРОВКА ====================
async def block_user(uid: int, reason: str) -> None:
    await db.execute("UPDATE users SET active=0 WHERE tg_id=?", (uid,))
    log.info("BLOCK uid=%s reason=%s", uid, reason)

    try:
        await bot.send_message(OWNER_ID,
            f"🚫 <b>Заблокирован</b>\nID: <code>{uid}</code>\nПричина: <code>{reason}</code>")
    except Exception as e:
        log.warning("owner notify: %s", e)

    partner = chat_partners.pop(uid, None)
    if partner:
        chat_partners.pop(partner, None)
        try:
            await bot.send_message(partner, "Собеседник заблокирован за нарушение.")
        except Exception:
            pass

    try:
        await bot.send_message(uid, "🚫 Доступ запрещён.")
    except Exception:
        pass

# ==================== АНТИФЛУД ====================
def flood_ok(uid: int) -> bool:
    now = time.monotonic()
    bucket = flood_state.setdefault(uid, [])
    bucket[:] = [t for t in bucket if now - t < FLOOD_WINDOW]
    if len(bucket) >= FLOOD_LIMIT:
        return False
    bucket.append(now)
    return True

# ==================== КЛАВИАТУРЫ ====================
def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔍 Поиск"), KeyboardButton(text="👤 Моя анкета")],
        [KeyboardButton(text="❤️ Мэтчи"), KeyboardButton(text="⚙️ Изменить")],
        [KeyboardButton(text="❓ Помощь")],
    ], resize_keyboard=True)

def gender_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Парень", callback_data="gender:m")],
        [InlineKeyboardButton(text="Девушка", callback_data="gender:f")],
    ])

def target_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Парней", callback_data="target:m")],
        [InlineKeyboardButton(text="Девушек", callback_data="target:f")],
        [InlineKeyboardButton(text="Всех", callback_data="target:all")],
    ])

def profile_actions(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❤️ Лайк", callback_data=f"like:{uid}"),
         InlineKeyboardButton(text="👎 Пропуск", callback_data=f"skip:{uid}")],
        [InlineKeyboardButton(text="🚫 Жалоба", callback_data=f"report:{uid}")],
    ])

def chat_kb(pid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Завершить", callback_data=f"stopchat:{pid}")],
    ])

# ==================== СОСТОЯНИЯ ====================
class Reg(StatesGroup):
    name = State(); age = State(); gender = State(); city = State()
    target = State(); bio = State(); photo = State()

# ==================== ФОРМАТ ====================
def fmt_target(t: str) -> str:
    return {"m": "парней", "f": "девушек", "all": "всех"}.get(t, t)

def profile_text(u) -> str:
    return (f"<b>{u['name']}</b>, {u['age']}\n🏙 {u['city']}\n"
            f"🔎 Ищу: {fmt_target(u['target'])}\n\n{u['bio'] or '—'}")

# ==================== РЕГИСТРАЦИЯ ====================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    u = await get_user(uid)
    if u:
        if u["active"] == 0:
            await message.answer("🚫 Доступ запрещён.")
            return
        await message.answer("С возвращением! Жми «🔍 Поиск».", reply_markup=main_menu())
        return
    await message.answer("Привет! Бот знакомств 18+.\n\nКак тебя зовут?",
                         reply_markup=ReplyKeyboardRemove())
    await state.set_state(Reg.name)

@router.message(Reg.name)
async def reg_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not (2 <= len(name) <= 30):
        await message.answer("Имя от 2 до 30 символов:")
        return
    await state.update_data(name=name)
    await message.answer(f"{name}, сколько тебе лет?")
    await state.set_state(Reg.age)

@router.message(Reg.age)
async def reg_age(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not txt.isdigit() or not (18 <= int(txt) <= 99):
        await message.answer("Возраст от 18 до 99:")
        return
    await state.update_data(age=int(txt))
    await message.answer("Твой пол?", reply_markup=gender_kb())
    await state.set_state(Reg.gender)

@router.callback_query(Reg.gender, F.data.startswith("gender:"))
async def reg_gender(cb: CallbackQuery, state: FSMContext):
    await state.update_data(gender=cb.data.split(":")[1])
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("Из какого ты города?")
    await state.set_state(Reg.city)
    await cb.answer()

@router.message(Reg.city)
async def reg_city(message: Message, state: FSMContext):
    city = (message.text or "").strip()
    if not (2 <= len(city) <= 40):
        await message.answer("Город от 2 до 40 символов:")
        return
    await state.update_data(city=city)
    await message.answer("Кого ищем?", reply_markup=target_kb())
    await state.set_state(Reg.target)

@router.callback_query(Reg.target, F.data.startswith("target:"))
async def reg_target(cb: CallbackQuery, state: FSMContext):
    await state.update_data(target=cb.data.split(":")[1])
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("О себе (до 300 символов). /skip — пропустить.")
    await state.set_state(Reg.bio)
    await cb.answer()

@router.message(Reg.bio, Command("skip"))
async def reg_bio_skip(message: Message, state: FSMContext):
    await state.update_data(bio="")
    await message.answer("Отправь фото. /skip — пропустить.")
    await state.set_state(Reg.photo)

@router.message(Reg.bio)
async def reg_bio(message: Message, state: FSMContext):
    bio = (message.text or "").strip()
    if len(bio) > 300:
        await message.answer("Максимум 300 символов.")
        return
    await state.update_data(bio=bio)
    await message.answer("Отправь фото. /skip — пропустить.")
    await state.set_state(Reg.photo)

@router.message(Reg.photo, Command("skip"))
async def reg_photo_skip(message: Message, state: FSMContext):
    data = await state.get_data()
    data["photo_id"] = ""
    data["username"] = message.from_user.username or ""
    await save_user(message.from_user.id, data)
    await state.clear()
    await message.answer("Готово! «🔍 Поиск».", reply_markup=main_menu())

@router.message(Reg.photo, F.photo)
async def reg_photo(message: Message, state: FSMContext):
    uid = message.from_user.id
    fid = message.photo[-1].file_unique_id
    reason = photo_hashes.get(fid)
    if reason is None:
        file = await bot.get_file(message.photo[-1].file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        reason = await se_image(buf.getvalue()) or ""
        photo_hashes[fid] = reason
    if reason:
        await block_user(uid, reason)
        await state.clear()
        return
    data = await state.get_data()
    data["photo_id"] = message.photo[-1].file_id
    data["username"] = message.from_user.username or ""
    await save_user(uid, data)
    await state.clear()
    await message.answer("Готово! «🔍 Поиск».", reply_markup=main_menu())

@router.message(Reg.photo)
async def reg_photo_invalid(message: Message):
    await message.answer("Нужно фото или /skip.")

# ==================== ПРОФИЛЬ ====================
@router.message(Command("me"))
@router.message(F.text == "👤 Моя анкета")
async def cmd_me(message: Message):
    u = await get_user(message.from_user.id)
    if not u:
        await message.answer("Сначала /start")
        return
    txt = profile_text(u)
    if u["photo_id"]:
        await message.answer_photo(u["photo_id"], caption=txt, reply_markup=main_menu())
    else:
        await message.answer(txt, reply_markup=main_menu())

@router.message(Command("edit"))
@router.message(F.text == "⚙️ Изменить")
async def cmd_edit(message: Message, state: FSMContext):
    if not await get_user(message.from_user.id):
        await message.answer("Сначала /start")
        return
    await message.answer("Заново. Как тебя зовут?")
    await state.set_state(Reg.name)

# ==================== ПОИСК ====================
@router.message(Command("search"))
@router.message(F.text == "🔍 Поиск")
async def cmd_search(message: Message):
    uid = message.from_user.id
    if uid in chat_partners:
        await message.answer("Сначала заверши чат — /stop")
        return
    me = await get_user(uid)
    if not me or me["active"] == 0:
        await message.answer("🚫 Недоступно.")
        return

    cur = await db.execute("""
        SELECT tg_id, name, age, city, bio, photo_id
        FROM users u
        WHERE u.tg_id != ? AND u.active = 1
          AND u.tg_id NOT IN (SELECT to_id FROM likes WHERE from_id = ?)
          AND u.tg_id NOT IN (SELECT user1 FROM matches WHERE user2 = ?)
          AND u.tg_id NOT IN (SELECT user2 FROM matches WHERE user1 = ?)
          AND u.tg_id NOT IN (SELECT to_id FROM blocks WHERE from_id = ?)
          AND u.tg_id NOT IN (SELECT from_id FROM blocks WHERE to_id = ?)
          AND (u.target = 'all' OR u.target = ?)
          AND (? = 'all' OR u.gender = ?)
        ORDER BY RANDOM() LIMIT 1
    """, (uid, uid, uid, uid, uid, uid, me["gender"], me["target"], me["target"]))
    row = await cur.fetchone()
    if not row:
        await message.answer("Пока никого нет.")
        return

    caption = f"<b>{row['name']}</b>, {row['age']}\n🏙 {row['city']}\n\n{row['bio'] or '—'}"
    kb = profile_actions(row["tg_id"])
    if row["photo_id"]:
        await message.answer_photo(row["photo_id"], caption=caption, reply_markup=kb)
    else:
        await message.answer(caption, reply_markup=kb)

# ==================== ЛАЙК / ПРОПУСК / ЖАЛОБА ====================
@router.callback_query(F.data.startswith("like:"))
async def cb_like(cb: CallbackQuery):
    from_id = cb.from_user.id
    to_id = int(cb.data.split(":")[1])
    if from_id == to_id:
        await cb.answer("Нельзя себя", show_alert=True)
        return
    await db.execute("INSERT OR IGNORE INTO likes (from_id, to_id) VALUES (?,?)",
                     (from_id, to_id))
    cur = await db.execute("SELECT 1 FROM likes WHERE from_id=? AND to_id=?",
                           (to_id, from_id))
    mutual = await cur.fetchone()
    if mutual:
        u1, u2 = sorted((from_id, to_id))
        await db.execute("INSERT OR IGNORE INTO matches (user1, user2) VALUES (?,?)",
                         (u1, u2))
    await cb.message.edit_reply_markup(reply_markup=None)
    if mutual:
        await cb.message.answer("💞 Взаимно! «❤️ Мэтчи».")
        try:
            await bot.send_message(to_id, "💞 Новый мэтч! «❤️ Мэтчи».")
        except Exception:
            pass
    else:
        await cb.message.answer("Лайк отправлен. /search — следующий.")
    await cb.answer()

@router.callback_query(F.data.startswith("skip:"))
async def cb_skip(cb: CallbackQuery):
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("Пропущено. /search — следующий.")
    await cb.answer()

@router.callback_query(F.data.startswith("report:"))
async def cb_report(cb: CallbackQuery):
    to_id = int(cb.data.split(":")[1])
    await db.execute("INSERT OR IGNORE INTO blocks (from_id, to_id) VALUES (?,?)",
                     (cb.from_user.id, to_id))
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("Жалоба принята.")
    await cb.answer()

# ==================== МЭТЧИ ====================
@router.message(Command("matches"))
@router.message(F.text == "❤️ Мэтчи")
async def cmd_matches(message: Message):
    uid = message.from_user.id
    cur = await db.execute("""
        SELECT u.tg_id, u.name, u.age, u.city
        FROM matches m
        JOIN users u ON u.tg_id = CASE WHEN m.user1=? THEN m.user2 ELSE m.user1 END
        WHERE m.user1=? OR m.user2=?
    """, (uid, uid, uid))
    rows = await cur.fetchall()
    if not rows:
        await message.answer("Пока нет мэтчей.")
        return
    await message.answer("Твои мэтчи:")
    for r in rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Чат", callback_data=f"chat:{r['tg_id']}")],
        ])
        await message.answer(f"<b>{r['name']}</b>, {r['age']} — {r['city']}", reply_markup=kb)

# ==================== ЧАТ ====================
@router.callback_query(F.data.startswith("chat:"))
async def cb_start_chat(cb: CallbackQuery):
    uid = cb.from_user.id
    partner = int(cb.data.split(":")[1])
    if uid in chat_partners:
        await cb.answer("Ты уже в чате. /stop", show_alert=True)
        return
    if partner in chat_partners and chat_partners[partner] != uid:
        await cb.answer("Занят.", show_alert=True)
        return
    chat_partners[uid] = partner
    chat_partners[partner] = uid
    await cb.message.answer("💬 Чат открыт. /stop — завершить.", reply_markup=chat_kb(partner))
    try:
        await bot.send_message(partner, "💬 С тобой хотят пообщаться. /stop — завершить.",
                               reply_markup=chat_kb(uid))
    except Exception:
        pass
    await cb.answer()

@router.callback_query(F.data.startswith("stopchat:"))
async def cb_stop_chat(cb: CallbackQuery):
    await _stop_chat(cb.from_user.id)
    await cb.message.answer("Чат завершён.", reply_markup=main_menu())
    await cb.answer()

@router.message(Command("stop"))
async def cmd_stop(message: Message):
    if message.from_user.id not in chat_partners:
        await message.answer("Ты не в чате.")
        return
    await _stop_chat(message.from_user.id)
    await message.answer("Чат завершён.", reply_markup=main_menu())

async def _stop_chat(uid: int) -> None:
    partner = chat_partners.pop(uid, None)
    if partner:
        chat_partners.pop(partner, None)
        try:
            await bot.send_message(partner, "Собеседник завершил чат.",
                                   reply_markup=main_menu())
        except Exception:
            pass

# ==================== РЕЛЕЙ ====================
@router.message(F.chat.type == "private")
async def relay(message: Message, state: FSMContext):
    uid = message.from_user.id
    if await state.get_state() is not None:
        return

    if not flood_ok(uid):
        await message.answer("Слишком часто. Подожди.")
        return

    partner = chat_partners.get(uid)
    if not partner:
        await message.answer("Не понимаю. /help")
        return

    if message.text:
        reason = await se_text(message.text)
        if reason:
            await block_user(uid, reason)
            return

    if message.photo:
        fid = message.photo[-1].file_unique_id
        reason = photo_hashes.get(fid)
        if reason is None:
            file = await bot.get_file(message.photo[-1].file_id)
            buf = io.BytesIO()
            await bot.download_file(file.file_path, buf)
            reason = await se_image(buf.getvalue()) or ""
            photo_hashes[fid] = reason
        if reason:
            await block_user(uid, reason)
            return

    try:
        if message.text:
            await bot.send_message(partner, f"💬 {message.text}")
        elif message.photo:
            await bot.send_photo(partner, message.photo[-1].file_id,
                                 caption=message.caption or "")
        elif message.voice:
            await bot.send_voice(partner, message.voice.file_id)
        elif message.sticker:
            await bot.send_sticker(partner, message.sticker.file_id)
        else:
            await bot.send_message(partner, "📎 Вложение")
    except Exception as e:
        log.warning("relay: %s", e)

# ==================== ПРОЧЕЕ ====================
@router.message(Command("help"))
@router.message(F.text == "❓ Помощь")
async def cmd_help(message: Message):
    await message.answer(
        "/start — регистрация\n/me — анкета\n/edit — изменить\n"
        "/search — искать\n/matches — мэтчи\n/stop — завершить чат\n/delete — удалить",
        reply_markup=main_menu())

@router.message(Command("delete"))
async def cmd_delete(message: Message):
    uid = message.from_user.id
    if uid in chat_partners:
        await _stop_chat(uid)
    await db.execute("UPDATE users SET active=0 WHERE tg_id=?", (uid,))
    await db.execute("DELETE FROM likes WHERE from_id=? OR to_id=?", (uid, uid))
    await db.execute("DELETE FROM matches WHERE user1=? OR user2=?", (uid, uid))
    await message.answer("Анкета удалена.", reply_markup=ReplyKeyboardRemove())

# ==================== КОМАНДЫ ВЛАДЕЛЬЦА ====================
@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    cur = await db.execute("SELECT COUNT(*) c FROM users WHERE active=1")
    users = (await cur.fetchone())["c"]
    cur = await db.execute("SELECT COUNT(*) c FROM matches")
    matches = (await cur.fetchone())["c"]
    cur = await db.execute("SELECT COUNT(*) c FROM likes")
    likes = (await cur.fetchone())["c"]
    await message.answer(
        f"👥 Активных: <b>{users}</b>\n"
        f"❤️ Лайков: <b>{likes}</b>\n"
        f"💞 Мэтчей: <b>{matches}</b>\n"
        f"💬 В чатах: <b>{len(chat_partners)//2}</b>"
    )

@router.message(Command("unban"))
async def cmd_unban(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /unban <tg_id>")
        return
    await db.execute("UPDATE users SET active=1 WHERE tg_id=?", (int(parts[1]),))
    await message.answer(f"Разбанен: {parts[1]}")

@router.message(Command("ban"))
async def cmd_ban(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /ban <tg_id>")
        return
    await block_user(int(parts[1]), "manual")
    await message.answer(f"Забанен: {parts[1]}")

# ==================== ФОНОВЫЕ ЗАДАЧИ ====================
async def cleaner_task() -> None:
    """Чистит кэш фото-хэшей и антифлуда раз в час."""
    while True:
        await asyncio.sleep(3600)
        photo_hashes.clear()
        flood_state.clear()
        log.info("Cache cleared. users_in_chat=%d", len(chat_partners))

# ==================== ЗАПУСК ====================
async def main() -> None:
    global http
    await open_db()
    http = aiohttp.ClientSession()
    asyncio.create_task(cleaner_task())
    log.info("Bot started")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await http.close()
        if db:
            await db.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped")