"""Database layer. Uses SQLite locally and Postgres when DATABASE_URL is set."""
import datetime as dt
import os
import time

from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger, DateTime, Float, Integer, String, Text,
    case, create_engine, delete, func, select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

load_dotenv()


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


_url = os.getenv("DATABASE_URL", "sqlite:///data.db")
if _url.startswith("postgres://"):
    _url = "postgresql://" + _url[len("postgres://"):]
if _url.startswith("postgresql://"):
    _url = "postgresql+psycopg2://" + _url[len("postgresql://"):]
    
IS_SQLITE = _url.startswith("sqlite")

engine = create_engine(
    _url,
    pool_pre_ping=True,
    pool_recycle=300,
    **({"connect_args": {"check_same_thread": False}} if IS_SQLITE else {}),
)
Session = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Server(Base):
    __tablename__ = "servers"
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(200), default="")
    added_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Member(Base):
    __tablename__ = "members"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|approved|denied
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    requested_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class UsageLog(Base):
    __tablename__ = "usage_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str] = mapped_column(String(200), default="")
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    guild_name: Mapped[str] = mapped_column(String(200), default="")
    command: Mapped[str] = mapped_column(String(50), default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok|error|denied
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    prompt: Mapped[str] = mapped_column(Text, default="")
    interval_s: Mapped[int] = mapped_column(Integer, default=600)
    next_run: Mapped[float] = mapped_column(Float, default=0.0)
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)


def init() -> None:
    Base.metadata.create_all(engine)


# ---------- servers ----------
def allowed_guild_ids() -> set[int]:
    with Session() as s:
        return set(s.scalars(select(Server.guild_id)))


def add_server(guild_id: int, name: str = "") -> None:
    with Session.begin() as s:
        if s.get(Server, guild_id) is None:
            s.add(Server(guild_id=guild_id, name=name))


def remove_server(guild_id: int) -> None:
    with Session.begin() as s:
        s.execute(delete(Server).where(Server.guild_id == guild_id))
        s.execute(delete(Job).where(Job.guild_id == guild_id))


def set_guild_name(guild_id: int, name: str) -> None:
    with Session.begin() as s:
        sv = s.get(Server, guild_id)
        if sv:
            sv.name = name


# ---------- members ----------
def member_status(user_id: int) -> str | None:
    with Session() as s:
        m = s.get(Member, user_id)
        return m.status if m else None


def request_access(user_id: int, username: str, guild_id: int | None) -> str:
    with Session.begin() as s:
        m = s.get(Member, user_id)
        if m is None:
            s.add(Member(user_id=user_id, username=username, status="pending", guild_id=guild_id))
            return "pending"
        m.username = username
        return m.status


def set_member(user_id: int, status: str, username: str | None = None) -> None:
    with Session.begin() as s:
        m = s.get(Member, user_id)
        if status == "remove":
            if m:
                s.delete(m)
            return
        if m is None:
            m = Member(user_id=user_id, username=username or "")
            s.add(m)
        m.status = status
        m.decided_at = None if status == "pending" else utcnow()
        if username:
            m.username = username


# ---------- usage ----------
def log_usage(user_id, username, guild_id, guild_name, command, prompt, status, duration_ms, error=""):
    with Session.begin() as s:
        s.add(UsageLog(
            user_id=user_id, username=username, guild_id=guild_id, guild_name=guild_name,
            command=command, prompt=(prompt or "")[:500], status=status,
            duration_ms=int(duration_ms), error=(error or "")[:300],
        ))
        m = s.get(Member, user_id)
        if m is not None and username:
            m.username = username


def count_recent_ok(user_id: int, hours: int = 24) -> int:
    since = utcnow() - dt.timedelta(hours=hours)
    with Session() as s:
        return s.scalar(
            select(func.count()).select_from(UsageLog).where(
                UsageLog.user_id == user_id, UsageLog.status == "ok", UsageLog.ts >= since
            )
        ) or 0


def usage_summary(since: dt.datetime | None) -> list[dict]:
    ok = func.sum(case((UsageLog.status == "ok", 1), else_=0))
    err = func.sum(case((UsageLog.status == "error", 1), else_=0))
    den = func.sum(case((UsageLog.status == "denied", 1), else_=0))
    avg_ok = func.avg(case((UsageLog.status == "ok", UsageLog.duration_ms)))
    q = (
        select(
            UsageLog.user_id,
            func.max(UsageLog.username).label("username"),
            func.count().label("total"),
            ok.label("ok"), err.label("errors"), den.label("denied"),
            avg_ok.label("avg_ms"),
            func.min(UsageLog.ts).label("first"),
            func.max(UsageLog.ts).label("last"),
        )
        .group_by(UsageLog.user_id)
        .order_by(func.count().desc())
    )
    if since:
        q = q.where(UsageLog.ts >= since)
    with Session() as s:
        return [dict(r._mapping) for r in s.execute(q)]


def recent_logs(since: dt.datetime | None, user_id: int | None = None, limit: int = 200) -> list[UsageLog]:
    q = select(UsageLog).order_by(UsageLog.ts.desc()).limit(limit)
    if since:
        q = q.where(UsageLog.ts >= since)
    if user_id:
        q = q.where(UsageLog.user_id == user_id)
    with Session() as s:
        return list(s.scalars(q))


# ---------- automations ----------
def upsert_job(guild_id, channel_id, prompt, interval_s, created_by) -> None:
    with Session.begin() as s:
        j = s.scalar(select(Job).where(Job.guild_id == guild_id, Job.channel_id == channel_id))
        if j is None:
            j = Job(guild_id=guild_id, channel_id=channel_id)
            s.add(j)
        j.prompt, j.interval_s, j.created_by = prompt, interval_s, created_by
        j.next_run = time.time() + interval_s


def remove_job(guild_id: int, channel_id: int) -> bool:
    with Session.begin() as s:
        res = s.execute(delete(Job).where(Job.guild_id == guild_id, Job.channel_id == channel_id))
        return res.rowcount > 0


def list_jobs(guild_id: int) -> list[Job]:
    with Session() as s:
        return list(s.scalars(select(Job).where(Job.guild_id == guild_id)))


def due_jobs(now: float) -> list[Job]:
    with Session() as s:
        return list(s.scalars(select(Job).where(Job.next_run <= now)))


def bump_job(job_id: int, next_run: float) -> None:
    with Session.begin() as s:
        j = s.get(Job, job_id)
        if j:
            j.next_run = next_run


# ---------- dashboard ----------
def overview() -> dict:
    since = utcnow() - dt.timedelta(hours=24)
    with Session() as s:
        servers = list(s.scalars(select(Server).order_by(Server.added_at)))
        members = list(s.scalars(select(Member).order_by(Member.requested_at.desc())))
        gen24 = s.scalar(
            select(func.count()).select_from(UsageLog).where(UsageLog.ts >= since, UsageLog.status == "ok")
        ) or 0
        jobs = s.scalar(select(func.count()).select_from(Job)) or 0
    return {"servers": servers, "members": members, "gen24": gen24, "jobs": jobs}
