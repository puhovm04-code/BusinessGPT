import os
import logging
import random
import aiohttp
import re
import asyncio  # Добавили для блокировки
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CURRENT_THRESHOLD = float(os.getenv("THRESHOLD", "0.2"))
ML_MODEL_URL = os.getenv("ML_MODEL_URL")
admin_ids_str = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in admin_ids_str.split(",") if x.strip().isdigit()]

logger.info(f"Initial THRESHOLD: {CURRENT_THRESHOLD}")
logger.info(f"ML_MODEL_URL: {ML_MODEL_URL}")

chat_histories = {}

# Глобальная блокировка, чтобы запросы уходили по очереди
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

            if text and not text.strip().startswith("/"):
                # Вырезаем тег бота
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

    try:
        async with aiohttp.ClientSession() as session:
            payload = {"prompt": context_string}
            
            logger.info(f"POST Request to: {url}")
            
            # Увеличили timeout до 60 секунд, чтобы не падало при очереди
            async with session.post(
                url,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=60 
            ) as response:
                
                if response.status == 200:
                    data = await response.json()
                    raw_text = data.get("generated_text", "")
                    return clean_model_output(raw_text, context_string)
                else:
                    logger.error(f"API Error. Status: {response.status}")
                    text = await response.text()
                    logger.error(f"Response text: {text}")
                    return None
                    
    except asyncio.TimeoutError:
        logger.error("API Error: Timeout (Server took too long to respond)")
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

    chat_id = message.chat.id
    text = message.text or ""
    
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
        chance = random.random()
        logger.info(f"Chance: {chance:.4f} / Threshold: {CURRENT_THRESHOLD}")
        if chance < CURRENT_THRESHOLD:
            trigger_type = "random"

    # --- ГЕНЕРАЦИЯ ---
    if trigger_type:
        # Если это рандом, но сервер занят другим запросом - пропускаем, чтобы не вешать очередь
        # Если forced (reply/mention) - ждем очереди
        if trigger_type == "random" and api_lock.locked():
            logger.info("Skipping random generation due to high load (Lock is busy)")
            return

        if chat_id not in chat_histories or not chat_histories[chat_id]:
             return 

        context_string = "\n".join(chat_histories[chat_id]) + "\n"
        
        await message.bot.send_chat_action(chat_id, "typing")
        
        # Используем LOCK, чтобы запросы шли строго по одному
        async with api_lock:
            result = await make_api_request(context_string)
        
        if result:
            if trigger_type == "forced":
                await message.reply(result)
            else:
                await message.answer(result)
            
            chat_histories[chat_id].append(f"[BOT]: {result}")
