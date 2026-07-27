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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = "8546755625"  # Жестко зафиксированный ID администратора

# Жестко фиксируем модель Gemini 1.5
GEMINI_MODEL = "gemini-1.5-flash"
SUPPORT_USERNAME = "@AImanagerGemini"

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN. Проверьте переменные окружения.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY)
else:
    logger.error("Критическая ошибка: Не найден GEMINI_API_KEY!")
    client = None

# Лимиты для пользователей (высокие, но защищающие от перегрузки токена)
MAX_REQUESTS_PER_MINUTE = 12

# ==========================================
# 2. РАБОТА С БАЗОЙ ДАННЫХ (SQLite)
# ==========================================

DB_NAME = "bot_database.db"

async def init_db():
    """Создает таблицы в базе данных."""
    async with aiosqlite.connect(DB_NAME) as db:
        # Таблица пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                is_authorized BOOLEAN DEFAULT 0,
                joined_at TIMESTAMP
            )
        """)
        # Таблица одноразовых ключей доступа
        await db.execute("""
            CREATE TABLE IF NOT EXISTS access_keys (
                key_code TEXT PRIMARY KEY,
                is_used BOOLEAN DEFAULT 0
            )
        """)
        # Таблица активности и статистики
        await db.execute("""
            CREATE TABLE IF NOT EXISTS activity (
                user_id INTEGER,
                requests_count INTEGER DEFAULT 0,
                photo_count INTEGER DEFAULT 0,
                ai_type TEXT DEFAULT 'Gemini 1.5 Flash',
                PRIMARY KEY (user_id)
            )
        """)
        # Таблица чатов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                title TEXT,
                created_at TIMESTAMP
            )
        """)
        # Таблица сообщений в чатах
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                role TEXT,
                content TEXT
            )
        """)
        await db.commit()
    logger.info("База данных успешно инициализирована.")

async def register_user_if_not_exists(user: types.User):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, is_authorized, joined_at) VALUES (?, ?, ?, 0, ?)",
            (user.id, user.username, user.first_name, datetime.now())
        )
        await db.execute(
            "INSERT OR IGNORE INTO activity (user_id, requests_count, photo_count, ai_type) VALUES (?, 0, 0, ?)",
            (user.id, GEMINI_MODEL)
        )
        await db.commit()

async def is_user_authorized(user_id: int) -> bool:
    if str(user_id) == ADMIN_ID:
        return True
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT is_authorized FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else False

# ==========================================
# 3. КЛАВИАТУРЫ
# ==========================================

def get_start_unauth_keyboard():
    """Клавиатура для неавторизованного пользователя."""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Предоставить код", callback_data="enter_code")],
            [InlineKeyboardButton(text="💬 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}")]
        ]
    )
    return kb

def get_main_keyboard():
    """Постоянные кнопки для авторизованного пользователя."""
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🟢 НАЧАТЬ НОВЫЙ ЧАТ")],
            [KeyboardButton(text="📂 ПОСМОТРЕТЬ СТАРЫЕ ЧАТЫ"), KeyboardButton(text="🚪 Выйти с сессии")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Напишите сообщение..."
    )
    return kb

def get_exit_confirm_keyboard():
    """Подтверждение выхода из сессии."""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, выйти", callback_data="confirm_exit")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_exit")]
        ]
    )
    return kb

def get_admin_keyboard():
    """Панель администратора."""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Сгенерировать ключ", callback_data="admin_gen_key")],
            [InlineKeyboardButton(text="📊 Активность", callback_data="admin_activity")],
            [InlineKeyboardButton(text="📢 Написать всем", callback_data="admin_broadcast")]
        ]
    )
    return kb

# ==========================================
# 4. FSM СТЕЙТЫ
# ==========================================

class UserStates(StatesGroup):
    waiting_for_code = State()
    chatting = State()

class AdminStates(StatesGroup):
    waiting_for_broadcast = State()

# ==========================================
# 5. СТАРТ И АВТОРИЗАЦИЯ
# ==========================================

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await register_user_if_not_exists(message.from_user)
    await state.clear()

    if str(message.from_user.id) == ADMIN_ID:
        await message.answer("👋 Здравствуйте, Администратор!", reply_markup=get_main_keyboard())
        await state.set_state(UserStates.chatting)
        return

    authorized = await is_user_authorized(message.from_user.id)
    if authorized:
        await message.answer("👋 С возвращением в систему!", reply_markup=get_main_keyboard())
        await state.set_state(UserStates.chatting)
    else:
        await message.answer(
            "👋 Добро пожаловать!\nДля использования бота необходимо предоставить код доступа или обратиться в поддержку.",
            reply_markup=get_start_unauth_keyboard()
        )
        await state.set_state(UserStates.waiting_for_code)

@dp.callback_query(F.data == "enter_code")
async def cb_enter_code(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Пожалуйста, введите ваш одноразовый код доступа:")
    await callback.answer()

@dp.message(UserStates.waiting_for_code, F.text)
async def process_access_code(message: Message, state: FSMContext):
    code = message.text.strip()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT is_used FROM access_keys WHERE key_code = ?", (code,)) as cursor:
            row = await cursor.fetchone()
            
        if row is None:
            await message.answer("❌ Неверный код доступа. Попробуйте еще раз или обратитесь в поддержку.", reply_markup=get_start_unauth_keyboard())
            return
        
        if row[0] == 1:
            await message.answer("⚠️ Этот код уже был использован ранее.", reply_markup=get_start_unauth_keyboard())
            return

        # Помечаем ключ как использованный
        await db.execute("UPDATE access_keys SET is_used = 1 WHERE key_code = ?", (code,))
        # Авторизуем пользователя
        await db.execute("UPDATE users SET is_authorized = 1 WHERE user_id = ?", (message.from_user.id,))
        await db.commit()

    await message.answer("✅ Код успешно принят! Доступ открыт.", reply_markup=get_main_keyboard())
    await state.set_state(UserStates.chatting)

# ==========================================
# 6. УПРАВЛЕНИЕ СЕССИЕЙ И ВЫХОД
# ==========================================

@dp.message(F.text == "🚪 Выйти с сессии")
async def ask_exit_session(message: Message):
    if str(message.from_user.id) == ADMIN_ID:
        await message.answer("Администратор не может выйти из сессии.")
        return
    await message.answer("Точно ли хотите выйти, потеряв доступ к чатам?", reply_markup=get_exit_confirm_keyboard())

@dp.callback_query(F.data == "confirm_exit")
async def confirm_exit(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET is_authorized = 0 WHERE user_id = ?", (user_id,))
        await db.commit()
    
    await state.set_state(UserStates.waiting_for_code)
    await callback.message.edit_text("Вы вышли из сессии. Доступ закрыт.")
    await callback.message.answer("Введите новый код для входа:", reply_markup=get_start_unauth_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "cancel_exit")
async def cancel_exit(callback: CallbackQuery):
    await callback.message.edit_text("Выход отменен.")
    await callback.answer()

# ==========================================
# 7. УПРАВЛЕНИЕ ЧАТАМИ И ИСТОРИЕЙ (БЕЗ ПАМЯТИ МЕЖДУ СТАРЫМИ ЧАТАМИ)
# ==========================================

async def create_new_chat(user_id: int, first_message_text: str) -> int:
    title = first_message_text[:25] + ("..." if len(first_message_text) > 25 else "")
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT INTO chats (user_id, title, created_at) VALUES (?, ?, ?)",
            (user_id, title, datetime.now())
        )
        chat_id = cursor.lastrowid
        await db.commit()
    return chat_id

@dp.message(F.text == "🟢 НАЧАТЬ НОВЫЙ ЧАТ")
async def start_new_chat(message: Message, state: FSMContext):
    await state.update_data(current_chat_id=None)
    await message.answer("🟢 Начат новый чат. Напишите ваш запрос нейросети:", reply_markup=get_main_keyboard())

@dp.message(F.text == "📂 ПОСМОТРЕТЬ СТАРЫЕ ЧАТЫ")
async def show_old_chats(message: Message):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT chat_id, title FROM chats WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", (user_id,)) as cursor:
            chats = await cursor.fetchall()

    if not chats:
        await message.answer("📂 У вас пока нет сохраненных старых чатов.")
        return

    kb_builder = InlineKeyboardMarkup(inline_keyboard=[])
    for chat_id, title in chats:
        kb_builder.inline_keyboard.append([
            InlineKeyboardButton(text=f"💬 {title}", callback_data=f"open_chat_{chat_id}")
        ])

    await message.answer("📂 Ваши прошлые чаты (выберите для просмотра):", reply_markup=kb_builder)

@dp.callback_query(F.data.startswith("open_chat_"))
async def open_specific_chat(callback: CallbackQuery, state: FSMContext):
    chat_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    # Проверяем принадлежность чата
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, title FROM chats WHERE chat_id = ?", (chat_id,)) as cursor:
            chat_row = await cursor.fetchone()
        
        if not chat_row or chat_row[0] != user_id:
            await callback.answer("Чат не найден.", show_alert=True)
            return

        chat_title = chat_row[1]
        async with db.execute("SELECT role, content FROM messages WHERE chat_id = ? ORDER BY message_id ASC", (chat_id,)) as cursor:
            msgs = await cursor.fetchall()

    # Устанавливаем текущий чат в стейт, но исторически для генерации памяти НЕ используем (строго по ТЗ: памяти к старым чатам нет)
    await state.update_data(current_chat_id=chat_id)

    response_text = f"📂 **Чат: {chat_title}**\n\n"
    if not msgs:
        response_text += "История пуста."
    else:
        for role, content in msgs:
            prefix = "👤 Вы: " if role == "user" else "🤖 AI: "
            response_text += f"{prefix}{content}\n\n"

    await callback.message.answer(response_text, parse_mode="Markdown")
    await callback.answer()

# ==========================================
# 8. ПАНЕЛЬ АДМИНИСТРАТОРА (ФУНКЦИОНАЛ)
# ==========================================

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if str(message.from_user.id) != ADMIN_ID:
        return
    await message.answer("⚙️ **Админ-панель:**", reply_markup=get_admin_keyboard(), parse_mode="Markdown")

@dp.callback_query(F.data == "admin_gen_key")
async def admin_gen_key(callback: CallbackQuery):
    if str(callback.from_user.id) != ADMIN_ID:
        return
    
    import random
    import string
    new_key = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO access_keys (key_code, is_used) VALUES (?, 0)", (new_key,))
        await db.commit()

    await callback.message.answer(f"🔑 **Сгенерирован новый одноразовый ключ:**\n`{new_key}`", parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "admin_activity")
async def admin_activity(callback: CallbackQuery):
    if str(callback.from_user.id) != ADMIN_ID:
        return

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            U.user_id, U.username, A.requests_count, A.photo_count, A.ai_type 
            FROM users U JOIN activity A ON U.user_id = A.user_id
        """) if False else db.execute("""
            SELECT u.user_id, u.username, a.requests_count, a.photo_count, a.ai_type 
            FROM users u JOIN activity a ON u.user_id = a.user_id
        """) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await callback.message.answer("📊 Активность пользователей пуста.")
        await callback.answer()
        return

    text = "📊 **Активность пользователей:**\n\n"
    for uid, uname, reqs, photos, ai_t in rows:
        username_str = f"@{uname}" if uname else "Без юзернейма"
        text += f"• ID: `{uid}` ({username_str})\n" \
                f"  Запросов: {reqs} | Фото: {photos} | ИИ: {ai_t}\n\n"

    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()

@dp.message(Command("delete"))
async def cmd_delete_user(message: Message):
    if str(message.from_user.id) != ADMIN_ID:
        return
    
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: `/delete <id_человека>`", parse_mode="Markdown")
        return
    
    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("Неверный формат ID.")
        return

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET is_authorized = 0 WHERE user_id = ?", (target_id,))
        await db.commit()

    # Принудительно сбрасываем стейт и шлем уведомление пользователю
    try:
        await bot.send_message(target_id, "⚠️ Ваша сессия была завершена администратором.")
    except Exception:
        pass

    await message.answer(f"✅ Сессия пользователя `{target_id}` успешно сброшена.", parse_mode="Markdown")

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != ADMIN_ID:
        return
    await callback.message.answer("Введите сообщение для рассылки всем пользователям, нажавшим старт:")
    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.answer()

@dp.message(StateFilter(AdminStates.waiting_for_broadcast))
async def process_broadcast(message: Message, state: FSMContext):
    if str(message.from_user.id) != ADMIN_ID:
        return

    text_to_send = message.text
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            users = await db.fetchall() if hasattr(db, 'fetchall') else await cursor.fetchall()
            # Исправление выборки
            rows = await cursor.fetchall() if not users else users

    success = 0
    # Получаем заново список чисто через execute
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            all_users = [row[0] for row in await cursor.fetchall()]

    status_msg = await message.answer("⏳ Рассылка запущенна...")
    for uid in all_users:
        try:
            await bot.send_message(uid, text_to_send)
            success += 1
            await asyncio.sleep(0.03)
        except Exception:
            pass

    await status_msg.edit_text(f"✅ Рассылка завершена. Успешно отправлено: {success} пользователям.")
    await state.clear()

# ==========================================
# 9. ОБРАБОТКА ЗАПРОСОВ К GEMINI 1.5
# ==========================================

@dp.message(UserStates.chatting, F.text)
async def handle_chat_message(message: Message, state: FSMContext):
    user_id = message.from_user.id

    # Проверка авторизации
    if str(user_id) != ADMIN_ID and not await is_user_authorized(user_id):
        await message.answer("❌ Ваша сессия завершена или не авторизована.")
        await state.set_state(UserStates.waiting_for_code)
        return

    # Проверка лимитов запросов (анти-спам)
    # Упрощенная проверка через подсчет в базе или простая задержка
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT requests_count FROM activity WHERE user_id = ?", (user_id,)) as cursor:
            res = await cursor.fetchone()
            reqs = res[0] if res else 0

    if not client:
        await message.answer("❌ Ошибка хостинга: API-ключ не настроен.")
        return

    data = await state.get_data()
    current_chat_id = data.get("current_chat_id")

    # Если чат еще не выбран, создаем новый автоматически
    if not current_chat_id:
        current_chat_id = await create_new_chat(user_id, message.text)
        await state.update_data(current_chat_id=current_chat_id)

    # Сохраняем сообщение пользователя в БД чата
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO messages (chat_id, role, content) VALUES (?, 'user', ?)", (current_chat_id, message.text))
        await db.commit()

    await bot.send_chat_action(chat_id=user_id, action="typing")

    try:
        # ЗАПРОС К GEMINI 1.5 (Без памяти к старым чатам, контекст только текущего чата)
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT role, content FROM messages WHERE chat_id = ? ORDER BY message_id ASC", (current_chat_id,)) as cursor:
                chat_history_rows = await cursor.fetchall()

        # Формируем содержимое для Gemini 1.5
        contents = [
            genai_types.ContentDict(role=r, parts=[c]) for r, c in chat_history_rows
        ]

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
        )
        ai_reply = response.text

        # Сохраняем ответ модели в БД чата
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT INTO messages (chat_id, role, content) VALUES (?, 'model', ?)", (current_chat_id, ai_reply))
            # Увеличиваем счетчик запросов
            await db.execute("UPDATE activity SET requests_count = requests_count + 1 WHERE user_id = ?", (user_id,))
            await db.commit()

        await message.answer(ai_reply, parse_mode="Markdown")

    except errors.APIError as e:
        err_str = str(e)
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            await message.answer("⏳ Превышен лимит запросов к Gemini 1.5. Пожалуйста, подождите минутку.")
        else:
            await message.answer(f"❌ Ошибка API: {err_str}")
    except Exception as e:
        logger.exception(f"Ошибка: {e}")
        await message.answer("❌ Произошла внутренняя ошибка обработки.")

# ==========================================
# 10. ЗАПУСК
# ==========================================

async def main():
    await init_db()
    logger.info("🚀 Бот запущен и готов к работе!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
