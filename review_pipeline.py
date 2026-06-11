#!/usr/bin/env python3
"""
Tamil Movie Review Dashboard — Pipeline
=========================================
Scrapes YouTube review comments for a Tamil film,
runs LLM sentiment analysis, and outputs a dashboard.

Usage:
  python3 review_pipeline.py "Karuppu"
  python3 review_pipeline.py "Amaran" --videos 3 --comments 150

Modes:
  - API mode: set youtube_api_key in config.yaml (free, 10K quota/day)
  - Cookie mode: yt-dlp --cookies-from-browser firefox (no key needed)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

try:
    import httpx
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.markdown import Markdown
    from rich import box
except ImportError:
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-q", "httpx", "rich"]
    )
    import httpx
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.markdown import Markdown
    from rich import box

console = Console()

# ─── Config ────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "youtube_api_key": "",
    "output_dir": "./output",
    "max_comments_per_video": 200,
    "max_videos": 5,
}


def load_config(path: str = "./config.yaml") -> dict:
    import yaml

    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(path):
        with open(path) as f:
            loaded = yaml.safe_load(f) or {}
            cfg.update(loaded)
    # Resolve env var refs like "${OPENROUTER_API_KEY}"
    for k, v in cfg.items():
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            cfg[k] = os.environ.get(v[2:-1], "")
    return cfg


# ─── YouTube Comment Extraction ────────────────────────────────────────────

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


def search_videos(api_key: str, film_name: str, max_results: int = 5) -> list[dict]:
    """Search YouTube for Tamil review videos about a film."""
    queries = [
        f"{film_name} movie review tamil",
        f"{film_name} public review tamil",
        f"{film_name} honest review tamil",
        f"{film_name} review",
        f"{film_name} galatta plus",
        f"{film_name} OTT review tamil",
        f"{film_name} netflix review tamil",
    ]
    seen_ids = set()
    videos = []

    for query in queries:
        if len(videos) >= max_results:
            break
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": min(max_results - len(videos) + 2, 10),
            "relevanceLanguage": "ta",
            "key": api_key,
        }
        resp = httpx.get(f"{YOUTUBE_API_BASE}/search", params=params, timeout=15)
        if resp.status_code != 200:
            console.print(f"  [yellow]Search API error: {resp.status_code} - {resp.text[:200]}[/]")
            continue
        for item in resp.json().get("items", []):
            vid = item["id"]["videoId"]
            if vid not in seen_ids and len(videos) < max_results:
                seen_ids.add(vid)
                videos.append(
                    {
                        "id": vid,
                        "title": item["snippet"]["title"],
                        "channel": item["snippet"]["channelTitle"],
                        "published": item["snippet"]["publishedAt"],
                    }
                )
    return videos


def fetch_comments_api(api_key: str, video_id: str, max_comments: int = 200) -> list[str]:
    """Fetch YouTube comments via Data API."""
    comments = []
    next_token = None
    while len(comments) < max_comments:
        params = {
            "part": "snippet",
            "videoId": video_id,
            "maxResults": min(100, max_comments - len(comments)),
            "order": "relevance",
            "key": api_key,
        }
        if next_token:
            params["pageToken"] = next_token

        resp = httpx.get(
            f"{YOUTUBE_API_BASE}/commentThreads", params=params, timeout=15
        )
        if resp.status_code != 200:
            if resp.status_code == 403 and "commentsDisabled" in resp.text:
                console.print(f"  [yellow]Comments disabled for video {video_id}[/]")
            else:
                console.print(f"  [yellow]Comment API error: {resp.status_code}[/]")
            break

        data = resp.json()
        for item in data.get("items", []):
            text = item["snippet"]["topLevelComment"]["snippet"]["textDisplay"]
            text = clean_comment(text)
            if text and len(text) > 5:
                comments.append(text)

        next_token = data.get("nextPageToken")
        if not next_token:
            break
        time.sleep(0.1)  # rate limit courtesy

    return comments[:max_comments]


def fetch_comments_ytdlp(
    video_id: str, max_comments: int = 200, cookies_from: str = ""
) -> list[str]:
    """Fetch comments via yt-dlp (cookie-based fallback)."""
    cmd = [
        "yt-dlp",
        "--write-comments",
        "--skip-download",
        "--no-warnings",
        "--max-comments", str(max_comments),
        "--extractor-args", "youtube:max_comments=%d" % max_comments,
        "-o", "%(id)s",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    if cookies_from:
        cmd += ["--cookies-from-browser", cookies_from]
    else:
        # Try common cookie paths
        for path in [
            os.path.expanduser("~/.config/google-chrome/Default/Cookies"),
            os.path.expanduser("~/.mozilla/firefox/*.default-release/cookies.sqlite"),
        ]:
            expanded = os.path.expanduser(path)
            if "*" in path:
                from glob import glob
                matches = glob(expanded)
                if matches:
                    cmd += ["--cookies", matches[0]]
                    break
            elif os.path.exists(expanded):
                cmd += ["--cookies", expanded]
                break

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    # yt-dlp writes comments to a .info.json file
    info_path = f"{video_id}.info.json"
    comments = []
    if os.path.exists(info_path):
        with open(info_path) as f:
            info = json.load(f)
        for c in info.get("comments", []):
            text = clean_comment(c.get("text", ""))
            if text and len(text) > 5:
                comments.append(text)
        os.remove(info_path)

    if not comments:
        console.print(f"  [yellow]yt-dlp returned 0 comments (may need browser cookies)[/]")

    return comments[:max_comments]


def clean_comment(text: str) -> str:
    """Remove HTML tags, excessive whitespace, promo spam."""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Skip obvious promos
    promo_patterns = [
        r"dofy", r"norton", r"sponsor", r"buy\s+now", r"click\s+here",
        r"subscribe\s+to\s+our", r"follow\s+@", r"instagram\.com/",
    ]
    if any(re.search(p, text, re.I) for p in promo_patterns):
        return ""
    return text


def extract_comments(
    film_name: str,
    api_key: str = "",
    max_videos: int = 5,
    max_comments_per_video: int = 200,
) -> dict:
    """Master function: search + extract comments for a film."""
    console.print(f"\n[bold cyan]🎬 {film_name} — Fetching YouTube reviews...[/]")

    if api_key:
        console.print("  [dim]Using YouTube Data API (key mode)[/]")
        videos = search_videos(api_key, film_name, max_videos)
        if not videos:
            console.print("  [red]No videos found via API.[/]")
            return {"film": film_name, "videos": [], "comments": []}

        all_comments = []
        video_data = []
        for v in videos:
            console.print(f"  [green]→ {v['title'][:70]}...[/] ({v['channel']})")
            comments = fetch_comments_api(api_key, v["id"], max_comments_per_video)
            console.print(f"    Got {len(comments)} comments")
            all_comments.extend(comments)
            video_data.append({**v, "comment_count": len(comments)})
    else:
        console.print("  [dim]No API key — using yt-dlp cookie mode[/]")
        console.print("  [yellow]Tip: run with --cookies-browser firefox for better results[/]")
        # Search with yt-dlp
        search_cmd = [
            "yt-dlp",
            f"ytsearch{max_videos}:{film_name} movie review tamil",
            "--print", "%(id)s\t%(title)s\t%(channel)s",
            "--no-warnings",
        ]
        result = subprocess.run(search_cmd, capture_output=True, text=True, timeout=30)
        videos = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            if len(parts) == 3:
                videos.append({"id": parts[0], "title": parts[1], "channel": parts[2]})

        if not videos:
            console.print("  [red]No videos found via yt-dlp.[/]")
            return {"film": film_name, "videos": [], "comments": []}

        all_comments = []
        video_data = []
        for v in videos:
            console.print(f"  [green]→ {v['title'][:70]}...[/] ({v['channel']})")
            comments = fetch_comments_ytdlp(v["id"], max_comments_per_video)
            console.print(f"    Got {len(comments)} comments")
            all_comments.extend(comments)
            video_data.append({**v, "comment_count": len(comments)})

    return {
        "film": film_name,
        "videos": video_data,
        "comments": all_comments,
        "total_comments": len(all_comments),
        "fetched_at": datetime.now().isoformat(),
    }


# ─── LLM Analysis ──────────────────────────────────────────────────────────

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def analyze_with_llm(
    film_name: str,
    comments: list[str],
    openrouter_key: str,
    model: str = "openai/gpt-chat-latest",
) -> dict:
    """Send comments to LLM for sentiment analysis + summary."""
    if not comments:
        return {"error": "No comments to analyze"}

    # Sample if too many (keep it under ~15K tokens)
    sample = comments
    if len(sample) > 150:
        # Take top 75 by position + 75 random for diversity
        import random
        top = sample[:75]
        rest = random.sample(sample[75:], min(75, len(sample[75:])))
        sample = top + rest

    comment_text = "\n".join(f"{i+1}. {c}" for i, c in enumerate(sample))

    prompt = f"""You are a Tamil cinema review analyst. Analyze the following YouTube comments about the Tamil film "{film_name}".

The comments are in Tamil, English, or Tanglish (code-mixed Tamil-English).

IMPORTANT: You MUST output ONLY a valid JSON object. No markdown, no backticks, no explanations, no code fences. Just raw JSON that can be parsed with json.loads().

Analyze the comments and return this exact JSON structure:
{{
  "sentiment_breakdown": {{
    "positive_percent": <0-100>,
    "negative_percent": <0-100>,
    "mixed_percent": <0-100>,
    "neutral_percent": <0-100>
  }},
  "total_comments_analyzed": 54,
  "rating": <overall out of 10, one decimal>,
  "top_themes": [
    {{"theme": "<theme name>", "frequency": "<very high/high/medium/low>", "sentiment": "<positive/negative/mixed>"}}
  ],
  "summary": "<2-3 sentence summary of what people are saying about this film>",
  "audience_verdict": "<one sentence: what the crowd consensus is>",
  "what_people_loved": ["<thing 1>", "<thing 2>", "<thing 3>"],
  "what_people_criticized": ["<thing 1>", "<thing 2>"]
}}

Comments to analyze:
{comment_text}"""

    console.print(f"\n  [dim]Sending {len(sample)} comments to LLM for analysis...[/]")

    resp = httpx.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {openrouter_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/tamil-movie-dashboard",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 2000,
            "response_format": {"type": "json_object"},
        },
        timeout=120,
    )

    if resp.status_code != 200:
        return {"error": f"LLM API error: {resp.status_code} - {resp.text[:300]}"}

    content = resp.json()["choices"][0]["message"]["content"]
    # Extract JSON from response — handle markdown fences, trailing text, etc.
    content = content.strip()
    # Remove markdown code fences
    content = re.sub(r"^```(?:json)?\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    # Find the outermost JSON object
    brace_start = content.find("{")
    brace_end = content.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        json_str = content[brace_start : brace_end + 1]
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            # Try cleaning common issues
            json_str = re.sub(r",\s*}", "}", json_str)  # trailing commas
            json_str = re.sub(r",\s*]", "]", json_str)  # trailing commas in arrays
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                return {"error": "Failed to parse LLM response as JSON", "raw": content[:1000]}
    else:
        return {"error": "No JSON found in LLM response", "raw": content[:1000]}


# ─── Output / Dashboard ────────────────────────────────────────────────────

def print_dashboard(film_name: str, data: dict, analysis: dict):
    """Print a beautiful dashboard to terminal."""
    console.print()
    console.print(
        Panel(
            f"[bold yellow]🎬 {film_name.upper()} — Social Media Review Dashboard[/]\n"
            f"[dim]Fetched {data.get('total_comments', 0)} comments from {len(data.get('videos', []))} review videos[/]",
            box=box.HEAVY,
            border_style="yellow",
        )
    )

    if "error" in analysis:
        console.print(f"[red]Analysis error: {analysis['error']}[/]")
        return analysis

    # Rating
    rating = analysis.get("rating", "N/A")
    console.print(
        Panel(
            f"[bold cyan]⭐ Rating: {rating}/10[/]",
            box=box.ROUNDED,
            border_style="cyan",
        )
    )

    # Sentiment bar
    sb = analysis.get("sentiment_breakdown", {})
    pos = sb.get("positive_percent", 0)
    neg = sb.get("negative_percent", 0)
    mix = sb.get("mixed_percent", 0)
    neut = sb.get("neutral_percent", 0)

    bar_len = 30
    pos_bar = "🟢" * int(pos / 100 * bar_len)
    neg_bar = "🔴" * int(neg / 100 * bar_len)
    mix_bar = "🟡" * int(mix / 100 * bar_len)
    neut_bar = "⚪" * int(neut / 100 * bar_len)

    console.print(
        Panel(
            f"[green]Positive: {pos}%[/]   [red]Negative: {neg}%[/]   "
            f"[yellow]Mixed: {mix}%[/]   [dim]Neutral: {neut}%[/]\n"
            f"{pos_bar}{neg_bar}{mix_bar}{neut_bar}",
            title="📊 Sentiment Breakdown",
            box=box.ROUNDED,
        )
    )

    # Themes
    themes = analysis.get("top_themes", [])
    if themes:
        table = Table(title="🔍 Top Discussion Themes", box=box.SIMPLE)
        table.add_column("Theme", style="cyan")
        table.add_column("Frequency", style="yellow")
        table.add_column("Sentiment")
        for t in themes:
            sent_style = "green" if t.get("sentiment") == "positive" else "red" if t.get("sentiment") == "negative" else "yellow"
            table.add_row(
                t.get("theme", ""),
                t.get("frequency", ""),
                f"[{sent_style}]{t.get('sentiment', '')}[/]",
            )
        console.print(table)

    # Summary
    summary = analysis.get("summary", "")
    if summary:
        console.print(
            Panel(
                Markdown(summary),
                title="📝 Crowd Summary",
                box=box.ROUNDED,
                border_style="green",
            )
        )

    # Verdict
    verdict = analysis.get("audience_verdict", "")
    if verdict:
        console.print(
            Panel(
                f"[bold]{verdict}[/]",
                title="🎯 Audience Verdict",
                box=box.HEAVY,
                border_style="cyan",
            )
        )

    # Loved / Criticized
    loved = analysis.get("what_people_loved", [])
    criticized = analysis.get("what_people_criticized", [])
    if loved or criticized:
        t = Table(box=box.SIMPLE)
        t.add_column("❤️ What People Loved", style="green")
        t.add_column("💔 What People Criticized", style="red")
        max_rows = max(len(loved), len(criticized))
        for i in range(max_rows):
            l = loved[i] if i < len(loved) else ""
            c = criticized[i] if i < len(criticized) else ""
            t.add_row(l, c)
        console.print(t)

    # Sources
    console.print("\n[dim]YouTube review sources used:[/]")
    for v in data.get("videos", []):
        url = f"https://youtube.com/watch?v={v['id']}"
        console.print(f"  [link={url}]▶ {v['title'][:60]}[/] [dim]({v.get('comment_count',0)} comments)[/]")

    return analysis


def save_report(film_name: str, data: dict, analysis: dict, output_dir: str = "./output"):
    """Save the full report to a JSON file and a markdown file."""
    os.makedirs(output_dir, exist_ok=True)
    slug = film_name.lower().replace(" ", "-").replace("(", "").replace(")", "")

    report = {
        "film": film_name,
        "fetched_at": data.get("fetched_at", datetime.now().isoformat()),
        "total_comments": data.get("total_comments", 0),
        "videos": [
            {
                "title": v["title"],
                "channel": v["channel"],
                "url": f"https://youtube.com/watch?v={v['id']}",
                "comments_fetched": v.get("comment_count", 0),
            }
            for v in data.get("videos", [])
        ],
        "analysis": analysis,
    }

    # JSON
    json_path = os.path.join(output_dir, f"{slug}-report.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    console.print(f"\n[dim]📄 JSON report saved: {json_path}[/]")

    # Markdown
    md_path = os.path.join(output_dir, f"{slug}-dashboard.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(generate_markdown(film_name, data, analysis))
    console.print(f"[dim]📄 Markdown dashboard saved: {md_path}[/]")

    return json_path, md_path


def generate_markdown(film_name: str, data: dict, analysis: dict) -> str:
    """Generate a markdown dashboard suitable for serving as HTML."""
    lines = []
    lines.append(f"# 🎬 {film_name} — Social Media Review Dashboard")
    lines.append("")
    lines.append(f"> Fetched {data.get('total_comments', 0)} comments from {len(data.get('videos', []))} YouTube review videos")
    lines.append(f"> Generated: {data.get('fetched_at', datetime.now().isoformat())[:19]}")
    lines.append("")

    if "error" in analysis:
        lines.append(f"**Error:** {analysis['error']}")
        return "\n".join(lines)

    rating = analysis.get("rating", "N/A")
    lines.append(f"## ⭐ Rating: **{rating}/10**")
    lines.append("")

    sb = analysis.get("sentiment_breakdown", {})
    lines.append("## 📊 Sentiment Breakdown")
    lines.append(f"- 😊 Positive: **{sb.get('positive_percent', 0)}%**")
    lines.append(f"- 😠 Negative: **{sb.get('negative_percent', 0)}%**")
    lines.append(f"- 🤔 Mixed: **{sb.get('mixed_percent', 0)}%**")
    lines.append(f"- 😶 Neutral: **{sb.get('neutral_percent', 0)}%**")
    lines.append("")

    summary = analysis.get("summary", "")
    if summary:
        lines.append("## 📝 Crowd Summary")
        lines.append(summary)
        lines.append("")

    verdict = analysis.get("audience_verdict", "")
    if verdict:
        lines.append(f"## 🎯 Audience Verdict")
        lines.append(f"> **{verdict}**")
        lines.append("")

    themes = analysis.get("top_themes", [])
    if themes:
        lines.append("## 🔍 Key Themes")
        lines.append("| Theme | Frequency | Sentiment |")
        lines.append("|-------|-----------|-----------|")
        for t in themes:
            emoji = "😊" if t.get("sentiment") == "positive" else "😠" if t.get("sentiment") == "negative" else "🤔"
            lines.append(f"| {t.get('theme', '')} | {t.get('frequency', '')} | {emoji} {t.get('sentiment', '')} |")
        lines.append("")

    loved = analysis.get("what_people_loved", [])
    criticized = analysis.get("what_people_criticized", [])
    if loved:
        lines.append("## ❤️ What People Loved")
        for item in loved:
            lines.append(f"- ✅ {item}")
        lines.append("")
    if criticized:
        lines.append("## 💔 What People Criticized")
        for item in criticized:
            lines.append(f"- ❌ {item}")
        lines.append("")

    lines.append("## 📺 Sources")
    for v in data.get("videos", []):
        url = f"https://youtube.com/watch?v={v['id']}"
        lines.append(f"- [{v['title']}]({url}) — {v['channel']} ({v.get('comment_count', 0)} comments)")
    lines.append("")
    lines.append("---")
    lines.append(f"*Generated by Tamil Movie Review Dashboard Pipeline*")

    return "\n".join(lines)


# ─── CLI ───────────────────────────────────────────────────────────────────

def main():
    # Auto-load .env if present
    dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(dotenv_path):
        with open(dotenv_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    # Also check Hermes .env
    hermes_dotenv = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(hermes_dotenv):
        with open(hermes_dotenv) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    parser = argparse.ArgumentParser(
        description="Tamil Movie Review Dashboard — YouTube comment analysis pipeline"
    )
    parser.add_argument("film", help="Tamil film name (e.g. 'Karuppu', 'Amaran')")
    parser.add_argument("--videos", type=int, default=5, help="Max review videos to fetch")
    parser.add_argument("--comments", type=int, default=200, help="Max comments per video")
    parser.add_argument("--config", default="./config.yaml", help="Config file path")
    parser.add_argument("--cookies-browser", help="Browser to extract cookies from (e.g. firefox, chrome)")
    parser.add_argument("--model", default="openai/gpt-chat-latest", help="LLM model for analysis")
    parser.add_argument("--no-save", action="store_true", help="Skip saving report files")
    args = parser.parse_args()

    config = load_config(args.config)
    api_key = config.get("youtube_api_key", "")
    output_dir = config.get("output_dir", "./output")

    # Step 1: Extract comments
    data = extract_comments(
        args.film,
        api_key=api_key,
        max_videos=args.videos,
        max_comments_per_video=args.comments,
    )

    if not data["comments"]:
        console.print("[red]❌ No comments found. Try a different film name or check YouTube access.[/]")
        sys.exit(1)

    console.print(f"\n[bold]✅ Total comments collected: {len(data['comments'])}[/]")

    # Step 2: LLM Analysis
    openrouter_key = (
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("OPENROUTER_KEY")
        or ""
    )
    if not openrouter_key:
        console.print("[red]❌ No OpenRouter API key found. Set OPENROUTER_API_KEY in env.[/]")
        sys.exit(1)

    analysis = analyze_with_llm(args.film, data["comments"], openrouter_key, args.model)

    # Step 3: Dashboard
    print_dashboard(args.film, data, analysis)

    # Step 4: Save
    if not args.no_save:
        save_report(args.film, data, analysis, output_dir)

    # Print copy-paste friendly markdown
    if "error" not in analysis:
        console.print("\n[bold]📋 Markdown Dashboard:[/]")
        md = generate_markdown(args.film, data, analysis)
        console.print(Panel(md.strip(), border_style="blue"))


if __name__ == "__main__":
    main()