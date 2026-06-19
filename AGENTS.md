     1|# Tamil Movie Review Dashboard — AGENTS.md
     2|
     3|> **Status: SUNSET (June 2026)** — No longer actively maintained. Cron jobs removed. Codebase preserved as reference.
     4|
     5|## Architecture Overview
     6|
     7|### Backend (VPS)
     8|- **FastAPI** server (`server.py`) on port 8080
     9|- **YouTube Data API v3** for comment/video scraping (free tier: 10K units/day)
    10|- **OpenRouter LLM** (`openai/gpt-chat-latest`) for sentiment analysis with JSON mode
    11|- Output stored in `output/` directory as JSON files
    12|- **Incremental processing**: only new comments are analyzed (tracks `processed_comment_ids`)
    13|
    14|### Frontend
    15|- Single-page HTML (`frontend.html`) served by backend at `/`
    16|- Also deployed to Vercel for public access
    17|- Hash-based routing: `#/` (leaderboard), `#/film/{name}` (theatrical detail), `#/trailer/{name}` (trailer detail)
    18|- Light/dark mode toggle (default: light), persists in localStorage
    19|- No build step — single HTML file with all CSS/JS inlined
    20|
    21|### Deployment
    22|- **VPS**: `http://<VPS_IP>:8080` (IP configured via `VPS_IP` env var)
    23|- **Cloudflare Named Tunnel**: `https://<your-domain>` → `localhost:8080` (permanent URL)
    24|- **Vercel**: `<your-vercel-url>` (env vars: `VITE_API_KEY`, `VITE_VPS_IP`)
    25|- Frontend placeholders: `__VITE_API_KEY__` and `__VITE_VPS_IP__` replaced at build time
    26|- **GitHub**: `<your-org>/movies-vibe-dashboard` (auto-deploys to Vercel on push)
    27|
    28|## Design Decisions
    29|
    30|### OTT Section Removed (June 2026)
    31|OTT tracking was removed because:
    32|- No reliable event source for OTT release dates (YouTube OTT videos are scattered)
    33|- False positive detection from video titles mentioning platform names
    34|- Low signal-to-noise ratio in OTT comments vs theatrical reviews
    35|- Films drop from leaderboard after 30-day theatrical window (assumed OTT)
    36|
    37|### Video Source Contamination Fix
    38|Short/numeric film names (e.g. "29") caused YouTube search to return unrelated videos.
    39|Fix: `_is_relevant_video()` validates film name appears in title with context keywords
    40|(movie/film/review/trailer/cast names) for titles ≤3 characters.
    41|
    42|### Dubbed Film Filtering
    43|Tamil dubbed versions of other language films are excluded from the trailer board.
    44|Detection: titles containing "tamil dubbed", "dubbed", "telugu to tamil", etc.
    45|
    46|### Wikipedia Verification
    47|Trailer board films are verified against Wikipedia **before** fetching comments/LLM:
    48|- Release date check: skip if already released (saves API quota)
    49|- Cast extraction: shown on detail pages
    50|- Director extraction: `check_wiki_director()` parses "Directed by" from infobox
    51|- Film name lookup: tries "(2026 film)", "(2025 film)", "(film)" variants
    52|- Results cached in `output/wiki-cache.json`
    53|
    54|### Popularity Score Formula
    55|- LLM base (0-50) + log-scaled volume bonus (0-30) + sentiment bonus (0-20) = 0-100
    56|- Hotness score = popularity × recency multiplier:
    57|  - 0-1 month: 1.0x (full weight)
    58|  - 1-2 months: 0.65x (mild decay)
    59|  - 2+ months: excluded from leaderboard
    60|
    61|### Hype Score (Trailer-Based)
    62|For upcoming/new films, computes hype from trailer data:
    63|- View count (log scale, **0-45**) — views matter most
    64|- **View threshold bonus**: 500K+ views → +5, 1M+ views → +10
    65|- Like ratio percentage (0-15)
    66|- LLM sentiment analysis of trailer comments (0-25)
    67|- Comment volume (log scale, 0-15)
    68|- LLM returns `category` (Celebratory/Excited/Cautiously Optimistic/Mixed/Polarizing/etc.)
    69|- LLM returns `genre_phrase` (short 5-8 word genre description, avoids generic enthusiasm)
    70|- Stored in film data under `"hype"` key
    71|
    72|### Trailer Category Labels
    73|Instead of raw sentiment (positive/negative), the LLM classifies into richer categories:
    74|Celebratory, Excited, Cautiously Optimistic, Mixed, Polarizing, Disappointed, Surprised, Divisive, Anticipated, Muted.
    75|These are shown on trailer board rows with emoji badges.
    76|Genre phrase replaces generic "Audience is excited" with specific genre descriptions.
    77|
    78|### Incremental Comment Processing
    79|The pipeline tracks `processed_comment_ids` per film. On refresh:
    80|1. Fetch comments sorted by time (newest first)
    81|2. Filter out already-seen comment IDs → get delta
    82|3. If delta ≥15 new comments: run LLM merge analysis
    83|4. If delta <15: skip (not worth LLM cost)
    84|5. Merge prompt updates existing analysis with new sentiment data
    85|
    86|This generalizes well around release dates — lots of new comments → frequent updates.
    87|
    88|### Leaderboard Rules
    89|- **Theatrical**: films within 30-day release window, minimum 5 comments
    90|- **Trailer**: upcoming/unreleased films ranked by trailer hype (6-hour cache)
    91|- Films transition from trailer board → theatrical board → drop off after 30 days
    92|- No OTT exclusion — all qualifying films appear on leaderboard
    93|
    94|### Light/Dark Mode
    95|- Default: light theme (`:root` CSS variables)
    96|- Dark mode: `[data-theme="dark"]` override
    97|- Toggle button (🌙/☀️) in fixed position, saves to localStorage
    98|- Respects OS `prefers-color-scheme` on first visit
    99|
   100|## Cron Jobs
   101|
   102|### Trailer Board Refresh
   103|- **Schedule**: Daily at 8 AM UTC
   104|- **Endpoint**: `POST /api/refresh-trailer`
   105|- **Cost**: ~37 units/day
   106|
   107|### Theatrical Board Refresh (Daily)
   108|- **Schedule**: Daily at 6 AM UTC
   109|- **Logic**: Checks if any film is within 3 days of release → full refresh. Otherwise lightweight check.
   110|- **Endpoint**: `POST /api/refresh` (only if near release)
   111|- **Cost**: ~0-22 units/day (skipped when no film near release)
   112|
   113|### Release Date Proximity Check (6h)
   114|- **Schedule**: Every 6h (00:00, 06:00, 12:00, 18:00 UTC)
   115|- **Logic**: Only triggers full pipeline refresh if any film is within ±3 days of its release date
   116|- **Cost**: ~22 units per trigger (only when a film is releasing)
   117|- **Saves quota**: Most runs return "no near release" with zero API calls
   118|
   119|### Total Daily Quota: ~60-125 units (~0.6-1.25% of 10K budget)
   120|
   121|## Known Issues
   122|
   123|### YouTube API Quota
   124|10K units/day free tier. search.list = 100 units (expensive!), commentThreads.list = 1 unit/page, videos.list = 1 unit.
   125|Search results cached for 24h to avoid redundant 100-unit search calls.
   126|Smart refresh skips films with no new videos. Trailer hype adds ~2-4 extra search calls per film.
   127|
   128|## File Structure
   129|
   130|- `server.py` — Backend: FastAPI app, pipeline, LLM analysis, YouTube scraping
   131|- `frontend.html` — Frontend: single-file SPA with 3-tier spoiler system
   132|- `review_pipeline.py` — Standalone CLI pipeline (legacy, not used by server)
   133|- `output/films.json` — Film registry (tracked films + metadata)
   134|- `output/{film}-data.json` — Processed analysis + hype data per film
   135|- `output/{film}-raw.json` — Raw scraped comments per film
   136|- `output/trailer-leaderboard-cache.json` — Cached trailer board (6h TTL)
   137|- `output/wiki-cache.json` — Wikipedia lookup cache
   138|- `.env` — Secrets (YOUTUBE_API_KEY, OPENROUTER_API_KEY, API_KEY, VPS_IP)
   139|- `.env.example` — Documented env var template
   140|- `config.yaml` — Pipeline configuration
   141|- `requirements.txt` — Python dependencies
   142|- `vercel.json` — Vercel build config (env var injection)
   143|- `AGENTS.md` — This file
   144|
   145|## API Endpoints
   146|
   147|| Endpoint | Method | Description |
   148||----------|--------|-------------|
   149|| `/api/leaderboard` | GET | Films ranked by hotness score |
   150|| `/api/new-releases` | GET | Fresh films <48h with <50 comments (includes hype) |
   151|| `/api/trailer-leaderboard` | GET | Upcoming films by trailer hype (6h cache) |
   152|| `/api/film/{name}` | GET | Full 3-tier analysis + hype data |
   153|| `/api/trailer/{name}` | GET | Trailer film detail (from cache) |
   154|| `/api/film/{name}/refresh` | POST | Force re-scrape a film |
   155|| `/api/refresh` | POST | Smart refresh all films (incremental) |
   156|| `/api/refresh-trailer` | POST | Force rebuild trailer leaderboard cache |
   157|| `/api/films` | GET | List tracked films |
   158|| `/api/films/add/{name}` | POST | Add and process new film |
   159|| `/api/tunnel-url` | GET | Current Cloudflare tunnel URL |
   160|| `/api/scrape-releases` | GET | Wikipedia scraper for recent releases |
   161|| `/` | GET | Dashboard frontend (serves frontend.html) |
   162|
   163|## YouTube API Quota Budget
   164|
   165|| Operation | Calls | Units |
   166||-----------|-------|-------|
   167|| Theatrical refresh (8 films) | ~8 search(100u each) + ~16 comment + ~1 stats | ~817 (first run), ~17 (cached) |
   168|| Trailer leaderboard refresh | ~10 search(100u each) + ~30 comment + ~1 stats | ~1031 (first run), ~31 (cached) |
   169|| **Daily total (with 24h search cache)** | | **~50-100** (mostly comments, searches cached) |
   170|