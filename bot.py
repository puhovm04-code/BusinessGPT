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
USER_MAPPING = {
    814759080: "A. H.",
    485898893: "Старый Мельник",
    1214336850: "Саня Блок",
    460174637: "Влад Блок",
    1313515064: "Булгак",
    1035739386: "Вован Крюк"
}

# Имя бота в истории, если модель вдруг не сгенерирует ник сама (резерв)
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

chat_histories = {}
router = Router()
msg_queue = asyncio.PriorityQueue()

# --- ФЕЙКОВЫЙ СЕРВЕР ---
async def start_dummy_server():
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

# --- MIDDLEWARE ---
class HistoryMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        if isinstance(event, Message) and event.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            # Лог ID для отладки
            user = event.from_user
            logger.info(f"🆔 USER INFO: ID={user.id} | Name='{user.full_name}'")

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
                    
                    # Сохраняем сообщение пользователя в формате [Имя]: Текст
                    formatted_line = f"[{user_name}]: {clean_text}"
                    chat_histories[chat_id].append(formatted_line)

        return await handler(event, data)

router.message.middleware(HistoryMiddleware())

# --- ФУНКЦИЯ ОБРАБОТКИ ОТВЕТА МОДЕЛИ ---
def parse_model_response(full_response: str, input_context: str) -> Tuple[str | None, str | None]:
    """
    Возвращает кортеж:
    1. Текст для отправки в чат (без [Имя]:)
    2. Полная строка для сохранения в историю (с [Имя]:)
    """
    if not full_response:
        return None, None

    # 1. Убираем входной контекст
    if full_response.startswith(input_context):
        generated_only = full_response[len(input_context):]
    else:
        generated_only = full_response

    if not generated_only.strip():
        return None, None

    # 2. Разбиваем на строки и берем ПОСЛЕДНЮЮ непустую
    lines = [line.strip() for line in generated_only.split('\n') if line.strip()]
    if not lines:
        return None, None
    
    last_line = lines[-1] # Берем последнюю строку, как просили

    # 3. Пытаемся найти паттерн [Имя]: Текст
    # Regex ищет что-то в квадратных скобках в начале строки, потом двоеточие
    match = re.match(r"^\[(.*?)\]:\s*(.*)", last_line)

    if match:
        # Если модель сгенерировала "[Саня Блок]: Привет"
        persona_name = match.group(1) # Саня Блок
        content_text = match.group(2).strip() # Привет
        
        full_history_line = last_line # В историю пишем как есть: [Саня Блок]: Привет
        text_to_send = content_text   # В чат пишем: Привет
    else:
        # Если модель сгенерировала просто текст без ника (редко, но бывает)
        text_to_send = last_line
        # В историю добавляем дефолтный ник, чтобы не ломать структуру
        full_history_line = f"[{DEFAULT_BOT_PERSONA}]: {last_line}"

    return text_to_send, full_history_line

# --- ЗАПРОС К API ---
async def make_api_request(context_string: str) -> Tuple[str | None, str | None]:
    if not ML_MODEL_URL:
        logger.error("ML_MODEL_URL is not set!")
        return None, None
    url = ML_MODEL_URL if ML_MODEL_URL.endswith("generate") else f"{ML_MODEL_URL.rstrip('/')}/generate"
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
                    
                    preview = raw_text[len(context_string):].strip().replace('\n', ' ')[:50]
                    logger.info(f"Done in {duration:.2f}s. Raw start: '{preview}...'")
                    
                    # Используем новую функцию парсинга
                    return parse_model_response(raw_text, context_string)
                else:
                    logger.error(f"API Error {response.status}")
                    return None, None
    except Exception as e:
        logger.error(f"API Exception: {e}")
        return None, None

# --- ВОРКЕР ОЧЕРЕДИ ---
async def queue_worker():
    logger.info("👷 Queue worker started")
    while True:
        try:
            priority, _, message, trigger_type = await msg_queue.get()
            chat_id = message.chat.id
            
            logger.info(f"👷 Worker processing: Chat={chat_id}, Trigger={trigger_type}, Queue Size={msg_queue.qsize()}")

            # --- СБОР КОНТЕКСТА ---
            context_string = ""
            has_history = chat_id in chat_histories and chat_histories[chat_id]
            
            if has_history:
                context_string = "\n".join(chat_histories[chat_id]) + "\n"
            
            if not has_history:
                if trigger_type == "forced":
                    logger.info("History empty, but forced trigger. Creating temporary context.")
                    raw_text = message.text or ""
                    clean_text = re.sub(f"@{BOT_USERNAME}", "", raw_text, flags=re.IGNORECASE).strip()
                    if not clean_text: 
                        clean_text = "..." 
                    user_name = USER_MAPPING.get(message.from_user.id, message.from_user.full_name)
                    context_string = f"[{user_name}]: {clean_text}\n"
                else:
                    logger.info("Skipping: Random trigger but no history.")
                    msg_queue.task_done()
                    continue

            if trigger_type == "forced":
                await message.bot.send_chat_action(chat_id, "typing")

            # --- ЗАПРОС И ОТПРАВКА ---
            # Получаем (Текст для чата, Строка для истории)
            text_to_send, history_line = await make_api_request(context_string)
            
            if text_to_send and history_line:
                try:
                    if trigger_type == "forced":
                        await message.reply(text_to_send)
                    else:
                        await message.answer(text_to_send)
                    
                    if chat_id not in chat_histories:
                        chat_histories[chat_id] = deque(maxlen=10)
                        
                    # ВАЖНО: Добавляем в историю полную строку (например "[Саня Блок]: Привет")
                    chat_histories[chat_id].append(history_line)
                    
                    # === ЛОГ ДЛЯ ПРОВЕРКИ ===
                    logger.info(f"✅ Sent: '{text_to_send}'")
                    logger.info(f"💾 Saved to History: '{history_line}'")
                    logger.info(f"📝 --- CURRENT CONTEXT ---")
                    for i, line in enumerate(chat_histories[chat_id]):
                        logger.info(f"{i+1}. {line}")
                    logger.info(f"📝 -----------------------")
                    # ========================
                    
                except Exception as e:
                    logger.error(f"Failed to send message: {e}")
            else:
                logger.warning("Model returned empty or invalid result")
            
            msg_queue.task_done()
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"Error in queue worker: {e}", exc_info=True)
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
    except ValueError:
        pass

# --- ГЛАВНЫЙ ХЕНДЛЕР ---
@router.message()
async def handle_messages(message: Message):
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
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

    logger.info(f"Queueing message from {message.from_user.full_name} (Priority: {priority})")
    await msg_queue.put((priority, time.time(), message, trigger_type))

# --- ЗАПУСК ---
async def main():
    bot = Bot(token=os.getenv("BOT_TOKEN"))
    dp = Dispatcher()
    dp.include_router(router)
    await start_dummy_server()
    asyncio.create_task(queue_worker())
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("🤖 Bot started polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
