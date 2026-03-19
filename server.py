# -*- coding: utf-8 -*-
import json
import logging
import os
import asyncio
from datetime import datetime
from groq import Groq
from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
WEBAPP_URL     = os.environ.get("WEBAPP_URL", "")
DATABASE_URL   = os.environ.get("DATABASE_URL", "")
GROUP_CHAT_ID  = int(os.environ.get("GROUP_CHAT_ID", "0"))
TEAM = ["Полина", "Аня", "Я (сам)"]

logging.basicConfig(level=logging.INFO)
groq_client = Groq(api_key=GROQ_API_KEY)
flask_app = Flask(__name__)
CORS(flask_app)

# ─── Database ─────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id SERIAL PRIMARY KEY,
                    board_id BIGINT NOT NULL,
                    task TEXT NOT NULL,
                    who TEXT NOT NULL,
                    priority TEXT DEFAULT 'обычно',
                    deadline TEXT,
                    source TEXT,
                    done BOOLEAN DEFAULT FALSE,
                    comments JSONB DEFAULT '[]',
                    created TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id SERIAL PRIMARY KEY,
                    board_id BIGINT NOT NULL,
                    title TEXT NOT NULL,
                    date TEXT,
                    time TEXT,
                    created TEXT
                )
            """)
        conn.commit()
    logging.info("Database initialized")

def get_board_id(chat_id: int) -> int:
    if GROUP_CHAT_ID:
        return GROUP_CHAT_ID
    return chat_id

# ─── Task DB operations ───────────────────────────────────────────────────────

def db_get_tasks(board_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tasks WHERE board_id = %s ORDER BY id DESC", (board_id,))
            rows = cur.fetchall()
            return [dict(r) for r in rows]

def db_add_task(board_id, item):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tasks (board_id, task, who, priority, deadline, source, done, comments, created)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
            """, (
                board_id,
                item.get("task", ""),
                item.get("who", "Я (сам)"),
                item.get("priority", "обычно"),
                item.get("deadline"),
                item.get("source", ""),
                False,
                json.dumps([]),
                datetime.now().strftime("%d.%m %H:%M")
            ))
            row = dict(cur.fetchone())
        conn.commit()
    row["comments"] = json.loads(row["comments"]) if isinstance(row["comments"], str) else row.get("comments", [])
    return row

def db_update_task(board_id, task_id, data):
    allowed = ["done", "who", "priority", "deadline", "task"]
    with get_db() as conn:
        with conn.cursor() as cur:
            for key in allowed:
                if key in data:
                    cur.execute(f"UPDATE tasks SET {key} = %s WHERE id = %s AND board_id = %s", (data[key], task_id, board_id))
            cur.execute("SELECT * FROM tasks WHERE id = %s AND board_id = %s", (task_id, board_id))
            row = cur.fetchone()
        conn.commit()
    if not row:
        return None
    row = dict(row)
    row["comments"] = json.loads(row["comments"]) if isinstance(row["comments"], str) else row.get("comments", [])
    return row

def db_delete_task(board_id, task_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE id = %s AND board_id = %s", (task_id, board_id))
        conn.commit()

def db_add_comment(board_id, task_id, comment):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT comments FROM tasks WHERE id = %s AND board_id = %s", (task_id, board_id))
            row = cur.fetchone()
            if not row:
                return None
            comments = json.loads(row["comments"]) if isinstance(row["comments"], str) else (row["comments"] or [])
            comments.append(comment)
            cur.execute("UPDATE tasks SET comments = %s WHERE id = %s AND board_id = %s", (json.dumps(comments), task_id, board_id))
            cur.execute("SELECT * FROM tasks WHERE id = %s AND board_id = %s", (task_id, board_id))
            row = dict(cur.fetchone())
        conn.commit()
    row["comments"] = json.loads(row["comments"]) if isinstance(row["comments"], str) else row.get("comments", [])
    return row

# ─── Event DB operations ──────────────────────────────────────────────────────

def db_get_events(board_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM events WHERE board_id = %s ORDER BY date, time", (board_id,))
            return [dict(r) for r in cur.fetchall()]

def db_add_event(board_id, item):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO events (board_id, title, date, time, created)
                VALUES (%s, %s, %s, %s, %s) RETURNING *
            """, (board_id, item.get("title",""), item.get("date",""), item.get("time"), datetime.now().strftime("%d.%m %H:%M")))
            row = dict(cur.fetchone())
        conn.commit()
    return row

def db_delete_event(board_id, event_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM events WHERE id = %s AND board_id = %s", (event_id, board_id))
        conn.commit()

# ─── Flask API ────────────────────────────────────────────────────────────────

@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "group_id": GROUP_CHAT_ID})

@flask_app.route("/tasks/<int:chat_id>", methods=["GET"])
def get_tasks_api(chat_id):
    return jsonify(db_get_tasks(get_board_id(chat_id)))

@flask_app.route("/tasks/<int:chat_id>/<int:task_id>", methods=["PATCH"])
def update_task_api(chat_id, task_id):
    t = db_update_task(get_board_id(chat_id), task_id, request.json)
    if not t:
        return jsonify({"error": "not found"}), 404
    return jsonify(t)

@flask_app.route("/tasks/<int:chat_id>/<int:task_id>", methods=["DELETE"])
def delete_task_api(chat_id, task_id):
    db_delete_task(get_board_id(chat_id), task_id)
    return jsonify({"ok": True})

@flask_app.route("/tasks/<int:chat_id>/<int:task_id>/comments", methods=["POST"])
def add_comment_api(chat_id, task_id):
    data = request.json
    comment = {
        "text": data.get("text", ""),
        "author": data.get("author", ""),
        "created": datetime.now().strftime("%d.%m %H:%M"),
    }
    t = db_add_comment(get_board_id(chat_id), task_id, comment)
    if not t:
        return jsonify({"error": "not found"}), 404
    return jsonify(t)

@flask_app.route("/analyze/<int:chat_id>", methods=["POST"])
def analyze_api(chat_id):
    board_id = get_board_id(chat_id)
    text = request.json.get("text", "")
    try:
        parsed, _ = analyze_all(text)
        added = [db_add_task(board_id, item) for item in parsed]
        return jsonify(added)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@flask_app.route("/events/<int:chat_id>", methods=["GET"])
def get_events_api(chat_id):
    return jsonify(db_get_events(get_board_id(chat_id)))

@flask_app.route("/events/<int:chat_id>", methods=["POST"])
def add_event_api(chat_id):
    e = db_add_event(get_board_id(chat_id), request.json)
    return jsonify(e)

@flask_app.route("/events/<int:chat_id>/<int:event_id>", methods=["DELETE"])
def delete_event_api(chat_id, event_id):
    db_delete_event(get_board_id(chat_id), event_id)
    return jsonify({"ok": True})

# ─── AI ───────────────────────────────────────────────────────────────────────

def analyze_all(text):
    today = datetime.now()
    import calendar
    last_day = calendar.monthrange(today.year, today.month)[1]
    next_day = today.day + 1 if today.day < last_day else 1
    next_month = today.month if today.day < last_day else (today.month % 12) + 1
    next_year = today.year if next_month != 1 or today.month != 12 else today.year + 1
    tomorrow_str = f"{next_day:02d}.{next_month:02d}.{next_year}"
    today_str = today.strftime("%d.%m.%Y")

    system = (
        "Ты помощник руководителя. Разбери текст и раздели на задачи и встречи по строгим правилам."
        "\n\nОПРЕДЕЛЕНИЯ:"
        "\nВСТРЕЧА = звонок/созвон/встреча/переговоры/совещание С КОНКРЕТНЫМ ЧЕЛОВЕКОМ или командой. Обязательно есть слово 'звонок', 'созвон', 'встреча', 'переговоры' или 'совещание'."
        "\nЗАДАЧА = поручение что-то СДЕЛАТЬ: подготовить, отправить, проверить, написать, оплатить и т.д."
        "\n\nГЛАВНОЕ ПРАВИЛО: если сообщение про встречу/звонок — это ТОЛЬКО встреча, НЕ задача. Не создавай задачу 'организовать встречу' или 'позвонить' — это уже сама встреча."
        "\n\nДля задач:"
        "\n- Аня: суды, претензии, договоры аренды, проверка договоров, юридические вопросы"
        "\n- Полина: всё остальное"
        "\n- Я (сам): только если явно 'я сделаю', 'мне нужно', 'напомни мне'"
        "\n- Максимум 1-2 задачи, объединяй похожие"
        "\n\nДля встреч:"
        "\n- date: в формате ДД.ММ.ГГГГ (сегодня=" + today_str + ", завтра=" + tomorrow_str + ")"
        "\n- time: в формате ЧЧ:ММ, если не указано — null"
        '\n\nВерни ТОЛЬКО JSON без markdown: {"tasks": [{"task":"...","who":"...","priority":"срочно|важно|обычно","deadline":"...или null","source":"..."}], "events": [{"title":"...","date":"ДД.ММ.ГГГГ","time":"ЧЧ:ММ или null"}]}'
    )
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
            temperature=0.1,
        )
        raw = response.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        return result.get("tasks", []), result.get("events", [])
    except Exception as e:
        logging.error(f"analyze_all error: {e}")
        return [], []

# ─── Telegram Bot ─────────────────────────────────────────────────────────────

def main_keyboard(chat_id=None):
    board_id = get_board_id(chat_id) if chat_id else chat_id
    url = f"{WEBAPP_URL}?id={board_id}" if board_id else WEBAPP_URL
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📋 Открыть задачи", web_app=WebAppInfo(url=url))]],
        resize_keyboard=True
    )

def task_keyboard(t):
    toggle_label = "✅ Готово" if not t["done"] else "↩ Вернуть"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(toggle_label, callback_data=f"toggle_{t['id']}"),
        InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{t['id']}")
    ]])

PRIORITY_EMOJI = {"срочно": "🔴", "важно": "🟡", "обычно": "🟢"}

def format_task(t, show_id=True):
    done = "✅" if t["done"] else "◻️"
    pri = PRIORITY_EMOJI.get(t["priority"], "⚪️")
    dl = f" · до {t['deadline']}" if t.get("deadline") else ""
    prefix = f"#{t['id']} " if show_id else ""
    src = f"\n  «{t['source']}»" if t.get("source") else ""
    return f"{done} {prefix}{pri} {t['task']} · {t['who']}{dl}{src}"

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    if chat_type in ["group", "supergroup"]:
        await update.message.reply_text(
            "👋 Привет! Пишите задачи прямо сюда — я разберу их и добавлю в общую доску. ID группы: " + str(chat_id)
        )
    else:
        await update.message.reply_text(
            "👋 Привет! Пришли рабочие сообщения — найду задачи и назначу исполнителя.\n\nОткрой интерфейс кнопкой внизу 👇\n\n/tasks — активные задачи\n/done — выполненные\n/clear — удалить все",
            reply_markup=main_keyboard(chat_id)
        )

async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    board_id = get_board_id(chat_id)
    active = [t for t in db_get_tasks(board_id) if not t["done"]]
    if not active:
        await update.message.reply_text("📭 Активных задач нет.", reply_markup=main_keyboard(chat_id))
        return
    await update.message.reply_text(f"📋 Активных задач: {len(active)}", reply_markup=main_keyboard(chat_id))
    for t in active:
        await update.message.reply_text(format_task(t), reply_markup=task_keyboard(t))

async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    board_id = get_board_id(chat_id)
    done = [t for t in db_get_tasks(board_id) if t["done"]]
    if not done:
        await update.message.reply_text("Выполненных задач пока нет.")
        return
    lines = "\n".join(format_task(t, show_id=False) for t in done)
    await update.message.reply_text("Выполнено (" + str(len(done)) + "):\n\n" + lines)

async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    board_id = get_board_id(update.effective_chat.id)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE board_id = %s", (board_id,))
        conn.commit()
    await update.message.reply_text("🗑 Все задачи удалены.")

async def cmd_groupid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ID этого чата: " + str(update.effective_chat.id))

async def cmd_comment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    board_id = get_board_id(chat_id)
    args = ctx.args
    if not args or len(args) < 2:
        await update.message.reply_text("Использование: /comment [номер задачи] [текст]. Например: /comment 3 позвонила клиенту")
        return
    try:
        task_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Укажи номер задачи числом.")
        return
    comment_text = " ".join(args[1:])
    author = update.effective_user.first_name or "Аноним"
    comment = {"text": comment_text, "author": author, "created": datetime.now().strftime("%d.%m %H:%M")}
    t = db_add_comment(board_id, task_id, comment)
    if not t:
        await update.message.reply_text("Задача #" + str(task_id) + " не найдена.")
        return
    await update.message.reply_text("Комментарий добавлен к задаче #" + str(task_id) + ": " + t["task"])

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    board_id = get_board_id(chat_id)
    text = update.message.text.strip()
    thinking = await update.message.reply_text("🤖 Анализирую...")
    try:
        parsed_tasks, parsed_events = analyze_all(text)
    except Exception as e:
        await thinking.edit_text("❌ Ошибка: " + str(e))
        return
    if not parsed_tasks and not parsed_events:
        await thinking.edit_text("🤷 Задач и встреч не нашёл.")
        return
    results = []
    if parsed_tasks:
        results.append("✨ Задач: " + str(len(parsed_tasks)))
    if parsed_events:
        results.append("📅 Встреч: " + str(len(parsed_events)))
    await thinking.edit_text(" · ".join(results))
    for item in parsed_tasks:
        t = db_add_task(board_id, item)
        await update.message.reply_text(format_task(t), reply_markup=task_keyboard(t))
    for item in parsed_events:
        e = db_add_event(board_id, item)
        time_str = " в " + e["time"] if e.get("time") else ""
        await update.message.reply_text("📅 " + e["title"] + time_str + " · " + e["date"])

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    board_id = get_board_id(chat_id)
    action, task_id = query.data.split("_", 1)
    task_id = int(task_id)
    if action == "toggle":
        tasks = db_get_tasks(board_id)
        t = next((x for x in tasks if x["id"] == task_id), None)
        if t:
            t = db_update_task(board_id, task_id, {"done": not t["done"]})
            await query.edit_message_text(format_task(t), reply_markup=task_keyboard(t))
    elif action == "delete":
        db_delete_task(board_id, task_id)
        await query.edit_message_text("🗑 Задача #" + str(task_id) + " удалена.")

# ─── Run ──────────────────────────────────────────────────────────────────────

async def run_bot():
    tg_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("tasks", cmd_tasks))
    tg_app.add_handler(CommandHandler("done", cmd_done))
    tg_app.add_handler(CommandHandler("clear", cmd_clear))
    tg_app.add_handler(CommandHandler("groupid", cmd_groupid))
    tg_app.add_handler(CommandHandler("comment", cmd_comment))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    tg_app.add_handler(CallbackQueryHandler(handle_callback))
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling()
    print("✅ Бот запущен.")
    await asyncio.Event().wait()

async def run_flask():
    port = int(os.environ.get("PORT", 5000))
    from werkzeug.serving import make_server
    server = make_server("0.0.0.0", port, flask_app)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, server.serve_forever)

async def main():
    init_db()
    await asyncio.gather(run_bot(), run_flask())

if __name__ == "__main__":
    asyncio.run(main())
