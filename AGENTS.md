# Tamil Movie Review Dashboard — AGENTS.md

## Architecture Overview

### Backend (VPS)
- **FastAPI** server (`server.py`) on port 8080
- **YouTube Data API v3** for comment/video scraping (free tier: 10K units/day)
- **OpenRouter LLM** (`openai/gpt-chat-latest`) for sentiment analysis with JSON mode
- Output stored in `output/` directory as JSON files
- **Incremental processing**: only new comments are analyzed (tracks `processed_comment_ids`)

### Frontend
- Single-page HTML (`frontend.html`) served by backend at `/`
- Also deployed to Vercel for public access
- Hash-based routing: `#/` (leaderboard), `#/film/{name}` (theatrical detail), `#/trailer/{name}` (trailer detail)
- Light/dark mode toggle (default: light), persists in localStorage
- No build step — single HTML file with all CSS/JS inlined

### Deployment
- **VPS**: `http://<VPS_IP>:8080` (IP configured via `VPS_IP` env var)
- **Cloudflare Named Tunnel**: `https://movies.onekural.com` → `localhost:8080` (permanent URL)
- **Vercel**: `movies-vibe-dashboard.vercel.app` (env vars: `VITE_API_KEY`, `VITE_VPS_IP`)
- Frontend placeholders: `__VITE_API_KEY__` and `__VITE_VPS_IP__` replaced at build time
- **GitHub**: `tharunsuresh-code/movies-vibe-dashboard` (auto-deploys to Vercel on push)

## Design Decisions

### OTT Section Removed (June 2026)
OTT tracking was removed because:
- No reliable event source for OTT release dates (YouTube OTT videos are scattered)
- False positive detection from video titles mentioning platform names
- Low signal-to-noise ratio in OTT comments vs theatrical reviews
- Films drop from leaderboard after 30-day theatrical window (assumed OTT)

### Video Source Contamination Fix
Short/numeric film names (e.g. "29") caused YouTube search to return unrelated videos.
Fix: `_is_relevant_video()` validates film name appears in title with context keywords
(movie/film/review/trailer/cast names) for titles ≤3 characters.

### Dubbed Film Filtering
Tamil dubbed versions of other language films are excluded from the trailer board.
Detection: titles containing "tamil dubbed", "dubbed", "telugu to tamil", etc.

### Wikipedia Verification
Trailer board films are verified against Wikipedia **before** fetching comments/LLM:
- Release date check: skip if already released (saves API quota)
- Cast extraction: shown on detail pages
- Director extraction: `check_wiki_director()` parses "Directed by" from infobox
- Film name lookup: tries "(2026 film)", "(2025 film)", "(film)" variants
- Results cached in `output/wiki-cache.json`

### Popularity Score Formula
- LLM base (0-50) + log-scaled volume bonus (0-30) + sentiment bonus (0-20) = 0-100
- Hotness score = popularity × recency multiplier:
  - 0-1 month: 1.0x (full weight)
  - 1-2 months: 0.65x (mild decay)
  - 2+ months: excluded from leaderboard

### Hype Score (Trailer-Based)
For upcoming/new films, computes hype from trailer data:
- View count (log scale, **0-45**) — views matter most
- **View threshold bonus**: 500K+ views → +5, 1M+ views → +10
- Like ratio percentage (0-15)
- LLM sentiment analysis of trailer comments (0-25)
- Comment volume (log scale, 0-15)
- LLM returns `category` (Celebratory/Excited/Cautiously Optimistic/Mixed/Polarizing/etc.)
- LLM returns `genre_phrase` (short 5-8 word genre description, avoids generic enthusiasm)
- Stored in film data under `"hype"` key

### Trailer Category Labels
Instead of raw sentiment (positive/negative), the LLM classifies into richer categories:
Celebratory, Excited, Cautiously Optimistic, Mixed, Polarizing, Disappointed, Surprised, Divisive, Anticipated, Muted.
These are shown on trailer board rows with emoji badges.
Genre phrase replaces generic "Audience is excited" with specific genre descriptions.

### Incremental Comment Processing
The pipeline tracks `processed_comment_ids` per film. On refresh:
1. Fetch comments sorted by time (newest first)
2. Filter out already-seen comment IDs → get delta
3. If delta ≥15 new comments: run LLM merge analysis
4. If delta <15: skip (not worth LLM cost)
5. Merge prompt updates existing analysis with new sentiment data

This generalizes well around release dates — lots of new comments → frequent updates.

### Leaderboard Rules
- **Theatrical**: films within 30-day release window, minimum 5 comments
- **Trailer**: upcoming/unreleased films ranked by trailer hype (6-hour cache)
- Films transition from trailer board → theatrical board → drop off after 30 days
- No OTT exclusion — all qualifying films appear on leaderboard

### Light/Dark Mode
- Default: light theme (`:root` CSS variables)
- Dark mode: `[data-theme="dark"]` override
- Toggle button (🌙/☀️) in fixed position, saves to localStorage
- Respects OS `prefers-color-scheme` on first visit

## Cron Jobs

### Trailer Board Refresh
- **Schedule**: Daily at 8 AM UTC (midnight Pacific)
- **Endpoint**: `POST /api/refresh-trailer`
- **Cost**: ~37 units/day

### Theatrical Board Refresh
- **Schedule**: Every 6h starting IST midnight (18:30, 00:30, 06:30, 12:30 UTC)
- **Endpoint**: `POST /api/refresh`
- **Cost**: ~22 units per run × 4/day = ~88 units/day
- Around release days: captures fresh audience feedback every 6h

### Total Daily Quota: ~125 units (~1.25% of 10K budget)

## Known Issues

### YouTube API Quota
10K units/day free tier. Each search = 1 unit, each comment page = 1 unit.
Smart refresh skips films with no new videos. Trailer hype adds ~2-4 extra search calls per film.

## File Structure

- `server.py` — Backend: FastAPI app, pipeline, LLM analysis, YouTube scraping
- `frontend.html` — Frontend: single-file SPA with 3-tier spoiler system
- `review_pipeline.py` — Standalone CLI pipeline (legacy, not used by server)
- `output/films.json` — Film registry (tracked films + metadata)
- `output/{film}-data.json` — Processed analysis + hype data per film
- `output/{film}-raw.json` — Raw scraped comments per film
- `output/trailer-leaderboard-cache.json` — Cached trailer board (6h TTL)
- `output/wiki-cache.json` — Wikipedia lookup cache
- `.env` — Secrets (YOUTUBE_API_KEY, OPENROUTER_API_KEY, API_KEY, VPS_IP)
- `.env.example` — Documented env var template
- `config.yaml` — Pipeline configuration
- `requirements.txt` — Python dependencies
- `vercel.json` — Vercel build config (env var injection)
- `AGENTS.md` — This file

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/leaderboard` | GET | Films ranked by hotness score |
| `/api/new-releases` | GET | Fresh films <48h with <50 comments (includes hype) |
| `/api/trailer-leaderboard` | GET | Upcoming films by trailer hype (6h cache) |
| `/api/film/{name}` | GET | Full 3-tier analysis + hype data |
| `/api/trailer/{name}` | GET | Trailer film detail (from cache) |
| `/api/film/{name}/refresh` | POST | Force re-scrape a film |
| `/api/refresh` | POST | Smart refresh all films (incremental) |
| `/api/refresh-trailer` | POST | Force rebuild trailer leaderboard cache |
| `/api/films` | GET | List tracked films |
| `/api/films/add/{name}` | POST | Add and process new film |
| `/api/tunnel-url` | GET | Current Cloudflare tunnel URL |
| `/api/scrape-releases` | GET | Wikipedia scraper for recent releases |
| `/` | GET | Dashboard frontend (serves frontend.html) |

## YouTube API Quota Budget

| Operation | Calls | Units |
|-----------|-------|-------|
| Theatrical refresh (8 films) | ~5 search + ~16 comment + ~1 stats | ~22 |
| Trailer leaderboard refresh | ~5 search + ~30 comment + ~1 stats | ~36 |
| Detail page clicks | 0 (cached) | 0 |
| **Daily total (with 4h theatrical + 1 daily trailer)** | | **~125** |
