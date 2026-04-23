import os
import asyncio
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BotCommand
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import google.generativeai as genai
import asyncpg
from aiohttp import web

# === НАСТРОЙКИ ===
# Вставь сюда свои токены или получай их из переменных окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "ТВОЙ_ТЕЛЕГРАМ_ТОКЕН")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "ТВОЙ_GEMINI_ТОКЕН")
DATABASE_URL = os.getenv("DATABASE_URL", "ТВОЙ_DATABASE_URL")

# Инициализация бота, диспетчера
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Настройка Google AI Studio (Gemini)
genai.configure(api_key=GEMINI_API_KEY)
# Используем модель 1.5 Flash, так как она отлично работает с аудио и бесплатна в лимитах
model = genai.GenerativeModel('gemini-1.5-flash')

# Пул подключений к базе данных PostgreSQL
db_pool = None

# Состояния для регистрации пользователя
class Registration(StatesGroup):
    waiting_for_name = State()

# Генератор системного промпта для оценки IELTS с учетом имени
def get_ielts_prompt(user_name: str) -> str:
    return f"""
You are an expert IELTS examiner. The user, whose name is {user_name}, has sent you an audio recording of their spoken answer.
Please address the user by their name in your feedback.
First, provide a full transcript of what they said.
Then, evaluate their answer based on the 4 IELTS Speaking criteria:
1. Fluency and Coherence
2. Lexical Resource (Vocabulary)
3. Grammatical Range and Accuracy
4. Pronunciation

Provide brief, constructive feedback, highlight good vocabulary used, point out mistakes, and estimate a band score (e.g., 6.5).
Keep your response concise and formatted nicely with emojis. Answer in English.
Format your response using HTML tags:
🗣 <b>Transcript:</b> [transcript here]

📝 <b>Feedback:</b> [feedback here]
"""

# === ОБРАБОТЧИКИ КОМАНД ===

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    # Проверяем, есть ли пользователь в базе
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow('SELECT name FROM users WHERE user_id = $1', message.from_user.id)
        
    if user:
        name = user['name']
        welcome_text = (
            f"👋 С возвращением, <b>{name}</b>! Я твой ИИ-репетитор по IELTS Speaking.\n\n"
            "🎤 Жду твои голосовые ответы для оценки!\n"
            "🎯 Нажми /task, чтобы получить случайное задание для тренировки."
        )
        await message.answer(welcome_text, parse_mode="HTML")
    else:
        # Если пользователя нет, запрашиваем имя
        await message.answer("👋 Привет! Я твой ИИ-репетитор по IELTS Speaking.\n\nКак мне к тебе обращаться? Напиши свое имя:")
        await state.set_state(Registration.waiting_for_name)

@dp.message(Registration.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    name = message.text.strip()
    
    # Сохраняем пользователя в базу данных
    async with db_pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO users (user_id, name) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET name = $2',
            message.from_user.id, name
        )
        
    # Сбрасываем состояние (бот больше не ждет имя)
    await state.clear()
    
    welcome_text = (
        f"Отлично, <b>{name}</b>! Приятно познакомиться.\n\n"
        "🎤 <b>Как это работает:</b>\n"
        "Отправь мне голосовое сообщение с твоим ответом на любой вопрос IELTS.\n"
        "Я прослушаю его, переведу в текст и дам подробный фидбек!\n\n"
        "🎯 <b>Хочешь потренироваться?</b> Нажми команду /task, чтобы выбрать часть экзамена и получить задание.\n\n"
        "📚 А еще я буду каждый день присылать тебе полезные фразы для экзамена (или используй команду /phrase)."
    )
    await message.answer(welcome_text, parse_mode="HTML")

@dp.message(Command("task"))
async def cmd_task(message: Message):
    """Выбор части IELTS Speaking"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗣 Part 1 (Короткие вопросы)", callback_data="task_p1")],
        [InlineKeyboardButton(text="🗣 Part 2 (Монолог / Cue Card)", callback_data="task_p2")],
        [InlineKeyboardButton(text="🗣 Part 3 (Дискуссия)", callback_data="task_p3")],
        [InlineKeyboardButton(text="🎓 Full Exam (Все 3 части связанные)", callback_data="task_full")]
    ])
    
    await message.answer(
        "🎯 <b>Выбери часть IELTS Speaking для тренировки:</b>\n\n"
        "На настоящем экзамене эти части идут последовательно (введение, карточка-монолог и глубокая дискуссия по теме карточки).\n\n"
        "Ты можешь тренировать их отдельно или выбрать <b>Full Exam</b> для полного погружения!",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("task_"))
async def process_task_callback(callback: CallbackQuery):
    # Убираем кнопки у старого сообщения, чтобы не нажимали дважды
    await callback.message.edit_reply_markup(reply_markup=None) 
    processing_msg = await callback.message.answer("⏳ Генерирую задание...")
    
    task_type = callback.data
    
    if task_type == "task_p1":
        prompt = (
            "Generate a set of 3-4 IELTS Speaking Part 1 questions on a single random everyday topic "
            "(like work, studies, hometown, hobbies, or food). "
            "Format cleanly using HTML tags like <b>bold</b>. Do NOT use markdown asterisks (*)."
        )
    elif task_type == "task_p2":
        prompt = (
            "Generate a random, unique IELTS Speaking Part 2 cue card. "
            "Include the main topic and 3-4 bullet points. "
            "Format cleanly using HTML tags like <b>bold</b>. Do NOT use markdown asterisks (*)."
        )
    elif task_type == "task_p3":
        prompt = (
            "Generate a set of 3-4 IELTS Speaking Part 3 abstract discussion questions on a random societal topic. "
            "Format cleanly using HTML tags like <b>bold</b>. Do NOT use markdown asterisks (*)."
        )
    elif task_type == "task_full":
        prompt = (
            "Generate a complete, sequential IELTS Speaking mock test. "
            "Part 1: 3-4 short questions on an everyday topic. "
            "Part 2: A cue card on a different topic. "
            "Part 3: 3-4 deep discussion questions strictly related to the Part 2 topic. "
            "Format cleanly using HTML tags like <b>bold</b>. Do NOT use markdown asterisks (*)."
        )
    else:
        return

    try:
        response = await asyncio.to_thread(model.generate_content, prompt)
        safe_text = response.text.replace("**", "") # На всякий случай удаляем звездочки, если нейросеть ошиблась
        
        try:
            await processing_msg.edit_text(
                f"🎯 <b>Твое задание:</b>\n\n{safe_text}\n\n🎤 <i>Запиши голосовое сообщение с ответом, и я его проверю!</i>", 
                parse_mode="HTML"
            )
        except Exception:
            await processing_msg.edit_text(
                f"🎯 Твое задание:\n\n{safe_text}\n\n🎤 Запиши голосовое сообщение с ответом, и я его проверю!"
            )
    except Exception as e:
        logging.error(f"Task gen error: {e}")
        await processing_msg.edit_text("❌ Произошла ошибка при генерации задания. Попробуй позже.")
    
    # Отвечаем серверу Telegram, что клик обработан
    await callback.answer()

@dp.message(Command("phrase"))
async def cmd_phrase(message: Message):
    """Позволяет получить фразу прямо сейчас через ИИ"""
    processing_msg = await message.answer("⏳ Ищу интересную фразу...")
    try:
        prompt = (
            "Generate ONE random, advanced C1/C2 English idiom, phrasal verb, or collocation useful for IELTS Speaking. "
            "Pick a random topic to ensure variety. "
            "Format strictly using HTML like this:\n"
            "📌 <b>[Phrase in English]</b> - [Translation to Russian]\n"
            "<i>[Example sentence in English]</i>\n"
            "Do NOT use markdown asterisks (*)."
        )
        response = await asyncio.to_thread(model.generate_content, prompt)
        safe_text = response.text.replace("**", "")
        
        try:
            await processing_msg.edit_text(f"Твоя случайная фраза для IELTS:\n\n{safe_text}", parse_mode="HTML")
        except Exception:
            await processing_msg.edit_text(f"Твоя случайная фраза для IELTS:\n\n{safe_text}")
    except Exception as e:
        logging.error(f"Phrase gen error: {e}")
        await processing_msg.edit_text("❌ Произошла ошибка при поиске фразы.")

@dp.message(Command("premium"))
async def cmd_premium(message: Message):
    """Реклама платного канала с пробным периодом"""
    text = (
        "👑 <b>Закрытый IELTS-клуб</b>\n\n"
        "Хочешь получать еще больше пользы и быстрее прокачать свой Speaking? Присоединяйся к моему закрытому Telegram-каналу!\n"
        "Там мы разбираем сложные темы Part 3, я публикую крутые шаблоны ответов на 8.0+ и мы проводим еженедельные созвоны.\n\n"
        "🎁 <b>Первый день — абсолютно бесплатно!</b> Затем всего 2000 тенге (или $5) в месяц.\n"
        "👉 <a href='ВСТАВЬ_СЮДА_ССЫЛКУ_ОТ_TRIBUTE'>Нажми сюда, чтобы забрать пробный день</a>"
    )
    # disable_web_page_preview=True убирает некрасивое превью ссылки внизу сообщения
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)

# === ОБРАБОТКА ГОЛОСОВЫХ СООБЩЕНИЙ ===

@dp.message(F.voice)
async def handle_voice(message: Message):
    # Уведомляем пользователя, что бот думает
    processing_msg = await message.answer("⏳ Скачиваю аудио...")
    
    try:
        # 1. Скачиваем голосовое сообщение
        voice_file_info = await bot.get_file(message.voice.file_id)
        file_path = f"voice_{message.from_user.id}.ogg"
        await bot.download_file(voice_file_info.file_path, file_path)
        
        await processing_msg.edit_text("⏳ Анализирую аудио через Gemini 1.5 Flash (транскрибация + оценка)...")

        # Получаем имя пользователя из БД для персонального фидбека
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow('SELECT name FROM users WHERE user_id = $1', message.from_user.id)
        user_name = user['name'] if user else "Student"

        # 2. Загружаем аудио в Google AI Studio и получаем ответ
        # Так как Gemini SDK работает синхронно, запускаем в отдельном потоке
        audio_file = await asyncio.to_thread(genai.upload_file, path=file_path)
        
        response = await asyncio.to_thread(
            model.generate_content,
            [get_ielts_prompt(user_name), audio_file]
        )
        
        feedback = response.text
        
        # Удаляем файлы из облака Google и с локального диска
        await asyncio.to_thread(audio_file.delete)
        os.remove(file_path)

        safe_feedback = feedback.replace("**", "")

        # 3. Отправляем финальный результат
        try:
            await processing_msg.edit_text(safe_feedback, parse_mode="HTML")
        except Exception:
            await processing_msg.edit_text(safe_feedback)

    except Exception as e:
        logging.error(f"Error processing voice: {e}")
        await processing_msg.edit_text("❌ Произошла ошибка при обработке аудио. Возможно, проблема с API ключом Gemini.")

# === ЕЖЕДНЕВНАЯ РАССЫЛКА ===

async def send_daily_phrase():
    # Берем всех пользователей из базы
    async with db_pool.acquire() as conn:
        users = await conn.fetch('SELECT user_id, name FROM users')
        
    if not users:
        return
    
    # Генерируем уникальную фразу на сегодня с помощью Gemini
    try:
        prompt = (
            "Generate ONE random, advanced C1/C2 English idiom or collocation for IELTS Speaking. "
            "Pick a completely random topic (e.g., environment, technology, feelings, work) so it doesn't repeat. "
            "Format strictly using HTML like this:\n"
            "📌 <b>[Phrase in English]</b> - [Translation to Russian]\n"
            "<i>[Example sentence in English]</i>\n"
            "Do NOT use markdown asterisks (*)."
        )
        response = await asyncio.to_thread(model.generate_content, prompt)
        phrase = response.text.strip().replace("**", "")
    except Exception as e:
        logging.error(f"Daily phrase gen error: {e}")
        return

    for user in users:
        user_id = user['user_id']
        name = user['name']
        try:
            # Обращаемся к пользователю по имени
            try:
                await bot.send_message(user_id, f"🌟 <b>Ежедневная фраза для тебя, {name}:</b>\n\n{phrase}", parse_mode="HTML")
            except Exception:
                await bot.send_message(user_id, f"🌟 Ежедневная фраза для тебя, {name}:\n\n{phrase}")
        except Exception as e:
            logging.error(f"Failed to send phrase to {user_id}: {e}")

# === ФИКТИВНЫЙ ВЕБ-СЕРВЕР ДЛЯ RENDER ===

async def handle_ping(request):
    return web.Response(text="Бот работает и слушает Render!")

async def start_dummy_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080)) # Render сам передаст нужный порт
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Фиктивный веб-сервер запущен на порту {port}")

# === ЗАПУСК БОТА ===

async def main():
    logging.basicConfig(level=logging.INFO)
    
    # Устанавливаем красивое меню команд в Telegram
    await bot.set_my_commands([
        BotCommand(command="start", description="Перезапустить бота"),
        BotCommand(command="task", description="Получить случайное задание"),
        BotCommand(command="phrase", description="Полезная фраза для IELTS"),
        BotCommand(command="premium", description="Закрытый IELTS-клуб")
    ])

    # Инициализация пула соединений БД и автоматическое создание таблицы, если её нет
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                name VARCHAR(255)
            )
        ''')

    # Настраиваем планировщик для ежедневной рассылки (например, в 10:00 утра)
    # Для теста можно поменять 'cron' на 'interval', seconds=30
    scheduler.add_job(send_daily_phrase, 'cron', hour=10, minute=0)
    scheduler.start()
    
    # Запускаем фиктивный веб-сервер для Render
    await start_dummy_server()
    
    print("Бот успешно запущен!")
    # Запуск поллинга
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

if __name__ == "__main__":
    asyncio.run(main())
