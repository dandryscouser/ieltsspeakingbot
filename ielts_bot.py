import os
import asyncio
import logging
from datetime import datetime
import re
import csv
import io
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
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "ТВОЙ_ТЕЛЕГРАМ_ТОКЕН")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "ТВОЙ_GEMINI_ТОКЕН")
DATABASE_URL = os.getenv("DATABASE_URL", "ТВОЙ_DATABASE_URL")
# ID администратора (твой Telegram ID)
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Инициализация Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = None
db_pool = None

# Хранилище для фоновых задач (чтобы Python их не удалял сборщиком мусора)
background_tasks = set()

# === СОСТОЯНИЯ (FSM) ===
class Registration(StatesGroup):
    waiting_for_name = State()

class ExamState(StatesGroup):
    waiting_for_part2 = State() # Ожидание ответа на Part 2 для таймера

# === ПРОМПТ ===
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

Provide brief, constructive feedback, highlight good vocabulary used, point out mistakes.
At the very end, give a final estimated band score (from 0 to 9.0).
Keep your response concise and formatted nicely with emojis. Answer in English.
Format your response EXACTLY using these HTML tags:
🗣 <b>Transcript:</b> [transcript here]

📝 <b>Feedback:</b> [feedback here]

📊 <b>Band Score:</b> [Score here, e.g., 6.5]
"""

# === ОБРАБОТЧИКИ КОМАНД ===

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
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
        await message.answer("👋 Привет! Я твой ИИ-репетитор по IELTS Speaking.\n\nКак мне к тебе обращаться? Напиши свое имя:")
        await state.set_state(Registration.waiting_for_name)

@dp.message(Registration.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    name = message.text.strip()
    async with db_pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO users (user_id, name) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET name = $2',
            message.from_user.id, name
        )
    await state.clear()
    
    welcome_text = (
        f"Отлично, <b>{name}</b>! Приятно познакомиться.\n\n"
        "🎤 <b>Как это работает:</b>\n"
        "Отправь мне голосовое сообщение с твоим ответом на любой вопрос IELTS.\n"
        "Я прослушаю его, переведу в текст и дам подробный фидбек!\n\n"
        "🎯 <b>Хочешь потренироваться?</b> Нажми команду /task, чтобы выбрать часть экзамена и получить задание.\n\n"
        "📚 А еще я буду каждый день присылать тебе полезные фразы для экзамена."
    )
    await message.answer(welcome_text, parse_mode="HTML")

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    """Сбрасывает все текущие таймеры и состояния"""
    await state.clear()
    await message.answer("✅ Действие отменено. Все таймеры сброшены. Бот готов к новым заданиям! Жми /task")

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    """Показывает статистику ученика"""
    processing_msg = await message.answer("⏳ Собираю твою статистику...")
    
    async with db_pool.acquire() as conn:
        records = await conn.fetch('SELECT score FROM attempts WHERE user_id = $1 ORDER BY created_at DESC', message.from_user.id)
    
    if not records:
        await processing_msg.edit_text("❌ У тебя пока нет оценок. Запиши хотя бы одно аудио, чтобы появилась статистика!")
        return
        
    valid_scores = []
    for r in records:
        try:
            valid_scores.append(float(r['score']))
        except ValueError:
            pass
            
    if not valid_scores:
        await processing_msg.edit_text("❌ В базе есть твои ответы, но не удалось извлечь из них цифровые баллы.")
        return
        
    avg_score = sum(valid_scores) / len(valid_scores)
    last_scores = valid_scores[:3]
    last_scores_str = ", ".join(map(str, last_scores))
    
    text = (
        f"📊 <b>Твоя статистика IELTS Speaking:</b>\n\n"
        f"📈 Средний балл: <b>{avg_score:.1f}</b>\n"
        f"🎯 Последние оценки: {last_scores_str}\n"
        f"🎤 Всего проверенных попыток: {len(records)}\n\n"
        f"<i>Продолжай тренироваться с /task, чтобы улучшить свой результат!</i>"
    )
    await processing_msg.edit_text(text, parse_mode="HTML")

@dp.message(Command("task"))
async def cmd_task(message: Message, state: FSMContext):
    await state.clear() # Сбрасываем старые таймеры, если человек нажал /task заново
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗣 Part 1 (Короткие вопросы)", callback_data="task_p1")],
        [InlineKeyboardButton(text="🗣 Part 2 (Монолог / Cue Card)", callback_data="task_p2")],
        [InlineKeyboardButton(text="🗣 Part 3 (Дискуссия)", callback_data="task_p3")],
        [InlineKeyboardButton(text="🎓 Full Exam", callback_data="task_full")]
    ])
    await message.answer("🎯 <b>Выбери часть IELTS Speaking для тренировки:</b>", reply_markup=keyboard, parse_mode="HTML")

# --- ТАЙМЕР ДЛЯ PART 2 ---
async def part2_timer(user_id: int, state: FSMContext, bot: Bot):
    """Асинхронный таймер для имитации реального экзамена"""
    try:
        await asyncio.sleep(70) # 10 сек чтение + 60 сек подготовка
        current_state = await state.get_state()
        if current_state == ExamState.waiting_for_part2.state:
            await bot.send_message(
                user_id, 
                "⏳ <b>Время на подготовку вышло!</b>\nНачинай говорить. Жду твое аудио (до 2 минут).\n\n<i>⚠️ У тебя есть 3 минуты на отправку ответа.</i>", 
                parse_mode="HTML"
            )
            
            # Ожидаем ответа 3 минуты (180 секунд)
            await asyncio.sleep(180)
            current_state = await state.get_state()
            if current_state == ExamState.waiting_for_part2.state:
                await bot.send_message(user_id, "❌ <b>Время вышло!</b> Ты не отправил аудиоответ за 3 минуты. Задание отменено. Жми /task для нового.", parse_mode="HTML")
                await state.clear()
    except Exception as e:
        logging.error(f"Timer error for user {user_id}: {e}")


@dp.callback_query(F.data.startswith("task_"))
async def process_task_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None) 
    processing_msg = await callback.message.answer("⏳ Генерирую задание...")
    task_type = callback.data
    
    prompt = ""
    is_part_2 = False

    if task_type == "task_p1":
        prompt = "Generate a set of 3-4 IELTS Speaking Part 1 questions. Format cleanly using ONLY <b> and <i> HTML tags. Use standard dashes (-) for bullet points. Do NOT use markdown asterisks (*)."
    elif task_type == "task_p2":
        is_part_2 = True
        prompt = "Generate a random IELTS Speaking Part 2 cue card. Format cleanly using ONLY <b> and <i> HTML tags. Use standard dashes (-) for bullet points. Do NOT use markdown asterisks (*)."
    elif task_type == "task_p3":
        prompt = "Generate 3-4 IELTS Speaking Part 3 questions. Format cleanly using ONLY <b> and <i> HTML tags. Use standard dashes (-) for bullet points. Do NOT use markdown asterisks (*)."
    elif task_type == "task_full":
        prompt = "Generate a complete IELTS Speaking mock test (Part 1, 2, 3). Format cleanly using ONLY <b> and <i> HTML tags. Use standard dashes (-) for bullet points. Do NOT use markdown asterisks (*)."

    try:
        response = await model.generate_content_async(prompt)
        safe_text = response.text.replace("**", "")
        
        if is_part_2:
            # Если это Part 2, запускаем таймер
            await state.set_state(ExamState.waiting_for_part2)
            
            # Создаем задачу и СОХРАНЯЕМ на нее ссылку, чтобы Python ее не удалил
            task = asyncio.create_task(part2_timer(callback.from_user.id, state, bot))
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)
            
            msg_text = (f"🎯 <b>Твое задание (Part 2):</b>\n\n{safe_text}\n\n"
                        f"⏱ <i>У тебя есть 70 секунд (10 на чтение и 60 на подготовку). Я пришлю уведомление, когда нужно будет начать говорить!</i>")
        else:
            msg_text = f"🎯 <b>Твое задание:</b>\n\n{safe_text}\n\n🎤 <i>Запиши голосовое сообщение с ответом, и я его проверю!</i>"

        try:
            await processing_msg.edit_text(msg_text, parse_mode="HTML")
        except Exception:
            await processing_msg.edit_text(msg_text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
            
    except Exception as e:
        await processing_msg.edit_text(f"❌ Произошла ошибка при генерации: {str(e)}")
    
    await callback.answer()

@dp.message(Command("phrase"))
async def cmd_phrase(message: Message):
    processing_msg = await message.answer("⏳ Ищу интересную фразу...")
    try:
        prompt = "Generate ONE random advanced C1/C2 English idiom for IELTS. Format strictly using HTML: 📌 <b>[Phrase]</b> - [Russian translation]\n<i>[Example]</i>. DO NOT use markdown asterisks (*)."
        response = await model.generate_content_async(prompt)
        safe_text = response.text.replace("**", "")
        try:
            await processing_msg.edit_text(f"Твоя случайная фраза для IELTS:\n\n{safe_text}", parse_mode="HTML")
        except Exception:
            await processing_msg.edit_text(f"Твоя случайная фраза для IELTS:\n\n{safe_text}")
    except Exception as e:
        await processing_msg.edit_text(f"❌ Ошибка генерации: {str(e)}")

@dp.message(Command("export"))
async def cmd_export(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ У вас нет прав для выполнения этой команды.")
        return

    processing_msg = await message.answer("⏳ Собираю данные из базы...")
    async with db_pool.acquire() as conn:
        records = await conn.fetch('SELECT u.name, a.attempt_number, a.score FROM attempts a JOIN users u ON a.user_id = u.user_id ORDER BY u.name, a.attempt_number')
    
    if not records:
        await processing_msg.edit_text("❌ Нет данных для выгрузки.")
        return

    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(['Имя пользователя', 'Попытка №', 'Балл'])
    for r in records:
        writer.writerow([r['name'], r['attempt_number'], r['score']])
    
    output.seek(0)
    file = types.BufferedInputFile(output.getvalue().encode('utf-8-sig'), filename="ielts_results.csv")
    await processing_msg.delete()
    await message.answer_document(file, caption="📊 Таблица со всеми результатами учеников")

# === ОБРАБОТКА ГОЛОСОВЫХ СООБЩЕНИЙ ===

@dp.message(F.voice)
async def handle_voice(message: Message, state: FSMContext):
    # ФИЛЬТР СПАМА И ДЛИННЫХ АУДИО (максимум 130 секунд)
    if message.voice.duration > 130:
        await message.answer("❌ Вы не уложились в 2 минуты. Задание IELTS Speaking Part 2 длится не более 2 минут. Пожалуйста, запишите ответ заново (до 130 секунд).")
        return

    # Если был активен таймер для Part 2 — отключаем его (так как человек ответил)
    await state.clear()

    processing_msg = await message.answer("⏳ Скачиваю аудио...")
    try:
        voice_file_info = await bot.get_file(message.voice.file_id)
        file_path = f"voice_{message.from_user.id}.ogg"
        await bot.download_file(voice_file_info.file_path, file_path)
        
        await processing_msg.edit_text("⏳ Анализирую аудио через Gemini...")

        async with db_pool.acquire() as conn:
            user = await conn.fetchrow('SELECT name FROM users WHERE user_id = $1', message.from_user.id)
        user_name = user['name'] if user else "Student"

        audio_file = await asyncio.to_thread(genai.upload_file, path=file_path)
        response = await model.generate_content_async([get_ielts_prompt(user_name), audio_file])
        feedback = response.text
        
        await asyncio.to_thread(audio_file.delete)
        os.remove(file_path)

        safe_feedback = feedback.replace("**", "")

        score_match = re.search(r'Band Score:</b>\s*([\d\.]+)', safe_feedback, re.IGNORECASE)
        band_score = score_match.group(1) if score_match else "N/A"

        async with db_pool.acquire() as conn:
            attempt_num = await conn.fetchval('SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM attempts WHERE user_id = $1', message.from_user.id)
            await conn.execute('INSERT INTO attempts (user_id, attempt_number, score) VALUES ($1, $2, $3)', message.from_user.id, attempt_num, band_score)

        try:
            await processing_msg.edit_text(safe_feedback, parse_mode="HTML")
        except Exception:
            await processing_msg.edit_text(safe_feedback.replace("<b>", "").replace("</b>", ""))

    except Exception as e:
        logging.error(f"Error processing voice: {e}")
        await processing_msg.edit_text(f"❌ Произошла ошибка при обработке аудио: {str(e)}")

# === ЕЖЕДНЕВНАЯ РАССЫЛКА ===

async def send_daily_phrase():
    async with db_pool.acquire() as conn:
        users = await conn.fetch('SELECT user_id, name FROM users')
    if not users: return
    
    try:
        prompt = "Generate ONE random advanced C1/C2 English idiom for IELTS. Format strictly using HTML: 📌 <b>[Phrase]</b> - [Russian translation]\n<i>[Example]</i>. Do NOT use markdown asterisks (*)."
        response = await model.generate_content_async(prompt)
        phrase = response.text.strip().replace("**", "")
    except Exception:
        return

    for user in users:
        try:
            await bot.send_message(user['user_id'], f"🌟 <b>Ежедневная фраза для тебя, {user['name']}:</b>\n\n{phrase}", parse_mode="HTML")
        except Exception:
            pass

# === ФИКТИВНЫЙ ВЕБ-СЕРВЕР ДЛЯ RENDER ===

async def handle_ping(request):
    return web.Response(text="Бот работает и слушает Render!")

async def start_dummy_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# === ЗАПУСК БОТА ===

async def main():
    logging.basicConfig(level=logging.INFO)
    
    await bot.set_my_commands([
        BotCommand(command="start", description="Перезапустить бота"),
        BotCommand(command="task", description="Получить случайное задание"),
        BotCommand(command="stats", description="Моя статистика и прогресс"),
        BotCommand(command="cancel", description="Отменить текущее задание/таймер"),
        BotCommand(command="phrase", description="Полезная фраза для IELTS")
    ])

    global model
    try:
        available_models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        target_model = next((m.replace('models/', '') for m in available_models if 'flash' in m.lower()), available_models[0].replace('models/', '') if available_models else 'gemini-1.5-flash-latest')
        model = genai.GenerativeModel(target_model)
    except Exception:
        model = genai.GenerativeModel('gemini-1.5-flash-latest')

    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, name VARCHAR(255));
            CREATE TABLE IF NOT EXISTS attempts (id SERIAL PRIMARY KEY, user_id BIGINT, attempt_number INT, score VARCHAR(10), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        ''')

    scheduler.add_job(send_daily_phrase, 'cron', hour=10, minute=0)
    scheduler.start()
    
    await start_dummy_server()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
