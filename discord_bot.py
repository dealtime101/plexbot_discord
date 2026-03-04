
from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple

import aiohttp
import discord
from discord import app_commands

__version__ = "0.2.4"

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
# Helpers
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


def _norm(s: str) -> str:
    """Normalize for fuzzy matching: lowercase and remove non-alnum (so 'TV Shows' == 'tvshows')."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


# =========================
# Plex HTTP
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
# Plex: sections / now playing
# =========================
async def fetch_library_sections() -> List[Dict[str, str]]:
    root = await _plex_get_xml("/library/sections")
    out: List[Dict[str, str]] = []
    for d in root.findall("./Directory"):
        out.append(
            {
                "id": _safe(d.get("key")),
                "title": _safe(d.get("title")),
                "type": _safe(d.get("type")),  # movie / show / artist / photo...
            }
        )
    return out


def _match_section(query: str, section: Dict[str, str]) -> bool:
    q = _norm(query or "")
    if not q:
        return True

    sid = _safe(section.get("id"))
    title = section.get("title", "")

    if q.isdigit() and sid == q:
        return True

    return q in _norm(title)


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


# =========================
# Plex: recently added (season-vs-episode logic)
# =========================
@dataclass(frozen=True)
class RecentItem:
    added_at: int
    line: str
    section_title: str
    section_id: str
    kind: str  # "season" | "episode" | "movie" | other
    season_key: str = ""  # ratingKey of the season (for season items)
    episode_parent_season_key: str = ""  # parentRatingKey of the episode (to suppress if season is present)


def _format_recent_item(el: ET.Element) -> Optional[RecentItem]:
    """
    Handles:
      - Video movie
      - Video episode
      - Directory season (TV libraries often show "Season 1" blocks)
      - Directory show (fallback)
    """
    added_at = int(el.get("addedAt") or "0")
    tag = el.tag.lower()
    media_type = _safe(el.get("type"))

    section_title = _safe(el.get("librarySectionTitle"))
    section_id = _safe(el.get("librarySectionID"))

    if tag == "video":
        if media_type == "movie":
            title = _safe(el.get("title"))
            year = _safe(el.get("year"))
            line = f"🎬 {title} ({year})" if year else f"🎬 {title}"
            return RecentItem(added_at, line, section_title, section_id, kind="movie")

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
                pass

            parent_season_key = _safe(el.get("parentRatingKey"))  # season ratingKey
            line = f"📺 {show} — {se}{title}".strip()
            return RecentItem(
                added_at,
                line,
                section_title,
                section_id,
                kind="episode",
                episode_parent_season_key=parent_season_key,
            )

        return None

    if tag == "directory":
        # TV libraries often return seasons as Directory entries
        if media_type == "season":
            show = _safe(el.get("parentTitle")) or _safe(el.get("grandparentTitle"))
            season_index = _safe(el.get("index"))
            season_key = _safe(el.get("ratingKey")) or _safe(el.get("key"))

            if not show:
                return None

            if season_index.isdigit():
                line = f"📺 {show} — Season {int(season_index)}"
            else:
                season_title = _safe(el.get("title")) or "Season"
                line = f"📺 {show} — {season_title}"

            return RecentItem(
                added_at,
                line,
                section_title,
                section_id,
                kind="season",
                season_key=season_key,
            )

        if media_type == "show":
            title = _safe(el.get("title")) or _safe(el.get("name"))
            if not title:
                return None
            return RecentItem(added_at, f"📺 {title}", section_title, section_id, kind="show")

    return None


def _apply_season_preference(items: List[RecentItem]) -> List[RecentItem]:
    """
    Rule (as requested):
      - If a full season entry exists (Directory season), show the season entry.
      - Hide individual episodes that belong to that season (parentRatingKey matches season ratingKey).
      - If only episodes exist, show episodes.
    This is applied per library section.
    """
    # Collect season keys per section
    season_keys_by_section: Dict[str, set] = {}
    for it in items:
        if it.kind == "season" and it.season_key and it.section_id:
            season_keys_by_section.setdefault(it.section_id, set()).add(it.season_key)

    if not season_keys_by_section:
        return items

    filtered: List[RecentItem] = []
    for it in items:
        if it.kind == "episode" and it.section_id in season_keys_by_section:
            if it.episode_parent_season_key and it.episode_parent_season_key in season_keys_by_section[it.section_id]:
                continue  # season is present, suppress episodes from that season
        filtered.append(it)
    return filtered


async def fetch_recently_added(limit: int = 10, library: Optional[str] = None) -> list[str]:
    sections = await fetch_library_sections()

    if library:
        matched = [s for s in sections if _match_section(library, s)]
        if not matched:
            examples = ", ".join(s["title"] for s in sections[:10] if s.get("title"))
            raise RuntimeError(f"Aucune bibliothèque trouvée pour '{library}'. Ex: {examples}")

        merged: List[RecentItem] = []
        for s in matched:
            sid = s.get("id")
            if not sid:
                continue
            root = await _plex_get_xml(f"/library/sections/{sid}/recentlyAdded?X-Plex-Container-Size=200")
            for child in list(root):
                it = _format_recent_item(child)
                if it:
                    # section id/title may be missing in per-section results; fill them from section info
                    if not it.section_id:
                        it = RecentItem(it.added_at, it.line, it.section_title or s["title"], sid, it.kind, it.season_key, it.episode_parent_season_key)
                    merged.append(it)

        merged.sort(key=lambda x: x.added_at, reverse=True)
        merged = _apply_season_preference(merged)
        return [it.line for it in merged[:limit]]

    # No filter: global recentlyAdded (already sorted by addedAt descending)
    root = await _plex_get_xml("/library/recentlyAdded?X-Plex-Container-Size=250")
    parsed: List[RecentItem] = []
    for child in list(root):
        it = _format_recent_item(child)
        if it:
            parsed.append(it)

    parsed = _apply_season_preference(parsed)

    out: List[str] = []
    for it in parsed:
        line = it.line
        if it.section_title:
            line = f"{line}  _( {it.section_title} )_"
        out.append(line)
        if len(out) >= limit:
            break
    return out


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

    q = (current or "").strip()
    choices = []
    for s in sections:
        title = s.get("title", "")
        sid = s.get("id", "")
        if not title:
            continue
        if not q or _norm(q) in _norm(title) or (sid and q.strip() in sid):
            label = f"{title} ({sid})" if sid else title
            choices.append(app_commands.Choice(name=label[:100], value=title[:100]))
        if len(choices) >= 25:
            break
    return choices


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
