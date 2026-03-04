from __future__ import annotations

import datetime as dt
import logging
import os
import random
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from typing import Optional, List, Dict, Tuple

import aiohttp
import discord
from discord import app_commands

__version__ = "0.4.0"

# =========================
# Env / Config
# =========================
DISCORD_TOKEN = (os.environ.get("PLEXBOT_DISCORD_TOKEN") or "").strip()
GUILD_ID = (os.environ.get("PLEXBOT_GUILD_ID") or "").strip()

PLEX_TOKEN = (os.environ.get("PLEXBOT_PLEX_TOKEN") or "").strip()
PLEX_BASE_URL = (os.environ.get("PLEXBOT_PLEX_BASE_URL") or "http://127.0.0.1:32400").strip()

RECENT_SEASON_COLLAPSE_THRESHOLD = int(os.environ.get("PLEXBOT_RECENT_SEASON_COLLAPSE_THRESHOLD") or "5")
HISTORY_PAGE_SIZE = int(os.environ.get("PLEXBOT_HISTORY_PAGE_SIZE") or "200")

if not DISCORD_TOKEN:
    raise RuntimeError("PLEXBOT_DISCORD_TOKEN manquant (variable d’environnement Windows).")

# =========================
# Logging
# =========================
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("PlexBot")


# =========================
# Helpers
# =========================
def _safe(text: str | None) -> str:
    return (text or "").strip()


def _to_int(s: str | None, default: int = 0) -> int:
    try:
        return int(s or "")
    except Exception:
        return default


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
    return {"playing": "▶️ Playing", "paused": "⏸️ Paused", "buffering": "⏳ Buffering"}.get(s, state or "Unknown")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _header(title: str) -> str:
    return f"{title}  _(v{__version__})_"


def _plex_url(path: str) -> str:
    """Build a Plex URL (with token) for relative paths like /library/metadata/..."""
    if not path:
        return ""
    # Sometimes thumb/art comes without leading slash
    p = path if path.startswith("/") else ("/" + path)
    url = f"{PLEX_BASE_URL}{p}"
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}X-Plex-Token={PLEX_TOKEN}"


# =========================
# Plex HTTP
# =========================
async def _plex_get_xml(path: str) -> ET.Element:
    if not PLEX_TOKEN:
        raise RuntimeError("PLEXBOT_PLEX_TOKEN manquant (variable d’environnement Windows).")

    url = f"{PLEX_BASE_URL}{path}"
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}X-Plex-Token={PLEX_TOKEN}"

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=18)) as session:
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
        out.append({"id": _safe(d.get("key")), "title": _safe(d.get("title")), "type": _safe(d.get("type"))})
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
        media_type = _safe(v.get("type"))
        title = _safe(v.get("title"))
        show = _safe(v.get("grandparentTitle"))
        season = v.get("parentIndex")
        ep = v.get("index")

        user_el = v.find("./User")
        user = _safe(user_el.get("title") if user_el is not None else "")

        player_el = v.find("./Player")
        state = _safe(player_el.get("state") if player_el is not None else "")

        duration = _to_int(v.get("duration"))
        view_offset = _to_int(v.get("viewOffset"))

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
                "quality": " ".join(x for x in [resolution, (vcodec.upper() if vcodec else ""), container] if x).strip(),
            }
        )

    return sessions


# =========================
# Plex: recently added (smart collapsing)
# =========================
@dataclass(frozen=True)
class RecentItem:
    added_at: int
    line: str
    section_title: str
    section_id: str
    kind: str  # movie/episode/season
    season_key: str = ""
    episode_parent_season_key: str = ""
    show_title: str = ""
    season_index: str = ""


def _format_recent_item(el: ET.Element) -> Optional[RecentItem]:
    added_at = _to_int(el.get("addedAt"))
    tag = el.tag.lower()
    media_type = _safe(el.get("type"))
    section_title = _safe(el.get("librarySectionTitle"))
    section_id = _safe(el.get("librarySectionID"))

    if tag == "video":
        if media_type == "movie":
            title = _safe(el.get("title"))
            year = _safe(el.get("year"))
            line = f"🎬 {title} ({year})" if year else f"🎬 {title}"
            return RecentItem(added_at, line, section_title, section_id, "movie")

        if media_type == "episode":
            show = _safe(el.get("grandparentTitle"))
            title = _safe(el.get("title"))
            season = _safe(el.get("parentIndex"))
            ep = _safe(el.get("index"))

            se = ""
            try:
                if season.isdigit() and ep.isdigit():
                    se = f"S{int(season):02d}E{int(ep):02d} "
            except ValueError:
                pass

            parent_season_key = _safe(el.get("parentRatingKey"))
            line = f"📺 {show} — {se}{title}".strip()
            return RecentItem(
                added_at,
                line,
                section_title,
                section_id,
                "episode",
                episode_parent_season_key=parent_season_key,
                show_title=show,
                season_index=season,
            )
        return None

    if tag == "directory" and media_type == "season":
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

        return RecentItem(added_at, line, section_title, section_id, "season", season_key=season_key, show_title=show, season_index=season_index)

    return None


def _collapse_episodes_to_seasons(items: List[RecentItem], threshold: int) -> List[RecentItem]:
    if not items:
        return items

    explicit_by_section: Dict[str, set] = {}
    for it in items:
        if it.kind == "season" and it.season_key and it.section_id:
            explicit_by_section.setdefault(it.section_id, set()).add(it.season_key)

    episode_groups: Dict[Tuple[str, str], List[RecentItem]] = {}
    for it in items:
        if it.kind == "episode" and it.section_id and it.episode_parent_season_key:
            episode_groups.setdefault((it.section_id, it.episode_parent_season_key), []).append(it)

    synthetic: List[RecentItem] = []
    suppressed: set[int] = set()

    for (section_id, parent_season_key), eps in episode_groups.items():
        if section_id in explicit_by_section and parent_season_key in explicit_by_section[section_id]:
            for e in eps:
                suppressed.add(id(e))
            continue

        if len(eps) >= threshold:
            newest = max(eps, key=lambda x: x.added_at)
            show = newest.show_title or "Unknown Show"
            season_index = newest.season_index
            if season_index.isdigit():
                line = f"📺 {show} — Season {int(season_index)} ({len(eps)} eps)"
            else:
                line = f"📺 {show} — Season ({len(eps)} eps)"
            synthetic.append(
                RecentItem(
                    newest.added_at,
                    line,
                    newest.section_title,
                    section_id,
                    "season",
                    season_key=parent_season_key,
                    show_title=show,
                    season_index=season_index,
                )
            )
            for e in eps:
                suppressed.add(id(e))

    kept = [it for it in items if not (it.kind == "episode" and id(it) in suppressed)]
    kept.extend(synthetic)
    kept.sort(key=lambda x: x.added_at, reverse=True)
    return kept


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
            root = await _plex_get_xml(f"/library/sections/{sid}/recentlyAdded?X-Plex-Container-Size=250")
            for child in list(root):
                it = _format_recent_item(child)
                if it:
                    if not it.section_id:
                        it = replace(it, section_id=sid)
                    if not it.section_title:
                        it = replace(it, section_title=s["title"])
                    merged.append(it)

        merged.sort(key=lambda x: x.added_at, reverse=True)
        merged = _collapse_episodes_to_seasons(merged, threshold=RECENT_SEASON_COLLAPSE_THRESHOLD)
        return [it.line for it in merged[:limit]]

    root = await _plex_get_xml("/library/recentlyAdded?X-Plex-Container-Size=300")
    parsed: List[RecentItem] = []
    for child in list(root):
        it = _format_recent_item(child)
        if it:
            parsed.append(it)

    parsed = _collapse_episodes_to_seasons(parsed, threshold=RECENT_SEASON_COLLAPSE_THRESHOLD)
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
# Plex: library stats
# =========================
async def fetch_library_stats() -> List[Tuple[str, int]]:
    sections = await fetch_library_sections()
    stats: List[Tuple[str, int]] = []
    for s in sections:
        sid = s.get("id")
        title = s.get("title") or f"Library {sid}"
        if not sid:
            continue
        root = await _plex_get_xml(f"/library/sections/{sid}/all?X-Plex-Container-Start=0&X-Plex-Container-Size=0")
        total = _to_int(root.get("totalSize"), default=_to_int(root.get("size"), default=0))
        stats.append((title, total))
    stats.sort(key=lambda x: x[0].lower())
    return stats


# =========================
# Plex: search (hubs)
# =========================
def _format_search_hit(el: ET.Element) -> Optional[str]:
    tag = el.tag.lower()
    media_type = _safe(el.get("type"))
    title = _safe(el.get("title"))
    section = _safe(el.get("librarySectionTitle"))

    if tag == "video":
        if media_type == "movie":
            year = _safe(el.get("year"))
            base = f"🎬 {title} ({year})" if year else f"🎬 {title}"
            return f"{base}  _( {section} )_" if section else base
        if media_type == "episode":
            show = _safe(el.get("grandparentTitle"))
            season = _safe(el.get("parentIndex"))
            ep = _safe(el.get("index"))
            se = f"S{int(season):02d}E{int(ep):02d} " if (season.isdigit() and ep.isdigit()) else ""
            base = f"📺 {show} — {se}{title}".strip()
            return f"{base}  _( {section} )_" if section else base
        if title:
            base = f"🎞️ {title}"
            return f"{base}  _( {section} )_" if section else base

    if tag == "directory":
        if media_type == "show":
            base = f"📺 {title}"
            return f"{base}  _( {section} )_" if section else base
        if media_type == "season":
            show = _safe(el.get("parentTitle")) or _safe(el.get("grandparentTitle"))
            idx = _safe(el.get("index"))
            if show and idx.isdigit():
                base = f"📺 {show} — Season {int(idx)}"
                return f"{base}  _( {section} )_" if section else base
        if title:
            base = f"📁 {title}"
            return f"{base}  _( {section} )_" if section else base
    return None


async def plex_search(query: str, limit: int = 10, library: Optional[str] = None) -> List[ET.Element]:
    """Returns raw Plex elements (Video/Directory) for best hits."""
    query = (query or "").strip()
    if not query:
        return []

    library_norm = _norm(library or "") if library else ""
    q = urllib.parse.quote(query)

    root = await _plex_get_xml(f"/hubs/search?query={q}&X-Plex-Container-Size=50")
    hits: List[ET.Element] = []
    seen: set[str] = set()

    for hub in root.findall("./Hub"):
        for el in list(hub):
            if library_norm:
                section = _safe(el.get("librarySectionTitle"))
                if library_norm not in _norm(section):
                    continue

            rid = _safe(el.get("ratingKey")) or _safe(el.get("key")) or _format_search_hit(el) or ""
            if not rid:
                continue
            k = rid.lower()
            if k in seen:
                continue
            seen.add(k)
            hits.append(el)
            if len(hits) >= limit:
                return hits

    # fallback
    root2 = await _plex_get_xml(f"/search?query={q}&X-Plex-Container-Size=50")
    for el in list(root2):
        if library_norm:
            section = _safe(el.get("librarySectionTitle"))
            if library_norm not in _norm(section):
                continue
        rid = _safe(el.get("ratingKey")) or _safe(el.get("key")) or _format_search_hit(el) or ""
        if not rid:
            continue
        k = rid.lower()
        if k in seen:
            continue
        seen.add(k)
        hits.append(el)
        if len(hits) >= limit:
            break

    return hits


# =========================
# Plex: info / metadata
# =========================
def _pick_best_search_hit(query: str, hits: List[ET.Element]) -> Optional[ET.Element]:
    if not hits:
        return None
    qn = _norm(query)
    # Prefer show/movie with exact-ish title match
    best = None
    best_score = -1
    for el in hits:
        t = _safe(el.get("title")) or _safe(el.get("grandparentTitle"))
        tn = _norm(t)
        mtype = _safe(el.get("type"))
        score = 0
        if tn == qn:
            score += 100
        if qn and qn in tn:
            score += 40
        if mtype in ("show", "movie"):
            score += 20
        if mtype == "episode":
            score -= 10
        if score > best_score:
            best_score = score
            best = el
    return best or hits[0]


async def fetch_metadata(rating_key: str) -> Optional[ET.Element]:
    if not rating_key:
        return None
    root = await _plex_get_xml(f"/library/metadata/{rating_key}")
    # Usually first child is Video/Directory
    for child in list(root):
        return child
    return None


def _metadata_to_embed(el: ET.Element) -> discord.Embed:
    mtype = _safe(el.get("type"))
    title = _safe(el.get("title"))
    year = _safe(el.get("year"))
    section = _safe(el.get("librarySectionTitle"))

    if mtype == "show":
        display = f"{title} ({year})" if year else title
        embed = discord.Embed(title=f"📺 Plex — Info  _(v{__version__})_", description=f"**{display}**")
        embed.add_field(name="Library", value=section or "n/a", inline=True)
        seasons = _safe(el.get("childCount"))
        episodes = _safe(el.get("leafCount"))
        if seasons:
            embed.add_field(name="Seasons", value=seasons, inline=True)
        if episodes:
            embed.add_field(name="Episodes", value=episodes, inline=True)

    else:
        # movie or other
        display = f"{title} ({year})" if year else title
        emoji = "🎬" if mtype == "movie" else "🎞️"
        embed = discord.Embed(title=f"{emoji} Plex — Info  _(v{__version__})_", description=f"**{display}**")
        embed.add_field(name="Library", value=section or "n/a", inline=True)
        if mtype:
            embed.add_field(name="Type", value=mtype, inline=True)

    # Rating (audienceRating is common)
    rating = _safe(el.get("audienceRating")) or _safe(el.get("rating"))
    if rating:
        embed.add_field(name="Rating", value=str(rating), inline=True)

    # Genres
    genres = [g.get("tag") for g in el.findall("./Genre") if g.get("tag")]
    if genres:
        embed.add_field(name="Genres", value=", ".join(genres)[:1024], inline=False)

    # Summary
    summary = _safe(el.get("summary"))
    if summary:
        # Discord field value max 1024; use description extension instead
        embed.add_field(name="Summary", value=summary[:1024], inline=False)

    # Poster/thumb
    thumb = _safe(el.get("thumb")) or _safe(el.get("art"))
    if thumb and PLEX_TOKEN:
        embed.set_thumbnail(url=_plex_url(thumb))

    embed.set_footer(text=f"PlexBot v{__version__}")
    return embed


# =========================
# Plex: onDeck
# =========================
def _format_ondeck_item(el: ET.Element) -> str:
    mtype = _safe(el.get("type"))
    if mtype == "episode":
        show = _safe(el.get("grandparentTitle"))
        title = _safe(el.get("title"))
        season = _safe(el.get("parentIndex"))
        ep = _safe(el.get("index"))
        se = f"S{int(season):02d}E{int(ep):02d} " if (season.isdigit() and ep.isdigit()) else ""
        return f"📺 {show} — {se}{title}".strip()
    if mtype == "movie":
        title = _safe(el.get("title"))
        year = _safe(el.get("year"))
        return f"🎬 {title} ({year})" if year else f"🎬 {title}"
    title = _safe(el.get("title")) or "Unknown"
    return f"🎞️ {title}"


async def fetch_ondeck(limit: int = 10, library: Optional[str] = None) -> List[str]:
    limit = max(1, min(15, limit))
    root = await _plex_get_xml(f"/library/onDeck?X-Plex-Container-Size=100")
    items: List[ET.Element] = [x for x in list(root) if x.tag.lower() in ("video", "directory")]

    if library:
        ln = _norm(library)
        items = [x for x in items if ln in _norm(_safe(x.get("librarySectionTitle")))]

    # Sort by addedAt/updatedAt if available (otherwise keep order)
    def _sort_key(x: ET.Element) -> int:
        return _to_int(x.get("updatedAt"), _to_int(x.get("addedAt"), 0))

    items.sort(key=_sort_key, reverse=True)

    out: List[str] = []
    for el in items:
        out.append(_format_ondeck_item(el))
        if len(out) >= limit:
            break
    return out


# =========================
# Plex: random
# =========================
async def fetch_random_item(library: Optional[str] = None) -> Optional[ET.Element]:
    sections = await fetch_library_sections()
    if library:
        matched = [s for s in sections if _match_section(library, s)]
        if not matched:
            examples = ", ".join(s["title"] for s in sections[:10] if s.get("title"))
            raise RuntimeError(f"Aucune bibliothèque trouvée pour '{library}'. Ex: {examples}")
        section = random.choice(matched)
    else:
        # pick a random library (prefer video libraries)
        video_sections = [s for s in sections if (s.get("type") in ("movie", "show") or s.get("type"))]
        section = random.choice(video_sections or sections)

    sid = section.get("id")
    if not sid:
        return None

    # Plex supports sort=random
    root = await _plex_get_xml(f"/library/sections/{sid}/all?sort=random&X-Plex-Container-Start=0&X-Plex-Container-Size=1")
    for child in list(root):
        return child
    return None


# =========================
# Plex: history-based stats (best-effort)
# =========================
async def _fetch_history_page(start: int, size: int) -> ET.Element:
    return await _plex_get_xml(f"/status/sessions/history/all?X-Plex-Container-Start={start}&X-Plex-Container-Size={size}")


async def fetch_activity(days: int = 1) -> Dict[str, object]:
    cutoff = int((dt.datetime.utcnow() - dt.timedelta(days=days)).timestamp())
    start = 0
    size = max(50, min(500, HISTORY_PAGE_SIZE))
    total_streams = 0
    users: Dict[str, int] = {}
    titles: Dict[str, int] = {}

    while True:
        root = await _fetch_history_page(start, size)
        items = root.findall("./Video")
        if not items:
            break
        for v in items:
            viewed_at = _to_int(v.get("viewedAt"))
            if viewed_at and viewed_at < cutoff:
                return {
                    "streams": total_streams,
                    "unique_users": len(users),
                    "top_title": max(titles.items(), key=lambda x: x[1])[0] if titles else None,
                    "days": days,
                }

            total_streams += 1
            user_el = v.find("./User")
            user = _safe(user_el.get("title") if user_el is not None else "") or "Unknown"
            users[user] = users.get(user, 0) + 1

            media_type = _safe(v.get("type"))
            if media_type == "episode":
                show = _safe(v.get("grandparentTitle")) or "Unknown"
                titles[show] = titles.get(show, 0) + 1
            else:
                title = _safe(v.get("title")) or "Unknown"
                titles[title] = titles.get(title, 0) + 1

        start += size
        if start >= 3000:
            break

    return {
        "streams": total_streams,
        "unique_users": len(users),
        "top_title": max(titles.items(), key=lambda x: x[1])[0] if titles else None,
        "days": days,
    }


async def fetch_top_users(days: int = 30, limit: int = 10) -> List[Tuple[str, int]]:
    cutoff = int((dt.datetime.utcnow() - dt.timedelta(days=days)).timestamp())
    start = 0
    size = max(50, min(500, HISTORY_PAGE_SIZE))
    counts: Dict[str, int] = {}

    while True:
        root = await _fetch_history_page(start, size)
        items = root.findall("./Video")
        if not items:
            break
        for v in items:
            viewed_at = _to_int(v.get("viewedAt"))
            if viewed_at and viewed_at < cutoff:
                return sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]
            user_el = v.find("./User")
            user = _safe(user_el.get("title") if user_el is not None else "") or "Unknown"
            counts[user] = counts.get(user, 0) + 1
        start += size
        if start >= 5000:
            break

    return sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]


# =========================
# Discord bot
# =========================
class PlexBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
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


# =========================
# Slash commands
# =========================
@bot.tree.command(name="plex_ping", description="Test PlexBot")
async def plex_ping(interaction: discord.Interaction):
    await interaction.response.send_message(_header("🏓 PlexBot Pong ✅"), ephemeral=True)


@bot.tree.command(name="plex_status", description="Statut du bot Plex")
async def plex_status(interaction: discord.Interaction):
    await interaction.response.send_message(_header("🎬 PlexBot Online"), ephemeral=True)


@bot.tree.command(name="plex_version", description="Affiche la version de PlexBot")
async def plex_version(interaction: discord.Interaction):
    await interaction.response.send_message(_header("📦 PlexBot Version"), ephemeral=True)


@bot.tree.command(name="plex_help", description="Aide et commandes disponibles")
async def plex_help(interaction: discord.Interaction):
    desc = "\n".join(
        [
            "**Core**",
            "• `/plex_status` — Bot status",
            "• `/plex_playing` — Now playing sessions",
            "• `/plex_recent` — Recently added (library filter + season collapse)",
            "• `/plex_search` — Search in Plex",
            "• `/plex_info` — Info (poster + summary)",
            "• `/plex_random` — Random pick (optional library)",
            "• `/plex_ondeck` — Continue watching / On Deck",
            "• `/plex_library_stats` — Library item counts",
            "• `/plex_activity` — Activity stats (history)",
            "• `/plex_users` — Top users (history)",
            "• `/plex_version` — Bot version",
            "• `/plex_ping` — Bot ping",
            "",
            "**Tips**",
            f"• Season collapse: ≥ **{RECENT_SEASON_COLLAPSE_THRESHOLD} eps**",
            "• Example: `/plex_recent library:\"TV Shows\" limit:15`",
            "• Example: `/plex_info query:\"one punch man\"`",
            "• Example: `/plex_random library:Anime`",
        ]
    )
    embed = discord.Embed(title="🎬 Plex — Help", description=desc)
    embed.set_footer(text=f"PlexBot v{__version__}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="plex_playing", description="Affiche ce qui joue présentement sur Plex")
async def plex_playing(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        sessions = await fetch_plex_sessions()
    except Exception as e:
        await interaction.followup.send(f"{_header('❌ Plex — Error')}\n\nErreur Plex: {e}", ephemeral=True)
        return
    if not sessions:
        await interaction.followup.send(f"{_header('🎬 Plex — Now Playing')}\n\nAucune lecture en cours sur Plex.", ephemeral=True)
        return

    lines = [_header("🎬 Plex — Now Playing")]
    for s in sessions[:10]:
        lines.append(
            f"\n**{s['user']}** — {_pretty_state(s['state'])}\n"
            f"🎞️ {s['title']}\n"
            f"⏱️ {s['progress']}\n"
            f"📺 {s['quality'] or 'n/a'}"
        )
    msg = "\n".join(lines)
    await interaction.followup.send(msg[:1900] + ("\n…(tronqué)" if len(msg) > 1900 else ""), ephemeral=True)


@bot.tree.command(name="plex_recent", description="Affiche les derniers ajouts Plex (option: bibliothèque)")
@app_commands.describe(library="Nom ou ID de bibliothèque (ex: Movies, TV Shows, Anime)", limit="Nombre d’items (1-15)")
async def plex_recent(interaction: discord.Interaction, library: Optional[str] = None, limit: Optional[int] = 10):
    await interaction.response.defer(ephemeral=True)
    try:
        n = int(limit) if limit is not None else 10
    except Exception:
        n = 10
    n = max(1, min(15, n))

    try:
        items = await fetch_recently_added(limit=n, library=library)
    except Exception as e:
        await interaction.followup.send(f"{_header('❌ Plex — Error')}\n\nErreur Plex: {e}", ephemeral=True)
        return

    title = _header("🆕 Plex — Recently Added")
    if library:
        title += f" _(filter: {library})_"
    title += f" _(collapse≥{RECENT_SEASON_COLLAPSE_THRESHOLD} eps)_"

    if not items:
        await interaction.followup.send(f"{title}\n\nAucun élément récent trouvé.", ephemeral=True)
        return

    msg = "\n".join([title, *[f"• {it}" for it in items]])
    await interaction.followup.send(msg[:1900] + ("\n…(tronqué)" if len(msg) > 1900 else ""), ephemeral=True)


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


@bot.tree.command(name="plex_library_stats", description="Affiche le nombre d’éléments par bibliothèque")
async def plex_library_stats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        stats = await fetch_library_stats()
    except Exception as e:
        await interaction.followup.send(f"{_header('❌ Plex — Error')}\n\nErreur Plex: {e}", ephemeral=True)
        return
    msg = "\n".join([_header("📚 Plex — Library Stats"), *[f"• **{t}**: {c}" for t, c in stats]])
    await interaction.followup.send(msg[:1900] + ("\n…(tronqué)" if len(msg) > 1900 else ""), ephemeral=True)


@bot.tree.command(name="plex_search", description="Recherche dans Plex (option: bibliothèque)")
@app_commands.describe(query="Texte à chercher", library="Optionnel: nom de bibliothèque", limit="Nombre de résultats (1-15)")
async def plex_search_cmd(interaction: discord.Interaction, query: str, library: Optional[str] = None, limit: Optional[int] = 10):
    await interaction.response.defer(ephemeral=True)
    try:
        n = int(limit) if limit is not None else 10
    except Exception:
        n = 10
    n = max(1, min(15, n))

    try:
        hits_raw = await plex_search(query=query, limit=50, library=library)
    except Exception as e:
        await interaction.followup.send(f"{_header('❌ Plex — Error')}\n\nErreur Plex: {e}", ephemeral=True)
        return

    title = _header("🔎 Plex — Search")
    if library:
        title += f" _(filter: {library})_"
    title += f"\nQuery: **{query}**"

    if not hits_raw:
        await interaction.followup.send(f"{title}\n\nAucun résultat.", ephemeral=True)
        return

    lines = [title, ""]
    count = 0
    for el in hits_raw:
        line = _format_search_hit(el)
        if not line:
            continue
        lines.append(f"• {line}")
        count += 1
        if count >= n:
            break

    await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)


@plex_search_cmd.autocomplete("library")
async def plex_search_library_autocomplete(interaction: discord.Interaction, current: str):
    return await plex_recent_library_autocomplete(interaction, current)


@bot.tree.command(name="plex_activity", description="Stats d’activité Plex (best-effort, basé sur l’historique)")
@app_commands.describe(days="Fenêtre en jours (1-30)")
async def plex_activity_cmd(interaction: discord.Interaction, days: Optional[int] = 1):
    await interaction.response.defer(ephemeral=True)
    try:
        d = int(days or 1)
    except Exception:
        d = 1
    d = max(1, min(30, d))

    try:
        info = await fetch_activity(days=d)
    except Exception as e:
        await interaction.followup.send(
            f"{_header('❌ Plex — Error')}\n\nErreur Plex (history): {e}\n\n"
            "Note: `history/all` peut être désactivé selon le serveur/token.",
            ephemeral=True,
        )
        return

    lines = [
        _header("📊 Plex — Activity"),
        f"• Window: **{info['days']} day(s)**",
        f"• Streams: **{info['streams']}**",
        f"• Unique users: **{info['unique_users']}**",
    ]
    if info.get("top_title"):
        lines.append(f"• Top title: **{info['top_title']}**")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@bot.tree.command(name="plex_users", description="Top utilisateurs Plex (best-effort, basé sur l’historique)")
@app_commands.describe(days="Fenêtre en jours (1-90)", limit="Nombre d’utilisateurs (1-15)")
async def plex_users_cmd(interaction: discord.Interaction, days: Optional[int] = 30, limit: Optional[int] = 10):
    await interaction.response.defer(ephemeral=True)
    try:
        d = int(days or 30)
    except Exception:
        d = 30
    d = max(1, min(90, d))

    try:
        n = int(limit) if limit is not None else 10
    except Exception:
        n = 10
    n = max(1, min(15, n))

    try:
        top = await fetch_top_users(days=d, limit=n)
    except Exception as e:
        await interaction.followup.send(
            f"{_header('❌ Plex — Error')}\n\nErreur Plex (history): {e}\n\n"
            "Note: `history/all` peut être désactivé selon le serveur/token.",
            ephemeral=True,
        )
        return

    if not top:
        await interaction.followup.send(f"{_header('👥 Plex — Top Users')}\n\nAucune donnée.", ephemeral=True)
        return

    lines = [_header("👥 Plex — Top Users"), f"• Window: **{d} day(s)**", ""]
    for i, (user, plays) in enumerate(top, start=1):
        lines.append(f"{i}. **{user}** — {plays} plays")

    msg = "\n".join(lines)
    await interaction.followup.send(msg[:1900] + ("\n…(tronqué)" if len(msg) > 1900 else ""), ephemeral=True)


# =========================
# v0.4.0 commands
# =========================
@bot.tree.command(name="plex_info", description="Infos sur un film / série (poster, genres, summary)")
@app_commands.describe(query="Titre à chercher", library="Optionnel: nom de bibliothèque (Anime, Movies, TV Shows...)")
async def plex_info_cmd(interaction: discord.Interaction, query: str, library: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)
    try:
        hits = await plex_search(query=query, limit=25, library=library)
    except Exception as e:
        await interaction.followup.send(f"{_header('❌ Plex — Error')}\n\nErreur Plex: {e}", ephemeral=True)
        return

    if not hits:
        await interaction.followup.send(f"{_header('ℹ️ Plex — Info')}\n\nAucun résultat pour: **{query}**", ephemeral=True)
        return

    best = _pick_best_search_hit(query, hits)
    rating_key = _safe(best.get("ratingKey")) or ""
    # Some nodes carry key like "/library/metadata/12345"
    if not rating_key:
        k = _safe(best.get("key"))
        m = re.search(r"/library/metadata/(\d+)", k or "")
        rating_key = m.group(1) if m else ""

    if not rating_key:
        # fallback: show as simple line
        line = _format_search_hit(best) or "Résultat trouvé, mais impossible d’ouvrir le détail."
        await interaction.followup.send(f"{_header('ℹ️ Plex — Info')}\n\n{line}", ephemeral=True)
        return

    try:
        meta = await fetch_metadata(rating_key)
    except Exception as e:
        await interaction.followup.send(f"{_header('❌ Plex — Error')}\n\nErreur metadata: {e}", ephemeral=True)
        return

    if not meta:
        await interaction.followup.send(f"{_header('ℹ️ Plex — Info')}\n\nImpossible de lire les metadata.", ephemeral=True)
        return

    embed = _metadata_to_embed(meta)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="plex_random", description="Choisit un item au hasard (option: bibliothèque)")
@app_commands.describe(library="Optionnel: nom de bibliothèque",)
async def plex_random_cmd(interaction: discord.Interaction, library: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)
    try:
        el = await fetch_random_item(library=library)
    except Exception as e:
        await interaction.followup.send(f"{_header('❌ Plex — Error')}\n\nErreur Plex: {e}", ephemeral=True)
        return

    if not el:
        await interaction.followup.send(f"{_header('🎲 Plex — Random')}\n\nAucun résultat.", ephemeral=True)
        return

    # If random returns an episode, try to show the parent show instead (nicer)
    mtype = _safe(el.get("type"))
    if mtype == "episode":
        parent_key = _safe(el.get("grandparentRatingKey")) or _safe(el.get("grandparentKey"))
        mk = re.search(r"/library/metadata/(\d+)", parent_key or "")
        if mk:
            try:
                meta = await fetch_metadata(mk.group(1))
                if meta is not None:
                    el = meta
            except Exception:
                pass

    embed = _metadata_to_embed(el)
    embed.title = f"🎲 Plex — Random  _(v{__version__})_"
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="plex_ondeck", description="Continue Watching / On Deck (option: bibliothèque)")
@app_commands.describe(library="Optionnel: nom de bibliothèque", limit="Nombre d’items (1-15)")
async def plex_ondeck_cmd(interaction: discord.Interaction, library: Optional[str] = None, limit: Optional[int] = 10):
    await interaction.response.defer(ephemeral=True)
    try:
        n = int(limit) if limit is not None else 10
    except Exception:
        n = 10
    n = max(1, min(15, n))

    try:
        items = await fetch_ondeck(limit=n, library=library)
    except Exception as e:
        await interaction.followup.send(f"{_header('❌ Plex — Error')}\n\nErreur Plex: {e}", ephemeral=True)
        return

    title = _header("▶️ Plex — On Deck")
    if library:
        title += f" _(filter: {library})_"

    if not items:
        await interaction.followup.send(f"{title}\n\nAucun élément On Deck.", ephemeral=True)
        return

    msg = "\n".join([title, "", *[f"• {x}" for x in items]])
    await interaction.followup.send(msg[:1900] + ("\n…(tronqué)" if len(msg) > 1900 else ""), ephemeral=True)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
