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

# --- КОНФИГУРАЦИЯ ---
# 1. СЮДА ВПИШИ ID ПОЛЬЗОВАТЕЛЕЙ (числа).
# Если ID нет в списке, бот будет использовать имя из профиля Telegram.
USER_MAPPING = {
    814759080: "A. H.",
    1214336850: "Саня Блок",
    485898893: "Александр Блок",
    485898893: "Старый Мельник",
    1313515064: "Булгак",
    485898893: "Егориус",
    485898893: "Некит Русанов",
    485898893: "Влад Блок",
    1035739386: "Вован Крюк"
}

# Настройка логирования
logger = logging.getLogger(__name__)

CURRENT_THRESHOLD = float(os.getenv("THRESHOLD", "0.2"))
ML_MODEL_URL = os.getenv("ML_MODEL_URL")
admin_ids_str = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in admin_ids_str.split(",") if x.strip().isdigit()]

logger.info(f"Initial THRESHOLD: {CURRENT_THRESHOLD}")
logger.info(f"ML_MODEL_URL: {ML_MODEL_URL}")
logger.info(f"Loaded ADMIN_IDS: {ADMIN_IDS}")

chat_histories = {}


# --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ЗАПРОСА ---
async def make_api_request(context_string: str) -> str | None:
    logger.info(f"API Request started. Context size: {len(context_string)} chars.")
    
    if not ML_MODEL_URL:
        logger.error("ML_MODEL_URL is not set!")
        return None

    try:
        async with aiohttp.ClientSession() as session:
            payload = {"input_string": context_string}
            
            logger.debug(f"Sending payload to API: {payload}")
            
            async with session.post(
                ML_MODEL_URL,
                json=payload,
                timeout=10
            ) as response:
                logger.info(f"API Response received. Status: {response.status}")
                
                if response.status == 200:
                    data = await response.json()
                    
                    text_resp = data if isinstance(data, str) else data.get("generated_text") or str(data)
                    logger.debug(f"Parsed API response: {text_resp[:50]}...")
                    return text_resp
                else:
                    logger.error(f"API returned non-200 status: {response.status}")
                    logger.error(f"API response content: {await response.text()}")
                    return None
    except aiohttp.ClientError as e:
        logger.error(f"AIOHTTP Client Error during API request: {e}")
        return None
    except Exception as e:
        logger.error(f"General Error during API request: {e}")
        return None


# --- КОМАНДЫ ---

@router.message(Command("threshold"))
async def set_threshold(message: Message, command: CommandObject):
    global CURRENT_THRESHOLD
    user_id = message.from_user.id
    logger.info(f"Command /threshold received from user ID {user_id}.")
    
    if user_id not in ADMIN_IDS:
        logger.warning(f"User ID {user_id} is not an admin. Ignoring command.")
        return

    if not command.args:
        logger.info("Displaying current threshold.")
        await message.reply(f"Текущий threshold: {CURRENT_THRESHOLD}")
        return

    try:
        new_value = float(command.args.replace(",", "."))
        if 0 <= new_value <= 1:
            CURRENT_THRESHOLD = new_value
            logger.info(f"New threshold set to: {CURRENT_THRESHOLD}")
            await message.reply(f"✅ Новый threshold установлен: {CURRENT_THRESHOLD}")
        else:
            logger.warning(f"Invalid threshold value attempted: {new_value}")
            await message.reply("❌ Число должно быть от 0 до 1")
    except ValueError:
        logger.error(f"ValueError for threshold command args: {command.args}")
        await message.reply("❌ Некорректное число. Пример: /threshold 0.5")


@router.message(Command("generate"))
async def force_generate(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    logger.info(f"Command /generate received in chat {chat_id} from user {user_id}.")
    
    if chat_id not in chat_histories or not chat_histories[chat_id]:
        logger.warning(f"History is empty for chat {chat_id}. Cannot generate.")
        await message.reply("История пуста, напишите что-нибудь.")
        return

    context_string = "\n".join(chat_histories[chat_id]) + "\n"
    logger.info(f"Generating based on {len(chat_histories[chat_id])} messages.")

    await message.bot.send_chat_action(chat_id, "typing")
    result = await make_api_request(context_string)

    if result:
        logger.info("Generation successful. Sending response.")
        await message.reply(result)
    else:
        logger.warning("Generation failed (API returned no valid response). Staying silent.")


# --- ОБРАБОТКА СООБЩЕНИЙ ---

@router.message()
async def handle_group_message(message: Message):
    # Проверка типа чата
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        logger.debug(f"Ignoring message in chat type: {message.chat.type}")
        return

    text = message.text or message.caption or ""
    if not text:
        logger.debug("Ignoring message with no text/caption.")
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Маппинг имени
    user_name = USER_MAPPING.get(user_id, message.from_user.full_name)
    
    logger.info(f"New message from [{user_name}] ({user_id}) in chat {chat_id}.")

    # Инициализация истории
    if chat_id not in chat_histories:
        chat_histories[chat_id] = deque(maxlen=10)
        logger.debug(f"Initialized new history for chat {chat_id}.")

    # Формирование строки и добавление в историю
    formatted_line = f"[{user_name}]: {text}"
    chat_histories[chat_id].append(formatted_line)
    logger.debug(f"History size now: {len(chat_histories[chat_id])}")

    # Логика рандома
    chance = random.random()
    logger.info(f"Random chance generated: {chance:.4f}. Current threshold: {CURRENT_THRESHOLD}")
    
    if chance < CURRENT_THRESHOLD:
        logger.info("Threshold passed! Initiating API request.")
        
        # Формируем строку для API
        context_string = "\n".join(chat_histories[chat_id]) + "\n"
        
        result = await make_api_request(context_string)
        
        if result:
            logger.info("API call successful. Answering message.")
            await message.answer(result)
        else:
            logger.warning("API call failed (no valid response). Bot remains silent.")
    else:
        logger.info("Threshold not passed. Bot remains silent.")
