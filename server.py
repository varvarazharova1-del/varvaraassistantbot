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

logging.basicConfig(level=logging.INFO)
groq_client = Groq(api_key=GROQ_API_KEY)
flask_app = Flask(__name__)
CORS(flask_app)

tasks: dict[int, list] = {}
task_counter: dict[int, int] = {}

def get_tasks(chat_id):
    return tasks.setdefault(chat_id, [])

def next_id(chat_id):
    task_counter[chat_id] = task_counter.get(chat_id, 0) + 1
    return task_counter[chat_id]

@flask_app.route("/tasks/<int:chat_id>", methods=["GET"])
def get_tasks_api(chat_id):
    return jsonify(get_tasks(chat_id))

@flask_app.route("/tasks/<int:chat_id>", methods=["POST"])
def add_task_api(chat_id):
    data = request.json
    t = {"id": next_id(chat_id), "task": data.get("task",""), "who": data.get("who","Я (сам)"), "priority": data.get("priority","обычно"), "deadline": data.get("deadline"), "source": data.get("source",""), "done": False, "created": datetime.now().strftime("%d.%m %H:%M")}
    get_tasks(chat_id).insert(0, t)
    return jsonify(t)

@flask_app.route("/tasks/<int:chat_id>/<int:task_id>", methods=["PATCH"])
def update_task_api(chat_id, task_id):
    data = request.json
    t = next((x for x in get_tasks(chat_id) if x["id"] == task_id), None)
    if not t: return jsonify({"error": "not found"}), 404
    t.update(data)
    return jsonify(t)

@flask_app.route("/tasks/<int:chat_id>/<int:task_id>", methods=["DELETE"])
def delete_task_api(chat_id, task_id):
    tasks[chat_id] = [x for x in get_tasks(chat_id) if x["id"] != task_id]
    return jsonify({"ok": True})

@flask_app.route("/analyze/<int:chat_id>", methods=["POST"])
def analyze_api(chat_id):
    text = request.json.get("text", "")
    try:
        parsed = analyze_messages(text)
        added = []
        for item in parsed:
            t = {"id": next_id(chat_id), "task": item.get("task",""), "who": item.get("who","Я (сам)"), "priority": item.get("priority","обычно"), "deadline": item.get("deadline"), "source": item.get("source",""), "done": False, "created": datetime.now().strftime("%d.%m %H:%M")}
            get_tasks(chat_id).insert(0, t)
            added.append(t)
        return jsonify(added)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})

def analyze_messages(text):
    system = f"""Ты помощник по управлению задачами. Команда: {", ".join(TEAM)}.
Извлеки задачи из текста. Верни ТОЛЬКО JSON массив без markdown:
[{{"task":"...","who":"...","priority":"срочно|важно|обычно","deadline":"...или null","source":"..."}}]"""
    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": text}],
        temperature=0.3,
    )
    raw = response.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
    return json.loads(raw)

def main_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("📋 Открыть задачи", web_app=WebAppInfo(url=WEBAPP_URL))]], resize_keyboard=True)

def task_keyboard(t):
    toggle_label = "✅ Готово" if not t["done"] else "↩ Вернуть"
    return InlineKeyboardMarkup([[InlineKeyboardButton(toggle_label, callback_data=f"toggle_{t['id']}"), InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{t['id']}")]])

PRIORITY_EMOJI = {"срочно": "🔴", "важно": "🟡", "обычно": "🟢"}

def format_task(t, show_id=True):
    done = "✅" if t["done"] else "◻️"
    pri = PRIORITY_EMOJI.get(t["priority"], "⚪️")
    dl = f" · до {t['deadline']}" if t.get("deadline") else ""
    prefix = f"#{t['id']} " if show_id else ""
    src = f"\n  «{t['source']}»" if t.get("source") else ""
    return f"{done} {prefix}{pri} {t['task']} · {t['who']}{dl}{src}"

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Привет! Пришли рабочие сообщения — найду задачи и назначу исполнителя.\n\nИли открой интерфейс кнопкой внизу 👇\n\n/tasks — активные задачи\n/done — выполненные\n/clear — удалить все", reply_markup=main_keyboard())

async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    active = [t for t in get_tasks(chat_id) if not t["done"]]
    if not active:
        await update.message.reply_text("📭 Активных задач нет.", reply_markup=main_keyboard())
        return
    await update.message.reply_text(f"📋 Активные задачи: {len(active)}", reply_markup=main_keyboard())
    for t in active:
        await update.message.reply_text(format_task(t), reply_markup=task_keyboard(t))

async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    done = [t for t in get_tasks(chat_id) if t["done"]]
    if not done:
        await update.message.reply_text("Выполненных задач пока нет.")
        return
    await update.message.reply_text("✅ Выполнено ({}):\n\n{}".format(len(done), "\n".join(format_task(t, show_id=False) for t in done)))

async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tasks[update.effective_chat.id] = []
    await update.message.reply_text("🗑 Все задачи удалены.")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    thinking = await update.message.reply_text("🤖 Анализирую...")
    try:
        parsed = analyze_messages(text)
    except Exception as e:
        await thinking.edit_text(f"❌ Ошибка: {e}")
        return
    if not parsed:
        await thinking.edit_text("🤷 Задач не нашёл.")
        return
    await thinking.edit_text(f"✨ Нашёл задач: {len(parsed)}")
    for item in parsed:
        t = {"id": next_id(chat_id), "task": item.get("task",""), "who": item.get("who","Я (сам)"), "priority": item.get("priority","обычно"), "deadline": item.get("deadline"), "source": item.get("source",""), "done": False, "created": datetime.now().strftime("%d.%m %H:%M")}
        get_tasks(chat_id).append(t)
        await update.message.reply_text(format_task(t), reply_markup=task_keyboard(t))

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    action, task_id = query.data.split("_", 1)
    task_id = int(task_id)
    t = next((x for x in get_tasks(chat_id) if x["id"] == task_id), None)
    if not t:
        await query.edit_message_text("Задача не найдена.")
        return
    if action == "toggle":
        t["done"] = not t["done"]
        await query.edit_message_text(format_task(t), reply_markup=task_keyboard(t))
    elif action == "delete":
        tasks[chat_id] = [x for x in get_tasks(chat_id) if x["id"] != task_id]
        await query.edit_message_text(f"🗑 Задача #{task_id} удалена.")

async def run_bot():
    tg_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("tasks", cmd_tasks))
    tg_app.add_handler(CommandHandler("done", cmd_done))
    tg_app.add_handler(CommandHandler("clear", cmd_clear))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    tg_app.add_handler(CallbackQueryHandler(handle_callback))
    print("✅ Бот запущен.")
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling()
    await asyncio.Event().wait()

async def main():
    bot_task = asyncio.create_task(run_bot())
    port = int(os.environ.get("PORT", 5000))
    from werkzeug.serving import make_server
    server = make_server("0.0.0.0", port, flask_app)
    server_task = asyncio.get_event_loop().run_in_executor(None, server.serve_forever)
    await asyncio.gather(bot_task, server_task)

if __name__ == "__main__":
    asyncio.run(main())
