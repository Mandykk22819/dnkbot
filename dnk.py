import asyncio
import logging
import os
import random
import re
from collections import defaultdict
 
import markovify
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
 
# ═══════════════════════════════════════════════════
# 1. НАСТРОЙКИ
# ═══════════════════════════════════════════════════
BOT_TOKEN = os.getenv('BOT_TOKEN')
BOT_USERNAME = "@HuesosPizduk_bot"
BOT_USERNAME_PLAIN = BOT_USERNAME.replace("@", "").lower()
 
SPONTANEOUS_CHANCE = 0.01          # шанс спонтанной генерации на каждое сообщение
REBUILD_EVERY_N_MESSAGES = 5       # раз в сколько новых сообщений пересобирать модель
HISTORY_LIMIT = 10                 # сколько последних фраз помнить, чтобы не повторяться
FALLBACK_NO_MODEL = "Я аутист и мне не хватает слов 😅"
FALLBACK_NO_PHRASE = "Я не могу придумать смехуятину 🤔"
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
 
# ═══════════════════════════════════════════════════
# 2. СОСТОЯНИЕ ПО ЧАТАМ (в памяти)
# ═══════════════════════════════════════════════════
last_phrases: dict[int, list[str]] = defaultdict(list)
 
# Кэш модели: chat_id -> {"model": Markov | None, "count": сообщений на момент сборки}
_model_cache: dict[int, dict] = {}
 
# Блокировка на чат, чтобы параллельные апдейты не портили файл/кэш
_chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
 
 
def _get_lock(chat_id: int) -> asyncio.Lock:
    return _chat_locks[chat_id]
 
 
# ═══════════════════════════════════════════════════
# 3. РАБОТА С ФАЙЛАМИ (синхронные функции, вызываются через to_thread)
# ═══════════════════════════════════════════════════
 
def get_corpus_file(chat_id: int) -> str:
    return f"messages_{chat_id}.txt"
 
 
def _save_message_sync(chat_id: int, text: str) -> None:
    filename = get_corpus_file(chat_id)
    with open(filename, "a", encoding="utf-8") as f:
        f.write(text + "\n")
 
 
def _load_corpus_sync(chat_id: int) -> str:
    filename = get_corpus_file(chat_id)
    if not os.path.exists(filename):
        return ""
    with open(filename, "r", encoding="utf-8") as f:
        return f.read()
 
 
def _clear_corpus_sync(chat_id: int) -> None:
    filename = get_corpus_file(chat_id)
    if os.path.exists(filename):
        os.remove(filename)
 
 
async def save_message(chat_id: int, text: str) -> None:
    if not text:
        return
    await asyncio.to_thread(_save_message_sync, chat_id, text)
 
 
async def load_corpus(chat_id: int) -> str:
    return await asyncio.to_thread(_load_corpus_sync, chat_id)
 
 
async def clear_corpus(chat_id: int) -> None:
    await asyncio.to_thread(_clear_corpus_sync, chat_id)
 
 
# ═══════════════════════════════════════════════════
# 4. МОДЕЛЬ МАРКОВА (кэшируется, не пересобирается на каждое сообщение)
# ═══════════════════════════════════════════════════
 
def _build_model(corpus: str):
    """Пытается собрать модель с state_size=2 (более осмысленные фразы),
    откатываясь на state_size=1, если текста для этого не хватает."""
    if len(corpus) < 10:
        return None
    for state_size in (2, 1):
        try:
            model = markovify.NewlineText(corpus, state_size=state_size)
            if model.chain.model:
                return model
        except (KeyError, IndexError):
            continue
    return None
 
 
async def get_markov_model(chat_id: int):
    """Возвращает модель из кэша, пересобирая её раз в REBUILD_EVERY_N_MESSAGES
    новых сообщений, а не на каждый вызов."""
    corpus = await load_corpus(chat_id)
    msg_count = corpus.count("\n")
 
    cached = _model_cache.get(chat_id)
    if cached and msg_count - cached["count"] < REBUILD_EVERY_N_MESSAGES:
        return cached["model"]
 
    model = await asyncio.to_thread(_build_model, corpus)
    _model_cache[chat_id] = {"model": model, "count": msg_count}
    return model
 
 
def invalidate_model_cache(chat_id: int) -> None:
    _model_cache.pop(chat_id, None)
 
 
# ═══════════════════════════════════════════════════
# 5. ГЕНЕРАЦИЯ ФРАЗ
# ═══════════════════════════════════════════════════
 
async def generate_phrase(chat_id: int) -> str:
    model = await get_markov_model(chat_id)
    if model is None:
        return FALLBACK_NO_MODEL
 
    history = last_phrases[chat_id]
 
    for _ in range(5):
        phrase = await asyncio.to_thread(model.make_sentence, max_words=50, tries=100)
        if phrase and len(phrase.split()) > 1 and phrase not in history:
            history.append(phrase)
            if len(history) > HISTORY_LIMIT:
                history.pop(0)
            return phrase
    return FALLBACK_NO_PHRASE
 
 
def is_real_phrase(phrase: str) -> bool:
    """True только если это настоящая сгенерированная фраза, а не fallback-заглушка."""
    return phrase not in (FALLBACK_NO_MODEL, FALLBACK_NO_PHRASE)
 
 
async def send_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text=None) -> None:
    chat_id = update.message.chat_id
    if text is None:
        text = await generate_phrase(chat_id)
    try:
        await update.message.reply_text(text)
    except TelegramError as e:
        logger.warning("Не удалось отправить сообщение в чат %s: %s", chat_id, e)
 
 
# ═══════════════════════════════════════════════════
# 6. ОБРАБОТЧИКИ КОМАНД
# ═══════════════════════════════════════════════════
 
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет! Я бот-генератор на цепях Маркова.\n\n"
        "📝 Упомяни меня или ответь на моё сообщение, чтобы я сгенерировал фразу.\n"
        "🎲 Иногда я пишу сам, если мне есть что сказать.\n\n"
        "ℹ️ Я запоминаю сообщения из этого чата, чтобы учиться на них говорить. "
        "Удалить всё можно командой /clear.\n\n"
        "Команды:\n"
        "/gen — сгенерировать вручную\n"
        "/clear — очистить память этого чата\n"
        "/stats — статистика по этому чату"
    )
 
 
async def gen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_reply(update, context)
 
 
async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    async with _get_lock(chat_id):
        await clear_corpus(chat_id)
        last_phrases[chat_id].clear()
        invalidate_model_cache(chat_id)
    await update.message.reply_text("🗑️ Память этого чата очищена!")
 
 
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    corpus = await load_corpus(chat_id)
    lines = corpus.splitlines() if corpus else []
    word_count = len(corpus.split())
    await update.message.reply_text(
        f"📊 В этом чате запомнено:\n"
        f"Сообщений: {len(lines)}\n"
        f"Слов: {word_count}"
    )
 
 
# ═══════════════════════════════════════════════════
# 7. ОБРАБОТКА СООБЩЕНИЙ
# ═══════════════════════════════════════════════════
 
def _is_reply_to_bot(update: Update) -> bool:
    msg = update.message
    return bool(
        msg.reply_to_message
        and msg.reply_to_message.from_user
        and msg.reply_to_message.from_user.is_bot
        and msg.reply_to_message.from_user.username
        and msg.reply_to_message.from_user.username.lower() == BOT_USERNAME_PLAIN
    )
 
 
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
 
    text = update.message.text
    chat_id = update.message.chat_id
 
    is_mentioned = BOT_USERNAME.lower() in text.lower()
    is_reply_to_bot = _is_reply_to_bot(update)
 
    # Сохраняем сообщение в файл этого чата (от 2 слов — одиночные слова
    # почти не дают полезных переходов для цепи Маркова)
    clean = clean_text(text)
    if clean and len(clean.split()) >= 2:
        async with _get_lock(chat_id):
            await save_message(chat_id, clean)
 
    if is_mentioned or is_reply_to_bot:
        await send_reply(update, context)
        return
 
    if random.random() < SPONTANEOUS_CHANCE:
        phrase = await generate_phrase(chat_id)
        if is_real_phrase(phrase):
            await send_reply(update, context, text=phrase)
 
 
async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Реагирует на стикер только если он отправлен в ответ на сообщение бота"""
    if not update.message:
        return
    if _is_reply_to_bot(update):
        phrase = await generate_phrase(update.message.chat_id)
        if is_real_phrase(phrase):
            await send_reply(update, context, text=phrase)
 
 
# ═══════════════════════════════════════════════════
# 8. УТИЛИТЫ
# ═══════════════════════════════════════════════════
 
def clean_text(text: str) -> str:
    """Очищает текст от команд, ссылок, упоминаний и переносов строк
    (перенос строки внутри сообщения ломает NewlineText, где строка = предложение)."""
    text = re.sub(r'\/\w+', '', text)
    text = re.sub(r'@\w+', '', text)
    text = re.sub(r'https?://\S+', '', text)
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r'\s+', ' ', text).strip()
    return text
 
 
# ═══════════════════════════════════════════════════
# 9. ГЛОБАЛЬНАЯ ОБРАБОТКА ОШИБОК
# ═══════════════════════════════════════════════════
 
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Необработанная ошибка при обработке апдейта %s", update, exc_info=context.error)
 
 
# ═══════════════════════════════════════════════════
# 10. ЗАПУСК
# ═══════════════════════════════════════════════════
 
def main():
    if not BOT_TOKEN:
        raise RuntimeError("Переменная окружения BOT_TOKEN не задана")
 
    print("✅ Бот запущен. Жду сообщений...")
 
    app = ApplicationBuilder().token(BOT_TOKEN).build()
 
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gen", gen))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
    app.add_error_handler(error_handler)
 
    app.run_polling(drop_pending_updates=True)
 
 
if __name__ == "__main__":
    main()

