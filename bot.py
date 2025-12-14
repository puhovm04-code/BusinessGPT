import os
import logging
import random
import aiohttp
import re
import asyncio
import time
from datetime import datetime
from collections import deque
from typing import Callable, Dict, Any, Awaitable, Tuple

from aiogram import Router, Bot, Dispatcher, BaseMiddleware
from aiogram.types import Message, TelegramObject
from aiogram.filters import Command, CommandObject
from aiogram.enums import ChatType
from aiohttp import web

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")

# !!! ВСТАВЬТЕ СЮДА ID ВАШЕГО ЧАТА (начинается с -100...) !!!
# Если не знаете, запустите бота, напишите сообщение, и посмотрите в логи (там будет WRONG CHAT ID)
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID", "0")) 

USER_MAPPING = {
    814759080: "A. H.",
    1214336850: "Саня Блок",
    485898893: "Влад Блок",
    1313515064: "Булгак",
    1035739386: "Вован Крюк"
}

DEFAULT_BOT_PERSONA = "BusinessGPT"
BOT_USERNAME = "businessgpt_text_bot"
MAX_INPUT_LENGTH = 800

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
logger.info(f"ALLOWED_CHAT_ID: {ALLOWED_CHAT_ID}")

chat_histories = {}
router = Router()
msg_queue = asyncio.PriorityQueue()

# --- ФЕЙКОВЫЙ СЕРВЕР ---
async def start_dummy_server():
    """Сервер для Render, чтобы бот не засыпал"""
    try:
        app = web.Application()
        async def handle(request):
            return web.Response(text="Bot is running OK")
        app.router.add_get('/', handle)
        app.router.add_get('/health', handle)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", 10000))
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"✅ Dummy web server started on port {port}")
    except Exception as e:
        logger.error(f"❌ Failed to start dummy server: {e}")

# --- MIDDLEWARE ---
class HistoryMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        if isinstance(event, Message) and event.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            # Проверка на разрешенный чат
            if ALLOWED_CHAT_ID != 0 and event.chat.id != ALLOWED_CHAT_ID:
                # Логируем только один раз, чтобы не спамить, или если это явно не тот чат
                logger.warning(f"⚠️ Message from WRONG CHAT [ID: {event.chat.id}]. Ignoring.")
                return # Прерываем обработку полностью

            user = event.from_user
            # Лог для отладки
            logger.info(f"📩 MSG from {user.full_name} (ID:{user.id}) in Chat:{event.chat.id}")

            text = event.text or event.caption or ""
            if len(text) > MAX_INPUT_LENGTH:
                text = text[:MAX_INPUT_LENGTH]

            if text and not text.strip().startswith("/"):
                # Удаляем упоминание
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

# --- ФУНКЦИЯ ОБРАБОТКИ ОТВЕТА ---
def parse_model_response(full_response: str, input_context: str) -> Tuple[str | None, str | None]:
    if not full_response:
        return None, None

    if full_response.startswith(input_context):
        generated_only = full_response[len(input_context):]
    else:
        generated_only = full_response

    if not generated_only.strip():
        return None, None

    # Берем последнюю непустую строку
    lines = [line.strip() for line in generated_only.split('\n') if line.strip()]
    if not lines:
        return None, None
    
    last_line = lines[-1]

    # Ищем паттерн [Имя]: Текст
    match = re.match(r"^\[(.*?)\]:\s*(.*)", last_line)
    if match:
        full_history_line = last_line
        text_to_send = match.group(2).strip()
    else:
        text_to_send = last_line
        full_history_line = f"[{DEFAULT_BOT_PERSONA}]: {last_line}"

    return text_to_send, full_history_line

# --- ЗАПРОС К API ---
async def make_api_request(context_string: str) -> Tuple[str | None, str | None]:
    if not ML_MODEL_URL:
        logger.error("ML_MODEL_URL is not set!")
        return None, None
    
    url = ML_MODEL_URL if ML_MODEL_URL.endswith("generate") else f"{ML_MODEL_URL.rstrip('/')}/generate"
    # Таймаут важен, чтобы воркер не завис
    timeout_settings = aiohttp.ClientTimeout(total=30, connect=10)

    try:
        async with aiohttp.ClientSession(timeout=timeout_settings) as session:
            payload = {"prompt": context_string}
            logger.info(f"📡 Sending request to Model...")
            start_time = time.time()
            async with session.post(url, json=payload) as response:
                duration = time.time() - start_time
                if response.status == 200:
                    data = await response.json()
                    raw_text = data.get("generated_text", "")
                    
                    preview = raw_text[len(context_string):].strip().replace('\n', ' ')[:50]
                    logger.info(f"✅ Model responded in {duration:.2f}s. Start: '{preview}...'")
                    
                    return parse_model_response(raw_text, context_string)
                else:
                    logger.error(f"❌ API Error {response.status}")
                    return None, None
    except Exception as e:
        logger.error(f"❌ API Request Failed: {e}")
        return None, None

# --- ВОРКЕР ОЧЕРЕДИ ---
async def queue_worker():
    logger.info("👷 Queue worker STARTED and waiting for tasks...")
    while True:
        try:
            # 1. Ждем задачу (этот вызов блокирует выполнение, пока очередь пуста)
            # logger.info("🔄 Worker waiting...") # Раскомментируйте, если хотите видеть каждое ожидание
            priority, _, message, trigger_type = await msg_queue.get()
            
            # Как только получили задачу:
            q_size = msg_queue.qsize()
            chat_id = message.chat.id
            logger.info(f"⚡ Worker PICKED UP task. Chat={chat_id}, Trigger={trigger_type}, Remaining Queue={q_size}")

            # 2. Подготовка контекста
            context_string = ""
            has_history = chat_id in chat_histories and chat_histories[chat_id]
            
            if has_history:
                context_string = "\n".join(chat_histories[chat_id]) + "\n"
            
            if not has_history:
                if trigger_type == "forced":
                    logger.info("creating temp context (empty history)")
                    raw_text = message.text or ""
                    clean_text = re.sub(f"@{BOT_USERNAME}", "", raw_text, flags=re.IGNORECASE).strip()
                    if not clean_text: clean_text = "..." 
                    user_name = USER_MAPPING.get(message.from_user.id, message.from_user.full_name)
                    context_string = f"[{user_name}]: {clean_text}\n"
                else:
                    logger.info("Skipping random trigger (no history)")
                    msg_queue.task_done()
                    continue

            if trigger_type == "forced":
                await message.bot.send_chat_action(chat_id, "typing")

            # 3. Запрос
            text_to_send, history_line = await make_api_request(context_string)
            
            if text_to_send and history_line:
                try:
                    if trigger_type == "forced":
                        await message.reply(text_to_send)
                    else:
                        await message.answer(text_to_send)
                    
                    if chat_id not in chat_histories:
                        chat_histories[chat_id] = deque(maxlen=10)
                        
                    chat_histories[chat_id].append(history_line)
                    
                    # Лог контекста
                    logger.info(f"📝 New Context State (Last 3):")
                    for line in list(chat_histories[chat_id])[-3:]:
                        logger.info(f"   {line}")
                    
                except Exception as e:
                    logger.error(f"❌ Telegram Send Error: {e}")
            else:
                logger.warning("⚠️ Model returned Nothing")
            
            # 4. Завершение задачи
            msg_queue.task_done()
            
            # Небольшая пауза
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"🔥 CRITICAL WORKER ERROR: {e}", exc_info=True)
            await asyncio.sleep(2) # Пауза перед рестартом цикла, если ошибка

# --- КОМАНДЫ ---
@router.message(Command("threshold"))
async def set_threshold(message: Message, command: CommandObject):
    global CURRENT_THRESHOLD
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        if not command.args:
            await message.reply(f"Threshold: {CURRENT_THRESHOLD}")
            return
        new_value = float(command.args.replace(",", "."))
        if 0 <= new_value <= 1:
            CURRENT_THRESHOLD = new_value
            await message.reply(f"✅ Threshold: {CURRENT_THRESHOLD}")
    except ValueError:
        pass

# --- ГЛАВНЫЙ ХЕНДЛЕР ---
@router.message()
async def handle_messages(message: Message):
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return
    
    # ПРОВЕРКА ЧАТА
    if ALLOWED_CHAT_ID != 0 and message.chat.id != ALLOWED_CHAT_ID:
        return

    if message.text and message.text.strip().startswith("/"):
        return
    if (datetime.now(message.date.tzinfo) - message.date).total_seconds() > 120:
        return

    text = message.text or ""
    bot_id = message.bot.id
    
    is_reply = message.reply_to_message is not None
    is_reply_to_bot = is_reply and message.reply_to_message.from_user.id == bot_id
    has_mention = f"@{BOT_USERNAME}" in text.lower()

    trigger_type = None
    priority = 10 

    if is_reply and not is_reply_to_bot and not has_mention:
        return

    if is_reply_to_bot or has_mention:
        trigger_type = "forced"
        priority = 1
    else:
        if random.random() < CURRENT_THRESHOLD:
            trigger_type = "random"
            priority = 2

    if not trigger_type:
        return

    # Логируем добавление в очередь
    q_size = msg_queue.qsize()
    logger.info(f"📥 Queueing message from {message.from_user.full_name} (Priority: {priority}).")
    logger.info(f"📊 Queue Status: {q_size + 1} messages waiting.")
    
    await msg_queue.put((priority, time.time(), message, trigger_type))

# --- ЗАПУСК ---
async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    
    # Сначала запускаем сервер
    await start_dummy_server()
    
    # Явно запускаем воркер и сохраняем ссылку на задачу
    worker_task = asyncio.create_task(queue_worker())
    
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("🤖 Bot started polling...")
    
    try:
        await dp.start_polling(bot)
    finally:
        worker_task.cancel() # Отмена воркера при остановке

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
