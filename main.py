import asyncio
import io
import logging
import re
from datetime import datetime
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
SE_CONCURRENCY = 8
SE_TIMEOUT = 25

OWNER_ID = 8544445592

FLOOD_LIMIT = 15
FLOOD_WINDOW = 10
MIN_AGE = 15
MAX_AGE = 35

# ==================== КАСТОМНЫЕ ЭМОДЗИ ====================
EMOJI_MAIN = "5253665513283817487"
EMOJI_NAME = "5253929503448667138"
EMOJI_AGE = "5256099995236471427"
EMOJI_BOY = "5388784600300401111"
EMOJI_GIRL = "5390923682992370246"
EMOJI_OK = "5255755135132405848"
EMOJI_WELCOME = "5255863552991858476"
EMOJI_SEARCH = "5253510237331164533"
EMOJI_PROFILE = "5255980543606034669"
EMOJI_DATE = "5253903445882082614"
EMOJI_LINK = "5255934999772828798"
EMOJI_ANON = "5256151393110104209"
EMOJI_CANCEL = "5253615193446977913"
EMOJI_END = "5206510891247371052"

def ce(eid: str, e: str) -> str:
    """Обернуть эмодзи в тег кастомного эмодзи."""
    return f'<tg-emoji emoji-id="{eid}">{e}</tg-emoji>'

# ==================== ЛОГИ ====================
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("dating")

# ==================== БОТ ====================
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

db: Optional[aiosqlite.Connection] = None
http: Optional[aiohttp.ClientSession] = None
se_sem = asyncio.Semaphore(SE_CONCURRENCY)
chat_partners: dict[int, int] = {}
flood_state: dict[int, list] = {}
photo_cache: dict[str, str] = {}
BOT_USERNAME = ""

# ==================== РУССКИЕ ИМЕНА ====================
RUSSIAN_NAMES = {
    "александр","алексей","анатолий","андрей","антон","аркадий","арсений","артем","артём","артур",
    "борис","вадим","валентин","валерий","василий","виктор","виталий","владимир","владислав","вячеслав",
    "геннадий","георгий","глеб","григорий","даниил","данил","денис","дмитрий","евгений","егор",
    "иван","игорь","илья","кирилл","константин","лев","леонид","максим","марк","матвей",
    "михаил","никита","николай","олег","павел","петр","пётр","роман","руслан","сергей",
    "станислав","степан","тимофей","тимур","федор","фёдор","эдуард","юрий","ярослав",
    "александра","алина","алиса","алла","анастасия","ангелина","анна","антонина","валентина","валерия",
    "варвара","василиса","вера","вероника","виктория","галина","дарья","диана","евгения","екатерина",
    "елена","елизавета","жанна","зинаида","инна","ирина","карина","кристина","ксения","лариса",
    "лидия","любовь","людмила","маргарита","марина","мария","надежда","наталья","нина","оксана",
    "ольга","полина","раиса","светлана","софия","софья","тамара","татьяна","ульяна","юлия","яна",
}

def valid_name(raw: str) -> bool:
    name = (raw or "").strip()
    if not (2 <= len(name) <= 20):
        return False
    if not re.fullmatch(r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?", name):
        return False
    return name.lower() in RUSSIAN_NAMES

# ==================== СОСТОЯНИЯ ====================
class Reg(StatesGroup):
    name = State()
    age = State()

class Settings(StatesGroup):
    name = State()
    age = State()
    photo = State()
    target = State()

class Anon(StatesGroup):
    typing = State()

# ==================== БД ====================
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    age INTEGER NOT NULL,
    gender TEXT NOT NULL,
    target TEXT NOT NULL,
    photo_id TEXT DEFAULT '',
    username TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS blocks (
    from_id INTEGER, to_id INTEGER,
    PRIMARY KEY (from_id, to_id)
);
CREATE TABLE IF NOT EXISTS anon_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id INTEGER NOT NULL,
    to_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    ts INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(active);
CREATE INDEX IF NOT EXISTS idx_users_target ON users(target, gender);
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
           (tg_id, name, age, gender, target, photo_id, username, active, created_at)
           VALUES (?,?,?,?,?,?,?,1,?)""",
        (uid, data["name"], data["age"], data["gender"], data["target"],
         data.get("photo_id", ""), data.get("username", ""),
         data.get("created_at", 0)),
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
    n = res.get("nudity", {})
    for c in ("sexual_activity", "sexual_display", "erotica", "very_suggestive"):
        if n.get(c, 0) >= THRESHOLD:
            return f"nudity:{c}"
    g = res.get("gore", {}).get("classes", {})
    for c in ("very_bloody", "body_organ", "serious_injury", "corpse"):
        if g.get(c, 0) >= THRESHOLD:
            return f"gore:{c}"
    if res.get("offensive", {}).get("prob", 0) >= THRESHOLD:
        return "offensive"
    return None

async def se_text(text: str) -> Optional[str]:
    if not text:
        return None
    if not re.search(r"(https?://|www\.|t\.me/|@[A-Za-z0-9_]{3,})", text):
        return None
    payload = {"text": text, "mode": "rules", "lang": "ru",
               "api_user": SCAN_USER, "api_secret": SCAN_SECRET}
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

# ==================== БЛОК ====================
async def block_user(uid: int, reason: str) -> None:
    await db.execute("UPDATE users SET active=0 WHERE tg_id=?", (uid,))
    log.info("BLOCK uid=%s reason=%s", uid, reason)
    try:
        await bot.send_message(OWNER_ID,
            f"🚫 <b>Заблокирован</b>\nID: <code>{uid}</code>\nПричина: <code>{reason}</code>")
    except Exception:
        pass
    partner = chat_partners.pop(uid, None)
    if partner:
        chat_partners.pop(partner, None)
        try:
            await bot.send_message(partner, "Собеседник заблокирован за нарушение.",
                                   reply_markup=main_menu())
        except Exception:
            pass
    try:
        await bot.send_message(uid, "🚫 Доступ запрещён.")
    except Exception:
        pass

# ==================== АНТИФЛУД ====================
def flood_ok(uid: int) -> bool:
    import time
    now = time.monotonic()
    b = flood_state.setdefault(uid, [])
    b[:] = [t for t in b if now - t < FLOOD_WINDOW]
    if len(b) >= FLOOD_LIMIT:
        return False
    b.append(now)
    return True

# ==================== КЛАВИАТУРЫ ====================
def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🤨 Поиск"), KeyboardButton(text="🤨 Настроить анкету")],
        [KeyboardButton(text="🤨 Профиль")],
    ], resize_keyboard=True)

def search_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔽 Закончить общение")],
        [KeyboardButton(text="🤨 Пожаловаться")],
    ], resize_keyboard=True)

def settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤨 Фото", callback_data="set:photo")],
        [InlineKeyboardButton(text="🤨 Имя", callback_data="set:name")],
        [InlineKeyboardButton(text="🤨 Возраст", callback_data="set:age")],
        [InlineKeyboardButton(text="🤨 Поиск", callback_data="set:target")],
    ])

def reg_target_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👦 Мальчика", callback_data="reg:target:m")],
        [InlineKeyboardButton(text="👧 Девочку", callback_data="reg:target:f")],
    ])

def settings_target_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👦 Мальчика", callback_data="setg:target:m")],
        [InlineKeyboardButton(text="👧 Девочку", callback_data="setg:target:f")],
    ])

def ok_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Окей", callback_data="reg:done")],
    ])

def start_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤨 Начать использование бота", callback_data="reg:start")],
    ])

def anon_cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤨 Отмена", callback_data="anon:cancel")],
    ])

def anon_confirm_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤨 Отправить", callback_data="anon:send")],
        [InlineKeyboardButton(text="🤨 Отменить", callback_data="anon:cancel")],
        [InlineKeyboardButton(text="🤨 Редактировать", callback_data="anon:edit")],
    ])

def anon_report_menu(qid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤨 Пожаловаться", callback_data=f"anon:report:{qid}")],
    ])

def profile_link_menu(uid: int) -> InlineKeyboardMarkup:
    url = f"https://t.me/{BOT_USERNAME}?start=anon_{uid}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤨 Ссылка для анонимных вопросов", url=url)],
    ])

# ==================== ХЕЛПЕРЫ ====================
def opposite(g: str) -> str:
    return "f" if g == "m" else "m"

def fmt_gender(g: str) -> str:
    return "Парень" if g == "m" else "Девушка"

def fmt_target(t: str) -> str:
    return "Мальчика" if t == "m" else "Девочку"

def fmt_date(ts: int) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")

# ==================== СТАРТ ====================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    # Deep-link: анонимный вопрос
    if arg.startswith("anon_"):
        target_str = arg[5:]
        if not target_str.isdigit():
            await message.answer("Некорректная ссылка.")
            return
        target_uid = int(target_str)
        if target_uid == uid:
            await message.answer("Нельзя задать вопрос самому себе.")
            return
        target = await get_user(target_uid)
        if not target or target["active"] == 0:
            await message.answer("Пользователь недоступен.")
            return
        await state.set_state(Anon.typing)
        await state.update_data(target_uid=target_uid, question="")
        await message.answer(
            f'{ce(EMOJI_ANON, "🤨")} Введите ваш вопрос:',
            reply_markup=ReplyKeyboardRemove(),
        )
        await message.answer("⬇️", reply_markup=anon_cancel_menu())
        return

    # Обычный старт
    u = await get_user(uid)
    if u:
        if u["active"] == 0:
            await message.answer("🚫 Доступ запрещён.")
            return
        await message.answer(
            f'{ce(EMOJI_WELCOME, "🤨")} Добро пожаловать в Познакомимся.\n'
            f'{ce(EMOJI_AGE, "🤨")} Здесь вы сможете найти друзей или пару.',
            reply_markup=main_menu(),
        )
        return

    await message.answer(
        f'{ce(EMOJI_MAIN, "🤨")} Все ваши переписки находятся в безопасности и не будут никому отправлены.\n'
        f'Исключение: проверка жалобы юзера.\n\n'
        f'{ce(EMOJI_MAIN, "🤨")} Начать использование бота',
        reply_markup=start_menu(),
    )

# ==================== РЕГИСТРАЦИЯ ====================
@router.callback_query(F.data == "reg:start")
async def reg_start(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(
        f'{ce(EMOJI_NAME, "🤨")} Ваше имя',
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(Reg.name)
    await cb.answer()

@router.message(Reg.name)
async def reg_name(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not valid_name(raw):
        await message.answer(
            "❌ Это не похоже на русское имя.\n"
            "Введи настоящее имя (например: Александр, Мария)."
        )
        return
    await state.update_data(name=raw.capitalize() if not raw[0].isupper() else raw)
    await message.answer(f'{ce(EMOJI_AGE, "🤨")} Ваш возраст?')
    await state.set_state(Reg.age)

@router.message(Reg.age)
async def reg_age(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not txt.isdigit() or not (MIN_AGE <= int(txt) <= MAX_AGE):
        await message.answer(f"❌ Возраст от {MIN_AGE} до {MAX_AGE}. Введи числом:")
        return
    await state.update_data(age=int(txt))
    await message.answer(
        f'{ce(EMOJI_AGE, "🤨")} Кого ищем?',
        reply_markup=reg_target_menu(),
    )

@router.callback_query(Reg.age, F.data.startswith("reg:target:"))
async def reg_target(cb: CallbackQuery, state: FSMContext):
    target = cb.data.split(":")[-1]  # m / f
    own = opposite(target)
    await state.update_data(target=target, gender=own)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(
        f'{ce(EMOJI_MAIN, "🤨")} Анкета создана. Изменить анкету можно в меню.',
        reply_markup=ok_menu(),
    )
    await cb.answer()

@router.callback_query(F.data == "reg:done")
async def reg_done(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    data = await state.get_data()
    if not data.get("name") or not data.get("age"):
        await cb.answer("Сессия истекла. /start", show_alert=True)
        return
    import time
    data["created_at"] = int(time.time())
    data["username"] = cb.from_user.username or ""
    data.setdefault("photo_id", "")
    await save_user(uid, data)
    await state.clear()
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(
        f'{ce(EMOJI_WELCOME, "🤨")} Добро пожаловать в Познакомимся.\n'
        f'{ce(EMOJI_AGE, "🤨")} Здесь вы сможете найти друзей или пару.',
        reply_markup=main_menu(),
    )
    await cb.answer()

# ==================== ПРОФИЛЬ ====================
@router.message(F.text == "🤨 Профиль")
@router.message(Command("me"))
async def cmd_profile(message: Message):
    uid = message.from_user.id
    u = await get_user(uid)
    if not u:
        await message.answer("Сначала /start")
        return
    text = (
        f'{ce(EMOJI_PROFILE, "🤨")} <b>Ваш профиль:</b>\n'
        f'{ce(EMOJI_NAME, "🤨")} Имя: <b>{u["name"]}</b>\n'
        f'{ce(EMOJI_AGE, "🤨")} Возраст: <b>{u["age"]}</b>\n'
        f'{ce(EMOJI_AGE, "🤨")} Ищу: <b>{fmt_target(u["target"])}</b>\n'
        f'{ce(EMOJI_DATE, "🤨")} Дата регистрации: <b>{fmt_date(u["created_at"])}</b>\n'
        f'{ce(EMOJI_LINK, "🤨")} Ссылка для анонимных вопросов:'
    )
    if u["photo_id"]:
        await message.answer_photo(u["photo_id"], caption=text,
                                   reply_markup=profile_link_menu(uid))
    else:
        await message.answer(text, reply_markup=profile_link_menu(uid))

# ==================== НАСТРОЙКА АНКЕТЫ ====================
@router.message(F.text == "🤨 Настроить анкету")
async def cmd_settings(message: Message):
    if not await get_user(message.from_user.id):
        await message.answer("Сначала /start")
        return
    await message.answer(
        f'{ce(EMOJI_AGE, "🤨")} Что поменяем?',
        reply_markup=settings_menu(),
    )

@router.callback_query(F.data == "set:photo")
async def set_photo(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("Отправь новое фото.")
    await state.set_state(Settings.photo)
    await cb.answer()

@router.callback_query(F.data == "set:name")
async def set_name(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(f'{ce(EMOJI_NAME, "🤨")} Введи новое имя:')
    await state.set_state(Settings.name)
    await cb.answer()

@router.callback_query(F.data == "set:age")
async def set_age(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(f'{ce(EMOJI_AGE, "🤨")} Введи новый возраст ({MIN_AGE}-{MAX_AGE}):')
    await state.set_state(Settings.age)
    await cb.answer()

@router.callback_query(F.data == "set:target")
async def set_target(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("Кого ищем?", reply_markup=settings_target_menu())
    await cb.answer()

@router.callback_query(F.data.startswith("setg:target:"))
async def set_target_do(cb: CallbackQuery):
    target = cb.data.split(":")[-1]
    own = opposite(target)
    await db.execute("UPDATE users SET target=?, gender=? WHERE tg_id=?",
                     (target, own, cb.from_user.id))
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("✅ Готово.", reply_markup=main_menu())
    await cb.answer()

@router.message(Settings.name)
async def set_name_do(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not valid_name(raw):
        await message.answer("❌ Это не похоже на русское имя.")
        return
    await db.execute("UPDATE users SET name=? WHERE tg_id=?",
                     (raw.capitalize() if not raw[0].isupper() else raw,
                      message.from_user.id))
    await state.clear()
    await message.answer("✅ Имя обновлено.", reply_markup=main_menu())

@router.message(Settings.age)
async def set_age_do(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not txt.isdigit() or not (MIN_AGE <= int(txt) <= MAX_AGE):
        await message.answer(f"❌ Возраст от {MIN_AGE} до {MAX_AGE}.")
        return
    await db.execute("UPDATE users SET age=? WHERE tg_id=?",
                     (int(txt), message.from_user.id))
    await state.clear()
    await message.answer("✅ Возраст обновлён.", reply_markup=main_menu())

@router.message(Settings.photo, F.photo)
async def set_photo_do(message: Message, state: FSMContext):
    uid = message.from_user.id
    fid = message.photo[-1].file_unique_id
    reason = photo_cache.get(fid)
    if reason is None:
        file = await bot.get_file(message.photo[-1].file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        reason = await se_image(buf.getvalue()) or ""
        photo_cache[fid] = reason
    if reason:
        await block_user(uid, reason)
        await state.clear()
        return
    await db.execute("UPDATE users SET photo_id=? WHERE tg_id=?",
                     (message.photo[-1].file_id, uid))
    await state.clear()
    await message.answer("✅ Фото обновлено.", reply_markup=main_menu())

@router.message(Settings.photo)
async def set_photo_invalid(message: Message):
    await message.answer("Нужно фото.")

# ==================== ПОИСК / ЧАТ ====================
@router.message(F.text == "🤨 Поиск")
@router.message(Command("search"))
async def cmd_search(message: Message):
    uid = message.from_user.id
    if uid in chat_partners:
        await message.answer("Ты уже в чате. Нажми «🔽 Закончить общение».")
        return
    me = await get_user(uid)
    if not me or me["active"] == 0:
        await message.answer("Сначала /start")
        return

    cur = await db.execute("""
        SELECT tg_id, name, age, photo_id FROM users u
        WHERE u.tg_id != ? AND u.active = 1
          AND u.gender = ? AND u.target = ?
          AND u.tg_id NOT IN (SELECT to_id FROM blocks WHERE from_id = ?)
          AND u.tg_id NOT IN (SELECT from_id FROM blocks WHERE to_id = ?)
        ORDER BY RANDOM() LIMIT 1
    """, (uid, me["target"], me["gender"], uid, uid))
    row = await cur.fetchone()
    if not row:
        await message.answer("Пока никого нет. Попробуй позже.")
        return

    partner = row["tg_id"]
    chat_partners[uid] = partner
    chat_partners[partner] = uid

    # уведомляем обоих
    card_text = f"<b>{row['name']}</b>, {row['age']}\n\n💬 Анонимный чат открыт."
    try:
        if row["photo_id"]:
            await bot.send_photo(uid, row["photo_id"], caption=card_text)
        else:
            await message.answer(card_text)
    except Exception:
        pass

    await message.answer(
        "💬 Чат открыт. Пиши сообщения — они будут пересылаться анонимно.",
        reply_markup=search_menu(),
    )
    try:
        await bot.send_message(
            partner,
            "💬 С тобой хотят пообщаться. Пиши сообщения — они анонимны.",
            reply_markup=search_menu(),
        )
    except Exception:
        pass

@router.message(F.text == "🔽 Закончить общение")
@router.message(Command("stop"))
async def cmd_stop(message: Message):
    uid = message.from_user.id
    if uid not in chat_partners:
        await message.answer("Ты не в чате.", reply_markup=main_menu())
        return
    await _stop_chat(uid)
    await message.answer("Общение завершено.", reply_markup=main_menu())

async def _stop_chat(uid: int) -> None:
    partner = chat_partners.pop(uid, None)
    if partner:
        chat_partners.pop(partner, None)
        try:
            await bot.send_message(partner, "Собеседник завершил общение.",
                                   reply_markup=main_menu())
        except Exception:
            pass

@router.message(F.text == "🤨 Пожаловаться")
async def cmd_report(message: Message):
    uid = message.from_user.id
    partner = chat_partners.get(uid)
    if not partner:
        await message.answer("Ты не в чате.", reply_markup=main_menu())
        return
    await db.execute("INSERT OR IGNORE INTO blocks (from_id, to_id) VALUES (?,?)",
                     (uid, partner))
    try:
        await bot.send_message(OWNER_ID,
            f"🚨 <b>Жалоба в чате</b>\nОт: <code>{uid}</code>\nНа: <code>{partner}</code>")
    except Exception:
        pass
    await _stop_chat(uid)
    await message.answer("Жалоба отправлена. Общение завершено.",
                         reply_markup=main_menu())

# ==================== РЕЛЕЙ СООБЩЕНИЙ ====================
@router.message(F.chat.type == "private")
async def relay(message: Message, state: FSMContext):
    uid = message.from_user.id

    # команды и reply-кнопки в FSM обработаны выше — здесь только обычные
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
        if await se_text(message.text):
            await block_user(uid, "link")
            return

    if message.photo:
        fid = message.photo[-1].file_unique_id
        reason = photo_cache.get(fid)
        if reason is None:
            file = await bot.get_file(message.photo[-1].file_id)
            buf = io.BytesIO()
            await bot.download_file(file.file_path, buf)
            reason = await se_image(buf.getvalue()) or ""
            photo_cache[fid] = reason
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

# ==================== АНОНИМНЫЕ ВОПРОСЫ ====================
@router.message(Anon.typing)
async def anon_question(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Введи текст вопроса.")
        return
    if len(text) > 500:
        await message.answer("Максимум 500 символов.")
        return
    await state.update_data(question=text)
    await message.answer(
        f'{ce(EMOJI_ANON, "🤨")} <b>Ваш вопрос:</b>\n\n"{text}"',
        reply_markup=anon_confirm_menu(),
    )

@router.callback_query(F.data == "anon:cancel")
async def anon_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("Отменено.")
    await cb.answer()

@router.callback_query(F.data == "anon:edit")
async def anon_edit(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.set_state(Anon.typing)
    await cb.message.answer(f'{ce(EMOJI_ANON, "🤨")} Введите ваш вопрос заново:')
    await cb.answer()

@router.callback_query(F.data == "anon:send")
async def anon_send(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target_uid = data.get("target_uid")
    text = data.get("question")
    if not target_uid or not text:
        await cb.answer("Сессия истекла.", show_alert=True)
        await state.clear()
        return

    cur = await db.execute(
        "INSERT INTO anon_questions (from_id, to_id, text) VALUES (?,?,?) RETURNING id",
        (cb.from_user.id, target_uid, text),
    )
    row = await cur.fetchone()
    qid = row["id"]

    target = await get_user(target_uid)
    if not target or target["active"] == 0:
        await cb.answer("Пользователь недоступен.", show_alert=True)
        await state.clear()
        return

    try:
        await bot.send_message(
            target_uid,
            f'{ce(EMOJI_ANON, "🤨")} <b>Вам задали вопрос:</b>\n'
            f'{ce(EMOJI_NAME, "🤨")} Кто задал: <b>Аноним</b>\n\n'
            f'"{text}"',
            reply_markup=anon_report_menu(qid),
        )
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer("✅ Вопрос отправлен анонимно.")
    except Exception as e:
        log.warning("anon send: %s", e)
        await cb.answer("Не удалось отправить.", show_alert=True)
    await state.clear()
    await cb.answer()

@router.callback_query(F.data.startswith("anon:report:"))
async def anon_report(cb: CallbackQuery):
    qid = int(cb.data.split(":")[-1])
    cur = await db.execute("SELECT * FROM anon_questions WHERE id=?", (qid,))
    q = await cur.fetchone()
    if not q:
        await cb.answer("Уже неактуально.", show_alert=True)
        return
    try:
        await bot.send_message(OWNER_ID,
            f"🚨 <b>Жалоба на анонимный вопрос</b>\n"
            f"От: <code>{q['from_id']}</code>\n"
            f"Кому: <code>{q['to_id']}</code>\n"
            f"Текст: {q['text']}")
    except Exception:
        pass
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("Жалоба отправлена.")
    await cb.answer()

# ==================== ПРОЧЕЕ ====================
@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "/start — перезапуск\n/me — профиль\n/search — поиск\n/stop — завершить чат",
        reply_markup=main_menu())

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    cur = await db.execute("SELECT COUNT(*) c FROM users WHERE active=1")
    users = (await cur.fetchone())["c"]
    cur = await db.execute("SELECT COUNT(*) c FROM anon_questions")
    anons = (await cur.fetchone())["c"]
    await message.answer(
        f"👥 Активных: <b>{users}</b>\n"
        f"💬 В чатах: <b>{len(chat_partners)//2}</b>\n"
        f"❓ Анонимных вопросов: <b>{anons}</b>"
    )

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

# ==================== ЗАПУСК ====================
async def main() -> None:
    global http, BOT_USERNAME
    await open_db()
    http = aiohttp.ClientSession()
    me = await bot.get_me()
    BOT_USERNAME = me.username
    log.info("Bot started as @%s", BOT_USERNAME)
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