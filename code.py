import asyncio
import logging
import random
import re
import time
from datetime import datetime

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ---------- ЭФЕМЕРНЫЕ СООБЩЕНИЯ (fallback в ЛС) ----------
try:
    from aiogram.types import EphemeralMessageParameters
    HAS_EPHEMERAL = True
except ImportError:
    HAS_EPHEMERAL = False
    EphemeralMessageParameters = None

# ---------- НАСТРОЙКИ ----------
BOT_TOKEN = "8622131046:AAHEQHre9qHrcRfT2V7qo8MVdQ-tG16E7Uc"
DB_PATH = "meetings.db"
OWNER_ID = 8544445592

MAX_MEETINGS = 15
TIMEOUT = 60
EMOJI_ROTATE = 24 * 60 * 60
MENTIONS_PER_MSG = 40
MEETING_EMOJIS_PER_MSG = 5

# ---------- ПРЕМИУМ-ЭМОДЗИ ----------
E_BOT    = "5931614414351372818"
E_SHIELD = "5212982655343141065"
E_PIN    = "5213467995237524118"
E_WARN   = "5213477830712632212"
E_HAMMER = "5276314275994954605"
E_STOP   = "5213211834798055548"


def e(eid: str, char: str) -> str:
    return f'<tg-emoji emoji-id="{eid}">{char}</tg-emoji>'


EMOJIS = [
    "👦", "👧", "🧑", "👨", "👩", "🧓", "👴", "👵",
    "😀", "😃", "😄", "😁", "😆", "😅", "🤣", "😂",
    "🙂", "🙃", "😉", "😊", "😇", "🥰", "😍", "🤩",
    "😘", "😗", "😚", "😙", "😋", "😛", "😜", "🤪",
    "😝", "🤗", "🤭", "🤫", "🤔", "🤐", "🤨", "😐",
    "😑", "😶", "😏", "😒", "🙄", "😬", "🤥", "😴",
    "🤤", "😪", "😵", "🤯", "🤠", "🥳", "😎", "🤓",
    "🧐", "👋", "🤚", "🖐", "✋", "🖖", "👌", "🤌",
]

dp = Dispatcher(storage=MemoryStorage())
meeting_log: dict[int, list[float]] = {}


# ---------- БД ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                is_bot INTEGER NOT NULL DEFAULT 0,
                emoji TEXT NOT NULL,
                emoji_set_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                added_at TEXT
            )
        """)
        await db.commit()


async def upsert_user(chat_id, user_id, username, first_name, is_bot=False):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (chat_id, user_id, username, first_name, is_bot, emoji, emoji_set_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                is_bot = excluded.is_bot
        """, (chat_id, user_id, username, first_name, int(is_bot),
              random.choice(EMOJIS), int(time.time())))
        await db.commit()


async def save_group(chat_id: int, title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO groups (chat_id, title, added_at) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title
        """, (chat_id, title, datetime.utcnow().isoformat()))
        await db.commit()


async def load_groups():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT chat_id, title FROM groups ORDER BY title") as c:
            return [dict(r) for r in await c.fetchall()]


class TrackUsers(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if (isinstance(event, Message)
                and event.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)):
            try:
                await save_group(event.chat.id, event.chat.title or "—")
            except Exception:
                pass
            if event.from_user:
                await upsert_user(event.chat.id, event.from_user.id,
                                  event.from_user.username,
                                  event.from_user.first_name or "",
                                  is_bot=event.from_user.is_bot)
            rt = event.reply_to_message
            if rt and rt.from_user:
                await upsert_user(event.chat.id, rt.from_user.id,
                                  rt.from_user.username,
                                  rt.from_user.first_name or "",
                                  is_bot=rt.from_user.is_bot)
            if event.new_chat_members:
                for m in event.new_chat_members:
                    await upsert_user(event.chat.id, m.id, m.username,
                                      m.first_name or "", is_bot=m.is_bot)
        return await handler(event, data)


dp.message.middleware(TrackUsers())


# ---------- ХЕЛПЕРЫ ----------
def add_to_group_kb(bot_username: str) -> InlineKeyboardMarkup:
    link = (f"https://t.me/{bot_username}"
            f"?startgroup=true&admin=delete_messages+pin_messages+restrict_members")
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Добавить бота в группу",
            url=link,
            icon_custom_emoji_id=E_BOT,
        )
    ]])


async def load_chat_users(chat_id: int, include_bots=True):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        q = "SELECT user_id, username, first_name, is_bot FROM users WHERE chat_id=?"
        if not include_bots:
            q += " AND is_bot=0"
        async with db.execute(q, (chat_id,)) as cur:
            return await cur.fetchall()


async def refresh_chat(bot: Bot, chat_id: int):
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        for adm in admins:
            u = adm.user
            await db.execute("""
                INSERT INTO users (chat_id, user_id, username, first_name, is_bot, emoji, emoji_set_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    username=excluded.username, first_name=excluded.first_name,
                    is_bot=excluded.is_bot
            """, (chat_id, u.id, u.username, u.first_name or "",
                  int(u.is_bot), random.choice(EMOJIS), int(time.time())))
        await db.commit()


async def get_user_emoji(chat_id: int, user_id: int) -> str:
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT emoji, emoji_set_at FROM users WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return random.choice(EMOJIS)
    if now - row["emoji_set_at"] >= EMOJI_ROTATE:
        new = random.choice(EMOJIS)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE users SET emoji=?, emoji_set_at=? WHERE chat_id=? AND user_id=?",
                (new, int(now), chat_id, user_id))
            await db.commit()
        return new
    return row["emoji"]


def check_antiflood(chat_id: int) -> bool:
    now = time.time()
    recent = [t for t in meeting_log.get(chat_id, []) if now - t < TIMEOUT]
    if len(recent) >= MAX_MEETINGS:
        meeting_log[chat_id] = recent
        return False
    recent.append(now)
    meeting_log[chat_id] = recent
    return True


async def send_ephemeral(bot: Bot, chat_id: int, user_id: int, text: str):
    """Эфемерное сообщение (aiogram >= 3.31.0) или отправка в ЛС владельцу."""
    if HAS_EPHEMERAL:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                ephemeral_message_parameters=EphemeralMessageParameters(
                    receiver_user_id=user_id
                )
            )
            return
        except Exception as ex:
            logging.warning(f"ephemeral send failed, fallback to DM: {ex}")

    try:
        await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as ex:
        logging.error(f"send_ephemeral fallback: {ex}")


# ---------- /start ----------
@dp.message(CommandStart())
async def cmd_start(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    me = await message.bot.get_me()
    await message.answer(
        'Привет!\n\n'
        'Я бот, который готов позвать <b>всю группу</b>.\n'
        'Бот был создан благодаря @hy3rm1z',
        parse_mode="HTML",
        reply_markup=add_to_group_kb(me.username),
    )


# ---------- добавление в группу ----------
@dp.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated):
    if event.new_chat_member.user.id != event.bot.id:
        return
    was_out = event.old_chat_member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)
    is_in = event.new_chat_member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)
    if not (was_out and is_in):
        return

    await save_group(event.chat.id, event.chat.title or "—")

    text = (
        f'{e(E_SHIELD, "🛡")} Спасибо что добавили меня в группу. '
        'Для работы с чатом мне понадобятся следующие разрешения:\n\n'
        f'{e(E_PIN, "📌")} Закреплять сообщения\n'
        f'{e(E_SHIELD, "🛡")} Управление группой\n'
        f'{e(E_STOP, "🛑")} Блокировка пользователей\n\n'
        'Если вы уже выдали боту эти права, напишите в чат /checkrights'
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Заказать такого бота",
            url="https://t.me/hy3rm1z",
            icon_custom_emoji_id=E_HAMMER,
        )
    ]])
    await event.bot.send_message(event.chat.id, text, parse_mode="HTML", reply_markup=kb)


# ---------- /amen ----------
@dp.message(Command("amen"))
async def cmd_amen(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if message.from_user is None or message.from_user.id != OWNER_ID:
        await message.reply(
            f'{e(E_WARN, "⚠")} Команда доступна только владельцу бота.',
            parse_mode="HTML")
        return

    chat_id = message.chat.id
    if not check_antiflood(chat_id):
        await message.reply(
            f'{e(E_WARN, "⚠")} Произошла ошибка, попробуйте позже\n'
            'Код ошибки: Частые собрания, таймаут 1 минута',
            parse_mode="HTML")
        return

    await refresh_chat(message.bot, chat_id)
    users = await load_chat_users(chat_id, include_bots=True)
    if not users:
        await message.reply("В базе пока нет участников этой группы.")
        return

    bot_me = await message.bot.get_me()
    users = [u for u in users if u["user_id"] != bot_me.id]

    mentions, seen = [], set()
    for u in users:
        if u["username"]:
            m = f"@{u['username']}"
        else:
            m = f'<a href="tg://user?id={u["user_id"]}">{u["first_name"] or "участник"}</a>'
        if m not in seen:
            seen.add(m)
            mentions.append(m)

    total = len(mentions)
    chunks = [mentions[i:i + MENTIONS_PER_MSG] for i in range(0, total, MENTIONS_PER_MSG)]

    for idx, chunk in enumerate(chunks, start=1):
        header = f'<b>Собрание!</b> ({idx}/{len(chunks)})\n'
        text = header + " ".join(chunk)
        try:
            await message.bot.send_message(chat_id, text, parse_mode="HTML",
                                            disable_web_page_preview=True)
        except Exception:
            half = max(1, len(chunk) // 2)
            for part in (chunk[:half], chunk[half:]):
                if not part:
                    continue
                await message.bot.send_message(chat_id, header + " ".join(part),
                                                parse_mode="HTML",
                                                disable_web_page_preview=True)
        await asyncio.sleep(0.5)


# ---------- /meeting (все юзеры, разбивка по 5 эмодзи) ----------
@dp.message(Command("meeting"))
async def cmd_meeting(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    chat_id = message.chat.id
    if not check_antiflood(chat_id):
        await message.reply(
            f'{e(E_WARN, "⚠")} Произошла ошибка, попробуйте позже\n'
            'Код ошибки: Частые собрания, таймаут 1 минута',
            parse_mode="HTML")
        return

    users = await load_chat_users(chat_id, include_bots=False)
    if not users:
        await message.reply("В базе пока нет участников этой группы.")
        return

    # берём ВСЕХ пользователей, перемешиваем
    all_users = list(users)
    random.shuffle(all_users)

    parts = []
    for u in all_users:
        emoji = await get_user_emoji(chat_id, u["user_id"])
        parts.append(f'<a href="tg://user?id={u["user_id"]}">{emoji}</a>')

    # режем по 5 эмодзи на сообщение
    chunks = [parts[i:i + MEETING_EMOJIS_PER_MSG]
              for i in range(0, len(parts), MEETING_EMOJIS_PER_MSG)]

    total = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        text = " ".join(chunk)
        if total > 1:
            text = f"({idx}/{total})\n{text}"
        try:
            await message.bot.send_message(
                chat_id, text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as ex:
            logging.error(f"meeting send {chat_id}: {ex}")
        await asyncio.sleep(0.4)


# ---------- /temp ----------
@dp.message(Command("temp"))
async def cmd_temp(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if message.from_user is None or message.from_user.id != OWNER_ID:
        await message.reply(
            f'{e(E_WARN, "⚠")} Только для владельца.',
            parse_mode="HTML")
        return

    chat_id = message.chat.id
    users = await load_chat_users(chat_id, include_bots=True)

    if not users:
        await send_ephemeral(message.bot, chat_id, message.from_user.id,
                             "В БД этой группы пока нет пользователей.")
        return

    lines = [f'<b>Пользователи в БД чата</b> ({len(users)}):\n']
    for i, u in enumerate(users, start=1):
        tag = "BOT" if u["is_bot"] else "USR"
        uname = f"@{u['username']}" if u["username"] else "—"
        lines.append(
            f'{i}. [{tag}] <a href="tg://user?id={u["user_id"]}">'
            f'{u["first_name"] or "без имени"}</a> ({uname}) '
            f'<code>{u["user_id"]}</code>'
        )

    full = "\n".join(lines)
    if len(full) > 4000:
        chunks = []
        current = []
        size = 0
        for line in lines:
            if size + len(line) + 1 > 4000:
                chunks.append("\n".join(current))
                current = [line]
                size = len(line)
            else:
                current.append(line)
                size += len(line) + 1
        if current:
            chunks.append("\n".join(current))

        for chunk in chunks:
            await send_ephemeral(message.bot, chat_id, message.from_user.id, chunk)
            await asyncio.sleep(0.3)
    else:
        await send_ephemeral(message.bot, chat_id, message.from_user.id, full)


# ---------- /checkrights ----------
@dp.message(Command("checkrights"))
async def cmd_checkrights(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    try:
        member = await message.bot.get_chat_member(message.chat.id, message.bot.id)
    except Exception:
        await message.reply(
            f'{e(E_WARN, "⚠")} Не удалось проверить права.',
            parse_mode="HTML")
        return
    ok = (member.status == ChatMemberStatus.ADMINISTRATOR
          and getattr(member, "can_pin_messages", False)
          and getattr(member, "can_delete_messages", False)
          and getattr(member, "can_restrict_members", False))
    if ok:
        await message.reply('Права выданы, бот готов к работе.', parse_mode="HTML")
    else:
        await message.reply(
            'Я не обнаружил требуемые права, попробуйте снова выдать права '
            'и использовать эту команду.',
            parse_mode="HTML")


# ---------- /reset ----------
@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE chat_id=?", (message.chat.id,))
        await db.commit()
    meeting_log.pop(message.chat.id, None)
    await refresh_chat(message.bot, message.chat.id)
    await message.reply('База группы обновлена.', parse_mode="HTML")


# ================================================================
#                       /ads  (ЛС, только владелец)
# ================================================================
class AdsState(StatesGroup):
    menu = State()
    text = State()
    buttons = State()
    groups = State()


def _ads_summary(d: dict) -> str:
    text_ok = bool(d.get("ad_text"))
    btns = d.get("ad_buttons") or []
    sel = d.get("ad_groups") or []
    return (
        "<b>Рекламная рассылка</b>\n\n"
        f"Текст: {'заполнен' if text_ok else 'пусто'}\n"
        f"Кнопки: {len(btns)} шт.\n"
        f"Куда: {'все группы' if not sel else f'{len(sel)} групп'}\n"
    )


def _ads_kb(d: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    text_ok = bool(d.get("ad_text"))
    btns = d.get("ad_buttons") or []
    sel = d.get("ad_groups") or []
    b.row(
        InlineKeyboardButton(text=f"Текст {'+' if text_ok else '—'}",
                             callback_data="ads:set_text", icon_custom_emoji_id=E_PIN),
        InlineKeyboardButton(text=f"Кнопки ({len(btns)})",
                             callback_data="ads:set_buttons", icon_custom_emoji_id=E_SHIELD),
    )
    b.row(
        InlineKeyboardButton(text=f"Куда отправить ({len(sel) or 'все'})",
                             callback_data="ads:set_groups", icon_custom_emoji_id=E_HAMMER),
    )
    b.row(
        InlineKeyboardButton(text="Отправить",
                             callback_data="ads:send", icon_custom_emoji_id=E_STOP),
    )
    b.row(
        InlineKeyboardButton(text="« Назад", callback_data="ads:close"),
    )
    return b.as_markup()


def _groups_kb(groups: list[dict], selected: list[int]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for g in groups:
        mark = "[+] " if g["chat_id"] in selected else "[ ] "
        b.row(InlineKeyboardButton(
            text=f'{mark}{g["title"]}',
            callback_data=f'ads:tgl:{g["chat_id"]}',
        ))
    b.row(InlineKeyboardButton(text="« Назад", callback_data="ads:back_menu"))
    return b.as_markup()


@dp.message(Command("ads"))
async def cmd_ads(message: Message, state: FSMContext):
    if message.chat.type != ChatType.PRIVATE:
        return
    if message.from_user.id != OWNER_ID:
        await message.answer(f'{e(E_WARN, "⚠")} Только для админа.', parse_mode="HTML")
        return
    await state.clear()
    await state.set_state(AdsState.menu)
    sent = await message.answer("<b>Рекламная рассылка</b>\n\nЗаполните данные:",
                                parse_mode="HTML", reply_markup=_ads_kb({}))
    await state.update_data(mc=sent.chat.id, mm=sent.message_id)


@dp.callback_query(F.data == "ads:close")
async def ads_close(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "ads:back_menu")
async def ads_back_menu(callback: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.set_state(AdsState.menu)
    try:
        await callback.message.edit_text(_ads_summary(d),
                                          parse_mode="HTML", reply_markup=_ads_kb(d))
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "ads:set_text")
async def ads_set_text(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdsState.text)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="« Назад", callback_data="ads:back_menu"))
    try:
        await callback.message.edit_text(
            "Введите <b>текст</b> рекламы.\n\n"
            "Можно использовать <b>премиум-эмодзи</b> — бот их сохранит и перешлёт как есть.",
            parse_mode="HTML", reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer()


@dp.message(AdsState.text, F.chat.type == ChatType.PRIVATE)
async def ads_text_input(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    entities_data = [ent.model_dump(mode="json") for ent in entities]
    await state.update_data(ad_text=text, ad_entities=entities_data)
    d = await state.get_data()
    await state.set_state(AdsState.menu)
    try:
        await message.bot.edit_message_text(
            chat_id=d["mc"], message_id=d["mm"],
            text=_ads_summary(d), parse_mode="HTML", reply_markup=_ads_kb(d))
    except Exception:
        sent = await message.answer(_ads_summary(d),
                                     parse_mode="HTML", reply_markup=_ads_kb(d))
        await state.update_data(mc=sent.chat.id, mm=sent.message_id)


@dp.callback_query(F.data == "ads:set_buttons")
async def ads_set_buttons(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdsState.buttons)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="« Назад", callback_data="ads:back_menu"))
    try:
        await callback.message.edit_text(
            "Введите кнопки, <b>по одной на строку</b> в формате:\n\n"
            "<code>Название | https://ссылка</code>\n\n"
            "Пример:\n"
            "<code>Канал | https://t.me/example</code>\n"
            "<code>Буст | https://t.me/boost</code>\n\n"
            "Премиум-эмодзи в названии тоже поддерживаются.",
            parse_mode="HTML", reply_markup=b.as_markup())
    except Exception:
        pass
    await callback.answer()


@dp.message(AdsState.buttons, F.chat.type == ChatType.PRIVATE)
async def ads_buttons_input(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return
    text = message.text or ""
    entities = message.entities or []

    custom_ids = [ent.custom_emoji_id for ent in entities
                  if ent.type == "custom_emoji"]

    buttons = []
    idx = 0
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            name, url = line.split("|", 1)
        else:
            parts = line.rsplit(None, 1)
            if len(parts) < 2:
                continue
            name, url = parts
        name, url = name.strip(), url.strip()
        if not url.startswith("http"):
            continue
        emoji_id = custom_ids[idx] if idx < len(custom_ids) else None
        idx += 1
        if emoji_id:
            name = re.sub(r"^[^\w\s]+", "", name).strip() or name
        buttons.append({"name": name, "url": url, "emoji_id": emoji_id})

    await state.update_data(ad_buttons=buttons)
    d = await state.get_data()
    await state.set_state(AdsState.menu)
    try:
        await message.bot.edit_message_text(
            chat_id=d["mc"], message_id=d["mm"],
            text=_ads_summary(d), parse_mode="HTML", reply_markup=_ads_kb(d))
    except Exception:
        pass


@dp.callback_query(F.data == "ads:set_groups")
async def ads_set_groups(callback: CallbackQuery, state: FSMContext):
    groups = await load_groups()
    if not groups:
        await callback.answer("Бот не добавлен ни в одну группу.", show_alert=True)
        return
    d = await state.get_data()
    selected = d.get("ad_groups") or []
    await state.set_state(AdsState.groups)
    try:
        await callback.message.edit_text(
            "Выберите группы (нажатие — вкл/выкл).\n"
            "Если ничего не выбрано — отправится <b>во все</b>.",
            parse_mode="HTML", reply_markup=_groups_kb(groups, selected))
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("ads:tgl:"))
async def ads_toggle_group(callback: CallbackQuery, state: FSMContext):
    gid = int(callback.data.split(":")[2])
    d = await state.get_data()
    selected = set(d.get("ad_groups") or [])
    if gid in selected:
        selected.discard(gid)
    else:
        selected.add(gid)
    await state.update_data(ad_groups=list(selected))
    groups = await load_groups()
    try:
        await callback.message.edit_reply_markup(
            reply_markup=_groups_kb(groups, list(selected)))
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "ads:send")
async def ads_send(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != OWNER_ID:
        await callback.answer()
        return
    d = await state.get_data()
    text = d.get("ad_text")
    entities_data = d.get("ad_entities") or []
    buttons_data = d.get("ad_buttons") or []
    selected = d.get("ad_groups") or []

    if not text:
        await callback.answer("Сначала заполните текст.", show_alert=True)
        return

    groups = await load_groups()
    targets = [g for g in groups if not selected or g["chat_id"] in selected]
    if not targets:
        await callback.answer("Нет групп для отправки.", show_alert=True)
        return

    kb = None
    if buttons_data:
        rows = []
        for b in buttons_data:
            btn_kwargs = {"text": b["name"], "url": b["url"]}
            if b.get("emoji_id"):
                btn_kwargs["icon_custom_emoji_id"] = b["emoji_id"]
            rows.append([InlineKeyboardButton(**btn_kwargs)])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)

    entities = [MessageEntity(**ed) for ed in entities_data] if entities_data else None

    ok, fail = 0, 0
    for g in targets:
        try:
            await callback.bot.send_message(
                g["chat_id"], text,
                entities=entities,
                reply_markup=kb,
                disable_web_page_preview=True,
                parse_mode=None if entities else ParseMode.HTML,
            )
            ok += 1
        except Exception as ex:
            logging.error(f"ads send {g['chat_id']}: {ex}")
            fail += 1
        await asyncio.sleep(0.15)

    await state.clear()
    try:
        await callback.message.edit_text(f"Отправлено: {ok}\nОшибок: {fail}")
    except Exception:
        pass
    await callback.answer()


# ================================================================
#                    BACKUP / RESTORE (ЛС)
# ================================================================
@dp.message(Command("backup"))
async def cmd_backup(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    if message.from_user.id != OWNER_ID:
        return
    try:
        with open(DB_PATH, "rb") as f:
            data = f.read()
        file = BufferedInputFile(
            data,
            filename=f"backup_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.db"
        )
        await message.answer_document(file, caption="Бэкап по запросу")
    except Exception as ex:
        await message.answer(f"Ошибка: {ex}")


@dp.message(Command("restore"))
async def cmd_restore(message: Message):
    if message.chat.type != ChatType.PRIVATE:
        return
    if message.from_user.id != OWNER_ID:
        return
    if not message.document:
        await message.answer(
            "Отправь <code>/restore</code> как подпись к файлу .db.",
            parse_mode="HTML")
        return
    doc = message.document
    if not doc.file_name.endswith(".db"):
        await message.answer("Нужен файл с расширением .db")
        return
    try:
        file = await message.bot.get_file(doc.file_id)
        data = await message.bot.download_file(file.file_path)
        raw = data.read()
        with open(DB_PATH, "wb") as f:
            f.write(raw)
        await message.answer(f"БД восстановлена ({len(raw)} байт). Перезапусти бота.")
    except Exception as ex:
        await message.answer(f"Ошибка: {ex}")


# ---------- автообновление админов ----------
async def auto_refresh_loop(bot: Bot):
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT DISTINCT chat_id FROM users") as cur:
                    chats = [r[0] for r in await cur.fetchall()]
            for chat_id in chats:
                await refresh_chat(bot, chat_id)
        except Exception:
            pass
        await asyncio.sleep(15)


# ---------- ЗАПУСК ----------
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(token=BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.delete_webhook(drop_pending_updates=True)

    asyncio.create_task(auto_refresh_loop(bot))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())