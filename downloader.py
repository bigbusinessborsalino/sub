"""
Torrent downloader using libtorrent.
Supports real-time progress callbacks fired every 30 seconds.
"""
import asyncio
import glob as _glob
import logging
import os
import time

logger = logging.getLogger(__name__)


async def download_magnet(
    magnet_url: str,
    output_dir: str,
    timeout: int = 900,
    on_progress=None,   # async callable(stats: dict) — called every 30 s
) -> str | None:
    """
    Download a file via magnet link using libtorrent.
    Returns the path to the downloaded file, or None on failure.

    on_progress receives a dict with keys:
        progress_pct, down_kb, up_kb, num_seeds, num_leechers,
        downloaded_mb, total_mb, elapsed_s
    """
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        asyncio.to_thread(
            _download_magnet_sync, magnet_url, output_dir, timeout, on_progress, loop
        ),
        timeout=timeout + 60,
    )


def _fire_progress(loop, on_progress, stats: dict):
    """Thread-safe helper to schedule the async callback onto the event loop."""
    if on_progress and loop:
        asyncio.run_coroutine_threadsafe(on_progress(stats), loop)


def _download_magnet_sync(
    magnet_url: str,
    output_dir: str,
    timeout: int,
    on_progress,
    loop,
) -> str | None:
    try:
        import libtorrent as lt
    except ImportError:
        logger.error("libtorrent is not installed")
        return None

    os.makedirs(output_dir, exist_ok=True)

    ses = lt.session()
    ses.apply_settings({
        # Disable UDP/DHT — Replit blocks those ports; HTTP trackers work fine
        "enable_dht": False,
        "enable_lsd": False,
        "enable_natpmp": False,
        "enable_upnp": False,
        # Download settings
        "connections_limit": 200,
        "active_downloads": 1,
        "seed_time_limit": 0,
        "stop_tracker_timeout": 10,
        "request_timeout": 30,
        "announce_to_all_trackers": True,
        "announce_to_all_tiers": True,
    })

    params = lt.parse_magnet_uri(magnet_url)
    params.save_path = output_dir

    handle = ses.add_torrent(params)
    start_time = time.time()
    deadline = start_time + timeout
    last_cb = 0

    logger.info("libtorrent: resolving metadata…")

    # Fire initial "connecting" callback
    _fire_progress(loop, on_progress, {
        "phase": "metadata",
        "progress_pct": 0.0,
        "down_kb": 0.0,
        "up_kb": 0.0,
        "num_seeds": 0,
        "num_leechers": 0,
        "downloaded_mb": 0.0,
        "total_mb": 0.0,
        "elapsed_s": 0,
    })

    # ── Phase 1: wait for metadata ────────────────────────────────────────────
    while not handle.has_metadata():
        if time.time() > deadline:
            logger.warning("libtorrent: timed out waiting for metadata")
            ses.remove_torrent(handle)
            return None
        time.sleep(1)

    info = handle.get_torrent_info()
    total_bytes = info.total_size()
    total_mb = total_bytes / 1_048_576
    logger.info("libtorrent: metadata OK — %s (%.1f MB)", info.name(), total_mb)

    # ── Phase 2: download ─────────────────────────────────────────────────────
    while True:
        s = handle.status()
        now = time.time()

        if s.is_seeding or s.progress >= 1.0:
            logger.info("libtorrent: download complete!")
            # Fire a 100% callback
            _fire_progress(loop, on_progress, {
                "phase": "done",
                "progress_pct": 100.0,
                "down_kb": s.download_rate / 1024,
                "up_kb": s.upload_rate / 1024,
                "num_seeds": s.list_seeds,
                "num_leechers": max(0, s.list_peers - s.list_seeds),
                "downloaded_mb": total_mb,
                "total_mb": total_mb,
                "elapsed_s": int(now - start_time),
            })
            break

        if now > deadline:
            logger.warning("libtorrent: download timed out at %.1f%%", s.progress * 100)
            ses.remove_torrent(handle)
            return None

        # Every 30 seconds fire progress update
        if now - last_cb >= 30:
            downloaded_mb = (s.progress * total_bytes) / 1_048_576
            stats = {
                "phase": "downloading",
                "progress_pct": round(s.progress * 100, 1),
                "down_kb": round(s.download_rate / 1024, 1),
                "up_kb": round(s.upload_rate / 1024, 1),
                "num_seeds": s.list_seeds,
                "num_leechers": max(0, s.list_peers - s.list_seeds),
                "downloaded_mb": round(downloaded_mb, 1),
                "total_mb": round(total_mb, 1),
                "elapsed_s": int(now - start_time),
            }
            logger.info(
                "libtorrent: %.1f%% — ↓%.1f KB/s — seeds: %d — leechers: %d",
                stats["progress_pct"], stats["down_kb"],
                stats["num_seeds"], stats["num_leechers"],
            )
            _fire_progress(loop, on_progress, stats)
            last_cb = now

        time.sleep(2)

    time.sleep(2)
    ses.remove_torrent(handle)

    # Find the downloaded file
    files = list(set(
        _glob.glob(os.path.join(output_dir, "**/*.mkv"), recursive=True) +
        _glob.glob(os.path.join(output_dir, "**/*.mp4"), recursive=True) +
        _glob.glob(os.path.join(output_dir, "*.mkv")) +
        _glob.glob(os.path.join(output_dir, "*.mp4"))
    ))
    if not files:
        logger.warning("libtorrent: no mkv/mp4 found in %s after download", output_dir)
        return None

    latest = max(files, key=os.path.getsize)
    logger.info("libtorrent: ready → %s (%.1f MB)", latest, os.path.getsize(latest) / 1_048_576)
    return latest
