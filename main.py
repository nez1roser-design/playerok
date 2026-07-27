import os
import asyncio
import logging
import aiosqlite
from datetime import datetime
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.types import (
    Message, 
    CallbackQuery, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from google import genai
from google.genai import errors
from google.genai import types as genai_types

# ==========================================
# 1. НАСТРОЙКИ И ИНИЦИАЛИЗАЦИЯ
# ==========================================

# Включаем подробное логирование для отслеживания ошибок на хостинге
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Загружаем переменные окружения (для Bothost или локального .env)
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# ID администратора нужно обязательно прописать в Bothost
ADMIN_ID = os.getenv("ADMIN_ID") 

# Жестко фиксируем стабильную модель, чтобы избежать ошибки "limit: 0"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

# Проверка наличия критических токенов
if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN. Проверьте переменные окружения.")

# Инициализация Telegram бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Инициализация клиента Google Gemini
if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY)
else:
    logger.error("Критическая ошибка: Не найден GEMINI_API_KEY!")
    client = None

# Глобальный словарь для временного хранения истории (в памяти)
# Формат: {user_id: [message_1, message_2, ...]}
user_histories = {}
MAX_HISTORY_LENGTH = 10  # Сколько сообщений бот помнит для контекста

# ==========================================
# 2. РАБОТА С БАЗОЙ ДАННЫХ (SQLite)
# ==========================================

DB_NAME = "bot_database.db"

async def init_db():
    """Создает таблицы в базе данных при первом запуске, если их нет."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TIMESTAMP
            )
        """)
        await db.commit()
    logger.info("База данных SQLite успешно инициализирована.")

async def add_user_to_db(user: types.User):
    """Добавляет нового пользователя в базу данных."""
    async with aiosqlite.connect(DB_NAME) as db:
        # Используем INSERT OR IGNORE, чтобы не дублировать записи
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, joined_at) VALUES (?, ?, ?, ?)",
            (user.id, user.username, user.first_name, datetime.now())
        )
        await db.commit()

async def get_users_count() -> int:
    """Возвращает общее количество пользователей бота."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0

async def get_all_user_ids() -> list:
    """Возвращает список ID всех пользователей (нужно для рассылки)."""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

# ==========================================
# 3. КЛАВИАТУРЫ (МЕНЮ)
# ==========================================

def get_main_keyboard():
    """Создает постоянную нижнюю клавиатуру."""
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🧹 Очистить контекст")],
            [KeyboardButton(text="🤖 Мой профиль"), KeyboardButton(text="ℹ️ Помощь")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Напиши запрос нейросети..."
    )
    return kb

def get_admin_keyboard():
    """Создает Inline-клавиатуру для админ-панели."""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin_broadcast")]
        ]
    )
    return kb

# ==========================================
# 4. FSM СТЕЙТЫ (ДЛЯ АДМИН-РАССЫЛКИ)
# ==========================================

class AdminStates(StatesGroup):
    waiting_for_broadcast_message = State()

# ==========================================
# 5. БАЗОВЫЕ ХЭНДЛЕРЫ
# ==========================================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    """Обработка команды /start."""
    await add_user_to_db(message.from_user)
    
    # Очищаем историю при перезапуске диалога
    user_histories[message.from_user.id] = []
    
    welcome_text = (
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        f"Я умный AI-ассистент на базе Google Gemini. Я помню контекст "
        f"нашей беседы и готов помочь с любыми задачами: от написания кода до ответов на сложные вопросы.\n\n"
        f"Просто напиши мне что-нибудь!"
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard())

@dp.message(F.text == "ℹ️ Помощь")
@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Обработка команды помощи."""
    help_text = (
        "🛠 **Доступные функции:**\n\n"
        "• Просто пиши текст, и я отвечу.\n"
        "• Я запоминаю последние сообщения диалога.\n"
        "• Кнопка **«Очистить контекст»** нужна, если ты хочешь сменить тему разговора, "
        "чтобы я не путался в старых данных.\n\n"
        "Если я долго не отвечаю, возможно исчерпан лимит запросов, подожди пару минут."
    )
    await message.answer(help_text, parse_mode="Markdown")

@dp.message(F.text == "🤖 Мой профиль")
async def show_profile(message: Message):
    """Показывает информацию о профиле пользователя."""
    profile_text = (
        f"👤 **Твой профиль:**\n"
        f"ID: `{message.from_user.id}`\n"
        f"Имя: {message.from_user.first_name}\n"
        f"Статус: Активный пользователь AI\n"
    )
    await message.answer(profile_text, parse_mode="Markdown")

@dp.message(F.text == "🧹 Очистить контекст")
@dp.message(Command("clear"))
async def cmd_clear_context(message: Message):
    """Очищает память нейросети для конкретного пользователя."""
    user_id = message.from_user.id
    user_histories[user_id] = []
    await message.answer("✅ Контекст диалога успешно очищен! Можем начать новую тему.", reply_markup=get_main_keyboard())

# ==========================================
# 6. ПАНЕЛЬ АДМИНИСТРАТОРА
# ==========================================

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    """Открывает скрытую админ-панель."""
    if not ADMIN_ID or str(message.from_user.id) != str(ADMIN_ID):
        # Если юзер не админ, игнорируем его
        return
        
    await message.answer(
        "⚙️ **Панель управления ботом**\nВыберите действие ниже:",
        reply_markup=get_admin_keyboard(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "admin_stats")
async def admin_callback_stats(callback: CallbackQuery):
    """Показывает количество пользователей в БД."""
    if str(callback.from_user.id) != str(ADMIN_ID):
        return await callback.answer("У вас нет прав!", show_alert=True)
        
    users_count = await get_users_count()
    await callback.message.edit_text(
        f"📊 **Статистика бота:**\n\n"
        f"👥 Всего пользователей: **{users_count}**\n"
        f"🧠 Модель API: `{GEMINI_MODEL}`",
        reply_markup=get_admin_keyboard(),
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_callback_broadcast(callback: CallbackQuery, state: FSMContext):
    """Запускает процесс рассылки."""
    if str(callback.from_user.id) != str(ADMIN_ID):
        return await callback.answer("У вас нет прав!", show_alert=True)
        
    await callback.message.answer("Введите текст для рассылки всем пользователям бота (или напишите 'отмена'):")
    await state.set_state(AdminStates.waiting_for_broadcast_message)
    await callback.answer()

@dp.message(StateFilter(AdminStates.waiting_for_broadcast_message))
async def process_broadcast_message(message: Message, state: FSMContext):
    """Рассылает сообщение всем юзерам из базы."""
    if message.text.lower() == 'отмена':
        await message.answer("Рассылка отменена.")
        await state.clear()
        return

    users = await get_all_user_ids()
    success_count = 0
    fail_count = 0
    
    # Отправляем сообщение о старте рассылки
    status_msg = await message.answer("⏳ Начинаю рассылку...")
    
    for user_id in users:
        try:
            await bot.send_message(chat_id=user_id, text=message.text)
            success_count += 1
            await asyncio.sleep(0.05) # Пауза, чтобы не словить спам-блок от Telegram
        except Exception as e:
            logger.warning(f"Не удалось отправить юзеру {user_id}: {e}")
            fail_count += 1

    await status_msg.edit_text(
        f"✅ **Рассылка завершена!**\n\n"
        f"Успешно: {success_count}\n"
        f"Ошибок (заблокировали бота): {fail_count}",
        parse_mode="Markdown"
    )
    await state.clear()

# ==========================================
# 7. ГЛАВНЫЙ ОБРАБОТЧИК (GEMINI AI)
# ==========================================

@dp.message(F.text)
async def handle_ai_request(message: Message):
    """Принимает все текстовые сообщения и отправляет их в Gemini."""
    if not client:
        return await message.answer("❌ Ошибка хостинга: API-ключ Gemini не настроен.")

    user_id = message.from_user.id
    user_text = message.text

    # Инициализируем историю для нового пользователя, если её нет
    if user_id not in user_histories:
        user_histories[user_id] = []

    # Добавляем сообщение пользователя в локальную память
    user_histories[user_id].append({"role": "user", "parts": [user_text]})
    
    # Обрезаем историю, чтобы не превысить лимиты токенов
    if len(user_histories[user_id]) > MAX_HISTORY_LENGTH:
        # Убираем самые старые сообщения (срез списка)
        user_histories[user_id] = user_histories[user_id][-MAX_HISTORY_LENGTH:]

    # Показываем статус "Печатает...", пока ждем ответ от серверов Google
    await bot.send_chat_action(chat_id=user_id, action="typing")

    try:
        # Формируем контент для отправки
        # Используем genai_types.ContentDict для передачи истории сообщений
        contents = [
            genai_types.ContentDict(role=msg["role"], parts=msg["parts"])
            for msg in user_histories[user_id]
        ]

        # Асинхронно ждем ответ от нейросети
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
        )
        
        ai_reply = response.text
        
        # Сохраняем ответ нейросети в историю
        user_histories[user_id].append({"role": "model", "parts": [ai_reply]})
        
        # Отправляем ответ в Telegram
        await message.answer(ai_reply, parse_mode="Markdown")

    except errors.APIError as e:
        error_msg = str(e)
        logger.error(f"Gemini API Error for user {user_id}: {error_msg}")
        
        # Обработка ошибки лимитов (429)
        if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
            # Удаляем последний запрос из истории, так как он не был обработан
            user_histories[user_id].pop()
            await message.answer(
                "⏳ **Сервер перегружен запросами.**\n"
                "Бесплатный лимит Google исчерпан. Пожалуйста, подожди 1-2 минуты и повтори запрос.",
                parse_mode="Markdown"
            )
        else:
            await message.answer(f"❌ Произошла ошибка API: `{error_msg}`", parse_mode="Markdown")
            
    except Exception as e:
        logger.exception(f"Непредвиденная ошибка: {e}")
        await message.answer("❌ Внутренняя ошибка обработки. Разработчик уже уведомлен.")

# ==========================================
# 8. ЗАПУСК БОТА
# ==========================================

async def main():
    """Главная функция запуска бота и базы данных."""
    logger.info("Подготовка к запуску...")
    
    # Инициализируем БД
    await init_db()
    
    logger.info(f"🚀 Бот успешно запущен! Используется модель: {GEMINI_MODEL}")
    
    # Пропускаем старые апдейты из Telegram, чтобы бот не спамил при включении
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Запускаем поллинг
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот принудительно остановлен.")
