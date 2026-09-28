import asyncio
import os
import random
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, ChatMemberUpdated, ChatPermissions, InlineKeyboardButton,
    InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery,
)

# ================== КОНФИГ ==================
TOKEN = "8621302583:AAF2ickfLfj5cIdPGZtzFDFKjPoCbQub0OA"
BOT_USERNAME = "VIPchatdefferbot"        # VIP-бот
MAIN_BOT_USERNAME = "badusersbot"        # основной бот (покупка подписки)
SIGHT_USER = "258849477"
SIGHT_SECRET = "4m8pMUiBfg7vkpUTgYSktsqP7zCi27KH"

GREEN = "success"
RED = "danger"
BLUE = "primary"

MAX_MEDIA = 6
OWNER_ID = 8544445592                    # @hy3rm1z — уже с подпиской Германец

# ================== LOCK ==================
_lock_handle = None

def acquire_lock():
    global _lock_handle
    if not HAS_FCNTL:
        return True
    try:
        _lock_handle = open("bot.lock", "w")
        fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_handle.write(str(os.getpid()))
        _lock_handle.flush()
        return True
    except (IOError, OSError):
        return False

# ================== БД ==================
conn = sqlite3.connect("bot.db", check_same_thread=False)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# ⚠️ Раскомментируй один раз для сброса:
# cur.execute("DROP TABLE IF EXISTS users")
# cur.execute("DROP TABLE IF EXISTS groups_settings")
# cur.execute("DROP TABLE IF EXISTS processed_updates")
# cur.execute("DROP TABLE IF EXISTS login_codes")
# conn.commit()

cur.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
    gender TEXT, custom_name TEXT, first_start TEXT, last_seen TEXT,
    accepted INTEGER DEFAULT 0, opt_out INTEGER DEFAULT 0,
    subscription TEXT, sub_until TEXT, balance INTEGER DEFAULT 0,
    greeting TEXT, greeting_date TEXT, menu_message_id INTEGER,
    vip_title TEXT, custom_status TEXT,
    invites INTEGER DEFAULT 0, msg_count INTEGER DEFAULT 0,
    logged_in INTEGER DEFAULT 0)""")

cur.execute("""CREATE TABLE IF NOT EXISTS groups_settings (
    chat_id INTEGER PRIMARY KEY, title TEXT,
    red_mode INTEGER DEFAULT 1, mats INTEGER DEFAULT 1,
    links INTEGER DEFAULT 1, casino INTEGER DEFAULT 1,
    threats INTEGER DEFAULT 1, mentions INTEGER DEFAULT 1,
    captcha INTEGER DEFAULT 0, porn INTEGER DEFAULT 1,
    antiraid INTEGER DEFAULT 0, welcome INTEGER DEFAULT 1,
    menu_message_id INTEGER, rules TEXT)""")

cur.execute("""CREATE TABLE IF NOT EXISTS processed_updates (
    update_id INTEGER PRIMARY KEY, ts TEXT)""")

cur.execute("""CREATE TABLE IF NOT EXISTS login_codes (
    code TEXT PRIMARY KEY, subscription TEXT, days INTEGER,
    created_at TEXT, used_at TEXT, used_by INTEGER)""")
conn.commit()

# ---------- Предзаполнение владельца ----------
def seed_owner():
    uid = OWNER_ID
    now = datetime.now().isoformat()
    until = (datetime.now() + timedelta(days=3650)).isoformat()
    cur.execute("SELECT user_id FROM users WHERE user_id=?", (uid,))
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO users (user_id, username, first_name, first_start, "
            "last_seen, accepted, subscription, sub_until, logged_in, vip_title) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uid, "hy3rm1z", "hy3rm1z", now, now, 1, "Германец", until, 1,
             "Я из Германии*"))
    else:
        cur.execute(
            "UPDATE users SET accepted=1, logged_in=1, subscription='Германец', "
            "sub_until=?, vip_title='Я из Германии*' WHERE user_id=?",
            (until, uid))
    conn.commit()

seed_owner()

SPAM = defaultdict(list)
RED_UNTIL = {}
WARNS = defaultdict(lambda: defaultdict(int))
ALBUM_CNT = defaultdict(int)

# ================== DEDUP ==================
class DedupMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        upd_id = getattr(event, "update_id", None)
        if upd_id is not None:
            cur.execute("INSERT OR IGNORE INTO processed_updates (update_id, ts) VALUES (?,?)",
                        (upd_id, datetime.now().isoformat()))
            conn.commit()
            if cur.rowcount == 0:
                return
        return await handler(event, data)

# ================== УТИЛИТЫ ==================
def get_user(uid):
    cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    return cur.fetchone()

def ensure_user(u):
    now = datetime.now().isoformat()
    row = get_user(u.id)
    if row is None:
        cur.execute("INSERT INTO users (user_id, username, first_name, first_start, last_seen) "
                    "VALUES (?,?,?,?,?)",
                    (u.id, u.username or "", u.first_name or "", now, now))
    else:
        cur.execute("UPDATE users SET username=?, first_name=?, last_seen=? WHERE user_id=?",
                    (u.username or "", u.first_name or "", now, u.id))
    conn.commit()
    return get_user(u.id)

def set_u(uid, f, v):
    cur.execute(f"UPDATE users SET {f}=? WHERE user_id=?", (v, uid))
    conn.commit()

def get_group(cid, title=""):
    cur.execute("SELECT * FROM groups_settings WHERE chat_id=?", (cid,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO groups_settings (chat_id, title) VALUES (?,?)", (cid, title))
        conn.commit()
        cur.execute("SELECT * FROM groups_settings WHERE chat_id=?", (cid,))
        row = cur.fetchone()
    elif title and row["title"] != title:
        cur.execute("UPDATE groups_settings SET title=? WHERE chat_id=?", (title, cid))
        conn.commit()
    return row

def set_g(cid, f, v):
    cur.execute(f"UPDATE groups_settings SET {f}=? WHERE chat_id=?", (v, cid))
    conn.commit()

def is_premium(row) -> bool:
    return bool(row and row["subscription"] and row["logged_in"])

def is_logged(row) -> bool:
    return bool(row and row["logged_in"])

def btn(text, cb=None, url=None, style=GREEN):
    if url:
        return InlineKeyboardButton(text=text, url=url, style=style)
    return InlineKeyboardButton(text=text, callback_data=cb, style=style)

async def safe_edit(message: Message, text: str, kb=None):
    try:
        await message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "message is not modified" in err or "message can't be edited" in err:
            return
        raise

# ================== СТРИМИНГ ==================
async def stream_answer(bot: Bot, chat_id: int, text: str, is_private: bool = True):
    html = f"<b>{text}</b>"
    if is_private:
        draft_id = random.randint(1, 2**31 - 1)
        for i in range(2, len(text) + 2, 2):
            try:
                await bot.send_message_draft(chat_id=chat_id, draft_id=draft_id,
                                             text=text[:i])
            except Exception:
                pass
            await asyncio.sleep(0.04)
        try:
            await bot.send_message(chat_id, html, parse_mode=ParseMode.HTML)
        except TelegramBadRequest:
            pass
        return None
    else:
        try:
            m = await bot.send_message(chat_id, "…")
        except TelegramBadRequest:
            return None
        for i in range(2, len(text) + 2, 2):
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=m.message_id,
                                            text=text[:i])
            except TelegramBadRequest:
                pass
            await asyncio.sleep(0.04)
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=m.message_id,
                                        text=html, parse_mode=ParseMode.HTML)
        except TelegramBadRequest:
            pass
        return m

# ================== ФИЛЬТРЫ КОМАНД ==================
GROUP_CMD_NAMES = {
    "setting", "settings", "настройки", "настройка", "настроить",
    "настроить чат", "настройки чата",
}
SCAN_CMD_NAMES = {"скан", "scan", "проверить"}
START_CMD_NAMES = {"start", "старт"}
PS_CMD_NAMES = {"пс", "ps", "соглашение"}
OS_CMD_NAMES = {"ос", "os"}
HELP_CMD_NAMES = {"help", "хелп", "помощь", "команды"}
STATS_CMD_NAMES = {"stats", "статистика", "стата"}
TOP_CMD_NAMES = {"top", "топ"}
INVITE_CMD_NAMES = {"invite", "инвайт", "пригласить"}
TITLE_CMD_NAMES = {"title", "титул"}
STATUS_CMD_NAMES = {"status", "статус"}
RULES_CMD_NAMES = {"rules", "правила"}
INFO_CMD_NAMES = {"info", "инфо", "информация"}
PING_CMD_NAMES = {"ping", "пинг"}
BALANCE_CMD_NAMES = {"balance", "баланс", "бал"}
DAILY_CMD_NAMES = {"daily", "дейли", "бонус"}
LOGOUT_CMD_NAMES = {"logout", "выход", "выйти"}

def normalize_cmd(t: str) -> str:
    if not t:
        return ""
    t = t.strip()
    if "." in t:
        return ""
    t = t.lower()
    for p in ("/", "!", "?"):
        if t.startswith(p):
            t = t[1:]
            break
    if "@" in t:
        t = t.split("@")[0]
    return t

def _in(t, names):
    return normalize_cmd(t) in names

def is_group_cmd(t): return _in(t, GROUP_CMD_NAMES)
def is_scan_cmd(t): return _in(t, SCAN_CMD_NAMES)
def is_start_cmd(t): return _in(t, START_CMD_NAMES)
def is_ps_cmd(t): return _in(t, PS_CMD_NAMES)
def is_os_cmd(t): return _in(t, OS_CMD_NAMES)
def is_help_cmd(t): return _in(t, HELP_CMD_NAMES)
def is_stats_cmd(t): return _in(t, STATS_CMD_NAMES)
def is_top_cmd(t): return _in(t, TOP_CMD_NAMES)
def is_invite_cmd(t): return _in(t, INVITE_CMD_NAMES)
def is_title_cmd(t): return _in(t, TITLE_CMD_NAMES)
def is_status_cmd(t): return _in(t, STATUS_CMD_NAMES)
def is_rules_cmd(t): return _in(t, RULES_CMD_NAMES)
def is_info_cmd(t): return _in(t, INFO_CMD_NAMES)
def is_ping_cmd(t): return _in(t, PING_CMD_NAMES)
def is_balance_cmd(t): return _in(t, BALANCE_CMD_NAMES)
def is_daily_cmd(t): return _in(t, DAILY_CMD_NAMES)
def is_logout_cmd(t): return _in(t, LOGOUT_CMD_NAMES)

def is_any_cmd(t):
    return any([is_group_cmd(t), is_scan_cmd(t), is_start_cmd(t),
                is_ps_cmd(t), is_os_cmd(t), is_help_cmd(t),
                is_stats_cmd(t), is_top_cmd(t), is_invite_cmd(t),
                is_title_cmd(t), is_status_cmd(t), is_rules_cmd(t),
                is_info_cmd(t), is_ping_cmd(t), is_balance_cmd(t),
                is_daily_cmd(t), is_logout_cmd(t)])

def is_login_code(t: str) -> bool:
    if not t:
        return False
    t = t.strip()
    return len(t) == 11 and t.isdigit()

# ================== ТЕКСТЫ ==================
BOT_NAME = "Плохие Люди"

START_TEXT = (f'<b>{BOT_NAME}</b> — VIP-бот защиты чатов.\n\n'
              f'Для входа отправь свой 11-значный код, полученный '
              f'в @{MAIN_BOT_USERNAME} после покупки Премиум или Германец.')

LOGIN_REQUIRED = (f'<b>{BOT_NAME}</b>\n\n'
                  f'🔒 Ты не вошёл в аккаунт.\n'
                  f'Отправь 11-значный код для входа.\n\n'
                  f'Получить код: @{MAIN_BOT_USERNAME} → купить Премиум или Германец.')

LOGIN_SUCCESS = (f'✅ Добро пожаловать в <b>{BOT_NAME}</b>!\n\n'
                 f'Подписка активирована. Код использован.')

LOGIN_BAD = ('❌ Неверный или уже использованный код.\n'
             'Проверь код или получи новый в @{}.').format(MAIN_BOT_USERNAME)

GREETINGS = [
    "Как дела?", "Что делаешь?", "Я соскучилась, где ты был?(",
    "Как погодка?", "Что нового?", "Твой чат под защитой?",
]

DECO_TEXTS = [
    "🛡 Защищу любой чат!", "Слава «Плохим Людям»!",
    "🛡 VIP-защита активна", "🌟 Германец — топ!",
    "🔒 Никаких сносов", "🚀 Готов к работе!",
    "😎 Держу оборону", "🎯 Умный фильтр онлайн",
    "🌐 Сканирую угрозы", "⚡ Антирейд включён",
    "💪 Не пропущу спамера", "🐱 Мяу... то есть — в бой!",
    "😈 Нарушители — трепещите", "🎩 VIP-бот в деле",
    "🔍 Скан на готове", "📊 Статистика ведётся",
]

AGREEMENT_TEXT = f"""<b>ПОЛЬЗОВАТЕЛЬСКОЕ СОГЛАШЕНИЕ — {BOT_NAME}</b>

1.0. Все данные, собранные ботом, остаются в пределах бота и Telegram.
1.1. Бот собирает информацию для обновления распознавания медиа.
1.2. Мы не используем данные чатов в личных целях. Личные данные удаляются.
1.3. Отказ от сбора — команда «/ос».
1.4. Нажимая «Принять», вы соглашаетесь со всем, что здесь написано.
1.5. Данные доступны только: Хостинг, Тех. администратор.
1.6. Нарушения платформы фиксируются.
1.7. Мы вправе заблокировать аккаунт при наличии платной подписки.
1.8. Причины: 3+ нарушения, оскорбления, тяжкие нарушения закона.
1.9. Все участники равны — правила действуют и на администраторов."""

HELP_TEXT = f"""<b>📘 Команды {BOT_NAME}</b>

<b>💬 В личке:</b>
/start — запуск
/ПС — соглашение
/ос — отказ от сбора данных
/скан — проверить медиа
/титул — поставить титул (Премиум+)
/статус — кастомный статус (Премиум+)
/инвайт — ссылка-приглашение
/баланс — баланс звёзд
/дейли — ежедневный бонус
/статистика — твоя статистика
/logout — выйти из аккаунта
/помощь — эта справка

<b>👥 В группах:</b>
настройки / настроить — меню
/скан — скан фото
/правила — правила чата
/инфо — инфа о чате
/топ — топ участников
/ping — проверка

<b>Подписки (в @{MAIN_BOT_USERNAME}):</b>
• Комфорт — 110 звёзд / 3 мес
• Премиум — 199 звёзд / 3 мес
• Германец — 350 звёзд / 3 мес

<b>Что даёт Премиум и Германец:</b>
• Титулы в профиле
• Кастомные статусы
• Приоритетную поддержку
• Инвайт-систему
• Расширенную аналитику
"""

BAD = ["хуй","хуе","хуё","пизд","пизж","ебат","ебал","ебуч","ёб","блят","бляд","сука","сучк",
       "мраз","гандон","долбоеб","долбоёб","мудак","мудил","шлюх","нахуй","нахуя","залуп",
       "пидор","пидар","пидр","fuck","shit","bitch"]
CASINO = ["казино","букмекер","букмекерск","1win","1xbet","1хбет","1вин","ставки","бет","bet",
          "vulkan","вулкан","joycasino","pin-up","pinup","pin up","казин","рулетк"]
THREATS = ["убью","убей","зарежу","прирежу","киллер","закажу тебя","шантаж","вымогаю",
           "найду тебя","отомщу","изобью","покалечу"]
PORN_W = ["порно","porn","xxx","хентай","hentai","nsfw","голые фото","нюдс","nudes",
          "секс видео","sex cam"]
URL_RE = re.compile(r"(https?://\S+|t\.me/\S+|@[\w\d_]{4,})", re.I)

def has_any(t, words):
    t = (t or "").lower()
    return any(w in t for w in words)

# ================== КЛАВИАТУРЫ ==================
def start_kb(accept=False):
    rows = [[btn("📜 Пользовательское Соглашение", cb="agreement")]]
    if accept:
        rows.append([btn("Принять", cb="accept", style=GREEN)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def agreement_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[btn("Я ознакомился", cb="back_to_start")]])

def back_kb(t="main_menu"):
    return InlineKeyboardMarkup(inline_keyboard=[[btn("« Назад", cb=t)]])

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("⚙️ Настройки", cb="settings"), btn("🤖 Кто я?", cb="whoami")],
        [btn("🆘 Поддержка", cb="support"), btn("👤 Профиль", cb="profile")],
        [btn("💎 Подписка", cb="subscription"),
         btn("➕ Добавить в чат",
             url=f"https://t.me/{BOT_USERNAME}?startgroup=true")],
        [btn("📘 Помощь", cb="help"), btn("📊 Статистика", cb="my_stats")],
        [btn(random.choice(DECO_TEXTS), cb="deco")],
    ])

def profile_kb(row):
    rows = [
        [btn("💳 Пополнить баланс", cb="topup")],
        [btn("🎖 Титул", cb="title_menu"), btn("💬 Статус", cb="status_menu")],
        [btn("📊 Статистика", cb="my_stats"), btn("🔗 Инвайт", cb="invite")],
        [btn("🚪 Выйти из аккаунта", cb="logout", style=RED)],
    ]
    rows.append([btn("« Назад", cb="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def topup_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("⭐ 50 звёзд", cb="topup_50"), btn("⭐ 100 звёзд", cb="topup_100")],
        [btn("⭐ 250 звёзд", cb="topup_250"), btn("⭐ 500 звёзд", cb="topup_500")],
        [btn("⭐ 1000 звёзд", cb="topup_1000")],
        [btn("« Назад", cb="profile")],
    ])

def settings_kb(row):
    g = row["gender"]
    gl = {"male": "Мужской", "female": "Женский"}.get(g, "не указан")
    nb = {"male": "Твоё имя, господин",
          "female": "Твоё имя, госпожа"}.get(g, "Твоё имя")
    rows = [
        [btn(f"Пол: {gl}", cb="noop")],
        [btn("Мужской", cb="set_male"), btn("Женский", cb="set_female")],
        [btn(nb, cb="set_name")],
        [btn("« Назад", cb="main_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

TOGGLES = [
    ("red_mode", "Красный режим"), ("mats", "Маты"),
    ("links", "Ссылки"), ("casino", "Казино"),
    ("threats", "Угрозы"), ("mentions", "Упоминание"),
    ("captcha", "Капча"), ("porn", "Порнография"),
    ("antiraid", "Антирейд"), ("welcome", "Приветствие"),
]

DESCR = {
    "red_mode": "<b>Красный режим</b> — 15+ сообщений за 5 сек → чат закрывается на 10 мин, все новые банятся.",
    "mats": "<b>Маты</b> — удаление матерных.",
    "links": "<b>Ссылки</b> — 1-е нарушение: мут 5 мин, 2-е: кик.",
    "casino": "<b>Казино</b> — удаление упоминаний казино/БК.",
    "threats": "<b>Угрозы</b> — мут 30 мин.",
    "mentions": "<b>Упоминание</b> — удаление @упоминаний.",
    "captcha": "<b>Капча</b> — 5 мин на подтверждение в ЛС (в разработке).",
    "porn": "<b>Порнография</b> — через Sightengine (порно/эротика/казино/QR).",
    "antiraid": "<b>Антирейд</b> — массовые входы → блокировка чата (в разработке).",
    "welcome": "<b>Приветствие</b> — приветствие новичкам.",
}

def group_kb(row):
    cid = row["chat_id"]
    rows = []
    pairs = [("red_mode", "mats"), ("links", "casino"),
             ("threats", "mentions"), ("captcha", "porn"),
             ("antiraid", "welcome")]
    for a, b in pairs:
        r = []
        for k in (a, b):
            style = GREEN if row[k] else RED
            r.append(btn(dict(TOGGLES)[k], cb=f"gtog:{cid}:{k}", style=style))
        rows.append(r)
    rows.append([btn("ℹ️ Инфо", cb=f"ginfo:{cid}", style=BLUE)])
    rows.append([btn("📜 Правила", cb=f"grules:{cid}"),
                 btn("🔗 Инвайт-ссылка", cb=f"glinvite:{cid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def group_text(row):
    return (f'<b>{BOT_NAME} — настройки чата</b>\n'
            f'«{row["title"]}»\n\n'
            f'Включено — зелёная кнопка.\n'
            f'Выключено — красная.\n'
            f'Менять может только владелец.')

def hide_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[btn("Скрыть", cb="hide_msg", style=RED)]])

def sub_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("💎 Комфорт — 110 звёзд / 3 мес", cb="sub_comfort")],
        [btn("🌟 Премиум — 199 звёзд / 3 мес", cb="sub_premium")],
        [btn("🎩 Германец — 350 звёзд / 3 мес", cb="sub_german")],
        [btn("« Назад", cb="main_menu")],
    ])

# ================== FSM ==================
class Form(StatesGroup):
    waiting_name = State()
    waiting_title = State()
    waiting_status = State()
    waiting_code = State()

# ================== SIGHTENGINE ==================
async def sight_check(url: str) -> dict:
    api = "https://api.sightengine.com/1.0/check.json"
    data = {
        "url": url,
        "models": "nudity-2.1,gambling,scam,qr,text-in-image",
        "api_user": SIGHT_USER,
        "api_secret": SIGHT_SECRET,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(api, data=data, timeout=aiohttp.ClientTimeout(total=25)) as r:
            return await r.json()

def classify(res: dict):
    n = res.get("nudity", {})
    if (n.get("sexual_activity", 0) > 0.3
            or n.get("sexual_display", 0) > 0.3
            or n.get("erotica", 0) > 0.5):
        return "porn"
    g = res.get("gambling", {})
    if g.get("gambling", 0) > 0.4:
        return "casino"
    qr = res.get("qr", {})
    if qr.get("prob", 0) > 0.3:
        cls = qr.get("classes", {}) or {}
        if cls.get("gambling", 0) > 0.3 or cls.get("casino", 0) > 0.3:
            return "casino"
        if (cls.get("unsafe", 0) > 0.3 or cls.get("adult", 0) > 0.3
                or cls.get("scam", 0) > 0.3):
            return "links"
    scam = res.get("scam", {})
    if scam.get("prob", 0) > 0.4:
        return "links"
    tii = res.get("text_in_image", {})
    if tii.get("prob", 0) > 0.3 and tii.get("has_links"):
        return "links"
    return None

def media_of(msg: Message):
    if msg.photo:
        return msg.photo[-1].file_id, "изображение"
    if msg.video:
        return msg.video.file_id, "видео"
    if msg.animation:
        return msg.animation.file_id, "GIF"
    if msg.document and msg.document.mime_type:
        mt = msg.document.mime_type
        if mt.startswith("image/"):
            return msg.document.file_id, "изображение"
        if mt.startswith("video/"):
            return msg.document.file_id, "видео"
    return None, None

def msg_for(kind: str, nick: str, media_word: str) -> str:
    if kind == "porn":
        return (f"Уважаемый {nick}, отправленное вами {media_word} "
                f"содержит материалы характера 18+ (порнографию/эротику). "
                f"Публикация и распространение не советуется.")
    if kind == "links":
        return (f"Уважаемый {nick}, на отправленном {media_word} "
                f"распознаны внешние ссылки или QR-код. "
                f"Лучше не отправлять туда, где есть я.")
    if kind == "casino":
        return (f"Уважаемый {nick}, на вашем {media_word} обнаружена "
                f"реклама азартных игр, ставок или казино.")
    return ""

# ================== ЛОГИКА ЛОГИНА ==================
def try_login_code(uid: int, code: str) -> str:
    cur.execute("SELECT * FROM login_codes WHERE code=?", (code,))
    row = cur.fetchone()
    if row is None:
        return "bad"
    if row["used_at"]:
        return "bad"
    days = row["days"] or 90
    sub = row["subscription"] or "Премиум"
    until = (datetime.now() + timedelta(days=days)).isoformat()
    set_u(uid, "logged_in", 1)
    set_u(uid, "subscription", sub)
    set_u(uid, "sub_until", until)
    if sub == "Германец":
        set_u(uid, "vip_title", "Я из Германии*")
    elif sub == "Премиум":
        set_u(uid, "vip_title", "Premium")
    cur.execute("UPDATE login_codes SET used_at=?, used_by=? WHERE code=?",
                (datetime.now().isoformat(), uid, code))
    conn.commit()
    return "ok"

# ================== ROUTER ==================
router = Router()

# ---------- /start ----------
@router.message(F.chat.type == ChatType.PRIVATE, F.text.func(is_start_cmd))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    row = ensure_user(msg.from_user)

    if is_logged(row):
        old = row["menu_message_id"]
        if old:
            try:
                await msg.bot.delete_message(msg.chat.id, old)
            except TelegramBadRequest:
                pass
        text, kb = main_text(row), main_kb()
        sent = await msg.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        set_u(msg.from_user.id, "menu_message_id", sent.message_id)
        try:
            await msg.bot.pin_chat_message(msg.chat.id, sent.message_id,
                                           disable_notification=True)
        except Exception:
            pass
        return

    sent = await msg.answer(LOGIN_REQUIRED, parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [btn(f"🔑 Получить код в @{MAIN_BOT_USERNAME}",
                                     url=f"https://t.me/{MAIN_BOT_USERNAME}")],
                            ]))
    set_u(msg.from_user.id, "menu_message_id", sent.message_id)
    await state.set_state(Form.waiting_code)

# ---------- Ввод кода ----------
@router.message(F.chat.type == ChatType.PRIVATE, Form.waiting_code)
async def login_input(msg: Message, state: FSMContext):
    text = (msg.text or "").strip()
    if is_any_cmd(text):
        await state.clear()
        return
    if not is_login_code(text):
        await msg.answer("❌ Код должен состоять из 11 цифр. Попробуй ещё раз.")
        return

    res = try_login_code(msg.from_user.id, text)
    if res == "bad":
        await msg.answer(LOGIN_BAD, parse_mode=ParseMode.HTML)
        return

    await state.clear()
    row = get_user(msg.from_user.id)
    old = row["menu_message_id"]
    if old:
        try:
            await msg.bot.delete_message(msg.chat.id, old)
        except TelegramBadRequest:
            pass
    sent = await msg.answer(LOGIN_SUCCESS, parse_mode=ParseMode.HTML)
    set_u(msg.from_user.id, "menu_message_id", sent.message_id)
    text2, kb2 = main_text(get_user(msg.from_user.id)), main_kb()
    menu = await msg.answer(text2, reply_markup=kb2, parse_mode=ParseMode.HTML)
    set_u(msg.from_user.id, "menu_message_id", menu.message_id)
    try:
        await msg.bot.pin_chat_message(msg.chat.id, menu.message_id,
                                       disable_notification=True)
    except Exception:
        pass

# ---------- /logout ----------
@router.message(F.chat.type == ChatType.PRIVATE, F.text.func(is_logout_cmd))
async def cmd_logout(msg: Message, state: FSMContext):
    await state.clear()
    row = ensure_user(msg.from_user)
    if not is_logged(row):
        await msg.answer("Ты и так не вошёл.")
        return
    set_u(msg.from_user.id, "logged_in", 0)
    set_u(msg.from_user.id, "subscription", None)
    set_u(msg.from_user.id, "sub_until", None)
    await msg.answer("🚪 Ты вышел из аккаунта.\nОтправь /start чтобы войти заново.",
                     parse_mode=ParseMode.HTML)

# ---------- /ПС ----------
@router.message(F.chat.type == ChatType.PRIVATE, F.text.func(is_ps_cmd))
async def cmd_ps(msg: Message):
    row = ensure_user(msg.from_user)
    if not is_logged(row):
        await msg.answer(LOGIN_REQUIRED, parse_mode=ParseMode.HTML)
        return
    mid = row["menu_message_id"]
    if mid:
        try:
            await msg.bot.edit_message_text(msg.chat.id, mid, AGREEMENT_TEXT,
                                            reply_markup=agreement_kb(),
                                            parse_mode=ParseMode.HTML)
            return
        except TelegramBadRequest:
            pass
    await msg.answer(AGREEMENT_TEXT, reply_markup=agreement_kb())

# ---------- /ос ----------
@router.message(F.chat.type == ChatType.PRIVATE, F.text.func(is_os_cmd))
async def cmd_os(msg: Message):
    row = ensure_user(msg.from_user)
    if not is_logged(row):
        await msg.answer(LOGIN_REQUIRED, parse_mode=ParseMode.HTML)
        return
    nv = 0 if row["opt_out"] else 1
    set_u(msg.from_user.id, "opt_out", nv)
    text = ("🚫 Вы отказались от сбора данных." if nv
            else "✅ Вы снова разрешили сбор данных.")
    mid = row["menu_message_id"]
    if mid:
        try:
            await msg.bot.edit_message_text(msg.chat.id, mid, text, reply_markup=back_kb())
            return
        except TelegramBadRequest:
            pass
    await msg.answer(text, reply_markup=back_kb())

# ---------- /помощь ----------
@router.message(F.chat.type == ChatType.PRIVATE, F.text.func(is_help_cmd))
async def cmd_help(msg: Message):
    row = ensure_user(msg.from_user)
    if not is_logged(row):
        await msg.answer(LOGIN_REQUIRED, parse_mode=ParseMode.HTML)
        return
    mid = row["menu_message_id"]
    if mid:
        try:
            await msg.bot.edit_message_text(msg.chat.id, mid, HELP_TEXT,
                                            reply_markup=back_kb(),
                                            parse_mode=ParseMode.HTML)
            return
        except TelegramBadRequest:
            pass
    await msg.answer(HELP_TEXT, reply_markup=back_kb())

# ---------- /ping ----------
@router.message(F.text.func(is_ping_cmd))
async def cmd_ping(msg: Message):
    t0 = time.time()
    sent = await msg.reply("Pong!")
    dt = (time.time() - t0) * 1000
    try:
        await sent.edit_text(f"Pong! {dt:.0f} ms")
    except TelegramBadRequest:
        pass

# ---------- /скан ----------
@router.message(F.text.func(is_scan_cmd))
async def cmd_scan(msg: Message):
    if msg.chat.type == ChatType.PRIVATE:
        row = ensure_user(msg.from_user)
        if not is_logged(row):
            await msg.answer(LOGIN_REQUIRED, parse_mode=ParseMode.HTML)
            return
    target = msg.reply_to_message or msg
    fid, media_word = media_of(target)
    if not fid:
        await msg.reply("📸 Отправь фото/видео/GIF с подписью /скан или ответь /скан на медиа.")
        return
    if msg.media_group_id:
        cnt = ALBUM_CNT[msg.media_group_id]
        if cnt >= MAX_MEDIA:
            return
        ALBUM_CNT[msg.media_group_id] = cnt + 1

    status = await msg.reply("📡 Сканирую изображение...")
    try:
        file = await msg.bot.get_file(fid)
        url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
        res = await sight_check(url)
    except Exception as e:
        try:
            await status.edit_text(f"❌ Ошибка сканирования: {e}")
        except TelegramBadRequest:
            pass
        return
    if res.get("status") != "success":
        try:
            await status.edit_text(f"❌ Sightengine: {res}")
        except TelegramBadRequest:
            pass
        return

    kind = classify(res)
    nick = msg.from_user.full_name if msg.from_user else "пользователь"
    is_private = msg.chat.type == ChatType.PRIVATE

    if kind:
        final = msg_for(kind, nick, media_word)
        try:
            await status.delete()
        except TelegramBadRequest:
            pass
        sent = await stream_answer(msg.bot, msg.chat.id, final, is_private)
        if not is_private and sent:
            try:
                await sent.edit_reply_markup(reply_markup=hide_kb())
            except TelegramBadRequest:
                pass
            asyncio.create_task(_auto_del(msg.bot, msg.chat.id, sent.message_id, 60))
    else:
        try:
            await status.edit_text("✅ Нарушений не найдено.")
        except TelegramBadRequest:
            pass

# ---------- /титул ----------
@router.message(F.chat.type == ChatType.PRIVATE, F.text.func(is_title_cmd))
async def cmd_title(msg: Message, state: FSMContext):
    row = ensure_user(msg.from_user)
    if not is_logged(row):
        await msg.answer(LOGIN_REQUIRED, parse_mode=ParseMode.HTML)
        return
    if not is_premium(row):
        await msg.answer("🎖 Титул доступен с Премиум или Германец.", reply_markup=back_kb())
        return
    await msg.answer("🎖 Пришли текст титула (до 30 символов).\n"
                     "Текущий: " + (row["vip_title"] or "нет"),
                     reply_markup=back_kb("main_menu"))
    await state.set_state(Form.waiting_title)

@router.message(Form.waiting_title, F.chat.type == ChatType.PRIVATE)
async def title_process(msg: Message, state: FSMContext):
    if is_any_cmd(msg.text or ""):
        return
    t = (msg.text or "").strip()[:30]
    if not t:
        return
    set_u(msg.from_user.id, "vip_title", t)
    await state.clear()
    await msg.answer(f"✅ Титул установлен: <b>{t}</b>",
                     reply_markup=back_kb("main_menu"))

# ---------- /статус ----------
@router.message(F.chat.type == ChatType.PRIVATE, F.text.func(is_status_cmd))
async def cmd_status(msg: Message, state: FSMContext):
    row = ensure_user(msg.from_user)
    if not is_logged(row):
        await msg.answer(LOGIN_REQUIRED, parse_mode=ParseMode.HTML)
        return
    if not is_premium(row):
        await msg.answer("💬 Кастомный статус доступен с Премиум или Германец.",
                         reply_markup=back_kb())
        return
    await msg.answer("💬 Пришли текст статуса (до 60 символов).\n"
                     "Текущий: " + (row["custom_status"] or "нет"),
                     reply_markup=back_kb("main_menu"))
    await state.set_state(Form.waiting_status)

@router.message(Form.waiting_status, F.chat.type == ChatType.PRIVATE)
async def status_process(msg: Message, state: FSMContext):
    if is_any_cmd(msg.text or ""):
        return
    t = (msg.text or "").strip()[:60]
    if not t:
        return
    set_u(msg.from_user.id, "custom_status", t)
    await state.clear()
    await msg.answer(f"✅ Статус установлен: <b>{t}</b>",
                     reply_markup=back_kb("main_menu"))

# ---------- /инвайт ----------
@router.message(F.chat.type == ChatType.PRIVATE, F.text.func(is_invite_cmd))
async def cmd_invite(msg: Message):
    row = ensure_user(msg.from_user)
    if not is_logged(row):
        await msg.answer(LOGIN_REQUIRED, parse_mode=ParseMode.HTML)
        return
    await msg.answer(
        f"🔗 Твоя ссылка-приглашение:\n"
        f"<code>https://t.me/{BOT_USERNAME}?start=inv_{row['user_id']}</code>\n\n"
        f"Приглашено: <b>{row['invites']}</b>",
        reply_markup=back_kb("main_menu"),
        parse_mode=ParseMode.HTML)

# ---------- /баланс ----------
@router.message(F.chat.type == ChatType.PRIVATE, F.text.func(is_balance_cmd))
async def cmd_balance(msg: Message):
    row = ensure_user(msg.from_user)
    if not is_logged(row):
        await msg.answer(LOGIN_REQUIRED, parse_mode=ParseMode.HTML)
        return
    await msg.answer(f"💳 Баланс: <b>{row['balance']} звёзд</b>\n"
                     f"Пополнить — кнопка в профиле.",
                     reply_markup=back_kb("main_menu"))

# ---------- /дейли ----------
@router.message(F.chat.type == ChatType.PRIVATE, F.text.func(is_daily_cmd))
async def cmd_daily(msg: Message):
    row = ensure_user(msg.from_user)
    if not is_logged(row):
        await msg.answer(LOGIN_REQUIRED, parse_mode=ParseMode.HTML)
        return
    set_u(msg.from_user.id, "balance", row["balance"] + 5)
    await msg.answer("🎁 Ежедневный бонус: <b>+5 звёзд</b>\n"
                     "Заходи завтра снова!",
                     reply_markup=back_kb("main_menu"))

# ---------- /статистика ----------
@router.message(F.chat.type == ChatType.PRIVATE, F.text.func(is_stats_cmd))
async def cmd_stats(msg: Message):
    row = ensure_user(msg.from_user)
    if not is_logged(row):
        await msg.answer(LOGIN_REQUIRED, parse_mode=ParseMode.HTML)
        return
    text = (f"📊 <b>Твоя статистика</b>\n\n"
            f"👤 ID: <code>{row['user_id']}</code>\n"
            f"📅 В боте с: {(row['first_start'] or '—')[:10]}\n"
            f"💬 Сообщений через бота: {row['msg_count']}\n"
            f"🔗 Приглашено: {row['invites']}\n"
            f"💎 Подписка: {row['subscription'] or 'Нету'}\n"
            f"💳 Баланс: {row['balance']} звёзд")
    await msg.answer(text, reply_markup=back_kb("main_menu"))

# ---------- /правила (группа) ----------
@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
                F.text.func(is_rules_cmd))
async def cmd_rules(msg: Message):
    row = get_group(msg.chat.id, msg.chat.title or "")
    if not row["rules"]:
        await msg.reply("📜 Правила ещё не заданы владельцем.")
        return
    await msg.reply(f"📜 <b>Правила чата:</b>\n\n{row['rules']}",
                    parse_mode=ParseMode.HTML)

# ---------- /инфо (группа) ----------
@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
                F.text.func(is_info_cmd))
async def cmd_info(msg: Message):
    row = get_group(msg.chat.id, msg.chat.title or "")
    try:
        members = await msg.bot.get_chat_member_count(msg.chat.id)
    except Exception:
        members = "?"
    filters = [dict(TOGGLES)[k] for k, _ in TOGGLES if row[k]]
    text = (f"ℹ️ <b>Информация о чате</b>\n\n"
            f"📌 Название: {msg.chat.title}\n"
            f"🆔 ID: <code>{msg.chat.id}</code>\n"
            f"👥 Участников: {members}\n"
            f"Активные фильтры: {', '.join(filters) if filters else '—'}")
    await msg.reply(text, parse_mode=ParseMode.HTML)

# ---------- /топ (группа) ----------
@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
                F.text.func(is_top_cmd))
async def cmd_top(msg: Message):
    await msg.reply("🏆 Топ участников по сообщениям — в разработке.")

# ---------- /Setting ----------
@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
                F.text.func(is_group_cmd))
async def cmd_setting(msg: Message):
    row = get_group(msg.chat.id, msg.chat.title or "")
    mid = row["menu_message_id"]
    kb = group_kb(row)
    text = group_text(row)
    if mid:
        try:
            await msg.bot.edit_message_text(msg.chat.id, mid, text, reply_markup=kb)
            return
        except TelegramBadRequest:
            pass
    sent = await msg.answer(text, reply_markup=kb)
    set_g(msg.chat.id, "menu_message_id", sent.message_id)

# ---------- Бот добавлен в группу ----------
@router.my_chat_member()
async def on_added(event: ChatMemberUpdated):
    if event.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    n, o = event.new_chat_member.status, event.old_chat_member.status
    if n not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        return
    if o in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        return
    get_group(event.chat.id, event.chat.title or "")
    owner = None
    try:
        admins = await event.bot.get_chat_administrators(event.chat.id)
        owner = next((a for a in admins if a.status == ChatMemberStatus.CREATOR), None)
    except Exception:
        pass
    text = f"Спасибо за добавление! Я — <b>{BOT_NAME}</b>, защита чатов.\n\n"
    mention = None
    if owner is not None:
        is_anon = getattr(owner, "is_anonymous", False)
        if not is_anon and owner.user:
            mention = f'<a href="tg://user?id={owner.user.id}">{owner.user.full_name}</a>'
    if mention:
        text += f"{mention}, настрой защиту через «настройки»."
    else:
        text += "Владелец, настрой защиту через «настройки»."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("⚙️ Открыть настройки", cb=f"gopen:{event.chat.id}")],
    ])
    try:
        sent = await event.bot.send_message(event.chat.id, text,
                                            reply_markup=kb, parse_mode=ParseMode.HTML)
        try:
            await event.bot.pin_chat_message(event.chat.id, sent.message_id,
                                             disable_notification=True)
        except TelegramBadRequest:
            pass
    except TelegramBadRequest:
        pass

# ---------- Модерация ----------
async def del_warn(bot, cid, msg, reason, mute=0):
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass
    try:
        await bot.send_message(cid, f"⚠️ {msg.from_user.mention_html()}: {reason}",
                               parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass
    if mute > 0 and msg.from_user:
        try:
            await bot.restrict_chat_member(
                cid, msg.from_user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=int(time.time()) + mute * 60)
        except TelegramBadRequest:
            pass

@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def group_filter(msg: Message):
    if msg.from_user and msg.from_user.is_bot:
        return
    text = msg.text or msg.caption or ""
    if is_any_cmd(text):
        return
    has_media = bool(msg.photo or msg.video or msg.animation or msg.document)
    if not text and not has_media:
        return

    row = get_group(msg.chat.id, msg.chat.title or "")
    now = time.time()

    if row["red_mode"] and msg.from_user:
        SPAM[msg.chat.id] = [t for t in SPAM[msg.chat.id] if now - t < 5]
        SPAM[msg.chat.id].append(now)
        if len(SPAM[msg.chat.id]) >= 15:
            RED_UNTIL[msg.chat.id] = now + 600
            SPAM[msg.chat.id].clear()
            try:
                await msg.bot.set_chat_permissions(msg.chat.id,
                                                   ChatPermissions(can_send_messages=False))
                await msg.bot.send_message(msg.chat.id,
                                           "🚨 Красный режим! Чат закрыт на 10 минут.")
            except TelegramBadRequest:
                pass
            asyncio.create_task(_unlock(msg.bot, msg.chat.id, 600))
            return

    if row["mats"] and has_any(text, BAD):
        await del_warn(msg.bot, msg.chat.id, msg, "мат запрещён.")
        return

    if row["links"] and URL_RE.search(text) and msg.from_user:
        WARNS[msg.chat.id][msg.from_user.id] += 1
        cnt = WARNS[msg.chat.id][msg.from_user.id]
        if cnt == 1:
            await del_warn(msg.bot, msg.chat.id, msg, "ссылки запрещены. Мут 5 мин.", 5)
        else:
            await del_warn(msg.bot, msg.chat.id, msg, "ссылки запрещены. Кик.")
            try:
                await msg.bot.ban_chat_member(msg.chat.id, msg.from_user.id)
                await msg.bot.unban_chat_member(msg.chat.id, msg.from_user.id)
            except TelegramBadRequest:
                pass
        return

    if row["casino"] and has_any(text, CASINO):
        await del_warn(msg.bot, msg.chat.id, msg, "казино/ставки запрещены.")
        return
    if row["threats"] and has_any(text, THREATS):
        await del_warn(msg.bot, msg.chat.id, msg, "угрозы запрещены. Мут 30 мин.", 30)
        return
    if row["mentions"] and re.search(r"@[\w\d_]{4,}", text):
        await del_warn(msg.bot, msg.chat.id, msg, "упоминания запрещены.")
        return
    if row["porn"] and has_any(text, PORN_W):
        await del_warn(msg.bot, msg.chat.id, msg, "порно-контент запрещён.")
        return

    if row["porn"] and msg.photo:
        if msg.media_group_id:
            cnt = ALBUM_CNT[msg.media_group_id]
            if cnt >= MAX_MEDIA:
                return
            ALBUM_CNT[msg.media_group_id] = cnt + 1
        try:
            photo = msg.photo[-1]
            file = await msg.bot.get_file(photo.file_id)
            url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
            res = await sight_check(url)
            kind = classify(res)
            if kind:
                nick = msg.from_user.full_name if msg.from_user else "пользователь"
                sent = await stream_answer(msg.bot, msg.chat.id,
                                           msg_for(kind, nick, "изображение"),
                                           is_private=False)
                if sent:
                    try:
                        await sent.edit_reply_markup(reply_markup=hide_kb())
                    except TelegramBadRequest:
                        pass
                    asyncio.create_task(_auto_del(msg.bot, msg.chat.id,
                                                  sent.message_id, 60))
                try:
                    await msg.delete()
                except TelegramBadRequest:
                    pass
        except Exception:
            pass

async def _auto_del(bot, cid, mid, sec):
    await asyncio.sleep(sec)
    try:
        await bot.delete_message(cid, mid)
    except TelegramBadRequest:
        pass

async def _unlock(bot, cid, sec):
    await asyncio.sleep(sec)
    try:
        await bot.set_chat_permissions(cid, ChatPermissions(
            can_send_messages=True, can_send_media_messages=True,
            can_send_other_messages=True, can_add_web_page_previews=True))
        await bot.send_message(cid, "Красный режим окончен. Чат открыт.")
    except TelegramBadRequest:
        pass

@router.message(F.new_chat_members)
async def on_new(msg: Message):
    if RED_UNTIL.get(msg.chat.id, 0) > time.time():
        for m in msg.new_chat_members:
            try:
                await msg.bot.ban_chat_member(msg.chat.id, m.id)
            except TelegramBadRequest:
                pass
        try:
            await msg.delete()
        except TelegramBadRequest:
            pass

# ---------- Владелец? ----------
async def is_owner(bot, cid, uid) -> bool:
    try:
        m = await bot.get_chat_member(cid, uid)
        return m.status == ChatMemberStatus.CREATOR
    except Exception:
        return False

# ---------- Колбэки ----------
@router.callback_query(F.data == "hide_msg")
async def cb_hide(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except TelegramBadRequest:
        pass
    await cb.answer()

@router.callback_query(F.data == "agreement")
async def cb_agr(cb: CallbackQuery):
    await safe_edit(cb.message, AGREEMENT_TEXT, agreement_kb())
    await cb.answer()

@router.callback_query(F.data == "back_to_start")
async def cb_bts(cb: CallbackQuery):
    row = ensure_user(cb.from_user)
    if not is_logged(row):
        await safe_edit(cb.message, LOGIN_REQUIRED, None)
        await cb.answer()
        return
    await safe_edit(cb.message, START_TEXT, start_kb(True))
    await cb.answer()

@router.callback_query(F.data == "accept")
async def cb_acc(cb: CallbackQuery):
    set_u(cb.from_user.id, "accepted", 1)
    row = get_user(cb.from_user.id)
    await safe_edit(cb.message, main_text(row), main_kb())
    await cb.answer("Спасибо! Соглашение принято.")

@router.callback_query(F.data == "main_menu")
async def cb_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    row = ensure_user(cb.from_user)
    if not is_logged(row):
        await cb.answer("Сначала войди по коду.", show_alert=True)
        return
    await safe_edit(cb.message, main_text(row), main_kb())
    await cb.answer()

@router.callback_query(F.data == "deco")
async def cb_deco(cb: CallbackQuery):
    try:
        await cb.message.edit_reply_markup(reply_markup=main_kb())
    except TelegramBadRequest:
        pass
    await cb.answer()

@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()

@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    row = ensure_user(cb.from_user)
    if not is_logged(row):
        await cb.answer("Сначала войди по коду.", show_alert=True)
        return
    await safe_edit(cb.message, HELP_TEXT, back_kb())
    await cb.answer()

@router.callback_query(F.data == "whoami")
async def cb_who(cb: CallbackQuery):
    text = (f"Я <b>{BOT_NAME}</b> — VIP-бот защиты чатов.\n\n"
            "Фильтры: маты, ссылки, казино, угрозы, порно, спам.\n"
            "Работаю 24/7, сканирую медиа через Sightengine.")
    await safe_edit(cb.message, text, back_kb())
    await cb.answer()

@router.callback_query(F.data == "support")
async def cb_sup(cb: CallbackQuery):
    row = ensure_user(cb.from_user)
    if not is_logged(row):
        await cb.answer("Сначала войди по коду.", show_alert=True)
        return
    if not row["subscription"]:
        text = "🆘 Поддержка доступна с Премиум или Германец."
    elif row["subscription"] == "Комфорт":
        text = "🆘 Поддержка: 15:00 – 16:00"
    elif row["subscription"] == "Премиум":
        text = "🆘 Поддержка: 13:30 – 17:00"
    else:
        text = "🆘 Поддержка: 13:30 – 22:00"
    await safe_edit(cb.message, text, back_kb())
    await cb.answer()

@router.callback_query(F.data == "logout")
async def cb_logout(cb: CallbackQuery):
    set_u(cb.from_user.id, "logged_in", 0)
    set_u(cb.from_user.id, "subscription", None)
    set_u(cb.from_user.id, "sub_until", None)
    await safe_edit(cb.message, "🚪 Ты вышел из аккаунта. Отправь /start чтобы войти.",
                    None)
    await cb.answer()

@router.callback_query(F.data.startswith("gopen:"))
async def cb_gopen(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    if not await is_owner(cb.bot, cid, cb.from_user.id):
        await cb.answer("Только владелец может открыть настройки.", show_alert=True)
        return
    row = get_group(cid)
    try:
        await cb.message.edit_text(group_text(row), reply_markup=group_kb(row))
    except TelegramBadRequest:
        pass
    await cb.answer()

@router.callback_query(F.data.startswith("ginfo:"))
async def cb_ginfo(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    text = "<b>Что делают фильтры:</b>\n\n" + "\n\n".join(DESCR[k] for k, _ in TOGGLES)
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("« Назад", cb=f"gopen:{cid}")]])
    await safe_edit(cb.message, text, kb)
    await cb.answer()

@router.callback_query(F.data.startswith("grules:"))
async def cb_grules(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    row = get_group(cid)
    text = row["rules"] or "Правила ещё не заданы."
    await safe_edit(cb.message, f"📜 <b>Правила чата:</b>\n\n{text}",
                    InlineKeyboardMarkup(inline_keyboard=[
                        [btn("« Назад", cb=f"gopen:{cid}")]]))
    await cb.answer()

@router.callback_query(F.data.startswith("glinvite:"))
async def cb_glinvite(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    if not await is_owner(cb.bot, cid, cb.from_user.id):
        await cb.answer("Только владелец.", show_alert=True)
        return
    try:
        link = await cb.bot.create_chat_invite_link(cid)
        await safe_edit(cb.message,
                        f"🔗 Инвайт-ссылка:\n<code>{link.invite_link}</code>",
                        InlineKeyboardMarkup(inline_keyboard=[
                            [btn("« Назад", cb=f"gopen:{cid}")]]))
    except Exception as e:
        await cb.answer(f"Ошибка: {e}", show_alert=True)
    await cb.answer()

@router.callback_query(F.data.startswith("gtog:"))
async def cb_gtog(cb: CallbackQuery):
    _, cid_s, key = cb.data.split(":")
    cid = int(cid_s)
    if not await is_owner(cb.bot, cid, cb.from_user.id):
        await cb.answer("Только владелец может менять настройки.", show_alert=True)
        return
    row = get_group(cid)
    set_g(cid, key, 0 if row[key] else 1)
    row = get_group(cid)
    try:
        await cb.message.edit_reply_markup(reply_markup=group_kb(row))
    except TelegramBadRequest:
        pass
    state_txt = "включено" if row[key] else "выключено"
    await cb.answer(f"{dict(TOGGLES)[key]}: {state_txt}")

@router.callback_query(F.data == "settings")
async def cb_set(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    row = ensure_user(cb.from_user)
    if not is_logged(row):
        await cb.answer("Сначала войди по коду.", show_alert=True)
        return
    await safe_edit(cb.message, "⚙️ <b>Настройки профиля</b>", settings_kb(row))
    await cb.answer()

@router.callback_query(F.data == "set_male")
async def cb_m(cb: CallbackQuery):
    set_u(cb.from_user.id, "gender", "male")
    row = get_user(cb.from_user.id)
    await safe_edit(cb.message, "⚙️ <b>Настройки профиля</b>", settings_kb(row))
    await cb.answer("Пол: Мужской")

@router.callback_query(F.data == "set_female")
async def cb_f(cb: CallbackQuery):
    set_u(cb.from_user.id, "gender", "female")
    row = get_user(cb.from_user.id)
    await safe_edit(cb.message, "⚙️ <b>Настройки профиля</b>", settings_kb(row))
    await cb.answer("Пол: Женский")

@router.callback_query(F.data == "set_name")
async def cb_sn(cb: CallbackQuery, state: FSMContext):
    row = get_user(cb.from_user.id)
    g = row["gender"]
    title = {"male": "Твоё имя, господин",
             "female": "Твоё имя, госпожа"}.get(g, "Твоё имя")
    await safe_edit(cb.message, f"{title}. Как тебя звать?", back_kb("settings"))
    await state.set_state(Form.waiting_name)
    await cb.answer()

@router.message(Form.waiting_name, F.chat.type == ChatType.PRIVATE)
async def proc_name(msg: Message, state: FSMContext):
    name = (msg.text or "").strip()[:50]
    if not name:
        return
    set_u(msg.from_user.id, "custom_name", name)
    await state.clear()
    row = get_user(msg.from_user.id)
    mid = row["menu_message_id"]
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("« В меню", cb="main_menu")]])
    if mid:
        try:
            await msg.bot.edit_message_text(msg.chat.id, mid, f"Привет, {name}!",
                                            reply_markup=kb)
            return
        except TelegramBadRequest:
            pass
    await msg.answer(f"Привет, {name}!", reply_markup=kb)

# ---------- ПРОФИЛЬ ----------
@router.callback_query(F.data == "profile")
async def cb_prof(cb: CallbackQuery):
    row = ensure_user(cb.from_user)
    if not is_logged(row):
        await cb.answer("Сначала войди по коду.", show_alert=True)
        return
    nick = f"@{row['username']}" if row["username"] else "—"
    d = (row["first_start"] or "—")[:10]
    sub = row["subscription"] or "Нету"
    until = row["sub_until"] or "—"
    sub_line = f"{sub} (до {until[:10]})" if row["subscription"] and row["sub_until"] else sub
    title = f"\n🎖 Титул: <b>{row['vip_title']}</b>" if row["vip_title"] else ""
    status = f"\n💬 Статус: <i>{row['custom_status']}</i>" if row["custom_status"] else ""
    text = (f"👤 <b>Профиль</b>\n\n"
            f"🆔 <code>{row['user_id']}</code>\n"
            f"📛 Ник: {nick}\n"
            f"📅 В боте с: {d}\n"
            f"💎 Подписка: {sub_line}{title}{status}\n"
            f"💳 Баланс: {row['balance']} звёзд\n"
            f"🔗 Приглашено: {row['invites']}")
    await safe_edit(cb.message, text, profile_kb(row))
    await cb.answer()

@router.callback_query(F.data == "my_stats")
async def cb_mystats(cb: CallbackQuery):
    row = ensure_user(cb.from_user)
    if not is_logged(row):
        await cb.answer("Сначала войди по коду.", show_alert=True)
        return
    text = (f"📊 <b>Статистика</b>\n\n"
            f"💬 Сообщений через бота: {row['msg_count']}\n"
            f"🔗 Приглашено: {row['invites']}\n"
            f"💳 Баланс: {row['balance']} звёзд\n"
            f"💎 Подписка: {row['subscription'] or 'нету'}")
    await safe_edit(cb.message, text, back_kb("main_menu"))
    await cb.answer()

@router.callback_query(F.data == "invite")
async def cb_inv(cb: CallbackQuery):
    row = ensure_user(cb.from_user)
    if not is_logged(row):
        await cb.answer("Сначала войди по коду.", show_alert=True)
        return
    text = (f"🔗 Твоя ссылка-приглашение:\n"
            f"<code>https://t.me/{BOT_USERNAME}?start=inv_{row['user_id']}</code>\n\n"
            f"Приглашено: <b>{row['invites']}</b>")
    await safe_edit(cb.message, text, back_kb("profile"))
    await cb.answer()

@router.callback_query(F.data == "title_menu")
async def cb_title_menu(cb: CallbackQuery, state: FSMContext):
    row = ensure_user(cb.from_user)
    if not is_logged(row):
        await cb.answer("Сначала войди по коду.", show_alert=True)
        return
    if not is_premium(row):
        await cb.answer("🎖 Титул — только Премиум+.", show_alert=True)
        return
    await safe_edit(cb.message,
                    "🎖 Пришли текст титула (до 30 символов).\n"
                    "Для отмены — /start.",
                    back_kb("profile"))
    await state.set_state(Form.waiting_title)
    await cb.answer()

@router.callback_query(F.data == "status_menu")
async def cb_status_menu(cb: CallbackQuery, state: FSMContext):
    row = ensure_user(cb.from_user)
    if not is_logged(row):
        await cb.answer("Сначала войди по коду.", show_alert=True)
        return
    if not is_premium(row):
        await cb.answer("💬 Статус — только Премиум+.", show_alert=True)
        return
    await safe_edit(cb.message,
                    "💬 Пришли текст статуса (до 60 символов).\n"
                    "Для отмены — /start.",
                    back_kb("profile"))
    await state.set_state(Form.waiting_status)
    await cb.answer()

# ---------- БАЛАНС ----------
@router.callback_query(F.data == "topup")
async def cb_top(cb: CallbackQuery):
    row = ensure_user(cb.from_user)
    if not is_logged(row):
        await cb.answer("Сначала войди по коду.", show_alert=True)
        return
    await safe_edit(cb.message, "💳 Выбери сумму пополнения:", topup_kb())
    await cb.answer()

@router.callback_query(F.data.startswith("topup_"))
async def cb_topup(cb: CallbackQuery):
    row = ensure_user(cb.from_user)
    if not is_logged(row):
        await cb.answer("Сначала войди по коду.", show_alert=True)
        return
    amount = int(cb.data.split("_")[1])
    await cb.message.answer_invoice(
        title=f"Пополнение баланса на {amount} звёзд",
        description=f"Зачисление {amount} звёзд на баланс {BOT_NAME}.",
        payload=f"topup:{amount}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Звёзды", amount=amount)],
    )
    await cb.answer()

# ---------- ПОДПИСКИ ----------
SUB_PRICES = {"comfort": 110, "premium": 199, "german": 350}
SUB_NAMES = {"comfort": "Комфорт", "premium": "Премиум", "german": "Германец"}

@router.callback_query(F.data == "subscription")
async def cb_sub(cb: CallbackQuery):
    row = ensure_user(cb.from_user)
    if not is_logged(row):
        await cb.answer("Сначала войди по коду.", show_alert=True)
        return
    text = (f"<b>Подписки {BOT_NAME}</b> — на 3 месяца\n\n"
            f"💎 <b>Комфорт</b> — базовая поддержка, кастом-статусы\n"
            f"🌟 <b>Премиум</b> — титулы, точный скан\n"
            f"🎩 <b>Германец</b> — всё выше + расширенная поддержка + VIP\n\n"
            f"⚠️ Покупка — в @{MAIN_BOT_USERNAME}.\n"
            f"После покупки ты получишь 11-значный код для входа сюда.")
    await safe_edit(cb.message, text, sub_kb())
    await cb.answer()

def sub_page(code, title, body):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn(f"🔑 Купить в @{MAIN_BOT_USERNAME}",
             url=f"https://t.me/{MAIN_BOT_USERNAME}")],
        [btn("« Назад", cb="subscription")],
    ])
    return f"<b>{title}</b>\n\n{body}", kb

@router.callback_query(F.data == "sub_comfort")
async def cb_sc(cb: CallbackQuery):
    t, k = sub_page("comfort", "💎 Комфорт",
                    "• Поддержка (15:00–16:00)\n"
                    "• Больше взаимодействия в группах\n"
                    "• Кастомные статусы\n"
                    "• Приоритет в чате\n\n"
                    "💰 110 звёзд / 3 месяца")
    await safe_edit(cb.message, t, k)
    await cb.answer()

@router.callback_query(F.data == "sub_premium")
async def cb_sp(cb: CallbackQuery):
    t, k = sub_page("premium", "🌟 Премиум",
                    "• Поддержка (13:30–17:00)\n"
                    "• Точность распознавания медиа\n"
                    "• Больше взаимодействия\n"
                    "• 🎖 Титулы в профиле\n"
                    "• Кастом VIP-бот\n\n"
                    "💰 199 звёзд / 3 месяца")
    await safe_edit(cb.message, t, k)
    await cb.answer()

@router.callback_query(F.data == "sub_german")
async def cb_sg(cb: CallbackQuery):
    t, k = sub_page("german", "🎩 Германец",
                    "! Это не оскорбление — просто название "
                    "из-за инфы, что в Германии лучше товары. "
                    "Времена 1941–1945 ни при чём.\n"
                    "* — просто шутка.\n\n"
                    "• Поддержка (13:30–22:00)\n"
                    "• Кастом VIP-бот\n"
                    "• Больше взаимодействия\n"
                    "• 🎖 Титул «Я из Германии*»\n"
                    "• Приоритет во всех чатах\n\n"
                    "💰 350 звёзд / 3 месяца")
    await safe_edit(cb.message, t, k)
    await cb.answer()

@router.pre_checkout_query()
async def on_pcq(q: PreCheckoutQuery):
    await q.answer(ok=True)

@router.message(F.successful_payment)
async def on_paid(msg: Message):
    p = msg.successful_payment.invoice_payload
    if p.startswith("topup:"):
        add = int(p.split(":")[1])
        row = ensure_user(msg.from_user)
        set_u(msg.from_user.id, "balance", row["balance"] + add)
        await msg.answer(f"✅ Баланс пополнен на {add} звёзд.")

# ================== MAIN TEXT ==================
def main_text(row):
    uid = row["user_id"]
    name = row["custom_name"] or row["first_name"] or "друг"
    mention = f'<a href="tg://user?id={uid}">{name}</a>'
    now = datetime.now()
    greeting, gdate = row["greeting"], row["greeting_date"]
    need = True
    if greeting and gdate:
        try:
            if (now - datetime.fromisoformat(gdate)).total_seconds() < 86400:
                need = False
        except Exception:
            pass
    if need:
        greeting = random.choice(GREETINGS)
        cur.execute("UPDATE users SET greeting=?, greeting_date=? WHERE user_id=?",
                    (greeting, now.isoformat(), uid))
        conn.commit()
    inactive = False
    if row["last_seen"]:
        try:
            if (now - datetime.fromisoformat(row["last_seen"])).days >= 3:
                inactive = True
        except Exception:
            pass
    sub_line = ""
    if row["subscription"]:
        sub_line = f"\n\n💎 Подписка: <b>{row['subscription']}</b>"
    if inactive:
        return (f"Здравствуй, {mention}.\n"
                f"Эй, мне скучно тоже. Может начнем работу?{sub_line}")
    return f"Здравствуй, {mention}.\n{greeting}{sub_line}"

# ================== ЗАПУСК ==================
async def main():
    if not acquire_lock():
        print("❌ Другой инстанс уже запущен. Выхожу.")
        sys.exit(0)
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(DedupMiddleware())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    print(f"✅ {BOT_NAME} запущен (@{BOT_USERNAME}).")
    await dp.start_polling(bot, drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())