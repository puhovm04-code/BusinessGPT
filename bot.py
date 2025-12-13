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

# Начальный порог
CURRENT_THRESHOLD = float(os.getenv("THRESHOLD", "0.2"))

# Адрес API
ML_MODEL_URL = os.getenv("ML_MODEL_URL")

# Список ID админов для смены порога
admin_ids_str = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in admin_ids_str.split(",") if x.strip().isdigit()]

# Хранилище истории: {chat_id: deque(maxlen=10)}
chat_histories = {}


# --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ЗАПРОСА ---
async def make_api_request(context_string: str) -> str | None:
    if not ML_MODEL_URL:
        logging.error("ML_MODEL_URL is not set!")
        return None

    try:
        async with aiohttp.ClientSession() as session:
            # Отправляем JSON с ключом input_string
            payload = {"input_string": context_string}
            
            async with session.post(
                ML_MODEL_URL,
                json=payload,
                timeout=10
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    # Поддержка разных форматов ответа (строка или json)
                    text_resp = data if isinstance(data, str) else data.get("response") or str(data)
                    return text_resp
                else:
                    logging.error(f"API returned status {response.status}")
                    return None
    except Exception as e:
        logging.error(f"API Request failed: {e}")
        return None


# --- КОМАНДЫ ---

@router.message(Command("threshold"))
async def set_threshold(message: Message, command: CommandObject):
    global CURRENT_THRESHOLD  # <--- ИСПРАВЛЕНО: global должно быть первой строкой
    
    if message.from_user.id not in ADMIN_IDS:
        return

    if not command.args:
        await message.reply(f"Текущий threshold: {CURRENT_THRESHOLD}")
        return

    try:
        new_value = float(command.args.replace(",", "."))
        if 0 <= new_value <= 1:
            CURRENT_THRESHOLD = new_value
            await message.reply(f"✅ Новый threshold установлен: {CURRENT_THRESHOLD}")
        else:
            await message.reply("❌ Число должно быть от 0 до 1")
    except ValueError:
        await message.reply("❌ Некорректное число. Пример: /threshold 0.5")


@router.message(Command("generate"))
async def force_generate(message: Message):
    chat_id = message.chat.id
    
    if chat_id not in chat_histories or not chat_histories[chat_id]:
        await message.reply("История пуста, напишите что-нибудь.")
        return

    # Собираем контекст: строки объединяем через \n, и в конце тоже добавляем \n
    context_string = "\n".join(chat_histories[chat_id]) + "\n"

    await message.bot.send_chat_action(chat_id, "typing")
    result = await make_api_request(context_string)

    if result:
        await message.reply(result)


# --- ОБРАБОТКА СООБЩЕНИЙ ---

@router.message()
async def handle_group_message(message: Message):
    if message.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return

    text = message.text or message.caption or ""
    if not text:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # --- МАППИНГ ИМЕН ---
    # Берём имя из словаря по ID, если нет — берем имя из Telegram
    user_name = USER_MAPPING.get(user_id, message.from_user.full_name)

    # Инициализация истории
    if chat_id not in chat_histories:
        chat_histories[chat_id] = deque(maxlen=10)

    # Формирование строки: [Имя]: Сообщение
    # \n здесь не ставим, оно добавится при склеивании (.join) перед отправкой
    formatted_line = f"[{user_name}]: {text}"
    chat_histories[chat_id].append(formatted_line)

    # Логика рандома
    chance = random.random()
    
    if chance < CURRENT_THRESHOLD:
        # Склеиваем историю. 
        # join добавит \n между сообщениями. 
        # + "\n" добавит перенос в самом конце.
        context_string = "\n".join(chat_histories[chat_id]) + "\n"
        
        result = await make_api_request(context_string)
        
        if result:
            await message.answer(result)
