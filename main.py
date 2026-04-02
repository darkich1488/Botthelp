import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from typing import Optional

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMINS_RAW = os.getenv("ADMINS", "").strip()  # example: "12345,67890"
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "support_bot.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not ADMINS_RAW:
    raise RuntimeError("ADMINS is missing. Example: ADMINS=12345,67890")

ADMINS = set()
for x in ADMINS_RAW.split(","):
    x = x.strip()
    if x.isdigit():
        ADMINS.add(int(x))

if not ADMINS:
    raise RuntimeError("ADMINS is empty or invalid")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("support_bot")

# =========================
# DB
# =========================

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    full_name TEXT,
    category TEXT NOT NULL,
    problem_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    admin_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS admin_sessions (
    admin_id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS user_sessions (
    user_id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL
)
""")

conn.commit()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def create_ticket(user_id: int, username: str, full_name: str, category: str, problem_text: str) -> int:
    now = now_str()
    cur.execute("""
        INSERT INTO tickets (user_id, username, full_name, category, problem_text, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'new', ?, ?)
    """, (user_id, username, full_name, category, problem_text, now, now))
    conn.commit()
    return cur.lastrowid


def get_ticket(ticket_id: int) -> Optional[sqlite3.Row]:
    cur.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
    return cur.fetchone()


def update_ticket_status(ticket_id: int, status: str, admin_id: Optional[int] = None):
    now = now_str()
    if admin_id is None:
        cur.execute("""
            UPDATE tickets
            SET status = ?, updated_at = ?
            WHERE id = ?
        """, (status, now, ticket_id))
    else:
        cur.execute("""
            UPDATE tickets
            SET status = ?, admin_id = ?, updated_at = ?
            WHERE id = ?
        """, (status, admin_id, now, ticket_id))
    conn.commit()


def assign_admin(ticket_id: int, admin_id: int):
    now = now_str()
    cur.execute("""
        UPDATE tickets
        SET admin_id = ?, status = 'in_progress', updated_at = ?
        WHERE id = ?
    """, (admin_id, now, ticket_id))
    conn.commit()


def set_admin_session(admin_id: int, ticket_id: int):
    cur.execute("""
        INSERT INTO admin_sessions (admin_id, ticket_id)
        VALUES (?, ?)
        ON CONFLICT(admin_id) DO UPDATE SET ticket_id = excluded.ticket_id
    """, (admin_id, ticket_id))
    conn.commit()


def get_admin_session(admin_id: int) -> Optional[int]:
    cur.execute("SELECT ticket_id FROM admin_sessions WHERE admin_id = ?", (admin_id,))
    row = cur.fetchone()
    return row["ticket_id"] if row else None


def clear_admin_session(admin_id: int):
    cur.execute("DELETE FROM admin_sessions WHERE admin_id = ?", (admin_id,))
    conn.commit()


def set_user_session(user_id: int, ticket_id: int):
    cur.execute("""
        INSERT INTO user_sessions (user_id, ticket_id)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET ticket_id = excluded.ticket_id
    """, (user_id, ticket_id))
    conn.commit()


def get_user_session(user_id: int) -> Optional[int]:
    cur.execute("SELECT ticket_id FROM user_sessions WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    return row["ticket_id"] if row else None


def clear_user_session(user_id: int):
    cur.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
    conn.commit()


def find_active_ticket_for_user(user_id: int) -> Optional[sqlite3.Row]:
    cur.execute("""
        SELECT * FROM tickets
        WHERE user_id = ? AND status = 'in_progress'
        ORDER BY id DESC
        LIMIT 1
    """, (user_id,))
    return cur.fetchone()


# =========================
# FSM
# =========================

class SupportForm(StatesGroup):
    choosing_category = State()
    waiting_problem = State()

# =========================
# TEXT / CATEGORIES / TEMPLATES
# =========================

CATEGORIES = {
    "stars": "Закупівля зірок",
    "numbers": "Віртуальні номери",
    "premium": "Premium",
    "login_premium": "Premium через вхід",
    "emoji": "Емодзі",
}

TEMPLATES = {
    "tpl_1": "Доброго дня. Ваша заявка прийнята в роботу. Очікуйте, будь ласка, відповідь від менеджера.",
    "tpl_2": "Щоб ми швидше допомогли, надішліть, будь ласка, скріншот проблеми або детальніший опис.",
    "tpl_3": "Ваше питання вже перевіряємо. Напишемо вам одразу після уточнення.",
    "tpl_4": "Питання вирішено. Перевірте, будь ласка, чи все зараз працює коректно.",
    "tpl_5": "На жаль, зараз не можемо виконати цей запит. Якщо хочете, опишіть проблему ще раз детальніше.",
}

# =========================
# KEYBOARDS
# =========================

def user_categories_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Закупівля зірок", callback_data="cat:stars")],
        [InlineKeyboardButton(text="📱 Віртуальні номери", callback_data="cat:numbers")],
        [InlineKeyboardButton(text="👑 Premium", callback_data="cat:premium")],
        [InlineKeyboardButton(text="🔐 Premium через вхід", callback_data="cat:login_premium")],
        [InlineKeyboardButton(text="😎 Емодзі", callback_data="cat:emoji")],
    ])


def admin_ticket_kb(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Виконано", callback_data=f"done:{ticket_id}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"reject:{ticket_id}")
        ],
        [
            InlineKeyboardButton(text="💬 В чат", callback_data=f"chat:{ticket_id}"),
            InlineKeyboardButton(text="📋 Шаблони", callback_data=f"templates:{ticket_id}")
        ]
    ])


def admin_chat_kb(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Шаблони", callback_data=f"templates:{ticket_id}"),
            InlineKeyboardButton(text="🛑 Вийти з чату", callback_data=f"closechat:{ticket_id}")
        ],
        [
            InlineKeyboardButton(text="✅ Виконано", callback_data=f"done:{ticket_id}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"reject:{ticket_id}")
        ]
    ])


def templates_kb(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1. Заявка прийнята", callback_data=f"sendtpl:{ticket_id}:tpl_1")],
        [InlineKeyboardButton(text="2. Потрібен скрін/деталі", callback_data=f"sendtpl:{ticket_id}:tpl_2")],
        [InlineKeyboardButton(text="3. Перевіряємо", callback_data=f"sendtpl:{ticket_id}:tpl_3")],
        [InlineKeyboardButton(text="4. Питання вирішено", callback_data=f"sendtpl:{ticket_id}:tpl_4")],
        [InlineKeyboardButton(text="5. Не можемо виконати", callback_data=f"sendtpl:{ticket_id}:tpl_5")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"back_to_ticket:{ticket_id}")],
    ])


# =========================
# HELPERS
# =========================

def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


def ticket_card_text(ticket: sqlite3.Row) -> str:
    username = ticket["username"] or "-"
    if username != "-" and not username.startswith("@"):
        username = f"@{username}"

    admin_text = str(ticket["admin_id"]) if ticket["admin_id"] else "ще не призначено"

    return (
        f"🆕 <b>Заявка #{ticket['id']}</b>\n\n"
        f"👤 <b>Ім'я:</b> {ticket['full_name']}\n"
        f"🆔 <b>User ID:</b> <code>{ticket['user_id']}</code>\n"
        f"📛 <b>Username:</b> {username}\n"
        f"📂 <b>Категорія:</b> {ticket['category']}\n"
        f"📌 <b>Статус:</b> {ticket['status']}\n"
        f"👮 <b>Адмін:</b> {admin_text}\n"
        f"🕒 <b>Створено:</b> {ticket['created_at']}\n\n"
        f"📝 <b>Проблема:</b>\n{ticket['problem_text']}"
    )


async def notify_admins(bot: Bot, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    for admin_id in ADMINS:
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception as e:
            logger.warning("Failed to notify admin %s: %s", admin_id, e)


# =========================
# ROUTERS
# =========================

router = Router()


@router.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    text = (
        "Привіт.\n\n"
        "Це бот підтримки.\n"
        "Оберіть тип проблеми нижче."
    )
    await message.answer(text, reply_markup=user_categories_kb())
    await state.set_state(SupportForm.choosing_category)


@router.message(Command("support"))
async def support_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Оберіть тип проблеми:", reply_markup=user_categories_kb())
    await state.set_state(SupportForm.choosing_category)


@router.callback_query(F.data.startswith("cat:"))
async def choose_category(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    category = CATEGORIES.get(key)
    if not category:
        await callback.answer("Невідома категорія", show_alert=True)
        return

    await state.update_data(category=category)
    await state.set_state(SupportForm.waiting_problem)

    await callback.message.edit_text(
        f"📂 Обрано: <b>{category}</b>\n\n"
        f"Тепер опишіть проблему одним повідомленням."
    )
    await callback.answer()


@router.message(SupportForm.waiting_problem)
async def receive_problem(message: Message, state: FSMContext, bot: Bot):
    if not message.text:
        await message.answer("Будь ласка, надішли проблему текстом.")
        return

    data = await state.get_data()
    category = data.get("category", "Інше")

    ticket_id = create_ticket(
        user_id=message.from_user.id,
        username=message.from_user.username or "-",
        full_name=message.from_user.full_name,
        category=category,
        problem_text=message.text.strip(),
    )

    ticket = get_ticket(ticket_id)
    set_user_session(message.from_user.id, ticket_id)

    await message.answer(
        f"✅ Вашу заявку <b>#{ticket_id}</b> створено.\n"
        f"📂 Категорія: <b>{category}</b>\n\n"
        f"Очікуйте відповіді від підтримки."
    )

    await notify_admins(
        bot,
        ticket_card_text(ticket),
        reply_markup=admin_ticket_kb(ticket_id)
    )

    await state.clear()


@router.callback_query(F.data.startswith("chat:"))
async def open_chat(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return

    ticket_id = int(callback.data.split(":")[1])
    ticket = get_ticket(ticket_id)
    if not ticket:
        await callback.answer("Заявку не знайдено", show_alert=True)
        return

    assign_admin(ticket_id, callback.from_user.id)
    set_admin_session(callback.from_user.id, ticket_id)
    set_user_session(ticket["user_id"], ticket_id)

    await callback.message.answer(
        f"💬 Ви увійшли в чат по заявці #{ticket_id}\n"
        f"Тепер просто надсилайте повідомлення користувачу.",
        reply_markup=admin_chat_kb(ticket_id)
    )
    await callback.bot.send_message(
        ticket["user_id"],
        f"👮 Підтримка взяла вашу заявку <b>#{ticket_id}</b> в роботу.\n"
        f"Тепер ви можете писати повідомлення сюди."
    )
    await callback.answer("Чат відкрито")


@router.callback_query(F.data.startswith("done:"))
async def done_ticket(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return

    ticket_id = int(callback.data.split(":")[1])
    ticket = get_ticket(ticket_id)
    if not ticket:
        await callback.answer("Заявку не знайдено", show_alert=True)
        return

    update_ticket_status(ticket_id, "done", callback.from_user.id)
    clear_admin_session(callback.from_user.id)
    clear_user_session(ticket["user_id"])

    await callback.bot.send_message(
        ticket["user_id"],
        f"✅ Вашу заявку <b>#{ticket_id}</b> виконано."
    )
    await callback.message.answer(f"Заявку #{ticket_id} позначено як виконану.")
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("reject:"))
async def reject_ticket(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return

    ticket_id = int(callback.data.split(":")[1])
    ticket = get_ticket(ticket_id)
    if not ticket:
        await callback.answer("Заявку не знайдено", show_alert=True)
        return

    update_ticket_status(ticket_id, "rejected", callback.from_user.id)
    clear_admin_session(callback.from_user.id)
    clear_user_session(ticket["user_id"])

    await callback.bot.send_message(
        ticket["user_id"],
        f"❌ Вашу заявку <b>#{ticket_id}</b> відхилено."
    )
    await callback.message.answer(f"Заявку #{ticket_id} відхилено.")
    await callback.answer("Відхилено")


@router.callback_query(F.data.startswith("templates:"))
async def show_templates(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return

    ticket_id = int(callback.data.split(":")[1])
    await callback.message.answer(
        f"📋 Шаблонні відповіді для заявки #{ticket_id}:",
        reply_markup=templates_kb(ticket_id)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sendtpl:"))
async def send_template(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return

    _, ticket_id_str, template_key = callback.data.split(":")
    ticket_id = int(ticket_id_str)

    ticket = get_ticket(ticket_id)
    if not ticket:
        await callback.answer("Заявку не знайдено", show_alert=True)
        return

    text = TEMPLATES.get(template_key)
    if not text:
        await callback.answer("Шаблон не знайдено", show_alert=True)
        return

    assign_admin(ticket_id, callback.from_user.id)
    set_admin_session(callback.from_user.id, ticket_id)
    set_user_session(ticket["user_id"], ticket_id)

    await callback.bot.send_message(
        ticket["user_id"],
        f"💬 <b>Підтримка:</b>\n{text}"
    )
    await callback.message.answer("Шаблон відправлено користувачу ✅")
    await callback.answer("Надіслано")


@router.callback_query(F.data.startswith("back_to_ticket:"))
async def back_to_ticket(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return

    ticket_id = int(callback.data.split(":")[1])
    ticket = get_ticket(ticket_id)
    if not ticket:
        await callback.answer("Заявку не знайдено", show_alert=True)
        return

    await callback.message.answer(
        ticket_card_text(ticket),
        reply_markup=admin_ticket_kb(ticket_id)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("closechat:"))
async def close_chat(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Немає доступу", show_alert=True)
        return

    ticket_id = int(callback.data.split(":")[1])
    ticket = get_ticket(ticket_id)
    if not ticket:
        await callback.answer("Заявку не знайдено", show_alert=True)
        return

    clear_admin_session(callback.from_user.id)

    await callback.message.answer(f"🛑 Чат по заявці #{ticket_id} закрито для адміна.")
    await callback.answer("Чат закрито")


# Адмін пише користувачу
@router.message(F.text)
async def relay_messages(message: Message):
    # 1) якщо це адмін і в нього активний чат
    if is_admin(message.from_user.id):
        ticket_id = get_admin_session(message.from_user.id)
        if ticket_id:
            ticket = get_ticket(ticket_id)
            if not ticket:
                await message.answer("Заявку не знайдено.")
                return

            if ticket["status"] in ("done", "rejected"):
                await message.answer("Ця заявка вже закрита.")
                return

            assign_admin(ticket_id, message.from_user.id)
            await message.bot.send_message(
                ticket["user_id"],
                f"💬 <b>Підтримка:</b>\n{message.text}"
            )
            await message.answer("Відправлено користувачу ✅")
            return

    # 2) якщо це користувач і в нього є активна заявка
    active_ticket = find_active_ticket_for_user(message.from_user.id)
    if active_ticket:
        admin_id = active_ticket["admin_id"]
        if admin_id:
            username = message.from_user.username or "-"
            if username != "-" and not username.startswith("@"):
                username = f"@{username}"

            await message.bot.send_message(
                admin_id,
                (
                    f"📩 <b>Повідомлення від користувача</b>\n"
                    f"Заявка: <b>#{active_ticket['id']}</b>\n"
                    f"👤 {message.from_user.full_name} | {username}\n"
                    f"🆔 <code>{message.from_user.id}</code>\n\n"
                    f"{message.text}"
                ),
                reply_markup=admin_chat_kb(active_ticket["id"])
            )
            await message.answer("Повідомлення передано в підтримку ✅")
            return

    # 3) fallback
    await message.answer(
        "Щоб створити заявку, натисни /support"
    )


# =========================
# HEALTH SERVER FOR RAILWAY
# =========================

async def health_handler(request):
    return web.Response(text="OK")


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("Health server started on port %s", PORT)


# =========================
# MAIN
# =========================

async def main():
    await start_health_server()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Bot started")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try: 
      asyncio.run(main())
    finally:
        conn.close()
  
