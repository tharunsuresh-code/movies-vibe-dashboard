     1|# 🎬 Kollywood Pulse — Tamil Movie Review Dashboard
     2|
     3|> **Status: Sunset (June 2026)** — This project is no longer actively maintained. The codebase and data remain as a reference.
     4|
     5|A social media review dashboard that tracks Tamil movie audience sentiment by analysing YouTube comments. Films are ranked by discussion volume and sentiment, with a 3-tier spoiler system (zero / mild / full) for detail pages.
     6|
     7|## What It Does
     8|
     9|- **Theatrical Leaderboard** — Films within 30-day release window, ranked by rating derived from YouTube comment sentiment
    10|- **Trailer Board** — Upcoming/unreleased films ranked by trailer hype (views, likes, LLM sentiment analysis)
    11|- **Detail Pages** — 3-tier spoiler tabs (zero/mild/full) with audience vibe, sentiment breakdown, key themes, loved/criticized points, and comparisons
    12|- **Incremental Processing** — Only new comments are analysed on each refresh (tracks `processed_comment_ids` per film)
    13|
    14|## Architecture
    15|
    16|```
    17|┌─────────────┐     ┌──────────────────┐     ┌──────────────┐
    18|│   YouTube    │────▶│   FastAPI Server  │────▶│   Vercel     │
    19|│   Data API   │     │   (VPS:8080)     │     │   Frontend   │
    20|└─────────────┘     └──────────────────┘     └──────────────┘
    21|                           │
    22|                    ┌──────┴──────┐
    23|                    │  OpenRouter  │
    24|                    │  (LLM)      │
    25|                    └─────────────┘
    26|```
    27|
    28|- **Backend**: FastAPI server on VPS (port 8080), exposed via Cloudflare Named Tunnel at `https://<your-domain>`
    29|- **Frontend**: Single-file HTML SPA (`frontend.html`) deployed to Vercel at `<your-vercel-url>`
    30|- **LLM**: OpenRouter `openai/gpt-chat-latest` for sentiment analysis with JSON mode output
    31|- **YouTube API**: Data API v3 (free tier: 10K units/day, ~60-125 units used daily)
    32|
    33|## Tech Stack
    34|
    35|| Component | Technology |
    36||-----------|-----------|
    37|| Backend | Python, FastAPI, httpx |
    38|| Frontend | Vanilla HTML/CSS/JS (single file, no build step) |
    39|| LLM | OpenRouter → OpenAI GPT (JSON mode) |
    40|| Data | YouTube Data API v3 |
    41|| Hosting | VPS (backend) + Vercel (frontend) |
    42|| Tunnel | Cloudflare Named Tunnel |
    43|| Routing | Hash-based (`#/`, `#/film/{name}`, `#/trailer/{name}`) |
    44|
    45|## Key Files
    46|
    47|| File | Purpose |
    48||------|---------|
    49|| `server.py` | Backend: FastAPI app, YouTube scraping, LLM analysis pipeline |
    50|| `frontend.html` | Frontend: single-file SPA with light/dark mode |
    51|| `output/films.json` | Film registry (tracked films + metadata) |
    52|| `output/{film}-data.json` | Processed analysis + hype data per film |
    53|| `output/{film}-raw.json` | Raw scraped YouTube comments |
    54|| `output/trailer-leaderboard-cache.json` | Cached trailer board (6h TTL) |
    55|| `output/wiki-cache.json` | Wikipedia lookup cache (release dates, cast) |
    56|
    57|## API Endpoints
    58|
    59|| Endpoint | Method | Description |
    60||----------|--------|-------------|
    61|| `/api/leaderboard` | GET | Films ranked by rating |
    62|| `/api/new-releases` | GET | Fresh films <48h with <50 comments |
    63|| `/api/trailer-leaderboard` | GET | Upcoming films by rating (6h cache) |
    64|| `/api/film/{name}` | GET | Full 3-tier analysis + hype data |
    65|| `/api/trailer/{name}` | GET | Trailer film detail (from cache) |
    66|| `/api/film/{name}/refresh` | POST | Force re-scrape a film |
    67|| `/api/refresh` | POST | Smart refresh all films (incremental) |
    68|| `/api/refresh-trailer` | POST | Force rebuild trailer leaderboard cache |
    69|| `/api/films` | GET | List tracked films |
    70|| `/api/films/add/{name}` | POST | Add and process new film |
    71|| `/api/tunnel-url` | GET | Current Cloudflare tunnel URL |
    72|| `/api/scrape-releases` | GET | Wikipedia scraper for recent releases |
    73|| `/` | GET | Dashboard frontend |
    74|
    75|## Daily Costs
    76|
    77|| Service | Cost |
    78||---------|------|
    79|| YouTube Data API | Free (10K units/day, using ~1%) |
    80|| OpenRouter LLM | ~$1/day (~20 LLM calls) |
    81|| Vercel | Free (Hobby tier) |
    82|| Cloudflare Tunnel | Free tier |
    83|| VPS | Fixed infrastructure cost |
    84|| **Total variable** | **~$1/day (~$30/month)** |
    85|
    86|## Design Decisions
    87|
    88|- **3-tier spoiler system** — Safe to share zero-spoiler tab with anyone; full spoiler tab for those who've seen the film
    89|- **Incremental processing** — Only analyses new comments (≥15 threshold) to save LLM costs
    90|- **Wikipedia verification** — Trailer board films checked against Wikipedia before processing (release date, cast, director)
    91|- **Rating-based ranking** — Uses LLM-derived sentiment rating (0-10) instead of raw popularity scores
    92|- **Non-Tamil film filtering** — Title keyword detection + comment script analysis (Telugu/Hindi unicode ranges) to exclude dubbed/non-Tamil films
    93|
    94|## Tracked Films (at sunset)
    95|
    96|Karuppu, Parimala and Co, Blast, 29, Love Insurance Kompany, Mr. X, Battle, Kara
    97|
    98|## Environment Variables
    99|
   100|```
   101|YOUTUBE_API_KEY=       # YouTube Data API v3 key
   102|OPENROUTER_API_KEY=    # OpenRouter API key for LLM
   103|API_KEY=               # Frontend auth (X-API-Key header)
   104|VPS_IP=                # VPS IP for frontend API calls
   105|```
   106|
   107|See `.env.example` for documented template.
   108|
   109|## Running Locally
   110|
   111|```bash
   112|pip install -r requirements.txt
   113|cp .env.example .env   # Fill in API keys
   114|python server.py       # Starts on port 8080
   115|```
   116|
   117|---
   118|
   119|*Built with YouTube Data API + OpenRouter. Analyzed ~400 comments per film across 8 tracked films.*
   120|