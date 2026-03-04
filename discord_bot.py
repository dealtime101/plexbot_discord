
from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from typing import Optional

import aiohttp
import discord
from discord import app_commands

# =========================
# Version
# =========================
__version__ = "0.2.0"

# =========================
# Env / Config
# =========================
DISCORD_TOKEN = (os.environ.get("PLEXBOT_DISCORD_TOKEN") or "").strip()
GUILD_ID = (os.environ.get("PLEXBOT_GUILD_ID") or "").strip()

PLEX_TOKEN = (os.environ.get("PLEXBOT_PLEX_TOKEN") or "").strip()
PLEX_BASE_URL = (os.environ.get("PLEXBOT_PLEX_BASE_URL") or "http://127.0.0.1:32400").strip()

if not DISCORD_TOKEN:
    raise RuntimeError("PLEXBOT_DISCORD_TOKEN manquant (variable d’environnement Windows).")

# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("PlexBot")


# =========================
# Plex helpers
# =========================
def _fmt_ms(ms: int) -> str:
    if ms <= 0:
        return "0:00"
    s = ms // 1000
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _safe(text: str | None) -> str:
    return (text or "").strip()


def _pretty_state(state: str) -> str:
    s = (state or "").lower().strip()
    return {
        "playing": "▶️ Playing",
        "paused": "⏸️ Paused",
        "buffering": "⏳ Buffering",
    }.get(s, f"• {state}" if state else "• Unknown")


def _emoji_for_type(media_type: str) -> str:
    t = (media_type or "").lower().strip()
    if t == "movie":
        return "🎬"
    if t == "episode":
        return "📺"
    if t in ("track", "music"):
        return "🎵"
    return "📦"


async def _plex_get_xml(path: str) -> ET.Element:
    if not PLEX_TOKEN:
        raise RuntimeError("PLEXBOT_PLEX_TOKEN manquant (variable d’environnement Windows).")

    url = f"{PLEX_BASE_URL}{path}"
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}X-Plex-Token={PLEX_TOKEN}"

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Plex HTTP {resp.status}: {body[:200]}")
            xml_text = await resp.text()

    return ET.fromstring(xml_text)


async def fetch_plex_sessions() -> list[dict]:
    root = await _plex_get_xml("/status/sessions")

    sessions: list[dict] = []
    for v in root.findall("./Video"):
        media_type = _safe(v.get("type"))
        title = _safe(v.get("title"))

        show = _safe(v.get("grandparentTitle"))
        season = v.get("parentIndex")
        ep = v.get("index")

        user_el = v.find("./User")
        user = _safe(user_el.get("title") if user_el is not None else "")

        player_el = v.find("./Player")
        state = _safe(player_el.get("state") if player_el is not None else "")

        duration = int(v.get("duration") or "0")
        view_offset = int(v.get("viewOffset") or "0")

        media_el = v.find("./Media")
        resolution = _safe(media_el.get("videoResolution") if media_el is not None else "")
        container = _safe(media_el.get("container") if media_el is not None else "")
        vcodec = _safe(media_el.get("videoCodec") if media_el is not None else "")

        if media_type == "episode" and show:
            se = ""
            try:
                if season and ep:
                    se = f"S{int(season):02d}E{int(ep):02d} "
            except ValueError:
                se = ""
            display_title = f"{show} — {se}{title}".strip()
        else:
            display_title = title

        sessions.append(
            {
                "user": user or "Unknown",
                "state": state or "unknown",
                "title": display_title or "Unknown title",
                "progress": f"{_fmt_ms(view_offset)} / {_fmt_ms(duration)}",
                "quality": " ".join(
                    x for x in [resolution, (vcodec.upper() if vcodec else ""), container] if x
                ).strip(),
            }
        )

    return sessions


async def fetch_recently_added(limit: int = 10) -> list[str]:
    # Plex supports X-Plex-Container-Start/Size for pagination, but a simple limit works for most.
    # We'll request a bit more and then trim, in case Plex returns mixed items.
    size = max(10, min(50, limit))
    root = await _plex_get_xml(f"/library/recentlyAdded?X-Plex-Container-Start=0&X-Plex-Container-Size={size}")

    items: list[str] = []
    for el in root:
        # Items can be <Video> (movie/episode) or <Directory> (season/show) in some setups.
        tag = el.tag.lower()
        if tag not in ("video", "directory"):
            continue

        media_type = _safe(el.get("type"))

        if media_type == "episode":
            show = _safe(el.get("grandparentTitle"))
            title = _safe(el.get("title"))
            season = el.get("parentIndex")
            ep = el.get("index")

            se = ""
            try:
                if season and ep:
                    se = f"S{int(season):02d}E{int(ep):02d} "
            except ValueError:
                se = ""
            line = f"{_emoji_for_type(media_type)} {show} — {se}{title}".strip()

        elif media_type == "movie":
            title = _safe(el.get("title"))
            year = _safe(el.get("year"))
            line = f"{_emoji_for_type(media_type)} {title}{f' ({year})' if year else ''}".strip()

        else:
            # Fallback for other types
            title = _safe(el.get("title")) or _safe(el.get("name"))
            line = f"{_emoji_for_type(media_type)} {title}".strip()

        if line:
            items.append(line)
        if len(items) >= limit:
            break

    return items


# =========================
# Discord bot
# =========================
class PlexBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Slash commands sync (guild): %s", len(synced))
        else:
            synced = await self.tree.sync()
            log.info("Slash commands sync (global): %s", len(synced))

    async def on_ready(self) -> None:
        log.info("Connecté en tant que %s (id=%s)", self.user, self.user.id)


bot = PlexBot()

# =========================
# Slash commands
# =========================
@bot.tree.command(name="plex_ping", description="Test PlexBot")
async def plex_ping(interaction: discord.Interaction):
    await interaction.response.send_message("PlexBot Pong ✅", ephemeral=True)


@bot.tree.command(name="plex_status", description="Statut du bot Plex")
async def plex_status(interaction: discord.Interaction):
    await interaction.response.send_message(f"PlexBot Online 🎬 (v{__version__})", ephemeral=True)


@bot.tree.command(name="plex_playing", description="Affiche ce qui joue présentement sur Plex")
async def plex_playing(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        sessions = await fetch_plex_sessions()
    except Exception as e:
        await interaction.followup.send(f"Erreur Plex: {e}", ephemeral=True)
        return

    if not sessions:
        await interaction.followup.send("Aucune lecture en cours sur Plex.", ephemeral=True)
        return

    sessions = sessions[:10]
    lines = ["🎬 **Plex — Now Playing**"]

    for s in sessions:
        lines.append(
            f"\n**{s['user']}** — {_pretty_state(s['state'])}\n"
            f"🎞️ {s['title']}\n"
            f"⏱️ {s['progress']}\n"
            f"📺 {s['quality'] or 'n/a'}"
        )

    msg = "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1900] + "\n…(tronqué)"

    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="plex_recent", description="Affiche les derniers ajouts sur Plex")
@app_commands.describe(limit="Nombre d’items à afficher (1-15)")
async def plex_recent(interaction: discord.Interaction, limit: Optional[int] = 10):
    await interaction.response.defer(ephemeral=True)

    try:
        n = int(limit) if limit is not None else 10
    except (TypeError, ValueError):
        n = 10
    n = max(1, min(15, n))

    try:
        items = await fetch_recently_added(limit=n)
    except Exception as e:
        await interaction.followup.send(f"Erreur Plex: {e}", ephemeral=True)
        return

    if not items:
        await interaction.followup.send("Aucun élément récent trouvé.", ephemeral=True)
        return

    lines = ["🆕 **Plex — Recently Added**"]
    for it in items:
        lines.append(f"• {it}")

    msg = "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1900] + "\n…(tronqué)"

    await interaction.followup.send(msg, ephemeral=True)


# =========================
# Entry
# =========================
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
