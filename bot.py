import asyncio
import logging
import calendar
from datetime import datetime, date, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import aiosqlite
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "habits.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ── Achievements ──────────────────────────────────────────────────────────────

ACHIEVEMENTS = [
    {"id": "streak_3",   "streak": 3,   "icon": "🥉", "title": "3 дня подряд",    "desc": "Хорошее начало!"},
    {"id": "streak_7",   "streak": 7,   "icon": "🥈", "title": "Неделя подряд",   "desc": "Привычка формируется!"},
    {"id": "streak_14",  "streak": 14,  "icon": "🥇", "title": "2 недели подряд", "desc": "Ты на верном пути!"},
    {"id": "streak_30",  "streak": 30,  "icon": "🏆", "title": "Месяц подряд",    "desc": "Настоящая привычка!"},
    {"id": "streak_60",  "streak": 60,  "icon": "💎", "title": "60 дней подряд",  "desc": "Легенда!"},
    {"id": "streak_100", "streak": 100, "icon": "🚀", "title": "100 дней подряд", "desc": "Невероятно!"},
]


# ── FSM States ────────────────────────────────────────────────────────────────

class AddHabit(StatesGroup):
    waiting_name = State()
    waiting_emoji = State()
    waiting_time = State()
    waiting_goal = State()

class SetGoal(StatesGroup):
    waiting_days = State()

class RenameHabit(StatesGroup):
    waiting_new_name = State()

class AddNote(StatesGroup):
    waiting_note = State()


# ── Database ──────────────────────────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
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
        for col, definition in [
            ("monthly_goal", "INTEGER DEFAULT NULL"),
            ("is_paused", "INTEGER DEFAULT 0"),
        ]:
            try:
                await db.execute(f"ALTER TABLE habits ADD COLUMN {col} {definition}")
            except Exception:
                pass
        try:
            await db.execute("ALTER TABLE completions ADD COLUMN note TEXT DEFAULT NULL")
        except Exception:
            pass
        await db.commit()


async def get_habits(user_id: int, include_paused: bool = False) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if include_paused:
            sql = "SELECT * FROM habits WHERE user_id=? AND is_active=1 ORDER BY id"
            params = (user_id,)
        else:
            sql = "SELECT * FROM habits WHERE user_id=? AND is_active=1 AND is_paused=0 ORDER BY id"
            params = (user_id,)
        async with db.execute(sql, params) as cur:
            return await cur.fetchall()


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
            await db.execute(
                "DELETE FROM completions WHERE habit_id=? AND completed_date=?",
                (habit_id, today)
            )
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

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои привычки", callback_data="show_today")],
        [InlineKeyboardButton(text="➕ Добавить привычку", callback_data="add_habit")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="show_stats")],
        [InlineKeyboardButton(text="📅 История за неделю", callback_data="show_week")],
        [InlineKeyboardButton(text="🏆 Достижения", callback_data="show_achievements")],
        [InlineKeyboardButton(text="🎯 Цели на месяц", callback_data="show_goals")],
        [InlineKeyboardButton(text="⚙️ Управление", callback_data="show_manage")],
    ])


def manage_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Переименовать привычку", callback_data="rename_list")],
        [InlineKeyboardButton(text="⏸ Поставить на паузу", callback_data="pause_list")],
        [InlineKeyboardButton(text="▶️ Снять с паузы", callback_data="unpause_list")],
        [InlineKeyboardButton(text="🗑 Удалить привычку", callback_data="delete_list")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu")],
    ])


async def today_kb(user_id: int) -> InlineKeyboardMarkup:
    habits = await get_habits(user_id)
    done = await get_today_completions(user_id)
    buttons = []
    for h in habits:
        status = "✅" if h["id"] in done else "⬜"
        buttons.append([InlineKeyboardButton(
            text=f"{status} {h['emoji']} {h['name']}",
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


async def delete_kb(user_id: int) -> InlineKeyboardMarkup:
    return await habit_select_kb(user_id, "del_", include_paused=True)


async def goals_kb(user_id: int) -> InlineKeyboardMarkup:
    habits = await get_habits(user_id, include_paused=True)
    buttons = []
    for h in habits:
        goal_text = f"({h['monthly_goal']} дн.)" if h["monthly_goal"] else "(нет цели)"
        buttons.append([InlineKeyboardButton(
            text=f"{h['emoji']} {h['name']} {goal_text}",
            callback_data=f"setgoal_{h['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Helpers ───────────────────────────────────────────────────────────────────

def progress_bar(percent: int, length: int = 10) -> str:
    filled = round(percent / 100 * length)
    return "█" * filled + "░" * (length - filled)


def goal_progress_bar(completed: int, goal: int, length: int = 10) -> str:
    pct = min(completed / goal, 1.0) if goal else 0
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


# ── Handlers: core ────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(msg: Message):
    await msg.answer(
        f"Привет, {msg.from_user.first_name}! 👋\n\n"
        "Я помогу отслеживать привычки.\n"
        "Отмечай каждый день — следи за стриками и достижениями! 🔥",
        reply_markup=main_menu_kb()
    )

@dp.message(Command("menu"))
async def cmd_menu(msg: Message):
    await msg.answer("Главное меню:", reply_markup=main_menu_kb())

@dp.message(Command("week"))
async def cmd_week(msg: Message):
    await _send_week(msg.from_user.id, msg)

@dp.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery):
    await cb.message.edit_text("Главное меню:", reply_markup=main_menu_kb())

@dp.callback_query(F.data == "show_manage")
async def cb_show_manage(cb: CallbackQuery):
    await cb.message.edit_text("⚙️ <b>Управление привычками</b>", reply_markup=manage_kb(), parse_mode="HTML")


# ── Today ─────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "show_today")
async def cb_show_today(cb: CallbackQuery):
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
        new_achs = await check_and_grant_achievements(cb.from_user.id, habit_id, streak)
        streak_text = f" 🔥 {streak}" if streak > 1 else ""
        await cb.answer(f"✅ {habit['emoji']} {habit['name']}{streak_text}", show_alert=False)
        for ach in new_achs:
            await bot.send_message(
                cb.from_user.id,
                f"🎉 <b>Новое достижение!</b>\n\n{ach['icon']} <b>{ach['title']}</b>\n"
                f"{habit['emoji']} {habit['name']}\n\n<i>{ach['desc']}</i>",
                parse_mode="HTML"
            )
        # Ask for note
        await state.set_state(AddNote.waiting_note)
        await state.update_data(habit_id=habit_id)
        await bot.send_message(
            cb.from_user.id,
            f"📝 Добавить заметку к <b>{habit['emoji']} {habit['name']}</b>? (или /skip)",
            parse_mode="HTML"
        )
    else:
        await cb.answer("⬜ Отметка снята", show_alert=False)

    done = await get_today_completions(cb.from_user.id)
    habits = await get_habits(cb.from_user.id)
    today_str = date.today().strftime("%d %B %Y")
    all_done = len(done) == len(habits) and len(habits) > 0
    header = f"🎉 <b>{today_str}</b>\n\nВсе привычки выполнены!" if all_done else f"📋 <b>{today_str}</b>\n\nОтметь выполненные:"
    await cb.message.edit_text(header, reply_markup=await today_kb(cb.from_user.id), parse_mode="HTML")


# ── Note ──────────────────────────────────────────────────────────────────────

@dp.message(Command("skip"), AddNote.waiting_note)
async def cmd_skip_note(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Окей, без заметки 👍")

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
    await msg.answer("📝 Заметка сохранена!")


# ── Add habit ─────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "add_habit")
async def cb_add_habit(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddHabit.waiting_name)
    await cb.message.edit_text(
        "➕ <b>Новая привычка</b>\n\nКак называется?\n<i>Например: Зарядка, Читать, Пить воду</i>",
        parse_mode="HTML"
    )

@dp.message(AddHabit.waiting_name)
async def fsm_habit_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text.strip())
    await state.set_state(AddHabit.waiting_emoji)
    await msg.answer(
        "Выбери эмодзи:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
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
        ])
    )

@dp.callback_query(F.data.startswith("emoji_"), AddHabit.waiting_emoji)
async def fsm_emoji_cb(cb: CallbackQuery, state: FSMContext):
    await state.update_data(emoji=cb.data.split("_", 1)[1])
    await _ask_remind_time(cb.message, state)

@dp.message(AddHabit.waiting_emoji)
async def fsm_emoji_text(msg: Message, state: FSMContext):
    await state.update_data(emoji=msg.text.strip())
    await _ask_remind_time(msg, state)

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
    text = "🔔 Когда напоминать?\n\nВыбери или напиши в формате <code>ЧЧ:ММ</code>"
    if hasattr(target, 'edit_text'):
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("time_"), AddHabit.waiting_time)
async def fsm_time_cb(cb: CallbackQuery, state: FSMContext):
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
    text = "🎯 <b>Цель на этот месяц</b>\n\nСколько дней хочешь выполнять привычку?"
    if hasattr(target, 'edit_text'):
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("goal_"), AddHabit.waiting_goal)
async def fsm_goal_cb(cb: CallbackQuery, state: FSMContext):
    val = cb.data.split("_", 1)[1]
    await state.update_data(monthly_goal=int(val) if val != "none" else None)
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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO habits (user_id, name, emoji, remind_time, monthly_goal) VALUES (?,?,?,?,?)",
            (user_id, data["name"], data["emoji"], data.get("remind_time"), data.get("monthly_goal"))
        )
        await db.commit()
    await state.clear()
    remind_text = f"⏰ {data.get('remind_time')}" if data.get("remind_time") else "🔕 Без напоминания"
    goal_text = f"🎯 Цель: {data['monthly_goal']} дней" if data.get("monthly_goal") else "🎯 Без цели"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 К привычкам на сегодня", callback_data="show_today")],
        [InlineKeyboardButton(text="➕ Добавить ещё", callback_data="add_habit")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
    ])
    text = f"✅ <b>Привычка добавлена!</b>\n\n{data['emoji']} {data['name']}\n{remind_text}\n{goal_text}"
    if hasattr(source, 'message'):
        await source.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await source.answer(text, reply_markup=kb, parse_mode="HTML")


# ── Rename ────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "rename_list")
async def cb_rename_list(cb: CallbackQuery):
    kb = await habit_select_kb(cb.from_user.id, "rename_", include_paused=True)
    await cb.message.edit_text("✏️ Выбери привычку для переименования:", reply_markup=kb)

@dp.callback_query(F.data.startswith("rename_"))
async def cb_rename_pick(cb: CallbackQuery, state: FSMContext):
    habit_id = int(cb.data.split("_")[1])
    await state.set_state(RenameHabit.waiting_new_name)
    await state.update_data(habit_id=habit_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT name, emoji FROM habits WHERE id=?", (habit_id,)) as cur:
            h = await cur.fetchone()
    await cb.message.edit_text(
        f"✏️ Текущее название: <b>{h['emoji']} {h['name']}</b>\n\nНапиши новое название:",
        parse_mode="HTML"
    )

@dp.message(RenameHabit.waiting_new_name)
async def fsm_rename(msg: Message, state: FSMContext):
    data = await state.get_data()
    new_name = msg.text.strip()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE habits SET name=? WHERE id=?", (new_name, data["habit_id"]))
        await db.commit()
    await state.clear()
    await msg.answer(f"✅ Переименовано: <b>{new_name}</b>", reply_markup=manage_kb(), parse_mode="HTML")


# ── Pause / Unpause ───────────────────────────────────────────────────────────

@dp.callback_query(F.data == "pause_list")
async def cb_pause_list(cb: CallbackQuery):
    habits = await get_habits(cb.from_user.id)
    active = [h for h in habits if not h["is_paused"]]
    if not active:
        await cb.answer("Нет активных привычек.", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(
        text=f"{h['emoji']} {h['name']}", callback_data=f"dopause_{h['id']}"
    )] for h in active]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="show_manage")])
    await cb.message.edit_text("⏸ Выбери привычку для паузы:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("dopause_"))
async def cb_dopause(cb: CallbackQuery):
    habit_id = int(cb.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT name, emoji FROM habits WHERE id=?", (habit_id,)) as cur:
            h = await cur.fetchone()
        await db.execute("UPDATE habits SET is_paused=1 WHERE id=?", (habit_id,))
        await db.commit()
    await cb.answer(f"⏸ {h['emoji']} {h['name']} — на паузе")
    await cb.message.edit_text("⚙️ <b>Управление привычками</b>", reply_markup=manage_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "unpause_list")
async def cb_unpause_list(cb: CallbackQuery):
    habits = await get_habits(cb.from_user.id, include_paused=True)
    paused = [h for h in habits if h["is_paused"]]
    if not paused:
        await cb.answer("Нет привычек на паузе.", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(
        text=f"{h['emoji']} {h['name']}", callback_data=f"dounpause_{h['id']}"
    )] for h in paused]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="show_manage")])
    await cb.message.edit_text("▶️ Выбери привычку для возобновления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("dounpause_"))
async def cb_dounpause(cb: CallbackQuery):
    habit_id = int(cb.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT name, emoji FROM habits WHERE id=?", (habit_id,)) as cur:
            h = await cur.fetchone()
        await db.execute("UPDATE habits SET is_paused=0 WHERE id=?", (habit_id,))
        await db.commit()
    await cb.answer(f"▶️ {h['emoji']} {h['name']} — возобновлена!")
    await cb.message.edit_text("⚙️ <b>Управление привычками</b>", reply_markup=manage_kb(), parse_mode="HTML")


# ── Delete ────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "delete_list")
async def cb_delete_list(cb: CallbackQuery):
    habits = await get_habits(cb.from_user.id, include_paused=True)
    if not habits:
        await cb.answer("Нет привычек для удаления.", show_alert=True)
        return
    await cb.message.edit_text("🗑 Выбери привычку для удаления:", reply_markup=await delete_kb(cb.from_user.id))

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
        await cb.message.edit_text("🗑 Выбери привычку для удаления:", reply_markup=await delete_kb(cb.from_user.id))


# ── Statistics ────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "show_stats")
async def cb_show_stats(cb: CallbackQuery):
    habits = await get_habits(cb.from_user.id)
    if not habits:
        await cb.answer("Сначала добавь привычки!", show_alert=True)
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
            f"  {streak_icon} Стрик: {streak} дн. · Рекорд: {best} дн.\n"
        )
    lines.append("📅 <b>Календари</b>")
    for h in habits:
        stats = await get_monthly_stats(h["id"])
        cal = calendar_grid(stats["dates"], today.year, today.month)
        lines.append(f"\n{h['emoji']} {h['name']}\n<code>{cal}</code>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 История за неделю", callback_data="show_week")],
        [InlineKeyboardButton(text="🏆 Достижения", callback_data="show_achievements")],
        [InlineKeyboardButton(text="🎯 Цели на месяц", callback_data="show_goals")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
    ])
    await cb.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")


# ── Week ──────────────────────────────────────────────────────────────────────

async def _send_week(user_id: int, target):
    habits = await get_habits(user_id)
    if not habits:
        text = "Сначала добавь привычки!"
        if hasattr(target, 'edit_text'):
            await target.edit_text(text, reply_markup=main_menu_kb())
        else:
            await target.answer(text, reply_markup=main_menu_kb())
        return

    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    days = [week_start + timedelta(days=i) for i in range(7)]
    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    header = "  " + " ".join(f"{day_names[i]:>2}" for i in range(7))
    lines = [f"📅 <b>Неделя {week_start.strftime('%d.%m')} — {days[-1].strftime('%d.%m')}</b>\n", f"<code>{header}"]

    for h in habits:
        week_done = await get_week_completions(h["id"])
        row = f"{h['emoji']} "
        for d in days:
            mark = "✅" if d.isoformat() in week_done else ("·· " if d > today else "❌ ")
            row += mark
        lines.append(row)

    lines.append("</code>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="show_stats")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
    ])
    text = "\n".join(lines)
    if hasattr(target, 'edit_text'):
        await target.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "show_week")
async def cb_show_week(cb: CallbackQuery):
    await _send_week(cb.from_user.id, cb.message)


# ── Achievements ──────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "show_achievements")
async def cb_show_achievements(cb: CallbackQuery):
    habits = await get_habits(cb.from_user.id)
    if not habits:
        await cb.answer("Сначала добавь привычки!", show_alert=True)
        return
    earned = await get_user_achievements(cb.from_user.id)
    earned_ids = {(r["habit_id"], r["achievement_id"]) for r in earned}
    lines = ["🏆 <b>Достижения</b>\n"]
    for h in habits:
        streak = await get_streak(h["id"])
        lines.append(f"{h['emoji']} <b>{h['name']}</b> — стрик: {streak} дн.")
        for ach in ACHIEVEMENTS:
            if (h["id"], ach["id"]) in earned_ids:
                lines.append(f"  {ach['icon']} {ach['title']}")
            else:
                lines.append(f"  🔒 {ach['title']} (нужно {ach['streak']} дн.)")
        lines.append("")
    if not earned:
        lines.append("Пока нет достижений. Начни отмечать привычки каждый день! 💪")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="show_stats")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
    ])
    await cb.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")


# ── Goals ─────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "show_goals")
async def cb_show_goals(cb: CallbackQuery):
    habits = await get_habits(cb.from_user.id)
    if not habits:
        await cb.answer("Сначала добавь привычки!", show_alert=True)
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
            if done >= goal:
                status = "✅ Цель достигнута!"
            elif remaining <= days_left:
                status = f"📈 Осталось {remaining} дн. — успеваешь!"
            else:
                status = f"⚠️ Осталось {remaining} дн., но дней в месяце {days_left}"
            lines.append(f"{h['emoji']} <b>{h['name']}</b>\n  {bar} {done}/{goal}\n  {status}\n")
        else:
            lines.append(f"{h['emoji']} <b>{h['name']}</b>\n  Цель не установлена\n")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить цели", callback_data="edit_goals")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
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
    await cb.message.edit_text(
        "🎯 Сколько дней в этом месяце?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Каждый день ({days_in_month})", callback_data=f"newgoal_{days_in_month}")],
            [InlineKeyboardButton(text="20", callback_data="newgoal_20"),
             InlineKeyboardButton(text="15", callback_data="newgoal_15"),
             InlineKeyboardButton(text="10", callback_data="newgoal_10")],
            [InlineKeyboardButton(text="❌ Убрать цель", callback_data="newgoal_none")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="edit_goals")],
        ])
    )

@dp.callback_query(F.data.startswith("newgoal_"), SetGoal.waiting_days)
async def cb_newgoal(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    val = cb.data.split("_", 1)[1]
    goal = int(val) if val != "none" else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE habits SET monthly_goal=? WHERE id=?", (goal, data["habit_id"]))
        await db.commit()
    await state.clear()
    await cb.answer(f"✅ {'Цель: ' + str(goal) + ' дней' if goal else 'Цель убрана'}")
    await cb.message.edit_text("🎯 Выбери привычку:", reply_markup=await goals_kb(cb.from_user.id))

@dp.message(SetGoal.waiting_days)
async def fsm_setgoal_text(msg: Message, state: FSMContext):
    try:
        goal = int(msg.text.strip())
        if 1 <= goal <= 31:
            data = await state.get_data()
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE habits SET monthly_goal=? WHERE id=?", (goal, data["habit_id"]))
                await db.commit()
            await state.clear()
            await msg.answer(f"✅ Цель: {goal} дней!", reply_markup=main_menu_kb())
        else:
            await msg.answer("Введи число от 1 до 31.")
    except ValueError:
        await msg.answer("Введи число, например: 20")


# ── Reminders + weekly report ─────────────────────────────────────────────────

async def send_weekly_report(user_id: int):
    habits = await get_habits(user_id)
    if not habits:
        return
    today = date.today()
    week_start = today - timedelta(days=7)
    lines = [f"📊 <b>Итог недели {week_start.strftime('%d.%m')}–{today.strftime('%d.%m')}</b>\n"]
    for h in habits:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM completions WHERE habit_id=? AND completed_date > ? AND completed_date <= ?",
                (h["id"], week_start.isoformat(), today.isoformat())
            ) as cur:
                count = (await cur.fetchone())[0]
        streak = await get_streak(h["id"])
        bar = progress_bar(round(count / 7 * 100))
        lines.append(f"{h['emoji']} <b>{h['name']}</b>\n  {bar} {count}/7 дн. · 🔥 {streak}\n")
    try:
        await bot.send_message(user_id, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Weekly report failed for {user_id}: {e}")


async def check_reminders():
    while True:
        now = datetime.now()
        now_str = now.strftime("%H:%M")
        # Reminders
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
        # Weekly report — Sunday at 21:00
        if now.weekday() == 6 and now_str == "21:00":
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT DISTINCT user_id FROM habits WHERE is_active=1") as cur:
                    users = [r[0] for r in await cur.fetchall()]
            for uid in users:
                await send_weekly_report(uid)
        await asyncio.sleep(60)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    await init_db()
    asyncio.create_task(check_reminders())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())