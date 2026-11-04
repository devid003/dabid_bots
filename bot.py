import io
import json
import logging
import os
import time
import urllib.parse
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("imagebot")

TOKEN = os.environ["DISCORD_TOKEN"]
ALLOWED_GUILDS = {
    int(x.strip())
    for x in os.getenv("ALLOWED_GUILD_IDS", "").split(",")
    if x.strip()
}
PROVIDER = os.getenv("IMAGE_PROVIDER", "pollinations").lower() 
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
POLLINATIONS_TOKEN = os.getenv("POLLINATIONS_TOKEN", "")
MIN_INTERVAL_MIN = int(os.getenv("MIN_INTERVAL_MINUTES", "10"))

JOBS_FILE = Path("jobs.json")
jobs: dict[str, dict] = json.loads(JOBS_FILE.read_text()) if JOBS_FILE.exists() else {}


def save_jobs():
    JOBS_FILE.write_text(json.dumps(jobs, indent=2))


class LockedTree(app_commands.CommandTree):
    """Blocks every command outside the allowed servers (and in DMs)."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id in ALLOWED_GUILDS:
            return True
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "This bot is private and not available in this server.", ephemeral=True
            )
        return False


class ImageBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = LockedTree(self)

    async def setup_hook(self):
        for gid in ALLOWED_GUILDS:
            guild = discord.Object(id=gid)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        scheduler.start()

    async def leave_unauthorized(self):
        for g in list(self.guilds):
            if g.id not in ALLOWED_GUILDS:
                log.warning("Leaving unauthorized guild %s (%s)", g.name, g.id)
                await g.leave()

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        # await self.leave_unauthorized()

    async def on_guild_join(self, guild: discord.Guild):
        if guild.id not in ALLOWED_GUILDS:
            log.warning("Invited to unauthorized guild %s, leaving", guild.id)
            pass


bot = ImageBot()


async def generate_image(prompt: str) -> bytes:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
        if PROVIDER == "openai":
            import base64

            async with s.post(
                "https://api.openai.com/v1/images/generations",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={"model": OPENAI_MODEL, "prompt": prompt, "size": "1024x1024"},
            ) as r:
                data = await r.json()
                if r.status != 200:
                    raise RuntimeError(data.get("error", {}).get("message", "OpenAI error"))
                item = data["data"][0]
                if "b64_json" in item:
                    return base64.b64decode(item["b64_json"])
                async with s.get(item["url"]) as img:
                    return await img.read()
        # Free, no key needed
        if POLLINATIONS_TOKEN:
            url = "https://gen.pollinations.ai/image/" + urllib.parse.quote(prompt)
            params = {"width": 1024, "height": 1024, "key": POLLINATIONS_TOKEN}
        else:
            url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
            params = {"width": 1024, "height": 1024, "nologo": "true"}
        async with s.get(url, params=params) as r:
            if r.status != 200:
                raise RuntimeError(f"Image service returned {r.status}")
            return await r.read()


def to_file(data: bytes) -> discord.File:
    return discord.File(io.BytesIO(data), filename="image.png")


@bot.tree.command(name="imagine", description="Generate a picture from a prompt")
@app_commands.guild_only()
@app_commands.describe(prompt="What should the picture show?")
@app_commands.checks.cooldown(1, 15.0, key=lambda i: (i.guild_id, i.user.id))
async def imagine(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer(thinking=True)
    try:
        data = await generate_image(prompt)
    except Exception as e:
        await interaction.followup.send(f"Generation failed: {e}")
        return
    await interaction.followup.send(f"**Prompt:** {prompt[:500]}", file=to_file(data))


auto = app_commands.Group(
    name="autoimage",
    description="Automatic scheduled image posting",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@auto.command(name="start", description="Post a generated image on a schedule")
@app_commands.describe(
    channel="Where to post",
    prompt="Prompt used for every image",
    every_minutes="Interval in minutes",
)
async def auto_start(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    prompt: str,
    every_minutes: app_commands.Range[int, 1, 10080],
):
    if every_minutes < MIN_INTERVAL_MIN:
        await interaction.response.send_message(
            f"Minimum interval is {MIN_INTERVAL_MIN} minutes.", ephemeral=True
        )
        return
    key = f"{interaction.guild_id}:{channel.id}"
    jobs[key] = {
        "guild_id": interaction.guild_id,
        "channel_id": channel.id,
        "prompt": prompt,
        "interval": every_minutes * 60,
        "next_run": time.time() + every_minutes * 60,
    }
    save_jobs()
    await interaction.response.send_message(
        f"Automation set: posting in {channel.mention} every {every_minutes} min."
    )


@auto.command(name="stop", description="Stop the automation in a channel")
async def auto_stop(interaction: discord.Interaction, channel: discord.TextChannel):
    key = f"{interaction.guild_id}:{channel.id}"
    if jobs.pop(key, None):
        save_jobs()
        await interaction.response.send_message(f"Stopped automation in {channel.mention}.")
    else:
        await interaction.response.send_message("No automation in that channel.", ephemeral=True)


@auto.command(name="list", description="Show active automations")
async def auto_list(interaction: discord.Interaction):
    mine = [j for j in jobs.values() if j["guild_id"] == interaction.guild_id]
    if not mine:
        await interaction.response.send_message("No active automations.", ephemeral=True)
        return
    lines = [f"<#{j['channel_id']}> every {j['interval'] // 60} min: {j['prompt'][:80]}" for j in mine]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


bot.tree.add_command(auto)


@bot.tree.error
async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Slow down, try again in {error.retry_after:.0f}s."
    else:
        log.exception("Command error", exc_info=error)
        msg = "Something went wrong."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


@tasks.loop(seconds=30)
async def scheduler():
    now = time.time()
    for key, job in list(jobs.items()):
        if job["guild_id"] not in ALLOWED_GUILDS:
            jobs.pop(key, None)
            save_jobs()
            continue
        if now < job["next_run"]:
            continue
        job["next_run"] = now + job["interval"]
        save_jobs()
        channel = bot.get_channel(job["channel_id"])
        if channel is None:
            continue
        try:
            data = await generate_image(job["prompt"])
            await channel.send(f"**Auto:** {job['prompt'][:500]}", file=to_file(data))
        except Exception:
            log.exception("Automation failed for %s", key)


bot.run(TOKEN)
