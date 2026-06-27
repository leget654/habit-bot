import asyncio
import logging
import calendar
from datetime import datetime, date, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    WebAppInfo, LabeledPrice, PreCheckoutQuery
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import aiosqlite
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "habits.db"
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ── Constants ─────────────────────────────────────────────────────────────────

ACHIEVEMENTS = [
    {"id": "streak_3",   "streak": 3,   "icon": "🥉", "title": "3 дня подряд",    "desc": "Хорошее начало!"},
    {"id": "streak_7",   "streak": 7,   "icon": "🥈", "title": "Неделя подряд",   "desc": "Привычка формируется!"},
    {"id": "streak_14",  "streak": 14,  "icon": "🥇", "title": "2 недели подряд", "desc": "Ты на верном пути!"},
    {"id": "streak_30",  "streak": 30,  "icon": "🏆", "title": "Месяц подряд",    "desc": "Настоящая привычка!"},
    {"id": "streak_60",  "streak": 60,  "icon": "💎", "title": "60 дней подряд",  "desc": "Легенда!"},
    {"id": "streak_100", "streak": 100, "icon": "🚀", "title": "100 дней подряд", "desc": "Невероятно!"},
]

LEVELS = [
    (0,    "🌱 Новичок"),
    (100,  "⚡ Ученик"),
    (300,  "🔥 Практик"),
    (700,  "💪 Мастер"),
    (1500, "🏆 Чемпион"),
    (3000, "💎 Легенда"),
    (6000, "🚀 Бог привычек"),
]

STREAK_SERIES = [
    (3,   "🔥 Серия 3 дня!",    "Разогреваешься!"),
    (7,   "🔥🔥 Серия неделя!", "Ты в потоке!"),
    (14,  "🔥🔥🔥 2 недели!",   "Машина привычек!"),
    (30,  "💥 МЕСЯЦ подряд!",   "Ты легенда!"),
    (100, "🚀 100 ДНЕЙ!",       "Просто невероятно!"),
]

XP_PER_HABIT = 10
XP_STREAK_BONUS = 5  # per streak day


def get_level(xp: int) -> tuple:
    level_num = 0
    level_name = LEVELS[0][1]
    for i, (req, name) in enumerate(LEVELS):
        if xp >= req:
            level_num = i + 1
            level_name = name
    next_xp = None
    for req, name in LEVELS:
        if req > xp:
            next_xp = req
            break
    return level_num, level_name, next_xp


# ── FSM States ────────────────────────────────────────────────────────────────

class AddHabit(StatesGroup):
    waiting_name = State()
    waiting_emoji = State()
    waiting_frequency = State()
    waiting_specific_days = State()
    waiting_times_per_week = State()
    waiting_time = State()
    waiting_goal = State()

class SetGoal(StatesGroup):
    waiting_days = State()

class RenameHabit(StatesGroup):
    waiting_new_name = State()

class AddNote(StatesGroup):
    waiting_note = State()

class SetUsername(StatesGroup):
    waiting_name = State()


# ── Database ──────────────────────────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                display_name TEXT,
                total_xp INTEGER DEFAULT 0,
                created_at DATE DEFAULT CURRENT_DATE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS habits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                emoji TEXT DEFAULT '✅',
                remind_time TEXT DEFAULT NULL,
                monthly_goal INTEGER DEFAULT NULL,
                is_paused INTEGER DEFAULT 0,
                created_at DATE DEFAULT CURRENT_DATE,
                is_active INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS completions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                habit_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                completed_date DATE NOT NULL,
                note TEXT DEFAULT NULL,
                UNIQUE(habit_id, completed_date)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                habit_id INTEGER NOT NULL,
                achievement_id TEXT NOT NULL,
                earned_at DATE DEFAULT CURRENT_DATE,
                UNIQUE(habit_id, achievement_id)
            )
        """)
        # Migrations for old databases
        migrations = [
            ("habits", "monthly_goal", "INTEGER DEFAULT NULL"),
            ("habits", "is_paused", "INTEGER DEFAULT 0"),
            ("completions", "note", "TEXT DEFAULT NULL"),
            ("users", "total_xp", "INTEGER DEFAULT 0"),
            ("users", "display_name", "TEXT"),
            ("users", "trial_started_at", "DATE DEFAULT NULL"),
            ("users", "premium_until", "DATE DEFAULT NULL"),
            ("habits", "frequency_type", "TEXT DEFAULT 'daily'"),
            ("habits", "frequency_data", "TEXT DEFAULT NULL"),
        ]
        for table, col, definition in migrations:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
                await db.commit()
            except Exception:
                pass
        await db.commit()


# ── Subscription ──────────────────────────────────────────────────────────────

FREE_HABIT_LIMIT = 3
TRIAL_DAYS = 3
SUBSCRIPTION_STARS_PRICE = 99  # Telegram Stars, ~ a couple dollars
SUBSCRIPTION_DAYS = 30


async def get_subscription_status(user_id: int) -> dict:
    """Returns dict with: is_premium, is_trial, trial_days_left, premium_until"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT trial_started_at, premium_until FROM users WHERE user_id=?",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        return {"is_premium": False, "is_trial": False, "trial_days_left": 0, "premium_until": None}

    today = date.today()

    # Active paid subscription
    if row["premium_until"]:
        premium_until = date.fromisoformat(row["premium_until"])
        if premium_until >= today:
            return {"is_premium": True, "is_trial": False, "trial_days_left": 0, "premium_until": premium_until}

    # Trial period
    if row["trial_started_at"]:
        trial_start = date.fromisoformat(row["trial_started_at"])
        trial_end = trial_start + timedelta(days=TRIAL_DAYS - 1)  # inclusive end day
        days_left = (trial_end - today).days
        if days_left >= 0:
            return {"is_premium": True, "is_trial": True, "trial_days_left": days_left + 1, "premium_until": None}

    return {"is_premium": False, "is_trial": False, "trial_days_left": 0, "premium_until": None}


async def start_trial_if_new(user_id: int):
    """Starts the 3-day trial the first time a user is seen, if they never had one."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT trial_started_at, premium_until FROM users WHERE user_id=?",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if row and row[0] is None and row[1] is None:
            await db.execute(
                "UPDATE users SET trial_started_at=? WHERE user_id=?",
                (date.today().isoformat(), user_id)
            )
            await db.commit()


async def grant_subscription(user_id: int, days: int = SUBSCRIPTION_DAYS):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT premium_until FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
        today = date.today()
        current_until = date.fromisoformat(row[0]) if row and row[0] else today
        base = max(current_until, today)
        new_until = base + timedelta(days=days)
        await db.execute(
            "UPDATE users SET premium_until=? WHERE user_id=?",
            (new_until.isoformat(), user_id)
        )
        await db.commit()
        return new_until


async def can_add_habit(user_id: int) -> bool:
    status = await get_subscription_status(user_id)
    if status["is_premium"]:
        return True
    habits = await get_habits(user_id, include_paused=True)
    return len(habits) < FREE_HABIT_LIMIT


async def ensure_user(user_id: int, first_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, display_name) VALUES (?, ?)",
            (user_id, first_name)
        )
        await db.commit()
    await start_trial_if_new(user_id)


async def user_exists(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)) as cur:
            return (await cur.fetchone()) is not None


async def add_xp(user_id: int, amount: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET total_xp = total_xp + ? WHERE user_id = ?",
            (amount, user_id)
        )
        await db.commit()
        async with db.execute("SELECT total_xp FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_user_xp(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT total_xp FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_leaderboard(limit: int = 10) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, display_name, total_xp FROM users ORDER BY total_xp DESC LIMIT ?",
            (limit,)
        ) as cur:
            return await cur.fetchall()


async def get_user_rank(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE total_xp > (SELECT total_xp FROM users WHERE user_id=?)",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return (row[0] + 1) if row else 1


async def get_habits(user_id: int, include_paused: bool = False) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if include_paused:
            sql = "SELECT * FROM habits WHERE user_id=? AND is_active=1 ORDER BY id"
        else:
            sql = "SELECT * FROM habits WHERE user_id=? AND is_active=1 AND is_paused=0 ORDER BY id"
        async with db.execute(sql, (user_id,)) as cur:
            return await cur.fetchall()


async def create_habit(user_id: int, name: str, emoji: str = "✅", remind_time=None, monthly_goal=None,
                        frequency_type: str = "daily", frequency_data=None):
    """Habit creation used by the Mini App API and bot FSM.
    frequency_type: 'daily' | 'times_per_week' | 'specific_days'
    frequency_data: for 'times_per_week' -> str(int) e.g. "3"
                     for 'specific_days' -> comma-separated weekday indices "0,2,4" (Mon=0)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO habits (user_id, name, emoji, remind_time, monthly_goal, frequency_type, frequency_data) "
            "VALUES (?,?,?,?,?,?,?)",
            (user_id, name, emoji, remind_time, monthly_goal, frequency_type, frequency_data)
        )
        await db.commit()


def is_due_today(habit, today: date = None) -> bool:
    """Whether a habit is scheduled for today based on its frequency."""
    today = today or date.today()
    freq_type = habit["frequency_type"] if habit["frequency_type"] else "daily"
    if freq_type == "daily":
        return True
    if freq_type == "specific_days":
        if not habit["frequency_data"]:
            return True
        days = {int(d) for d in habit["frequency_data"].split(",") if d != ""}
        return today.weekday() in days
    if freq_type == "times_per_week":
        # "due" every day shown, but goal is N times that week - always show as available
        return True
    return True


def frequency_label(habit) -> str:
    freq_type = habit["frequency_type"] if habit["frequency_type"] else "daily"
    if freq_type == "daily":
        return "каждый день"
    if freq_type == "times_per_week":
        n = habit["frequency_data"] or "?"
        return f"{n} раз{'а' if n not in ('1',) else ''} в неделю"
    if freq_type == "specific_days":
        names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        if not habit["frequency_data"]:
            return "по дням"
        days = sorted(int(d) for d in habit["frequency_data"].split(",") if d != "")
        return ", ".join(names[d] for d in days)
    return "каждый день"


async def rename_habit(habit_id: int, new_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE habits SET name=? WHERE id=?", (new_name, habit_id))
        await db.commit()


async def set_habit_paused(habit_id: int, paused: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE habits SET is_paused=? WHERE id=?", (1 if paused else 0, habit_id))
        await db.commit()


async def delete_habit(habit_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE habits SET is_active=0 WHERE id=?", (habit_id,))
        await db.commit()


async def set_display_name(user_id: int, name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET display_name=? WHERE user_id=?", (name, user_id))
        await db.commit()


async def get_today_completions(user_id: int) -> set:
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT habit_id FROM completions WHERE user_id=? AND completed_date=?",
            (user_id, today)
        ) as cur:
            return {row[0] for row in await cur.fetchall()}


async def toggle_completion(user_id: int, habit_id: int) -> bool:
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM completions WHERE habit_id=? AND completed_date=?",
            (habit_id, today)
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            await db.execute("DELETE FROM completions WHERE habit_id=? AND completed_date=?", (habit_id, today))
            await db.commit()
            return False
        else:
            await db.execute(
                "INSERT OR IGNORE INTO completions (habit_id, user_id, completed_date) VALUES (?,?,?)",
                (habit_id, user_id, today)
            )
            await db.commit()
            return True


async def get_streak(habit_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT completed_date FROM completions WHERE habit_id=? ORDER BY completed_date DESC",
            (habit_id,)
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return 0
    dates = sorted([date.fromisoformat(r[0]) for r in rows], reverse=True)
    streak = 0
    check = date.today()
    for d in dates:
        if d == check:
            streak += 1
            check -= timedelta(days=1)
        elif d == check + timedelta(days=1):
            streak += 1
            check = d - timedelta(days=1)
        else:
            break
    return streak


async def get_best_streak(habit_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT completed_date FROM completions WHERE habit_id=? ORDER BY completed_date ASC",
            (habit_id,)
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return 0
    dates = sorted([date.fromisoformat(r[0]) for r in rows])
    best = 1
    current = 1
    for i in range(1, len(dates)):
        if (dates[i] - dates[i-1]).days == 1:
            current += 1
            best = max(best, current)
        else:
            current = 1
    return best


async def get_monthly_stats(habit_id: int) -> dict:
    today = date.today()
    first_day = today.replace(day=1)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT completed_date FROM completions WHERE habit_id=? AND completed_date >= ?",
            (habit_id, first_day.isoformat())
        ) as cur:
            rows = await cur.fetchall()
    completed_days = {r[0] for r in rows}
    days_passed = today.day
    percent = round(len(completed_days) / days_passed * 100) if days_passed else 0
    return {"completed": len(completed_days), "total": days_passed, "percent": percent, "dates": completed_days}


async def get_week_completions(habit_id: int) -> set:
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT completed_date FROM completions WHERE habit_id=? AND completed_date >= ?",
            (habit_id, week_start.isoformat())
        ) as cur:
            return {r[0] for r in await cur.fetchall()}


async def check_and_grant_achievements(user_id: int, habit_id: int, streak: int) -> list:
    new_achievements = []
    async with aiosqlite.connect(DB_PATH) as db:
        for ach in ACHIEVEMENTS:
            if streak >= ach["streak"]:
                try:
                    await db.execute(
                        "INSERT INTO achievements (user_id, habit_id, achievement_id) VALUES (?,?,?)",
                        (user_id, habit_id, ach["id"])
                    )
                    await db.commit()
                    new_achievements.append(ach)
                except Exception:
                    pass
    return new_achievements


async def get_user_achievements(user_id: int) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT a.achievement_id, a.habit_id, a.earned_at, h.name, h.emoji
               FROM achievements a JOIN habits h ON a.habit_id = h.id
               WHERE a.user_id=? ORDER BY a.earned_at DESC""",
            (user_id,)
        ) as cur:
            return await cur.fetchall()


# ── Keyboards ─────────────────────────────────────────────────────────────────

def main_reply_kb() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📋 Привычки"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="🏆 Рейтинг"), KeyboardButton(text="👤 Мой профиль")],
        [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="⚙️ Управление")],
        [KeyboardButton(text="✨ Premium")],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои привычки", callback_data="show_today")],
        [InlineKeyboardButton(text="➕ Добавить привычку", callback_data="add_habit")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="show_stats")],
        [InlineKeyboardButton(text="📅 История за неделю", callback_data="show_week")],
        [InlineKeyboardButton(text="🏆 Рейтинг", callback_data="show_leaderboard")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="show_profile")],
        [InlineKeyboardButton(text="✨ Premium", callback_data="show_premium")],
        [InlineKeyboardButton(text="⚙️ Управление", callback_data="show_manage")],
    ])


def manage_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Переименовать", callback_data="rename_list")],
        [InlineKeyboardButton(text="⏸ Поставить на паузу", callback_data="pause_list")],
        [InlineKeyboardButton(text="▶️ Снять с паузы", callback_data="unpause_list")],
        [InlineKeyboardButton(text="🎯 Цели на месяц", callback_data="show_goals")],
        [InlineKeyboardButton(text="🗑 Удалить привычку", callback_data="delete_list")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu")],
    ])


async def today_kb(user_id: int) -> InlineKeyboardMarkup:
    habits = await get_habits(user_id)
    done = await get_today_completions(user_id)
    buttons = []
    for h in habits:
        status = "✅" if h["id"] in done else "⬜"
        due_mark = "" if is_due_today(h) else " 💤"
        buttons.append([InlineKeyboardButton(
            text=f"{status} {h['emoji']} {h['name']}{due_mark}",
            callback_data=f"toggle_{h['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def habit_select_kb(user_id: int, prefix: str, back: str = "show_manage", include_paused: bool = False) -> InlineKeyboardMarkup:
    habits = await get_habits(user_id, include_paused=include_paused)
    buttons = [[InlineKeyboardButton(
        text=f"{h['emoji']} {h['name']}" + (" ⏸" if h["is_paused"] else ""),
        callback_data=f"{prefix}{h['id']}"
    )] for h in habits]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def goals_kb(user_id: int) -> InlineKeyboardMarkup:
    habits = await get_habits(user_id, include_paused=True)
    buttons = []
    for h in habits:
        goal_text = f"({h['monthly_goal']} дн.)" if h["monthly_goal"] else "(нет цели)"
        buttons.append([InlineKeyboardButton(
            text=f"{h['emoji']} {h['name']} {goal_text}",
            callback_data=f"setgoal_{h['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="show_manage")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Helpers ───────────────────────────────────────────────────────────────────

def progress_bar(percent: int, length: int = 10) -> str:
    filled = round(percent / 100 * length)
    return "█" * filled + "░" * (length - filled)


def goal_progress_bar(completed: int, goal: int, length: int = 10) -> str:
    pct = min(completed / goal, 1.0) if goal else 0
    filled = round(pct * length)
    return "█" * filled + "░" * (length - filled)


def xp_bar(xp: int, next_xp: int | None, length: int = 10) -> str:
    if next_xp is None:
        return "█" * length + " MAX"
    prev_xp = 0
    for req, _ in LEVELS:
        if req <= xp:
            prev_xp = req
    span = next_xp - prev_xp
    done = xp - prev_xp
    pct = done / span if span else 1
    filled = round(pct * length)
    return "█" * filled + "░" * (length - filled)


def calendar_grid(dates: set, year: int, month: int) -> str:
    cal = calendar.monthcalendar(year, month)
    lines = ["Пн Вт Ср Чт Пт Сб Вс"]
    for week in cal:
        row = []
        for day in week:
            if day == 0:
                row.append("  ")
            else:
                d = date(year, month, day).isoformat()
                row.append("✅" if d in dates else f"{day:2d}")
        lines.append(" ".join(row))
    return "\n".join(lines)


def rank_medal(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}.")


# ── Handlers: start & menu ────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(msg: Message):
    is_new = not await user_exists(msg.from_user.id)
    await ensure_user(msg.from_user.id, msg.from_user.first_name)

    if is_new:
        await msg.answer(
            f"Привет, {msg.from_user.first_name}! 👋\n\n"
            "Отслеживай привычки, зарабатывай опыт ⚡ и поднимайся в рейтинге 🏆\n\n"
            f"🎁 Тебе доступен бесплатный <b>пробный период {TRIAL_DAYS} дня</b> — "
            "все функции открыты без ограничений!\n\n"
            "Используй кнопки внизу 👇",
            parse_mode="HTML",
            reply_markup=main_reply_kb()
        )
    else:
        await msg.answer(
            f"Привет, {msg.from_user.first_name}! 👋\n\n"
            "Отслеживай привычки, зарабатывай опыт ⚡ и поднимайся в рейтинге 🏆\n\n"
            "Используй кнопки внизу 👇",
            reply_markup=main_reply_kb()
        )

@dp.message(Command("menu"))
async def cmd_menu_msg(msg: Message):
    await msg.answer("Меню:", reply_markup=main_menu_kb())

@dp.message(Command("reset"))
async def cmd_reset(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("✅ Готово! Можешь пользоваться ботом.", reply_markup=main_reply_kb())

@dp.message(Command("premium"))
async def cmd_premium(msg: Message):
    await ensure_user(msg.from_user.id, msg.from_user.first_name)
    await send_premium_screen(msg.from_user.id, msg)

@dp.callback_query(F.data == "show_premium")
async def cb_show_premium(cb: CallbackQuery):
    await send_premium_screen(cb.from_user.id, cb.message)


async def send_premium_screen(user_id: int, target):
    status = await get_subscription_status(user_id)

    if status["is_premium"] and not status["is_trial"]:
        text = (
            f"✨ <b>У тебя активна подписка</b>\n\n"
            f"Действует до: {status['premium_until'].strftime('%d.%m.%Y')}\n\n"
            f"Безлимитные привычки, рейтинг, мини-приложение — всё открыто."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Продлить ещё на 30 дней", callback_data="buy_premium")],
        ])
    elif status["is_trial"]:
        text = (
            f"🎁 <b>Пробный период активен</b>\n\n"
            f"Осталось дней: {status['trial_days_left']}\n\n"
            f"Сейчас доступны все функции бесплатно. После окончания пробного периода "
            f"бесплатно останется {FREE_HABIT_LIMIT} привычки — для безлимита оформи подписку."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✨ Подписка — {SUBSCRIPTION_STARS_PRICE}⭐/мес", callback_data="buy_premium")],
        ])
    else:
        text = (
            f"✨ <b>Premium подписка</b>\n\n"
            f"• Безлимитные привычки (сейчас доступно {FREE_HABIT_LIMIT})\n"
            f"• Рейтинг и достижения\n"
            f"• Мини-приложение с красивыми карточками\n"
            f"• Расширенная статистика\n\n"
            f"Цена: <b>{SUBSCRIPTION_STARS_PRICE} ⭐ Stars</b> за 30 дней"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✨ Оформить за {SUBSCRIPTION_STARS_PRICE}⭐", callback_data="buy_premium")],
        ])

    if hasattr(target, 'message_id'):
        await target.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "buy_premium")
async def cb_buy_premium(cb: CallbackQuery):
    await bot.send_invoice(
        chat_id=cb.from_user.id,
        title="Premium подписка — 30 дней",
        description="Безлимитные привычки, рейтинг, достижения и мини-приложение",
        payload=f"premium_30d_{cb.from_user.id}",
        currency="XTR",
        prices=[LabeledPrice(label="Premium 30 дней", amount=SUBSCRIPTION_STARS_PRICE)],
    )
    await cb.answer()


@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_q.id, ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(msg: Message):
    new_until = await grant_subscription(msg.from_user.id, SUBSCRIPTION_DAYS)
    await msg.answer(
        f"🎉 <b>Спасибо за подписку!</b>\n\n"
        f"Premium активен до: {new_until.strftime('%d.%m.%Y')}\n\n"
        f"Теперь у тебя безлимитные привычки и доступ ко всем функциям!",
        parse_mode="HTML",
        reply_markup=main_reply_kb()
    )

@dp.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery):
    await cb.message.edit_text("Главное меню:", reply_markup=main_menu_kb())

@dp.callback_query(F.data == "show_manage")
async def cb_show_manage(cb: CallbackQuery):
    await cb.message.edit_text("⚙️ <b>Управление</b>", reply_markup=manage_kb(), parse_mode="HTML")


# ── Reply keyboard routing ────────────────────────────────────────────────────

@dp.message(F.text == "📋 Привычки")
async def reply_habits(msg: Message):
    await ensure_user(msg.from_user.id, msg.from_user.first_name)
    habits = await get_habits(msg.from_user.id)
    if not habits:
        await msg.answer("Нет привычек. Нажми ➕ Добавить!", reply_markup=main_reply_kb())
        return
    today_str = date.today().strftime("%d %B %Y")
    await msg.answer(
        f"📋 <b>{today_str}</b>\n\nОтметь выполненные:",
        reply_markup=await today_kb(msg.from_user.id),
        parse_mode="HTML"
    )

@dp.message(F.text == "📊 Статистика")
async def reply_stats(msg: Message):
    await ensure_user(msg.from_user.id, msg.from_user.first_name)
    await _send_stats(msg.from_user.id, msg)

@dp.message(F.text == "🏆 Рейтинг")
async def reply_leaderboard(msg: Message):
    await ensure_user(msg.from_user.id, msg.from_user.first_name)
    await _send_leaderboard(msg.from_user.id, msg)

@dp.message(F.text == "👤 Мой профиль")
async def reply_profile(msg: Message):
    await ensure_user(msg.from_user.id, msg.from_user.first_name)
    await _send_profile(msg.from_user.id, msg)

@dp.message(F.text == "➕ Добавить")
async def reply_add(msg: Message, state: FSMContext):
    await ensure_user(msg.from_user.id, msg.from_user.first_name)
    if not await can_add_habit(msg.from_user.id):
        await send_limit_reached(msg.from_user.id, msg)
        return
    await state.set_state(AddHabit.waiting_name)
    await msg.answer(
        "➕ <b>Новая привычка</b>\n\nКак называется?\n<i>Например: Зарядка, Читать, Пить воду</i>",
        parse_mode="HTML"
    )


async def send_limit_reached(user_id: int, target):
    text = (
        f"🔒 <b>Достигнут лимит привычек</b>\n\n"
        f"На бесплатном тарифе доступно до {FREE_HABIT_LIMIT} привычек.\n"
        f"Оформи Premium подписку для безлимита!"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Узнать про Premium", callback_data="show_premium")],
    ])
    if hasattr(target, 'message_id'):
        await target.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")

@dp.message(F.text == "⚙️ Управление")
async def reply_manage(msg: Message):
    await msg.answer("⚙️ <b>Управление</b>", reply_markup=manage_kb(), parse_mode="HTML")

@dp.message(F.text == "✨ Premium")
async def reply_premium(msg: Message):
    await ensure_user(msg.from_user.id, msg.from_user.first_name)
    await send_premium_screen(msg.from_user.id, msg)


# ── Today / toggle ────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "show_today")
async def cb_show_today(cb: CallbackQuery):
    await ensure_user(cb.from_user.id, cb.from_user.first_name)
    habits = await get_habits(cb.from_user.id)
    if not habits:
        await cb.answer("Сначала добавь привычки!", show_alert=True)
        return
    today_str = date.today().strftime("%d %B %Y")
    await cb.message.edit_text(
        f"📋 <b>{today_str}</b>\n\nОтметь выполненные:",
        reply_markup=await today_kb(cb.from_user.id),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("toggle_"))
async def cb_toggle(cb: CallbackQuery, state: FSMContext):
    habit_id = int(cb.data.split("_")[1])
    is_done = await toggle_completion(cb.from_user.id, habit_id)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT name, emoji FROM habits WHERE id=?", (habit_id,)) as cur:
            habit = await cur.fetchone()

    if is_done:
        streak = await get_streak(habit_id)
        # XP
        xp_earned = XP_PER_HABIT + streak * XP_STREAK_BONUS
        old_xp = await get_user_xp(cb.from_user.id)
        new_xp = await add_xp(cb.from_user.id, xp_earned)
        old_level = get_level(old_xp)[0]
        new_level_num, new_level_name, next_xp = get_level(new_xp)

        # Achievements
        new_achs = await check_and_grant_achievements(cb.from_user.id, habit_id, streak)

        streak_text = f" 🔥{streak}" if streak > 1 else ""
        await cb.answer(f"✅ +{xp_earned}⚡{streak_text}", show_alert=False)

        # Level up notification
        if new_level_num > old_level:
            await bot.send_message(
                cb.from_user.id,
                f"🎉 <b>Новый уровень!</b>\n\n{new_level_name}\n\nПродолжай в том же духе!",
                parse_mode="HTML"
            )

        # Achievement notifications
        for ach in new_achs:
            await bot.send_message(
                cb.from_user.id,
                f"🏅 <b>Новое достижение!</b>\n\n{ach['icon']} <b>{ach['title']}</b>\n"
                f"{habit['emoji']} {habit['name']}\n\n<i>{ach['desc']}</i>",
                parse_mode="HTML"
            )

        # Streak series notification
        for days, title, subtitle in STREAK_SERIES:
            if streak == days:
                await bot.send_message(
                    cb.from_user.id,
                    f"{title}\n{habit['emoji']} {habit['name']}\n\n<i>{subtitle}</i>",
                    parse_mode="HTML"
                )

        # Ask for note
        await state.set_state(AddNote.waiting_note)
        await state.update_data(habit_id=habit_id)
        await bot.send_message(
            cb.from_user.id,
            f"📝 Заметка к <b>{habit['emoji']} {habit['name']}</b>? (или /skip)",
            parse_mode="HTML"
        )
    else:
        await cb.answer("⬜ Отметка снята", show_alert=False)

    done = await get_today_completions(cb.from_user.id)
    habits = await get_habits(cb.from_user.id)
    today_str = date.today().strftime("%d %B %Y")
    all_done = len(done) == len(habits) and len(habits) > 0
    header = f"🎉 <b>{today_str}</b>\n\nВсе выполнено!" if all_done else f"📋 <b>{today_str}</b>\n\nОтметь выполненные:"
    await cb.message.edit_text(header, reply_markup=await today_kb(cb.from_user.id), parse_mode="HTML")


# ── Note ──────────────────────────────────────────────────────────────────────

@dp.message(Command("skip"), AddNote.waiting_note)
async def cmd_skip_note(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Окей 👍", reply_markup=main_reply_kb())

@dp.message(AddNote.waiting_note)
async def fsm_note(msg: Message, state: FSMContext):
    data = await state.get_data()
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE completions SET note=? WHERE habit_id=? AND completed_date=?",
            (msg.text.strip(), data["habit_id"], today)
        )
        await db.commit()
    await state.clear()
    await msg.answer("📝 Заметка сохранена!", reply_markup=main_reply_kb())


# ── Add habit ─────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "add_habit")
async def cb_add_habit(cb: CallbackQuery, state: FSMContext):
    if not await can_add_habit(cb.from_user.id):
        await send_limit_reached(cb.from_user.id, cb.message)
        return
    await state.set_state(AddHabit.waiting_name)
    await cb.message.edit_text(
        "➕ <b>Новая привычка</b>\n\nКак называется?\n<i>Например: Зарядка, Читать, Пить воду</i>",
        parse_mode="HTML"
    )

@dp.message(AddHabit.waiting_name)
async def fsm_habit_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text.strip())
    await state.set_state(AddHabit.waiting_emoji)
    await msg.answer("Выбери эмодзи:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💪", callback_data="emoji_💪"),
         InlineKeyboardButton(text="🏃", callback_data="emoji_🏃"),
         InlineKeyboardButton(text="📚", callback_data="emoji_📚"),
         InlineKeyboardButton(text="💧", callback_data="emoji_💧"),
         InlineKeyboardButton(text="🧘", callback_data="emoji_🧘")],
        [InlineKeyboardButton(text="🥗", callback_data="emoji_🥗"),
         InlineKeyboardButton(text="😴", callback_data="emoji_😴"),
         InlineKeyboardButton(text="🎯", callback_data="emoji_🎯"),
         InlineKeyboardButton(text="✍️", callback_data="emoji_✍️"),
         InlineKeyboardButton(text="🎵", callback_data="emoji_🎵")],
        [InlineKeyboardButton(text="✅ Без эмодзи", callback_data="emoji_✅")],
    ]))

@dp.callback_query(F.data.startswith("emoji_"))
async def fsm_emoji_cb(cb: CallbackQuery, state: FSMContext):
    current = await state.get_state()
    if current != AddHabit.waiting_emoji.state:
        await cb.answer("Сначала начни добавление привычки.", show_alert=True)
        return
    await state.update_data(emoji=cb.data.split("_", 1)[1])
    await _ask_frequency(cb.message, state)

@dp.message(AddHabit.waiting_emoji)
async def fsm_emoji_text(msg: Message, state: FSMContext):
    await state.update_data(emoji=msg.text.strip())
    await _ask_frequency(msg, state)


async def _ask_frequency(target, state: FSMContext):
    await state.set_state(AddHabit.waiting_frequency)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Каждый день", callback_data="freq_daily")],
        [InlineKeyboardButton(text="🔢 N раз в неделю", callback_data="freq_times")],
        [InlineKeyboardButton(text="🗓 Конкретные дни", callback_data="freq_days")],
    ])
    text = "📆 <b>Как часто выполнять?</b>"
    if hasattr(target, 'message_id'):
        await target.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "freq_daily")
async def fsm_freq_daily(cb: CallbackQuery, state: FSMContext):
    current = await state.get_state()
    if current != AddHabit.waiting_frequency.state:
        await cb.answer("Сначала начни добавление привычки.", show_alert=True)
        return
    await state.update_data(frequency_type="daily", frequency_data=None)
    await _ask_remind_time(cb.message, state)


@dp.callback_query(F.data == "freq_times")
async def fsm_freq_times(cb: CallbackQuery, state: FSMContext):
    current = await state.get_state()
    if current != AddHabit.waiting_frequency.state:
        await cb.answer("Сначала начни добавление привычки.", show_alert=True)
        return
    await state.set_state(AddHabit.waiting_times_per_week)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="2", callback_data="times_2"),
         InlineKeyboardButton(text="3", callback_data="times_3"),
         InlineKeyboardButton(text="4", callback_data="times_4")],
        [InlineKeyboardButton(text="5", callback_data="times_5"),
         InlineKeyboardButton(text="6", callback_data="times_6")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="freq_back")],
    ])
    await cb.message.edit_text("🔢 Сколько раз в неделю?", reply_markup=kb)


@dp.callback_query(F.data.startswith("times_"))
async def fsm_times_pick(cb: CallbackQuery, state: FSMContext):
    current = await state.get_state()
    if current != AddHabit.waiting_times_per_week.state:
        await cb.answer("Сначала начни добавление привычки.", show_alert=True)
        return
    n = cb.data.split("_", 1)[1]
    await state.update_data(frequency_type="times_per_week", frequency_data=n)
    await _ask_remind_time(cb.message, state)


@dp.callback_query(F.data == "freq_days")
async def fsm_freq_days(cb: CallbackQuery, state: FSMContext):
    current = await state.get_state()
    if current != AddHabit.waiting_frequency.state:
        await cb.answer("Сначала начни добавление привычки.", show_alert=True)
        return
    await state.set_state(AddHabit.waiting_specific_days)
    await state.update_data(selected_days=[])
    await cb.message.edit_text("🗓 Выбери дни (можно несколько):", reply_markup=specific_days_kb([]))


def specific_days_kb(selected: list) -> InlineKeyboardMarkup:
    names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    buttons = []
    row = []
    for i, name in enumerate(names):
        mark = "✅ " if i in selected else ""
        row.append(InlineKeyboardButton(text=f"{mark}{name}", callback_data=f"day_{i}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="✔️ Готово", callback_data="days_done")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="freq_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data.startswith("day_"))
async def fsm_day_toggle(cb: CallbackQuery, state: FSMContext):
    current = await state.get_state()
    if current != AddHabit.waiting_specific_days.state:
        await cb.answer("Сначала начни добавление привычки.", show_alert=True)
        return
    day_idx = int(cb.data.split("_", 1)[1])
    data = await state.get_data()
    selected = data.get("selected_days", [])
    if day_idx in selected:
        selected.remove(day_idx)
    else:
        selected.append(day_idx)
    await state.update_data(selected_days=selected)
    await cb.message.edit_reply_markup(reply_markup=specific_days_kb(selected))


@dp.callback_query(F.data == "days_done")
async def fsm_days_done(cb: CallbackQuery, state: FSMContext):
    current = await state.get_state()
    if current != AddHabit.waiting_specific_days.state:
        await cb.answer("Сначала начни добавление привычки.", show_alert=True)
        return
    data = await state.get_data()
    selected = data.get("selected_days", [])
    if not selected:
        await cb.answer("Выбери хотя бы один день!", show_alert=True)
        return
    days_str = ",".join(str(d) for d in sorted(selected))
    await state.update_data(frequency_type="specific_days", frequency_data=days_str)
    await _ask_remind_time(cb.message, state)


@dp.callback_query(F.data == "freq_back")
async def fsm_freq_back(cb: CallbackQuery, state: FSMContext):
    await _ask_frequency(cb.message, state)


async def _ask_remind_time(target, state: FSMContext):
    await state.set_state(AddHabit.waiting_time)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="07:00", callback_data="time_07:00"),
         InlineKeyboardButton(text="08:00", callback_data="time_08:00"),
         InlineKeyboardButton(text="09:00", callback_data="time_09:00")],
        [InlineKeyboardButton(text="20:00", callback_data="time_20:00"),
         InlineKeyboardButton(text="21:00", callback_data="time_21:00"),
         InlineKeyboardButton(text="22:00", callback_data="time_22:00")],
        [InlineKeyboardButton(text="🔕 Без напоминания", callback_data="time_none")],
    ])
    text = "🔔 Когда напоминать?\n\nВыбери или напиши <code>ЧЧ:ММ</code>"
    if hasattr(target, 'message_id'):
        await target.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("time_"))
async def fsm_time_cb(cb: CallbackQuery, state: FSMContext):
    current = await state.get_state()
    if current != AddHabit.waiting_time.state:
        await cb.answer("Сначала начни добавление привычки.", show_alert=True)
        return
    val = cb.data.split("_", 1)[1]
    await state.update_data(remind_time=val if val != "none" else None)
    await _ask_goal(cb.message, state)

@dp.message(AddHabit.waiting_time)
async def fsm_time_text(msg: Message, state: FSMContext):
    try:
        datetime.strptime(msg.text.strip(), "%H:%M")
        await state.update_data(remind_time=msg.text.strip())
        await _ask_goal(msg, state)
    except ValueError:
        await msg.answer("Неверный формат. Напиши <code>08:30</code> или выбери кнопку.", parse_mode="HTML")

async def _ask_goal(target, state: FSMContext):
    await state.set_state(AddHabit.waiting_goal)
    days_in_month = calendar.monthrange(date.today().year, date.today().month)[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Каждый день ({days_in_month})", callback_data=f"goal_{days_in_month}")],
        [InlineKeyboardButton(text="20 дней", callback_data="goal_20"),
         InlineKeyboardButton(text="15 дней", callback_data="goal_15"),
         InlineKeyboardButton(text="10 дней", callback_data="goal_10")],
        [InlineKeyboardButton(text="Без цели", callback_data="goal_none")],
    ])
    text = "🎯 <b>Цель на месяц</b>\n\nСколько дней хочешь выполнять привычку?"
    if hasattr(target, 'message_id'):
        await target.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("goal_"))
async def fsm_goal_cb(cb: CallbackQuery, state: FSMContext):
    current = await state.get_state()
    if current != AddHabit.waiting_goal.state:
        await cb.answer("Сначала начни добавление привычки через кнопку Добавить.", show_alert=True)
        return
    val = cb.data.split("_", 1)[1]
    goal = int(val) if val != "none" else None
    await state.update_data(monthly_goal=goal)
    await _save_habit(cb, state)

@dp.message(AddHabit.waiting_goal)
async def fsm_goal_text(msg: Message, state: FSMContext):
    try:
        goal = int(msg.text.strip())
        if 1 <= goal <= 31:
            await state.update_data(monthly_goal=goal)
            await _save_habit(msg, state)
        else:
            await msg.answer("Введи число от 1 до 31.")
    except ValueError:
        await msg.answer("Введи число, например: 20")

async def _save_habit(source, state: FSMContext):
    data = await state.get_data()
    user_id = source.from_user.id
    freq_type = data.get("frequency_type", "daily")
    freq_data = data.get("frequency_data")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO habits (user_id, name, emoji, remind_time, monthly_goal, frequency_type, frequency_data) "
            "VALUES (?,?,?,?,?,?,?)",
            (user_id, data["name"], data["emoji"], data.get("remind_time"), data.get("monthly_goal"), freq_type, freq_data)
        )
        await db.commit()
    await state.clear()
    remind_text = f"⏰ {data.get('remind_time')}" if data.get("remind_time") else "🔕 Без напоминания"
    goal_text = f"🎯 Цель: {data['monthly_goal']} дней" if data.get("monthly_goal") else "🎯 Без цели"
    freq_label = frequency_label({"frequency_type": freq_type, "frequency_data": freq_data})
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 К привычкам", callback_data="show_today")],
        [InlineKeyboardButton(text="➕ Добавить ещё", callback_data="add_habit")],
    ])
    text = f"✅ <b>Привычка добавлена!</b>\n\n{data['emoji']} {data['name']}\n📆 {freq_label}\n{remind_text}\n{goal_text}"
    if hasattr(source, 'message'):
        await source.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await source.answer(text, reply_markup=kb, parse_mode="HTML")


# ── Profile ───────────────────────────────────────────────────────────────────

async def _send_profile(user_id: int, target):
    xp = await get_user_xp(user_id)
    rank = await get_user_rank(user_id)
    level_num, level_name, next_xp = get_level(xp)
    bar = xp_bar(xp, next_xp)
    achs = await get_user_achievements(user_id)
    habits = await get_habits(user_id)
    sub_status = await get_subscription_status(user_id)

    next_text = f"{next_xp - xp} ⚡ до следующего уровня" if next_xp else "Максимальный уровень!"

    if sub_status["is_premium"] and sub_status["is_trial"]:
        sub_line = f"🎁 Пробный период · осталось {sub_status['trial_days_left']} дн."
    elif sub_status["is_premium"]:
        sub_line = f"✨ Premium до {sub_status['premium_until'].strftime('%d.%m.%Y')}"
    else:
        sub_line = f"🔒 Бесплатный тариф · до {FREE_HABIT_LIMIT} привычек"

    lines = [
        f"👤 <b>Мой профиль</b>\n",
        f"{level_name}  •  Уровень {level_num}",
        f"⚡ {xp} XP  •  {rank_medal(rank)} #{rank} в рейтинге",
        f"<code>{bar}</code>  {next_text}\n",
        f"📌 Привычек: {len(habits)}",
        f"🏅 Достижений: {len(achs)}",
        f"\n{sub_line}",
    ]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Premium", callback_data="show_premium")],
        [InlineKeyboardButton(text="✏️ Изменить имя в рейтинге", callback_data="set_username")],
        [InlineKeyboardButton(text="🏆 Рейтинг", callback_data="show_leaderboard")],
        [InlineKeyboardButton(text="🏅 Достижения", callback_data="show_achievements")],
    ])
    text = "\n".join(lines)
    if hasattr(target, 'message_id'):
        await target.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "show_profile")
async def cb_show_profile(cb: CallbackQuery):
    await ensure_user(cb.from_user.id, cb.from_user.first_name)
    await _send_profile(cb.from_user.id, cb.message)

@dp.callback_query(F.data == "set_username")
async def cb_set_username(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SetUsername.waiting_name)
    await cb.message.edit_text("✏️ Напиши имя которое будет отображаться в рейтинге:")

@dp.message(SetUsername.waiting_name)
async def fsm_set_username(msg: Message, state: FSMContext):
    name = msg.text.strip()[:32]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET display_name=? WHERE user_id=?", (name, msg.from_user.id))
        await db.commit()
    await state.clear()
    await msg.answer(f"✅ Имя изменено на <b>{name}</b>", parse_mode="HTML", reply_markup=main_reply_kb())


# ── Leaderboard ───────────────────────────────────────────────────────────────

async def _send_leaderboard(user_id: int, target):
    leaders = await get_leaderboard(10)
    rank = await get_user_rank(user_id)
    xp = await get_user_xp(user_id)
    _, level_name, _ = get_level(xp)

    lines = ["🏆 <b>Таблица лидеров</b>\n"]
    for i, row in enumerate(leaders, 1):
        medal = rank_medal(i)
        _, lv_name, _ = get_level(row["total_xp"])
        name = row["display_name"] or "Аноним"
        is_me = "← ты" if row["user_id"] == user_id else ""
        lines.append(f"{medal} <b>{name}</b>  {lv_name}\n   ⚡ {row['total_xp']} XP  {is_me}")

    lines.append(f"\n📍 Твоё место: #{rank}  •  ⚡ {xp} XP  •  {level_name}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="show_profile")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="show_leaderboard")],
    ])
    text = "\n".join(lines)
    if hasattr(target, 'message_id'):
        await target.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "show_leaderboard")
async def cb_show_leaderboard(cb: CallbackQuery):
    await ensure_user(cb.from_user.id, cb.from_user.first_name)
    await _send_leaderboard(cb.from_user.id, cb.message)


# ── Statistics ────────────────────────────────────────────────────────────────

async def _send_stats(user_id: int, target):
    habits = await get_habits(user_id)
    if not habits:
        text = "Нет привычек. Добавь первую!"
        if hasattr(target, 'message_id'):
            await target.answer(text)
        else:
            await target.edit_text(text)
        return
    today = date.today()
    lines = [f"📊 <b>Статистика — {today.strftime('%B %Y')}</b>\n"]
    for h in habits:
        streak = await get_streak(h["id"])
        best = await get_best_streak(h["id"])
        stats = await get_monthly_stats(h["id"])
        streak_icon = "🔥" if streak >= 3 else ("✨" if streak > 0 else "💤")
        if h["monthly_goal"]:
            bar = goal_progress_bar(stats["completed"], h["monthly_goal"])
            goal_line = f"  {bar} {stats['completed']}/{h['monthly_goal']} дн."
        else:
            bar = progress_bar(stats["percent"])
            goal_line = f"  {bar} {stats['percent']}%"
        lines.append(
            f"{h['emoji']} <b>{h['name']}</b>\n"
            f"{goal_line}\n"
            f"  {streak_icon} Стрик: {streak}  •  Рекорд: {best}\n"
        )
    lines.append("📅 <b>Календари</b>")
    for h in habits:
        stats = await get_monthly_stats(h["id"])
        cal = calendar_grid(stats["dates"], today.year, today.month)
        lines.append(f"\n{h['emoji']} {h['name']}\n<code>{cal}</code>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 История за неделю", callback_data="show_week")],
        [InlineKeyboardButton(text="🏆 Рейтинг", callback_data="show_leaderboard")],
    ])
    text = "\n".join(lines)
    if hasattr(target, 'message_id'):
        await target.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "show_stats")
async def cb_show_stats(cb: CallbackQuery):
    await _send_stats(cb.from_user.id, cb.message)


# ── Week ──────────────────────────────────────────────────────────────────────

async def _send_week(user_id: int, target):
    habits = await get_habits(user_id)
    if not habits:
        text = "Нет привычек."
        if hasattr(target, 'message_id'):
            await target.answer(text)
        else:
            await target.edit_text(text)
        return
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    days = [week_start + timedelta(days=i) for i in range(7)]
    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    header = "  " + " ".join(f"{d:>2}" for d in day_names)
    lines = [f"📅 <b>Неделя {week_start.strftime('%d.%m')}–{days[-1].strftime('%d.%m')}</b>\n",
             f"<code>{header}"]
    for h in habits:
        week_done = await get_week_completions(h["id"])
        row = f"{h['emoji']} "
        for d in days:
            row += "✅" if d.isoformat() in week_done else ("·· " if d > today else "❌ ")
        lines.append(row)
    lines.append("</code>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="show_stats")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
    ])
    text = "\n".join(lines)
    if hasattr(target, 'message_id'):
        await target.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")

@dp.message(Command("week"))
async def cmd_week(msg: Message):
    await _send_week(msg.from_user.id, msg)

@dp.callback_query(F.data == "show_week")
async def cb_show_week(cb: CallbackQuery):
    await _send_week(cb.from_user.id, cb.message)


# ── Achievements ──────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "show_achievements")
async def cb_show_achievements(cb: CallbackQuery):
    habits = await get_habits(cb.from_user.id)
    earned = await get_user_achievements(cb.from_user.id)
    earned_ids = {(r["habit_id"], r["achievement_id"]) for r in earned}
    lines = ["🏅 <b>Достижения</b>\n"]
    for h in habits:
        streak = await get_streak(h["id"])
        lines.append(f"{h['emoji']} <b>{h['name']}</b> — стрик: {streak} дн.")
        for ach in ACHIEVEMENTS:
            if (h["id"], ach["id"]) in earned_ids:
                lines.append(f"  {ach['icon']} {ach['title']}")
            else:
                lines.append(f"  🔒 {ach['title']} ({ach['streak']} дн.)")
        lines.append("")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль", callback_data="show_profile")],
    ])
    await cb.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")


# ── Goals ─────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "show_goals")
async def cb_show_goals(cb: CallbackQuery):
    habits = await get_habits(cb.from_user.id)
    if not habits:
        await cb.answer("Нет привычек.", show_alert=True)
        return
    today = date.today()
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_left = days_in_month - today.day
    lines = [f"🎯 <b>Цели на {today.strftime('%B')}</b>\n"]
    for h in habits:
        stats = await get_monthly_stats(h["id"])
        if h["monthly_goal"]:
            goal = h["monthly_goal"]
            done = stats["completed"]
            remaining = max(goal - done, 0)
            bar = goal_progress_bar(done, goal)
            status = "✅ Цель достигнута!" if done >= goal else (
                f"📈 Осталось {remaining} дн." if remaining <= days_left else f"⚠️ Осталось {remaining} дн., дней в месяце: {days_left}"
            )
            lines.append(f"{h['emoji']} <b>{h['name']}</b>\n  {bar} {done}/{goal}\n  {status}\n")
        else:
            lines.append(f"{h['emoji']} <b>{h['name']}</b>\n  Цель не установлена\n")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить цели", callback_data="edit_goals")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="show_manage")],
    ])
    await cb.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "edit_goals")
async def cb_edit_goals(cb: CallbackQuery):
    await cb.message.edit_text("🎯 Выбери привычку:", reply_markup=await goals_kb(cb.from_user.id))

@dp.callback_query(F.data.startswith("setgoal_"))
async def cb_setgoal(cb: CallbackQuery, state: FSMContext):
    habit_id = int(cb.data.split("_")[1])
    await state.set_state(SetGoal.waiting_days)
    await state.update_data(habit_id=habit_id)
    days_in_month = calendar.monthrange(date.today().year, date.today().month)[1]
    await cb.message.edit_text("🎯 Сколько дней?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Каждый день ({days_in_month})", callback_data=f"newgoal_{days_in_month}")],
        [InlineKeyboardButton(text="20", callback_data="newgoal_20"),
         InlineKeyboardButton(text="15", callback_data="newgoal_15"),
         InlineKeyboardButton(text="10", callback_data="newgoal_10")],
        [InlineKeyboardButton(text="❌ Убрать цель", callback_data="newgoal_none")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="edit_goals")],
    ]))

@dp.callback_query(F.data.startswith("newgoal_"))
async def cb_newgoal(cb: CallbackQuery, state: FSMContext):
    current = await state.get_state()
    if current != SetGoal.waiting_days.state:
        await cb.answer("Сначала выбери привычку для изменения цели.", show_alert=True)
        return
    data = await state.get_data()
    val = cb.data.split("_", 1)[1]
    goal = int(val) if val != "none" else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE habits SET monthly_goal=? WHERE id=?", (goal, data["habit_id"]))
        await db.commit()
    await state.clear()
    await cb.answer(f"✅ {'Цель: ' + str(goal) + ' дней' if goal else 'Цель убрана'}")
    await cb.message.edit_text("🎯 Выбери привычку:", reply_markup=await goals_kb(cb.from_user.id))


# ── Rename / Pause / Delete ───────────────────────────────────────────────────

@dp.callback_query(F.data == "rename_list")
async def cb_rename_list(cb: CallbackQuery):
    await cb.message.edit_text("✏️ Выбери привычку:", reply_markup=await habit_select_kb(cb.from_user.id, "rename_", include_paused=True))

@dp.callback_query(F.data.startswith("rename_"))
async def cb_rename_pick(cb: CallbackQuery, state: FSMContext):
    habit_id = int(cb.data.split("_")[1])
    await state.set_state(RenameHabit.waiting_new_name)
    await state.update_data(habit_id=habit_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT name, emoji FROM habits WHERE id=?", (habit_id,)) as cur:
            h = await cur.fetchone()
    await cb.message.edit_text(f"✏️ Сейчас: <b>{h['emoji']} {h['name']}</b>\n\nНапиши новое название:", parse_mode="HTML")

@dp.message(RenameHabit.waiting_new_name)
async def fsm_rename(msg: Message, state: FSMContext):
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE habits SET name=? WHERE id=?", (msg.text.strip(), data["habit_id"]))
        await db.commit()
    await state.clear()
    await msg.answer(f"✅ Переименовано: <b>{msg.text.strip()}</b>", parse_mode="HTML", reply_markup=main_reply_kb())

@dp.callback_query(F.data == "pause_list")
async def cb_pause_list(cb: CallbackQuery):
    habits = [h for h in await get_habits(cb.from_user.id) if not h["is_paused"]]
    if not habits:
        await cb.answer("Нет активных привычек.", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(text=f"{h['emoji']} {h['name']}", callback_data=f"dopause_{h['id']}")] for h in habits]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="show_manage")])
    await cb.message.edit_text("⏸ Выбери привычку:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("dopause_"))
async def cb_dopause(cb: CallbackQuery):
    habit_id = int(cb.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT name, emoji FROM habits WHERE id=?", (habit_id,)) as cur:
            h = await cur.fetchone()
        await db.execute("UPDATE habits SET is_paused=1 WHERE id=?", (habit_id,))
        await db.commit()
    await cb.answer(f"⏸ На паузе: {h['emoji']} {h['name']}")
    await cb.message.edit_text("⚙️ <b>Управление</b>", reply_markup=manage_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "unpause_list")
async def cb_unpause_list(cb: CallbackQuery):
    habits = await get_habits(cb.from_user.id, include_paused=True)
    paused = [h for h in habits if h["is_paused"]]
    if not paused:
        await cb.answer("Нет привычек на паузе.", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(text=f"{h['emoji']} {h['name']}", callback_data=f"dounpause_{h['id']}")] for h in paused]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="show_manage")])
    await cb.message.edit_text("▶️ Выбери привычку:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("dounpause_"))
async def cb_dounpause(cb: CallbackQuery):
    habit_id = int(cb.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT name, emoji FROM habits WHERE id=?", (habit_id,)) as cur:
            h = await cur.fetchone()
        await db.execute("UPDATE habits SET is_paused=0 WHERE id=?", (habit_id,))
        await db.commit()
    await cb.answer(f"▶️ Возобновлена: {h['emoji']} {h['name']}")
    await cb.message.edit_text("⚙️ <b>Управление</b>", reply_markup=manage_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "delete_list")
async def cb_delete_list(cb: CallbackQuery):
    habits = await get_habits(cb.from_user.id, include_paused=True)
    if not habits:
        await cb.answer("Нет привычек.", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(text=f"🗑 {h['emoji']} {h['name']}", callback_data=f"del_{h['id']}")] for h in habits]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="show_manage")])
    await cb.message.edit_text("🗑 Выбери привычку:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("del_"))
async def cb_delete(cb: CallbackQuery):
    habit_id = int(cb.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT name, emoji FROM habits WHERE id=?", (habit_id,)) as cur:
            h = await cur.fetchone()
        await db.execute("UPDATE habits SET is_active=0 WHERE id=?", (habit_id,))
        await db.commit()
    await cb.answer(f"Удалено: {h['emoji']} {h['name']}")
    habits = await get_habits(cb.from_user.id, include_paused=True)
    if not habits:
        await cb.message.edit_text("Привычек не осталось.", reply_markup=main_menu_kb())
    else:
        buttons = [[InlineKeyboardButton(text=f"🗑 {h['emoji']} {h['name']}", callback_data=f"del_{h['id']}")] for h in habits]
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="show_manage")])
        await cb.message.edit_text("🗑 Выбери привычку:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


# ── Reminders + weekly report ─────────────────────────────────────────────────

async def send_weekly_report(user_id: int):
    habits = await get_habits(user_id)
    if not habits:
        return
    today = date.today()
    week_start = today - timedelta(days=7)
    rank = await get_user_rank(user_id)
    xp = await get_user_xp(user_id)
    _, level_name, _ = get_level(xp)
    lines = [f"📊 <b>Итог недели</b>  •  {rank_medal(rank)} #{rank}  •  {level_name}\n"]
    for h in habits:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM completions WHERE habit_id=? AND completed_date > ? AND completed_date <= ?",
                (h["id"], week_start.isoformat(), today.isoformat())
            ) as cur:
                count = (await cur.fetchone())[0]
        streak = await get_streak(h["id"])
        bar = progress_bar(round(count / 7 * 100))
        lines.append(f"{h['emoji']} <b>{h['name']}</b>\n  {bar} {count}/7  •  🔥 {streak}\n")
    try:
        await bot.send_message(user_id, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Weekly report failed for {user_id}: {e}")


async def check_reminders():
    while True:
        now = datetime.now()
        now_str = now.strftime("%H:%M")
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT DISTINCT user_id, remind_time FROM habits WHERE is_active=1 AND is_paused=0 AND remind_time=?",
                (now_str,)
            ) as cur:
                rows = await cur.fetchall()
        for row in rows:
            user_id = row["user_id"]
            habits = await get_habits(user_id)
            done = await get_today_completions(user_id)
            pending = [h for h in habits if h["id"] not in done and h["remind_time"] == now_str]
            if pending:
                names = ", ".join(f"{h['emoji']} {h['name']}" for h in pending)
                try:
                    await bot.send_message(user_id, f"⏰ Время для привычек!\n\n{names}",
                                           reply_markup=await today_kb(user_id))
                except Exception as e:
                    logger.warning(f"Reminder failed for {user_id}: {e}")
        if now.weekday() == 6 and now_str == "21:00":
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT DISTINCT user_id FROM habits WHERE is_active=1") as cur:
                    users = [r[0] for r in await cur.fetchall()]
            for uid in users:
                await send_weekly_report(uid)

        # Trial ending reminder, once a day at 12:00
        if now_str == "12:00":
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT user_id, trial_started_at FROM users WHERE trial_started_at IS NOT NULL AND premium_until IS NULL"
                ) as cur:
                    trial_users = await cur.fetchall()
            for row in trial_users:
                status = await get_subscription_status(row["user_id"])
                if status["is_trial"] and status["trial_days_left"] == 1:
                    try:
                        await bot.send_message(
                            row["user_id"],
                            f"⏳ <b>Пробный период заканчивается завтра!</b>\n\n"
                            f"После этого бесплатно останется {FREE_HABIT_LIMIT} привычки. "
                            f"Оформи Premium чтобы сохранить безлимит.",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="✨ Узнать про Premium", callback_data="show_premium")]
                            ])
                        )
                    except Exception as e:
                        logger.warning(f"Trial reminder failed for {row['user_id']}: {e}")

        await asyncio.sleep(60)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    await init_db()
    asyncio.create_task(check_reminders())

    # Start Mini App web server
    try:
        from webapp_server import run_webapp
        import os as _os
        port = int(_os.getenv("PORT", "8080"))
        db_helpers = {
            "get_habits": get_habits,
            "create_habit": create_habit,
            "get_today_completions": get_today_completions,
            "toggle_completion": toggle_completion,
            "get_streak": get_streak,
            "get_best_streak": get_best_streak,
            "get_monthly_stats": get_monthly_stats,
            "get_week_completions": get_week_completions,
            "check_and_grant_achievements": check_and_grant_achievements,
            "get_user_xp": get_user_xp,
            "add_xp": add_xp,
            "get_level": get_level,
            "get_leaderboard": get_leaderboard,
            "get_user_rank": get_user_rank,
            "ensure_user": ensure_user,
            "can_add_habit": can_add_habit,
            "get_subscription_status": get_subscription_status,
            "rename_habit": rename_habit,
            "set_habit_paused": set_habit_paused,
            "delete_habit": delete_habit,
            "set_display_name": set_display_name,
            "frequency_label": frequency_label,
            "FREE_HABIT_LIMIT": FREE_HABIT_LIMIT,
            "SUBSCRIPTION_STARS_PRICE": SUBSCRIPTION_STARS_PRICE,
            "LEVELS": LEVELS,
        }
        await run_webapp(BOT_TOKEN, db_helpers, port=port)
        logger.info("Mini app web server started")
    except Exception as e:
        logger.warning(f"Mini app server failed to start: {e}")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
