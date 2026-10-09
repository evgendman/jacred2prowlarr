#!/usr/bin/env python3

import email.utils
import html
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 9128
VERSION = "2.2.5"
SERVER_TITLE = "JacRed TV + Movies"
CACHE_TTL_SECONDS = max(1, int(os.environ.get("JACRED_CACHE_TTL_SECONDS", "180")))
CACHE_MAX_ENTRIES = max(1, int(os.environ.get("JACRED_CACHE_MAX_ENTRIES", "512")))
JACRED_MIN_INTERVAL_MS = max(0, int(os.environ.get("JACRED_MIN_INTERVAL_MS", "1000")))
JACRED_429_BACKOFF_SECONDS = max(1, int(os.environ.get("JACRED_429_BACKOFF_SECONDS", "60")))
JACRED_MAX_QUEUE_WAIT_SECONDS = max(1, int(os.environ.get("JACRED_MAX_QUEUE_WAIT_SECONDS", "10")))
JACRED_URL = "https://jac.red/api/v2.0/indexers/all/results"
TORZNAB_NS = "http://torznab.com/schemas/2015/feed"

TV_CATEGORIES = {5000, 5070}
MOVIE_CATEGORIES = {2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080, 2090}

TEST_INFOHASH = "0000000000000000000000000000000000000001"
TEST_TV_INFOHASH = "0000000000000000000000000000000000000002"

# Cache complete Torznab responses for a short period. The cache key contains
# every query parameter that can affect the adapter output; the API key is
# deliberately ignored so identical searches from different clients share
# one cached response.
CACHE_LOCK = threading.Lock()
CACHE = {}
INFLIGHT_LOCKS = {}

# Global upstream limiter shared by all HTTP handler threads.
RATE_LIMIT_LOCK = threading.Lock()
UPSTREAM_REQUEST_LOCK = threading.Lock()
LAST_UPSTREAM_START = 0.0
UPSTREAM_BLOCK_UNTIL = 0.0


class UpstreamCoolingDown(Exception):
    def __init__(self, retry_after_seconds, message):
        super().__init__(message)
        self.retry_after_seconds = max(1, int(retry_after_seconds))


def parse_retry_after(exc):
    headers = getattr(exc, "headers", None)
    value = clean(headers.get("Retry-After", "") if headers else "")
    if value:
        try:
            return max(1.0, float(value))
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(1.0, (parsed.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return float(JACRED_429_BACKOFF_SECONDS)


def register_upstream_backoff(seconds, reason):
    global UPSTREAM_BLOCK_UNTIL
    seconds = max(1.0, float(seconds))
    with RATE_LIMIT_LOCK:
        UPSTREAM_BLOCK_UNTIL = max(
            UPSTREAM_BLOCK_UNTIL,
            time.monotonic() + seconds,
        )
        blocked_for = max(0.0, UPSTREAM_BLOCK_UNTIL - time.monotonic())
    print(
        f"JACRED BACKOFF seconds={blocked_for:.1f} reason={reason}",
        file=sys.stderr,
        flush=True,
    )


def wait_for_upstream_slot(deadline):
    global LAST_UPSTREAM_START

    while True:
        with RATE_LIMIT_LOCK:
            now = time.monotonic()
            next_slot = max(
                LAST_UPSTREAM_START + JACRED_MIN_INTERVAL_MS / 1000.0,
                UPSTREAM_BLOCK_UNTIL,
            )
            delay = next_slot - now
            if delay <= 0:
                LAST_UPSTREAM_START = now
                return

            remaining_budget = deadline - now
            if remaining_budget <= 0 or delay > remaining_budget:
                retry_after = max(1, int(delay + 0.999))
                raise UpstreamCoolingDown(
                    retry_after,
                    f"JacRed request deferred for {delay:.1f}s; "
                    f"maximum queue wait is {JACRED_MAX_QUEUE_WAIT_SECONDS}s",
                )

        time.sleep(delay)


def cache_key(params):
    pairs = []
    for name, values in params.items():
        if name.lower() == "apikey":
            continue
        pairs.append(
            (
                name.lower(),
                tuple(sorted(clean(v) for v in values)),
            )
        )
    return tuple(sorted(pairs))


def get_inflight_lock(key):
    with CACHE_LOCK:
        lock = INFLIGHT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            INFLIGHT_LOCKS[key] = lock
        return lock


def prune_cache(now):
    expired = [
        key
        for key, (created, _) in CACHE.items()
        if now - created >= CACHE_TTL_SECONDS
    ]
    for key in expired:
        CACHE.pop(key, None)

    if len(CACHE) <= CACHE_MAX_ENTRIES:
        return

    oldest = sorted(CACHE.items(), key=lambda entry: entry[1][0])
    for key, _ in oldest[: len(CACHE) - CACHE_MAX_ENTRIES]:
        CACHE.pop(key, None)


def cached_request(key, producer):
    now = time.monotonic()
    with CACHE_LOCK:
        cached = CACHE.get(key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1], True

    lock = get_inflight_lock(key)
    with lock:
        now = time.monotonic()
        with CACHE_LOCK:
            cached = CACHE.get(key)
            if cached and now - cached[0] < CACHE_TTL_SECONDS:
                return cached[1], True

        # The producer returns (xml_bytes, cacheable). Cache only complete
        # successful responses; the HTTP handler always receives XML bytes.
        result, cacheable = producer()

        if cacheable:
            now = time.monotonic()
            with CACHE_LOCK:
                CACHE[key] = (now, result)
                prune_cache(now)
        return result, False


def clean(value):
    return "" if value is None else html.unescape(str(value)).strip()


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def unique_preserve(values):
    result, seen = [], set()
    for value in values:
        value = clean(value)
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def item_categories(item):
    values = item.get("Category") or []
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    return {as_int(v) for v in values if as_int(v)}


# ----------------------------------------------------------------
# Audio
# ----------------------------------------------------------------

def normalize_audio_title(title):
    title = clean(title)
    if not title:
        return ""
    for pattern in (
        r"(?i)^MVO\s*\[\s*(.*?)\s*\](.*)$",
        r"(?i)^MVO\s*\|\s*(.+)$",
        r"(?i)^MVO\s*[-–—]\s*(.+)$",
    ):
        match = re.match(pattern, title)
        if match:
            title = clean(match.group(1))
            suffix = clean(match.group(2)) if match.lastindex and match.lastindex > 1 else ""
            if suffix:
                title += " " + suffix
            break
    else:
        if re.fullmatch(r"(?i)MVO", title):
            return ""
    title = re.sub(r"^\s*\[\s*(.*?)\s*\]\s*$", r"\1", title)
    title = re.sub(r"\s+", " ", title).strip()
    return re.sub(r"(?i)\bHDrezka\b", "HDRezka", title)


def russian_audio_tracks(ffprobe):
    if not isinstance(ffprobe, list):
        return []
    result = []
    for stream in ffprobe:
        if not isinstance(stream, dict) or stream.get("codec_type") != "audio":
            continue
        tags = stream.get("tags") or {}
        language = clean(tags.get("language") or tags.get("LANGUAGE")).lower()
        if language not in {"rus", "ru"}:
            continue
        voice = normalize_audio_title(tags.get("title") or tags.get("TITLE"))
        if voice:
            result.append(voice)
    return unique_preserve(result)


def fallback_voices(info):
    voices = info.get("voices")
    return unique_preserve(normalize_audio_title(v) for v in voices) if isinstance(voices, list) else []


def title_voice_fallback(title):
    title = clean(title)
    result = []
    for match in re.finditer(r"(?i)(?:\b\d+\s*[xх×]\s*)?MVO\s*\(([^)]*)\)", title):
        result.extend(normalize_audio_title(x) for x in re.split(r"[,;/]+", match.group(1)))
    for match in re.finditer(r"(?i)(?:\b\d+\s*[xх×]\s*)?MVO\s*\[([^\]]+)\]", title):
        result.append(normalize_audio_title("MVO [" + match.group(1) + "]"))
    for match in re.finditer(r"(?i)\bMVO\s*[-–—:]\s*([^|+]+)", title):
        result.append(normalize_audio_title("MVO - " + match.group(1)))
    for match in re.finditer(r"(?i)\bMVO\s*\|\s*([^|]+)", title):
        result.append(normalize_audio_title("MVO | " + match.group(1)))
    return unique_preserve(result)


# ----------------------------------------------------------------
# Quality / video
# ----------------------------------------------------------------

def first_video_stream(ffprobe):
    if not isinstance(ffprobe, list):
        return None
    candidates = []
    for stream in ffprobe:
        if not isinstance(stream, dict) or stream.get("codec_type") != "video":
            continue
        if clean(stream.get("codec_name")).lower() == "mjpeg":
            continue
        width, height = as_int(stream.get("width")), as_int(stream.get("height"))
        if width < 640 and height < 640:
            continue
        candidates.append((width * height, stream))
    return max(candidates, key=lambda x: x[0])[1] if candidates else None


def resolution_class(width, height):
    width = as_int(width)
    if width >= 3000:
        return "2160p"
    if width >= 1700:
        return "1080p"
    if width >= 1200:
        return "720p"
    return "480p" if width > 0 else ""


def normalize_quality_value(value):
    text = clean(value)
    if not text:
        return ""
    match = re.search(r"(?i)(2160|1080|720|576|480)\s*p?\b", text)
    if match:
        return match.group(1) + "p"
    if re.search(r"(?i)\b4k\b", text):
        return "2160p"
    return ""


def hdr_marker(item, source_title):
    info = item.get("info") or {}
    text = " ".join(clean(v) for v in (info.get("videotype"), info.get("video_type"), source_title)).lower()
    return "HDR" if (
        re.search(r"\bhdr10?\+?\b", text)
        or re.search(r"\bdolby\s+vision\b", text)
        or re.search(r"\bdv\b", text)
        or re.search(r"\bhdr\b", text)
    ) else ""


def extract_actual_quality(item, source_title):
    # Priority: ffprobe -> JacRed info.quality -> title.
    video = first_video_stream(item.get("ffprobe") or [])
    quality = resolution_class(video.get("width"), video.get("height")) if video else ""
    if not quality:
        quality = normalize_quality_value((item.get("info") or {}).get("quality"))
    if not quality:
        quality = normalize_quality_value(source_title)
    return quality + " HDR" if quality == "2160p" and hdr_marker(item, source_title) else quality


def actual_resolution(item):
    video = first_video_stream(item.get("ffprobe") or [])
    if not video:
        return "", ""
    width, height = as_int(video.get("width")), as_int(video.get("height"))
    return (f"{width}x{height}" if width and height else "", clean(video.get("codec_name")))


# ----------------------------------------------------------------
# TV parsing
# ----------------------------------------------------------------

def normalize_season_episode(title):
    title = clean(title)
    patterns = (
        r"\bS(\d{1,3})E(\d{1,4})(?:-E?(\d{1,4}))?\b",
        r"\b(\d{1,3})\s*[xх×]\s*(\d{1,4})\s*[-–—]\s*(\d{1,4})\b",
        r"\b(\d{1,3})\s*[xх×]\s*(\d{1,4})\b",
        r"\bS(\d{1,3})\s*\(\s*(\d{1,4})\s*[-–—]\s*(\d{1,4})",
        r"\b(\d{1,3})\s*(?:-?(?:й|ой|ый|ого|го|ему|ому|му|м|ом|ым))?\s+сез\w*\s*\(\s*(\d{1,4})\s*[-–—]\s*(\d{1,4})",
    )
    for pattern in patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if not match:
            continue
        season = int(match.group(1))
        ep1 = int(match.group(2))
        result = f"S{season:02d}E{ep1:02d}"
        if match.lastindex and match.lastindex >= 3 and match.group(3):
            result += f"-{int(match.group(3)):02d}"
        return result
    match = re.search(r"\b(\d{1,3})\s*(?:-?(?:й|ой|ый|ого|го|ему|ому|му|м|ом|ым))?\s+сез\w*", title, re.IGNORECASE)
    return f"S{int(match.group(1)):02d}" if match else ""


def result_matches_season_episode(item, season, episode):
    if not season and not episode:
        return True
    title = clean(item.get("Title"))
    season_match = re.search(r"\d+", str(season or ""))
    episode_match = re.search(r"\d+", str(episode or ""))
    wanted_season = int(season_match.group()) if season_match else None
    wanted_episode = int(episode_match.group()) if episode_match else None

    match = re.search(r"(?i)\bS(\d{1,3})E(\d{1,4})(?:-E?(\d{1,4}))?\b", title)
    if not match:
        match = re.search(r"(?i)\b(\d{1,3})\s*[xх×]\s*(\d{1,4})(?:[-–—](\d{1,4}))?\b", title)
    if not match:
        return True

    if wanted_season is not None and int(match.group(1)) != wanted_season:
        return False
    if wanted_episode is None:
        return True

    first_ep = int(match.group(2))
    last_ep = int(match.group(3)) if match.group(3) else first_ep
    return first_ep <= wanted_episode <= last_ep


# ----------------------------------------------------------------
# General release data
# ----------------------------------------------------------------

def extract_year(item):
    info = item.get("info") or {}
    for text in (info.get("relased"), item.get("Title")):
        match = re.search(r"\b((?:19|20)\d{2})\b", clean(text))
        if match:
            return match.group(1)
    return ""


def extract_format(title):
    title = clean(title)
    patterns = (
        r"\b(WEB-DLRip)\s*\(([^)]+)\)",
        r"\b(WEB-DLRip)\b",
        r"\b(WEB-DL)(?:-(?:480p|576p|720p|1080p|2160p))?\b",
        r"\b(WEBRip|BDRip|BRRip|BluRay|HDRip|HDTV)\b",
    )
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            return f"{match.group(1)}-{clean(match.group(2))}" if index == 0 else match.group(1)
    return ""


def extract_languages(item):
    names = {
        "rus":"Russian", "ru":"Russian", "eng":"English", "en":"English",
        "ukr":"Ukrainian", "uk":"Ukrainian", "deu":"German", "ger":"German", "de":"German",
        "fra":"French", "fre":"French", "fr":"French", "spa":"Spanish", "es":"Spanish",
        "ita":"Italian", "it":"Italian", "pol":"Polish", "pl":"Polish",
        "ces":"Czech", "cze":"Czech", "cs":"Czech", "por":"Portuguese", "pt":"Portuguese",
        "jpn":"Japanese", "ja":"Japanese", "kor":"Korean", "ko":"Korean",
        "chi":"Chinese", "zho":"Chinese", "zh":"Chinese",
    }
    result = []
    for value in item.get("languages") or []:
        value = clean(value).lower()
        if value in names:
            result.append(names[value])
    if not result:
        for stream in item.get("ffprobe") or []:
            if not isinstance(stream, dict) or stream.get("codec_type") != "audio":
                continue
            tags = stream.get("tags") or {}
            value = clean(tags.get("language") or tags.get("LANGUAGE")).lower()
            if value in names:
                result.append(names[value])
    return unique_preserve(result)


def infohash_from_magnet(magnet):
    match = re.search(r"(?i)xt=urn:btih:([^&]+)", clean(magnet))
    return urllib.parse.unquote(match.group(1)).lower() if match else ""


def source_original_title(item):
    info = item.get("info") or {}
    return clean(info.get("originalname") or info.get("originalName") or info.get("name") or item.get("Title"))


def normalize_tv_original(text):
    text = re.sub(r"(?i)\s*\[\s*\d{1,3}\s*[xх×]\s*\d{1,4}(?:\s*[-–—]\s*\d{1,4})?(?:\s+из\s+\d{1,4})?\s*\]", "", text)
    text = re.sub(r"(?i)\s+\bS\d{1,3}(?:E\d{1,4}(?:-\d{1,4})?)?\b", "", text)
    text = re.sub(r"(?i)\s+\b\d{1,3}\s*(?:-?(?:й|ой|ый|ого|го|ему|ому|му|м|ом|ым))?\s+сез\w*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_movie_original(text):
    text = re.sub(r"(?i)\s*\[\s*(?:2160p|1080p|720p|576p|480p|4k|hdr10?\+?|dolby\s+vision|dv)[^\]]*\]", "", text)
    text = re.sub(r"(?i)\s+\b(?:2160p|1080p|720p|576p|480p|4k|hdr10?\+?|dv)\b", "", text)
    text = re.sub(r"(?i)\s+\b(?:WEB-DL|WEB-DLRip|WEBRip|BDRip|BRRip|BluRay|HDRip|HDTV)\b(?:[-.]?(?:AVC|HEVC|H\.265|x264|x265))?", "", text)
    text = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", text)
    return re.sub(r"\s+", " ", text).strip(" -–—|")


def release_voices(item, source_title):
    voices = russian_audio_tracks(item.get("ffprobe") or [])
    if not voices:
        voices = fallback_voices(item.get("info") or {})
    if not voices:
        voices = title_voice_fallback(source_title)
    return voices


def make_tv_title(item):
    source = clean(item.get("Title"))
    parts = [normalize_tv_original(source_original_title(item)), normalize_season_episode(source)]
    year = extract_year(item)
    fmt = extract_format(source)
    quality = extract_actual_quality(item, source)

    if year:
        parts.append(f"({year})")
    if fmt:
        parts.append(fmt)
    if quality:
        parts.append(quality)

    title = " ".join(p for p in parts if p)
    voices = release_voices(item, source)
    if voices:
        title += " | " + " | ".join(voices)
    return title + (" *** " + source if source and source != title else "")


def make_movie_title(item):
    source = clean(item.get("Title"))
    parts = [normalize_movie_original(source_original_title(item))]
    year = extract_year(item)
    fmt = extract_format(source)
    quality = extract_actual_quality(item, source)

    if year:
        parts.append(f"({year})")
    if fmt:
        parts.append(fmt)
    if quality:
        parts.append(quality)

    title = " ".join(p for p in parts if p)
    voices = release_voices(item, source)
    if voices:
        title += " | " + " | ".join(voices)
    return title + (" *** " + source if source and source != title else "")


def make_title(item, media_type):
    return make_movie_title(item) if media_type == "movie" else make_tv_title(item)


# ----------------------------------------------------------------
# Media type and category filtering
# ----------------------------------------------------------------

def item_media_type(item):
    categories = item_categories(item)
    info = item.get("info") or {}
    types = {clean(x).lower() for x in (info.get("types") or [])}
    if categories & MOVIE_CATEGORIES or types & {"movie", "film"}:
        return "movie"
    if categories & TV_CATEGORIES or types & {"serial", "series", "tv"}:
        return "tv"
    return ""


def requested_media_types(request_type, requested_categories):
    if request_type == "movie":
        return {"movie"}
    if request_type == "tvsearch":
        return {"tv"}
    if requested_categories & MOVIE_CATEGORIES and not requested_categories & TV_CATEGORIES:
        return {"movie"}
    if requested_categories & TV_CATEGORIES and not requested_categories & MOVIE_CATEGORIES:
        return {"tv"}
    return {"movie", "tv"}


def item_matches_categories(item, requested, media_type):
    if not requested:
        return True
    cats = item_categories(item)
    if media_type == "movie":
        if not cats & MOVIE_CATEGORIES:
            return False
        # JacRed v2 may expose a movie only as parent category 2000.
        # A Torznab request for a movie subcategory must still return
        # the movie; the adapter must not invent a more specific source
        # category that JacRed did not provide.
        return bool(requested & MOVIE_CATEGORIES)
    if not cats & TV_CATEGORIES:
        return False
    return 5000 in requested or bool(cats & requested & TV_CATEGORIES)


# ----------------------------------------------------------------
# JacRed and XML
# ----------------------------------------------------------------

def query_jacred(query, year, media_type):
    global LAST_UPSTREAM_START

    params = {
        "q": query,
        "category": "movie_" if media_type == "movie" else "tv_",
        "limit": "1000",
    }
    if year:
        params["year"] = year
    url = JACRED_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"JacRed-V2-Torznab-Adapter/{VERSION}",
            "Accept": "application/json",
        },
    )

    # Serialize all upstream calls, then pace starts relative to the previous
    # call's completion. The wait budget includes time spent queued on this lock.
    deadline = time.monotonic() + JACRED_MAX_QUEUE_WAIT_SECONDS
    acquired = UPSTREAM_REQUEST_LOCK.acquire(
        timeout=max(0.0, deadline - time.monotonic())
    )
    if not acquired:
        raise UpstreamCoolingDown(
            JACRED_MAX_QUEUE_WAIT_SECONDS,
            "JacRed request queue is busy; maximum queue wait was reached",
        )

    try:
        wait_for_upstream_slot(deadline)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                register_upstream_backoff(
                    parse_retry_after(exc),
                    "HTTP 429",
                )
            raise
        finally:
            with RATE_LIMIT_LOCK:
                LAST_UPSTREAM_START = time.monotonic()
    finally:
        UPSTREAM_REQUEST_LOCK.release()

    return [x for x in (data.get("Results") or []) if isinstance(x, dict)]


def parse_publish_date(value):
    value = clean(value)
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        except ValueError:
            pass

        for fmt in (
            "%m/%d/%Y %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
        ):
            try:
                parsed = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
                return parsed.strftime("%a, %d %b %Y %H:%M:%S GMT")
            except ValueError:
                pass

    # Never turn a malformed/missing source date into "now": that creates a
    # misleading 0-minute age in Prowlarr. A fixed epoch date keeps the RSS
    # item valid while clearly representing an unknown timestamp.
    if value:
        print(
            f"WARNING invalid JacRed PublishDate: {value!r}",
            file=sys.stderr,
            flush=True,
        )
    return "Thu, 01 Jan 1970 00:00:00 GMT"


def add_text(parent, name, value):
    if value is not None:
        node = ET.SubElement(parent, name)
        node.text = str(value)


def add_attr(parent, name, value):
    node = ET.SubElement(parent, f"{{{TORZNAB_NS}}}attr")
    node.set("name", name)
    node.set("value", str(value))


def make_caps():
    caps = ET.Element("caps")
    ET.SubElement(
        caps,
        "server",
        version="1.0",
        title=SERVER_TITLE,
        strapline="JacRed v2 TV + Movies JSON to Torznab adapter",
        url=f"http://127.0.0.1:{PORT}/",
    )
    ET.SubElement(caps, "limits", max="1000", **{"default": "100"})
    searching = ET.SubElement(caps, "searching")
    ET.SubElement(searching, "search", available="yes", supportedParams="q,limit,offset,cat,year")
    ET.SubElement(searching, "tv-search", available="yes", supportedParams="q,limit,offset,cat,year,season,ep")
    ET.SubElement(searching, "movie-search", available="yes", supportedParams="q,limit,offset,cat,year")
    categories = ET.SubElement(caps, "categories")
    ET.SubElement(categories, "category", id="2000", name="Movies")
    tv = ET.SubElement(categories, "category", id="5000", name="TV")
    ET.SubElement(tv, "subcat", id="5070", name="Anime")
    return ET.tostring(caps, encoding="utf-8", xml_declaration=True)


def make_feed(results):
    rss = ET.Element("rss", version="2.0", **{"xmlns:torznab": TORZNAB_NS})
    channel = ET.SubElement(rss, "channel")
    add_text(channel, "title", SERVER_TITLE)
    add_text(channel, "link", "https://jac.red/")
    add_text(channel, "description", "JacRed v2 TV + Movies JSON to Torznab adapter")

    for item, media_type in results:
        node = ET.SubElement(channel, "item")
        source = clean(item.get("Title"))
        title = make_title(item, media_type)
        details = clean(item.get("Details"))
        magnet = clean(item.get("MagnetUri"))
        infohash = infohash_from_magnet(magnet)
        size = as_int(item.get("Size"))
        seeders = as_int(item.get("Seeders"))
        leechers = as_int(item.get("Peers"))
        languages = extract_languages(item)
        resolution, codec = actual_resolution(item)
        quality = extract_actual_quality(item, source)
        category_id = "2000" if media_type == "movie" else "5000"

        add_text(node, "title", title)
        guid = ET.SubElement(node, "guid", isPermaLink="false")
        guid.text = infohash or details or magnet or title
        add_text(node, "link", details or magnet)
        add_text(node, "comments", details)
        add_text(node, "description", source)
        add_text(node, "pubDate", parse_publish_date(item.get("PublishDate")))
        add_text(node, "category", category_id)
        if languages:
            add_text(node, "language", ", ".join(languages))

        if magnet:
            ET.SubElement(
                node,
                "enclosure",
                url=magnet,
                length=str(size),
                type="application/x-bittorrent",
            )

        add_attr(node, "category", category_id)
        add_attr(node, "size", size)
        add_attr(node, "seeders", seeders)
        add_attr(node, "leechers", leechers)
        add_attr(node, "peers", seeders + leechers)

        if infohash:
            add_attr(node, "infohash", infohash)
        if magnet:
            add_attr(node, "magneturl", magnet)
        if details:
            add_attr(node, "guid", details)

        for language in languages:
            add_attr(node, "language", language)
        if resolution:
            add_attr(node, "resolution", resolution)
        if codec:
            add_attr(node, "video", codec)
        if quality:
            add_attr(node, "quality", quality)

        year = extract_year(item)
        if year:
            add_attr(node, "year", year)

        for category in sorted(item_categories(item) & MOVIE_CATEGORIES):
            if media_type == "movie" and str(category) != category_id:
                add_attr(node, "category", category)

        if media_type == "tv" and 5070 in item_categories(item):
            add_attr(node, "category", 5070)

    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


# ----------------------------------------------------------------
# Local test response
# ----------------------------------------------------------------

def make_test_feed(media_types, requested_categories=None):
    requested_categories = requested_categories or set()
    results = []

    if "movie" in media_types:
        movie_categories = {2000} | (requested_categories & MOVIE_CATEGORIES)
        movie = {
            "PublishDate": "10/03/2019 00:00:00",
            "Title": "The Gentlemen (2019) WEB-DL 720p | JacRed V2 local movie test",
            "Details": "http://127.0.0.1:9128/torznab/api?t=search&q=jacred-v2-local-test",
            "MagnetUri": f"magnet:?xt=urn:btih:{TEST_INFOHASH}&dn=JacRed+V2+local+movie+test",
            "Size": 1,
            "Seeders": 0,
            "Peers": 0,
            "Category": sorted(movie_categories),
            "info": {
                "name": "The Gentlemen",
                "relased": "10/03/2019 00:00:00",
                "quality": "720p",
                "types": ["movie"],
            },
        }
        results.append((movie, "movie"))

    if "tv" in media_types:
        tv_categories = {5000}
        if 5070 in requested_categories:
            tv_categories.add(5070)
        tv = {
            "PublishDate": "10/03/2024 00:00:00",
            "Title": "The Gentlemen (2024) WEB-DL 1080p | JacRed V2 local TV test",
            "Details": "http://127.0.0.1:9128/torznab/api?t=search&q=jacred-v2-local-test-tv",
            "MagnetUri": f"magnet:?xt=urn:btih:{TEST_TV_INFOHASH}&dn=JacRed+V2+local+TV+test",
            "Size": 1,
            "Seeders": 0,
            "Peers": 0,
            "Category": sorted(tv_categories),
            "info": {
                "name": "The Gentlemen",
                "relased": "10/03/2024 00:00:00",
                "quality": "1080p",
                "types": ["series"],
            },
        }
        results.append((tv, "tv"))

    return make_feed(results)


# ----------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_xml(self, xml, content_type="application/xml; charset=utf-8"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(xml)))
        self.end_headers()
        self.wfile.write(xml)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in {"/torznab/api", "/torznab", "/api"}:
            self.send_error(404)
            return

        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        request_type = params.get("t", ["tvsearch"])[0].lower()

        if request_type == "caps":
            self.send_xml(make_caps())
            return

        if request_type not in {"search", "tvsearch", "movie"}:
            self.send_xml(make_feed([]))
            return

        query = params.get("q", [""])[0].strip()
        requested = {as_int(x) for x in params.get("cat", [""])[0].split(",") if as_int(x)}
        season = params.get("season", [""])[0].strip()
        episode = params.get("ep", [""])[0].strip()
        year = params.get("year", [""])[0].strip()

        if not query:
            has_ids = any(
                params.get(k, [""])[0].strip()
                for k in ("imdbid", "tvdbid", "tmdbid")
            )
            # Empty-q connectivity tests are served locally, with item type
            # and categories matched to the Torznab request.
            if not has_ids and not (season or episode or year):
                if request_type == "movie":
                    test_media_types = {"movie"}
                elif request_type == "tvsearch":
                    test_media_types = {"tv"}
                elif requested:
                    test_media_types = set()
                    if requested & MOVIE_CATEGORIES:
                        test_media_types.add("movie")
                    if requested & TV_CATEGORIES:
                        test_media_types.add("tv")
                else:
                    # A generic Prowlarr test without categories covers both.
                    test_media_types = {"movie", "tv"}

                self.send_xml(
                    make_test_feed(test_media_types, requested),
                    "application/rss+xml; charset=utf-8",
                )
                return

            # A structured search without q cannot be resolved reliably.
            # Never invent a title or send an empty query to JacRed.
            self.send_xml(make_feed([]))
            return

        media_types = requested_media_types(request_type, requested)

        jac_query = query
        if "tv" in media_types:
            jac_query = re.sub(r"(?i)\bS\d{1,3}E\d{1,4}(?:-E?\d{1,4})?\b", " ", jac_query)
            jac_query = re.sub(r"(?i)\b\d{1,3}\s*[xх×]\s*\d{1,4}(?:[-–—]\d{1,4})?\b", " ", jac_query)
            jac_query = re.sub(r"(?i)\b(?:season|сезон)\s*\d{1,3}\b", " ", jac_query)
            jac_query = re.sub(r"\s+", " ", jac_query).strip() or query

        request_cache_key = cache_key(params)

        def build_response():
            try:
                results = []
                errors = []

                for media_type in sorted(media_types):
                    try:
                        upstream_results = query_jacred(jac_query, year, media_type)
                        print(
                            f"UPSTREAM OK type={media_type} q={jac_query!r} count={len(upstream_results)}",
                            file=sys.stderr,
                            flush=True,
                        )
                    except Exception as exc:
                        errors.append((media_type, exc))
                        print(
                            f"UPSTREAM ERROR type={media_type} q={jac_query!r}: {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                        continue

                    for item in upstream_results:
                        if item_media_type(item) != media_type:
                            continue
                        if not item_matches_categories(item, requested, media_type):
                            continue
                        if media_type == "tv" and not result_matches_season_episode(item, season, episode):
                            continue
                        results.append((item, media_type))

                if errors and not results:
                    raise errors[0][1]

                unique, seen = [], set()
                for item, media_type in results:
                    key_value = clean(
                        item.get("MagnetUri")
                        or item.get("Details")
                        or item.get("Title")
                    ).casefold()
                    key = (media_type, key_value)
                    if not key_value or key in seen:
                        continue
                    seen.add(key)
                    unique.append((item, media_type))

                limit = max(1, min(as_int(params.get("limit", ["100"])[0], 100), 1000))
                offset = max(0, as_int(params.get("offset", ["0"])[0], 0))
                return (
                    make_feed(unique[offset:offset + limit]),
                    not errors,
                )

            except Exception:
                traceback.print_exc(file=sys.stderr)
                raise

        try:
            xml, from_cache = cached_request(request_cache_key, build_response)
            print(
                f"CACHE {'HIT' if from_cache else 'MISS'} "
                f"type={request_type} q={query!r}",
                file=sys.stderr,
                flush=True,
            )
            self.send_xml(
                xml,
                "application/rss+xml; charset=utf-8",
            )
        except UpstreamCoolingDown as exc:
            body = str(exc).encode("utf-8", errors="replace")
            self.send_response(503)
            self.send_header("Retry-After", str(exc.retry_after_seconds))
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except urllib.error.HTTPError as exc:
            body = str(exc).encode("utf-8", errors="replace")
            status = 503 if exc.code == 429 else 502
            self.send_response(status)
            if exc.code == 429:
                self.send_header(
                    "Retry-After",
                    str(max(1, int(parse_retry_after(exc) + 0.999))),
                )
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            body = str(exc).encode("utf-8", errors="replace")
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)



if __name__ == "__main__":
    print(
        f"{SERVER_TITLE} v{VERSION} listening on http://{HOST}:{PORT}/torznab",
        flush=True,
    )
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
