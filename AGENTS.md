# Tamil Movie Reviews Dashboard — AGENTS.md

## Architecture Overview

### Backend (VPS)
- **FastAPI** server (`server.py`) on port 8080
- **YouTube Data API v3** for comment/video scraping (free tier: 10K units/day)
- **OpenRouter LLM** (`openai/gpt-chat-latest`) for sentiment analysis with JSON mode
- Output stored in `output/` directory as JSON files
- Smart refresh: only re-processes films with new YouTube videos

### Frontend
- Single-page HTML (`frontend.html`) served by backend at `/`
- Also deployed to Vercel for public access
- Hash-based routing: `#/` (leaderboard), `#/film/{name}` (3-tier detail)
- Light/dark mode toggle (default: light), persists in localStorage
- No build step — single HTML file with all CSS/JS inlined

### Deployment
- **VPS**: `http://<VPS_IP>:8080` (IP configured via `VPS_IP` env var)
- **Cloudflare Quick Tunnel**: random URL (changes on restart, dynamic discovery via `/api/tunnel-url`)
- **Vercel**: `movies-vibe-dashboard.vercel.app` (env vars: `VITE_API_KEY`, `VITE_VPS_IP`)
- Frontend placeholders: `__VITE_API_KEY__` and `__VITE_VPS_IP__` replaced at serve time

## Design Decisions

### OTT Section Removed (June 2026)
OTT tracking was removed because:
- No reliable event source for OTT release dates (YouTube OTT videos are scattered)
- False positive detection from video titles mentioning platform names
- Low signal-to-noise ratio in OTT comments vs theatrical reviews
- Films with strong theatrical runs just stay on the main leaderboard

### Video Source Contamination Fix
Short/numeric film names (e.g. "29") caused YouTube search to return unrelated videos.
Fix: `_is_relevant_video()` validates film name appears in title with context keywords
(movie/film/review/trailer/cast names) for titles ≤3 characters.

### Popularity Score Formula
- LLM base (0-50) + log-scaled volume bonus (0-30) + sentiment bonus (0-20) = 0-100
- Hotness score = popularity × recency multiplier:
  - 0-1 month: 1.0x (full weight)
  - 1-2 months: 0.65x (mild decay)
  - 2+ months: excluded from leaderboard

### Hype Score (Trailer-Based)
For upcoming/new films, computes hype from trailer data:
- View count (log scale, 0-30)
- Like ratio percentage (0-20)
- LLM sentiment analysis of trailer comments (0-30)
- Comment volume (log scale, 0-20)
- Stored in film data under `"hype"` key

### Leaderboard Rules
- Films within 2-month release window only
- Minimum 5 comments required
- Films <48 hours old with <50 comments → "New Releases" section instead
- No OTT exclusion — all qualifying films appear on leaderboard

### Light/Dark Mode
- Default: light theme (`:root` CSS variables)
- Dark mode: `[data-theme="dark"]` override
- Toggle button (🌙/☀️) in fixed position, saves to localStorage
- Respects OS `prefers-color-scheme` on first visit

## Known Issues

### Cloudflare Tunnel URL
Quick tunnel URLs change on restart. Frontend auto-discovers via `/api/tunnel-url`,
but mixed-content blocking prevents Vercel-served pages from reaching HTTP VPS.
**Workaround:** Update `TUNNEL_URL` in frontend.html after restart, or use VPS-served frontend.

### YouTube API Quota
10K units/day free tier. Each search = 1 unit, each comment page = 1 unit.
Smart refresh skips films with no new videos. Trailer hype adds ~2-4 extra search calls per film.

## File Structure

- `server.py` — Backend: FastAPI app, pipeline, LLM analysis, YouTube scraping
- `frontend.html` — Frontend: single-file SPA with 3-tier spoiler system
- `output/films.json` — Film registry (tracked films + metadata)
- `output/{film}-data.json` — Processed analysis + hype data per film
- `output/{film}-raw.json` — Raw scraped comments per film
- `.env` — Secrets (YOUTUBE_API_KEY, OPENROUTER_API_KEY)
- `config.yaml` — Pipeline configuration
- `AGENTS.md` — This file

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/leaderboard` | GET | Films ranked by hotness score |
| `/api/new-releases` | GET | Fresh films <48h with <50 comments (includes hype) |
| `/api/film/{name}` | GET | Full 3-tier analysis + hype data |
| `/api/film/{name}/refresh` | POST | Force re-scrape a film |
| `/api/refresh` | POST | Smart refresh all films |
| `/api/films` | GET | List tracked films |
| `/api/films/add/{name}` | POST | Add and process new film |
| `/api/tunnel-url` | GET | Current Cloudflare tunnel URL |
| `/api/scrape-releases` | GET | Wikipedia scraper for recent releases |
| `/` | GET | Dashboard frontend (serves index.html) |
