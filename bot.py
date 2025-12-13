import os
import logging
import random
import aiohttp
from collections import deque
from aiogram import Router, Bot
from aiogram.types import Message
from aiogram.enums import ChatType

router = Router()

THRESHOLD = float(os.getenv("THRESHOLD", "0.2"))
ML_MODEL_URL = os.getenv("ML_MODEL_URL")

chat_histories = {}

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

    if chance < THRESHOLD:
        context_string = "\n".join(chat_histories[chat_id]) + "\n"
        
        try:
            async with aiohttp.ClientSession() as session:
                payload = {
                    "input_string": context_string
                }
                
                async with session.post(
                    ML_MODEL_URL,
                    json=payload,
                    timeout=10
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        model_response = data if isinstance(data, str) else data.get("response", str(data))
                        
                        if model_response:
                            await message.answer(model_response)
                    else:
                        logging.error(f"API Error: {response.status}")
                        
        except Exception as e:
            logging.error(f"Error calling ML model: {e}")
