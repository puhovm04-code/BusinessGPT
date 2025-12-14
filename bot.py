import os
import logging
import random
import aiohttp
import re
import asyncio
import time
from datetime import datetime
from collections import deque
from typing import Callable, Dict, Any, Awaitable

from aiogram import Router, Bot, Dispatcher, BaseMiddleware
from aiogram.types import Message, TelegramObject
from aiogram.filters import Command, CommandObject
from aiogram.enums import ChatType
from aiohttp import web  # Нужно для "обмана" Render

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Убедитесь, что токен берется из env или вставьте сюда
USER_MAPPING = {
    814759080: "A. H.",
    1214336850: "Саня Блок",
    485898893: "Влад Блок",
    1313515064: "Булгак",
    1035739386: "Вован Крюк"
}

BOT_USERNAME = "businessgpt_text_bot"
MAX_INPUT_LENGTH = 800  # Лимит символов

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

CURRENT_THRESHOLD = float(os.getenv("THRESHOLD", "0.2"))
ML_MODEL_URL = os.getenv("ML_MODEL_URL")
admin_ids_str = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in admin_ids_str.split(",") if x.strip().isdigit()]

logger.info(f"Initial THRESHOLD: {CURRENT_THRESHOLD}")
logger.info(f"ML_MODEL_URL: {ML_MODEL_URL}")

chat_histories = {}
router = Router()

# Очередь сообщений с приоритетами
# Структура элемента: (priority, timestamp, message_object, trigger_type)
msg_queue = asyncio.PriorityQueue()

# --- ФЕЙКОВЫЙ СЕРВЕР ДЛЯ RENDER ---
async def start_dummy_server():
    """Запускает маленький веб-сервер, чтобы Render не убивал бота"""
    app = web.Application()
    async def handle(request):
        return web.Response(text="Bot is running OK")
    
    app.router.add_get('/', handle)
    app.router.add_get('/health', handle)
    
    runner = web.AppRunner(app)
    await runner.setup()
    # Render требует слушать порт 10000 (или тот, что в переменной PORT)
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"✅ Dummy web server started on port {port}")

# --- MIDDLEWARE ---
class HistoryMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        if isinstance(event, Message) and event.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            text = event.text or event.caption or ""

            if len(text) > MAX_INPUT_LENGTH:
                text = text[:MAX_INPUT_LENGTH]

            if text and not text.strip().startswith("/"):
                clean_text = re.sub(f"@{BOT_USERNAME}", "", text, flags=re.IGNORECASE).strip()
                clean_text = re.sub(r'\s+', ' ', clean_text)

                if clean_text:
                    chat_id = event.chat.id
                    user_id = event.from_user.id
                    user_name = USER_MAPPING.get(user_id, event.from_user.full_name)
                    
                    if chat_id not in chat_histories:
                        chat_histories[chat_id] = deque(maxlen=10)
                    
                    formatted_line = f"[{user_name}]: {clean_text}"
                    chat_histories[chat_id].append(formatted_line)

        return await handler(event, data)

router.message.middleware(HistoryMiddleware())

# --- ФУНКЦИЯ ОЧИСТКИ ---
def clean_model_output(full_response: str, input_context: str) -> str | None:
    if not full_response:
        return None

    if full_response.startswith(input_context):
        generated_only = full_response[len(input_context):]
    else:
        generated_only = full_response

    generated_only = generated_only.strip()
    if not generated_only:
        return None

    # Убираем [Bot]: в начале
    clean_text = re.sub(r"^\[.*?\]:\s*", "", generated_only)
    
    # Обрезаем, если бот начал генерировать реплики за других пользователей
    split_match = re.search(r"\n\[.*?\]:", clean_text)
    if split_match:
        clean_text = clean_text[:split_match.start()]

    return clean_text.strip() if clean_text.strip() else None

# --- ЗАПРОС К API ---
async def make_api_request(context_string: str) -> str | None:
    if not ML_MODEL_URL:
        logger.error("ML_MODEL_URL is not set!")
        return None

    url = ML_MODEL_URL
    if not url.endswith("generate"):
        url = f"{url.rstrip('/')}/generate"

    timeout_settings = aiohttp.ClientTimeout(total=30, connect=5)

    try:
        async with aiohttp.ClientSession(timeout=timeout_settings) as session:
            payload = {"prompt": context_string}
            
            logger.info(f"Generating...")
            start_time = time.time()
            
            async with session.post(url, json=payload) as response:
                duration = time.time() - start_time
                
                if response.status == 200:
                    data = await response.json()
                    raw_text = data.get("generated_text", "")
                    
                    # Логируем начало ответа для отладки
                    preview = raw_text[len(context_string):].strip().replace('\n', ' ')[:50]
                    logger.info(f"Done in {duration:.2f}s. Raw start: '{preview}...'")
                    
                    return clean_model_output(raw_text, context_string)
                else:
                    logger.error(f"API Error {response.status}")
                    return None
                    
    except asyncio.TimeoutError:
        logger.error("API Timeout (>30s)")
        return None
    except Exception as e:
        logger.error(f"API Exception: {e}")
        return None

# --- ВОРКЕР ОЧЕРЕДИ ---
async def queue_worker():
    """Фоновая задача для обработки очереди сообщений по одному"""
    logger.info("👷 Queue worker started")
    while True:
        try:
            # Получаем задачу из очереди: (приоритет, время, сообщение, тип_триггера)
            priority, _, message, trigger_type = await msg_queue.get()
            
            chat_id = message.chat.id
            
            # Если истории нет, пропускаем
            if chat_id not in chat_histories or not chat_histories[chat_id]:
                msg_queue.task_done()
                continue

            # Собираем контекст (история могла обновиться, пока сообщение лежало в очереди)
            context_string = "\n".join(chat_histories[chat_id]) + "\n"

            # Показываем "печатает..." только для прямых обращений
            if trigger_type == "forced":
                await message.bot.send_chat_action(chat_id, "typing")

            # Делаем запрос к модели
            result = await make_api_request(context_string)

            if result:
                try:
                    # Отправляем ответ
                    if trigger_type == "forced":
                        await message.reply(result)
                    else:
                        await message.answer(result)
                    
                    # ВАЖНО: Добавляем ответ бота в историю, чтобы он учитывался в следующих генерациях
                    chat_histories[chat_id].append(f"[BOT]: {result}")
                    
                except Exception as e:
                    logger.error(f"Failed to send message: {e}")
            
            # Отмечаем задачу как выполненную
            msg_queue.task_done()
            
            # Небольшая пауза между сообщениями, чтобы не спамить слишком быстро
            await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Error in queue worker: {e}")
            # На случай критической ошибки не роняем воркер полностью
            await asyncio.sleep(1)

# --- КОМАНДЫ ---
@router.message(Command("threshold"))
async def set_threshold(message: Message, command: CommandObject):
    global CURRENT_THRESHOLD
    if message.from_user.id not in ADMIN_IDS:
        return
    
    if not command.args:
        await message.reply(f"Threshold: {CURRENT_THRESHOLD}")
        return

    try:
        new_value = float(command.args.replace(",", "."))
        if 0 <= new_value <= 1:
            CURRENT_THRESHOLD = new_value
            await message.reply(f"✅ Threshold: {CURRENT_THRESHOLD}")
        else:
            await message.reply("❌ 0.0 - 1.0")
    except ValueError:
        pass

# --- ГЛАВНЫЙ ХЕНДЛЕР ---
@router.message()
async def handle_messages(message: Message):
    # Игнорируем личные сообщения и команды
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return
    if message.text and message.text.strip().startswith("/"):
        return

    # Защита от обработки старых сообщений после рестарта (старше 2 минут)
    if (datetime.now(message.date.tzinfo) - message.date).total_seconds() > 120:
        return

    text = message.text or ""
    bot_id = message.bot.id
    
    # Логика определения триггера
    is_reply = message.reply_to_message is not None
    is_reply_to_bot = is_reply and message.reply_to_message.from_user.id == bot_id
    has_mention = f"@{BOT_USERNAME}" in text.lower()

    trigger_type = None
    priority = 10  # По умолчанию низкий приоритет

    # 1. Если это реплай, но НЕ боту, и бота НЕ упомянули -> ИГНОРИРУЕМ
    if is_reply and not is_reply_to_bot and not has_mention:
        return

    # 2. Определение приоритета и типа
    if is_reply_to_bot or has_mention:
        trigger_type = "forced"
        priority = 1  # Высокий приоритет для ответов и упоминаний
    else:
        # Рандомное срабатывание (только для обычных сообщений)
        if random.random() < CURRENT_THRESHOLD:
            trigger_type = "random"
            priority = 2  # Приоритет ниже, чем у прямых обращений

    if not trigger_type:
        return

    # Добавляем в очередь
    # Используем time.time() как второй элемент кортежа для сохранения порядка (FIFO) при одинаковом приоритете
    logger.info(f"Queueing message from {message.from_user.full_name} (Priority: {priority})")
    await msg_queue.put((priority, time.time(), message, trigger_type))

# --- ЗАПУСК ---
async def main():
    bot = Bot(token=os.getenv("BOT_TOKEN")) # Токен должен быть в ENV
    dp = Dispatcher()
    dp.include_router(router)
    
    # 1. Запускаем фейковый сервер для Render
    await start_dummy_server()
    
    # 2. Запускаем воркер очереди
    asyncio.create_task(queue_worker())
    
    # 3. Удаляем вебхук (на случай конфликтов) и запускаем поллинг
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("🤖 Bot started polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
