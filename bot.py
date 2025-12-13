import os
import logging
import random
import aiohttp
import re
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

# Имя бота для вырезания из текста (должно совпадать с реальным юзернеймом без @)
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

# --- MIDDLEWARE (ИСТОРИЯ + ОЧИСТКА ОТ ТЕГОВ) ---
class HistoryMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        if isinstance(event, Message) and event.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]:
            text = event.text or event.caption or ""

            # 1. Игнорируем команды, начинающиеся с /
            if text and not text.strip().startswith("/"):
                
                # 2. Вырезаем тег бота из текста (@botname), чтобы не засорять контекст
                # re.IGNORECASE позволяет удалять и @BusinessGPT_text_bot и @businessgpt_text_bot
                clean_text = re.sub(f"@{BOT_USERNAME}", "", text, flags=re.IGNORECASE).strip()
                
                # Убираем двойные пробелы, если они остались после удаления тега
                clean_text = re.sub(r'\s+', ' ', clean_text)

                if clean_text: # Сохраняем, только если остался текст
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


# --- ФУНКЦИЯ ОЧИСТКИ ОТВЕТА МОДЕЛИ ---
def clean_model_output(full_response: str, input_context: str) -> str | None:
    if not full_response:
        return None

    # Отрезаем повтор входного контекста
    if full_response.startswith(input_context):
        generated_only = full_response[len(input_context):]
    else:
        generated_only = full_response

    # Берем последнюю строку
    lines = [line.strip() for line in generated_only.split('\n') if line.strip()]
    
    if not lines:
        return None
    
    last_line = lines[-1]

    # Удаляем "[Имя]: " из начала строки
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
            
            async with session.post(
                url,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=20
            ) as response:
                
                if response.status == 200:
                    data = await response.json()
                    raw_text = data.get("generated_text", "")
                    
                    final_text = clean_model_output(raw_text, context_string)
                    return final_text
                else:
                    logger.error(f"API Error. Status: {response.status}")
                    logger.error(f"Response text: {await response.text()}")
                    return None
    except Exception as e:
        logger.error(f"API Connection Error: {e}")
        return None


# --- КОМАНДЫ УПРАВЛЕНИЯ ---

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


# --- ЕДИНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ ---
@router.message()
async def handle_messages(message: Message):
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return

    # Игнорируем команды
    if message.text and message.text.strip().startswith("/"):
        return

    chat_id = message.chat.id
    text = message.text or ""
    
    # --- ЛОГИКА ТРИГГЕРА ---
    should_reply = False
    
    # 1. Проверяем, является ли сообщение ответом (reply) на сообщение бота
    # message.bot.id вернет ID текущего бота
    bot_id = message.bot.id
    if message.reply_to_message and message.reply_to_message.from_user.id == bot_id:
        should_reply = True
        logger.info("Trigger: Reply to bot")

    # 2. Проверяем, тегнули ли бота (@businessgpt_text_bot)
    elif f"@{BOT_USERNAME}" in text.lower():
        should_reply = True
        logger.info("Trigger: Mention of bot")

    # 3. Если не тегнули и не реплай, используем рандом
    else:
        chance = random.random()
        logger.info(f"Chance: {chance:.4f} / Threshold: {CURRENT_THRESHOLD}")
        if chance < CURRENT_THRESHOLD:
            should_reply = True

    # --- ГЕНЕРАЦИЯ ---
    if should_reply:
        # Если история пуста, бот не может сгенерировать контекст
        if chat_id not in chat_histories or not chat_histories[chat_id]:
             # Если сообщение содержит только тег и история пуста, можно ответить заглушкой или промолчать
             return 

        context_string = "\n".join(chat_histories[chat_id]) + "\n"
        
        # Показываем статус "печатает..."
        await message.bot.send_chat_action(chat_id, "typing")
        
        result = await make_api_request(context_string)
        
        if result:
            # Отвечаем реплаем на сообщение, которое стриггерило бота
            await message.reply(result)
            chat_histories[chat_id].append(f"[BOT]: {result}")
