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
# ID чата, в котором работает бот (строго один чат)
ALLOWED_CHAT_ID = -1002576074706

# Маппинг известных ID (добавляй сюда найденных в логах людей)
USER_MAPPING = {
    814759080: "A. H.",
    485898893: "Старый Мельник",
    1214336850: "Саня Блок",
    460174637: "Влад Блок",
    1313515064: "Булгак",
    1035739386: "Вован Крюк",
    407221863: "Некит Русанов"
    # Остальных добавишь, посмотрев в логи с пометкой [ID LOG]
}

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
logger.info(f"WORKING ONLY IN CHAT ID: {ALLOWED_CHAT_ID}")

chat_histories = {}
api_lock = asyncio.Lock()
router = Router()

# --- ФЕЙКОВЫЙ СЕРВЕР ДЛЯ RENDER ---
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

# --- MIDDLEWARE (Обработка входящих сообщений и истории) ---
class HistoryMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        # 1. ПРОВЕРКА ЧАТА
        if event.chat.id != ALLOWED_CHAT_ID:
            return

        # 2. ЛОГИРОВАНИЕ ID УЧАСТНИКОВ
        user = event.from_user
        if user:
            logger.info(f"[ID LOG] User: {user.full_name} | ID: {user.id} | Username: @{user.username}")

        # Сбор истории
        if event.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            text = event.text or event.caption or ""

            if len(text) > MAX_INPUT_LENGTH:
                text = text[:MAX_INPUT_LENGTH]

            if text and not text.strip().startswith("/"):
                clean_text = re.sub(f"@{BOT_USERNAME}", "", text, flags=re.IGNORECASE).strip()
                clean_text = re.sub(r'\s+', ' ', clean_text)

                if clean_text:
                    chat_id = event.chat.id
                    user_id = user.id
                    # Если ID нет в базе, берем имя из профиля
                    user_name = USER_MAPPING.get(user_id, user.full_name)
                    
                    if chat_id not in chat_histories:
                        chat_histories[chat_id] = deque(maxlen=10)
                    
                    formatted_line = f"[{user_name}]: {clean_text}"
                    chat_histories[chat_id].append(formatted_line)
                    
                    # 5. ЛОГИРОВАНИЕ ОЧЕРЕДИ
                    current_queue = list(chat_histories[chat_id])
                    logger.info(f"[QUEUE DEBUG] Updated context ({len(current_queue)} lines):\n" + "\n".join(current_queue))

        return await handler(event, data)

router.message.middleware(HistoryMiddleware())

# --- ФУНКЦИЯ ОЧИСТКИ ОТВЕТА ---
def process_model_output(full_response: str, input_context: str) -> Tuple[str | None, str | None]:
    """
    Возвращает: (чистый_текст_для_чата, строка_для_истории)
    """
    if not full_response:
        return None, None

    # Отрезаем контекст, который мы посылали
    # Проверка на точное совпадение начала
    if full_response.startswith(input_context):
        generated_only = full_response[len(input_context):]
    else:
        # Если модель чуть исказила начало, пробуем найти конец контекста
        # Но обычно startswith работает корректно, если промпт передан верно
        generated_only = full_response

    generated_only = generated_only.strip()
    if not generated_only:
        return None, None

    # Нам нужна ТОЛЬКО первая строка до следующего переноса строки, где начинается новое имя
    # Например: "[Саня]: Привет\n[Влад]: Пока" -> берем только "[Саня]: Привет"
    
    # Ищем, где начинается следующее сообщение (новая строка + квадратная скобка)
    split_match = re.search(r"\n\[.*?\]:", generated_only)
    if split_match:
        first_message_block = generated_only[:split_match.start()].strip()
    else:
        first_message_block = generated_only.strip()

    if not first_message_block:
        return None, None

    # Разбираем полученную строку: "[Имя]: Текст"
    match_prefix = re.match(r"^\[(.*?)\]:\s*(.*)", first_message_block)
    
    if match_prefix:
        persona_name = match_prefix.group(1)
        clean_text = match_prefix.group(2).strip()
        # Сохраняем как есть (со скобками)
        history_line = f"[{persona_name}]: {clean_text}"
        return clean_text, history_line
    else:
        # Если модель не выдала скобки (как в случае с 'Пока вы это делаете...')
        # Мы добавляем [BOT], чтобы не ломать структуру очереди для следующих запросов
        clean_text = first_message_block
        history_line = f"[BOT]: {clean_text}"
        return clean_text, history_line

# --- ЗАПРОС К API ---
async def make_api_request(chat_id: int) -> Tuple[str | None, str | None]:
    if not ML_MODEL_URL:
        logger.error("ML_MODEL_URL is not set!")
        return None, None
    
    # ФОРМИРОВАНИЕ ПРОМПТА (Строгое соответствие требованию)
    # Собираем строку так, чтобы после КАЖДОГО сообщения был \n
    history_list = list(chat_histories[chat_id])
    context_string = ""
    for line in history_list:
        context_string += f"{line}\n"
    
    # На выходе получается: "[Имя]: Текст\n[Имя2]: Текст2\n"

    url = ML_MODEL_URL
    if not url.endswith("generate"):
        url = f"{url.rstrip('/')}/generate"

    timeout_settings = aiohttp.ClientTimeout(total=40, connect=10)

    try:
        async with aiohttp.ClientSession(timeout=timeout_settings) as session:
            payload = {"prompt": context_string}
            
            logger.info(f"Generating... (Lock state: {api_lock.locked()})")
            start_time = time.time()
            
            async with session.post(url, json=payload) as response:
                duration = time.time() - start_time
                
                if response.status == 200:
                    data = await response.json()
                    # Модель возвращает: Промпт + Новое
                    raw_text = data.get("generated_text", "")
                    
                    # Логирование сырого начала ответа (без промпта) для отладки
                    if raw_text.startswith(context_string):
                        preview = raw_text[len(context_string):].strip()[:50]
                    else:
                        preview = raw_text[:50]
                    
                    logger.info(f"Done in {duration:.2f}s. Raw start: '{preview}...'")
                    
                    return process_model_output(raw_text, context_string)
                else:
                    logger.error(f"API Error {response.status}")
                    return None, None
                    
    except asyncio.TimeoutError:
        logger.error("API Timeout (>40s)")
        return None, None
    except Exception as e:
        logger.error(f"API Exception: {e}")
        return None, None

# --- КОМАНДЫ ---
@router.message(Command("threshold"))
async def set_threshold(message: Message, command: CommandObject):
    global CURRENT_THRESHOLD
    if message.from_user.id not in ADMIN_IDS:
        return
    if message.chat.id != ALLOWED_CHAT_ID:
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
    if message.chat.id != ALLOWED_CHAT_ID:
        return

    if message.text and message.text.strip().startswith("/"):
        return

    # Защита от старых сообщений (более 120 сек)
    if (datetime.now(message.date.tzinfo) - message.date).total_seconds() > 120:
        return

    trigger_type = None
    bot_id = message.bot.id
    text = message.text or ""
    
    if message.reply_to_message and message.reply_to_message.from_user.id == bot_id:
        trigger_type = "forced"
    elif f"@{BOT_USERNAME}" in text.lower():
        trigger_type = "forced"
    else:
        if api_lock.locked():
            return
        if random.random() < CURRENT_THRESHOLD:
            trigger_type = "random"

    if not trigger_type:
        return

    if trigger_type == "random" and api_lock.locked():
        logger.info("Skip random: Busy")
        return

    if message.chat.id in chat_histories and chat_histories[message.chat.id]:
        
        if trigger_type == "forced":
            await message.bot.send_chat_action(message.chat.id, "typing")
        
        async with api_lock:
            # Передаем ID чата, функция сама соберет промпт
            result_text, history_line = await make_api_request(message.chat.id)
        
        if result_text and history_line:
            try:
                if trigger_type == "forced":
                    await message.reply(result_text)
                else:
                    await message.answer(result_text)
                
                # Добавляем в историю (со скобками), чтобы контекст не прерывался
                chat_histories[message.chat.id].append(history_line)
                
                logger.info(f"[QUEUE DEBUG] Added bot response. Context:\n" + "\n".join(chat_histories[message.chat.id]))
                
            except Exception as e:
                logger.error(f"Failed to send message: {e}")

# --- ЗАПУСК ---
async def main():
    bot = Bot(token=os.getenv("BOT_TOKEN"))
    dp = Dispatcher()
    dp.include_router(router)
    
    await start_dummy_server()
    
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("🤖 Bot started polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
