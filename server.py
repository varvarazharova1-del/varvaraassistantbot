# -*- coding: utf-8 -*-
import json
import logging
import os
import asyncio
from datetime import datetime
from groq import Groq
from flask import Flask, request, jsonify
from flask_cors import CORS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
WEBAPP_URL     = os.environ.get("WEBAPP_URL", "")
TEAM = ["Полина", "Аня", "Я (сам)"]

# ID общей группы — заполняется автоматически когда бот получит первое сообщение из группы
# Можно также задать вручную через переменную окружения GROUP_CHAT_ID
GROUP_CHAT_ID = int(os.environ.get("GROUP_CHAT_ID", "0"))

logging.basicConfig(level=logging.INFO)
groq_client = Groq(api_key=GROQ_API_KEY)
flask_app = Flask(__name__)
CORS(flask_app)

tasks: dict = {}
task_counter: dict = {}
events: dict = {}
event_counter: dict = {}

def get_events(chat_id):
    return events.setdefault(int(chat_id), [])

def next_event_id(chat_id):
    chat_id = int(chat_id)
    event_counter[chat_id] = event_counter.get(chat_id, 0) + 1
    return event_counter[chat_id]

def get_board_id(chat_id: int) -> int:
    """Возвращает ID общей доски — группы если есть, иначе личный чат"""
    if GROUP_CHAT_ID:
        return GROUP_CHAT_ID
    return chat_id

def get_tasks(chat_id):
    return tasks.setdefault(int(chat_id), [])

def next_id(chat_id):
    chat_id = int(chat_id)
    task_counter[chat_id] = task_counter.get(chat_id, 0) + 1
    return task_counter[chat_id]

# ─── Flask API ────────────────────────────────────────────────────────────────

@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "group_id": GROUP_CHAT_ID})

@flask_app.route("/tasks/<int:chat_id>", methods=["GET"])
def get_tasks_api(chat_id):
    board_id = get_board_id(chat_id)
    return jsonify(get_tasks(board_id))

@flask_app.route("/tasks/<int:chat_id>/<int:task_id>", methods=["PATCH"])
def update_task_api(chat_id, task_id):
    board_id = get_board_id(chat_id)
    data = request.json
    t = next((x for x in get_tasks(board_id) if x["id"] == task_id), None)
    if not t:
        return jsonify({"error": "not found"}), 404
    t.update(data)
    return jsonify(t)

@flask_app.route("/tasks/<int:chat_id>/<int:task_id>", methods=["DELETE"])
def delete_task_api(chat_id, task_id):
    board_id = get_board_id(chat_id)
    tasks[board_id] = [x for x in get_tasks(board_id) if x["id"] != task_id]
    return jsonify({"ok": True})

@flask_app.route("/analyze/<int:chat_id>", methods=["POST"])
def analyze_api(chat_id):
    board_id = get_board_id(chat_id)
    text = request.json.get("text", "")
    try:
        parsed = analyze_messages(text)
        added = []
        for item in parsed:
            t = make_task(board_id, item)
            get_tasks(board_id).insert(0, t)
            added.append(t)
        return jsonify(added)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@flask_app.route("/tasks/<int:chat_id>/<int:task_id>/comments", methods=["POST"])
def add_comment_api(chat_id, task_id):
    board_id = get_board_id(chat_id)
    data = request.json
    t = next((x for x in get_tasks(board_id) if x["id"] == task_id), None)
    if not t:
        return jsonify({"error": "not found"}), 404
    if "comments" not in t:
        t["comments"] = []
    comment = {
        "text": data.get("text", ""),
        "author": data.get("author", ""),
        "created": datetime.now().strftime("%d.%m %H:%M"),
    }
    t["comments"].append(comment)
    return jsonify(t)

@flask_app.route("/events/<int:chat_id>", methods=["GET"])
def get_events_api(chat_id):
    board_id = get_board_id(chat_id)
    return jsonify(get_events(board_id))

@flask_app.route("/events/<int:chat_id>", methods=["POST"])
def add_event_api(chat_id):
    board_id = get_board_id(chat_id)
    data = request.json
    e = {
        "id": next_event_id(board_id),
        "title": data.get("title", ""),
        "date": data.get("date", ""),
        "time": data.get("time", ""),
        "created": datetime.now().strftime("%d.%m %H:%M"),
    }
    get_events(board_id).insert(0, e)
    return jsonify(e)

@flask_app.route("/events/<int:chat_id>/<int:event_id>", methods=["DELETE"])
def delete_event_api(chat_id, event_id):
    board_id = get_board_id(chat_id)
    events[board_id] = [x for x in get_events(board_id) if x["id"] != event_id]
    return jsonify({"ok": True})

# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_task(chat_id, item):
    return {
        "id": next_id(chat_id),
        "task": item.get("task", ""),
        "who": item.get("who", "Я (сам)"),
        "priority": item.get("priority", "обычно"),
        "deadline": item.get("deadline"),
        "source": item.get("source", ""),
        "done": False,
        "created": datetime.now().strftime("%d.%m %H:%M"),
        "comments": [],
    }

def analyze_messages(text):
    system = f"""Ты помощник по управлению задачами. Команда: {", ".join(TEAM)}.

Правила распределения задач:
- Аня: суды, претензии, договоры аренды, проверка договоров, юридические вопросы
- Полина: всё остальное
- Я (сам): только если в тексте явно написано "я", "мне", "сам сделаю", "напомни мне"

Правила создания задач:
- Если несколько сообщений об одной теме — объедини в ОДНУ задачу
- Создавай максимум 1-2 задачи из любого сообщения
- Выбирай самую суть, не дроби на мелкие подзадачи
- Определяй исполнителя по теме задачи согласно правилам выше

Верни ТОЛЬКО JSON массив без markdown:
[{{"task":"...","who":"...","priority":"срочно|важно|обычно","deadline":"...или null","source":"..."}}]"""
    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
        temperature=0.3,
    )
    raw = response.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

def analyze_events(text):
    today = datetime.now()
    import calendar
    last_day = calendar.monthrange(today.year, today.month)[1]
    next_day = today.day + 1 if today.day < last_day else 1
    next_month = today.month if today.day < last_day else (today.month % 12) + 1
    next_year = today.year if next_month != 1 or today.month != 12 else today.year + 1
    tomorrow_str = f"{next_day:02d}.{next_month:02d}.{next_year}"
    today_str = today.strftime("%d.%m.%Y")

    system = (
        "Из текста извлеки встречи, звонки, созвоны, встречи с людьми. "
        "Для каждой встречи определи: "
        "title (название, например 'Звонок с Артёмом'), "
        "date (дата в формате ДД.ММ.ГГГГ, сегодня=" + today_str + ", завтра=" + tomorrow_str + "), "
        "time (время в формате ЧЧ:ММ, если не указано — null). "
        "Верни ТОЛЬКО JSON массив без markdown. Если встреч нет — верни []. "
        'Пример: [{"title":"Звонок с Артёмом","date":"20.03.2026","time":"12:00"}]'
    )
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
            temperature=0.2,
        )
        raw = response.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
        return json.loads(raw)
    except:
        return []

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
            f"👋 Привет! Я буду разбирать сообщения в этой группе.\n\n"
            f"ID группы: `{chat_id}` — сохрани его!\n\n"
            f"Пиши задачи прямо сюда, я их разберу.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "👋 Привет! Пришли рабочие сообщения — найду задачи и назначу исполнителя.\n\n"
            "Или открой интерфейс кнопкой внизу 👇\n\n"
            "/tasks — активные задачи\n/done — выполненные\n/clear — удалить все",
            reply_markup=main_keyboard(chat_id)
        )

async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    board_id = get_board_id(chat_id)
    active = [t for t in get_tasks(board_id) if not t["done"]]
    if not active:
        await update.message.reply_text("📭 Активных задач нет.", reply_markup=main_keyboard(chat_id))
        return
    await update.message.reply_text(f"📋 Активных задач: {len(active)}", reply_markup=main_keyboard(chat_id))
    for t in active:
        await update.message.reply_text(format_task(t), reply_markup=task_keyboard(t))

async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    board_id = get_board_id(chat_id)
    done = [t for t in get_tasks(board_id) if t["done"]]
    if not done:
        await update.message.reply_text("Выполненных задач пока нет.")
        return
    lines = "\n".join(format_task(t, show_id=False) for t in done)
    await update.message.reply_text(f"✅ Выполнено ({len(done)}):\n\n{lines}")

async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    board_id = get_board_id(chat_id)
    tasks[board_id] = []
    await update.message.reply_text("🗑 Все задачи удалены.")

async def cmd_groupid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(f"ID этого чата: `{chat_id}`", parse_mode="Markdown")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    board_id = get_board_id(chat_id)
    text = update.message.text.strip()

    thinking = await update.message.reply_text("🤖 Анализирую...")
    try:
        parsed_tasks = analyze_messages(text)
        parsed_events = analyze_events(text)
    except Exception as e:
        await thinking.edit_text(f"❌ Ошибка: {e}")
        return
    if not parsed_tasks and not parsed_events:
        await thinking.edit_text("🤷 Задач и встреч не нашёл.")
        return
    results = []
    if parsed_tasks: results.append(f"✨ Задач: {len(parsed_tasks)}")
    if parsed_events: results.append(f"📅 Встреч: {len(parsed_events)}")
    await thinking.edit_text(" · ".join(results))
    for item in parsed_tasks:
        t = make_task(board_id, item)
        get_tasks(board_id).append(t)
        await update.message.reply_text(format_task(t), reply_markup=task_keyboard(t))
    for item in parsed_events:
        e = {
            "id": next_event_id(board_id),
            "title": item.get("title", ""),
            "date": item.get("date", ""),
            "time": item.get("time", ""),
            "created": datetime.now().strftime("%d.%m %H:%M"),
        }
        get_events(board_id).append(e)
        time_str = f" в {e['time']}" if e.get("time") else ""
        await update.message.reply_text(f"📅 Встреча добавлена в календарь:

{e['title']}{time_str} · {e['date']}")

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    board_id = get_board_id(chat_id)
    action, task_id = query.data.split("_", 1)
    task_id = int(task_id)
    t = next((x for x in get_tasks(board_id) if x["id"] == task_id), None)
    if not t:
        await query.edit_message_text("Задача не найдена.")
        return
    if action == "toggle":
        t["done"] = not t["done"]
        await query.edit_message_text(format_task(t), reply_markup=task_keyboard(t))
    elif action == "delete":
        tasks[board_id] = [x for x in get_tasks(board_id) if x["id"] != task_id]
        await query.edit_message_text(f"🗑 Задача #{task_id} удалена.")

async def cmd_comment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /comment 5 текст комментария"""
    chat_id = update.effective_chat.id
    board_id = get_board_id(chat_id)
    args = ctx.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Использование: /comment [номер задачи] [текст]

Например: /comment 3 позвонила клиенту, ждём ответа"
        )
        return
    try:
        task_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Укажи номер задачи числом. Например: /comment 3 текст")
        return
    comment_text = " ".join(args[1:])
    t = next((x for x in get_tasks(board_id) if x["id"] == task_id), None)
    if not t:
        await update.message.reply_text(f"Задача #{task_id} не найдена.")
        return
    if "comments" not in t:
        t["comments"] = []
    author = update.effective_user.first_name or "Аноним"
    comment = {
        "text": comment_text,
        "author": author,
        "created": datetime.now().strftime("%d.%m %H:%M"),
    }
    t["comments"].append(comment)
    await update.message.reply_text(
        f"💬 Комментарий добавлен к задаче #{task_id}:

"
        f"◻️ {t['task']} · {t['who']}

"
        f"«{comment_text}» — {author}"
    )

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
    await asyncio.gather(run_bot(), run_flask())

if __name__ == "__main__":
    asyncio.run(main())
