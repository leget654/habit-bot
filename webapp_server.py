"""
Web server providing REST API for the Telegram Mini App.
Validates Telegram WebApp initData and serves habit data.
"""
import json
import logging
from datetime import date, timedelta

from aiohttp import web
from init_data_py import InitData

logger = logging.getLogger(__name__)


def validate_init_data(init_data: str, bot_token: str) -> dict | None:
    """Validate Telegram WebApp initData signature using the init-data-py library.
    Returns the parsed user dict, or None if invalid/missing.
    """
    if not init_data:
        logger.warning("validate_init_data: empty init_data received")
        return None
    try:
        data = InitData.parse(init_data)
        is_valid = data.validate(bot_token, lifetime=86400)
        if not is_valid:
            logger.warning("validate_init_data: signature invalid (library validate() returned False)")
            return None
        if not data.user:
            logger.warning("validate_init_data: signature OK but no user field")
            return None
        return json.loads(data.user.to_json())
    except Exception as e:
        logger.warning(f"initData validation failed: {e}")
        return None


def make_app(bot_token: str, db_helpers: dict, dev_mode: bool = False):
    """
    db_helpers: dict of async functions imported from bot.py, e.g.:
        get_habits, get_today_completions, toggle_completion, get_streak,
        get_best_streak, get_monthly_stats, get_week_completions,
        check_and_grant_achievements, get_user_xp, add_xp, get_level,
        get_leaderboard, get_user_rank, ensure_user, create_habit, xp_per_habit
    """
    app = web.Application()

    def get_user_id(request) -> int | None:
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        logger.info(f"get_user_id: init_data length={len(init_data)}")
        user = validate_init_data(init_data, bot_token)
        if user:
            return user.get("id")
        if dev_mode:
            # Allow a fixed dev user id for local testing without Telegram
            return int(request.headers.get("X-Dev-User-Id", "0")) or None
        return None

    async def handle_index(request):
        return web.FileResponse("webapp/index.html")

    async def handle_get_habits(request):
        user_id = get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        habits = await db_helpers["get_habits"](user_id)
        today_done = await db_helpers["get_today_completions"](user_id)
        today = date.today()

        result = []
        for h in habits:
            streak = await db_helpers["get_streak"](h["id"])
            week_done = await db_helpers["get_week_completions"](h["id"])
            week_start = today - timedelta(days=today.weekday())
            week_bools = [(week_start + timedelta(days=i)).isoformat() in week_done for i in range(7)]
            goal_text = None
            if h["monthly_goal"]:
                stats = await db_helpers["get_monthly_stats"](h["id"])
                goal_text = f"{stats['completed']}/{h['monthly_goal']} дн. в этом месяце"
            result.append({
                "id": h["id"],
                "name": h["name"],
                "emoji": h["emoji"],
                "streak": streak,
                "done_today": h["id"] in today_done,
                "week": week_bools,
                "goal_text": goal_text,
            })

        return web.json_response({"habits": result, "today_done": len(today_done)})

    async def handle_create_habit(request):
        user_id = get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await request.json()
        name = (body.get("name") or "").strip()[:64]
        emoji = (body.get("emoji") or "✅").strip()[:8]
        if not name:
            return web.json_response({"error": "name required"}, status=400)
        await db_helpers["ensure_user"](user_id, body.get("first_name", "User"))
        await db_helpers["create_habit"](user_id, name, emoji)
        return web.json_response({"ok": True})

    async def handle_toggle_habit(request):
        user_id = get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)
        habit_id = int(request.match_info["habit_id"])

        is_done = await db_helpers["toggle_completion"](user_id, habit_id)
        xp_earned = 0
        if is_done:
            streak = await db_helpers["get_streak"](habit_id)
            xp_earned = 10 + streak * 5
            await db_helpers["add_xp"](user_id, xp_earned)
            await db_helpers["check_and_grant_achievements"](user_id, habit_id, streak)

        return web.json_response({"done": is_done, "xp_earned": xp_earned})

    async def handle_stats(request):
        user_id = get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        habits = await db_helpers["get_habits"](user_id)
        xp = await db_helpers["get_user_xp"](user_id)
        level_num, level_name, next_xp = db_helpers["get_level"](xp)

        total_completed = 0
        best_streak = 0
        goals = []
        today = date.today()
        first_day = today.replace(day=1)
        days_in_month = today.day

        # Build heatmap (aggregate across all habits, normalized 0-3)
        day_counts = {}
        for h in habits:
            stats = await db_helpers["get_monthly_stats"](h["id"])
            total_completed += stats["completed"]
            best = await db_helpers["get_best_streak"](h["id"])
            best_streak = max(best_streak, best)
            for d in stats["dates"]:
                day_counts[d] = day_counts.get(d, 0) + 1
            if h["monthly_goal"]:
                goals.append({
                    "name": h["name"], "emoji": h["emoji"],
                    "done": stats["completed"], "goal": h["monthly_goal"]
                })

        heatmap = []
        for i in range(days_in_month):
            d = (first_day + timedelta(days=i)).isoformat()
            count = day_counts.get(d, 0)
            level = 0 if count == 0 else (1 if count == 1 else (2 if count <= 3 else 3))
            heatmap.append(level)

        return web.json_response({
            "total_completed": total_completed,
            "best_streak": best_streak,
            "level_name": level_name,
            "xp": xp,
            "heatmap": heatmap,
            "goals": goals,
        })

    async def handle_leaderboard(request):
        user_id = get_user_id(request)
        if not user_id:
            return web.json_response({"error": "unauthorized"}, status=401)

        leaders = await db_helpers["get_leaderboard"](10)
        rank = await db_helpers["get_user_rank"](user_id)
        xp = await db_helpers["get_user_xp"](user_id)
        level_num, level_name, next_xp = db_helpers["get_level"](xp)

        prev_xp = 0
        for req, _ in db_helpers["LEVELS"]:
            if req <= xp:
                prev_xp = req
        span = (next_xp - prev_xp) if next_xp else 1
        progress_pct = round(((xp - prev_xp) / span) * 100) if next_xp else 100

        top = [{
            "name": row["display_name"] or "Аноним",
            "xp": row["total_xp"],
            "is_me": row["user_id"] == user_id,
        } for row in leaders]

        return web.json_response({
            "me": {
                "name": "Я", "rank": rank, "xp": xp,
                "level_name": level_name, "next_xp": next_xp,
                "progress_pct": progress_pct,
            },
            "top": top,
        })

    app.router.add_get("/", handle_index)
    app.router.add_get("/api/habits", handle_get_habits)
    app.router.add_post("/api/habits", handle_create_habit)
    app.router.add_post("/api/habits/{habit_id}/toggle", handle_toggle_habit)
    app.router.add_get("/api/stats", handle_stats)
    app.router.add_get("/api/leaderboard", handle_leaderboard)
    app.router.add_static("/static", "webapp", show_index=False)

    return app


async def run_webapp(bot_token: str, db_helpers: dict, port: int = 8080, dev_mode: bool = False):
    app = make_app(bot_token, db_helpers, dev_mode=dev_mode)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Mini app server running on port {port}")
    return runner
