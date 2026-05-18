import os
import glob
import asyncio
import logging
import sys
import io
import time
import aiohttp
import re
import urllib.parse
from PIL import Image
from pyrogram import Client, filters
from pyrogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiohttp import web
from pyrogram import idle
import downloader

# ── Configuration ─────────────────────────────────────────────────────────────
def get_env_list(var_name, default=[]):
    val = os.getenv(var_name)
    if val:
        return [int(x.strip()) for x in val.split(',') if x.strip().lstrip("-").isdigit()]
    return default

def get_env_int(var_name, default=None):
    val = os.getenv(var_name)
    if val and val.strip().lstrip("-").isdigit():
        return int(val)
    return default

API_ID      = get_env_int("API_ID")
API_HASH    = os.getenv("API_HASH")
BOT_TOKEN   = os.getenv("BOT_TOKEN")
PORT        = get_env_int("PORT", 8000)

ADMIN_IDS   = get_env_list("ADMIN_IDS")
DB_CHANNEL  = get_env_int("DB_CHANNEL")  # private channel used to store/cache files
STICKER_ID  = "CAACAgUAAxkBAAEQJ6hpV0JDpDDOI68yH7lV879XbIWiFwACGAADQ3PJEs4sW1y9vZX3OAQ"

# Split size: max bytes per part before splitting (default 2000 MB, safely under Telegram's 2 GB bot limit)
_split_mb   = os.getenv("SPLIT_SIZE", "2000")
SPLIT_SIZE  = int(_split_mb) * 1024 * 1024 if _split_mb.isdigit() else 2000 * 1024 * 1024

# Ad configuration (all optional)
AD_TEXT         = os.getenv("AD_TEXT", "")            # message shown before the file
AD_BUTTON_TEXT  = os.getenv("AD_BUTTON_TEXT", "")     # button label
AD_BUTTON_URL   = os.getenv("AD_BUTTON_URL", "")      # button URL

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ── Pyrogram client ───────────────────────────────────────────────────────────
app = Client("anime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)


def is_admin(message: Message) -> bool:
    if not ADMIN_IDS:
        return True
    return bool(message.from_user and message.from_user.id in ADMIN_IDS)


# ── Ad helper ─────────────────────────────────────────────────────────────────
async def _send_ad(chat_id: int):
    """Send an ad message to the user if AD_TEXT is configured."""
    if not AD_TEXT:
        return
    keyboard = None
    if AD_BUTTON_TEXT and AD_BUTTON_URL:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(AD_BUTTON_TEXT, url=AD_BUTTON_URL)]]
        )
    try:
        await app.send_message(chat_id, AD_TEXT, reply_markup=keyboard)
    except Exception as e:
        logger.warning("Ad send failed: %s", e)


# ── File splitter ─────────────────────────────────────────────────────────────
async def split_file(file_path: str, split_size: int = SPLIT_SIZE) -> list:
    """
    Split a large file into equal-sized parts.
    Returns a list of part paths. If the file is small enough, returns [file_path].
    Parts are named: filename.part01.ext, filename.part02.ext, …
    """
    file_size = os.path.getsize(file_path)
    if file_size <= split_size:
        return [file_path]

    base, ext = os.path.splitext(file_path)

    def _split_sync():
        parts = []
        pnum = 1
        with open(file_path, "rb") as src:
            while True:
                chunk = src.read(split_size)
                if not chunk:
                    break
                part_path = f"{base}.part{pnum:02d}{ext}"
                with open(part_path, "wb") as dst:
                    dst.write(chunk)
                parts.append(part_path)
                pnum += 1
        return parts

    parts = await asyncio.to_thread(_split_sync)
    logger.info("Split %s into %d parts", os.path.basename(file_path), len(parts))
    return parts


# ── DB channel storage ────────────────────────────────────────────────────────
async def _store_in_db(file_path: str, caption: str = ""):
    """
    Upload file to DB_CHANNEL for caching.
    Returns the stored Message so we can copy its file_id later.
    """
    if not DB_CHANNEL:
        return None
    try:
        msg = await app.send_document(
            DB_CHANNEL,
            document=file_path,
            caption=caption,
            force_document=True,
        )
        return msg
    except Exception as e:
        logger.warning("DB store failed: %s", e)
    return None


# ── Caption builder ───────────────────────────────────────────────────────────
def _build_caption(title, genres, status):
    hashtag = "".join(x for x in title if x.isalnum())
    return f"**{title}**\n\n➜ **Genres:** {genres}\n➜ **Status:** {status}\n\n#{hashtag}"


# ── Anime info (Jikan / AniList / Kitsu) ─────────────────────────────────────
def _best_title_score(query: str, candidate_titles: list) -> float:
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    def _score(q: str, c: str) -> float:
        qn, cn = _norm(q), _norm(c)
        if not qn or not cn:
            return 0.0
        qt, ct = set(qn.split()), set(cn.split())
        if not qt:
            return 0.0
        score = len(qt & ct) / len(qt)
        if qn == cn:
            score += 1.0
        elif cn.startswith(qn):
            score += 0.5
        score -= 0.05 * max(0, len(ct - qt) - 2)
        return score

    return max((_score(query, t) for t in candidate_titles if t), default=0.0)


async def _get_from_jikan(session: aiohttp.ClientSession, anime_name: str):
    try:
        url = f"https://api.jikan.moe/v4/anime?q={anime_name}&limit=8"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                results = (await resp.json()).get('data') or []
                if not results:
                    return None, None, 0.0

                def _titles(a):
                    t = [a.get('title_english'), a.get('title'), a.get('title_japanese')]
                    t += [s.get('title', '') for s in (a.get('titles') or [])]
                    t += a.get('title_synonyms') or []
                    return [x for x in t if x]

                best = max(results, key=lambda a: _best_title_score(anime_name, _titles(a)))
                score = _best_title_score(anime_name, _titles(best))
                title = best.get('title_english') or best.get('title')
                genres = ", ".join(g['name'] for g in best.get('genres', []))
                status = best.get('status', 'Unknown')
                image_url = best['images']['jpg']['large_image_url']
                if title and image_url:
                    return _build_caption(title, genres, status), image_url, score
    except Exception as e:
        logger.warning("Jikan failed: %s", e)
    return None, None, 0.0


async def _get_from_anilist(session: aiohttp.ClientSession, anime_name: str):
    try:
        query = """
        query ($search: String) {
          Page(perPage: 5) {
            media(search: $search, type: ANIME) {
              title { english romaji native }
              genres status
              coverImage { extraLarge }
            }
          }
        }
        """
        async with session.post(
            "https://graphql.anilist.co",
            json={"query": query, "variables": {"search": anime_name}},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                results = (((await resp.json()).get("data") or {}).get("Page") or {}).get("media") or []
                if not results:
                    return None, None, 0.0

                def _titles(m):
                    t = m.get("title") or {}
                    return [v for v in [t.get("english"), t.get("romaji"), t.get("native")] if v]

                best = max(results, key=lambda m: _best_title_score(anime_name, _titles(m)))
                score = _best_title_score(anime_name, _titles(best))
                title = (best.get("title") or {}).get("english") or (best.get("title") or {}).get("romaji")
                genres = ", ".join(best.get("genres") or [])
                status = (best.get("status") or "Unknown").replace("_", " ").title()
                image_url = (best.get("coverImage") or {}).get("extraLarge")
                if title and image_url:
                    return _build_caption(title, genres, status), image_url, score
    except Exception as e:
        logger.warning("AniList failed: %s", e)
    return None, None, 0.0


async def _get_from_kitsu(session: aiohttp.ClientSession, anime_name: str):
    try:
        url = f"https://kitsu.io/api/edge/anime?filter[text]={urllib.parse.quote(anime_name)}&page[limit]=5"
        async with session.get(url, headers={"Accept": "application/vnd.api+json"},
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                items = (await resp.json()).get("data") or []
                if not items:
                    return None, None, 0.0

                def _titles(item):
                    t = (item.get("attributes") or {}).get("titles") or {}
                    attrs = item.get("attributes") or {}
                    return [v for v in [t.get("en"), t.get("en_jp"), t.get("ja_jp"),
                                        attrs.get("canonicalTitle")] if v]

                best = max(items, key=lambda i: _best_title_score(anime_name, _titles(i)))
                score = _best_title_score(anime_name, _titles(best))
                attrs = best.get("attributes") or {}
                t = attrs.get("titles") or {}
                title = t.get("en") or t.get("en_jp") or attrs.get("canonicalTitle")
                status = (attrs.get("status") or "Unknown").replace("_", " ").title()
                image_url = (attrs.get("posterImage") or {}).get("large")
                if title and image_url:
                    return _build_caption(title, "", status), image_url, score
    except Exception as e:
        logger.warning("Kitsu failed: %s", e)
    return None, None, 0.0


async def get_anime_info(anime_name: str):
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            _get_from_jikan(session, anime_name),
            _get_from_anilist(session, anime_name),
            _get_from_kitsu(session, anime_name),
            return_exceptions=True,
        )

    best_caption, best_image, best_score = None, None, -1.0
    for name, result in zip(["Jikan", "AniList", "Kitsu"], results):
        if isinstance(result, Exception):
            logger.warning("%s raised exception: %s", name, result)
            continue
        caption, image_url, score = result
        if caption and image_url and score > best_score:
            best_caption, best_image, best_score = caption, image_url, score

    return best_caption, best_image


# ── Image helpers ─────────────────────────────────────────────────────────────
_MAX_PHOTO_SIDE = 2560
_MAX_PHOTO_SUM  = 9500


def _resize_for_telegram(raw: bytes) -> io.BytesIO:
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = img.size
    scale = 1.0
    if w > _MAX_PHOTO_SIDE or h > _MAX_PHOTO_SIDE:
        scale = min(_MAX_PHOTO_SIDE / w, _MAX_PHOTO_SIDE / h)
    if (w * scale) + (h * scale) > _MAX_PHOTO_SUM:
        scale = min(scale, _MAX_PHOTO_SUM / (w + h))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf


async def _download_image_bytes(session: aiohttp.ClientSession, url: str):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                return _resize_for_telegram(await resp.read())
    except Exception as e:
        logger.warning("Image download failed: %s", e)
    return None


# ── Download via SubsPlease ───────────────────────────────────────────────────
DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def _name_to_slug(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")


async def _get_magnet_link(page_slug: str, episode: str, resolution: str) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "subsplease-dl", "magnet",
            "--page", page_slug,
            "--episode", str(episode),
            "--quality", str(resolution),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        output = (out + err).decode(errors="replace").strip()
        logger.info("subsplease-dl magnet output:\n%s", output[:300])

        collapsed = re.sub(r'\s+', '', output)
        m = re.search(r'(magnet:\?[^\s]+)', collapsed)
        if m:
            magnet = m.group(1)
            logger.info("subsplease-dl: magnet extracted (%d chars)", len(magnet))
            return magnet

        logger.warning("subsplease-dl magnet: no magnet link in output")
    except Exception as e:
        logger.error("subsplease-dl magnet error: %s", e)
    return None


def _make_progress_bar(pct: float, width: int = 20) -> str:
    filled = int(width * pct / 100)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


async def _download_via_subsplease(
    anime_name: str, episode: str, resolution: str,
    on_progress=None,
) -> str | None:
    safe_name = re.sub(r"[^\w]", "_", anime_name)
    slug = _name_to_slug(anime_name)
    logger.info("subsplease-dl: derived slug '%s' from '%s'", slug, anime_name)

    magnet = await _get_magnet_link(slug, episode, resolution)
    if not magnet:
        logger.warning("subsplease-dl: no magnet for '%s' ep %s %sp", anime_name, episode, resolution)
        return None

    logger.info("subsplease-dl: got magnet for '%s' ep %s %sp", anime_name, episode, resolution)

    ep_dir = os.path.join(DOWNLOAD_DIR, f"{safe_name}_ep{episode}_{resolution}p")
    os.makedirs(ep_dir, exist_ok=True)

    try:
        downloaded_file = await downloader.download_magnet(
            magnet, ep_dir, timeout=900, on_progress=on_progress
        )
    except asyncio.TimeoutError:
        logger.warning("libtorrent: download timed out")
        return None

    if not downloaded_file or not os.path.exists(downloaded_file):
        logger.warning("libtorrent: returned no file")
        return None

    ext = os.path.splitext(downloaded_file)[1] or ".mkv"
    final_path = os.path.join(DOWNLOAD_DIR, f"Ep_{episode}_{safe_name}_{resolution}p{ext}")
    os.rename(downloaded_file, final_path)

    try:
        os.rmdir(ep_dir)
    except OSError:
        pass

    logger.info("Ready: %.1f MB → %s", os.path.getsize(final_path) / 1_048_576, final_path)
    return final_path


# ── Web health check ──────────────────────────────────────────────────────────
async def web_server():
    server = web.Application()
    server.router.add_get("/", lambda r: web.Response(text="Bot is running!"))
    runner = web.AppRunner(server)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info("Web server started on port %s", PORT)


async def check_channels():
    logger.info("Checking DB_CHANNEL access...")
    if not DB_CHANNEL:
        logger.warning("DB_CHANNEL not set — files will not be cached.")
        return
    try:
        chat = await app.get_chat(DB_CHANNEL)
        logger.info("DB_CHANNEL OK: %s", chat.title)
    except Exception as e:
        logger.warning("Cannot access DB_CHANNEL: %s", e)


# ── /start ────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("start") & filters.private)
async def start(client, message: Message):
    await message.reply_text(
        "👋 **Welcome to Anime DL Bot!**\n\n"
        "Send me an anime name, episode, and resolution and I'll deliver the file right here.\n\n"
        "**Command:**\n"
        "`/anime <name> -e <episode> -r <resolution>`\n\n"
        "**Examples:**\n"
        "`/anime One Piece -e 1162 -r 720`\n"
        "`/anime Naruto -e 1 -r 1080`\n"
        "`/anime Bleach -e 5 -r 360`\n\n"
        "Resolutions: `360`, `720`, `1080`\n\n"
        "Powered by **SubsPlease**"
    )


# ── /anime ────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("anime") & filters.private)
async def anime_download(client, message: Message):
    user_id   = message.from_user.id
    user_name = message.from_user.first_name or "there"

    command_text = message.text.split(" ", 1)
    if len(command_text) < 2 or "-e" not in command_text[1] or "-r" not in command_text[1]:
        await message.reply_text(
            "Usage: `/anime <name> -e <episode> -r <resolution>`\n"
            "Example: `/anime One Piece -e 1162 -r 720`"
        )
        return

    args = command_text[1]
    parts = args.split("-e")
    anime_name = parts[0].strip()
    rest = parts[1].split("-r")
    episode = rest[0].strip()
    resolution = re.sub(r"\D", "", rest[1].strip())

    if not resolution:
        await message.reply_text("Please give a valid resolution: 360, 720, or 1080.")
        return

    status_msg = await message.reply_text(f"🔍 Looking up **{anime_name}**…")

    # ── Step 1: fetch and send anime info card ────────────────────────────────
    caption, image_url = await get_anime_info(anime_name)
    if caption and image_url:
        try:
            async with aiohttp.ClientSession() as dl_session:
                img_bytes = await _download_image_bytes(dl_session, image_url)
            if img_bytes:
                await app.send_photo(user_id, photo=img_bytes, caption=caption)
            else:
                await app.send_photo(user_id, photo=image_url, caption=caption)
        except Exception as e:
            logger.warning("Info card send failed: %s", e)

    await status_msg.edit_text(
        f"📡 **{anime_name}** — Ep **{episode}** ({resolution}p)\n"
        f"Connecting to peers…"
    )

    # ── Step 2: download with live progress ───────────────────────────────────
    async def on_progress(stats: dict):
        try:
            phase = stats.get("phase", "downloading")
            pct   = stats.get("progress_pct", 0.0)
            dl_mb = stats.get("downloaded_mb", 0.0)
            tot   = stats.get("total_mb", 0.0)
            spd   = stats.get("down_kb", 0.0)
            seeds = stats.get("num_seeds", 0)
            leech = stats.get("num_leechers", 0)

            if phase == "metadata":
                text = (
                    f"📡 **{anime_name}** — Ep **{episode}** ({resolution}p)\n"
                    f"Connecting to peers…"
                )
            elif phase == "done":
                text = (
                    f"✅ **{anime_name}** — Ep **{episode}** ({resolution}p)\n"
                    f"Download complete! Uploading…"
                )
            else:
                bar   = _make_progress_bar(pct)
                speed = f"{spd:,.0f} KB/s" if spd < 1024 else f"{spd/1024:.1f} MB/s"
                text  = (
                    f"📥 **{anime_name}** — Ep **{episode}** ({resolution}p)\n"
                    f"{bar} **{pct:.1f}%**\n"
                    f"📦 {dl_mb:.1f} / {tot:.1f} MB\n"
                    f"⬇️ {speed}   🌱 {seeds} seeds · {leech} peers"
                )
            await status_msg.edit_text(text)
        except Exception as e:
            logger.warning("Progress edit failed: %s", e)

    final_path = await _download_via_subsplease(
        anime_name, episode, resolution, on_progress=on_progress
    )

    if not final_path:
        await status_msg.edit_text(
            f"❌ Could not download **{anime_name}** Ep **{episode}** ({resolution}p).\n"
            f"The episode may not be available on SubsPlease yet."
        )
        return

    # ── Step 3: split if needed, then store each part in DB channel ──────────
    file_size = os.path.getsize(final_path)
    if file_size > SPLIT_SIZE:
        await status_msg.edit_text(
            f"✂️ **{anime_name}** — Ep **{episode}** ({resolution}p)\n"
            f"File is {file_size / 1_048_576:.0f} MB — splitting into parts…"
        )

    parts = await split_file(final_path)
    num_parts = len(parts)
    is_split = num_parts > 1

    # Remove the original merged file if we split it (parts are separate files now)
    if is_split and os.path.exists(final_path) and final_path not in parts:
        os.remove(final_path)

    stored_msgs = []  # list of (part_path, stored_msg or None)
    _last_up = [0.0]

    for idx, part_path in enumerate(parts, start=1):
        part_size = os.path.getsize(part_path)
        part_label = f" | Part {idx}/{num_parts}" if is_split else ""

        await status_msg.edit_text(
            f"📤 **{anime_name}** — Ep **{episode}** ({resolution}p){part_label}\n"
            f"Uploading ({part_size / 1_048_576:.1f} MB)…"
        )

        _last_up[0] = 0.0

        async def upload_progress(current, total,
                                   _sm=status_msg, _a=anime_name,
                                   _ep=episode, _r=resolution,
                                   _pl=part_label, _last=_last_up):
            now = time.time()
            if now - _last[0] < 8 and current < total:
                return
            _last[0] = now
            pct   = current * 100 / total
            up_mb = current / 1_048_576
            tot_m = total / 1_048_576
            bar   = _make_progress_bar(pct)
            try:
                await _sm.edit_text(
                    f"📤 **{_a}** — Ep **{_ep}** ({_r}p){_pl}\n"
                    f"{bar} **{pct:.1f}%**\n"
                    f"📦 {up_mb:.1f} / {tot_m:.1f} MB\n"
                    f"⬆️ Uploading…"
                )
            except Exception:
                pass

        stored_msg = None
        if DB_CHANNEL:
            try:
                db_caption = (
                    f"[SubsPlease] {anime_name} — Ep {episode} ({resolution}p)"
                    + (f" | Part {idx}/{num_parts}" if is_split else "")
                )
                stored_msg = await app.send_document(
                    DB_CHANNEL,
                    document=part_path,
                    caption=db_caption,
                    force_document=True,
                    progress=upload_progress,
                )
            except Exception as e:
                logger.warning("DB_CHANNEL store failed (part %d): %s", idx, e)

        stored_msgs.append((part_path, stored_msg))

    # ── Step 4: show ad once, then deliver all parts to user ─────────────────
    await status_msg.delete()
    await _send_ad(user_id)

    all_paths_to_clean = list(parts)

    for idx, (part_path, stored_msg) in enumerate(stored_msgs, start=1):
        part_label = f" | Part {idx}/{num_parts}" if is_split else ""
        caption = f"**{anime_name}** — Ep **{episode}** ({resolution}p){part_label} ✅"
        try:
            if stored_msg and stored_msg.document:
                await app.send_document(
                    user_id,
                    document=stored_msg.document.file_id,
                    caption=caption,
                )
            elif os.path.exists(part_path):
                await app.send_document(
                    user_id,
                    document=part_path,
                    caption=caption,
                    force_document=True,
                )
        except Exception as e:
            await message.reply_text(f"❌ Failed to send part {idx}/{num_parts}: {e}")

    for p in all_paths_to_clean:
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


# ── Admin: /broadcast ─────────────────────────────────────────────────────────
@app.on_message(filters.command("broadcast") & filters.private)
async def broadcast(client, message: Message):
    if not is_admin(message):
        return
    text = message.text.split(" ", 1)
    if len(text) < 2:
        await message.reply_text("Usage: /broadcast <message>")
        return
    await message.reply_text(f"📢 Broadcast sent: {text[1]}")


# ── Startup ───────────────────────────────────────────────────────────────────
async def main():
    await app.start()
    await check_channels()
    await web_server()
    logger.info("Bot is fully running...")
    await idle()


if __name__ == "__main__":
    logger.info("Bot Starting...")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
