# 🎬 Kollywood Pulse — Tamil Movie Review Dashboard

> **Status: Sunset (June 2026)** — This project is no longer actively maintained. The codebase and data remain as a reference.

A social media review dashboard that tracks Tamil movie audience sentiment by analysing YouTube comments. Films are ranked by discussion volume and sentiment, with a 3-tier spoiler system (zero / mild / full) for detail pages.

## What It Does

- **Theatrical Leaderboard** — Films within 30-day release window, ranked by rating derived from YouTube comment sentiment
- **Trailer Board** — Upcoming/unreleased films ranked by trailer hype (views, likes, LLM sentiment analysis)
- **Detail Pages** — 3-tier spoiler tabs (zero/mild/full) with audience vibe, sentiment breakdown, key themes, loved/criticized points, and comparisons
- **Incremental Processing** — Only new comments are analysed on each refresh (tracks `processed_comment_ids` per film)

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌──────────────┐
│   YouTube    │────▶│   FastAPI Server  │────▶│   Vercel     │
│   Data API   │     │   (VPS:8080)     │     │   Frontend   │
└─────────────┘     └──────────────────┘     └──────────────┘
                           │
                    ┌──────┴──────┐
                    │  OpenRouter  │
                    │  (LLM)      │
                    └─────────────┘
```

- **Backend**: FastAPI server on VPS (port 8080), exposed via Cloudflare Named Tunnel at `https://movies.onekural.com`
- **Frontend**: Single-file HTML SPA (`frontend.html`) deployed to Vercel at `movies-vibe-dashboard.vercel.app`
- **LLM**: OpenRouter `openai/gpt-chat-latest` for sentiment analysis with JSON mode output
- **YouTube API**: Data API v3 (free tier: 10K units/day, ~60-125 units used daily)

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | Python, FastAPI, httpx |
| Frontend | Vanilla HTML/CSS/JS (single file, no build step) |
| LLM | OpenRouter → OpenAI GPT (JSON mode) |
| Data | YouTube Data API v3 |
| Hosting | VPS (backend) + Vercel (frontend) |
| Tunnel | Cloudflare Named Tunnel |
| Routing | Hash-based (`#/`, `#/film/{name}`, `#/trailer/{name}`) |

## Key Files

| File | Purpose |
|------|---------|
| `server.py` | Backend: FastAPI app, YouTube scraping, LLM analysis pipeline |
| `frontend.html` | Frontend: single-file SPA with light/dark mode |
| `output/films.json` | Film registry (tracked films + metadata) |
| `output/{film}-data.json` | Processed analysis + hype data per film |
| `output/{film}-raw.json` | Raw scraped YouTube comments |
| `output/trailer-leaderboard-cache.json` | Cached trailer board (6h TTL) |
| `output/wiki-cache.json` | Wikipedia lookup cache (release dates, cast) |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/leaderboard` | GET | Films ranked by rating |
| `/api/new-releases` | GET | Fresh films <48h with <50 comments |
| `/api/trailer-leaderboard` | GET | Upcoming films by rating (6h cache) |
| `/api/film/{name}` | GET | Full 3-tier analysis + hype data |
| `/api/trailer/{name}` | GET | Trailer film detail (from cache) |
| `/api/film/{name}/refresh` | POST | Force re-scrape a film |
| `/api/refresh` | POST | Smart refresh all films (incremental) |
| `/api/refresh-trailer` | POST | Force rebuild trailer leaderboard cache |
| `/api/films` | GET | List tracked films |
| `/api/films/add/{name}` | POST | Add and process new film |
| `/api/tunnel-url` | GET | Current Cloudflare tunnel URL |
| `/api/scrape-releases` | GET | Wikipedia scraper for recent releases |
| `/` | GET | Dashboard frontend |

## Daily Costs

| Service | Cost |
|---------|------|
| YouTube Data API | Free (10K units/day, using ~1%) |
| OpenRouter LLM | ~$1/day (~20 LLM calls) |
| Vercel | Free (Hobby tier) |
| Cloudflare Tunnel | Free tier |
| VPS | Fixed infrastructure cost |
| **Total variable** | **~$1/day (~$30/month)** |

## Design Decisions

- **3-tier spoiler system** — Safe to share zero-spoiler tab with anyone; full spoiler tab for those who've seen the film
- **Incremental processing** — Only analyses new comments (≥15 threshold) to save LLM costs
- **Wikipedia verification** — Trailer board films checked against Wikipedia before processing (release date, cast, director)
- **Rating-based ranking** — Uses LLM-derived sentiment rating (0-10) instead of raw popularity scores
- **Non-Tamil film filtering** — Title keyword detection + comment script analysis (Telugu/Hindi unicode ranges) to exclude dubbed/non-Tamil films

## Tracked Films (at sunset)

Karuppu, Parimala and Co, Blast, 29, Love Insurance Kompany, Mr. X, Battle, Kara

## Environment Variables

```
YOUTUBE_API_KEY=       # YouTube Data API v3 key
OPENROUTER_API_KEY=    # OpenRouter API key for LLM
API_KEY=               # Frontend auth (X-API-Key header)
VPS_IP=                # VPS IP for frontend API calls
```

See `.env.example` for documented template.

## Running Locally

```bash
pip install -r requirements.txt
cp .env.example .env   # Fill in API keys
python server.py       # Starts on port 8080
```

---

*Built with YouTube Data API + OpenRouter. Analyzed ~400 comments per film across 8 tracked films.*
