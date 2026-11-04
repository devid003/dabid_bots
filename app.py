"""Admin dashboard + Discord bot in ONE process (so one Render service runs both)."""
import asyncio
import csv
import datetime as dt
import hmac
import io
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("panel")

import db  # noqa: E402
import discord_bot  # noqa: E402

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    raise SystemExit("Set ADMIN_PASSWORD in your .env file (this is the dashboard login password).")
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)
if not os.getenv("SECRET_KEY"):
    log.warning("SECRET_KEY not set: dashboard logins will reset every restart.")
if os.getenv("RENDER") and db.IS_SQLITE:
    log.warning("DATABASE_URL is not set: data will be LOST on every restart/deploy on Render!")

try:
    TZ = ZoneInfo(os.getenv("DISPLAY_TZ", "Asia/Manila"))
except Exception:
    TZ = dt.timezone.utc

run = asyncio.to_thread
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def local(v):
    if v is None:
        return ""
    return v.replace(tzinfo=dt.timezone.utc).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")


def secs(ms):
    return "" if ms is None else f"{float(ms) / 1000:.1f}s"


templates.env.filters["local"] = local
templates.env.filters["secs"] = secs
TZ_LABEL = os.getenv("DISPLAY_TZ", "Asia/Manila")


# ---------- helpers ----------
def authed(request) -> bool:
    return request.session.get("auth") is True


def csrf_token(request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(16)
    return request.session["csrf"]


def redirect(url: str):
    return RedirectResponse(url, status_code=303)


def flash(request, msg: str):
    request.session["flash"] = msg


async def guarded_post(request):
    """Returns (form, None) if allowed, else (None, response)."""
    if not authed(request):
        return None, redirect("/login")
    form = await request.form()
    if not hmac.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "")):
        return None, PlainTextResponse("Bad CSRF token. Go back and reload the page.", status_code=400)
    return form, None


def parse_id(value) -> int | None:
    s = str(value or "").strip()
    return int(s) if s.isdigit() and 15 <= len(s) <= 20 else None


PERIODS = [("24h", "Last 24h"), ("7d", "7 days"), ("30d", "30 days"), ("all", "All time")]
PERIOD_DELTA = {"24h": dt.timedelta(hours=24), "7d": dt.timedelta(days=7), "30d": dt.timedelta(days=30), "all": None}


def since_for(period: str):
    delta = PERIOD_DELTA.get(period, PERIOD_DELTA["7d"])
    return db.utcnow() - delta if delta else None


def csv_safe(v) -> str:
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


# ---------- auth ----------
FAILS: list[float] = []


async def login(request):
    error = None
    status = 200
    if request.method == "POST":
        form = await request.form()
        now = time.time()
        FAILS[:] = [t for t in FAILS if now - t < 300]
        if len(FAILS) >= 10:
            error, status = "Too many attempts. Wait 5 minutes.", 429
        elif hmac.compare_digest(str(form.get("password", "")).encode(), ADMIN_PASSWORD.encode()):
            request.session.clear()
            request.session["auth"] = True
            csrf_token(request)
            return redirect("/")
        else:
            FAILS.append(now)
            error, status = "Wrong password.", 401
    return templates.TemplateResponse(request=request, name="login.html", context={"error": error}, status_code=status)


async def logout(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    request.session.clear()
    return redirect("/login")


# ---------- pages ----------
async def index(request):
    if not authed(request):
        return redirect("/login")
    data = await run(db.overview)
    b = discord_bot.bot
    ready = b.is_ready()
    bot_info = {
        "online": ready,
        "name": str(b.user) if b.user else "not connected",
        "guilds": len(b.guilds) if ready else 0,
        "ping": round(b.latency * 1000) if ready and b.latency == b.latency else None,
    }
    members = data["members"]
    ctx = {
        "csrf": csrf_token(request),
        "flash": request.session.pop("flash", None),
        "bot": bot_info,
        "servers": data["servers"],
        "members": members,
        "pending": [m for m in members if m.status == "pending"],
        "approved_count": sum(1 for m in members if m.status == "approved"),
        "gen24": data["gen24"],
        "jobs": data["jobs"],
        "tz": TZ_LABEL,
    }
    return templates.TemplateResponse(request=request, name="index.html", context=ctx)


async def usage(request):
    if not authed(request):
        return redirect("/login")
    period = request.query_params.get("period", "7d")
    if period not in PERIOD_DELTA:
        period = "7d"
    user = parse_id(request.query_params.get("user"))
    since = since_for(period)
    summary = await run(db.usage_summary, since)
    logs = await run(db.recent_logs, since, user, 200)
    ctx = {
        "csrf": csrf_token(request), "periods": PERIODS, "period": period, "user": user,
        "summary": summary, "logs": logs, "tz": TZ_LABEL,
    }
    return templates.TemplateResponse(request=request, name="usage.html", context=ctx)


async def usage_csv(request):
    if not authed(request):
        return redirect("/login")
    period = request.query_params.get("period", "7d")
    user = parse_id(request.query_params.get("user"))
    logs = await run(db.recent_logs, since_for(period), user, 5000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "user_id", "username", "server", "command", "status", "duration_s", "prompt", "error"])
    for r in logs:
        w.writerow([
            local(r.ts), r.user_id, csv_safe(r.username), csv_safe(r.guild_name), r.command, r.status,
            f"{r.duration_ms / 1000:.1f}", csv_safe(r.prompt), csv_safe(r.error),
        ])
    return Response(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="usage-log.csv"'},
    )


# ---------- actions ----------
async def servers_add(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    gid = parse_id(form.get("guild_id"))
    if gid is None:
        flash(request, "That doesn't look like a server ID (it's a long number, 17 to 20 digits).")
    else:
        await run(db.add_server, gid, str(form.get("name", "")).strip()[:200])
        flash(request, f"Server {gid} approved. Invite the bot to it (or, if it's already there, wait ~30 seconds).")
    return redirect("/")


async def servers_remove(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    gid = parse_id(form.get("guild_id"))
    if gid:
        await run(db.remove_server, gid)
        flash(request, f"Server {gid} removed. The bot will leave it within ~30 seconds.")
    return redirect("/")


async def members_set(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    uid = parse_id(form.get("user_id"))
    status = str(form.get("status", ""))
    if uid is None or status not in ("approved", "denied", "pending", "remove"):
        flash(request, "That doesn't look like a valid user ID.")
    else:
        await run(db.set_member, uid, status, str(form.get("username", "")).strip()[:200] or None)
        flash(request, f"User {uid}: {status}.")
    return redirect("/")


async def health(request):
    return PlainTextResponse("ok")


@asynccontextmanager
async def lifespan(app):
    await run(db.init)
    task = None
    if discord_bot.TOKEN:
        task = asyncio.create_task(discord_bot.bot.start(discord_bot.TOKEN))
        task.add_done_callback(lambda t: t.cancelled() or (t.exception() and log.error("Bot stopped: %r", t.exception())))
    else:
        log.error("DISCORD_TOKEN is not set. The dashboard works but the bot is offline.")
    yield
    await discord_bot.bot.close()


app = Starlette(
    routes=[
        Route("/", index),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/usage", usage),
        Route("/usage.csv", usage_csv),
        Route("/servers/add", servers_add, methods=["POST"]),
        Route("/servers/remove", servers_remove, methods=["POST"]),
        Route("/members/set", members_set, methods=["POST"]),
        Route("/health", health),
    ],
    middleware=[
        Middleware(
            SessionMiddleware, secret_key=SECRET_KEY, same_site="lax",
            https_only=bool(os.getenv("RENDER")), max_age=60 * 60 * 12,
        )
    ],
    lifespan=lifespan,
)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
