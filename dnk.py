from __future__ import annotations

import asyncio
import copy
import functools
import json
import logging
import os
import random
import re
from collections import OrderedDict, defaultdict, deque

import markovify
from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# ═══════════════════════════════════════════════════
# 1. НАСТРОЙКИ
# ═══════════════════════════════════════════════════
BOT_TOKEN = os.getenv('BOT_TOKEN')
BOT_USERNAME = "@HuesosPizduk_bot"
BOT_USERNAME_PLAIN = BOT_USERNAME.replace("@", "").lower()

DEFAULT_CHANCE = 0.01              # шанс спонтанной генерации по умолчанию (можно менять через /chance)
REACTION_CHANCE = 0.03             # шанс поставить эмодзи-реакцию вместо ответа
REBUILD_EVERY_N_MESSAGES = 5       # раз в сколько новых сообщений пересобирать модель
HISTORY_LIMIT = 10                 # сколько последних фраз помнить, чтобы не повторяться
GENERATION_ATTEMPTS = 20           # сколько раз пробовать сгенерировать неповторяющуюся фразу
SETTINGS_FILE = "chat_settings.json"

# Ограничения по памяти и диску (рассчитаны на сервер с 1 ГБ RAM)
MAX_CORPUS_LINES = 20000           # сколько последних сообщений хранить на чат
TRIM_SLACK = 2000                  # обрезаем файл не на каждое сообщение, а когда набежит запас
MAX_CACHED_MODELS = 5              # сколько моделей одновременно держать в памяти
MAX_MESSAGE_CHARS = 1000           # слишком длинные сообщения не запоминаем

# Стикерпак: короткое имя из ссылки t.me/addstickers/ИМЯ_ПАКА (пустая строка = выключено)
STICKER_PACK_NAME = "MandykPack"
STICKER_CHANCE = 0.1               # шанс, что вместо текстового ответа бот отправит стикер из пака

FALLBACK_NO_MODEL = "Мне не хватает слов"
FALLBACK_NO_PHRASE = "Я не могу придумать ответ"
FALLBACK_NO_START = "Не получилось придумать фразу с этим словом 🤷"

# Реакции, которые Telegram разрешает ставить ботам
REACTIONS = ["👍", "🔥", "😁", "🤔", "🤯", "😱", "🎉", "🤩", "👀", "🤡", "🗿", "💯", "🤣", "🤨", "😐", "🙈", "😎", "🤪", "🥴", "🌚"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════
# 2. СОСТОЯНИЕ ПО ЧАТАМ (в памяти)
# ═══════════════════════════════════════════════════
last_phrases: dict[int, list[str]] = defaultdict(list)

# Кэш моделей (LRU): chat_id -> {"model": Markov | None, "count": сообщений на момент сборки}
_model_cache: OrderedDict[int, dict] = OrderedDict()

# Счётчик строк в файле каждого чата, чтобы не читать файл ради подсчёта
_line_counts: dict[int, int] = {}

# Последнее сохранённое сообщение чата, чтобы не писать подряд одинаковые
_last_saved: dict[int, str] = {}

# Блокировка на чат, чтобы параллельные апдейты не портили файл/кэш
_chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def _get_lock(chat_id: int) -> asyncio.Lock:
    return _chat_locks[chat_id]


# ═══════════════════════════════════════════════════
# 3. НАСТРОЙКИ ЧАТОВ (шанс, mute) — хранятся в JSON
# ═══════════════════════════════════════════════════
_settings: dict[str, dict] = {}
_settings_lock = asyncio.Lock()


def load_settings() -> None:
    global _settings
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            _settings = json.load(f)
    except (OSError, ValueError) as e:
        logger.warning("Не удалось прочитать %s: %s", SETTINGS_FILE, e)
        _settings = {}


def _save_settings_sync(data: dict) -> None:
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)  # атомарная замена, файл не останется битым


async def set_setting(chat_id: int, key: str, value) -> None:
    async with _settings_lock:
        _settings.setdefault(str(chat_id), {})[key] = value
        snapshot = copy.deepcopy(_settings)
        await asyncio.to_thread(_save_settings_sync, snapshot)


def get_chance(chat_id: int) -> float:
    return _settings.get(str(chat_id), {}).get("chance", DEFAULT_CHANCE)


def is_muted(chat_id: int) -> bool:
    return _settings.get(str(chat_id), {}).get("muted", False)


# ═══════════════════════════════════════════════════
# 4. ПРОВЕРКА АДМИНОВ
# ═══════════════════════════════════════════════════

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    msg = update.effective_message
    if chat is None or msg is None:
        return False

    # В личке с ботом человек сам себе админ
    if chat.type == ChatType.PRIVATE:
        return True

    # Админ, пишущий анонимно (от имени группы)
    if msg.sender_chat and msg.sender_chat.id == chat.id:
        return True

    user = update.effective_user
    if user is None:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except TelegramError as e:
        logger.warning("Не удалось проверить права %s в чате %s: %s", user.id, chat.id, e)
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def admin_only(func):
    """Декоратор: команду могут выполнять только админы чата."""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        if not await is_admin(update, context):
            await update.message.reply_text("🚫 Эту команду могут использовать только админы чата.")
            return
        await func(update, context)
    return wrapper


# ═══════════════════════════════════════════════════
# 5. РАБОТА С ФАЙЛАМИ (синхронные функции, вызываются через to_thread)
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


def _count_lines_sync(chat_id: int) -> int:
    """Считает строки потоково, не загружая файл в память целиком."""
    filename = get_corpus_file(chat_id)
    if not os.path.exists(filename):
        return 0
    with open(filename, "r", encoding="utf-8") as f:
        return sum(1 for _ in f)


def _corpus_stats_sync(chat_id: int) -> tuple[int, int]:
    """Возвращает (число строк, число слов), читая файл потоково."""
    filename = get_corpus_file(chat_id)
    if not os.path.exists(filename):
        return 0, 0
    lines = words = 0
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            lines += 1
            words += len(line.split())
    return lines, words


def _trim_corpus_sync(chat_id: int, keep: int) -> int:
    """Оставляет в файле только последние keep строк. Возвращает их число."""
    filename = get_corpus_file(chat_id)
    with open(filename, "r", encoding="utf-8") as f:
        tail = deque(f, maxlen=keep)
    tmp = filename + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(tail)
    os.replace(tmp, filename)  # атомарная замена
    return len(tail)


async def get_line_count(chat_id: int) -> int:
    """Число сообщений в файле чата. С диска считается один раз, дальше из памяти."""
    if chat_id not in _line_counts:
        _line_counts[chat_id] = await asyncio.to_thread(_count_lines_sync, chat_id)
    return _line_counts[chat_id]


async def save_message(chat_id: int, text: str) -> None:
    if not text:
        return
    if _last_saved.get(chat_id) == text:
        return  # не пишем подряд одинаковые сообщения (спам/копипаста)

    count = await get_line_count(chat_id)
    await asyncio.to_thread(_save_message_sync, chat_id, text)
    _last_saved[chat_id] = text
    count += 1
    _line_counts[chat_id] = count

    # Файл не растёт бесконечно: когда набежал запас, оставляем последние MAX_CORPUS_LINES
    if count > MAX_CORPUS_LINES + TRIM_SLACK:
        _line_counts[chat_id] = await asyncio.to_thread(_trim_corpus_sync, chat_id, MAX_CORPUS_LINES)
        invalidate_model_cache(chat_id)  # счётчик уменьшился, модель надо пересобрать


# ═══════════════════════════════════════════════════
# 6. МОДЕЛЬ МАРКОВА (кэшируется, не пересобирается на каждое сообщение)
# ═══════════════════════════════════════════════════

def _build_model(corpus: str):
    """state_size=1: при маленьком корпусе только так модель вообще
    способна что-то генерировать (state_size=2 требует гораздо больше текста
    и на маленьком корпусе почти всегда вернёт None)."""
    if len(corpus) < 10:
        return None
    try:
        model = markovify.NewlineText(corpus, state_size=1)
        if model.chain.model:
            return model
    except (KeyError, IndexError):
        pass
    return None


def _build_model_from_file(chat_id: int):
    """Читает корпус и собирает модель в одном потоке (корпус не задерживается в памяти)."""
    return _build_model(_load_corpus_sync(chat_id))


def _cache_put(chat_id: int, entry: dict) -> None:
    _model_cache[chat_id] = entry
    _model_cache.move_to_end(chat_id)
    while len(_model_cache) > MAX_CACHED_MODELS:
        _model_cache.popitem(last=False)  # выгружаем давно не использованную модель


async def get_markov_model(chat_id: int):
    """Возвращает модель из кэша, пересобирая её раз в REBUILD_EVERY_N_MESSAGES
    новых сообщений, а не на каждый вызов."""
    msg_count = await get_line_count(chat_id)

    cached = _model_cache.get(chat_id)
    if cached and msg_count - cached["count"] < REBUILD_EVERY_N_MESSAGES:
        _model_cache.move_to_end(chat_id)
        return cached["model"]

    model = await asyncio.to_thread(_build_model_from_file, chat_id)
    _cache_put(chat_id, {"model": model, "count": msg_count})
    return model


def invalidate_model_cache(chat_id: int) -> None:
    _model_cache.pop(chat_id, None)


# ═══════════════════════════════════════════════════
# 7. ГЕНЕРАЦИЯ ФРАЗ
# ═══════════════════════════════════════════════════

def _make_sentence(model, start: str | None = None):
    """Генерирует одно предложение. Если задано слово start — пытается
    построить фразу вокруг него (пробуя разные регистры)."""
    if start:
        for variant in dict.fromkeys((start, start.lower(), start.capitalize())):
            try:
                phrase = model.make_sentence_with_start(
                    variant, strict=False, max_words=50, tries=100
                )
            except Exception:  # слова нет в корпусе (ParamError/KeyError)
                continue
            if phrase:
                return phrase
        return None
    return model.make_sentence(max_words=50, tries=100)


def _pick_phrase(model, start: str | None, recent: list[str]):
    """Все попытки генерации одним вызовом (один переход в поток вместо 20).
    Возвращает первую фразу, которой нет среди recent, а если все повторяются —
    первую удачную. None, если не удалось ничего."""
    best_phrase = None
    for _ in range(GENERATION_ATTEMPTS):
        phrase = _make_sentence(model, start)
        if not phrase or len(phrase.split()) <= 1:
            continue
        if best_phrase is None:
            best_phrase = phrase  # запасной вариант, если всё будет повтором
        if phrase not in recent:
            return phrase
    return best_phrase


async def generate_phrase(chat_id: int, start: str | None = None) -> str:
    model = await get_markov_model(chat_id)
    if model is None:
        return FALLBACK_NO_MODEL

    history = last_phrases[chat_id]

    # При маленьком корпусе у цепи мало вариантов вообще, поэтому строго
    # требовать "не повторяться" со всей историей — значит почти всегда
    # проваливаться в FALLBACK_NO_PHRASE. Сужаем окно уникальности под размер
    # словаря модели: чем меньше уникальных слов, тем меньше окно.
    vocab_size = len(model.chain.model)
    effective_window = max(1, min(HISTORY_LIMIT, vocab_size // 3))
    recent = history[-effective_window:]

    phrase = await asyncio.to_thread(_pick_phrase, model, start, recent)
    if phrase:
        history.append(phrase)
        if len(history) > HISTORY_LIMIT:
            history.pop(0)
        return phrase

    return FALLBACK_NO_START if start else FALLBACK_NO_PHRASE


def is_real_phrase(phrase: str) -> bool:
    """True только если это настоящая сгенерированная фраза, а не fallback-заглушка."""
    return phrase not in (FALLBACK_NO_MODEL, FALLBACK_NO_PHRASE, FALLBACK_NO_START)


async def send_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text=None) -> None:
    chat_id = update.message.chat_id
    if text is None:
        text = await generate_phrase(chat_id)
    try:
        await update.message.reply_text(text)
    except TelegramError as e:
        logger.warning("Не удалось отправить сообщение в чат %s: %s", chat_id, e)


_sticker_cache: list[str] = []


async def _load_stickers(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    """Загружает file_id стикеров из пака один раз и кэширует."""
    global _sticker_cache
    if _sticker_cache:
        return _sticker_cache
    if not STICKER_PACK_NAME:
        return []
    try:
        sticker_set = await context.bot.get_sticker_set(STICKER_PACK_NAME)
    except TelegramError as e:
        logger.warning("Не удалось загрузить стикерпак %s: %s", STICKER_PACK_NAME, e)
        return []
    _sticker_cache = [s.file_id for s in sticker_set.stickers]
    return _sticker_cache


async def maybe_send_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """С шансом STICKER_CHANCE отвечает случайным стикером из пака.
    Возвращает True, если стикер отправлен (тогда текст слать не нужно)."""
    if random.random() >= STICKER_CHANCE:
        return False
    stickers = await _load_stickers(context)
    if not stickers:
        return False
    try:
        await update.message.reply_sticker(random.choice(stickers))
        return True
    except TelegramError as e:
        logger.warning("Не удалось отправить стикер в чат %s: %s", update.message.chat_id, e)
        return False


async def reply_if_real_phrase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отвечает фразой, только если удалось сгенерировать настоящую (без заглушек)."""
    if await maybe_send_sticker(update, context):
        return
    phrase = await generate_phrase(update.message.chat_id)
    if is_real_phrase(phrase):
        await send_reply(update, context, text=phrase)


async def react_to_message(update: Update) -> None:
    """Ставит случайную эмодзи-реакцию на сообщение (нужен python-telegram-bot 21+)."""
    try:
        await update.message.set_reaction(random.choice(REACTIONS))
    except (TelegramError, AttributeError) as e:
        logger.warning("Не удалось поставить реакцию в чате %s: %s", update.message.chat_id, e)


# ═══════════════════════════════════════════════════
# 8. ОБРАБОТЧИКИ КОМАНД
# ═══════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет! Я бот-генератор Dnk на цепях Маркова.\n\n"
        "📝 Упомяни меня или ответь на моё сообщение, чтобы я сгенерировал фразу.\n"
        "🎲 Иногда я пишу сам или ставлю реакции, если мне есть что сказать.\n\n"
        "ℹ️ Я запоминаю сообщения из этого чата, чтобы учиться на них говорить.\n\n"
        "Стандартные команды:\n"
        "/gen — сгенерировать фразу\n"
        "/gen СЛОВО — фраза со словом\n"
        "/stats — статистика по этому чату\n\n"
        "Админ команды:\n"
        "/chance 0-100 — шанс спонтанных сообщений в %\n"
        "/mute — заставить меня молчать (но учиться я продолжу)\n"
        "/unmute — снова разрешить говорить"
    )


async def gen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    start_word = context.args[0] if context.args else None
    phrase = await generate_phrase(update.message.chat_id, start_word)
    await send_reply(update, context, text=phrase)


@admin_only
async def chance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id

    if not context.args:
        await update.message.reply_text(
            f"🎲 Сейчас я пишу сам с шансом {get_chance(chat_id) * 100:g}% на каждое сообщение.\n"
            "Изменить: /chance 5"
        )
        return

    raw = context.args[0].replace(",", ".").rstrip("%")
    try:
        value = float(raw)
    except ValueError:
        await update.message.reply_text("Нужно число от 0 до 100, например: /chance 5")
        return

    if not 0 <= value <= 100:
        await update.message.reply_text("Шанс должен быть от 0 до 100.")
        return

    await set_setting(chat_id, "chance", value / 100)
    await update.message.reply_text(f"✅ Шанс спонтанных сообщений: {value:g}%")


@admin_only
async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_setting(update.message.chat_id, "muted", True)
    await update.message.reply_text(
        "🤐 Молчу. Спонтанные сообщения, реакции и ответы на упоминания отключены, "
        "но я продолжаю учиться. Команда /gen всё ещё работает."
    )


@admin_only
async def unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_setting(update.message.chat_id, "muted", False)
    await update.message.reply_text("🔊 Снова на связи!")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    lines, word_count = await asyncio.to_thread(_corpus_stats_sync, chat_id)
    status = "молчу 🤐" if is_muted(chat_id) else "говорю 🔊"
    await update.message.reply_text(
        f"📊 В этом чате запомнено:\n"
        f"Сообщений: {lines}\n"
        f"Слов: {word_count}\n\n"
        f"Статус: {status}\n"
        f"Шанс спонтанных сообщений: {get_chance(chat_id) * 100:g}%"
    )


# ═══════════════════════════════════════════════════
# 9. ОБРАБОТКА СООБЩЕНИЙ
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
    # почти не дают полезных переходов для цепи Маркова).
    # Учимся всегда, даже когда бот в муте.
    clean = clean_text(text)
    if clean and len(clean.split()) >= 2 and len(clean) <= MAX_MESSAGE_CHARS:
        async with _get_lock(chat_id):
            await save_message(chat_id, clean)

    if is_muted(chat_id):
        return

    if is_mentioned or is_reply_to_bot:
        if await maybe_send_sticker(update, context):
            return
        await send_reply(update, context)
        return

    if random.random() < get_chance(chat_id):
        await reply_if_real_phrase(update, context)
    elif random.random() < REACTION_CHANCE:
        await react_to_message(update)


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Стикеры, фото, голосовые, видео, гифки: отвечает, если это ответ боту,
    иначе с некоторым шансом пишет фразу или ставит реакцию."""
    if not update.message:
        return

    chat_id = update.message.chat_id
    if is_muted(chat_id):
        return

    if _is_reply_to_bot(update):
        await reply_if_real_phrase(update, context)
        return

    if random.random() < get_chance(chat_id):
        await reply_if_real_phrase(update, context)
    elif random.random() < REACTION_CHANCE:
        await react_to_message(update)


# ═══════════════════════════════════════════════════
# 10. УТИЛИТЫ
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
# 11. ГЛОБАЛЬНАЯ ОБРАБОТКА ОШИБОК
# ═══════════════════════════════════════════════════

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Необработанная ошибка при обработке апдейта %s", update, exc_info=context.error)


# ═══════════════════════════════════════════════════
# 12. ЗАПУСК
# ═══════════════════════════════════════════════════

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Переменная окружения BOT_TOKEN не задана")

    load_settings()
    print("✅ Бот запущен. Жду сообщений...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gen", gen))
    app.add_handler(CommandHandler("chance", chance))
    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("unmute", unmute))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(
        filters.Sticker.ALL | filters.PHOTO | filters.VOICE | filters.VIDEO
        | filters.VIDEO_NOTE | filters.ANIMATION,
        handle_media,
    ))
    app.add_error_handler(error_handler)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
