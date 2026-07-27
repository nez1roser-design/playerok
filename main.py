import asyncio
import os
import uuid
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
from google import genai
from google.genai.errors import APIError

# Загружаем переменные окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash") # Берем модель из .env, по умолчанию 2.0-flash
ADMIN_ID = int(os.getenv("ADMIN_ID", 8546755625))

# Лимиты для пользователей
LIMIT_TEXT = 300
LIMIT_IMAGES = 100

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = genai.Client(api_key=GEMINI_API_KEY)

# ================= ФАЙЛ БАЗЫ ДАННЫХ =================
DB_FILE = "bot_database.db"

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            is_auth INTEGER DEFAULT 0,
            text_requests INTEGER DEFAULT 0,
            image_requests INTEGER DEFAULT 0
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS access_keys (
            key_code TEXT PRIMARY KEY,
            is_used INTEGER DEFAULT 0
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            created_at TEXT
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS messages (
            msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            role TEXT,
            text TEXT
        )''')
        await db.commit()

# ================= МАШИНА СОСТОЯНИЙ =================
class BotStates(StatesGroup):
    waiting_for_code = State()
    chatting_with_ai = State()
    waiting_for_broadcast = State()

# ================= КЛАВИАТУРЫ =================
def get_admin_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔑 Сгенерировать ключ"), KeyboardButton(text="📊 Активность")],
            [KeyboardButton(text="📢 Написать всем")]
        ],
        resize_keyboard=True
    )

def get_user_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="НАЧАТЬ НОВЫЙ ЧАТ")],
            [KeyboardButton(text="ПОСМОТРЕТЬ СТАРЫЕ ЧАТЫ")],
            [KeyboardButton(text="Выйти с сессии")]
        ],
        resize_keyboard=True
    )

def get_support_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="👨‍💻 Поддержка", url="https://t.me/AImanagerGemini")]]
    )

def get_logout_confirm_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, выйти", callback_data="logout_yes")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="logout_no")]
        ]
    )

# ================= АДМИН ПАНЕЛЬ =================
@dp.message(F.text == "🔑 Сгенерировать ключ", F.from_user.id == ADMIN_ID)
async def admin_gen_key(message: types.Message):
    new_key = str(uuid.uuid4())[:8].upper()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO access_keys (key_code) VALUES (?)", (new_key,))
        await db.commit()
    await message.answer(f"✅ Новый ключ сгенерирован:\n\n`{new_key}`\n\nОн сработает только 1 раз.", parse_mode="Markdown")

@dp.message(F.text == "📊 Активность", F.from_user.id == ADMIN_ID)
async def admin_activity(message: types.Message):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT user_id, username, text_requests, image_requests FROM users WHERE is_auth=1") as cursor:
            users = await cursor.fetchall()
            
    if not users:
        await message.answer("Пока нет авторизованных пользователей.")
        return
        
    text = f"📊 **Активность (Текущая модель: {GEMINI_MODEL}):**\n\n"
    for uid, uname, txt_req, img_req in users:
        uname_str = f"@{uname}" if uname else "Без юзернейма"
        text += f"👤 {uname_str} (ID: `{uid}`)\n📝 Текстов: {txt_req}/{LIMIT_TEXT} | 🖼 Картинок: {img_req}/{LIMIT_IMAGES}\n\n"
        
    text += "Чтобы удалить сессию: `/delete ID`"
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("delete"), F.from_user.id == ADMIN_ID)
async def admin_delete_user(message: types.Message):
    try:
        target_id = int(message.text.split()[1])
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("UPDATE users SET is_auth=0 WHERE user_id=?", (target_id,))
            await db.commit()
        await message.answer(f"✅ Сессия пользователя {target_id} завершена.")
    except (IndexError, ValueError):
        await message.answer("⚠️ Использование: `/delete 123456789`", parse_mode="Markdown")

@dp.message(F.text == "📢 Написать всем", F.from_user.id == ADMIN_ID)
async def admin_broadcast_start(message: types.Message, state: FSMContext):
    await message.answer("Введите сообщение для рассылки всем пользователям бота (или напишите 'отмена'):")
    await state.set_state(BotStates.waiting_for_broadcast)

@dp.message(BotStates.waiting_for_broadcast, F.from_user.id == ADMIN_ID)
async def admin_broadcast_send(message: types.Message, state: FSMContext):
    if message.text.lower() == 'отмена':
        await message.answer("Рассылка отменена.", reply_markup=get_admin_kb())
        await state.clear()
        return

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            users = await cursor.fetchall()

    count = 0
    for (uid,) in users:
        try:
            await bot.send_message(uid, f"📢 **Сообщение от администратора:**\n\n{message.text}", parse_mode="Markdown")
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await message.answer(f"✅ Рассылка завершена. Доставлено: {count} пользователям.", reply_markup=get_admin_kb())
    await state.clear()


# ================= ЛОГИКА ПОЛЬЗОВАТЕЛЯ =================
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
        await db.commit()
        
        async with db.execute("SELECT is_auth FROM users WHERE user_id=?", (user_id,)) as cursor:
            is_auth = (await cursor.fetchone())[0]

    if user_id == ADMIN_ID:
        await message.answer("Добро пожаловать в панель администратора!", reply_markup=get_admin_kb())
        return

    if is_auth == 1:
        await message.answer("С возвращением! Выберите действие:", reply_markup=get_user_kb())
    else:
        await message.answer("🔒 Предоставьте код доступа:", reply_markup=get_support_kb())
        await state.set_state(BotStates.waiting_for_code)

@dp.message(BotStates.waiting_for_code)
async def process_code(message: types.Message, state: FSMContext):
    code = message.text.strip().upper()
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT is_used FROM access_keys WHERE key_code=?", (code,)) as cursor:
            row = await cursor.fetchone()
            
            if row is None:
                await message.answer("❌ Неверный код. Попробуйте еще раз или обратитесь в поддержку.", reply_markup=get_support_kb())
                return
            if row[0] == 1:
                await message.answer("❌ Этот код уже был использован.")
                return
                
            await db.execute("UPDATE access_keys SET is_used=1 WHERE key_code=?", (code,))
            await db.execute("UPDATE users SET is_auth=1 WHERE user_id=?", (user_id,))
            await db.commit()
            
    await message.answer("✅ Доступ разрешен! Добро пожаловать.", reply_markup=get_user_kb())
    await state.clear()

# ================= МЕНЮ ПОЛЬЗОВАТЕЛЯ =================
@dp.message(F.text == "НАЧАТЬ НОВЫЙ ЧАТ")
async def start_new_chat(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT is_auth FROM users WHERE user_id=?", (user_id,)) as cursor:
            if (await cursor.fetchone())[0] == 0:
                return

    title = f"Чат от {datetime.now().strftime('%d.%m %H:%M')}"
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("INSERT INTO chats (user_id, title, created_at) VALUES (?, ?, ?)", 
                                  (user_id, title, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        chat_id = cursor.lastrowid
        await db.commit()

    await state.update_data(current_chat_id=chat_id)
    await state.set_state(BotStates.chatting_with_ai)
    await message.answer(f"🤖 Новый диалог начат!\nВсе следующие сообщения будут отправлены ИИ (Модель: {GEMINI_MODEL}).")

@dp.message(F.text == "ПОСМОТРЕТЬ СТАРЫЕ ЧАТЫ")
async def view_old_chats(message: types.Message):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT chat_id, title FROM chats WHERE user_id=? ORDER BY chat_id DESC LIMIT 10", (user_id,)) as cursor:
            chats = await cursor.fetchall()

    if not chats:
        await message.answer("У вас пока нет старых чатов.")
        return

    builder = InlineKeyboardMarkup(inline_keyboard=[])
    for chat_id, title in chats:
        builder.inline_keyboard.append([InlineKeyboardButton(text=title, callback_data=f"history_{chat_id}")])

    await message.answer("Ваши последние чаты:", reply_markup=builder)

@dp.message(F.text == "Выйти с сессии")
async def logout_attempt(message: types.Message):
    await message.answer("⚠️ Точно ли хотите выйти?", reply_markup=get_logout_confirm_kb())

@dp.callback_query(F.data.startswith("logout_"))
async def logout_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.data == "logout_yes":
        user_id = callback.from_user.id
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("UPDATE users SET is_auth=0 WHERE user_id=?", (user_id,))
            await db.commit()
        await callback.message.edit_text("✅ Вы вышли из системы.")
        await callback.message.answer("🔒 Предоставьте код доступа:", reply_markup=get_support_kb())
        await state.set_state(BotStates.waiting_for_code)
    else:
        await callback.message.edit_text("Выход отменен.")
    await callback.answer()

@dp.callback_query(F.data.startswith("history_"))
async def show_history(callback: types.CallbackQuery):
    chat_id = int(callback.data.split("_")[1])
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT role, text FROM messages WHERE chat_id=? ORDER BY msg_id ASC LIMIT 10", (chat_id,)) as cursor:
            msgs = await cursor.fetchall()
            
    if not msgs:
        await callback.message.answer("Этот чат пуст.")
    else:
        text = "📜 **История чата:**\n\n"
        for role, msg_text in msgs:
            icon = "👤" if role == "user" else "🤖"
            short_msg = msg_text[:300] + "..." if len(msg_text) > 300 else msg_text
            text += f"{icon}: {short_msg}\n\n"
        await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()

# ================= ОБЩЕНИЕ С ИИ (Gemini) =================
@dp.message(BotStates.chatting_with_ai, F.text)
async def ai_chat_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT text_requests FROM users WHERE user_id=?", (user_id,)) as cursor:
            requests = (await cursor.fetchone())[0]
            
        if requests >= LIMIT_TEXT:
            await message.answer("❌ Вы исчерпали лимит запросов.")
            return

    data = await state.get_data()
    chat_id = data.get("current_chat_id")
    
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL, # Используем модель из настроек
            contents=message.text,
        )
        ai_text = response.text
    except APIError as e:
        if "429" in str(e):
            await message.answer("⚠️ Слишком много запросов к ИИ (Лимит Google). Подождите минутку и попробуйте снова.")
        elif "404" in str(e):
            await message.answer(f"❌ Ошибка 404: Модель '{GEMINI_MODEL}' не найдена или недоступна для этого ключа. Поменяйте GEMINI_MODEL в файле .env")
        else:
            await message.answer(f"❌ Ошибка API Google: {e}")
        print(f"Ошибка Gemini API: {e}")
        return
    except Exception as e:
        await message.answer("❌ Произошла неизвестная ошибка при обращении к ИИ.")
        print(f"Системная ошибка: {e}")
        return

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO messages (chat_id, role, text) VALUES (?, ?, ?)", (chat_id, "user", message.text))
        await db.execute("INSERT INTO messages (chat_id, role, text) VALUES (?, ?, ?)", (chat_id, "ai", ai_text))
        await db.execute("UPDATE users SET text_requests = text_requests + 1 WHERE user_id=?", (user_id,))
        await db.commit()

    await message.answer(ai_text)


# ================= ЗАПУСК БОТА =================
async def main():
    await init_db()
    print(f"Бот успешно запущен! Используется модель: {GEMINI_MODEL}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())