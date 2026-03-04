
from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from typing import Optional, List, Dict

import aiohttp
import discord
from discord import app_commands

__version__ = "0.2.2"

# =========================
# Env / Config
# =========================
DISCORD_TOKEN = (os.environ.get("PLEXBOT_DISCORD_TOKEN") or "").strip()
GUILD_ID = (os.environ.get("PLEXBOT_GUILD_ID") or "").strip()

PLEX_TOKEN = (os.environ.get("PLEXBOT_PLEX_TOKEN") or "").strip()
PLEX_BASE_URL = (os.environ.get("PLEXBOT_PLEX_BASE_URL") or "http://127.0.0.1:32400").strip()

if not DISCORD_TOKEN:
    raise RuntimeError("PLEXBOT_DISCORD_TOKEN manquant")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("PlexBot")


# =========================
# Small helpers
# =========================
def _safe(text: str | None) -> str:
    return (text or "").strip()


def _fmt_ms(ms: int) -> str:
    if ms <= 0:
        return "0:00"
    s = ms // 1000
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _pretty_state(state: str) -> str:
    s = (state or "").lower().strip()
    return {
        "playing": "▶️ Playing",
        "paused": "⏸️ Paused",
        "buffering": "⏳ Buffering",
    }.get(s, state or "Unknown")


# =========================
# Plex HTTP helpers
# =========================
async def _plex_get_xml(path: str) -> ET.Element:
    if not PLEX_TOKEN:
        raise RuntimeError("PLEXBOT_PLEX_TOKEN manquant (variable d’environnement Windows).")

    url = f"{PLEX_BASE_URL}{path}"
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}X-Plex-Token={PLEX_TOKEN}"

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Plex HTTP {resp.status}: {body[:200]}")
            xml_text = await resp.text()

    return ET.fromstring(xml_text)


# =========================
# Plex data fetchers
# =========================
async def fetch_library_sections() -> List[Dict[str, str]]:
    """
    Returns a list of Plex library sections like:
      [{"id":"2","title":"TV Shows","type":"show"}, ...]
    """
    root = await _plex_get_xml("/library/sections")
    out: List[Dict[str, str]] = []
    for d in root.findall("./Directory"):
        out.append(
            {
                "id": _safe(d.get("key")),
                "title": _safe(d.get("title")),
                "type": _safe(d.get("type")),  # movie / show / artist / photo etc.
            }
        )
    return out


async def fetch_plex_sessions() -> list[dict]:
    root = await _plex_get_xml("/status/sessions")
    sessions: list[dict] = []

    for v in root.findall("./Video"):
        media_type = _safe(v.get("type"))  # movie / episode
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
                pass
            display_title = f"{show} — {se}{title}"
        else:
            display_title = title

        sessions.append(
            {
                "user": user or "Unknown",
                "state": state or "unknown",
                "title": display_title,
                "progress": f"{_fmt_ms(view_offset)} / {_fmt_ms(duration)}",
                "quality": " ".join(
                    x for x in [resolution, (vcodec.upper() if vcodec else ""), container] if x
                ).strip(),
            }
        )

    return sessions


def _match_section(query: str, section: Dict[str, str]) -> bool:
    q = (query or "").strip().lower()
    if not q:
        return True
    # allow ID match
    if q.isdigit() and section.get("id") == q:
        return True
    # title contains match
    return q in (section.get("title", "").lower())


async def fetch_recently_added(limit: int = 10, library: Optional[str] = None) -> list[str]:
    """
    Fetch recently added items.
    If library is provided, filter by Plex library section title (case-insensitive substring) OR by section id.
    """
    sections = await fetch_library_sections()
    matched_sections = [s for s in sections if _match_section(library or "", s)] if library else sections

    if library and not matched_sections:
        raise RuntimeError(
            f"Aucune bibliothèque trouvée pour '{library}'. "
            f"Exemples: {', '.join(s['title'] for s in sections[:8])}"
        )

    allowed_ids = {s["id"] for s in matched_sections if s.get("id")}
    # Pull a larger page then filter down
    root = await _plex_get_xml("/library/recentlyAdded?X-Plex-Container-Size=150")

    items: list[str] = []

    for el in root.findall("./Video"):
        section_id = _safe(el.get("librarySectionID"))
        section_title = _safe(el.get("librarySectionTitle"))

        if allowed_ids and section_id and section_id not in allowed_ids:
            continue

        media_type = _safe(el.get("type"))

        if media_type == "movie":
            title = _safe(el.get("title"))
            year = _safe(el.get("year"))
            prefix = "🎬"
            line = f"{prefix} {title} ({year})" if year else f"{prefix} {title}"

        elif media_type == "episode":
            show = _safe(el.get("grandparentTitle"))
            title = _safe(el.get("title"))
            season = el.get("parentIndex")
            ep = el.get("index")

            se = ""
            try:
                if season and ep:
                    se = f"S{int(season):02d}E{int(ep):02d} "
            except ValueError:
                pass

            prefix = "📺"
            line = f"{prefix} {show} — {se}{title}"

        else:
            continue

        # Include library label if user didn't filter (helps when you have many libraries)
        if not library and section_title:
            line = f"{line}  _( {section_title} )_"

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
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Connecté en tant que %s", self.user)


bot = PlexBot()


# Autocomplete for library names in /plex_recent
@bot.tree.command(name="plex_recent", description="Affiche les derniers ajouts Plex (option: bibliothèque)")
@app_commands.describe(library="Nom ou ID de bibliothèque (ex: Movies, TV Shows, Anime)", limit="Nombre d’items (1-15)")
async def plex_recent(interaction: discord.Interaction, library: Optional[str] = None, limit: Optional[int] = 10):
    await interaction.response.defer(ephemeral=True)

    try:
        n = int(limit) if limit is not None else 10
    except (TypeError, ValueError):
        n = 10
    n = max(1, min(15, n))

    try:
        items = await fetch_recently_added(limit=n, library=library)
    except Exception as e:
        await interaction.followup.send(f"Erreur Plex: {e}", ephemeral=True)
        return

    if not items:
        await interaction.followup.send("Aucun élément récent trouvé.", ephemeral=True)
        return

    title = "🆕 **Plex — Recently Added**"
    if library:
        title += f" _(filter: {library})_"
    lines = [title]
    for it in items:
        lines.append(f"• {it}")

    msg = "\n".join(lines)
    if len(msg) > 1900:
        msg = msg[:1900] + "\n…(tronqué)"

    await interaction.followup.send(msg, ephemeral=True)


@plex_recent.autocomplete("library")
async def plex_recent_library_autocomplete(interaction: discord.Interaction, current: str):
    try:
        sections = await fetch_library_sections()
    except Exception:
        return []

    q = (current or "").lower().strip()
    choices = []
    for s in sections:
        title = s.get("title", "")
        sid = s.get("id", "")
        if not title:
            continue
        if not q or q in title.lower() or (sid and q in sid):
            # Show "Title (id)" to make it easy to pick
            label = f"{title} ({sid})" if sid else title
            # value should be something we can match; use title
            choices.append(app_commands.Choice(name=label[:100], value=title[:100]))
        if len(choices) >= 25:
            break
    return choices


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


@bot.tree.command(name="plex_ping", description="Test PlexBot")
async def plex_ping(interaction: discord.Interaction):
    await interaction.response.send_message("PlexBot Pong ✅", ephemeral=True)


@bot.tree.command(name="plex_status", description="Statut du bot Plex")
async def plex_status(interaction: discord.Interaction):
    await interaction.response.send_message(f"PlexBot Online 🎬 (v{__version__})", ephemeral=True)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
