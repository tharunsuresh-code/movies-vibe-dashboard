import asyncio
import json
import os
import subprocess
import sys
import time
import random
import re
from datetime import datetime, timezone
from pathlib import Path
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Tamil Movie Review API")

# Allow all CORS (Origin check happens in auth middleware below)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

REPO_DIR = Path(__file__).parent
OUTPUT_DIR = REPO_DIR / "output"
FILMS_REGISTRY = OUTPUT_DIR / "films.json"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

# ─── Auth Config ───────────────────────────────────────────────────────────
# API_KEY must match what the frontend sends as X-API-Key header.
# Loaded from env: set API_KEY on the VPS env, and VITE_API_KEY on Vercel.
API_KEY = os.environ.get("API_KEY", "")
if not API_KEY:
    import logging
    logging.warning("API_KEY not set — API routes are UNPROTECTED. Set API_KEY env var in production.")
ALLOWED_ORIGINS = [
    "https://movies-vibe-dashboard.vercel.app",
    "https://movies-vibe-dashboard.vercel.app/*",
    "https://onekural.vercel.app",
]

@app.middleware("http")
async def auth_middleware(request, call_next):
    # Allow CORS preflight and health checks from localhost
    if request.method == "OPTIONS":
        return await call_next(request)
    if request.client and request.client.host in ("127.0.0.1", "::1", "localhost"):
        return await call_next(request)

    path = request.url.path
    # Only protect /api/ routes
    if path.startswith("/api/"):
        # Allow tunnel URL discovery without auth (needed by Vercel frontend)
        if path == "/api/tunnel-url":
            return await call_next(request)
        # Check API key header
        key = request.headers.get("x-api-key", "")
        if key != API_KEY:
            return JSONResponse(status_code=403, content={"error": "Forbidden"})
    return await call_next(request)

# ─── Films Registry ───────────────────────────────────────────────────────

DEFAULT_FILMS = [
    "Karuppu",
    "Parimala and Co",
    "Blast",
    "29",
    "Love Insurance Kompany",
    "Mr. X",
    "Battle",
    "Kara",
]

# ─── Film Metadata (release dates) ──────────────────────────
FILMS_META = {
    "Parimala and Co":        {"release_date": "2026-06-05", "star": "Jayaram, Urvashi, Mysskin, Pandiraaj"},
    "Blast":                  {"release_date": "2026-05-28", "star": "Arjun, Preity, Abhirami"},
    "Karuppu":                {"release_date": "2026-05-15", "star": "Suriya, Trisha, RJ Balaji"},
    "29":                     {"release_date": "2026-05-08", "star": "Vidhu, Preethi Asrani"},
    "Love Insurance Kompany": {"release_date": "2026-04-10", "star": "Pradeep Ranganathan, SJ Suryah, Krithi Shetty"},
    "Mr. X":                  {"release_date": "2026-04-17", "star": "Arya, Gautham Ram Karthik"},
    "Battle":                 {"release_date": "2026-04-24", "star": "Arjun Prabhakaran, Aradhya Krishna"},
    "Kara":                   {"release_date": "2026-04-30", "star": "Dhanush, Mamitha Baiju, Jayaram"},
    "Retta Thala":            {"release_date": "2026-03-20", "star": "Arun Vijay, Siddhi"},
}

def load_films() -> dict:
    """Load tracked films. Returns {film_name: {last_video_ids: [...], last_checked: iso}}"""
    if FILMS_REGISTRY.exists():
        with open(FILMS_REGISTRY) as f:
            return json.load(f)
    # Seed with defaults
    registry = {f: {"last_video_ids": [], "last_checked": None, "added": datetime.now(timezone.utc).isoformat()} for f in DEFAULT_FILMS}
    save_films(registry)
    return registry

def save_films(registry: dict):
    OUTPUT_DIR.mkdir(exist_ok=True)
    with open(FILMS_REGISTRY, "w") as f:
        json.dump(registry, f, indent=2)

# ─── API Key helpers ──────────────────────────────────────────────────────

def get_youtube_key() -> str:
    """Load YouTube API key from .env or config."""
    key = os.environ.get("YOUTUBE_API_KEY", "")
    if key:
        return key
    for dotenv in [REPO_DIR / ".env", Path.home() / ".hermes" / ".env"]:
        if dotenv.exists():
            with open(dotenv) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("YOUTUBE_API_KEY="):
                        return line.split("=", 1)[1].strip()
    return ""

def get_openrouter_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        return key
    for dotenv in [REPO_DIR / ".env", Path.home() / ".hermes" / ".env"]:
        if dotenv.exists():
            with open(dotenv) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("OPENROUTER_API_KEY="):
                        return line.split("=", 1)[1].strip()
    return ""

# ─── YouTube Search & Change Detection ────────────────────────────────────

def _is_relevant_video(video: dict, film: str) -> bool:
    """Check if a video is actually about the specified film.
    Handles short/numeric titles (e.g. '29') that cause false positives."""
    title = video.get("title", "").lower()
    film_lower = film.lower()

    if len(film_lower) <= 3:
        # Short/numeric: require film name + context (movie/film/review/cast)
        meta = FILMS_META.get(film, {})
        cast_keywords = [k.strip() for k in meta.get("star", "").split(",") if k.strip()]
        context_words = ["movie", "film", "review", "trailer", "teaser", "public review", "honest review"]

        has_film = bool(re.search(r'\b' + re.escape(film_lower) + r'\b', title))
        has_context = any(kw in title for kw in context_words)
        has_cast = any(kw.lower() in title for kw in cast_keywords)

        return has_film and (has_context or has_cast)

    return film_lower in title


def _get_search_cache() -> dict:
    """Load search cache from disk."""
    cache_path = OUTPUT_DIR / "search-cache.json"
    if cache_path.exists():
        try:
            with open(cache_path) as f:
                return json.load(f)
        except:
            pass
    return {}

def _save_search_cache(cache: dict):
    """Save search cache to disk."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    with open(OUTPUT_DIR / "search-cache.json", "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def search_review_videos(film: str, api_key: str, max_videos: int = 6) -> list[dict]:
    """Search YouTube for review videos. Returns [{id, title, channel, published}].
    Caches results for 24h to save quota (search.list = 100 units)."""
    # Check cache first (24h TTL)
    cache = _get_search_cache()
    cache_key = f"review:{film.lower()}"
    cached = cache.get(cache_key)
    if cached and cached.get("expires", 0) > datetime.now().timestamp():
        return cached.get("results", [])[:max_videos]
    review_queries = [
        f"{film} movie review tamil",
        f"{film} review tamil",
        f"{film} public review",
        f"{film} honest review",
        f"{film} Baradwaj Rangan",
        f"{film} galatta plus",
    ]

    seen: dict[str, dict] = {}
    skip_keywords = ["#shorts", "deleted scene", "collection", "24th day", "box office"]

    for q in review_queries:
        if len(seen) >= max_videos * 5:
            break
        try:
            params = {"part": "snippet", "q": q, "type": "video", "maxResults": 10,
                      "relevanceLanguage": "ta", "key": api_key, "order": "relevance"}
            resp = httpx.get(f"{YOUTUBE_API_BASE}/search", params=params, timeout=15)
            if resp.status_code != 200:
                continue
            for item in resp.json().get("items", []):
                vid = item["id"]["videoId"]
                title = item["snippet"]["title"].lower()
                if any(kw in title for kw in skip_keywords):
                    continue
                if vid not in seen:
                    seen[vid] = {
                        "id": vid,
                        "title": item["snippet"]["title"],
                        "channel": item["snippet"]["channelTitle"],
                        "published": item["snippet"]["publishedAt"],
                    }
        except:
            continue

    result = list(seen.values())
    # Filter out videos that aren't actually about this film (critical for short/numeric names)
    result = [v for v in result if _is_relevant_video(v, film)]
    final = result[:max_videos]
    # Cache for 24h
    cache[cache_key] = {"results": final, "expires": datetime.now().timestamp() + 86400}
    _save_search_cache(cache)
    return final


def search_trailer_videos(film: str, api_key: str, max_videos: int = 3) -> list[dict]:
    """Search YouTube for official trailer/teaser videos for a film.
    Returns [{id, title, channel, published}].
    Caches results for 24h to save quota (search.list = 100 units)."""
    # Check cache first (24h TTL)
    cache = _get_search_cache()
    cache_key = f"trailer:{film.lower()}"
    cached = cache.get(cache_key)
    if cached and cached.get("expires", 0) > datetime.now().timestamp():
        return cached.get("results", [])[:max_videos]

    queries = [
        f"{film} official trailer tamil",
        f"{film} trailer tamil",
        f"{film} teaser tamil",
        f"{film} official teaser",
    ]
    seen: dict[str, dict] = {}
    skip_keywords = ["review", "reaction", "behind the scenes", "interview", "#shorts"]

    for q in queries:
        if len(seen) >= max_videos * 4:
            break
        try:
            params = {"part": "snippet", "q": q, "type": "video", "maxResults": 10,
                      "relevanceLanguage": "ta", "key": api_key, "order": "relevance"}
            resp = httpx.get(f"{YOUTUBE_API_BASE}/search", params=params, timeout=15)
            if resp.status_code != 200:
                continue
            for item in resp.json().get("items", []):
                vid = item["id"]["videoId"]
                title = item["snippet"]["title"].lower()
                if any(kw in title for kw in skip_keywords):
                    continue
                if "trailer" not in title and "teaser" not in title:
                    continue
                if vid not in seen:
                    seen[vid] = {
                        "id": vid,
                        "title": item["snippet"]["title"],
                        "channel": item["snippet"]["channelTitle"],
                        "published": item["snippet"]["publishedAt"],
                    }
        except:
            continue
    final = list(seen.values())[:max_videos]
    # Cache for 24h
    cache[cache_key] = {"results": final, "expires": datetime.now().timestamp() + 86400}
    _save_search_cache(cache)
    return final


def fetch_video_stats(api_key: str, video_ids: list[str]) -> dict[str, dict]:
    """Fetch view count, like count, and description for video IDs via videos API.
    Returns {video_id: {views: int, likes: int, description: str}}."""
    stats: dict[str, dict] = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i+50]
        try:
            params = {"part": "statistics,snippet", "id": ",".join(batch), "key": api_key}
            resp = httpx.get(f"{YOUTUBE_API_BASE}/videos", params=params, timeout=15)
            if resp.status_code != 200:
                continue
            for item in resp.json().get("items", []):
                vid = item["id"]
                stat = item.get("statistics", {})
                snippet = item.get("snippet", {})
                stats[vid] = {
                    "views": int(stat.get("viewCount", 0)),
                    "likes": int(stat.get("likeCount", 0)),
                    "description": snippet.get("description", ""),
                }
        except:
            continue
    return stats


def normalize_analysis(analysis: dict) -> dict:
    """Ensure array fields are arrays, not strings. LLM sometimes returns strings."""
    # Clean empty strings from all list fields
    for key in ['what_people_loved', 'what_people_criticized']:
        val = analysis.get(key, [])
        if isinstance(val, list):
            analysis[key] = [v for v in val if isinstance(v, str) and v.strip()]
    for key in ['tab_zero_spoiler', 'tab_mild_spoiler', 'tab_full_spoiler']:
        tab = analysis.get(key, {})
        if isinstance(tab, dict):
            arh = tab.get('audience_reaction_highlights')
            if isinstance(arh, str):
                sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', arh) if s.strip()]
                tab['audience_reaction_highlights'] = sentences if sentences else [arh]
            elif isinstance(arh, list):
                tab['audience_reaction_highlights'] = [v for v in arh if isinstance(v, str) and v.strip()]
    return analysis


def extract_metadata_from_descriptions(descriptions: list[str]) -> dict:
    """Extract director, cast, music, release date from YouTube trailer descriptions.
    Returns {director: str, cast: str, music: str, release_date: str}.
    Only extracts names, not full sentences."""
    import re
    combined = "\n".join(descriptions)

    # Helper: clean extracted name — stop at sentence boundaries
    def clean_name(text: str) -> str:
        # Stop at sentence continuations (with or without comma)
        text = re.split(r'(?:,\s*|\s+)(?:the\s+film|who|and\s+(?:the|his|her|produced|also|versatile)|produced|presented|features|offers|boasts|has|is|was|will|cinematography|editing|production|direction)', text, flags=re.IGNORECASE)[0]
        # Stop at period+space but NOT at initials like 'G. V.'
        text = re.split(r'\.\s+(?=[a-z]{2,})', text)[0]
        text = text.strip().rstrip("|–-,.").strip()
        # Remove leading prepositions
        text = re.sub(r'^(?:and|,|\s|of\s+|by\s+)+', '', text).strip()
        return text

    # Director patterns
    director = ""
    for pat in [
        r'(?:written\s+and\s+)?directed\s+by\s+([A-Z][^,\n]{2,60})',
        r'(?:movie\s+)?director(?!\s+of\s+photography)[:\s]+([A-Z][^,\n]{2,60})',
        r'(?:இயக்குனர்)[:\s]*([^\n,]+)',
    ]:
        m = re.search(pat, combined, re.IGNORECASE)
        if m:
            director = clean_name(m.group(1))
            break

    # Cast/Star patterns — grab comma-separated names
    cast = ""
    for pat in [
        r'(?:starring|star\s*cast)[:\s]+([^\n]+?)(?:\s+in\s+(?:key|pivotal|lead|main|important)|$)',
        r'(?:cast|actors?)[:\s]+([A-Z][^\n]{3,120})',
        r'(?:features?\s+)?(?:a\s+)?star-studded\s+cast\s+(?:of\s+)?(?:including\s+)?([A-Z][^\n]{3,120})',
    ]:
        m = re.search(pat, combined, re.IGNORECASE)
        if m:
            raw = m.group(1).strip()
            # Extract just name-like tokens: "Name, Name and Name"
            names = re.findall(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*', raw)
            if names:
                cast = ", ".join(names[:6])  # max 6 names
            break

    # Music composer patterns
    music = ""
    for pat in [
        r'music\s+(?:composed|by|director)[:\s]+([A-Z][^,\n]{2,60})',
        r'(?:composer|bgm|background\s+music)[:\s]+([A-Z][^,\n]{2,60})',
        r'(?:music\s+)?(?:by|composed\s+by)\s+([A-Z][^,\n]{2,60})',
    ]:
        m = re.search(pat, combined, re.IGNORECASE)
        if m:
            music = clean_name(m.group(1))
            break

    # Release date patterns
    release = ""
    for pat in [
        r'(?:release\s+date|releasing|coming\s+on|in\s+theatres?\s+on)[:\s]*(\d{1,2}[\s/\-]?\w+[\s/\-]?\d{2,4})',
        r'(?:release\s+date|releasing|coming\s+on)[:\s]*([^\n]+)',
    ]:
        m = re.search(pat, combined, re.IGNORECASE)
        if m:
            release = m.group(1).strip().rstrip("|–-").strip()
            break

    return {"director": director, "cast": cast, "music": music, "release_date": release}


def compute_hype_score(trailer_stats: list[dict], comments: list[str], llm_key: str, film: str = "") -> dict:
    """Compute hype score from trailer metrics + comment analysis.
    Returns {hype_score: 0-100, views, likes, like_ratio, comment_count, sentiment, vibe}."""
    import math as _math
    total_views = sum(s.get("views", 0) for s in trailer_stats)
    total_likes = sum(s.get("likes", 0) for s in trailer_stats)

    # Views matter most for hype — log-scale with generous cap
    view_score = min(45, round(_math.log10(max(1, total_views)) * 9))
    like_ratio = total_likes / max(1, total_views) * 100
    ratio_score = min(15, round(like_ratio * 1.5))

    llm_score = 0
    sentiment = "unknown"
    vibe = ""
    category = "Anticipated"
    genre_phrase = ""
    rating_label = ""
    tagline = ""
    if comments and llm_key:
        try:
            prompt = f"""Analyze these YouTube comments about the upcoming Tamil film "{film}" trailer.
Output ONLY valid JSON:
{{"sentiment": "<positive/mixed/negative>", "excitement_level": <0-100>, "category": "<one of: Celebratory/Excited/Cautiously Optimistic/Mixed/Polarizing/Disappointed/Surprised/Divisive/Anticipated/Muted>", "rating_label": "<2-4 word expressive mood label capturing overall audience feeling, e.g. 'Mind Blowing', 'Average Buzz', 'Mass Hype', 'Solid Expectations', 'Underwhelming', 'Mixed Bag' — vary per film, be specific to what comments say, NOT generic>", "tagline": "<2-3 word genre/tone summary inferred from title and comments, e.g. 'Action Thriller', 'Family Drama', 'Mass Entertainer', 'Romantic Comedy', 'Psychological Thriller'>", "genre_phrase": "<short 5-8 word phrase describing the film's genre and tone, e.g. 'Intense action thriller with emotional core' or 'Light-hearted family comedy with heart' — avoid generic words like exciting/anticipate/hype>", "vibe": "<1-2 sentence insight on audience expectations and reaction — vary your language, do NOT start with 'Audience is highly' or 'Viewers are excited' — be specific to what comments actually say>", "top_expectations": ["<thing>", "<thing>"]}}

Comments:
{chr(10).join(comments[:100])}"""
            resp = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {llm_key}", "Content-Type": "application/json"},
                json={"model": "openai/gpt-chat-latest",
                       "messages": [{"role": "user", "content": prompt}],
                       "response_format": {"type": "json_object"},
                       "max_tokens": 1000, "temperature": 0.3},
                timeout=30,
            )
            if resp.status_code == 200:
                result = resp.json()["choices"][0]["message"]["content"]
                parsed = json.loads(result)
                llm_score = min(25, round(parsed.get("excitement_level", 50) * 0.25))
                sentiment = parsed.get("sentiment", "unknown")
                vibe = parsed.get("vibe", "")
                category = parsed.get("category", "Anticipated")
                genre_phrase = parsed.get("genre_phrase", "")
                rating_label = parsed.get("rating_label", "")
                tagline = parsed.get("tagline", "")
                # Fallback: derive rating_label from category if empty
                if not rating_label and category:
                    cat_labels = {
                        'Celebratory': 'Huge Celebration',
                        'Excited': 'High Excitement',
                        'Cautiously Optimistic': 'Cautious Hope',
                        'Mixed': 'Mixed Feelings',
                        'Polarizing': 'Divided Fans',
                        'Disappointed': 'Let Down',
                        'Surprised': 'Pleasant Surprise',
                        'Divisive': 'Split Opinions',
                        'Anticipated': 'High Anticipation',
                        'Muted': 'Low Buzz',
                    }
                    rating_label = cat_labels.get(category, '')
        except:
            pass

    comment_score = min(15, round(_math.log10(max(1, len(comments))) * 6))
    hype_score = min(100, view_score + ratio_score + llm_score + comment_score)

    # Threshold bonus: big-view trailers get a boost
    if total_views >= 1_000_000:
        hype_score = min(100, hype_score + 10)
    elif total_views >= 500_000:
        hype_score = min(100, hype_score + 5)

    return {
        "hype_score": hype_score,
        "total_views": total_views,
        "total_likes": total_likes,
        "like_ratio": round(like_ratio, 1),
        "comment_count": len(comments),
        "sentiment": sentiment,
        "vibe": vibe,
        "category": category,
        "genre_phrase": genre_phrase,
        "rating_label": rating_label,
        "tagline": tagline,
    }


def has_new_videos(film: str, videos: list[dict], registry: dict) -> bool:
    """Check if any new review videos appeared since last check."""
    entry = registry.get(film, {})
    old_ids = set(entry.get("last_video_ids", []))
    new_ids = {v["id"] for v in videos}
    return bool(new_ids - old_ids)


def short_mood_label(mood: str) -> str:
    """Extract a 2-4 word label from a verbose audience_mood string."""
    if not mood:
        return ""
    mood_lower = mood.lower()
    # Map common phrases to expressive labels
    label_map = [
        ("celebratory", "Celebratory"),
        ("blockbuster", "Blockbuster"),
        ("explosive", "Explosive"),
        ("divisive", "Divisive"),
        ("polarizing", "Polarizing"),
        ("disappointed", "Disappointed"),
        ("underwhelming", "Underwhelming"),
        ("mixed", "Mixed"),
        ("tepid", "Tepid Reception"),
        ("muted", "Muted Response"),
        ("enthusiastic", "Enthusiastic"),
        ("ecstatic", "Ecstatic"),
        ("joyful", "Joyful"),
        ("thrilled", "Thrilled"),
        ("cautiously", "Cautiously Optimistic"),
        ("optimistic", "Optimistic"),
        ("strongly positive", "Raving"),
        ("positive", "Positive"),
        ("negative", "Poor Reception"),
        ("excited", "High Expectations"),
        ("moderately", "Fairly Good"),
        ("boring", "Found Boring"),
        ("average", "Passable"),
        ("loved", "Audience Favorite"),
        ("praised", "Highly Praised"),
        ("appreciated", "Appreciated"),
        ("solid", "Solid"),
        ("impressive", "Impressive"),
        ("decent", "Decent"),
        ("outstanding", "Outstanding"),
        ("fantastic", "Fantastic"),
        ("brilliant", "Brilliant"),
        ("stunning", "Stunning"),
        ("gripping", "Gripping"),
        ("refreshing", "Refreshing"),
        ("heartwarming", "Heartwarming"),
        ("intense", "Intense"),
        ("superb", "Superb"),
        ("masterful", "Masterful"),
        ("lackluster", "Lackluster"),
        ("disappointing", "Disappointing"),
        ("average", "So-So"),
        ("mediocre", "Mediocre"),
    ]
    for key, label in label_map:
        if key in mood_lower:
            return label
    # Fallback: take first 4 words
    words = mood.split()[:4]
    return " ".join(words) + ("…" if len(mood.split()) > 4 else "")


def fetch_comments_api(api_key: str, video_id: str, max_comments: int = 200) -> list[str]:
    """Fetch comments via YouTube Data API."""
    comments, token = [], None
    while len(comments) < max_comments:
        params = {"part": "snippet", "videoId": video_id, "maxResults": min(100, max_comments - len(comments)),
                  "order": "relevance", "key": api_key}
        if token: params["pageToken"] = token
        resp = httpx.get(f"{YOUTUBE_API_BASE}/commentThreads", params=params, timeout=15)
        if resp.status_code != 200: break
        data = resp.json()
        for item in data.get("items", []):
            text = item["snippet"]["topLevelComment"]["snippet"]["textDisplay"]
            text = re.sub(r"<[^>]+>", "", text).strip()
            if len(text) > 5 and not re.search(r"dofy|norton|sponsor|subscribe", text, re.I):
                comments.append(text)
        token = data.get("nextPageToken")
        if not token: break
        time.sleep(0.05)
    return comments[:max_comments]


def fetch_new_comments(api_key: str, video_id: str, seen_ids: set, max_comments: int = 200) -> tuple[list[str], set]:
    """Fetch comments and return only NEW ones (not in seen_ids).
    Returns (new_comments, updated_seen_ids)."""
    new_comments, token, seen = [], None, set(seen_ids)
    while len(new_comments) < max_comments:
        params = {"part": "snippet", "videoId": video_id, "maxResults": min(100, max_comments - len(new_comments)),
                  "order": "time", "key": api_key}  # time order = newest first
        if token: params["pageToken"] = token
        resp = httpx.get(f"{YOUTUBE_API_BASE}/commentThreads", params=params, timeout=15)
        if resp.status_code != 200: break
        data = resp.json()
        for item in data.get("items", []):
            cid = item["id"]
            if cid in seen:
                return new_comments, seen  # Hit already-seen comment, stop
            seen.add(cid)
            text = item["snippet"]["topLevelComment"]["snippet"]["textDisplay"]
            text = re.sub(r"<[^>]+>", "", text).strip()
            if len(text) > 5 and not re.search(r"dofy|norton|sponsor|subscribe", text, re.I):
                new_comments.append(text)
        token = data.get("nextPageToken")
        if not token: break
        time.sleep(0.05)
    return new_comments, seen

# ─── LLM Analysis (3-tier) ────────────────────────────────────────────────

ANALYSIS_PROMPT_TEMPLATE = """You are a Tamil cinema review analyst. Analyze these YouTube comments about the Tamil film "{film}".

Comments are in Tamil, English, or Tanglish.
IMPORTANT: Limit top_themes to max 5, what_people_loved to max 5, what_people_criticized to max 5. NEVER include empty strings "" in any array — if you have fewer items, use fewer. Each array item must be a non-empty string.

Output ONLY valid JSON:
{{
  "rating": <0-10, one decimal>,
  "sentiment_breakdown": {{"positive_percent": N, "negative_percent": N, "mixed_percent": N, "neutral_percent": N}},
  "genre": ["<genre 1>", "<genre 2>", "<genre 3>"],
  "popularity_score": <0-100 based on discussion volume and energy>,

  "tab_zero_spoiler": {{
    "vibe_summary": "<2 sentences on the overall audience vibe/reaction, zero plot details>",
    "audience_mood": "<ONE OR TWO words max, e.g.: Celebratory/Excited/Cautiously Optimistic/Mixed/Polarizing/Muted. Never sentences.>",
    "genre_tags": ["<tag>", "<tag>"],
    "who_should_watch": "<one sentence>",
    "performance_highlights": "<audience takes on acting standout performances, zero plot details>",
    "technical_highlights": "<audience takes on BGM, visuals, direction, craft from comments>"
  }},

  "tab_mild_spoiler": {{
    "first_half_vibe": "<audience takes on the opening and first half, may mention slight structure>",
    "second_half_vibe": "<audience takes on the second half and pacing>",
    "uniqueness": "<what makes this film stand out or feel different, based on audience comments, may reveal a bit about the premise>",
    "why_watch": "<compelling reason to watch, drawn from audience opinions, slightly revealing>"
  }},

  "tab_full_spoiler": {{
    "audience_reaction_highlights": ["<specific audience opinion comment that reveals plot context>", "<another revealing audience take>", "<third>"],
    "climax_analysis": "<what people say about the ending from comments only>",
    "technical_breakdown": "<direction, music, VFX, cinematography critiques from audience>"
  }},

  "top_themes": [{{"theme": "<theme>", "frequency": "very high/high/medium/low", "sentiment": "positive/negative/mixed"}}],
  "what_people_loved": ["<thing>", "<thing>", "<thing>", "<thing>", "<thing>"],
  "what_people_criticized": ["<thing>", "<thing>", "<thing>", "<thing>", "<thing>"],

  "audience_engagement": {{
    "theatre_response": "<one sentence>",
    "comparisons": ["<ONLY if commenters themselves compare this film to another film/director/style — extract their exact comparison. DO NOT list hero's filmography or director's previous films unless commenters explicitly mention them>", "<another commenter-driven comparison, same rules>"]
  }}
}}

Comments:
{comments}"""


def run_llm_analysis(film: str, comments: list[str], api_key: str) -> dict:
    """Send comments to LLM and get full 3-tier analysis."""
    if not comments or not api_key:
        return {}
    sample = comments[:70]
    if len(comments) > 70:
        sample += random.sample(comments[70:], min(40, len(comments) - 70))
    random.shuffle(sample)

    prompt = ANALYSIS_PROMPT_TEMPLATE.format(
        film=film,
        comments="\n".join(f"{i+1}. {c}" for i, c in enumerate(sample))
    )
    try:
        resp = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "openai/gpt-chat-latest", "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.3, "max_tokens": 4000, "response_format": {"type": "json_object"}},
            timeout=120,
        )
        if resp.status_code != 200:
            return {}
        content = resp.json()["choices"][0]["message"]["content"]
        content = content.strip()
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
        start, end = content.find("{"), content.rfind("}")
        if start >= 0 and end > start:
            result = json.loads(content[start:end + 1])
            result = normalize_analysis(result)
            result["_comments_analyzed"] = len(sample)
            return result
    except Exception:
        pass
    return {}


MERGE_PROMPT = """You are updating an audience sentiment analysis for the Tamil film "{film}" based on NEW comments.
The previous analysis was based on older comments. Now incorporate these new comments to update the analysis.
IMPORTANT: Limit top_themes to max 5, what_people_loved to max 5, what_people_criticized to max 5. NEVER include empty strings "" in any array — if you have fewer items, use fewer. Each array item must be a non-empty string.

Previous analysis:
{previous}

New comments ({count} new):
{new_comments}

Update the analysis JSON. Keep ALL existing fields and their structure. Adjust:
- sentiment_breakdown.positive_percent and negative_percent based on new comment sentiment
- tab_zero_spoiler.audience_mood if the mood has shifted
- tab_mild_spoiler: keep first_half_vibe, second_half_vibe, uniqueness, why_watch — update if new comments reveal changes
- tab_full_spoiler: keep audience_reaction_highlights, climax_analysis, technical_breakdown — update if new comments reveal changes
- top_themes: add any new themes, update frequencies
- what_people_loved / what_people_criticized: add new points
- rating: adjust slightly if sentiment shifted significantly
- popularity_score: adjust based on new volume + sentiment

IMPORTANT: Include ALL these fields in your output: rating, sentiment_breakdown, genre, popularity_score, tab_zero_spoiler, tab_mild_spoiler, tab_full_spoiler, top_themes, what_people_loved, what_people_criticized, audience_engagement.

Output ONLY valid JSON with the updated analysis. Include _comments_analyzed field with total count (old + new)."""


def run_merge_analysis(film: str, existing_analysis: dict, new_comments: list[str], api_key: str) -> dict:
    """Merge new comments into existing analysis. Only processes the delta."""
    if not new_comments or not api_key:
        return existing_analysis
    # Sample new comments
    sample = new_comments[:50]
    if len(new_comments) > 50:
        sample += random.sample(new_comments[50:], min(30, len(new_comments) - 50))
    random.shuffle(sample)
    # Summarize existing analysis for the prompt (keep it compact)
    prev_summary = json.dumps({k: v for k, v in existing_analysis.items() if not k.startswith("_")}, indent=1)[:2000]
    prompt = MERGE_PROMPT.format(
        film=film, previous=prev_summary, count=len(sample),
        new_comments="\n".join(f"{i+1}. {c}" for i, c in enumerate(sample))
    )
    try:
        resp = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "openai/gpt-chat-latest", "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.3, "max_tokens": 4000, "response_format": {"type": "json_object"}},
            timeout=120,
        )
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            content = content.strip()
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
            start, end = content.find("{"), content.rfind("}")
            if start >= 0 and end > start:
                merged = json.loads(content[start:end + 1])
                merged = normalize_analysis(merged)
                merged["_comments_analyzed"] = existing_analysis.get("_comments_analyzed", 0) + len(sample)
                merged["_merge_update"] = True
                return merged
    except Exception:
        pass
    return existing_analysis

# ─── Full Pipeline for a Single Film ──────────────────────────────────────

def process_film(film: str, force: bool = False) -> dict:
    """Full pipeline: search → detect new → fetch comments → LLM → save.
    Supports incremental processing: only new comments are analyzed."""
    api_key = get_youtube_key()
    llm_key = get_openrouter_key()
    registry = load_films()
    entry = registry.get(film, {"last_video_ids": [], "last_checked": None, "added": datetime.now(timezone.utc).isoformat()})

    # Load existing data for incremental processing
    cache_path = OUTPUT_DIR / f"{film.lower().replace(' ','-')}-data.json"
    existing_analysis = {}
    seen_comment_ids = set()
    if cache_path.exists():
        try:
            with open(cache_path) as f:
                existing_data = json.load(f)
            existing_analysis = existing_data.get("analysis", {})
            seen_comment_ids = set(existing_data.get("processed_comment_ids", []))
        except:
            pass

    # Step 1: Search for review videos
    videos = search_review_videos(film, api_key)
    if not videos:
        return {"error": f"No review videos found for {film}"}

    # Step 2: Check if anything new (unless forced)
    # Even without new videos, we still check for new comments on existing videos
    has_new_vids = has_new_videos(film, videos, registry)
    if not force and not has_new_vids and seen_comment_ids:
        # No new videos AND we already have processed comments — check for new comments
        new_comments_total = []
        updated_ids = set(seen_comment_ids)
        for v in videos[:3]:  # Check top 3 videos for new comments
            try:
                new_comments, updated_ids = fetch_new_comments(api_key, v["id"], updated_ids, 80)
                new_comments_total.extend(new_comments)
                v["comment_count"] = len(new_comments)
            except:
                pass
        if len(new_comments_total) < 15:
            # Not enough new comments to warrant re-analysis
            if cache_path.exists():
                with open(cache_path) as f:
                    cached = json.load(f)
                cached["_status"] = "cached"
                cached["_new_comments"] = len(new_comments_total)
                return cached
            return {"error": "No new videos or comments", "_status": "nochange"}
        # Enough new comments — merge them
        analysis = run_merge_analysis(film, existing_analysis, new_comments_total, llm_key)
        seen_comment_ids = updated_ids
        all_comments = new_comments_total
        # Count total comments (existing + new)
        total_comments = existing_analysis.get("_comments_analyzed", 0) + len(new_comments_total)
    else:
        # Full processing: new videos OR first-time
        all_comments = []
        updated_ids = set(seen_comment_ids)
        for v in videos:
            try:
                if force or has_new_vids:
                    # Full fetch for new/forced
                    comments = fetch_comments_api(api_key, v["id"], 100)
                else:
                    # Incremental fetch for existing videos
                    comments, updated_ids = fetch_new_comments(api_key, v["id"], updated_ids, 100)
                v["comment_count"] = len(comments)
                all_comments.extend(comments)
            except Exception:
                v["comment_count"] = 0
        seen_comment_ids = updated_ids
        total_comments = len(all_comments)

        if existing_analysis and all_comments:
            # Merge new comments into existing analysis
            analysis = run_merge_analysis(film, existing_analysis, all_comments, llm_key)
        else:
            # First time — full analysis
            analysis = run_llm_analysis(film, all_comments, llm_key)

    # Step 4b: Hype scoring for upcoming/new films (trailer comments + views/likes)
    hype = {}
    try:
        trailers = search_trailer_videos(film, api_key)
        if trailers:
            trailer_vid_ids = [t["id"] for t in trailers]
            trailer_stats_map = fetch_video_stats(api_key, trailer_vid_ids)
            trailer_comments = []
            for t in trailers[:2]:  # Fetch comments from top 2 trailers
                tc = fetch_comments_api(api_key, t["id"], 100)
                trailer_comments.extend(tc)
            trailer_stats_list = [trailer_stats_map.get(tid, {}) for tid in trailer_vid_ids]
            hype = compute_hype_score(trailer_stats_list, trailer_comments, llm_key, film=film)
    except:
        pass

    # Step 5: Build data payload
    # Spread popularity: LLM base (0-50) + log volume (0-30) + sentiment boost (0-20)
    llm_base = analysis.get("popularity_score", 50) * 0.5  # scale LLM to 0-50
    import math
    vol_bonus = min(30, round(math.log10(max(1, len(all_comments))) * 12))
    sb = analysis.get("sentiment_breakdown", {})
    pos_pct = sb.get("positive_percent", 50)
    sent_bonus = min(20, round(pos_pct / 5))
    popularity_score = min(100, round(llm_base + vol_bonus + sent_bonus))

    data = {
        "film": film,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "total_comments": total_comments if 'total_comments' in dir() else len(all_comments),
        "popularity_score": popularity_score,
        "hype": hype,
        "videos": [{"id": v["id"], "title": v["title"], "channel": v["channel"],
                     "url": f"https://youtube.com/watch?v={v['id']}",
                     "comment_count": v.get("comment_count", 0)} for v in videos],
        "analysis": analysis,
        "processed_comment_ids": list(seen_comment_ids),
        "_status": "fresh",
    }

    # Muted override: if very few comments, mark mood as Muted
    if len(all_comments) < 50 and "tab_zero_spoiler" in analysis:
        analysis["tab_zero_spoiler"]["audience_mood"] = "Muted"

    # Step 6: Save
    OUTPUT_DIR.mkdir(exist_ok=True)
    slug = film.lower().replace(" ", "-")
    with open(OUTPUT_DIR / f"{slug}-raw.json", "w") as f:
        json.dump({"film": film, "videos": videos, "comments": all_comments},
                  f, ensure_ascii=False, indent=2)
    with open(OUTPUT_DIR / f"{slug}-data.json", "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # Step 7: Update registry
    registry[film] = {
        "last_video_ids": [v["id"] for v in videos],
        "last_checked": datetime.now(timezone.utc).isoformat(),
        "added": entry.get("added", datetime.now(timezone.utc).isoformat()),
        "popularity_score": popularity_score,
        "total_comments": len(all_comments),
    }
    save_films(registry)

    return data


def load_film_data(film: str) -> dict:
    """Load film data from cache, or process if missing."""
    slug = film.lower().replace(" ", "-")
    path = OUTPUT_DIR / f"{slug}-data.json"
    if path.exists():
        with open(path) as f:
            data = json.load(f)
        # Add leaderboard context
        registry = load_films()
        entry = registry.get(film, {})
        data["popularity_score"] = data.get("popularity_score", entry.get("popularity_score", 50))
        return data
    return process_film(film)

# ─── Smart Refresh ────────────────────────────────────────────────────────

def smart_refresh():
    """Check all films for new content, update only those with changes."""
    registry = load_films()
    results = {}
    for film in list(registry.keys()):
        result = process_film(film, force=False)
        results[film] = result.get("_status", result.get("error", "error"))
    return results


def get_recency_multiplier(release_date: str) -> float:
    """30-day window. Films older than 30 days excluded (OTT drop).
    0-30 days: full weight → 1.0
    30+ days: excluded (assumed OTT release)
    """
    if not release_date:
        return 0.0
    try:
        from datetime import datetime
        release = datetime.strptime(release_date, "%Y-%m-%d")
        now = datetime.now()
        days = (now - release).days
        if days <= 30: return 1.0
        return 0.0
    except:
        return 0.0


def get_leaderboard() -> list[dict]:
    """Build leaderboard sorted by popularity with recency boost."""
    registry = load_films()
    board = []
    for film, entry in registry.items():
        data = load_film_data(film)
        score = data.get("popularity_score", entry.get("popularity_score", 0))
        analysis = data.get("analysis", {})
        sb = analysis.get("sentiment_breakdown", {})
        meta = FILMS_META.get(film, {})
        release = meta.get("release_date", "")

        multiplier = get_recency_multiplier(release)
        if multiplier == 0.0:
            continue  # skip stale films

        total = data.get("total_comments", 0) or entry.get("total_comments", 0)
        # Skip films with too few comments (noisy data)
        if total < 5:
            continue

        hotness = round(score * multiplier)
        board.append({
            "film": film,
            "release_date": release,
            "popularity_score": score,
            "hotness_score": hotness,
            "total_comments": total,
            "rating": analysis.get("rating"),
            "positive_pct": sb.get("positive_percent"),
            "genre": analysis.get("genre", [])[:2] or (meta.get("language", "") if isinstance(meta.get("language"), str) else ""),
            "buzz": short_mood_label(analysis.get("tab_zero_spoiler", {}).get("audience_mood", "")),
            "fetched_at": data.get("fetched_at", entry.get("last_checked")),
        })
    board.sort(key=lambda x: x.get("hotness_score", 0) or 0, reverse=True)
    return board[:20]


def get_new_releases() -> list[dict]:
    """Return films that are very fresh (<48h old) and have too few comments to analyse yet.
    Once a film has enough data (>50 comments) or is >48h old, it moves to the leaderboard.
    """
    registry = load_films()
    releases = []
    for film, entry in registry.items():
        data = load_film_data(film)
        meta = FILMS_META.get(film, {})
        release = meta.get("release_date", "")
        if not release:
            continue
        total = data.get("total_comments", 0) or entry.get("total_comments", 0)
        score = data.get("popularity_score", entry.get("popularity_score", 0))
        analysis = data.get("analysis", {})
        sb = analysis.get("sentiment_breakdown", {})

        from datetime import datetime
        try:
            rd = datetime.strptime(release, "%Y-%m-%d")
            hours_old = (datetime.now() - rd).total_seconds() / 3600
            # Only show in new releases if <48h old AND <50 comments (not enough data yet)
            if hours_old < 48 and total < 50:
                hype = data.get("hype", {})
                releases.append({
                    "film": film,
                    "release_date": release,
                    "star": meta.get("star", ""),
                    "popularity_score": score,
                    "total_comments": total,
                    "rating": analysis.get("rating"),
                    "buzz": short_mood_label(analysis.get("tab_zero_spoiler", {}).get("audience_mood", "")),
                    "hype_score": hype.get("hype_score", 0),
                    "trailer_views": hype.get("total_views", 0),
                    "vibe": hype.get("vibe", ""),
                })
        except:
            pass
    releases.sort(key=lambda x: x.get("release_date", "2000-01-01"), reverse=True)
    return releases[:5]


# ─── Trailer Leaderboard ──────────────────────────────────────────────────

def check_wiki_release_date(film_name: str) -> tuple[str | None, str | None, str | None]:
    """Check Wikipedia for a film's release date and cast.
    Returns (release_date, cast_string, wiki_url) or (None, None, None).
    Generalized: works for any film name, caches results."""
    import urllib.parse, re

    cache_path = OUTPUT_DIR / "wiki-cache.json"
    cache = {}
    if cache_path.exists():
        try:
            with open(cache_path) as f:
                cache = json.load(f)
        except:
            pass

    # Check cache first
    cache_key = film_name.lower().strip()
    if cache_key in cache:
        entry = cache[cache_key]
        return entry.get("release_date"), entry.get("cast"), entry.get("url")

    # Try multiple Wikipedia title patterns
    title_patterns = [
        f"{film_name} (2026 film)",
        f"{film_name} (2025 film)",
        f"{film_name} (film)",
        film_name,
    ]

    for title in title_patterns:
        try:
            escaped = urllib.parse.quote(title)
            resp = httpx.get(
                f"https://en.wikipedia.org/w/api.php?action=parse&page={escaped}&prop=text&format=json",
                timeout=10,
                headers={"User-Agent": "TamilMovieDashboard/1.0"},
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            if "error" in data:
                continue
            html = data.get("parse", {}).get("text", {}).get("*", "")
            wiki_url = f"https://en.wikipedia.org/wiki/{escaped}"

            # Extract release date from infobox
            release_date = None
            # Look for "Release date" or "Opening" in infobox
            date_patterns = [
                r'(?:release\s+date|opening)\s*</th>\s*<td[^>]*>(.*?)</td>',
                r'(?:release\s+date|opening)\s*</th>\s*<td[^>]*>\s*<[^>]*>(.*?)</',
            ]
            for pat in date_patterns:
                m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
                if m:
                    date_text = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                    # Extract ISO date or structured date
                    iso_m = re.search(r'(\d{4})-(\d{2})-(\d{2})', date_text)
                    if iso_m:
                        release_date = iso_m.group(0)
                        break
                    # Try "14 June 2026" format
                    full_date = re.search(
                        r'(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})',
                        date_text, re.IGNORECASE
                    )
                    if full_date:
                        month_map = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
                                     "july":7,"august":8,"september":9,"october":10,"november":11,"december":12}
                        d, m_name, y = full_date.group(1), full_date.group(2).lower(), full_date.group(3)
                        release_date = f"{y}-{month_map[m_name]:02d}-{int(d):02d}"
                        break

            # Extract cast from infobox
            cast = None
            cast_m = re.search(r'(?:starring|cast)\s*</th>\s*<td[^>]*>(.*?)</td>', html, re.IGNORECASE | re.DOTALL)
            if cast_m:
                cast_text = re.sub(r'<[^>]+>', '', cast_m.group(1)).strip()
                # Clean up: take first few names
                names = [n.strip() for n in cast_text.split(',') if n.strip()]
                cast = ', '.join(names[:5])

            if release_date or cast:
                # Cache the result
                cache[cache_key] = {"release_date": release_date, "cast": cast, "url": wiki_url}
                OUTPUT_DIR.mkdir(exist_ok=True)
                with open(cache_path, "w") as f:
                    json.dump(cache, f, ensure_ascii=False, indent=2)
                return release_date, cast, wiki_url
        except:
            continue

    # Cache negative result too
    cache[cache_key] = {"release_date": None, "cast": None, "url": None}
    OUTPUT_DIR.mkdir(exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    return None, None, None


def check_wiki_director(film_name: str) -> str | None:
    """Extract director from Wikipedia infobox. Uses same cache as release date."""
    import urllib.parse
    cache_path = OUTPUT_DIR / "wiki-cache.json"
    cache = {}
    if cache_path.exists():
        try:
            with open(cache_path) as f:
                cache = json.load(f)
        except:
            pass
    cache_key = film_name.lower().strip()
    entry = cache.get(cache_key, {})
    if "director" in entry:
        return entry.get("director")

    title_patterns = [
        f"{film_name} (2026 film)",
        f"{film_name} (2025 film)",
        f"{film_name} (film)",
        film_name,
    ]
    for title in title_patterns:
        try:
            escaped = urllib.parse.quote(title)
            resp = httpx.get(
                f"https://en.wikipedia.org/w/api.php?action=parse&page={escaped}&prop=text&format=json",
                timeout=10,
                headers={"User-Agent": "TamilMovieDashboard/1.0"},
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            if "error" in data:
                continue
            html = data.get("parse", {}).get("text", {}).get("*", "")
            dir_m = re.search(r'(?:directed\s+by|director)\s*</th>\s*<td[^>]*>(.*?)</td>', html, re.IGNORECASE | re.DOTALL)
            if dir_m:
                dir_text = re.sub(r'<[^>]+>', '', dir_m.group(1)).strip()
                names = [n.strip() for n in dir_text.split(',') if n.strip()]
                director = ', '.join(names[:3])
                entry["director"] = director
                cache[cache_key] = entry
                OUTPUT_DIR.mkdir(exist_ok=True)
                with open(cache_path, "w") as f:
                    json.dump(cache, f, ensure_ascii=False, indent=2)
                return director
        except:
            continue

    entry["director"] = None
    cache[cache_key] = entry
    OUTPUT_DIR.mkdir(exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    return None


def check_wiki_music(film_name: str) -> str | None:
    """Extract music composer from Wikipedia infobox. Uses same cache as release date."""
    import urllib.parse
    cache_path = OUTPUT_DIR / "wiki-cache.json"
    cache = {}
    if cache_path.exists():
        try:
            with open(cache_path) as f:
                cache = json.load(f)
        except:
            pass
    cache_key = film_name.lower().strip()
    entry = cache.get(cache_key, {})
    if "music" in entry:
        return entry.get("music")

    title_patterns = [
        f"{film_name} (2026 film)",
        f"{film_name} (2025 film)",
        f"{film_name} (film)",
        film_name,
    ]
    for title in title_patterns:
        try:
            escaped = urllib.parse.quote(title)
            resp = httpx.get(
                f"https://en.wikipedia.org/w/api.php?action=parse&page={escaped}&prop=text&format=json",
                timeout=10,
                headers={"User-Agent": "TamilMovieDashboard/1.0"},
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            if "error" in data:
                continue
            html = data.get("parse", {}).get("text", {}).get("*", "")
            music_m = re.search(r'(?:music\s+by|composed\s+by|music\s+director)\\s*</th>\\s*<td[^>]*>(.*?)</td>', html, re.IGNORECASE | re.DOTALL)
            if music_m:
                music_text = re.sub(r'<[^>]+>', '', music_m.group(1)).strip()
                names = [n.strip() for n in music_text.split(',') if n.strip()]
                music = ', '.join(names[:3])
                entry["music"] = music
                cache[cache_key] = entry
                OUTPUT_DIR.mkdir(exist_ok=True)
                with open(cache_path, "w") as f:
                    json.dump(cache, f, ensure_ascii=False, indent=2)
                return music
        except:
            continue

    entry["music"] = None
    cache[cache_key] = entry
    OUTPUT_DIR.mkdir(exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    return None


def get_trailer_leaderboard() -> list[dict]:
    """Search YouTube for upcoming Tamil movie trailers and rank by hype.
    Returns films NOT yet released (release_date >= today) sorted by hype score.
    Caches results for 6 hours to avoid excessive API calls."""
    import math as _math
    from datetime import datetime, timedelta

    cache_path = OUTPUT_DIR / "trailer-leaderboard-cache.json"
    # Return cached data if available (even if expired) — rebuild only on
    # explicit /api/refresh-trailer call or cron. Prevents blocking GET
    # requests with a full YouTube + LLM rebuild.
    if cache_path.exists():
        try:
            with open(cache_path) as f:
                cache = json.load(f)
            return cache.get("trailers", [])
        except:
            pass

    # No cache at all — do a first build (only if explicitly triggered or first run)
    api_key = get_youtube_key()
    llm_key = get_openrouter_key()
    if not api_key:
        return []

    # Search for upcoming Tamil movie trailers
    queries = [
        "upcoming tamil movie trailer 2026",
        "tamil movie trailer 2026",
        "new tamil movie teaser 2026",
        "tamil film official trailer",
        "tollywood trailer 2026 tamil",
    ]

    seen_trailers: dict[str, dict] = {}  # film_name -> {id, title, channel, published}
    skip_kw = ["review", "reaction", "behind the scenes", "interview", "#shorts", "box office", "collection"]
    dubbed_kw = ["tamil dubbed", "dubbed in tamil", "dubbed", "telugu to tamil", "hindi to tamil", "malayalam to tamil", " kannada to tamil"]

    for q in queries:
        if len(seen_trailers) >= 30:
            break
        try:
            params = {"part": "snippet", "q": q, "type": "video", "maxResults": 10,
                      "relevanceLanguage": "ta", "key": api_key, "order": "relevance"}
            resp = httpx.get(f"{YOUTUBE_API_BASE}/search", params=params, timeout=15)
            if resp.status_code != 200:
                continue
            for item in resp.json().get("items", []):
                vid = item["id"]["videoId"]
                title = item["snippet"]["title"].lower()
                if any(kw in title for kw in skip_kw):
                    continue
                if any(kw in title for kw in dubbed_kw):
                    continue  # Skip dubbed versions of other language films
                if "trailer" not in title and "teaser" not in title:
                    continue
                # Skip old content
                published = item["snippet"]["publishedAt"]
                if published < "2025-01-01":
                    continue
                if vid not in seen_trailers:
                    seen_trailers[vid] = {
                        "id": vid,
                        "title": item["snippet"]["title"],
                        "channel": item["snippet"]["channelTitle"],
                        "published": published,
                    }
        except:
            continue

    if not seen_trailers:
        return []

    # Fetch stats for all trailers
    all_ids = list(seen_trailers.keys())
    stats_map = fetch_video_stats(api_key, all_ids)

    # Fetch comments for top trailers (by views)
    sorted_vids = sorted(all_ids, key=lambda v: stats_map.get(v, {}).get("views", 0), reverse=True)

    # Group trailers by likely film name (extract from title)
    # Simple heuristic: take the first few words before "trailer"/"teaser"
    film_trailers: dict[str, list[dict]] = {}

    def clean_film_name(raw: str) -> str:
        """Clean film name extracted from YouTube title for Wikipedia lookup."""
        name = raw.strip().rstrip("-–|·").strip()
        # Remove common suffixes/prefixes from YouTube titles
        remove_patterns = [
            r'\(tamil\)', r'\(telugu\)', r'\(hindi\)', r'\(malayalam\)',
            r'official', r'tamil', r'telugu', r'hindi', r'malayalam',
            r'trailer', r'teaser', r'lyrical', r'song', r'video',
            r'#\w+',  # hashtags
        ]
        for pat in remove_patterns:
            name = re.sub(pat, '', name, flags=re.IGNORECASE)
        name = re.sub(r'[-–|·]+', ' ', name)  # replace separators with spaces
        name = re.sub(r'\s+', ' ', name).strip()
        # Remove trailing years
        name = re.sub(r'\s*\d{4}\s*$', '', name).strip()
        return name if len(name) > 1 else raw[:40]

    for vid in sorted_vids:
        t = seen_trailers[vid]
        title_lower = t["title"].lower()
        # Extract film name: everything before "official trailer", "trailer", "teaser"
        for marker in ["official trailer", "trailer", "teaser", "official teaser"]:
            idx = title_lower.find(marker)
            if idx > 0:
                film_name = clean_film_name(t["title"][:idx])
                break
        else:
            film_name = clean_film_name(t["title"][:40])

        if not film_name:
            film_name = t["title"][:40]

        if film_name not in film_trailers:
            film_trailers[film_name] = []
        film_trailers[film_name].append({
            "id": vid,
            "stats": stats_map.get(vid, {}),
            "title": t["title"],
            "channel": t["channel"],
            "published": t["published"],
        })

    # Manual overrides for films Wikipedia can't find by name
    # Maps cleaned film name -> actual release date (YYYY-MM-DD)
    MANUAL_RELEASE_DATES = {
        "tn2026": "2026-05-01",  # Ajith Kumar film, already released
        "#tn2026": "2026-05-01",
        "#tn2026 - ": "2026-05-01",
        "sattendru maarudhu vaanilai": "2026-06-12",  # Already released
        "sattendru maarudhu": "2026-06-12",
        "gandhi talks": "2026-01-30",  # Released Jan 30 2026
    }

    # Compute hype for each film group
    from datetime import datetime as _dt
    results = []
    for film_name, trailers in film_trailers.items():
        # Skip if this film is already tracked in the registry (it's released)
        registry = load_films()
        if film_name in registry or film_name.lower() in [k.lower() for k in registry]:
            continue

        # Check manual overrides for known released films
        manual_key = film_name.lower().strip().strip("#-–").strip()
        if manual_key in MANUAL_RELEASE_DATES:
            try:
                release = _dt.strptime(MANUAL_RELEASE_DATES[manual_key], "%Y-%m-%d")
                if release < _dt.now():
                    continue  # Already released, skip from trailer board
            except:
                pass

        # Check Wikipedia FIRST — skip released films before wasting API quota
        wiki_release, wiki_cast, wiki_url = check_wiki_release_date(film_name)
        wiki_director = check_wiki_director(film_name)
        wiki_music = check_wiki_music(film_name)
        if wiki_release:
            try:
                release = _dt.strptime(wiki_release, "%Y-%m-%d")
                if release < _dt.now():
                    continue  # Already released, skip from trailer board
            except:
                pass

        # NOW fetch comments and run LLM (only for unreleased films)
        trailer_stats = [t["stats"] for t in trailers]
        total_views = sum(s.get("views", 0) for s in trailer_stats)
        total_likes = sum(s.get("likes", 0) for s in trailer_stats)

        # Extract metadata from trailer descriptions (free, already fetched with stats)
        descriptions = [t["stats"].get("description", "") for t in trailers if t["stats"].get("description")]
        desc_meta = extract_metadata_from_descriptions(descriptions) if descriptions else {}

        # Fetch comments from top 2 trailers
        trailer_comments = []
        comment_counts = {}
        for t in trailers[:2]:
            tc = fetch_comments_api(api_key, t["id"], 80)
            comment_counts[t["id"]] = len(tc)
            trailer_comments.extend(tc)

        # Compute hype
        hype = compute_hype_score(trailer_stats, trailer_comments, llm_key, film=film_name)

        # Run full sentiment analysis (pre-computed for detail page)
        sentiment_data = {}
        if trailer_comments and llm_key:
            sentiment_data = run_llm_analysis(film_name, trailer_comments, llm_key)

        results.append({
            "film": film_name,
            "hype_score": hype.get("hype_score", 0),
            "total_views": total_views,
            "total_likes": total_likes,
            "like_ratio": hype.get("like_ratio", 0),
            "total_comments": len(trailer_comments),
            "comment_count": len(trailer_comments),
            "sentiment": hype.get("sentiment", "unknown"),
            "vibe": hype.get("vibe", ""),
            "category": hype.get("category", "Anticipated"),
            "genre_phrase": hype.get("genre_phrase", ""),
            "rating_label": hype.get("rating_label", ""),
            "tagline": hype.get("tagline", ""),
            "trailer_count": len(trailers),
            "latest_trailer": trailers[0]["title"] if trailers else "",
            "channel": trailers[0]["channel"] if trailers else "",
            "cast": wiki_cast or desc_meta.get("cast", ""),
            "director": wiki_director or desc_meta.get("director", ""),
            "music": wiki_music or desc_meta.get("music", ""),
            "release_date": wiki_release or desc_meta.get("release_date", ""),
            "wiki_url": wiki_url or "",
            "trailer_ids": [t["id"] for t in trailers],
            "videos": [{"id": t["id"], "url": f"https://youtube.com/watch?v={t['id']}", "title": t["title"], "comment_count": comment_counts.get(t["id"], 0)} for t in trailers],
            "analysis": sentiment_data,
        })

    results.sort(key=lambda x: x.get("hype_score", 0), reverse=True)

    # Cache results
    OUTPUT_DIR.mkdir(exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump({"cached_at": datetime.now().isoformat(), "trailers": results[:15]}, f, ensure_ascii=False, indent=2)

    return results[:15]


# ─── Wikipedia Weekly Release Scraper ────────────────────────────────────

WIKI_URLS = [
    "https://en.wikipedia.org/wiki/List_of_Tamil_films_of_2026",
    "https://en.wikipedia.org/wiki/List_of_Tamil_films_of_2025",
]

MONTH_MAP = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "MAY": 5, "JUNE": 6,
    "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
}

def scrape_wiki_releases() -> list[dict]:
    """Scrape Wikipedia for recent Tamil film releases with release dates.
    Handles 3 row types: month header (7 cols), day row (6 cols, rowspan on day),
    shared-day row (5 cols, no day cell)."""
    import httpx
    from bs4 import BeautifulSoup
    import re
    films = []
    for url in WIKI_URLS:
        year_match = re.search(r"(\d{4})", url)
        year = year_match.group(1) if year_match else "2026"
        try:
            resp = httpx.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            tables = soup.find_all("table", class_="wikitable")
            for table in tables:
                rows = table.find_all("tr")
                if len(rows) < 5:
                    continue
                header = rows[0].find_all(["th", "td"])
                header_text = [h.get_text(strip=True) for h in header]
                if "Opening" not in header_text and "Title" not in header_text:
                    continue
                current_month = None
                current_day = None
                for row in rows[1:]:
                    cols = row.find_all(["td", "th"])
                    if not cols:
                        continue
                    n = len(cols)
                    first_text = cols[0].get_text(strip=True).upper()
                    # Month header: 7 cols (MONTH | DAY | TITLE | ...)
                    if first_text in MONTH_MAP:
                        current_month = MONTH_MAP[first_text]
                        if n > 1 and cols[1].get_text(strip=True).isdigit():
                            current_day = int(cols[1].get_text(strip=True))
                        title = cols[2].get_text(strip=True) if n > 2 else ""
                    # Day row: 6 cols with rowspan on day cell
                    elif n >= 6 and first_text.isdigit() and cols[0].get("rowspan"):
                        current_day = int(first_text)
                        title = cols[1].get_text(strip=True)
                    # Day row without rowspan (e.g. Ananthan Kaadu "25")
                    elif n >= 6 and first_text.isdigit():
                        current_day = int(first_text)
                        title = cols[1].get_text(strip=True)
                    # Shared-day row: 5 cols (no day cell)
                    elif n == 5 and current_month is not None:
                        title = cols[0].get_text(strip=True)
                    else:
                        continue
                    title = title.split("[")[0].split("\n")[0].strip()
                    if not title or title in ("Title", "Opening"):
                        continue
                    if current_month is not None and current_day is not None:
                        date_str = f"{year}-{current_month:02d}-{current_day:02d}"
                        films.append({"name": title, "release_date": date_str})
        except:
            continue
    return films


@app.get("/api/scrape-releases")
async def api_scrape_releases():
    films = await asyncio.to_thread(scrape_wiki_releases)
    return JSONResponse(content=films, headers={"Cache-Control": "public, max-age=3600"})

@app.get("/api/tunnel-url")
async def api_tunnel_url():
    """Return the permanent Cloudflare tunnel URL."""
    return JSONResponse(content={"url": "https://movies.onekural.com"})

# ─── API Routes ───────────────────────────────────────────────────────────

@app.get("/")
async def api_root():
    """Serve the dashboard frontend."""
    frontend_path = REPO_DIR / "frontend.html"
    if frontend_path.exists():
        with open(frontend_path) as f:
            html = f.read()
        html = html.replace("__VITE_API_KEY__", API_KEY)
        vps_ip = os.environ.get("VPS_IP", "http://localhost:8080")
        html = html.replace("__VITE_VPS_IP__", vps_ip)
        return HTMLResponse(content=html)
    return JSONResponse(content={"error": "Frontend not found"})

@app.get("/api/leaderboard")
async def api_leaderboard():
    data = await asyncio.to_thread(get_leaderboard)
    return JSONResponse(content=data, headers={"Cache-Control": "public, max-age=18000, s-maxage=18000"})

@app.get("/api/new-releases")
async def api_new_releases():
    data = await asyncio.to_thread(get_new_releases)
    return JSONResponse(content=data, headers={"Cache-Control": "public, max-age=18000, s-maxage=18000"})

@app.get("/api/trailer-leaderboard")
async def api_trailer_leaderboard():
    data = await asyncio.to_thread(get_trailer_leaderboard)
    return JSONResponse(content=data, headers={"Cache-Control": "public, max-age=18000, s-maxage=18000"})

@app.post("/api/refresh-trailer")
async def api_refresh_trailer():
    """Force refresh trailer leaderboard cache. Call from cron."""
    cache_path = OUTPUT_DIR / "trailer-leaderboard-cache.json"
    if cache_path.exists():
        cache_path.unlink()
    data = await asyncio.to_thread(get_trailer_leaderboard)
    return JSONResponse(content={"status": "ok", "count": len(data)})

@app.get("/api/trailer/{film}")
async def api_trailer_detail(film: str):
    """Get detail data for a trailer leaderboard film. Serves from cache only."""
    cache_path = OUTPUT_DIR / "trailer-leaderboard-cache.json"
    if not cache_path.exists():
        return JSONResponse(content={"error": "Trailer leaderboard not built yet"}, status_code=404)
    with open(cache_path) as f:
        cache = json.load(f)
    
    for t in cache.get("trailers", []):
        if t["film"].lower() == film.lower():
            return JSONResponse(content=t, headers={"Cache-Control": "public, max-age=18000, s-maxage=18000"})
    
    return JSONResponse(content={"error": f"Film '{film}' not found in trailer leaderboard"}, status_code=404)

@app.get("/api/film/{film}")
async def api_film(film: str):
    data = await asyncio.to_thread(load_film_data, film)
    if "error" in data and data.get("_status") == "nochange":
        data = await asyncio.to_thread(load_film_data, film)
    # Enrich with cast from FILMS_META
    meta = FILMS_META.get(film, {})
    if meta.get("star"):
        data["cast"] = meta["star"]
    if meta.get("release_date"):
        data["release_date"] = meta["release_date"]
    # Try to get director from wiki cache
    if not data.get("director"):
        data["director"] = await asyncio.to_thread(check_wiki_director, film) or ""
    if not data.get("music"):
        data["music"] = await asyncio.to_thread(check_wiki_music, film) or ""
    return JSONResponse(content=data, headers={"Cache-Control": "public, max-age=18000, s-maxage=18000"})

@app.post("/api/film/{film}/refresh")
async def api_refresh_film(film: str):
    data = await asyncio.to_thread(process_film, film, force=True)
    return JSONResponse(content={"status": "ok" if "_status" in data else "error", "film": film})

@app.post("/api/refresh")
async def api_refresh_all():
    results = await asyncio.to_thread(smart_refresh)
    updated = [f for f, s in results.items() if s == "fresh"]
    return JSONResponse(content={"status": "ok", "updated": updated})

@app.get("/api/films")
async def api_films_list():
    registry = load_films()
    return JSONResponse(content=list(registry.keys()))

@app.post("/api/films/add/{film}")
async def api_add_film(film: str):
    registry = await asyncio.to_thread(load_films)
    if film not in registry:
        registry[film] = {"last_video_ids": [], "last_checked": None, "added": datetime.now(timezone.utc).isoformat()}
        await asyncio.to_thread(save_films, registry)
    data = await asyncio.to_thread(process_film, film, force=True)
    return JSONResponse(content={"status": "ok", "film": film})


@app.post("/api/deploy-frontend")
async def api_deploy_frontend():
    """Hot-deploy frontend.html — accepts HTML body and writes to disk."""
    import sys
    body = await request.body()
    path = REPO_DIR / "frontend.html"
    path.write_bytes(body)
    return JSONResponse(content={"status": "ok", "path": str(path)})


@app.post("/api/restart-tunnel")
async def api_restart_tunnel():
    """Restart cloudflared tunnel service."""
    import subprocess
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "restart", "cloudflared-tunnel.service"],
            capture_output=True, text=True, timeout=30
        )
        return JSONResponse(content={
            "status": "ok" if result.returncode == 0 else "error",
            "output": result.stdout,
            "error": result.stderr,
        })
    except Exception as e:
        return JSONResponse(content={"status": "error", "error": str(e)})




# ─── Standalone ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
