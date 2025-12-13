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

from aiogram import Router, Bot, BaseMiddleware
from aiogram.types import Message, TelegramObject
from aiogram.filters import Command, CommandObject
from aiogram.enums import ChatType

router = Router()

# --- КОНФИГУРАЦИЯ ---
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

# Блокировка для очереди запросов
api_lock = asyncio.Lock()

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

            # 1. Обрезка входящего текста
            if len(text) > MAX_INPUT_LENGTH:
                text = text[:MAX_INPUT_LENGTH]

            if text and not text.strip().startswith("/"):
                # Вырезаем тег бота и лишние пробелы
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
                    logger.debug(f"Saved to history: {formatted_line}")

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

    lines = [line.strip() for line in generated_only.split('\n') if line.strip()]
    
    if not lines:
        return None
    
    last_line = lines[-1]
    # Убираем возможные артефакты в начале строки типа "[Bot]:"
    cleaned_line = re.sub(r"^\[.*?\]:\s*", "", last_line)

    return cleaned_line if cleaned_line else None


# --- ЗАПРОС К API ---
async def make_api_request(context_string: str) -> str | None:
    if not ML_MODEL_URL:
        logger.error("ML_MODEL_URL is not set!")
        return None

    url = ML_MODEL_URL
    if not url.endswith("generate"):
        url = f"{url.rstrip('/')}/generate"

    # Настройка таймаутов: connect - быстро отвалиться если сервер лежит
    # total - ждать генерации не более 25 сек
    timeout_settings = aiohttp.ClientTimeout(total=25, connect=5)

    try:
        async with aiohttp.ClientSession(timeout=timeout_settings) as session:
            payload = {"prompt": context_string}
            
            logger.info(f"POST Request sending... (Queue size: Locked={api_lock.locked()})")
            start_time = time.time()
            
            async with session.post(
                url,
                headers={"Content-Type": "application/json"},
                json=payload
            ) as response:
                
                duration = time.time() - start_time
                logger.info(f"Request finished in {duration:.2f}s with status {response.status}")

                if response.status == 200:
                    data = await response.json()
                    raw_text = data.get("generated_text", "")
                    return clean_model_output(raw_text, context_string)
                else:
                    logger.error(f"API Error. Status: {response.status}")
                    return None
                    
    except asyncio.TimeoutError:
        logger.error("API Error: Timeout (Server took >25s or unreachable)")
        return None
    except Exception as e:
        logger.error(f"API Connection Error: {e}")
        return None


# --- КОМАНДЫ ---
@router.message(Command("threshold"))
async def set_threshold(message: Message, command: CommandObject):
    global CURRENT_THRESHOLD
    if message.from_user.id not in ADMIN_IDS:
        return

    if not command.args:
        await message.reply(f"Текущий threshold: {CURRENT_THRESHOLD}")
        return

    try:
        new_value = float(command.args.replace(",", "."))
        if 0 <= new_value <= 1:
            CURRENT_THRESHOLD = new_value
            await message.reply(f"✅ Новый threshold: {CURRENT_THRESHOLD}")
        else:
            await message.reply("❌ Число от 0 до 1")
    except ValueError:
        await message.reply("❌ Некорректное число")


# --- ОБРАБОТЧИК СООБЩЕНИЙ ---
@router.message()
async def handle_messages(message: Message):
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return

    if message.text and message.text.strip().startswith("/"):
        return

    # --- ЗАЩИТА ОТ СТАРЫХ СООБЩЕНИЙ ---
    # Если сообщение старше 120 секунд, игнорируем его (чтобы бот не отвечал на историю после рестарта)
    msg_date = message.date
    if (datetime.now(msg_date.tzinfo) - msg_date).total_seconds() > 120:
        logger.warning(f"Skipping old message from {msg_date}")
        return

    text = message.text or ""
    # Обрезаем текст и тут для проверки триггеров
    if len(text) > MAX_INPUT_LENGTH:
        text = text[:MAX_INPUT_LENGTH]

    trigger_type = None
    bot_id = message.bot.id
    
    # 1. Reply
    if message.reply_to_message and message.reply_to_message.from_user.id == bot_id:
        trigger_type = "forced"
        logger.info("Trigger: Reply to bot")

    # 2. Mention
    elif f"@{BOT_USERNAME}" in text.lower():
        trigger_type = "forced"
        logger.info("Trigger: Mention of bot")

    # 3. Random
    else:
        # ПРОВЕРКА БЛОКИРОВКИ СРАЗУ
        # Если бот уже занят генерацией, мы даже не кидаем кубик для рандома, чтобы не спамить в логи
        if api_lock.locked():
            return 

        chance = random.random()
        if chance < CURRENT_THRESHOLD:
            logger.info(f"Random trigger hit! ({chance:.4f} < {CURRENT_THRESHOLD})")
            trigger_type = "random"
        else:
            logger.info(f"Random skip ({chance:.4f} >= {CURRENT_THRESHOLD})")

    # --- ГЕНЕРАЦИЯ ---
    if trigger_type:
        # Двойная проверка для Random: если блокировка занята - выход
        if trigger_type == "random" and api_lock.locked():
            logger.info("Skipping random generation (Lock busy)")
            return

        if message.chat.id not in chat_histories or not chat_histories[message.chat.id]:
             return 

        context_string = "\n".join(chat_histories[message.chat.id]) + "\n"
        
        # Визуальная реакция только для forced, чтобы не спамить "печатает" постоянно
        if trigger_type == "forced":
            await message.bot.send_chat_action(message.chat.id, "typing")
        
        # Блокировка
        async with api_lock:
            # Внутри блокировки проверяем ещё раз, не прошло ли слишком много времени
            result = await make_api_request(context_string)
        
        if result:
            if trigger_type == "forced":
                await message.reply(result)
            else:
                await message.answer(result)
            
            chat_histories[message.chat.id].append(f"[BOT]: {result}")
