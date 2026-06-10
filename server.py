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
API_KEY = os.environ.get("API_KEY", "dev-key-change-in-prod")
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

# ─── Film Metadata (release dates + OTT) ────────────────────
FILMS_META = {
    "Parimala and Co":        {"release_date": "2026-06-05", "star": "Jayaram, Urvashi, Mysskin, Pandiraaj", "ott_release_date": None, "ott_platform": None},
    "Blast":                  {"release_date": "2026-05-28", "star": "Arjun, Preity, Abhirami", "ott_release_date": None, "ott_platform": None},
    "Karuppu":                {"release_date": "2026-05-15", "star": "Suriya, Trisha, RJ Balaji", "ott_release_date": None, "ott_platform": None},
    "29":                     {"release_date": "2026-05-08", "star": "Vidhu, Preethi Asrani", "ott_release_date": None, "ott_platform": "Netflix"},
    "Love Insurance Kompany": {"release_date": "2026-04-10", "star": "Pradeep Ranganathan, SJ Suryah, Krithi Shetty", "ott_release_date": "2026-05-06", "ott_platform": "Amazon Prime Video"},
    "Mr. X":                  {"release_date": "2026-04-17", "star": "Arya, Gautham Ram Karthik", "ott_release_date": "2026-05-14", "ott_platform": "Disney+ Hotstar"},
    "Battle":                 {"release_date": "2026-04-24", "star": "Arjun Prabhakaran, Aradhya Krishna", "ott_release_date": None, "ott_platform": None},
    "Kara":                   {"release_date": "2026-04-30", "star": "Dhanush, Mamitha Baiju, Jayaram", "ott_release_date": "2026-05-29", "ott_platform": "Netflix"},
    "Retta Thala":            {"release_date": "2026-03-20", "star": "Arun Vijay, Siddhi", "ott_release_date": None, "ott_platform": None},
}

# ─── OTT keyword detection ─────────────────────────────────────
# Keywords that identify a video as OTT-focused (not theatrical)
OTT_TITLE_KEYWORDS = [
    "ott release", "ott review", "ott watch", "ott discussion",
    "netflix review", "netflix release", "prime video review",
    "streaming now", "digital release", "digital premiere",
    "now streaming", "streaming review",
    # Platform names in titles
    "netflix", "prime video", "amazon prime", "disney+", "hotstar",
    "aha tamil", "zee5", "sony liv",
]

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

def is_ott_video(video: dict) -> bool:
    """Check if a video is about OTT/post-theatrical discussion based on title."""
    title = video.get("title", "").lower()
    # Check title for OTT keywords (phrase matching, not bare substring)
    for kw in OTT_TITLE_KEYWORDS:
        if kw in title:
            return True
    return False


def search_review_videos(film: str, api_key: str, max_videos: int = 6) -> list[dict]:
    """Search YouTube for review videos. Returns [{id, title, channel, published, is_ott, ott_source}]."""
    queries = [
        f"{film} movie review tamil",
        f"{film} review tamil",
        f"{film} public review",
        f"{film} honest review",
        f"{film} Baradwaj Rangan",
        f"{film} galatta plus",
        # OTT-specific queries
        f"{film} OTT review tamil",
        f"{film} ott watch review",
        f"{film} netflix review tamil",
        f"{film} prime video tamil review",
        f"{film} streaming tamil",
    ]
    seen = {}
    for q in queries:
        if len(seen) >= max_videos * 2:  # collect more candidates, filter down later
            break
        params = {"part": "snippet", "q": q, "type": "video", "maxResults": 15,
                  "relevanceLanguage": "ta", "key": api_key, "order": "relevance"}
        resp = httpx.get(f"{YOUTUBE_API_BASE}/search", params=params, timeout=15)
        if resp.status_code != 200:
            continue
        for item in resp.json().get("items", []):
            vid = item["id"]["videoId"]
            title = item["snippet"]["title"].lower()
            # Skip shorts and non-review content
            if any(kw in title for kw in ["#shorts", "deleted scene", "collection", "24th day", "box office"]):
                continue
            if vid not in seen:
                v = {
                    "id": vid,
                    "title": item["snippet"]["title"],
                    "channel": item["snippet"]["channelTitle"],
                    "published": item["snippet"]["publishedAt"],
                }
                v["is_ott"] = is_ott_video(v)
                seen[vid] = v
    # Ensure we get a mix: at least 1 OTT video if available
    all_videos = list(seen.values())
    ott_vids = [v for v in all_videos if v["is_ott"]]
    theatrical_vids = [v for v in all_videos if not v["is_ott"]]
    # Return up to max_videos, ensuring at most max_videos total with OTT priority
    result = theatrical_vids[:4] + ott_vids[:2]
    return result[:max_videos]


def has_new_videos(film: str, videos: list[dict], registry: dict) -> bool:
    """Check if any new review videos appeared since last check."""
    entry = registry.get(film, {})
    old_ids = set(entry.get("last_video_ids", []))
    new_ids = {v["id"] for v in videos}
    return bool(new_ids - old_ids)


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

# ─── LLM Analysis (3-tier) ────────────────────────────────────────────────

ANALYSIS_PROMPT_TEMPLATE = """You are a Tamil cinema review analyst. Analyze these YouTube comments about the Tamil film "{film}".

Comments are in Tamil, English, or Tanglish.

Output ONLY valid JSON:
{{
  "rating": <0-10, one decimal>,
  "sentiment_breakdown": {{"positive_percent": N, "negative_percent": N, "mixed_percent": N, "neutral_percent": N}},
  "genre": ["<genre 1>", "<genre 2>", "<genre 3>"],
  "popularity_score": <0-100 based on discussion volume and energy>,

  "tab_zero_spoiler": {{
    "vibe_summary": "<2 sentences on the overall audience vibe/reaction, zero plot details>",
    "audience_mood": "<one word like: Celebratory/Mixed/Disappointed/Excited/Muted>",
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
  "what_people_loved": ["<thing>", "<thing>", "<thing>"],
  "what_people_criticized": ["<thing>", "<thing>"],

  "audience_engagement": {{
    "theatre_response": "<one sentence>",
    "comparisons": ["<comparison 1>", "<comparison 2>"]
  }}
}}

Comments:
{comments}"""

OTT_ANALYSIS_PROMPT_TEMPLATE = """You are a Tamil cinema review analyst. Analyze these YouTube comments about the Tamil film "{film}".

IMPORTANT CONTEXT: These comments are from POST-OTT release discussions — people watching the film on Netflix/Prime Video/Disney+ Hotstar AFTER its theatrical run. The vibe and expectations are often DIFFERENT from theatrical reactions.

Comments are in Tamil, English, or Tanglish.

Focus your analysis on catching the OTT-specific discourse:
- How does the OTT audience's reaction DIFFER from theatrical audience?
- Are people watching with family vs solo? Is the context different?
- Do OTT viewers have a more balanced/critical take since they didn't pay for tickets?
- Any comparisons between "theatre experience" and "home watch"?

Output ONLY valid JSON:
{{
  "rating": <0-10, one decimal>,
  "sentiment_breakdown": {{"positive_percent": N, "negative_percent": N, "mixed_percent": N, "neutral_percent": N}},
  "genre": ["<genre 1>", "<genre 2>", "<genre 3>"],
  "popularity_score": <0-100>,

  "ott_vibe": {{
    "ott_mood": "<one word: Rediscovered/LessHype/Mixed/FamilyFriendly/StillExciting/Disappointed>",
    "how_it_differs": "<2-3 sentences on how the OTT discussion vibe differs from theatrical — is it more relaxed? More critical? Family watching? New disagreements?>",
    "theatrical_vs_ott_verdict": "<one sentence: do OTT viewers agree with theatrical verdict, or is there a shift?>"
  }},

  "tab_zero_spoiler": {{
    "vibe_summary": "<2 sentences on the overall audience vibe from OTT viewpoint>",
    "audience_mood": "<one word>",
    "performance_highlights": "<OTT audience takes on performances — any new appreciation?>",
    "technical_highlights": "<OTT audience takes on craft, BGM, visuals from home-viewing perspective>"
  }},

  "tab_mild_spoiler": {{
    "first_half_vibe": "<OTT audience take on first half — does it hold up on rewatch?>",
    "second_half_vibe": "<OTT audience take on second half — does it feel different at home?>",
    "why_watch_now": "<why should someone watch it on OTT right now, based on audience comments>"
  }},

  "top_themes": [{{"theme": "<theme>", "frequency": "very high/high/medium/low", "sentiment": "positive/negative/mixed"}}],
  "what_people_loved": ["<thing>", "<thing>", "<thing>"],
  "what_people_criticized": ["<thing>", "<thing>"]
}}

Comments:
{comments}"""



def scrape_wiki_ott_info(film: str) -> tuple[str | None, str | None]:
    """Scrape Wikipedia article for OTT release date and platform.
    Returns (ott_date, ott_platform) or (None, None).
    Looks for 'Home media' section with streaming platform info."""
    import re
    import httpx
    import urllib.parse
    
    titles = [
        f"{film} (2026 film)",
        f"{film} (2025 film)",
        f"{film} (film)",
        film,
    ]
    platform_keywords = {
        "netflix": "Netflix",
        "prime video": "Amazon Prime Video",
        "amazon prime": "Amazon Prime Video",
        "disney+": "Disney+ Hotstar",
        "hotstar": "Disney+ Hotstar",
        "aha": "Aha Tamil",
        "zee5": "ZEE5",
        "sony liv": "Sony LIV",
    }
    month_map = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
                 "july":7,"august":8,"september":9,"october":10,"november":11,"december":12}
    
    for title in titles:
        try:
            escaped = urllib.parse.quote(title)
            resp = httpx.get(
                f"https://en.wikipedia.org/w/api.php?action=parse&page={escaped}&prop=text&format=json",
                timeout=10,
                headers={"User-Agent": "TamilMovieDashboard/1.0"}
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            if "error" in data:
                continue
            html = data.get("parse", {}).get("text", {}).get("*", "")
            
            # Find the Home media section
            h3_start = html.find("id=\"Home_media\"")
            if h3_start < 0:
                h3_start = html.find("Home_media")
            if h3_start < 0:
                continue
            
            h3_close = html.find("</h3>", h3_start)
            if h3_close < 0:
                continue
            
            # Get all content until next section heading
            remaining = html[h3_close:]
            next_section = re.search(r'<(h[23]|div\s+class="mw-heading")', remaining)
            section_html = remaining[:next_section.start()] if next_section else remaining[:2000]
            
            # Extract text from <p> tags
            p_tags = re.findall(r'<p>(.*?)</p>', section_html, re.DOTALL)
            section_text = " ".join(re.sub(r'<[^>]+>', '', p) for p in p_tags)
            section_text = re.sub(r'\s+', ' ', section_text).strip()
            
            if not section_text:
                continue
            
            # Detect platform
            platform = None
            for kw, plat in platform_keywords.items():
                if kw in section_text.lower():
                    platform = plat
                    break
            
            # Detect date
            date_match = re.search(
                r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
                section_text
            )
            ott_date = None
            if date_match:
                day = int(date_match.group(1))
                month = month_map.get(date_match.group(2).lower(), 1)
                year = date_match.group(3)
                ott_date = f"{year}-{month:02d}-{day:02d}"
            
            if platform or ott_date:
                return ott_date, platform
        except:
            continue
    return None, None



def fetch_video_details(video_ids: list[str], api_key: str) -> list[dict]:
    """Fetch full snippet (incl. full description) for a batch of video IDs."""
    if not video_ids:
        return []
    results = []
    # Process in batches of 50 (YouTube API limit)
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i+50]
        try:
            params = {"part": "snippet", "id": ",".join(batch), "key": api_key}
            resp = httpx.get(f"{YOUTUBE_API_BASE}/videos", params=params, timeout=15)
            if resp.status_code == 200:
                for item in resp.json().get("items", []):
                    snip = item.get("snippet", {})
                    results.append({
                        "id": item["id"],
                        "title": snip.get("title", ""),
                        "description": snip.get("description", ""),
                        "published": snip.get("publishedAt", ""),
                        "channel": snip.get("channelTitle", ""),
                    })
        except:
            pass
    return results


PLATFORM_KEYWORDS = {
    "netflix": "Netflix",
    "prime video": "Amazon Prime Video",
    "amazon prime": "Amazon Prime Video",
    "disney+": "Disney+ Hotstar",
    "hotstar": "Disney+ Hotstar",
    "aha": "Aha Tamil",
    "zee5": "ZEE5",
    "sony liv": "Sony LIV",
}

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}
MONTH_ABBR = {m[:3]: v for m, v in MONTH_NAMES.items()}
MONTH_LOOKUP = {**MONTH_NAMES, **MONTH_ABBR}

DATE_PATTERNS = [
    # ISO format: 2026-05-29
    r"(\d{4})-(\d{1,2})-(\d{1,2})",
    # "May 29, 2026" or "29 May 2026"
    r"(\d{1,2})\s+(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s*,?\s*(202[456789])",
    r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2})\s*,?\s*(202[456789])",
    # "releases on 29th" or "available from 29 May"
    r"releas(?:es|ed)\s+(?:on\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b",
    r"(?:available|streaming|coming|out)\s+(?:from|on|since)\s+(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2})",
]

def parse_date_match(m: re.Match) -> str | None:
    """Convert a regex match from DATE_PATTERNS into YYYY-MM-DD string."""
    try:
        groups = m.groups()
        # ISO format: (YYYY, MM, DD)
        if len(groups) >= 3 and groups[0] and len(groups[0]) == 4:
            y, mo, d = groups[0], groups[1].zfill(2), groups[2].zfill(2)
            return f"{y}-{mo}-{d}"
        # "29 may 2026" or "may 29, 2026"
        if len(groups) >= 3:
            if groups[0].isdigit():
                day, month_name, year = groups[0], groups[1].lower(), groups[2]
            else:
                month_name, day, year = groups[0].lower(), groups[1], groups[2]
            month_num = MONTH_LOOKUP.get(month_name, 1)
            return f"{year}-{month_num:02d}-{int(day):02d}"
        # "available from may 29" or "releases on 29 may"
        if len(groups) == 2:
            a, b = groups[0], groups[1]
            if a.isdigit():
                day, month_name = a, b.lower()
            else:
                day, month_name = b, a.lower()
            month_num = MONTH_LOOKUP.get(month_name, 1)
            # Use current year as default
            year = str(datetime.now().year)
            return f"{year}-{month_num:02d}-{int(day):02d}"
    except:
        pass
    return None


def extract_ott_info_llm(film: str, videos: list[dict], llm_key: str) -> tuple[str | None, str | None]:
    """Use LLM to extract OTT release date and platform from OTT video titles+descriptions."""
    if not llm_key or not videos:
        return None, None

    context = []
    for v in videos[:6]:
        desc_preview = v.get("description", "")[:500]
        context.append(f"Title: {v['title']}\nPublished: {v.get('published', '')[:10]}\nDescription: {desc_preview}")

    prompt = f"""You are analyzing YouTube videos about the Tamil film "{film}" to determine its OTT (streaming) release details.

These videos are known to be about OTT/streaming release of this film.
Based on their titles, descriptions, and publish dates, determine:

1. Which OTT platform is it streaming on? (Netflix, Amazon Prime Video, Disney+ Hotstar, Aha Tamil, ZEE5, Sony LIV, or None)
2. What is the OTT release date? (YYYY-MM-DD format, or None if unclear)

Rules:
- The OTT release date is usually when the film became available on the platform
- Video publication dates are often close to or after the OTT release
- If a video title/description says "now streaming" or "available now", the release date is around the video's publish date
- Look for phrases like "releases on", "available from", "streaming from", "out on" in descriptions
- The platform is often mentioned in the title or description

Output ONLY valid JSON:
{{"platform": "<platform or null>", "release_date": "<YYYY-MM-DD or null>", "confidence": "high/medium/low", "reasoning": "<brief explanation>"}}

Videos:
{chr(10).join(context)}"""

    try:
        resp = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {llm_key}", "Content-Type": "application/json"},
            json={"model": "openai/gpt-chat-latest", "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.1, "max_tokens": 500, "response_format": {"type": "json_object"}},
            timeout=30,
        )
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
            result = json.loads(content)
            platform = result.get("platform") or None
            date = result.get("release_date") or None
            # Validate date format
            if date:
                try:
                    datetime.strptime(date, "%Y-%m-%d")
                except:
                    date = None
            return date, platform
    except:
        pass
    return None, None


def detect_ott_release_date(film: str, api_key: str, videos: list[dict] | None = None, llm_key: str | None = None) -> tuple[str | None, str | None]:
    """Robustly detect OTT release date and platform by searching YouTube + analyzing video details.
    Returns (release_date, platform) tuple.

    Strategy hierarchy:
    1. Check video titles from search (fast regex)
    2. Fetch full descriptions via videos API (more text to match)
    3. If llm_key provided, use LLM to intelligently extract info
    """
    # Phase 1: Use provided OTT videos if available (no extra API call)
    existing_videos = videos or []
    for v in existing_videos:
        title_desc = (v.get("title", "") + " " + (v.get("description", "")[:500])).lower()
        platform = None
        for kw, plat in PLATFORM_KEYWORDS.items():
            if kw in title_desc:
                platform = plat
                break
        if platform:
            # Check for availability signals
            if any(w in title_desc for w in ["available now", "streaming now", "now streaming", "out now", "released today"]):
                return datetime.now().strftime("%Y-%m-%d"), platform

    # Phase 2: Search YouTube with OTT queries
    queries = [
        f"{film} OTT release date tamil",
        f"{film} ott release date",
        f"{film} netflix release",
        f"{film} streaming now",
        f"{film} now streaming",
        f"{film} digital release",
        f"{film} OTT",
    ]
    all_video_ids = set()
    title_hits = []  # (title, desc, platform, published)
    for q in queries:
        try:
            params = {"part": "snippet", "q": q, "type": "video", "maxResults": 8,
                      "key": api_key, "order": "relevance"}
            resp = httpx.get(f"{YOUTUBE_API_BASE}/search", params=params, timeout=10)
            if resp.status_code != 200:
                continue
            for item in resp.json().get("items", []):
                vid = item["id"]["videoId"]
                title = item["snippet"]["title"].lower()
                desc = item["snippet"]["description"][:500].lower()
                text = title + " " + desc

                # Detect platform from title/desc snippet
                platform = None
                for kw, plat in PLATFORM_KEYWORDS.items():
                    if kw in text:
                        platform = plat
                        break

                # Check for immediate availability signals (strong signal → return immediately)
                if any(w in text for w in ["available now", "streaming now", "now streaming", "out now", "released today"]):
                    return datetime.now().strftime("%Y-%m-%d"), platform

                all_video_ids.add(vid)
                title_hits.append((title, desc, platform, item["snippet"]["publishedAt"]))
        except:
            continue

    # Phase 3: Fetch full descriptions of found videos
    full_videos = fetch_video_details(list(all_video_ids)[:20], api_key) if all_video_ids else []
    all_found = []
    for fv in full_videos:
        title = fv["title"].lower()
        desc = fv.get("description", "").lower()
        text = title + " " + desc

        platform = None
        for kw, plat in PLATFORM_KEYWORDS.items():
            if kw in text:
                platform = plat
                break

        # Check availability signals again with full description
        if any(w in text for w in ["available now", "streaming now", "now streaming", "out now", "released today"]):
            return datetime.now().strftime("%Y-%m-%d"), platform

        all_found.append({
            "title": fv["title"],
            "description": fv.get("description", ""),
            "published": fv.get("published", ""),
            "platform": platform,
        })

    # Phase 4: LLM analysis (most reliable — understands context, ignores false positives)
    if llm_key and (all_found or title_hits):
        llm_videos = all_found if all_found else [
            {"title": t, "description": d, "published": p, "platform": plat}
            for t, d, plat, p in title_hits[:8]
        ]
        llm_date, llm_platform = extract_ott_info_llm(film, llm_videos, llm_key)
        if llm_date or llm_platform:
            return llm_date, llm_platform

    # Phase 5: Regex date extraction from full descriptions (less reliable, used as last resort)
    for fv in full_videos:
        text = (fv["title"] + " " + fv.get("description", "")).lower()
        platform = None
        for kw, plat in PLATFORM_KEYWORDS.items():
            if kw in text:
                platform = plat
                break
        for pat in DATE_PATTERNS:
            m = re.search(pat, text)
            if m:
                date_str = parse_date_match(m)
                if date_str:
                    return date_str, platform

    # Phase 6: Use earliest OTT video publish date as proxy
    # If videos clearly mention a platform in their titles but have no explicit date,
    # use the publish date of the earliest relevant video as an estimate
    if all_found:
        platform_videos = [v for v in all_found if v.get("platform")]
        if platform_videos:
            dates = [v["published"][:10] for v in platform_videos if v.get("published")]
            if dates:
                return min(dates), platform_videos[0]["platform"]

    return None, None


def run_llm_analysis(film: str, comments: list[str], api_key: str, ott_mode: bool = False) -> dict:
    """Send comments to LLM and get full 3-tier analysis. ott_mode=True uses OTT-specific prompt."""
    if not comments or not api_key:
        return {}
    sample = comments[:70]
    if len(comments) > 70:
        sample += random.sample(comments[70:], min(40, len(comments) - 70))
    random.shuffle(sample)

    template = OTT_ANALYSIS_PROMPT_TEMPLATE if ott_mode else ANALYSIS_PROMPT_TEMPLATE
    prompt = template.format(
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
            result["_comments_analyzed"] = len(sample)
            return result
    except Exception:
        pass
    return {}

# ─── Full Pipeline for a Single Film ──────────────────────────────────────

def process_film(film: str, force: bool = False) -> dict:
    """Full pipeline: search → detect new → fetch comments → LLM → save."""
    api_key = get_youtube_key()
    llm_key = get_openrouter_key()
    registry = load_films()
    entry = registry.get(film, {"last_video_ids": [], "last_checked": None, "added": datetime.now(timezone.utc).isoformat()})

    # Step 1: Search for review videos
    videos = search_review_videos(film, api_key)
    if not videos:
        return {"error": f"No review videos found for {film}"}

    # Step 2: Check if anything new (unless forced)
    if not force and not has_new_videos(film, videos, registry):
        # Load cached data if exists
        cache = OUTPUT_DIR / f"{film.lower().replace(' ','-')}-data.json"
        if cache.exists():
            with open(cache) as f:
                cached = json.load(f)
            cached["_status"] = "cached"
            return cached
        return {"error": "No new videos since last check", "_status": "nochange"}

    # Step 3: Fetch comments from all videos, separated by OTT vs theatrical
    all_comments = []
    ott_comments = []
    theatrical_comments = []
    for v in videos:
        try:
            comments = fetch_comments_api(api_key, v["id"], 100)
            v["comment_count"] = len(comments)
            all_comments.extend(comments)
            if v.get("is_ott"):
                ott_comments.extend(comments)
            else:
                theatrical_comments.extend(comments)
        except Exception:
            v["comment_count"] = 0

    # Step 4: Run analysis — theatrical first, then OTT if we have enough
    analysis = run_llm_analysis(film, all_comments, llm_key)
    ott_analysis = {}
    if len(ott_comments) >= 10:
        ott_analysis = run_llm_analysis(film, ott_comments, llm_key, ott_mode=True)

    # Step 5: Detect OTT release date and platform
    meta = FILMS_META.get(film, {})
    ott_release_date = meta.get("ott_release_date", None)
    ott_platform = meta.get("ott_platform", None)
    
    # Source 1: Wikipedia Home media section (most reliable)
    if not ott_release_date and len(ott_comments) >= 5:
        wiki_date, wiki_platform = scrape_wiki_ott_info(film)
        if wiki_date or wiki_platform:
            ott_release_date = wiki_date
            ott_platform = wiki_platform
            if film in FILMS_META:
                if wiki_date: FILMS_META[film]["ott_release_date"] = wiki_date
                if wiki_platform: FILMS_META[film]["ott_platform"] = wiki_platform
    
    # Source 2: YouTube keyword detection (fallback) — improved: passes OTT videos + LLM key for smart extraction
    if not ott_release_date and len(ott_comments) >= 5:
        detected_date, detected_platform = detect_ott_release_date(
            film, api_key,
            videos=[v for v in videos if v.get("is_ott")],
            llm_key=llm_key,
        )
        if detected_date:
            ott_release_date = detected_date
            ott_platform = ott_platform or detected_platform
            # Save back to FILMS_META (in-memory only for now)
            if film in FILMS_META:
                FILMS_META[film]["ott_release_date"] = detected_date
                if detected_platform:
                    FILMS_META[film]["ott_platform"] = detected_platform

    # Step 6: Build data payload
    # Spread popularity: LLM base (0-50) + log volume (0-30) + sentiment boost (0-20)
    llm_base = analysis.get("popularity_score", 50) * 0.5  # scale LLM to 0-50
    import math
    vol_bonus = min(30, round(math.log10(max(1, len(all_comments))) * 12))
    sb = analysis.get("sentiment_breakdown", {})
    pos_pct = sb.get("positive_percent", 50)
    sent_bonus = min(20, round(pos_pct / 5))
    popularity_score = min(100, round(llm_base + vol_bonus + sent_bonus))

    # OTT popularity
    ott_score = 0
    if ott_analysis:
        ott_llm_base = ott_analysis.get("popularity_score", 50) * 0.5
        ott_vol = min(30, round(math.log10(max(1, len(ott_comments))) * 12))
        ott_sb = ott_analysis.get("sentiment_breakdown", {})
        ott_pos = ott_sb.get("positive_percent", 50)
        ott_sent = min(20, round(ott_pos / 5))
        ott_score = min(100, round(ott_llm_base + ott_vol + ott_sent))

    data = {
        "film": film,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "total_comments": len(all_comments),
        "popularity_score": popularity_score,
        "ott_status": {
            "ott_comments": len(ott_comments),
            "ott_release_date": ott_release_date,
            "ott_platform": ott_platform,
            "ott_score": ott_score,
            "has_ott_analysis": bool(ott_analysis),
        },
        "videos": [{"id": v["id"], "title": v["title"], "channel": v["channel"],
                     "url": f"https://youtube.com/watch?v={v['id']}",
                     "comment_count": v.get("comment_count", 0),
                     "is_ott": v.get("is_ott", False)} for v in videos],
        "analysis": analysis,
        "ott_analysis": ott_analysis,
        "_status": "fresh",
    }

    # Muted override: if very few comments, mark mood as Muted
    if len(all_comments) < 50 and "tab_zero_spoiler" in analysis:
        analysis["tab_zero_spoiler"]["audience_mood"] = "Muted"

    # Step 7: Save
    OUTPUT_DIR.mkdir(exist_ok=True)
    slug = film.lower().replace(" ", "-")
    with open(OUTPUT_DIR / f"{slug}-raw.json", "w") as f:
        json.dump({"film": film, "videos": videos, "comments": all_comments,
                   "ott_comments": ott_comments, "theatrical_comments": theatrical_comments},
                  f, ensure_ascii=False, indent=2)
    with open(OUTPUT_DIR / f"{slug}-data.json", "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # Step 8: Update registry
    registry[film] = {
        "last_video_ids": [v["id"] for v in videos],
        "last_checked": datetime.now(timezone.utc).isoformat(),
        "added": entry.get("added", datetime.now(timezone.utc).isoformat()),
        "popularity_score": popularity_score,
        "total_comments": len(all_comments),
        "ott_comments": len(ott_comments),
        "ott_score": ott_score,
        "ott_release_date": ott_release_date,
        "ott_platform": ott_platform,
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
        # Ensure ott_status exists from registry fallback
        if "ott_status" not in data or not data["ott_status"]:
            data["ott_status"] = {
                "ott_comments": entry.get("ott_comments", 0),
                "ott_release_date": entry.get("ott_release_date"),
                "ott_platform": entry.get("ott_platform"),
                "ott_score": entry.get("ott_score", 0),
                "has_ott_analysis": bool(entry.get("ott_score", 0)),
            }
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
    """2-month window. Films older than 2mo mostly excluded.
    0-1 month: full weight → 1.0
    1-2 months: mild decay → 0.65
    2+ months: excluded (too old for leaderboard)
    """
    if not release_date:
        return 0.0
    try:
        from datetime import datetime, timedelta
        release = datetime.strptime(release_date, "%Y-%m-%d")
        now = datetime.now()
        days = (now - release).days
        if days <= 30: return 1.0
        if days <= 60: return 0.65
        return 0.0
    except:
        return 0.0


def get_leaderboard() -> list[dict]:
    """Build leaderboard sorted by popularity with recency boost.
    Excludes films with confirmed OTT releases (they belong on OTT board)."""
    registry = load_films()
    board = []
    for film, entry in registry.items():
        data = load_film_data(film)
        score = data.get("popularity_score", entry.get("popularity_score", 0))
        analysis = data.get("analysis", {})
        sb = analysis.get("sentiment_breakdown", {})
        meta = FILMS_META.get(film, {})
        release = meta.get("release_date", "")

        # Skip films with confirmed OTT release (they belong on OTT board)
        # Check both metadata and analysis data
        if meta.get("ott_release_date"):
            continue
        ott_status = data.get("ott_status", {})
        if ott_status.get("has_ott_analysis") and ott_status.get("ott_release_date"):
            continue

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
            "buzz": analysis.get("tab_zero_spoiler", {}).get("audience_mood", ""),
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
                releases.append({
                    "film": film,
                    "release_date": release,
                    "star": meta.get("star", ""),
                    "popularity_score": score,
                    "total_comments": total,
                    "rating": analysis.get("rating"),
                    "buzz": analysis.get("tab_zero_spoiler", {}).get("audience_mood", ""),
                })
        except:
            pass
    releases.sort(key=lambda x: x.get("release_date", "2000-01-01"), reverse=True)
    return releases[:5]


def get_ott_watch() -> list[dict]:
    """Return films with confirmed OTT release date, retained for 30 days, top 10.
    Includes films from FILMS_META even without OTT analysis (uses metadata fallback)."""
    registry = load_films()
    ott_list = []
    now = datetime.now()
    for film, entry in registry.items():
        data = load_film_data(film)
        ott_status = data.get("ott_status", {})
        ott_analysis = data.get("ott_analysis", {})
        meta = FILMS_META.get(film, {})

        # Get confirmed OTT release date from metadata or stored data
        ott_release_date = meta.get("ott_release_date") or ott_status.get("ott_release_date") or entry.get("ott_release_date")
        if not ott_release_date:
            continue

        # Retain for 30 days from OTT release
        try:
            rd = datetime.strptime(ott_release_date, "%Y-%m-%d")
            days_old = (now - rd).days
        except:
            continue
        if days_old > 30:
            continue

        # Determine if we have analysis data or need metadata fallback
        has_analysis = ott_status.get("has_ott_analysis") and bool(ott_analysis)

        ott_list.append({
            "film": film,
            "ott_release_date": ott_release_date,
            "ott_platform": meta.get("ott_platform") or ott_status.get("ott_platform"),
            "ott_score": ott_status.get("ott_score", 0) if has_analysis else 0,
            "ott_comments": ott_status.get("ott_comments", 0),
            "rating": ott_analysis.get("rating") if has_analysis else None,
            "positive_pct": ott_analysis.get("sentiment_breakdown", {}).get("positive_percent") if has_analysis else None,
            "ott_mood": ott_analysis.get("ott_vibe", {}).get("ott_mood", "") if has_analysis else "",
            "how_it_differs": ott_analysis.get("ott_vibe", {}).get("how_it_differs", "") if has_analysis else "",
            "theatrical_vs_ott": ott_analysis.get("ott_vibe", {}).get("theatrical_vs_ott_verdict", "") if has_analysis else "",
            "theatrical_rating": data.get("analysis", {}).get("rating"),
            "genre": ott_analysis.get("genre", [])[:2] if has_analysis else [],
            "what_people_loved": ott_analysis.get("what_people_loved", [])[:3] if has_analysis else [],
            "what_people_criticized": ott_analysis.get("what_people_criticized", [])[:2] if has_analysis else [],
            "fetched_at": data.get("fetched_at") if has_analysis else None,
        })
    # Sort: films with analysis first (by score), then metadata-only
    ott_list.sort(key=lambda x: (x.get("ott_score", 0) or 0, bool(x.get("rating"))), reverse=True)
    return ott_list[:10]


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
    films = scrape_wiki_releases()
    return JSONResponse(content=films, headers={"Cache-Control": "public, max-age=3600"})

@app.get("/api/tunnel-url")
async def api_tunnel_url():
    """Return the current Cloudflare tunnel URL for the frontend to use."""
    import re
    log_file = Path("/var/log/cloudflared-tunnel.log")
    if log_file.exists():
        content = log_file.read_text()
        urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", content)
        if urls:
            return JSONResponse(content={"url": urls[-1]})  # latest tunnel URL, not first
    return JSONResponse(content={"url": None})

# ─── API Routes ───────────────────────────────────────────────────────────

@app.get("/")
async def api_root():
    """Serve the dashboard frontend."""
    frontend_path = REPO_DIR / "frontend.html"
    if frontend_path.exists():
        with open(frontend_path) as f:
            html = f.read()
        html = html.replace("__VITE_API_KEY__", API_KEY)
        return HTMLResponse(content=html)
    return JSONResponse(content={"error": "Frontend not found"})

@app.get("/api/leaderboard")
async def api_leaderboard():
    data = get_leaderboard()
    return JSONResponse(content=data, headers={"Cache-Control": "public, max-age=18000, s-maxage=18000"})

@app.get("/api/new-releases")
async def api_new_releases():
    data = get_new_releases()
    return JSONResponse(content=data, headers={"Cache-Control": "public, max-age=18000, s-maxage=18000"})

@app.get("/api/ott-watch")
async def api_ott_watch():
    data = get_ott_watch()
    return JSONResponse(content=data, headers={"Cache-Control": "public, max-age=18000, s-maxage=18000"})

@app.get("/api/film/{film}")
async def api_film(film: str):
    data = load_film_data(film)
    if "error" in data and data.get("_status") == "nochange":
        data = load_film_data(film)
    return JSONResponse(content=data, headers={"Cache-Control": "public, max-age=18000, s-maxage=18000"})

@app.post("/api/film/{film}/refresh")
async def api_refresh_film(film: str):
    data = process_film(film, force=True)
    return JSONResponse(content={"status": "ok" if "_status" in data else "error", "film": film})

@app.post("/api/refresh")
async def api_refresh_all():
    results = smart_refresh()
    updated = [f for f, s in results.items() if s == "fresh"]
    return JSONResponse(content={"status": "ok", "updated": updated})

@app.get("/api/films")
async def api_films_list():
    registry = load_films()
    return JSONResponse(content=list(registry.keys()))

@app.post("/api/films/add/{film}")
async def api_add_film(film: str):
    registry = load_films()
    if film not in registry:
        registry[film] = {"last_video_ids": [], "last_checked": None, "added": datetime.now(timezone.utc).isoformat()}
        save_films(registry)
    data = process_film(film, force=True)
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