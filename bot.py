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

# ВПИСАН ТВОЙ ID ИЗ ЛОГОВ (ЖЕСТКО)
ALLOWED_CHAT_ID = -1002576074706

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

chat_histories = {}
router = Router()
msg_queue = asyncio.PriorityQueue()

# --- ФЕЙКОВЫЙ СЕРВЕР (Для Render) ---
async def start_dummy_server():
    try:
        app = web.Application()
        async def handle(request):
            return web.Response(text="Bot is running")
        app.router.add_get('/', handle)
        app.router.add_get('/health', handle)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", 10000))
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"🌍 Dummy server started on port {port}")
    except Exception as e:
        logger.error(f"❌ Dummy server failed: {e}")

# --- MIDDLEWARE ---
class HistoryMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        if isinstance(event, Message) and event.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            # СТРОГАЯ ПРОВЕРКА ЧАТА
            if event.chat.id != ALLOWED_CHAT_ID:
                return 

            text = event.text or event.caption or ""
            if len(text) > MAX_INPUT_LENGTH:
                text = text[:MAX_INPUT_LENGTH]

            if text and not text.strip().startswith("/"):
                # Убираем @bot
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

# --- ОБРАБОТКА ТЕКСТА ОТ МОДЕЛИ ---
def parse_model_response(full_response: str, input_context: str) -> Tuple[str | None, str | None]:
    if not full_response:
        return None, None

    if full_response.startswith(input_context):
        generated_only = full_response[len(input_context):]
    else:
        generated_only = full_response

    if not generated_only.strip():
        return None, None

    # Берем последнюю строку
    lines = [line.strip() for line in generated_only.split('\n') if line.strip()]
    if not lines:
        return None, None
    
    last_line = lines[-1]

    # Пытаемся найти [Имя]: Текст
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
        return None, None
    
    url = ML_MODEL_URL if ML_MODEL_URL.endswith("generate") else f"{ML_MODEL_URL.rstrip('/')}/generate"
    timeout_settings = aiohttp.ClientTimeout(total=40, connect=10)

    try:
        async with aiohttp.ClientSession(timeout=timeout_settings) as session:
            payload = {"prompt": context_string}
            logger.info(f"📡 Sending to API...")
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    raw_text = data.get("generated_text", "")
                    logger.info(f"✅ API Response received")
                    return parse_model_response(raw_text, context_string)
                else:
                    logger.error(f"❌ API Error {response.status}")
                    return None, None
    except Exception as e:
        logger.error(f"❌ API Request Failed: {e}")
        return None, None

# --- ГЛАВНЫЙ ВОРКЕР (БЕССМЕРТНЫЙ) ---
async def queue_worker(bot: Bot):
    logger.info("✅ BACKGROUND WORKER STARTED AND READY.")
    while True:
        try:
            # Ждем задачу
            priority, _, message, trigger_type = await msg_queue.get()
            chat_id = message.chat.id
            
            logger.info(f"⚡ PROCESSING TASK: Trigger={trigger_type}, QueueSize={msg_queue.qsize()}")

            # 1. Контекст
            context_string = ""
            has_history = chat_id in chat_histories and chat_histories[chat_id]
            
            if has_history:
                context_string = "\n".join(chat_histories[chat_id]) + "\n"
            else:
                if trigger_type == "forced":
                    # Временный контекст, если истории нет
                    logger.info("creating temp context")
                    raw_text = message.text or ""
                    clean_text = re.sub(f"@{BOT_USERNAME}", "", raw_text, flags=re.IGNORECASE).strip()
                    if not clean_text: clean_text = "..." 
                    user_name = USER_MAPPING.get(message.from_user.id, message.from_user.full_name)
                    context_string = f"[{user_name}]: {clean_text}\n"
                else:
                    msg_queue.task_done()
                    continue

            # 2. Тайпинг
            if trigger_type == "forced":
                await bot.send_chat_action(chat_id, "typing")

            # 3. Запрос
            text_to_send, history_line = await make_api_request(context_string)
            
            # 4. Отправка и сохранение
            if text_to_send and history_line:
                if trigger_type == "forced":
                    await message.reply(text_to_send)
                else:
                    await message.answer(text_to_send)
                
                if chat_id not in chat_histories:
                    chat_histories[chat_id] = deque(maxlen=10)
                
                # Добавляем ответ бота в историю
                chat_histories[chat_id].append(history_line)
                
                # Лог контекста
                logger.info(f"💾 Updated Context: {list(chat_histories[chat_id])[-2:]}")

            msg_queue.task_done()
            await asyncio.sleep(1) # Пауза чтобы не спамить

        except Exception as e:
            logger.error(f"🔥 WORKER CRASHED (Restarting...): {e}", exc_info=True)
            await asyncio.sleep(5) # Если упал, ждем 5 сек и пробуем снова

# --- ГЛАВНЫЙ ХЕНДЛЕР ---
@router.message()
async def handle_messages(message: Message):
    # Строгая фильтрация
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]: return
    if message.chat.id != ALLOWED_CHAT_ID: return
    if message.text and message.text.strip().startswith("/"): return
    if (datetime.now(message.date.tzinfo) - message.date).total_seconds() > 120: return

    text = message.text or ""
    bot_id = message.bot.id
    
    is_reply = message.reply_to_message is not None
    is_reply_to_bot = is_reply and message.reply_to_message.from_user.id == bot_id
    has_mention = f"@{BOT_USERNAME}" in text.lower()

    trigger_type = None
    priority = 10 

    if is_reply and not is_reply_to_bot and not has_mention: return

    if is_reply_to_bot or has_mention:
        trigger_type = "forced"
        priority = 1
    else:
        if random.random() < CURRENT_THRESHOLD:
            trigger_type = "random"
            priority = 2

    if trigger_type:
        logger.info(f"📥 Enqueueing message: {message.from_user.full_name} ({trigger_type})")
        await msg_queue.put((priority, time.time(), message, trigger_type))

# --- КОМАНДЫ ---
@router.message(Command("threshold"))
async def set_threshold(message: Message, command: CommandObject):
    global CURRENT_THRESHOLD
    if message.from_user.id not in ADMIN_IDS: return
    try:
        new_value = float(command.args.replace(",", "."))
        if 0 <= new_value <= 1:
            CURRENT_THRESHOLD = new_value
            await message.reply(f"Threshold: {CURRENT_THRESHOLD}")
    except: pass

# --- ЗАПУСК ---
async def on_startup(bot: Bot):
    # Запускаем фоновые задачи при старте бота
    asyncio.create_task(start_dummy_server())
    asyncio.create_task(queue_worker(bot))

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup) # РЕГИСТРАЦИЯ СТАРТАП ХУКА
    
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("🚀 Polling started...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
