import os
import logging
import random
import aiohttp
from collections import deque
from aiogram import Router, Bot
from aiogram.types import Message
from aiogram.filters import Command, CommandObject
from aiogram.enums import ChatType

router = Router()

CURRENT_THRESHOLD = float(os.getenv("THRESHOLD", "0.2"))
ML_MODEL_URL = os.getenv("ML_MODEL_URL")
admin_ids_str = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in admin_ids_str.split(",") if x.strip().isdigit()]

chat_histories = {}


async def make_api_request(context_string: str) -> str | None:
    """
    Делает запрос к API.
    Возвращает текст ответа или None, если произошла ошибка.
    """
    if not ML_MODEL_URL:
        logging.error("ML_MODEL_URL is not set!")
        return None

    try:
        async with aiohttp.ClientSession() as session:
            payload = {"input_string": context_string}
            
            async with session.post(
                ML_MODEL_URL,
                json=payload,
                timeout=10
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    text_resp = data if isinstance(data, str) else data.get("response") or str(data)
                    return text_resp
                else:
                    logging.error(f"API returned status {response.status}")
                    return None
    except Exception as e:
        logging.error(f"API Request failed: {e}")
        return None

@router.message(Command("threshold"))
async def set_threshold(message: Message, command: CommandObject):
    """
    Изменяет вероятность ответа (0.0 - 1.0).
    Доступно только админам.
    """
    if message.from_user.id not in ADMIN_IDS:
        return

    if not command.args:
        await message.reply(f"Текущий threshold: {CURRENT_THRESHOLD}")
        return

    try:
        new_value = float(command.args.replace(",", "."))
        if 0 <= new_value <= 1:
            global CURRENT_THRESHOLD
            CURRENT_THRESHOLD = new_value
            await message.reply(f"✅ Новый threshold установлен: {CURRENT_THRESHOLD}")
        else:
            await message.reply("❌ Число должно быть от 0 до 1")
    except ValueError:
        await message.reply("❌ Некорректное число. Пример: /threshold 0.5")


@router.message(Command("generate"))
async def force_generate(message: Message):
    """
    Принудительная генерация на основе текущего контекста.
    """
    chat_id = message.chat.id
    
    if chat_id not in chat_histories or not chat_histories[chat_id]:
        await message.reply("История пуста, напишите что-нибудь.")
        return

    context_string = "\n".join(chat_histories[chat_id]) + "\n"

    await message.bot.send_chat_action(chat_id, "typing")
    result = await make_api_request(context_string)

    if result:
        await message.reply(result)
    else:
        pass

@router.message()
async def handle_group_message(message: Message):
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return

    text = message.text or message.caption or ""
    if not text:
        return

    chat_id = message.chat.id
    user_name = message.from_user.full_name

    if chat_id not in chat_histories:
        chat_histories[chat_id] = deque(maxlen=10)

    formatted_line = f"[{user_name}]: {text}"
    chat_histories[chat_id].append(formatted_line)

    chance = random.random()
    
    if chance < CURRENT_THRESHOLD:
        context_string = "\n".join(chat_histories[chat_id]) + "\n"
        
        result = await make_api_request(context_string)
        
        if result:
            await message.answer(result)
