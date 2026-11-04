
"""Discord bot: only works in servers and for members approved in the dashboard."""
import asyncio
import base64
import io
import logging
import os
import time
import urllib.parse

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import db

load_dotenv()
log = logging.getLogger("imagebot")

TOKEN = os.getenv("DISCORD_TOKEN", "")
OWNER_IDS = {
    int(x) for x in os.getenv("OWNER_IDS", "").split(",")
    if x.strip().isdigit()
}
PROVIDER = os.getenv("IMAGE_PROVIDER", "pollinations").lower()
POLLINATIONS_TOKEN = os.getenv("POLLINATIONS_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
MIN_INTERVAL_MIN = int(os.getenv("MIN_INTERVAL_MINUTES", "10"))
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT_PER_USER", "0"))

run = asyncio.to_thread


def clean(msg: str) -> str:
    """Never let a secret end up in logs or the dashboard."""
    for secret in (POLLINATIONS_TOKEN, OPENAI_API_KEY, TOKEN):
        if secret:
            msg = msg.replace(secret, "***")
    return msg[:300]


async def deny(interaction: discord.Interaction, text: str):
    if not interaction.response.is_done():
        await interaction.response.send_message(text, ephemeral=True)


async def log_use(interaction, command, prompt, status, ms, error=""):
    g = interaction.guild
    await run(
        db.log_usage,
        interaction.user.id,
        str(interaction.user),
        interaction.guild_id,
        g.name if g else "",
        command,
        prompt,
        status,
        ms,
        error,
    )


# ---------- access control ----------
class LockedTree(app_commands.CommandTree):
    """Runs before every command: server must be allowed AND member approved."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = await run(db.allowed_guild_ids)

        if interaction.guild_id is None or interaction.guild_id not in allowed:
            await deny(
                interaction,
                "This bot is private and isn't available in this server.",
            )
            return False

        uid = interaction.user.id

        if uid in OWNER_IDS:
            return True

        status = await run(db.member_status, uid)

        if status == "approved":
            return True

        cmd = interaction.command.qualified_name if interaction.command else "?"

        if status == "denied":
            await log_use(
                interaction, cmd, "", "denied", 0, "access denied"
            )
            await deny(
                interaction,
                "Your access to this bot was declined by the owner.",
            )
            return False

        await run(
            db.request_access,
            uid,
            str(interaction.user),
            interaction.guild_id,
        )
        await log_use(
            interaction, cmd, "", "denied", 0, "awaiting approval"
        )
        await deny(
            interaction,
            "🔒 You're not approved yet. I sent an access request "
            "to the owner. You can use the bot once it's approved.",
        )
        return False


class ImageBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = LockedTree(self)

    async def setup_hook(self):
        reconcile.start()
        scheduler.start()

    async def on_ready(self):
        log.info("Bot logged in as %s", self.user)

    async def on_guild_join(self, guild: discord.Guild):
        await do_reconcile()


bot = ImageBot()
synced: set[int] = set()


async def do_reconcile():
    """Leave servers that aren't approved; sync commands into approved ones."""
    allowed = await run(db.allowed_guild_ids)

    for g in list(bot.guilds):
        if g.id not in allowed:
            log.warning(
                "Leaving unapproved server %s (%s)", g.name, g.id
            )
            await g.leave()
            continue

        if g.id not in synced:
            try:
                obj = discord.Object(id=g.id)
                bot.tree.copy_global_to(guild=obj)
                await bot.tree.sync(guild=obj)
                synced.add(g.id)
                await run(db.set_guild_name, g.id, g.name)
                log.info("Commands synced to %s", g.name)
            except Exception:
                log.exception("Could not sync commands to %s", g.name)

    synced.intersection_update(allowed)


@tasks.loop(seconds=30)
async def reconcile():
    try:
        await do_reconcile()
    except Exception:
        log.exception("reconcile failed")


@reconcile.before_loop
async def _wait_reconcile():
    await bot.wait_until_ready()


# ---------- image generation ----------
async def generate_image(prompt: str) -> bytes:
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=120)
    ) as s:

        if PROVIDER == "openai":
            async with s.post(
                "https://api.openai.com/v1/images/generations",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}"
                },
                json={
                    "model": OPENAI_MODEL,
                    "prompt": prompt,
                    "size": "1024x1024",
                },
            ) as r:
                data = await r.json()

                if r.status != 200:
                    raise RuntimeError(
                        f"Image service returned {r.status}"
                    )

                item = data["data"][0]

                if "b64_json" in item:
                    return base64.b64decode(item["b64_json"])

                async with s.get(item["url"]) as img:
                    return await img.read()

       
        url = (
            "https://image.pollinations.ai/prompt/"
            + urllib.parse.quote(prompt)
        )
        params = {
            "width": 1024,
            "height": 1024,
            "nologo": "true",
        }

        async with s.get(url, params=params) as r:
            if r.status != 200:
                raise RuntimeError(
                    f"Image service returned {r.status}"
                )
            return await r.read()


def to_file(data: bytes) -> discord.File:
    return discord.File(
        io.BytesIO(data),
        filename="image.png",
    )


def ms_since(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


# ---------- commands ----------
@bot.tree.command(
    name="imagine",
    description="Generate a picture from a prompt",
)
@app_commands.guild_only()
@app_commands.describe(prompt="What should the picture show?")
@app_commands.checks.cooldown(
    1, 15.0, key=lambda i: (i.guild_id, i.user.id)
)
async def imagine(
    interaction: discord.Interaction,
    prompt: app_commands.Range[str, 1, 1000],
):
    uid = interaction.user.id

    if DAILY_LIMIT and uid not in OWNER_IDS:
        used = await run(db.count_recent_ok, uid, 24)

        if used >= DAILY_LIMIT:
            await log_use(
                interaction,
                "imagine",
                prompt,
                "denied",
                0,
                "daily limit",
            )
            await interaction.response.send_message(
                f"You've reached the limit of "
                f"{DAILY_LIMIT} images per 24 hours.",
                ephemeral=True,
            )
            return

    await interaction.response.defer(thinking=True)
    t0 = time.monotonic()

    try:
        data = await generate_image(prompt)
    except Exception as e:
        await log_use(
            interaction,
            "imagine",
            prompt,
            "error",
            ms_since(t0),
            clean(repr(e)),
        )
        reason = f" ({e})" if isinstance(e, RuntimeError) else ""
        await interaction.followup.send(
            f"Generation failed{reason}. Please try again in a moment."
        )
        return

    await interaction.followup.send(
        f"**Prompt:** {prompt[:500]}",
        file=to_file(data),
    )
    await log_use(
        interaction, "imagine", prompt, "ok", ms_since(t0)
    )


auto = app_commands.Group(
    name="autoimage",
    description="Automatic scheduled image posting",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@auto.command(
    name="start",
    description="Post a generated image on a schedule",
)
@app_commands.describe(
    channel="Where to post",
    prompt="Prompt used for every image",
    every_minutes="Interval in minutes",
)
async def auto_start(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    prompt: app_commands.Range[str, 1, 1000],
    every_minutes: app_commands.Range[int, 1, 10080],
):
    if every_minutes < MIN_INTERVAL_MIN:
        await interaction.response.send_message(
            f"Minimum interval is {MIN_INTERVAL_MIN} minutes.",
            ephemeral=True,
        )
        return

    perms = channel.permissions_for(interaction.guild.me)

    if not (perms.send_messages and perms.attach_files):
        await interaction.response.send_message(
            f"I need Send Messages and Attach Files in {channel.mention}.",
            ephemeral=True,
        )
        return

    await run(
        db.upsert_job,
        interaction.guild_id,
        channel.id,
        prompt,
        every_minutes * 60,
        interaction.user.id,
    )
    await interaction.response.send_message(
        f"Automation set: posting in {channel.mention} "
        f"every {every_minutes} min."
    )


@auto.command(
    name="stop",
    description="Stop the automation in a channel",
)
async def auto_stop(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
):
    if await run(db.remove_job, interaction.guild_id, channel.id):
        await interaction.response.send_message(
            f"Stopped automation in {channel.mention}."
        )
    else:
        await interaction.response.send_message(
            "No automation in that channel.",
            ephemeral=True,
        )


@auto.command(
    name="list",
    description="Show active automations",
)
async def auto_list(interaction: discord.Interaction):
    jobs = await run(db.list_jobs, interaction.guild_id)

    if not jobs:
        await interaction.response.send_message(
            "No active automations.",
            ephemeral=True,
        )
        return

    lines = [
        f"<#{j.channel_id}> every {j.interval_s // 60} min: "
        f"{j.prompt[:80]}"
        for j in jobs
    ]
    await interaction.response.send_message(
        "\n".join(lines),
        ephemeral=True,
    )


bot.tree.add_command(auto)


@bot.tree.error
async def on_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Slow down, try again in {error.retry_after:.0f}s."
    else:
        log.exception("Command error", exc_info=error)
        msg = "Something went wrong."

    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(
            msg, ephemeral=True
        )


# ---------- scheduler ----------
@tasks.loop(seconds=30)
async def scheduler():
    try:
        allowed = await run(db.allowed_guild_ids)
        now = time.time()

        for job in await run(db.due_jobs, now):
            await run(db.bump_job, job.id, now + job.interval_s)

            if job.guild_id not in allowed:
                continue

            if (
                job.created_by not in OWNER_IDS
                and await run(db.member_status, job.created_by)
                != "approved"
            ):
                continue  # creator lost access, so skip

            channel = bot.get_channel(job.channel_id)

            if channel is None:
                continue

            t0 = time.monotonic()

            try:
                data = await generate_image(job.prompt)
                await channel.send(
                    f"**Auto:** {job.prompt[:500]}",
                    file=to_file(data),
                )
                status, err = "ok", ""
            except Exception as e:
                status, err = "error", clean(repr(e))

            await run(
                db.log_usage,
                job.created_by,
                "automation",
                job.guild_id,
                channel.guild.name,
                "auto",
                job.prompt,
                status,
                ms_since(t0),
                err,
            )

    except Exception:
        log.exception("scheduler failed")


@scheduler.before_loop
async def _wait_scheduler():
    await bot.wait_until_ready()
