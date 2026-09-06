import markovify
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import re
import os
import random

# ═══════════════════════════════════════════════════
# 1. НАСТРОЙКИ
# ═══════════════════════════════════════════════════
BOT_TOKEN = os.getenv('BOT_TOKEN')
BOT_USERNAME = "@HuesosPizduk_bot"

# Храним последние фразы отдельно для каждого чата
last_phrases = {}  # chat_id -> список фраз

# ═══════════════════════════════════════════════════
# 2. РАБОТА С ФАЙЛАМИ (по чатам)
# ═══════════════════════════════════════════════════

def get_corpus_file(chat_id):
    """Возвращает имя файла для конкретного чата"""
    return f"messages_{chat_id}.txt"

def save_message(chat_id, text):
    """Сохраняет сообщение в файл конкретного чата"""
    if not text:
        return
    filename = get_corpus_file(chat_id)
    with open(filename, "a", encoding="utf-8") as f:
        f.write(text + "\n")

def load_corpus(chat_id):
    """Загружает текст из файла конкретного чата"""
    filename = get_corpus_file(chat_id)
    if not os.path.exists(filename):
        return ""
    with open(filename, "r", encoding="utf-8") as f:
        return f.read()

def get_markov_model(chat_id):
    """Строит модель для конкретного чата"""
    corpus = load_corpus(chat_id)
    if len(corpus) < 10:
        return None
    return markovify.NewlineText(corpus, state_size=1) #Осмысленность предложений

def generate_phrase(chat_id):
    """Генерирует фразу на основе сообщений из конкретного чата"""
    model = get_markov_model(chat_id)
    if model is None:
        return "Я аутист и мне не хватает слов 😅"

    # Получаем историю фраз для этого чата
    if chat_id not in last_phrases:
        last_phrases[chat_id] = []
    history = last_phrases[chat_id]

    for _ in range(5):
        phrase = model.make_sentence(max_words=50, tries=100)
        if phrase and len(phrase.split()) > 1:
            if phrase not in history:
                history.append(phrase)
                if len(history) > 10:
                    history.pop(0)
                return phrase
    return "Я не могу придумать смехуятину 🤔"

async def send_reply(update, context, text=None):
    """Отправляет текстовый ответ"""
    chat_id = update.message.chat_id
    if text is None:
        text = generate_phrase(chat_id)
    await update.message.reply_text(text)

# ═══════════════════════════════════════════════════
# 3. ОБРАБОТЧИКИ КОМАНД
# ═══════════════════════════════════════════════════

async def start(update, context):
    await update.message.reply_text(
        "👋 Привет! Я бот-генератор на цепях Маркова.\n\n"
        "📝 Упомяни меня или ответь на моё сообщение, чтобы я сгенерировал фразу.\n"
        "🎲 Иногда я пишу сам, если мне есть что сказать.\n\n"
        "Команды:\n"
        "/gen — сгенерировать вручную\n"
        "/clear — очистить память этого чата\n"
        "/stats — статистика по этому чату"
    )

async def gen(update, context):
    chat_id = update.message.chat_id
    phrase = generate_phrase(chat_id)
    await send_reply(update, context, text=f"{phrase}")

async def clear(update, context):
    chat_id = update.message.chat_id
    filename = get_corpus_file(chat_id)
    if os.path.exists(filename):
        os.remove(filename)
    if chat_id in last_phrases:
        last_phrases[chat_id].clear()
    await update.message.reply_text("🗑️ Память этого чата очищена!")

async def stats(update, context):
    chat_id = update.message.chat_id
    corpus = load_corpus(chat_id)
    word_count = len(corpus.split())
    msg_count = corpus.count("\n") if corpus else 0
    await update.message.reply_text(
        f"📊 В этом чате запомнено:\n"
        f"Сообщений: {msg_count}\n"
        f"Слов: {word_count}"
    )

# ═══════════════════════════════════════════════════
# 4. ОБРАБОТКА СООБЩЕНИЙ
# ═══════════════════════════════════════════════════

async def handle_message(update, context):
    if not update.message or not update.message.text:
        return
    text = update.message.text
    chat_id = update.message.chat_id

    is_mentioned = BOT_USERNAME.lower() in text.lower()
    is_reply_to_bot = (
        update.message.reply_to_message
        and update.message.reply_to_message.from_user
        and update.message.reply_to_message.from_user.is_bot
        and update.message.reply_to_message.from_user.username == BOT_USERNAME.replace("@", "")
    )

    # Сохраняем сообщение в файл этого чата (от 1 слова)
    clean = clean_text(text)
    if clean and len(clean.split()) >= 1:
        save_message(chat_id, clean)

    # Если бота упомянули или ответили на него — генерируем
    if is_mentioned or is_reply_to_bot:
        await send_reply(update, context)
        return

    # Спонтанная генерация (1% шанс)
    if random.random() < 0.01:
        phrase = generate_phrase(chat_id)
        if phrase and not phrase.startswith("Я аутист"):
            await send_reply(update, context, text=phrase)

async def handle_sticker(update, context):
    """Реагирует на стикер только если он отправлен в ответ на сообщение бота"""
    chat_id = update.message.chat_id
    is_reply_to_bot = (
        update.message.reply_to_message
        and update.message.reply_to_message.from_user
        and update.message.reply_to_message.from_user.is_bot
        and update.message.reply_to_message.from_user.username == BOT_USERNAME.replace("@", "")
    )
    if is_reply_to_bot:
        phrase = generate_phrase(chat_id)
        await update.message.reply_text(f"{phrase}")

# ═══════════════════════════════════════════════════
# 5. УТИЛИТЫ
# ═══════════════════════════════════════════════════

def clean_text(text):
    """Очищает текст от команд, ссылок, упоминаний"""
    text = re.sub(r'\/\w+', '', text)
    text = re.sub(r'@\w+', '', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

# ═══════════════════════════════════════════════════
# 6. ЗАПУСК
# ═══════════════════════════════════════════════════

def main():
    print("✅ Бот запущен. Жду сообщений...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gen", gen))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
