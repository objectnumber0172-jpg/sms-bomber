# -*- coding: utf-8 -*-
import telebot
from telebot import types
import sqlite3
import html
import requests
from datetime import datetime

# ==================== КОНФИГ ====================
TOKEN = "8838023768:AAEoLb4hRVQboHD7vo9TgLp94Z6WpKmCGYQ"
ADMIN_ID = 8544445592
OWNER_USER = "@hy3rm1z"
OWNER_CH = "@hy3rm1zbio"
CAPTCHA_CODE = "SHjPtN75!"

PRICES = {"bot": 3000, "channel": 1200, "group": 1500}
INF = 10 ** 12

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
BOT_USERNAME = bot.get_me().username

states = {}
STYLE_MAP = {"blue": "primary", "green": "success", "red": "danger"}

# ==================== БД ====================
def db():
    conn = sqlite3.connect("rubles.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY,
        username TEXT,
        reg_date TEXT,
        refs INTEGER DEFAULT 0,
        ref_by INTEGER,
        balance INTEGER DEFAULT 0,
        theme TEXT DEFAULT 'blue',
        captcha INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT,
        name TEXT,
        link TEXT,
        chat_id TEXT,
        reward INTEGER,
        condition TEXT,
        active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS done(
        user_id INTEGER, task_id INTEGER, date TEXT,
        PRIMARY KEY(user_id, task_id)
    );
    CREATE TABLE IF NOT EXISTS complaints(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER, user_id INTEGER, reason TEXT, date TEXT
    );
    CREATE TABLE IF NOT EXISTS ads(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, type TEXT, link TEXT,
        status TEXT DEFAULT 'new', date TEXT
    );
    CREATE TABLE IF NOT EXISTS child_bots(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, token TEXT, username TEXT, date TEXT
    );
    """)
    c.commit()
    c.close()

# ==================== УТИЛИТЫ ====================
def esc(x):
    return html.escape(str(x)) if x is not None else ""

def get_user(uid):
    c = db()
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    c.close()
    return u

def ensure_user(uid, username=None, ref_by=None):
    c = db()
    cur = c.cursor()
    u = cur.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        bal = INF if uid == ADMIN_ID else 0
        cur.execute(
            "INSERT INTO users(id, username, reg_date, ref_by, balance, theme, captcha) VALUES(?,?,?,?,?,?,0)",
            (uid, username, datetime.now().strftime("%d.%m.%Y"), ref_by, bal, "blue"),
        )
        if ref_by and ref_by != uid:
            cur.execute("UPDATE users SET refs = refs + 1 WHERE id=?", (ref_by,))
        c.commit()
    elif username and u["username"] != username:
        cur.execute("UPDATE users SET username=? WHERE id=?", (username, uid))
        c.commit()
    u = cur.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    c.close()
    return u

def set_theme(uid, theme):
    c = db()
    c.execute("UPDATE users SET theme=? WHERE id=?", (theme, uid))
    c.commit()
    c.close()

def get_theme(uid):
    u = get_user(uid)
    return (u["theme"] if u and u["theme"] else "blue")

def styled_kb(row_width=1):
    return types.InlineKeyboardMarkup(row_width=row_width)

def add_styled_button(kb, uid, text, callback_data=None, url=None, **kwargs):
    style = STYLE_MAP.get(get_theme(uid), "primary")
    btn_kwargs = {"text": text, "style": style, **kwargs}
    if callback_data:
        btn_kwargs["callback_data"] = callback_data
    if url:
        btn_kwargs["url"] = url
    # Фоллбек для старых версий pyTelegramBotAPI, если 'style' не принимается напрямую
    try:
        btn = types.InlineKeyboardButton(**btn_kwargs)
    except TypeError:
        btn_kwargs.pop("style")
        btn_kwargs["api_kwargs"] = {"style": style}
        btn = types.InlineKeyboardButton(**btn_kwargs)
    kb.add(btn)
    return btn

def ref_link(uid):
    return f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"

def ref_rank(uid):
    c = db()
    rows = c.execute("SELECT id FROM users ORDER BY refs DESC").fetchall()
    c.close()
    for i, r in enumerate(rows, 1):
        if r["id"] == uid:
            return i
    return len(rows) + 1

def bal_text(u):
    if u["id"] == ADMIN_ID or u["balance"] >= INF:
        return "∞"
    return str(u["balance"])

def change_balance(uid, amount):
    if uid == ADMIN_ID:
        return
    c = db()
    u = c.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        c.close()
        return
    nb = max(0, u["balance"] + amount)
    c.execute("UPDATE users SET balance=? WHERE id=?", (nb, uid))
    c.commit()
    c.close()

def check_sub(chat_id, uid):
    try:
        m = bot.get_chat_member(chat_id, uid)
        return m.status in ("member", "administrator", "creator", "restricted")
    except Exception:
        return None

# ==================== КЛАВИАТУРЫ ====================
MAIN_TEXT = "Рубль - платформа для продвижения каналов, групп или ботов в telegram."

def main_menu_kb(uid):
    kb = styled_kb(row_width=2)
    add_styled_button(kb, uid, "💸 Заработать", callback_data="earn")
    add_styled_button(kb, uid, "🔗 Реклама", callback_data="ads")
    add_styled_button(kb, uid, "👤 Профиль", callback_data="profile")
    add_styled_button(kb, uid, "🤖 Наши боты", callback_data="our_bots")
    add_styled_button(kb, uid, "➕ Свой бот", callback_data="own_bot")
    add_styled_button(kb, uid, "🆕 Создать бота", callback_data="create_bot")
    return kb

def back_main_kb(uid):
    kb = styled_kb(row_width=1)
    add_styled_button(kb, uid, "« Назад", callback_data="back_main")
    return kb

# ==================== /start ====================
@bot.message_handler(commands=["start"])
def cmd_start(m):
    uid = m.from_user.id
    ref_by = None
    parts = m.text.split()
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            ref_by = int(parts[1][4:])
        except Exception:
            ref_by = None
    ensure_user(uid, m.from_user.username, ref_by)

    kb = styled_kb(row_width=1)
    add_styled_button(kb, uid, "Начнем?", callback_data="start_captcha")
    bot.send_message(
        m.chat.id,
        "Добро пожаловать в <b>Рубль</b>.\n"
        "Мы помогаем продвигать ваши каналы, боты, чаты.",
        reply_markup=kb,
    )

# ==================== КАПЧА ====================
@bot.callback_query_handler(func=lambda c: c.data == "start_captcha")
def cb_start_captcha(c):
    uid = c.from_user.id
    states[uid] = {"state": "captcha"}
    bot.answer_callback_query(c.id)
    bot.send_message(
        c.message.chat.id,
        f"Пройдите капчу. Введите <code>{CAPTCHA_CODE}</code>",
        protect_content=True,
    )

@bot.message_handler(
    func=lambda m: states.get(m.from_user.id, {}).get("state") == "captcha",
    content_types=["text"],
)
def handle_captcha(m):
    uid = m.from_user.id
    txt = (m.text or "").strip()
    if txt == CAPTCHA_CODE:
        c = db()
        c.execute("UPDATE users SET captcha=1 WHERE id=?", (uid,))
        c.commit()
        c.close()
        states.pop(uid, None)
        bot.send_message(m.chat.id, MAIN_TEXT, reply_markup=main_menu_kb(uid))
    else:
        bot.send_message(m.chat.id, "Ошибка:\nНеверно. Попробуйте снова!")

# ==================== ГЛАВНЫЙ ДИСПЕТЧЕР ====================
@bot.callback_query_handler(func=lambda c: True)
def cb_router(c):
    uid = c.from_user.id
    u = get_user(uid)
    if not u:
        ensure_user(uid, c.from_user.username)
        u = get_user(uid)
    data = c.data or ""

    # ---------- главное меню ----------
    if data == "back_main":
        try:
            bot.edit_message_text(MAIN_TEXT, c.message.chat.id, c.message.message_id,
                                  reply_markup=main_menu_kb(uid))
        except Exception:
            bot.send_message(c.message.chat.id, MAIN_TEXT, reply_markup=main_menu_kb(uid))
        return bot.answer_callback_query(c.id)

    # ---------- ЗАРАБОТАТЬ ----------
    if data == "earn":
        kb = styled_kb(row_width=3)
        add_styled_button(kb, uid, "Каналы", callback_data="earn_channel")
        add_styled_button(kb, uid, "Группы", callback_data="earn_group")
        add_styled_button(kb, uid, "Боты", callback_data="earn_bot")
        add_styled_button(kb, uid, "« Назад", callback_data="back_main")
        try:
            bot.edit_message_text(
                "Выполните условия и получите сумму, указанную за подписку.",
                c.message.chat.id, c.message.message_id, reply_markup=kb,
            )
        except Exception:
            bot.send_message(c.message.chat.id, "Выполните условия и получите сумму, указанную за подписку.",
                             reply_markup=kb)
        return bot.answer_callback_query(c.id)

    if data.startswith("earn_"):
        ttype = data.split("_", 1)[1]
        titles = {"channel": "Каналы:", "group": "Группы:", "bot": "Боты:"}
        conn = db()
        tasks = conn.execute(
            "SELECT * FROM tasks WHERE type=? AND active=1 ORDER BY id DESC", (ttype,)
        ).fetchall()
        done_ids = {r["task_id"] for r in conn.execute(
            "SELECT task_id FROM done WHERE user_id=?", (uid,)).fetchall()}
        conn.close()

        if ttype == "bot":
            kb = styled_kb(row_width=2)
            add_styled_button(kb, uid, "Без условий", callback_data="earn_bot_simple")
            add_styled_button(kb, uid, "С условиями", callback_data="earn_bot_cond")
            add_styled_button(kb, uid, "« Назад", callback_data="earn")
            try:
                bot.edit_message_text("Выберите:", c.message.chat.id, c.message.message_id, reply_markup=kb)
            except Exception:
                bot.send_message(c.message.chat.id, "Выберите:", reply_markup=kb)
            return bot.answer_callback_query(c.id)

        kb = styled_kb(row_width=2)
        rows = []
        for t in tasks:
            if t["id"] in done_ids:
                continue
            rows.append(types.InlineKeyboardButton(f"💸 {t['reward']}", callback_data=f"task_{t['id']}"))
            rows.append(types.InlineKeyboardButton("Выполнять", callback_data=f"task_{t['id']}"))
        if rows:
            kb.add(*rows)
        add_styled_button(kb, uid, "« Назад", callback_data="earn")
        text = titles.get(ttype, "Список:") + ("" if rows else "\n\nПока нет доступных заданий.")
        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
        except Exception:
            bot.send_message(c.message.chat.id, text, reply_markup=kb)
        return bot.answer_callback_query(c.id)

    if data in ("earn_bot_simple", "earn_bot_cond"):
        conn = db()
        tasks = conn.execute("SELECT * FROM tasks WHERE type='bot' AND active=1 ORDER BY id DESC").fetchall()
        done_ids = {r["task_id"] for r in conn.execute(
            "SELECT task_id FROM done WHERE user_id=?", (uid,)).fetchall()}
        conn.close()
        kb = styled_kb(row_width=2)
        rows = []
        for t in tasks:
            if t["id"] in done_ids:
                continue
            rows.append(types.InlineKeyboardButton(f"💸 {t['reward']}", callback_data=f"task_{t['id']}"))
            rows.append(types.InlineKeyboardButton("Выполнять", callback_data=f"task_{t['id']}"))
        if rows:
            kb.add(*rows)
        add_styled_button(kb, uid, "« Назад", callback_data="earn_bot")
        text = "Без условий:" if data.endswith("simple") else "С условиями:"
        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
        except Exception:
            bot.send_message(c.message.chat.id, text, reply_markup=kb)
        return bot.answer_callback_query(c.id)

    # ---------- КАРТОЧКА ЗАДАНИЯ ----------
    if data.startswith("task_"):
        tid = int(data.split("_", 1)[1])
        conn = db()
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        already = conn.execute("SELECT 1 FROM done WHERE user_id=? AND task_id=?", (uid, tid)).fetchone()
        conn.close()
        if not t:
            return bot.answer_callback_query(c.id, "Задание не найдено", show_alert=True)
        if already:
            return bot.answer_callback_query(c.id, "Вы уже выполнили это задание", show_alert=True)

        kind = {"channel": "Канал", "group": "Группа", "bot": "Бот"}.get(t["type"], "Канал")
        cond = t["condition"] or (
            "Нажать /start и не блокировать бота в течении 7 дней."
            if t["type"] == "bot" else
            "Подписаться и остаться в течении 7 дней."
        )
        punish = ("Блокировка аккаунта в данном боте" if t["type"] == "bot"
                  else "Блокировка аккаунта в боте")
        text = (
            f"<b>{kind}:</b> {esc(t['name'])}\n"
            f"🔗 {esc(t['link'])}\n\n"
            f"<b>Условие:</b> {esc(cond)}\n"
            f"<b>Наказание:</b> {esc(punish)}"
        )
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton(
            "🚀 Зайти" if t["type"] == "bot" else "📢 Подписаться",
            url=t["link"]
        ))
        kb.add(types.InlineKeyboardButton("✅ Я выполнил / Проверить", callback_data=f"check_{tid}"))
        row_kb = styled_kb(row_width=2)
        add_styled_button(row_kb, uid, "« Назад", callback_data=f"earn_{t['type']}")
        add_styled_button(row_kb, uid, "✎ Пожаловаться", callback_data=f"complain_{tid}")
        kb.add(*row_kb.keyboard[0])
        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb,
                                  disable_web_page_preview=True)
        except Exception:
            bot.send_message(c.message.chat.id, text, reply_markup=kb, disable_web_page_preview=True)
        return bot.answer_callback_query(c.id)

    # ---------- ПРОВЕРКА ВЫПОЛНЕНИЯ ----------
    if data.startswith("check_"):
        tid = int(data.split("_", 1)[1])
        conn = db()
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        already = conn.execute("SELECT 1 FROM done WHERE user_id=? AND task_id=?", (uid, tid)).fetchone()
        conn.close()
        if not t:
            return bot.answer_callback_query(c.id, "Задание не найдено", show_alert=True)
        if already:
            return bot.answer_callback_query(c.id, "Уже выполнено", show_alert=True)

        ok = True
        if t["type"] in ("channel", "group") and t["chat_id"]:
            res = check_sub(t["chat_id"], uid)
            if res is None:
                return bot.answer_callback_query(
                    c.id, "Не удалось проверить. Убедитесь что вы подписались.", show_alert=True
                )
            ok = res
        if not ok:
            return bot.answer_callback_query(c.id, "❌ Вы ещё не выполнили условие.", show_alert=True)

        conn = db()
        conn.execute("INSERT OR IGNORE INTO done(user_id, task_id, date) VALUES(?,?,?)",
                     (uid, tid, datetime.now().strftime("%d.%m.%Y %H:%M")))
        conn.commit()
        conn.close()
        change_balance(uid, t["reward"])
        bot.answer_callback_query(c.id, f"✅ Начислено {t['reward']} ₽", show_alert=True)
        try:
            bot.edit_message_text(
                f"✅ Задание выполнено!\nВам начислено <b>{t['reward']}</b> ₽.",
                c.message.chat.id, c.message.message_id,
                reply_markup=back_main_kb(uid),
            )
        except Exception:
            pass
        bot.send_message(c.message.chat.id,
                         f"Заказать бота на подобие этого можно у {OWNER_USER}.\nПодробности: {OWNER_CH}")
        return

    # ---------- ЖАЛОБА ----------
    if data.startswith("complain_"):
        tid = int(data.split("_", 1)[1])
        conn = db()
        t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        conn.close()
        if not t:
            return bot.answer_callback_query(c.id, "Не найдено", show_alert=True)
        reasons = ["Скам бот", "Докс бот", "Логгер", "Стиллер", "Мошенничество", "Другое"] \
            if t["type"] == "bot" else \
            ["Канал с порнографией" if t["type"] == "channel" else "Группа с порнографией", "Другое"]
        kb = styled_kb(row_width=1)
        for i, r in enumerate(reasons):
            add_styled_button(kb, uid, r, callback_data=f"creason_{tid}_{i}")
        add_styled_button(kb, uid, "« Назад", callback_data=f"task_{tid}")
        states[uid] = {"state": "complaint", "task_id": tid, "reasons": reasons}
        try:
            bot.edit_message_text("Выберите причину:", c.message.chat.id, c.message.message_id, reply_markup=kb)
        except Exception:
            bot.send_message(c.message.chat.id, "Выберите причину:", reply_markup=kb)
        return bot.answer_callback_query(c.id)

    if data.startswith("creason_"):
        _, tid, idx = data.split("_")
        tid, idx = int(tid), int(idx)
        st = states.get(uid, {})
        reasons = st.get("reasons") or ["Другое"]
        reason = reasons[idx] if idx < len(reasons) else "Другое"
        conn = db()
        conn.execute("INSERT INTO complaints(task_id, user_id, reason, date) VALUES(?,?,?,?)",
                     (tid, uid, reason, datetime.now().strftime("%d.%m.%Y %H:%M")))
        conn.commit()
        conn.close()
        states.pop(uid, None)
        bot.answer_callback_query(c.id, "Жалоба отправлена", show_alert=True)
        try:
            bot.edit_message_text("✅ Жалоба отправлена администрации.", c.message.chat.id,
                                  c.message.message_id, reply_markup=back_main_kb(uid))
        except Exception:
            pass
        try:
            bot.send_message(ADMIN_ID, f"⚠️ Новая жалоба от <a href='tg://user?id={uid}'>{uid}</a>\n"
                                       f"Задание #{tid}\nПричина: {esc(reason)}")
        except Exception:
            pass
        return

    # ---------- РЕКЛАМА ----------
    if data == "ads":
        kb = styled_kb(row_width=3)
        add_styled_button(kb, uid, "🤖 Бот", callback_data="ad_bot")
        add_styled_button(kb, uid, "👥 Группа", callback_data="ad_group")
        add_styled_button(kb, uid, "📢 Канал", callback_data="ad_channel")
        add_styled_button(kb, uid, "« Назад", callback_data="back_main")
        text = (
            "Выберите, что прорекламировать:\n\n"
            f"💠 Ваш баланс: <b>{bal_text(u)}</b> ₽\n\n"
            f"Цены:\n• Бот — 3000 ₽\n• Канал — 1200 ₽\n• Группа — 1500 ₽"
        )
        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
        except Exception:
            bot.send_message(c.message.chat.id, text, reply_markup=kb)
        return bot.answer_callback_query(c.id)

    if data in ("ad_bot", "ad_group", "ad_channel"):
        ttype = data.split("_", 1)[1]
        price = PRICES[ttype]
        if uid != ADMIN_ID and u["balance"] < price:
            return bot.answer_callback_query(
                c.id, f"Недостаточно средств. Нужно {price} ₽", show_alert=True
            )
        states[uid] = {"state": "ad_link", "type": ttype}
        kb = styled_kb(row_width=1)
        add_styled_button(kb, uid, "« Отмена", callback_data="ads")
        try:
            bot.edit_message_text(
                f"Отправьте ссылку на {ttype} (<code>@username</code> или https://t.me/...)\n\n"
                f"Стоимость: <b>{price} ₽</b>",
                c.message.chat.id, c.message.message_id, reply_markup=kb,
            )
        except Exception:
            bot.send_message(c.message.chat.id, "Отправьте ссылку.", reply_markup=kb)
        return bot.answer_callback_query(c.id)

    # ---------- ПРОФИЛЬ ----------
    if data == "profile":
        conn = db()
        row = conn.execute("SELECT COUNT(*) AS n FROM users WHERE ref_by=?", (uid,)).fetchone()
        conn.close()
        refs = row["n"] if row else 0
        uname = f"@{u['username']}" if u["username"] else None
        link = ref_link(uid)
        text = "Ваш профиль:\n#Профиль\n"
        text += f"×🆔 ID: <code>{uid}</code>\n"
        if uname:
            text += f"👤 Username: {esc(uname)}\n"
        text += f"💰 Баланс: <b>{bal_text(u)}</b> ₽\n"
        text += f"🔗 Реферальная ссылка:\n<code>{esc(link)}</code>\n\n"
        text += "#Статистика:\n"
        text += f"Перешло по ссылке: <b>{refs}</b>\n"
        text += f"Дата регистрации: <b>{esc(u['reg_date'])}</b>\n"
        text += f"Топ в реферальной системе: <b>#{ref_rank(uid)}</b>"

        kb = styled_kb(row_width=1)
        try:
            kb.add(types.InlineKeyboardButton(
                text="📋 Скопировать реф. ссылку",
                copy_text={"text": link},
                style=STYLE_MAP.get(get_theme(uid), "primary")
            ))
        except Exception:
            add_styled_button(kb, uid, "📋 Показать реф. ссылку", callback_data="show_ref")
        add_styled_button(kb, uid, "« Назад", callback_data="back_main")
        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb,
                                  disable_web_page_preview=True)
        except Exception:
            bot.send_message(c.message.chat.id, text, reply_markup=kb, disable_web_page_preview=True)
        return bot.answer_callback_query(c.id)

    if data == "show_ref":
        return bot.answer_callback_query(c.id, ref_link(uid), show_alert=True)

    # ---------- НАШИ БОТЫ ----------
    if data == "our_bots":
        kb = styled_kb(row_width=1)
        add_styled_button(kb, uid, "🤖 t.me/hy3rm1zbio", url="https://t.me/hy3rm1zbio")
        add_styled_button(kb, uid, "« Назад", callback_data="back_main")
        text = f"Наши боты:\n\nСсылка на: <a href='https://t.me/hy3rm1zbio'>t.me/hy3rm1zbio</a>"
        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb,
                                  disable_web_page_preview=True)
        except Exception:
            bot.send_message(c.message.chat.id, text, reply_markup=kb, disable_web_page_preview=True)
        return bot.answer_callback_query(c.id)

    # ---------- СВОЙ БОТ (старая кнопка, оставим для совместимости) ----------
    if data == "own_bot":
        states[uid] = {"state": "own_bot_token"}
        kb = styled_kb(row_width=1)
        add_styled_button(kb, uid, "« Назад", callback_data="back_main")
        text = (
            "➕ <b>Свой дочерний бот</b>\n\n"
            "Отправьте <b>токен</b> своего бота (получить можно у @BotFather).\n"
            "Формат: <code>123456:AA...</code>\n\n"
            "После проверки бот будет подключён к платформе."
        )
        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
        except Exception:
            bot.send_message(c.message.chat.id, text, reply_markup=kb)
        return bot.answer_callback_query(c.id)

    # ---------- СОЗДАТЬ БОТА (MANAGED) ----------
    if data == "create_bot":
        suggested = f"user{uid}_bot"
        bot_name = "Мой бот"
        deep_link = f"https://t.me/newbot/{BOT_USERNAME}/{suggested}?name={bot_name}"

        text = (
            "🆕 <b>Создание управляемого бота</b>\n\n"
            "Нажми кнопку ниже — Telegram откроет экран создания бота.\n"
            "Имя и username уже подставлены. Подтверди создание — и бот появится у тебя в чатах.\n\n"
            "После подтверждения токен будет получен автоматически."
        )

        kb = styled_kb(row_width=1)
        kb.add(types.InlineKeyboardButton("🚀 Создать бота", url=deep_link))
        add_styled_button(kb, uid, "« Назад", callback_data="back_main")

        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.message_id,
                                  reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            bot.send_message(c.message.chat.id, text, reply_markup=kb,
                             disable_web_page_preview=True)
        return bot.answer_callback_query(c.id)

    # ---------- АДМИН-ПАНЕЛЬ ----------
    if data.startswith("admin_"):
        if uid != ADMIN_ID:
            return bot.answer_callback_query(c.id, "Нет доступа", show_alert=True)
        return admin_router(c, data)

# ==================== МАШИННЫЕ СОСТОЯНИЯ (message) ====================
@bot.message_handler(func=lambda m: states.get(m.from_user.id, {}).get("state") in (
    "ad_link", "own_bot_token", "admin_add_name", "admin_add_link", "admin_add_chat",
    "admin_add_reward", "admin_balance_user", "admin_balance_amount", "broadcast"
), content_types=["text"])
def handle_states(m):
    uid = m.from_user.id
    st = states.get(uid, {})
    state = st.get("state")

    if state == "ad_link":
        ttype = st["type"]
        price = PRICES[ttype]
        u = get_user(uid)
        if uid != ADMIN_ID:
            if u["balance"] < price:
                states.pop(uid, None)
                return bot.send_message(m.chat.id, "❌ Недостаточно средств.")
            change_balance(uid, -price)
        conn = db()
        conn.execute("INSERT INTO ads(user_id, type, link, status, date) VALUES(?,?,?,?,?)",
                     (uid, ttype, m.text.strip(), "new", datetime.now().strftime("%d.%m.%Y %H:%M")))
        conn.commit()
        conn.close()
        states.pop(uid, None)
        bot.send_message(m.chat.id, "✅ Заявка на рекламу принята и отправлена на модерацию.",
                         reply_markup=back_main_kb(uid))
        try:
            bot.send_message(ADMIN_ID,
                             f"📣 Новая заявка на рекламу\n"
                             f"Тип: {ttype}\n"
                             f"От: <a href='tg://user?id={uid}'>{uid}</a>\n"
                             f"Ссылка: {esc(m.text.strip())}\n"
                             f"Статус: new")
        except Exception:
            pass
        bot.send_message(m.chat.id,
                         f"Заказать бота на подобие этого можно у {OWNER_USER}.\nПодробности: {OWNER_CH}")
        return

    if state == "own_bot_token":
        token = m.text.strip()
        if ":" not in token:
            return bot.send_message(m.chat.id, "❌ Неверный формат токена. Попробуйте ещё раз.")
        try:
            test_bot = telebot.TeleBot(token)
            me = test_bot.get_me()
            uname = me.username
        except Exception:
            return bot.send_message(m.chat.id, "❌ Токен невалиден. Попробуйте ещё раз.")
        conn = db()
        conn.execute("INSERT INTO child_bots(user_id, token, username, date) VALUES(?,?,?,?)",
                     (uid, token, uname, datetime.now().strftime("%d.%m.%Y %H:%M")))
        conn.commit()
        conn.close()
        states.pop(uid, None)
        bot.send_message(m.chat.id,
                         f"✅ Бот @{uname} сохранён и отправлен на проверку администратору.",
                         reply_markup=back_main_kb(uid))
        try:
            bot.send_message(ADMIN_ID,
                             f"🤖 Новый дочерний бот\nОт: <a href='tg://user?id={uid}'>{uid}</a>\n"
                             f"Юзер: @{uname}\nТокен: <code>{esc(token)}</code>")
        except Exception:
            pass
        return

    if state == "admin_add_name":
        st["name"] = m.text.strip()
        st["state"] = "admin_add_link"
        states[uid] = st
        return bot.send_message(m.chat.id, "Введите ссылку (https://t.me/...):")

    if state == "admin_add_link":
        st["link"] = m.text.strip()
        if st["type"] == "bot":
            st["chat_id"] = ""
            st["state"] = "admin_add_reward"
            states[uid] = st
            return bot.send_message(m.chat.id, "Введите награду (число ₽):")
        st["state"] = "admin_add_chat"
        states[uid] = st
        return bot.send_message(m.chat.id, "Введите chat_id канала/группы (например @mychannel или -1001234567890):")

    if state == "admin_add_chat":
        st["chat_id"] = m.text.strip()
        st["state"] = "admin_add_reward"
        states[uid] = st
        return bot.send_message(m.chat.id, "Введите награду (число ₽):")

    if state == "admin_add_reward":
        try:
            reward = int(m.text.strip())
        except Exception:
            return bot.send_message(m.chat.id, "❌ Введите число.")
        cond = {
            "channel": "Подписаться на канал и остаться в нем в течении 7 дней.",
            "group": "Подписаться на группу и остаться в ней в течении 7 дней.",
            "bot": "Нажать /start и не блокировать бота в течении 7 дней.",
        }[st["type"]]
        conn = db()
        conn.execute(
            "INSERT INTO tasks(type, name, link, chat_id, reward, condition, active) VALUES(?,?,?,?,?,?,1)",
            (st["type"], st["name"], st["link"], st["chat_id"], reward, cond),
        )
        conn.commit()
        conn.close()
        states.pop(uid, None)
        return bot.send_message(m.chat.id, "✅ Задание добавлено.", reply_markup=admin_kb())

    if state == "admin_balance_user":
        try:
            target = int(m.text.strip())
        except Exception:
            return bot.send_message(m.chat.id, "❌ Введите ID числом.")
        st["target"] = target
        st["state"] = "admin_balance_amount"
        states[uid] = st
        return bot.send_message(m.chat.id, "Введите сумму (можно отрицательную):")

    if state == "admin_balance_amount":
        try:
            amount = int(m.text.strip())
        except Exception:
            return bot.send_message(m.chat.id, "❌ Введите число.")
        change_balance(st["target"], amount)
        states.pop(uid, None)
        bot.send_message(m.chat.id, f"✅ Баланс пользователя {st['target']} изменён на {amount}.",
                         reply_markup=admin_kb())
        try:
            bot.send_message(st["target"], f"💰 Ваш баланс изменён на {amount} ₽.")
        except Exception:
            pass
        return

    if state == "broadcast":
        conn = db()
        rows = conn.execute("SELECT id FROM users").fetchall()
        conn.close()
        ok, fail = 0, 0
        for r in rows:
            try:
                bot.send_message(r["id"], m.text)
                ok += 1
            except Exception:
                fail += 1
        states.pop(uid, None)
        return bot.send_message(m.chat.id, f"📢 Рассылка завершена.\nУспешно: {ok}\nОшибок: {fail}",
                                reply_markup=admin_kb())

# ==================== АДМИН-ПАНЕЛЬ ====================
def admin_kb():
    kb = styled_kb(row_width=2)
    add_styled_button(kb, ADMIN_ID, "📊 Статистика", callback_data="admin_stats")
    add_styled_button(kb, ADMIN_ID, "📢 Рассылка", callback_data="admin_broadcast")
    add_styled_button(kb, ADMIN_ID, "➕ Добавить задание", callback_data="admin_add")
    add_styled_button(kb, ADMIN_ID, "📋 Задания", callback_data="admin_tasks")
    add_styled_button(kb, ADMIN_ID, "⚠️ Жалобы", callback_data="admin_complaints")
    add_styled_button(kb, ADMIN_ID, "📣 Реклама-заявки", callback_data="admin_ads")
    add_styled_button(kb, ADMIN_ID, "💰 Выдать баланс", callback_data="admin_balance")
    add_styled_button(kb, ADMIN_ID, "🤖 Дочерние боты", callback_data="admin_childs")
    add_styled_button(kb, ADMIN_ID, "« В меню", callback_data="back_main")
    return kb

def admin_text():
    return (
        "🛠 <b>Админ-панель «Рубль»</b>\n\n"
        f"👑 Владелец: <a href='tg://user?id={ADMIN_ID}'>{ADMIN_ID}</a>\n"
        "Управление заданиями, рекламой, балансом и пользователями.\n"
    )

@bot.message_handler(commands=["admin"])
def cmd_admin(m):
    if m.from_user.id != ADMIN_ID:
        return
    bot.send_message(m.chat.id, admin_text(), reply_markup=admin_kb())

def admin_router(c, data):
    uid = c.from_user.id
    if data == "admin_stats":
        conn = db()
        users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        tasks = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE active=1").fetchone()["n"]
        ads = conn.execute("SELECT COUNT(*) AS n FROM ads").fetchone()["n"]
        comps = conn.execute("SELECT COUNT(*) AS n FROM complaints").fetchone()["n"]
        done = conn.execute("SELECT COUNT(*) AS n FROM done").fetchone()["n"]
        conn.close()
        text = (f"📊 <b>Статистика</b>\n\n"
                f"👥 Пользователей: <b>{users}</b>\n"
                f"📋 Заданий: <b>{tasks}</b>\n"
                f"✅ Выполнений: <b>{done}</b>\n"
                f"📣 Реклам: <b>{ads}</b>\n"
                f"⚠️ Жалоб: <b>{comps}</b>")
        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=admin_kb())
        except Exception:
            bot.send_message(c.message.chat.id, text, reply_markup=admin_kb())
        return bot.answer_callback_query(c.id)

    if data == "admin_broadcast":
        states[uid] = {"state": "broadcast"}
        return bot.send_message(c.message.chat.id, "Отправьте текст рассылки:")

    if data == "admin_add":
        kb = styled_kb(row_width=3)
        add_styled_button(kb, ADMIN_ID, "Канал", callback_data="admin_addtype_channel")
        add_styled_button(kb, ADMIN_ID, "Группа", callback_data="admin_addtype_group")
        add_styled_button(kb, ADMIN_ID, "Бот", callback_data="admin_addtype_bot")
        add_styled_button(kb, ADMIN_ID, "« Назад", callback_data="admin_panel")
        bot.send_message(c.message.chat.id, "Выберите тип задания:", reply_markup=kb)
        return bot.answer_callback_query(c.id)

    if data.startswith("admin_addtype_"):
        ttype = data.split("_", 2)[2]
        states[uid] = {"state": "admin_add_name", "type": ttype}
        return bot.send_message(c.message.chat.id, "Введите название (имя) задания:")

    if data == "admin_tasks":
        conn = db()
        rows = conn.execute("SELECT * FROM tasks WHERE active=1 ORDER BY id DESC").fetchall()
        conn.close()
        if not rows:
            return bot.answer_callback_query(c.id, "Заданий нет", show_alert=True)
        kb = styled_kb(row_width=1)
        for t in rows:
            add_styled_button(kb, ADMIN_ID,
                              f"🗑 #{t['id']} [{t['type']}] {t['name']} — {t['reward']}₽",
                              callback_data=f"admin_del_{t['id']}")
        add_styled_button(kb, ADMIN_ID, "« Назад", callback_data="admin_panel")
        bot.send_message(c.message.chat.id, "📋 Активные задания (нажмите для удаления):", reply_markup=kb)
        return bot.answer_callback_query(c.id)

    if data.startswith("admin_del_"):
        tid = int(data.split("_", 2)[2])
        conn = db()
        conn.execute("UPDATE tasks SET active=0 WHERE id=?", (tid,))
        conn.commit()
        conn.close()
        return bot.answer_callback_query(c.id, f"Задание #{tid} удалено", show_alert=True)

    if data == "admin_complaints":
        conn = db()
        rows = conn.execute("SELECT * FROM complaints ORDER BY id DESC LIMIT 30").fetchall()
        conn.close()
        if not rows:
            return bot.answer_callback_query(c.id, "Жалоб нет", show_alert=True)
        text = "⚠️ <b>Последние жалобы:</b>\n\n"
        for r in rows:
            text += (f"#{r['id']} • Задание {r['task_id']} • "
                     f"От <a href='tg://user?id={r['user_id']}'>{r['user_id']}</a>\n"
                     f"Причина: {esc(r['reason'])} • {esc(r['date'])}\n\n")
        bot.send_message(c.message.chat.id, text, disable_web_page_preview=True)
        return bot.answer_callback_query(c.id)

    if data == "admin_ads":
        conn = db()
        rows = conn.execute("SELECT * FROM ads ORDER BY id DESC LIMIT 30").fetchall()
        conn.close()
        if not rows:
            return bot.answer_callback_query(c.id, "Заявок нет", show_alert=True)
        text = "📣 <b>Заявки на рекламу:</b>\n\n"
        for r in rows:
            text += (f"#{r['id']} [{r['type']}] {esc(r['status'])}\n"
                     f"От <a href='tg://user?id={r['user_id']}'>{r['user_id']}</a>\n"
                     f"{esc(r['link'])}\n{esc(r['date'])}\n\n")
        bot.send_message(c.message.chat.id, text, disable_web_page_preview=True)
        return bot.answer_callback_query(c.id)

    if data == "admin_balance":
        states[uid] = {"state": "admin_balance_user"}
        return bot.send_message(c.message.chat.id, "Введите ID пользователя:")

    if data == "admin_childs":
        conn = db()
        rows = conn.execute("SELECT * FROM child_bots ORDER BY id DESC LIMIT 30").fetchall()
        conn.close()
        if not rows:
            return bot.answer_callback_query(c.id, "Дочерних ботов нет", show_alert=True)
        text = "🤖 <b>Дочерние боты:</b>\n\n"
        for r in rows:
            text += (f"#{r['id']} @{esc(r['username'])}\n"
                     f"Владелец: <a href='tg://user?id={r['user_id']}'>{r['user_id']}</a>\n"
                     f"<code>{esc(r['token'])}</code>\n{esc(r['date'])}\n\n")
        bot.send_message(c.message.chat.id, text)
        return bot.answer_callback_query(c.id)

    if data == "admin_panel":
        try:
            bot.edit_message_text(admin_text(), c.message.chat.id, c.message.message_id, reply_markup=admin_kb())
        except Exception:
            bot.send_message(c.message.chat.id, admin_text(), reply_markup=admin_kb())
        return bot.answer_callback_query(c.id)

# ==================== MANAGED BOTS ====================
def handle_managed_bot(update):
    try:
        new_bot = update.managed_bot.bot
        bot_id = new_bot.id
        bot_username = new_bot.username
        owner_id = update.from_user.id

        token_url = f"https://api.telegram.org/bot{TOKEN}/getManagedBotToken"
        resp = requests.post(token_url, json={"user_id": bot_id}, timeout=10)
        data = resp.json()

        if not data.get("ok"):
            bot.send_message(ADMIN_ID, f"❌ Не удалось получить токен для @{bot_username}: {data}")
            return

        child_token = data["result"]

        conn = db()
        conn.execute(
            "INSERT INTO child_bots(user_id, token, username, date) VALUES(?,?,?,?)",
            (owner_id, child_token, bot_username, datetime.now().strftime("%d.%m.%Y %H:%M")),
        )
        conn.commit()
        conn.close()

        bot.send_message(
            owner_id,
            f"✅ Бот @{bot_username} успешно создан!\n"
            f"Токен сохранён. Ты можешь управлять им через панель."
        )
        bot.send_message(
            ADMIN_ID,
            f"🤖 Новый управляемый бот\n"
            f"Владелец: <a href='tg://user?id={owner_id}'>{owner_id}</a>\n"
            f"Бот: @{bot_username} (id: <code>{bot_id}</code>)\n"
            f"Токен: <code>{child_token}</code>"
        )
    except Exception as e:
        bot.send_message(ADMIN_ID, f"⚠️ Ошибка в handle_managed_bot: {e}")

# Регистрация обработчика managed_bot (pyTelegramBotAPI 4.24+)
try:
    bot.register_managed_bot_handler(handle_managed_bot)
except AttributeError:
    # Фоллбек для версий, где register_managed_bot_handler отсутствует
    try:
        bot.register_handler(content_types=["managed_bot"], callback=handle_managed_bot)
    except Exception as e:
        print(f"[WARN] Не удалось зарегистрировать managed_bot handler: {e}")

# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    init_db()
    print(f"🤖 Бот @{BOT_USERNAME} запущен.")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)