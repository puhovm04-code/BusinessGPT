import os
import logging
import random
import aiohttp
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

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CURRENT_THRESHOLD = float(os.getenv("THRESHOLD", "0.2"))
ML_MODEL_URL = os.getenv("ML_MODEL_URL")
admin_ids_str = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in admin_ids_str.split(",") if x.strip().isdigit()]

logger.info(f"Initial THRESHOLD: {CURRENT_THRESHOLD}")
logger.info(f"ML_MODEL_URL: {ML_MODEL_URL}")

chat_histories = {}

# --- MIDDLEWARE (ЧТОБЫ БОТ ЗАПОМИНАЛ ВСЕ) ---
class HistoryMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        if isinstance(event, Message) and event.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            text = event.text or event.caption or ""

            # Игнорируем пустые сообщения и КОМАНДЫ (начинаются с /)
            if text and not text.strip().startswith("/"):
                chat_id = event.chat.id
                user_id = event.from_user.id
                user_name = USER_MAPPING.get(user_id, event.from_user.full_name)
                
                if chat_id not in chat_histories:
                    chat_histories[chat_id] = deque(maxlen=10)
                
                formatted_line = f"[{user_name}]: {text}"
                chat_histories[chat_id].append(formatted_line)
                logger.debug(f"Saved to history: {formatted_line}")

        return await handler(event, data)

router.message.middleware(HistoryMiddleware())


# --- ФУНКЦИЯ ЗАПРОСА (ИСПРАВЛЕНА ПОД API) ---
async def make_api_request(context_string: str) -> str | None:
    if not ML_MODEL_URL:
        logger.error("ML_MODEL_URL is not set!")
        return None

    # Добавляем /generate, если его нет в URL
    url = ML_MODEL_URL
    if not url.endswith("generate"):
        url = f"{url.rstrip('/')}/generate"

    try:
        async with aiohttp.ClientSession() as session:
            # ИСПРАВЛЕНИЕ 1: Ключ JSON изменен с "input_string" на "prompt"
            payload = {"prompt": context_string}
            
            logger.info(f"POST Request to: {url}")
            
            async with session.post(
                url,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=20
            ) as response:
                
                if response.status == 200:
                    data = await response.json()
                    # ИСПРАВЛЕНИЕ 2: Берем поле "generated_text"
                    return data.get("generated_text")
                else:
                    logger.error(f"API Error. Status: {response.status}")
                    logger.error(f"Response text: {await response.text()}")
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


@router.message(Command("generate"))
async def force_generate(message: Message):
    chat_id = message.chat.id
    
    # Так как мы теперь игнорируем команды в истории, проверяем, есть ли там вообще обычные сообщения
    if chat_id not in chat_histories or not chat_histories[chat_id]:
        await message.reply("История пуста (команды не сохраняются). Напишите что-нибудь.")
        return

    context_string = "\n".join(chat_histories[chat_id]) + "\n"
    
    await message.bot.send_chat_action(chat_id, "typing")
    result = await make_api_request(context_string)

    if result:
        await message.reply(result)
        chat_histories[chat_id].append(f"[BOT]: {result}")
    else:
        await message.reply("Ошибка API (проверьте логи консоли).")


# --- ОБРАБОТКА РАНДОМА ---

@router.message()
async def handle_random_response(message: Message):
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return

    # Пропускаем команды, чтобы бот не триггерился на случайные команды
    if message.text and message.text.startswith("/"):
        return

    chance = random.random()
    logger.info(f"Chance: {chance:.4f} / Threshold: {CURRENT_THRESHOLD}")
    
    if chance < CURRENT_THRESHOLD:
        chat_id = message.chat.id
        # Если история пуста, берем текущее сообщение (так как middleware его уже добавил, если оно не команда)
        if chat_id not in chat_histories:
             # На всякий случай, если middleware сработал позже или рассинхрон
             return 

        context_string = "\n".join(chat_histories[chat_id]) + "\n"
        
        result = await make_api_request(context_string)
        
        if result:
            await message.answer(result)
            chat_histories[chat_id].append(f"[BOT]: {result}")
