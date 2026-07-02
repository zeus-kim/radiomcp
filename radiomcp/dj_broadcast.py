"""
dj_broadcast.py - Multi-provider DJ broadcast system for radiomcp

Providers (priority order, auto-fallback):
  1. spotify      (AppleScript, requires Spotify.app)
  2. apple_music  (AppleScript, requires Music.app)
  3. youtube      (yt-dlp + mpv, always available fallback)

Features:
  - Noise filtering: prefer official/audio, skip live/cover/remix noise
  - DJ voice comments (edge-tts) inserted between songs
  - 24h scheduled auto-broadcast with time-slot programming
"""
import os
import re
import json
import time
import shutil
import subprocess
import threading
import random
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, Future

DATA_DIR = os.path.expanduser("~/.radiocli")
os.makedirs(DATA_DIR, exist_ok=True)

SCHEDULE_FILE = os.path.join(DATA_DIR, "broadcast_schedule.json")
STATE_FILE = os.path.join(DATA_DIR, "broadcast_state.json")
LOG_FILE = os.path.join(DATA_DIR, "broadcast.log")

# --- Cross-process playback coordination (singleton lock) ---
# Every radiomcp instance on this machine shares this file. Writing a newer
# timestamp tells ALL instances' DJ workers to stop the current set. This makes
# playback effectively single: any stop halts audio no matter which instance
# started it, and a new start preempts whatever was already playing elsewhere.
STOP_TOKEN_FILE = os.path.join(DATA_DIR, "playback.stop")


def _signal_global_stop():
    """Tell every radiomcp instance to stop the current DJ set."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(STOP_TOKEN_FILE, "w") as f:
            f.write(repr(time.time()))
    except Exception:
        pass


def _global_stop_token():
    """Latest global-stop timestamp (0.0 if never signaled)."""
    try:
        with open(STOP_TOKEN_FILE) as f:
            return float(f.read().strip() or 0)
    except Exception:
        return 0.0
STATS_FILE = os.path.join(DATA_DIR, "broadcast_stats.json")


# ============================================================
# Logging & Statistics for Production
# ============================================================
def _log(level, msg):
    """Append log entry."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _log_info(msg):
    _log("INFO", msg)


def _log_error(msg):
    _log("ERROR", msg)


def _update_stats(key, increment=1):
    """Update broadcast statistics."""
    try:
        stats = {}
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, "r") as f:
                stats = json.load(f)

        stats[key] = stats.get(key, 0) + increment
        stats["last_updated"] = datetime.now().isoformat()

        with open(STATS_FILE, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception:
        pass


def get_broadcast_stats():
    """Get broadcast statistics."""
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {"songs_played": 0, "comments_spoken": 0, "errors": 0}


def get_broadcast_logs(lines=50):
    """Get recent log entries."""
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
                return all_lines[-lines:]
    except Exception:
        pass
    return []


# ============================================================
# News RSS for DJ Broadcast
# ============================================================
import xml.etree.ElementTree as ET
import urllib.request

NEWS_RSS_FEEDS = {
    # === 한국 ===
    "연합뉴스": "https://www.yonhapnewstv.co.kr/browse/feed/",
    "조선일보": "https://www.chosun.com/arc/outboundfeeds/rss/?outputType=xml",
    "중앙일보": "https://rss.joins.com/joins_news_list.xml",
    "KBS": "https://world.kbs.co.kr/rss/rss_news.htm?lang=k",

    # === 국제 통신사 ===
    "Reuters": "https://www.reutersagency.com/feed/?taxonomy=best-topics&post_type=best",
    "AP News": "https://rsshub.app/apnews/topics/apf-topnews",
    "AFP": "https://www.afp.com/en/rss-feeds",

    # === 영국 ===
    "BBC": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "The Guardian": "https://www.theguardian.com/world/rss",
    "Financial Times": "https://www.ft.com/rss/home",

    # === 미국 ===
    "NPR": "https://feeds.npr.org/1001/rss.xml",
    "CNN": "http://rss.cnn.com/rss/edition_world.rss",
    "NYTimes": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "Washington Post": "https://feeds.washingtonpost.com/rss/world",
    "PBS": "https://www.pbs.org/newshour/feeds/rss/headlines",

    # === 일본 ===
    "NHK": "https://www.nhk.or.jp/rss/news/cat0.xml",
    "Asahi": "https://www.asahi.com/rss/asahi/newsheadlines.rdf",
    "Nikkei": "https://www.nikkei.com/rss/",

    # === 중국 ===
    "Xinhua": "http://www.xinhuanet.com/english/rss/worldrss.xml",
    "SCMP": "https://www.scmp.com/rss/91/feed",

    # === 유럽 ===
    "DW (독일)": "https://rss.dw.com/rdf/rss-en-all",
    "France24": "https://www.france24.com/en/rss",
    "El País (스페인)": "https://feeds.elpais.com/mrss-s/pages/ep/site/english.elpais.com/portada",
    "ANSA (이탈리아)": "https://www.ansa.it/sito/ansait_rss.xml",
    "NOS (네덜란드)": "https://feeds.nos.nl/nosnieuwsalgemeen",
    "SVT (스웨덴)": "https://www.svt.se/nyheter/rss.xml",

    # === 중동 ===
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "Times of Israel": "https://www.timesofisrael.com/feed/",
    "Arab News": "https://www.arabnews.com/rss.xml",

    # === 아시아 ===
    "CNA (싱가포르)": "https://www.channelnewsasia.com/rssfeeds/8395986",
    "Straits Times": "https://www.straitstimes.com/news/world/rss.xml",
    "Bangkok Post": "https://www.bangkokpost.com/rss/data/world.xml",
    "Hindustan Times": "https://www.hindustantimes.com/feeds/rss/world-news/rssfeed.xml",
    "Dawn (파키스탄)": "https://www.dawn.com/feeds/home",

    # === 오세아니아 ===
    "ABC (호주)": "https://www.abc.net.au/news/feed/51120/rss.xml",
    "NZ Herald": "https://www.nzherald.co.nz/arc/outboundfeeds/rss/curated/78/?outputType=xml",

    # === 아프리카 ===
    "News24 (남아공)": "https://feeds.news24.com/articles/news24/World/rss",
    "Daily Nation (케냐)": "https://nation.africa/kenya/rss.xml",

    # === 남미 ===
    "Folha (브라질)": "https://feeds.folha.uol.com.br/world/rss091.xml",
    "La Nación (아르헨티나)": "https://www.lanacion.com.ar/arcio/rss/",

    # === 러시아/동유럽 ===
    "TASS": "https://tass.com/rss/v2.xml",
    "Kyiv Independent": "https://kyivindependent.com/feed/",

    # === 기술/비즈니스 ===
    "TechCrunch": "https://techcrunch.com/feed/",
    "Hacker News": "https://hnrss.org/frontpage",
    "Bloomberg": "https://feeds.bloomberg.com/markets/news.rss",
}

NEWS_SUBSCRIPTIONS_FILE = os.path.join(DATA_DIR, "news_subscriptions.json")


def get_news_subscriptions():
    """Get user's RSS subscriptions."""
    try:
        if os.path.exists(NEWS_SUBSCRIPTIONS_FILE):
            with open(NEWS_SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    # 기본 구독
    return {"sources": ["BBC", "연합뉴스", "Reuters"], "custom": []}


def set_news_subscriptions(sources=None, custom_feeds=None):
    """Set user's RSS subscriptions.

    Args:
        sources: List of preset source names (e.g. ["BBC", "연합뉴스"])
        custom_feeds: List of custom RSS URLs with names
                      e.g. [{"name": "My Blog", "url": "https://..."}]
    """
    subs = get_news_subscriptions()
    if sources is not None:
        subs["sources"] = sources
    if custom_feeds is not None:
        subs["custom"] = custom_feeds

    with open(NEWS_SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(subs, f, ensure_ascii=False, indent=2)

    return subs


def list_available_feeds():
    """List all available preset RSS feeds."""
    return {
        "presets": list(NEWS_RSS_FEEDS.keys()),
        "subscriptions": get_news_subscriptions()
    }


def add_custom_feed(name, url, category="custom"):
    """Add a custom RSS feed subscription.

    Args:
        name: Display name for the feed
        url: RSS feed URL
        category: Category (news, reddit, meme, podcast, blog, etc.)
    """
    subs = get_news_subscriptions()
    if "custom" not in subs:
        subs["custom"] = []

    # 중복 체크
    for feed in subs["custom"]:
        if feed.get("url") == url:
            return {"status": "exists", "feed": feed}

    new_feed = {"name": name, "url": url, "category": category}
    subs["custom"].append(new_feed)

    with open(NEWS_SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(subs, f, ensure_ascii=False, indent=2)

    return {"status": "added", "feed": new_feed}


def remove_custom_feed(url):
    """Remove a custom RSS feed."""
    subs = get_news_subscriptions()
    if "custom" in subs:
        subs["custom"] = [f for f in subs["custom"] if f.get("url") != url]
        with open(NEWS_SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(subs, f, ensure_ascii=False, indent=2)
    return {"status": "removed"}


# Reddit RSS feeds (popular subreddits)
REDDIT_FEEDS = {
    "r/worldnews": "https://www.reddit.com/r/worldnews/.rss",
    "r/technology": "https://www.reddit.com/r/technology/.rss",
    "r/music": "https://www.reddit.com/r/music/.rss",
    "r/kpop": "https://www.reddit.com/r/kpop/.rss",
    "r/funny": "https://www.reddit.com/r/funny/.rss",
    "r/memes": "https://www.reddit.com/r/memes/.rss",
    "r/todayilearned": "https://www.reddit.com/r/todayilearned/.rss",
    "r/showerthoughts": "https://www.reddit.com/r/Showerthoughts/.rss",
    "r/upliftingnews": "https://www.reddit.com/r/UpliftingNews/.rss",
    "r/nottheonion": "https://www.reddit.com/r/nottheonion/.rss",
}


def fetch_reddit_posts(subreddit="funny", limit=5):
    """Fetch posts from a subreddit RSS."""
    if subreddit.startswith("r/"):
        subreddit = subreddit[2:]

    url = f"https://www.reddit.com/r/{subreddit}/.rss"
    posts = []

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 radiomcp/1.0"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read()
            root = ET.fromstring(content)

            # Atom format (Reddit uses Atom)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall("atom:entry", ns)[:limit]:
                title = entry.find("atom:title", ns)
                if title is not None and title.text:
                    posts.append({
                        "title": title.text.strip(),
                        "subreddit": subreddit
                    })
    except Exception as e:
        _log_error(f"Reddit fetch failed (r/{subreddit}): {e}")

    return posts


def make_reddit_talk(subreddits=None, max_items=2):
    """Create DJ talk material from Reddit posts.

    Args:
        subreddits: List of subreddits (default: showerthoughts, todayilearned)
        max_items: Max items to include

    Returns:
        String for DJ to read, or None
    """
    if subreddits is None:
        subreddits = ["Showerthoughts", "todayilearned"]

    items = []
    for sub in subreddits:
        posts = fetch_reddit_posts(sub, limit=3)
        if posts:
            items.extend(posts[:1])  # 각 서브레딧에서 1개씩
        if len(items) >= max_items:
            break

    if not items:
        return None

    talk = "재미있는 이야기 하나 할게요. "
    for item in items[:max_items]:
        title = item["title"]
        # 너무 긴 제목 자르기
        if len(title) > 80:
            title = title[:80] + "..."
        talk += f"{title} "

    return talk.strip()


# Telegram channel RSS (via rsshub or tg.i-c-a.su)
def get_telegram_rss_url(channel):
    """Get RSS URL for a Telegram channel.

    Args:
        channel: Channel username (without @) or full URL

    Returns:
        RSS feed URL
    """
    if channel.startswith("https://t.me/"):
        channel = channel.split("/")[-1]
    if channel.startswith("@"):
        channel = channel[1:]

    # RSShub 사용 (가장 안정적)
    return f"https://rsshub.app/telegram/channel/{channel}"


def add_telegram_channel(channel, name=None):
    """Subscribe to a Telegram channel via RSS.

    Args:
        channel: Channel username (e.g. "duaborams" or "@duaborams")
        name: Display name (default: @channel)

    Returns:
        Subscription result
    """
    if channel.startswith("@"):
        channel = channel[1:]

    url = get_telegram_rss_url(channel)
    display_name = name or f"@{channel}"

    return add_custom_feed(display_name, url, category="telegram")


def fetch_telegram_channel(channel, limit=5):
    """Fetch posts from a Telegram channel."""
    url = get_telegram_rss_url(channel)
    return fetch_rss_headlines(url, limit)


def get_feed_content(category="all", limit=10):
    """Get content from all subscribed feeds.

    Args:
        category: Filter by category (all, news, reddit, meme, etc.)
        limit: Max items per feed

    Returns:
        Dict with content organized by source
    """
    subs = get_news_subscriptions()
    content = {}

    # Preset feeds
    for source in subs.get("sources", []):
        if source in NEWS_RSS_FEEDS:
            headlines = fetch_rss_headlines(NEWS_RSS_FEEDS[source], limit)
            if headlines:
                content[source] = {"type": "news", "items": headlines}

    # Custom feeds
    for feed in subs.get("custom", []):
        if category != "all" and feed.get("category") != category:
            continue
        headlines = fetch_rss_headlines(feed["url"], limit)
        if headlines:
            content[feed["name"]] = {"type": feed.get("category", "custom"), "items": headlines}

    return content

NEWS_CACHE_FILE = os.path.join(DATA_DIR, "news_cache.json")


def fetch_rss_headlines(feed_url, limit=5):
    """Fetch headlines from RSS feed."""
    headlines = []
    try:
        req = urllib.request.Request(feed_url, headers={
            "User-Agent": "Mozilla/5.0 radiomcp/1.0"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read()
            root = ET.fromstring(content)

            # RSS 2.0 format
            for item in root.findall(".//item")[:limit]:
                title = item.find("title")
                if title is not None and title.text:
                    headlines.append(title.text.strip())

            # Atom format fallback
            if not headlines:
                for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry")[:limit]:
                    title = entry.find("{http://www.w3.org/2005/Atom}title")
                    if title is not None and title.text:
                        headlines.append(title.text.strip())
    except Exception as e:
        _log_error(f"RSS fetch failed ({feed_url}): {e}")

    return headlines


# 주요 통신사 (톱뉴스 우선순위)
TOP_NEWS_PRIORITY = [
    # 국제 통신사 (가장 중요)
    "Reuters", "AP News", "AFP",
    # 주요 방송
    "BBC", "CNN", "NHK",
    # 한국 주요
    "연합뉴스", "KBS",
    # 권위지
    "NYTimes", "The Guardian", "Financial Times",
]


def fetch_top_news(sources=None, limit=5, prioritize_top=True):
    """Fetch top news from multiple sources.

    Args:
        sources: List of source names (default: from subscriptions)
        limit: Headlines per source
        prioritize_top: Put major wire services first

    Returns:
        Dict with headlines by source, ordered by importance
    """
    if sources is None:
        subs = get_news_subscriptions()
        sources = subs.get("sources", ["Reuters", "BBC", "연합뉴스"])

    # 우선순위 정렬
    if prioritize_top:
        priority_sources = [s for s in TOP_NEWS_PRIORITY if s in sources]
        other_sources = [s for s in sources if s not in TOP_NEWS_PRIORITY]
        sources = priority_sources + other_sources

    news = {}
    for source in sources:
        if source in NEWS_RSS_FEEDS:
            headlines = fetch_rss_headlines(NEWS_RSS_FEEDS[source], limit)
            if headlines:
                news[source] = headlines

    # Cache results
    try:
        cache = {
            "fetched_at": datetime.now().isoformat(),
            "news": news
        }
        with open(NEWS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return news


def get_top_headlines(max_items=5):
    """Get the most important headlines across all sources.

    Returns headlines prioritized by source importance.
    """
    news = get_cached_news()
    if not news:
        news = fetch_top_news(limit=3, prioritize_top=True)

    headlines = []
    # 우선순위 순서대로 수집
    for source in TOP_NEWS_PRIORITY:
        if source in news and news[source]:
            headlines.append({
                "source": source,
                "headline": news[source][0],
                "priority": "high"
            })
        if len(headlines) >= max_items:
            break

    # 부족하면 다른 소스에서 추가
    for source, items in news.items():
        if source not in TOP_NEWS_PRIORITY and items:
            headlines.append({
                "source": source,
                "headline": items[0],
                "priority": "normal"
            })
        if len(headlines) >= max_items:
            break

    return headlines


NEWS_CACHE_TTL = 300  # 5분 (라이브 방송용 짧은 캐시)


def get_cached_news(max_age_seconds=None):
    """Get cached news (avoid frequent RSS fetches).

    Args:
        max_age_seconds: Max cache age (default: NEWS_CACHE_TTL = 5분)
    """
    max_age = max_age_seconds or NEWS_CACHE_TTL
    try:
        if os.path.exists(NEWS_CACHE_FILE):
            with open(NEWS_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
                fetched = datetime.fromisoformat(cache["fetched_at"])
                age = (datetime.now() - fetched).seconds
                if age < max_age:
                    return cache["news"]
    except Exception:
        pass
    return None


def fetch_fresh_news(sources=None, limit=5):
    """Force fetch fresh news (ignore cache)."""
    return fetch_top_news(sources=sources, limit=limit, prioritize_top=True)


NEWS_OUTPUT_LANG_FILE = os.path.join(DATA_DIR, "news_output_lang.json")


def get_news_output_lang():
    """Get preferred language for news output."""
    try:
        if os.path.exists(NEWS_OUTPUT_LANG_FILE):
            with open(NEWS_OUTPUT_LANG_FILE, "r") as f:
                return json.load(f).get("lang", "ko")
    except Exception:
        pass
    return "ko"


def set_news_output_lang(lang):
    """Set preferred language for news output.

    Args:
        lang: Language code (ko, en, ja, zh, es, fr, de, etc.)
    """
    with open(NEWS_OUTPUT_LANG_FILE, "w") as f:
        json.dump({"lang": lang}, f)
    return {"lang": lang}


def translate_text(text, target_lang="ko"):
    """Translate text using local LLM (ollama).

    Args:
        text: Text to translate
        target_lang: Target language code

    Returns:
        Translated text or original if translation fails
    """
    if not text:
        return text

    lang_names = {
        "ko": "Korean", "en": "English", "ja": "Japanese",
        "zh": "Chinese", "es": "Spanish", "fr": "French",
        "de": "German", "it": "Italian", "pt": "Portuguese",
        "ru": "Russian", "ar": "Arabic", "hi": "Hindi",
    }
    target_name = lang_names.get(target_lang, "Korean")

    prompt = f"Translate to {target_name}. Output ONLY the translation, nothing else:\n\n{text}"

    try:
        r = subprocess.run(
            ["ollama", "run", "gemma3:4b", prompt],
            capture_output=True, text=True, timeout=15, env=_env())
        result = r.stdout.strip()
        # 결과가 있고, 원문보다 짧지 않으면 번역 성공
        if result and len(result) > 5:
            return result
    except Exception:
        pass

    return text  # 번역 실패 시 원문 반환


def make_news_brief(max_items=3, lang=None):
    """Create a brief news summary for DJ to read.

    Args:
        max_items: Maximum news items
        lang: Output language (default: from settings)

    Returns:
        String with news brief for TTS
    """
    lang = lang or get_news_output_lang()

    # 캐시 확인, 없으면 새로 가져오기
    news = get_cached_news()
    if not news:
        news = fetch_top_news(limit=3)

    if not news:
        return None

    # 뉴스 브리핑 생성
    items = []
    for source, headlines in news.items():
        if headlines:
            items.append(headlines[0])
        if len(items) >= max_items:
            break

    if not items:
        return None

    # 언어별 인트로/아웃트로
    intros = {
        "ko": "이 시각 주요 뉴스입니다.",
        "en": "Here are the top news stories.",
        "ja": "主要ニュースをお伝えします。",
        "zh": "以下是头条新闻。",
        "es": "Estas son las noticias principales.",
        "fr": "Voici les principales actualités.",
    }
    outros = {
        "ko": "자세한 내용은 뉴스에서 확인하세요.",
        "en": "For more details, check the news.",
        "ja": "詳しくはニュースをご覧ください。",
        "zh": "详情请关注新闻。",
        "es": "Para más detalles, consulte las noticias.",
        "fr": "Pour plus de détails, consultez les actualités.",
    }

    brief = intros.get(lang, intros["ko"]) + " "

    for item in items:
        title = item[:60] + "..." if len(item) > 60 else item
        # 필요시 번역
        if lang != "en" and any(ord(c) < 128 for c in title[:10]):
            # 영어로 보이면 번역
            title = translate_text(title, lang)
        brief += f"{title}. "

    brief += outros.get(lang, outros["ko"])
    return brief


def _write_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _read_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"status": "idle"}

MPV = "/opt/homebrew/bin/mpv"
YTDLP = "/Users/dragon/.pyenv/shims/yt-dlp"
if not os.path.exists(MPV):
    MPV = shutil.which("mpv") or "mpv"
if not os.path.exists(YTDLP):
    YTDLP = shutil.which("yt-dlp") or "yt-dlp"


def _env():
    env = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
    return env


# ---- Noise filtering ----
NOISE_TERMS = [
    "live", "concert", "cover", "karaoke", "remix", "sped up",
    "slowed", "reverb", "8d", "nightcore", "lyrics video", "reaction",
    "fan made", "amv", "loop", "1 hour", "10 hours", "mashup",
]
GOOD_TERMS = ["official audio", "official video", "audio", "mv"]


def build_clean_query(title, prefer_audio=True):
    q = title.strip()
    if prefer_audio and not any(g in q.lower() for g in GOOD_TERMS):
        q = f"{q} official audio"
    return q


# ---- Provider detection ----
def _app_installed(app_name):
    for base in ("/Applications", "/System/Applications", os.path.expanduser("~/Applications")):
        if os.path.exists(os.path.join(base, app_name)):
            return True
    return False


def available_providers():
    provs = []
    if _app_installed("Spotify.app"):
        provs.append("spotify")
    if _app_installed("Music.app"):
        provs.append("apple_music")
    provs.append("youtube")  # always available
    return provs


def _osascript(script):
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0, (r.stdout or r.stderr).strip()
    except Exception as e:
        return False, str(e)


# ---- Provider: Spotify (AppleScript) ----
def play_spotify(query):
    ok, out = _osascript(
        'tell application "Spotify" to play track '
        '(get first item of (search "%s"))' % query.replace('"', ""))
    if not ok:
        # fallback: just search & play via URI is complex; use simple play
        ok, out = _osascript('tell application "Spotify" to play')
    return ok


# ---- Provider: Apple Music (native AppleScript, no GUI clicking) ----
def _music_player_state():
    """Return Music.app player state ('playing'/'paused'/'stopped', or '')."""
    ok, state = _osascript('tell application "Music" to player state')
    return (state or "").strip() if ok else ""


def _music_current_pid():
    """Persistent ID of Music.app's current track ('' if none/unknown)."""
    ok, out = _osascript(
        'tell application "Music" to get persistent ID of current track')
    return (out or "").strip() if ok else ""


def _music_pause():
    """Pause Music.app (prevents auto-advance into unrelated library tracks)."""
    _osascript('tell application "Music" to pause')


def _split_artist_title(query):
    """Split 'Artist - Title' into (artist, title). Falls back to ('', query)."""
    if " - " in query:
        artist, title = query.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", query.strip()


def _as_str(s):
    """Escape a string for safe embedding in an AppleScript double-quoted literal."""
    return (s or "").replace("\\", "").replace('"', "")


def resolve_library_track(query):
    """Find a track in the user's Apple Music LIBRARY matching `query`.

    Returns dict {pid, name, artist, duration} or None if not found.
    Match priority: title+artist, then title only.
    """
    artist, title = _split_artist_title(query)
    title_s, artist_s = _as_str(title), _as_str(artist)
    if not title_s:
        return None

    conds = []
    if artist_s:
        conds.append(f'name contains "{title_s}" and artist contains "{artist_s}"')
    conds.append(f'name contains "{title_s}"')

    for cond in conds:
        script = f'''
tell application "Music"
    set theTracks to (every track of library playlist 1 whose {cond})
    if (count of theTracks) is 0 then return "NOTFOUND"
    set t to item 1 of theTracks
    return (get persistent ID of t) & "|~|" & (get name of t) & "|~|" & (get artist of t) & "|~|" & (get duration of t)
end tell
'''
        ok, out = _osascript(script)
        if ok and out and out != "NOTFOUND" and "|~|" in out:
            pid, name, art, dur = (out.split("|~|") + ["", "", "", "0"])[:4]
            try:
                duration = float(dur)
            except (TypeError, ValueError):
                duration = 0.0
            return {"pid": pid, "name": name, "artist": art, "duration": duration}
    return None


def play_library_track_by_id(pid):
    """Play a library track by its persistent ID. Returns True only if playing."""
    pid_s = _as_str(pid)
    script = f'''
tell application "Music"
    activate
    set song repeat to off
    set theTracks to (every track of library playlist 1 whose persistent ID is "{pid_s}")
    if (count of theTracks) is 0 then return "NOTFOUND"
    play (item 1 of theTracks)
    delay 0.4
    return (player state as text)
end tell
'''
    ok, out = _osascript(script)
    return ok and "playing" in (out or "")


def play_apple_music_library(query):
    """Play a single track from the library by name (native, no GUI). Returns bool."""
    track = resolve_library_track(query)
    if not track:
        return False
    return play_library_track_by_id(track["pid"])


def play_apple_music(query):
    """Play a song via Apple Music.

    Strategy (most reliable first):
      1. Native library playback by persistent ID — no search, no click.
      2. GUI fallback: Cmd+F search then play first result (catalog/streaming);
         requires Accessibility permission. Verified via player state.
    Returns True only if Music.app actually reports 'playing'.
    """
    # 1) Native library playback first — bulletproof, no clicking.
    if play_apple_music_library(query):
        return True

    # 2) GUI fallback for catalog search (needs Accessibility permission).
    try:
        q = _as_str(query)
        script = f'''
tell application "Music" to activate
delay 0.6
tell application "System Events"
    keystroke "f" using command down
    delay 0.4
    keystroke "a" using command down
    key code 51
    delay 0.2
    keystroke "{q}"
    delay 1.3
    key code 36
    delay 1.5
end tell
'''
        subprocess.run(["osascript", "-e", script],
                       capture_output=True, timeout=12, env=_env())
        time.sleep(0.6)
        if _music_player_state() != "playing":
            # Nudge: move to first result and press return to play.
            nudge = '''
tell application "System Events"
    key code 48
    delay 0.3
    key code 36
    delay 1
end tell
'''
            subprocess.run(["osascript", "-e", nudge],
                           capture_output=True, timeout=10, env=_env())
            time.sleep(0.8)
        return _music_player_state() == "playing"
    except Exception:
        return False


def stop_apple_music():
    """Stop Apple Music playback."""
    _osascript('tell application "Music" to pause')
    return True


def play_via_siri(query):
    """Play music by sending command to Siri (requires Type to Siri enabled)."""
    command = f"{query} 틀어줘"
    script = f'''
tell application "System Events"
    -- Siri 호출 (Control+Space 또는 시스템 설정에 따라 다름)
    key code 49 using {{control down}}
    delay 1.5

    -- 명령 입력
    keystroke "{command}"
    delay 0.3

    -- Enter로 실행
    key code 36
end tell
'''
    try:
        subprocess.run(["osascript", "-e", script],
                      capture_output=True, timeout=10, env=_env())
        return {"status": "sent", "command": command}
    except Exception as e:
        return {"error": str(e)}


def play_via_shortcut(query, shortcut_name="Play Music"):
    """Play music via macOS Shortcut."""
    try:
        result = subprocess.run(
            ["shortcuts", "run", shortcut_name],
            input=query.encode(),
            capture_output=True, timeout=15)
        time.sleep(2)
        ok, state = _osascript('tell application "Music" to player state')
        return {"status": "ok" if state == "playing" else "unknown",
                "shortcut": shortcut_name, "query": query}
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# Song/Artist Information Search
# ============================================================
def search_song_info(query, lang="ko"):
    """Search for song/artist information via web search, YouTube, Wikipedia.

    Args:
        query: Song or artist name to search
        lang: Language for results (ko, en, ja)

    Returns:
        Dict with artist info, song info, related songs, web search results
    """
    import urllib.request
    import urllib.parse

    result = {
        "query": query,
        "artist": None,
        "songs": [],
        "description": None,
        "youtube_videos": [],
        "web_results": [],
    }

    # 1. DuckDuckGo 웹검색 (가수/곡 정보)
    try:
        search_query = f"{query} 가수 프로필" if lang == "ko" else f"{query} singer profile"
        ddg_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(search_query)}"
        req = urllib.request.Request(ddg_url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
            # 간단한 결과 추출 (정규식으로 snippet 찾기)
            import re
            snippets = re.findall(r'class="result__snippet"[^>]*>([^<]+)<', html)
            for snippet in snippets[:3]:
                clean = snippet.strip()
                if clean and len(clean) > 20:
                    result["web_results"].append(clean)
    except Exception:
        pass

    # 2. YouTube에서 관련 영상 검색
    try:
        cmd = [YTDLP, f"ytsearch5:{query}",
               "--flat-playlist",
               "--print", "%(title)s|||%(channel)s|||%(view_count)s",
               "--no-warnings"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=_env())
        for line in r.stdout.strip().split('\n'):
            if '|||' in line:
                parts = line.split('|||')
                if len(parts) >= 2:
                    result["youtube_videos"].append({
                        "title": parts[0],
                        "channel": parts[1],
                        "views": parts[2] if len(parts) > 2 else "0"
                    })
    except Exception:
        pass

    # 3. Wikipedia에서 정보 검색
    try:
        wiki_lang = {"ko": "ko", "en": "en", "ja": "ja"}.get(lang, "ko")
        search_url = f"https://{wiki_lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(query)}"
        req = urllib.request.Request(search_url, headers={"User-Agent": "radiomcp/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            result["description"] = data.get("extract", "")[:500]
            result["artist"] = data.get("title", query)
    except Exception:
        pass

    # 4. 웹 결과에서 설명 보충
    if not result["description"] and result["web_results"]:
        result["description"] = " ".join(result["web_results"][:2])

    # 5. 가수 이름 추출 시도
    if not result["artist"]:
        if " - " in query:
            result["artist"] = query.split(" - ")[0].strip()
        else:
            result["artist"] = query

    return result


def generate_rich_comment(song, song_info=None, idx=0, total=1, lang="ko"):
    """Generate a rich DJ comment with song information.

    Args:
        song: Song query string
        song_info: Result from search_song_info() or None
        idx: Song index in playlist
        total: Total songs in playlist
        lang: Language code

    Returns:
        DJ comment string with song information
    """
    name = _format_song(song)

    # 기본 멘트
    if idx == 0:
        base = f"첫 곡은 {name}입니다."
    elif idx == total - 1:
        base = f"마지막 곡, {name}입니다."
    else:
        base = f"다음곡은 {name}입니다."

    # song_info가 있으면 설명 추가
    if song_info:
        # 힌트 사용 (소형 모델용)
        if song_info.get("hint"):
            base += f" {song_info['hint']}"
        elif song_info.get("description"):
            desc = song_info["description"]
            first_sentence = desc.split('.')[0] + '.'
            if len(first_sentence) < 100:
                base += f" {first_sentence}"

    return base


def search_song_info_for_local(query, lang="ko"):
    """Search song info with pre-formatted hints for local/small models.

    Returns structured data that small models can directly use,
    without needing to synthesize from raw search results.

    Args:
        query: Song or artist name
        lang: Language code

    Returns:
        Dict with:
        - artist: 가수명
        - song: 곡명 (추출된)
        - hint: 바로 사용 가능한 한 줄 설명
        - facts: 짧은 팩트 리스트
        - related_shows: 관련 프로그램 (한일가왕전 등)
        - youtube_titles: 검색된 YouTube 제목들
    """
    info = search_song_info(query, lang)

    # 아티스트/곡 분리
    artist = info.get("artist", "")
    song_title = ""
    if " - " in query:
        parts = query.split(" - ", 1)
        artist = parts[0].strip()
        song_title = parts[1].strip()
    elif " " in query and info.get("youtube_videos"):
        # YouTube 제목에서 추출 시도
        first_title = info["youtube_videos"][0].get("title", "")
        if " - " in first_title:
            artist = first_title.split(" - ")[0].strip()

    # YouTube 제목에서 관련 프로그램 추출
    related_shows = []
    yt_titles = [v.get("title", "") for v in info.get("youtube_videos", [])]
    for title in yt_titles:
        if "한일가왕전" in title:
            related_shows.append("한일가왕전")
        if "한일톱텐쇼" in title:
            related_shows.append("한일톱텐쇼")
        if "나가수" in title or "나는 가수다" in title:
            related_shows.append("나는 가수다")
        if "복면가왕" in title:
            related_shows.append("복면가왕")
    related_shows = list(set(related_shows))

    # 힌트 생성 (소형 모델이 바로 사용 가능)
    hint = ""
    if related_shows:
        hint = f"{artist}는 {', '.join(related_shows)}에 출연한 가수입니다."
    elif info.get("description"):
        hint = info["description"].split('.')[0] + '.'

    # 팩트 리스트
    facts = []
    if related_shows:
        facts.append(f"출연: {', '.join(related_shows)}")
    if info.get("youtube_videos"):
        views = info["youtube_videos"][0].get("views", "")
        if views and views.isdigit() and int(views) > 100000:
            facts.append(f"인기 영상 조회수: {int(views):,}회")

    return {
        "query": query,
        "artist": artist,
        "song": song_title,
        "hint": hint,
        "facts": facts,
        "related_shows": related_shows,
        "youtube_titles": yt_titles[:5],
        "description": info.get("description", ""),
    }


# ---- Provider: YouTube (yt-dlp + mpv) ----
def _get_youtube_url(query, retries=2):
    """Prefetch YouTube URL without playing (for prefetch).

    Args:
        query: Search query
        retries: Number of retry attempts on failure

    Returns:
        URL string or None
    """
    q = build_clean_query(query)
    exclude = " ".join(f"-{t}" for t in ["live", "cover", "remix", "8d", "nightcore"])
    search = f"ytsearch1:{q} {exclude}"

    for attempt in range(retries + 1):
        try:
            r = subprocess.run(
                [YTDLP, "-f", "bestaudio/best", "--no-playlist", "--no-warnings",
                 "-g", search],
                capture_output=True, text=True, timeout=30, env=_env())
            url = r.stdout.strip()
            if url and url.startswith("http"):
                return url
            # 실패 시 잠시 대기 후 재시도
            if attempt < retries:
                time.sleep(1)
        except subprocess.TimeoutExpired:
            if attempt < retries:
                time.sleep(1)
        except Exception:
            if attempt < retries:
                time.sleep(0.5)

    return None


def _wait_process_interruptible(proc, stop_event, poll_interval=0.5, max_duration=300):
    """Wait for process with periodic stop check and health monitoring."""
    elapsed = 0
    last_check = 0
    stall_count = 0

    while proc.poll() is None:
        # 1. 중단 요청 체크
        if stop_event and stop_event.is_set():
            _kill_process_tree(proc)
            return False  # Interrupted

        time.sleep(poll_interval)
        elapsed += poll_interval

        # 2. 최대 시간 초과
        if elapsed > max_duration:
            _kill_process_tree(proc)
            return False

        # 3. 10초마다 프로세스 상태 체크
        if elapsed - last_check > 10:
            last_check = elapsed
            if not _is_mpv_playing():
                stall_count += 1
                if stall_count >= 2:
                    _kill_process_tree(proc)
                    return False  # Stalled
            else:
                stall_count = 0

    return proc.returncode == 0


def _kill_process_tree(proc):
    """Kill process and all children."""
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
    except Exception:
        pass


def _is_mpv_playing():
    """Check if mpv is currently playing audio."""
    try:
        r = subprocess.run(["pgrep", "-f", "mpv.*--no-video"],
                          capture_output=True, timeout=2)
        return r.returncode == 0
    except Exception:
        return False


def play_youtube_url(url, wait=False, stop_event=None):
    """Play a pre-fetched YouTube URL directly."""
    if not url:
        return False

    cmd = [MPV, "--no-video", "--really-quiet", url]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=_env(),
                             start_new_session=True)

        # 재생 시작 확인 (0.5초 후에도 프로세스가 살아있으면 성공)
        time.sleep(0.5)
        if p.poll() is not None:
            # 프로세스가 바로 죽음 = 실패
            return False

        if wait:
            return _wait_process_interruptible(p, stop_event)
        return True
    except Exception:
        return False


def play_youtube_track(query, wait=False, stop_event=None):
    """Play YouTube track - URL-first approach (more stable than pipe)."""
    # 1. URL 먼저 가져오기 (파이프보다 안정적)
    url = _get_youtube_url(query)

    if url:
        # 2. URL로 직접 재생
        return play_youtube_url(url, wait=wait, stop_event=stop_event)

    # 3. URL 실패 시 파이프 폴백
    q = build_clean_query(query)
    exclude = " ".join(f"-{t}" for t in ["live", "cover", "remix", "8d", "nightcore"])
    search = f"ytsearch1:{q} {exclude}"
    cmd = (f'{YTDLP} -f "bestaudio/best" --no-playlist --no-warnings '
           f'-o - "{search}" 2>/dev/null | {MPV} --no-video --really-quiet -')
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=_env(),
                         start_new_session=True)
    if wait:
        return _wait_process_interruptible(p, stop_event)
    return True
    return True


# ---- Unified play with provider fallback ----
def play_track(query, provider=None, wait=False):
    order = [provider] if provider else available_providers()
    for prov in order:
        try:
            if prov == "spotify" and _app_installed("Spotify.app"):
                if play_spotify(query):
                    return {"provider": "spotify", "query": query}
            elif prov == "apple_music" and _app_installed("Music.app"):
                if play_apple_music(query):
                    return {"provider": "apple_music", "query": query}
            elif prov == "youtube":
                play_youtube_track(query, wait=wait)
                return {"provider": "youtube", "query": query}
        except Exception:
            continue
    # final fallback
    play_youtube_track(query, wait=wait)
    return {"provider": "youtube", "query": query}


def stop_all():
    subprocess.run(["pkill", "-f", "mpv"], capture_output=True)
    if _app_installed("Spotify.app"):
        _osascript('tell application "Spotify" to pause')
    if _app_installed("Music.app"):
        _osascript('tell application "Music" to pause')


# ============================================================
# Video playback (windowed, on the Mac's logged-in desktop)
# ============================================================
_video_stop = threading.Event()
_video_thread = None


def stop_video():
    """Stop the windowed video player and its auto-refresh supervisor."""
    _video_stop.set()
    subprocess.run(["pkill", "-f", "mpv --no-terminal"], capture_output=True)
    return {"status": "stopped"}


def _resolve_stream_url(target, video=True):
    """Resolve a directly-playable stream URL via yt-dlp.

    target may be a YouTube watch URL, a /live channel URL, or a plain search
    query. Returns a URL string, or '' on failure.
    """
    t = (target or "").strip()
    if not (t.startswith("http://") or t.startswith("https://")):
        t = "ytsearch1:" + t
    fmt = "b/best" if video else "bestaudio/best"
    try:
        r = subprocess.run([YTDLP, "-f", fmt, "-g", t],
                           capture_output=True, text=True, env=_env(), timeout=45)
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("http"):
                return line
    except Exception:
        pass
    return ""


def _mpv_window_running():
    try:
        r = subprocess.run(["pgrep", "-f", "mpv --no-terminal"],
                           capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def _launch_video_once(url, fullscreen, ontop, geometry):
    """Resolve-free launch of mpv in the GUI session for a ready stream URL."""
    try:
        uid = str(os.getuid())
    except Exception:
        uid = "501"
    args = ["launchctl", "asuser", uid, MPV,
            "--no-terminal", "--force-window=yes"]
    if ontop:
        args.append("--ontop")
    if fullscreen:
        args.append("--fullscreen")
    elif geometry:
        args.append(f"--geometry={geometry}")
    args.append(url)
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=_env(),
                         start_new_session=True)
        return True
    except Exception:
        return False


def _video_supervisor(source, fullscreen, ontop, geometry, keep_alive):
    """Keep video playing: (re)resolve the stream URL and (re)launch mpv.

    For live streams the resolved URL expires after a while and mpv exits;
    when keep_alive is on we re-resolve and relaunch so playback never dies
    until stop_video() is called. For finite videos (keep_alive off) we play
    once and finish.
    """
    while not _video_stop.is_set():
        url = _resolve_stream_url(source, video=True)
        if not url:
            _log_error(f"Video resolve failed: {source[:50]}")
            if not keep_alive:
                break
            if _video_stop.wait(8):
                break
            continue

        if _video_stop.is_set():
            break
        subprocess.run(["pkill", "-f", "mpv --no-terminal"], capture_output=True)
        _launch_video_once(url, fullscreen, ontop, geometry)
        _log_info(f"Video playing: {source[:50]} (keep_alive={keep_alive})")

        # Wait for mpv to appear, then watch until it exits.
        for _ in range(12):
            if _video_stop.is_set() or _mpv_window_running():
                break
            time.sleep(1)
        while _mpv_window_running():
            if _video_stop.is_set():
                break
            time.sleep(2)

        if _video_stop.is_set() or not keep_alive:
            break
        _log_info("Video stream ended — auto-refreshing...")
        if _video_stop.wait(2):
            break

    _write_state({"status": "idle"})
    _log_info("Video supervisor ended")


def play_video(source, fullscreen=False, ontop=True,
               geometry="820x480+140+140", keep_alive=None):
    """Play a video in a window on the Mac's logged-in desktop.

    source: a YouTube watch URL, a /live channel URL, or a search query.

    Resolves a direct stream URL with yt-dlp, then launches mpv INSIDE the GUI
    (Aqua) session via `launchctl asuser` so a real window appears with sound.
    A supervisor thread auto-refreshes the stream so live channels keep playing
    even after the resolved URL expires.

    keep_alive: None = auto (on for '/live' URLs), True/False to force.
    """
    global _video_thread
    src = (source or "").strip()
    if not src:
        return {"status": "error", "error": "empty source"}

    if keep_alive is None:
        keep_alive = "/live" in src.lower()

    # Stop any current video + supervisor, then start fresh.
    stop_video()
    if _video_thread and _video_thread.is_alive():
        _video_thread.join(timeout=3)
    _video_stop.clear()

    _video_thread = threading.Thread(
        target=_video_supervisor,
        args=(src, fullscreen, ontop, geometry, keep_alive),
        daemon=True)
    _video_thread.start()

    _write_state({"status": "video", "source": src,
                  "fullscreen": fullscreen, "keep_alive": keep_alive,
                  "started": datetime.now().isoformat()})
    return {"status": "started", "mode": "video", "source": src,
            "fullscreen": fullscreen, "keep_alive": keep_alive}


# ---- DJ voice comment (edge-tts or macOS say) ----
DJ_VOICE = "ko-KR-SunHiNeural"

# macOS voice mapping (for say command)
MACOS_VOICES = {
    "ko": "Yuna",
    "en": "Samantha",
    "ja": "Kyoko",
    "zh": "Tingting",
}


def speak_dj(text, voice=DJ_VOICE, wait=True, use_say=False):
    """Generate TTS and play.

    Args:
        text: Text to speak
        voice: edge-tts voice name or language code for macOS say
        wait: Wait for speech to finish
        use_say: Use macOS say command (faster, offline) instead of edge-tts
    """
    # Option 1: macOS say (fast, offline)
    if use_say:
        lang = voice[:2] if "-" in voice else voice
        macos_voice = MACOS_VOICES.get(lang, "Samantha")
        cmd = ["say", "-v", macos_voice, text]
        if wait:
            subprocess.run(cmd, capture_output=True)
        else:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True

    # Option 2: edge-tts (better quality, requires network)
    out = os.path.join(DATA_DIR, "dj_tmp.mp3")
    try:
        subprocess.run(
            ["edge-tts", "--voice", voice, "--text", text, "--write-media", out],
            capture_output=True, env=_env(), timeout=30)
    except Exception:
        try:
            import asyncio, edge_tts
            async def _gen():
                await edge_tts.Communicate(text, voice).save(out)
            asyncio.run(_gen())
        except Exception:
            # Fallback to macOS say
            lang = voice[:2] if "-" in voice else "en"
            macos_voice = MACOS_VOICES.get(lang, "Samantha")
            if wait:
                subprocess.run(["say", "-v", macos_voice, text], capture_output=True)
            else:
                subprocess.Popen(["say", "-v", macos_voice, text],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    if not os.path.exists(out):
        return False
    if wait:
        subprocess.run([MPV, "--no-video", "--really-quiet", out],
                       capture_output=True, env=_env())
    else:
        subprocess.Popen([MPV, "--no-video", "--really-quiet", out],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=_env())
    return True


def _format_song(song):
    """Parse 'artist - title' into a natural Korean phrase: '아티스트의 타이틀'."""
    s = str(song).strip()
    # strip common noise tokens
    for tok in ["official audio", "official video", "mv", "m/v", "lyrics",
                "audio", "가사", "(오디오)", "[오디오]"]:
        s = s.replace(tok, "").replace(tok.upper(), "").replace(tok.title(), "")
    s = s.strip(" -·|")
    sep = None
    for d in [" - ", " – ", " — ", " / "]:
        if d in s:
            sep = d
            break
    if sep:
        artist, title = s.split(sep, 1)
        artist = artist.strip()
        title = title.strip(" \"'()[]")
        if artist and title:
            return f"{artist}의 {title}"
    return s


# ---- Language configuration ----
DJ_LANG_FILE = os.path.join(DATA_DIR, "dj_lang.json")

DJ_LANGS = {
    "ko": {
        "name": "한국어",
        "voice": "ko-KR-SunHiNeural",
        "prompt_role": "한국 라디오 DJ",
        "prompt_lang": "한국어로",
    },
    "en": {
        "name": "English",
        "voice": "en-US-JennyNeural",
        "prompt_role": "radio DJ",
        "prompt_lang": "in English",
    },
    "ja": {
        "name": "日本語",
        "voice": "ja-JP-NanamiNeural",
        "prompt_role": "日本のラジオDJ",
        "prompt_lang": "日本語で",
    },
    "zh": {
        "name": "中文",
        "voice": "zh-CN-XiaoxiaoNeural",
        "prompt_role": "中国电台DJ",
        "prompt_lang": "用中文",
    },
    "es": {
        "name": "Español",
        "voice": "es-ES-ElviraNeural",
        "prompt_role": "DJ de radio",
        "prompt_lang": "en español",
    },
    "fr": {
        "name": "Français",
        "voice": "fr-FR-DeniseNeural",
        "prompt_role": "DJ radio",
        "prompt_lang": "en français",
    },
}


def get_dj_lang():
    """Get current DJ language setting."""
    try:
        if os.path.exists(DJ_LANG_FILE):
            with open(DJ_LANG_FILE, "r") as f:
                data = json.load(f)
                return data.get("lang", "ko")
    except Exception:
        pass
    return "ko"


def set_dj_lang(lang):
    """Set DJ language."""
    if lang not in DJ_LANGS:
        return {"error": f"Unknown language: {lang}", "available": list(DJ_LANGS.keys())}
    with open(DJ_LANG_FILE, "w") as f:
        json.dump({"lang": lang}, f)
    return {"lang": lang, "name": DJ_LANGS[lang]["name"], "voice": DJ_LANGS[lang]["voice"]}


def _generate_comment_llm(song, idx, total, slot_name=None, news=None, lang=None):
    """Use local LLM to generate DJ comment in specified language."""
    lang = lang or get_dj_lang()
    lang_cfg = DJ_LANGS.get(lang, DJ_LANGS["ko"])

    name = _format_song(song)
    now = datetime.now()
    hour = now.hour

    # Position context
    if idx == 0:
        position_hint = "first song, welcome listeners"
        sentences = "2-3 sentences"
    elif idx == total - 1:
        position_hint = "last song, say goodbye"
        sentences = "2 sentences"
    else:
        position_hint = f"song #{idx+1}"
        sentences = "1 sentence only"

    prompt = f"""You are a {lang_cfg['prompt_role']}.
Introduce the song "{name}" ({position_hint}).
{f'Show: {slot_name}' if slot_name else ''}
{f'Recent news to mention: {news}' if news else ''}
Time: {hour}:00
Write {sentences} {lang_cfg['prompt_lang']}.
Warm, friendly tone. Output ONLY the DJ comment, nothing else."""

    try:
        r = subprocess.run(
            ["ollama", "run", "gemma3:4b", prompt],
            capture_output=True, text=True, timeout=15, env=_env())
        comment = r.stdout.strip()
        comment = re.sub(r'^["\']|["\']$', '', comment)
        comment = re.sub(r'\*+', '', comment)
        comment = comment.strip()
        if comment and len(comment) > 5:
            return comment
    except Exception:
        pass
    return None


def make_dj_comment(song, idx, total, slot_name=None, news=None, use_llm=True, lang=None):
    """Generate DJ comment - LLM first, fallback to simple template."""
    lang = lang or get_dj_lang()

    if use_llm:
        comment = _generate_comment_llm(song, idx, total, slot_name, news, lang)
        if comment:
            return comment

    # Minimal fallback (LLM should handle i18n)
    name = _format_song(song)
    if idx == 0:
        return f"{name}."
    return f"{name}."


# ---- DJ set: comment -> song -> comment -> song ... ----
_dj_thread = None
_dj_stop = threading.Event()
_prefetch_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dj_prefetch")


def _dj_set_worker(songs, provider, slot_name, with_comments, comments=None, lang=None, news_interval=0):
    """Play DJ set with prefetching, logging, auto-retry, news reading.

    Args:
        songs: List of song names/queries
        provider: Music provider (youtube, spotify, apple_music)
        slot_name: Optional show/slot name
        with_comments: Whether to include DJ comments
        comments: Optional list of pre-generated comments (from Claude/Codex)
        lang: Language for TTS voice
    """
    total = len(songs)
    prefetch_future: Future | None = None
    lang = lang or get_dj_lang()
    lang_cfg = DJ_LANGS.get(lang, DJ_LANGS["ko"])
    voice = lang_cfg["voice"]

    _log_info(f"DJ set started: {total} songs, provider={provider}, slot={slot_name}")

    # Prefetch first song immediately
    use_youtube = provider in (None, "youtube")
    if use_youtube and total > 0:
        prefetch_future = _prefetch_executor.submit(_get_youtube_url, songs[0])

    # Prepare (queue) Apple Music library tracks up front so playback is instant.
    am_tracks = {}
    if provider == "apple_music":
        _log_info("Preparing Apple Music library queue...")
        found = 0
        for s in songs:
            if _dj_stop.is_set():
                break
            t = resolve_library_track(s)
            am_tracks[s] = t
            if t:
                found += 1
                _log_info(f"  queued: {t['name']} - {t['artist']}")
            else:
                _log_info(f"  not in library (YouTube fallback): {s[:40]}")
        _log_info(f"Apple Music queue ready: {found}/{total} in library")

    for i, song in enumerate(songs):
        if _dj_stop.is_set():
            _log_info("DJ set stopped by user")
            break

        _log_info(f"Playing song {i+1}/{total}: {song[:50]}")

        # Get prefetched URL for current song (or wait for it)
        current_url = None
        if use_youtube and prefetch_future:
            try:
                current_url = prefetch_future.result(timeout=45)
            except Exception as e:
                _log_error(f"Prefetch failed: {e}")
                current_url = None
            prefetch_future = None

        # Start prefetching NEXT song while doing DJ comment
        if use_youtube and i + 1 < total:
            prefetch_future = _prefetch_executor.submit(_get_youtube_url, songs[i + 1])

        # DJ comment
        if with_comments:
            if comments and i < len(comments) and comments[i]:
                comment = comments[i]
            else:
                comment = make_dj_comment(song, i, total, slot_name, lang=lang)

            if speak_dj(comment, voice=voice, wait=True):
                _update_stats("comments_spoken")
            else:
                _log_error(f"TTS failed for comment: {comment[:30]}")

        if _dj_stop.is_set():
            break

        # Play the song with retry logic
        play_success = False
        for attempt in range(3):  # 3번 재시도
            if _dj_stop.is_set():
                break

            if use_youtube:
                # URL이 없으면 다시 가져오기
                if not current_url:
                    _log_info(f"Fetching URL (attempt {attempt+1})")
                    current_url = _get_youtube_url(song)

                if current_url:
                    play_success = play_youtube_url(current_url, wait=True, stop_event=_dj_stop)
                    if play_success:
                        _update_stats("songs_played")
                        break
                    else:
                        _log_error(f"Playback failed (attempt {attempt+1})")
                        current_url = None  # 다음 시도에서 새 URL
                        time.sleep(1)
                else:
                    _log_error(f"URL fetch failed (attempt {attempt+1})")
                    time.sleep(1)
            else:
                # ---- App-based providers (apple_music / spotify) ----
                started = False
                duration = 0
                actual = provider
                track = None

                if provider == "apple_music":
                    track = am_tracks.get(song) if am_tracks else resolve_library_track(song)
                    if track:
                        started = play_library_track_by_id(track["pid"])
                        duration = track.get("duration") or 0
                    if started:
                        actual = "apple_music"
                    else:
                        # Not in library or playback failed → YouTube fallback.
                        # Pause Music first so it can't auto-advance into other
                        # library tracks (e.g. 안전지대) while YouTube plays.
                        _music_pause()
                        _log_info(f"Apple Music unavailable, YouTube fallback: {song[:40]}")
                        url = _get_youtube_url(song)
                        if url and play_youtube_url(url, wait=True, stop_event=_dj_stop):
                            play_success = True
                            _update_stats("songs_played")
                            break
                        _log_error(f"Playback failed (attempt {attempt+1})")
                        time.sleep(1)
                        continue

                elif provider == "spotify":
                    result = play_track(song, provider="spotify", wait=False)
                    actual = result.get("provider")
                    ok, st = _osascript('tell application "Spotify" to player state')
                    started = ok and "playing" in (st or "")
                    if not started:
                        _log_error(f"Playback failed (attempt {attempt+1})")
                        time.sleep(1)
                        continue

                else:
                    result = play_track(song, provider=provider, wait=False)
                    actual = result.get("provider")
                    started = True

                play_success = True
                _update_stats("songs_played")

                # Wait for the track to finish (user stop, natural end, or timeout).
                started_pid = track["pid"] if (actual == "apple_music" and track) else None
                if actual == "apple_music" and duration and duration > 0:
                    max_wait = int(duration) + 8
                else:
                    max_wait = 210 if actual != "youtube" else 300
                for _ in range(max_wait):
                    if _dj_stop.is_set():
                        stop_all()
                        break
                    if actual == "apple_music":
                        if _music_player_state() != "playing":
                            break
                        # Music auto-advanced to a different library track → stop the leak
                        if started_pid and _music_current_pid() != started_pid:
                            break
                    time.sleep(1)
                # Keep Music from rolling into unrelated library tracks between songs
                if actual == "apple_music":
                    _music_pause()
                break

        if not play_success and not _dj_stop.is_set():
            _log_error(f"Skipping song after 3 failed attempts: {song[:50]}")
            _update_stats("errors")

        # 뉴스 읽기 (news_interval마다)
        if news_interval > 0 and (i + 1) % news_interval == 0 and i < total - 1:
            if not _dj_stop.is_set():
                _log_info("Reading news brief")
                news_brief = make_news_brief(max_items=3)
                if news_brief:
                    speak_dj(news_brief, voice=voice, wait=True)
                    _update_stats("news_read")

    _write_state({"status": "idle"})
    _log_info("DJ set ended")


def _watch_global_stop(started_at, poll=0.5):
    """Halt this instance's DJ set if a newer global stop is signaled.

    Runs in a daemon thread for the lifetime of one set. If any instance
    signals a stop (or a later start preempts us) after `started_at`, we set the
    local stop event and kill audio so nothing keeps respawning.
    """
    while not _dj_stop.is_set():
        try:
            if _global_stop_token() > started_at:
                _dj_stop.set()
                stop_all()
                subprocess.run(["pkill", "-f", "afplay"], capture_output=True)
                break
        except Exception:
            pass
        time.sleep(poll)


def start_dj_set(songs, provider=None, slot_name=None, with_comments=True,
                 comments=None, lang=None, news_interval=0):
    """Start DJ set playback.

    Args:
        songs: List of song names/queries
        provider: Music provider (youtube, spotify, apple_music, or None for auto)
        slot_name: Optional show name
        with_comments: Include DJ voice comments
        comments: Pre-generated comments list (from Claude/Codex/AI)
        lang: Language code (ko, en, ja, zh, es, fr)
        news_interval: Read news every N songs (0=disabled, 3=every 3 songs)
    """
    global _dj_thread
    stop_dj_set()              # preempt any set on this OR other instances
    _dj_stop.clear()
    started_at = time.time()   # strictly newer than the stop token just written
    # Watcher: if a newer global stop appears (another instance stopped, or a
    # later start preempts us), halt this set. Keeps playback singleton.
    threading.Thread(target=_watch_global_stop, args=(started_at,),
                     daemon=True).start()
    _dj_thread = threading.Thread(
        target=_dj_set_worker,
        args=(songs, provider, slot_name, with_comments, comments, lang, news_interval),
        daemon=True)
    _dj_thread.start()
    _write_state({"status": "playing", "songs": songs,
                  "provider": provider or "auto", "slot": slot_name,
                  "news_interval": news_interval,
                  "started": datetime.now().isoformat()})
    return {"status": "started", "count": len(songs),
            "provider": provider or "auto", "comments": with_comments,
            "news_interval": news_interval,
            "lang": lang or get_dj_lang()}


def stop_dj_set():
    _signal_global_stop()   # tell every instance to stop, not just this one
    _dj_stop.set()
    stop_all()
    subprocess.run(["pkill", "-f", "afplay"], capture_output=True)
    _write_state({"status": "idle"})
    return {"status": "stopped"}


def stop_everything():
    """Stop ALL playback from any source — DJ set, radio stream, video, and the
    Music app. Any 'stop' tool routes here so even a weak model that calls the
    'wrong' stop still silences everything, across all instances.
    """
    _signal_global_stop()
    _dj_stop.set()
    try:
        _video_stop.set()
    except Exception:
        pass
    stop_all()  # pkill mpv (incl. video window) + pause Spotify/Music
    subprocess.run(["pkill", "-f", "afplay"], capture_output=True)
    _write_state({"status": "idle"})
    return {"status": "stopped", "scope": "all"}


# ============================================================
# 24-Hour Scheduled Auto-Broadcast
# ============================================================
# Schedule format: list of slots (AI can customize all fields)
#
# Basic slot (AI searches songs):
#   {"start": "07:00", "name": "모닝 카페", "query": "morning acoustic cafe", "count": 8}
#
# Full slot (AI provides everything):
#   {
#     "start": "07:00",
#     "name": "모닝 카페",
#     "songs": ["아이유 - 좋은날", "볼빨간사춘기 - 여행", ...],  # AI-selected songs
#     "comments": ["좋은 아침이에요...", "다음곡은...", ...],    # AI-generated comments
#     "provider": "apple_music",  # or "youtube", "spotify"
#     "lang": "ko",               # TTS language
#     "news": "오늘의 주요 뉴스..."  # Optional news to mention
#   }

DEFAULT_SCHEDULE = [
    {"start": "06:00", "name": "모닝 카페", "query": "morning acoustic cafe playlist", "count": 10},
    {"start": "09:00", "name": "집중 워크", "query": "lofi focus study beats", "count": 12},
    {"start": "12:00", "name": "런치 팝", "query": "feel good pop hits", "count": 10},
    {"start": "15:00", "name": "애프터눈 재즈", "query": "smooth jazz afternoon", "count": 10},
    {"start": "18:00", "name": "이브닝 시티팝", "query": "city pop evening drive", "count": 10},
    {"start": "21:00", "name": "나이트 R&B", "query": "chill rnb night vibes", "count": 10},
    {"start": "23:00", "name": "미드나잇 앰비언트", "query": "ambient sleep relaxing", "count": 12},
    {"start": "02:00", "name": "심야 로파이", "query": "late night lofi hip hop", "count": 12},
]


def create_schedule_from_ai(slots):
    """Create and save a schedule from AI-generated slot definitions.

    Args:
        slots: List of slot dicts, each with:
            - start: "HH:MM" format
            - name: Show name
            - songs: List of song names (optional, or use query+count)
            - comments: List of DJ comments (optional)
            - provider: "apple_music", "youtube", "spotify" (optional)
            - query: Search query if songs not provided (optional)
            - count: Number of songs to search (optional, default 10)
            - lang: Language code (optional)
            - news: News to mention (optional)

    Returns:
        Status dict
    """
    if not slots or not isinstance(slots, list):
        return {"error": "slots must be a non-empty list"}

    # Validate slots
    for i, slot in enumerate(slots):
        if "start" not in slot:
            return {"error": f"Slot {i} missing 'start' time"}
        if "name" not in slot:
            slot["name"] = f"Show {i+1}"
        if "songs" not in slot and "query" not in slot:
            return {"error": f"Slot {i} needs either 'songs' or 'query'"}

    save_schedule(slots)
    return {"status": "schedule_created", "slots": len(slots),
            "schedule": slots}

_sched_thread = None
_sched_stop = threading.Event()


def load_schedule():
    try:
        if os.path.exists(SCHEDULE_FILE):
            with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return DEFAULT_SCHEDULE


def save_schedule(schedule):
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
        json.dump(schedule, f, ensure_ascii=False, indent=2)
    return {"status": "saved", "slots": len(schedule)}


def _parse_hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def current_slot(schedule=None, now=None):
    """Return the active slot for the given time (wraps around 24h)."""
    schedule = schedule or load_schedule()
    if not schedule:
        return None
    now = now or datetime.now()
    now_min = now.hour * 60 + now.minute
    slots = sorted(schedule, key=lambda s: _parse_hhmm(s["start"]))
    active = slots[-1]  # default: last slot (wraps past midnight)
    for slot in slots:
        if _parse_hhmm(slot["start"]) <= now_min:
            active = slot
        else:
            break
    return active


def _fetch_slot_songs(slot):
    """Build a song query list for a slot using yt-dlp search."""
    q = slot.get("query", "music")
    count = int(slot.get("count", 10))

    # If songs are pre-defined, use them directly
    if slot.get("songs"):
        return slot["songs"][:count]

    # yt-dlp flat search -> list of "title" strings for track queries
    try:
        cmd = [YTDLP, f"ytsearch{count}:{q}",
               "--flat-playlist", "--print", "%(title)s", "--no-warnings"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=_env())
        titles = [t.strip() for t in r.stdout.splitlines() if t.strip()]
        if titles:
            return titles
    except Exception:
        pass
    # fallback: repeat the query itself
    return [q] * count


# ============================================================
# Quick 1-Hour DJ Broadcast
# ============================================================
def start_one_hour_broadcast(genre="k-pop hits", provider="apple_music",
                             songs=None, comments=None, lang=None):
    """Start a quick 1-hour DJ broadcast.

    Args:
        genre: Genre/query for song search (e.g. "k-pop hits", "jazz classics")
        provider: Music provider ("apple_music", "youtube", "spotify")
        songs: Pre-selected song list (12-15 songs for 1 hour)
        comments: Pre-generated DJ comments from AI
        lang: Language code (ko, en, ja, etc.)

    Returns:
        Status dict with broadcast info
    """
    # If no songs provided, search for them
    if not songs:
        songs = _fetch_slot_songs({"query": genre, "count": 15})

    now = datetime.now()
    slot_name = f"{now.strftime('%H:%M')} DJ 방송"

    return start_dj_set(
        songs=songs,
        provider=provider,
        slot_name=slot_name,
        with_comments=True,
        comments=comments,
        lang=lang
    )


def _scheduler_worker():
    last_slot_name = None
    while not _sched_stop.is_set():
        slot = current_slot()
        if slot and slot.get("name") != last_slot_name:
            last_slot_name = slot.get("name")

            # Get songs (AI-provided or search)
            songs = slot.get("songs") or _fetch_slot_songs(slot)

            # Get AI-provided comments (or None for auto-generation)
            comments = slot.get("comments")

            # Start DJ set with all AI customizations
            start_dj_set(
                songs=songs,
                provider=slot.get("provider", "apple_music"),
                slot_name=slot.get("name"),
                with_comments=True,
                comments=comments,
                lang=slot.get("lang"),
            )

        # check every 60s for slot change
        for _ in range(60):
            if _sched_stop.is_set():
                break
            time.sleep(1)


def start_scheduler():
    global _sched_thread
    stop_scheduler()
    _sched_stop.clear()
    _sched_thread = threading.Thread(target=_scheduler_worker, daemon=True)
    _sched_thread.start()
    slot = current_slot()
    return {"status": "scheduler_started",
            "current_slot": slot.get("name") if slot else None,
            "slots": len(load_schedule())}


def stop_scheduler():
    _sched_stop.set()
    stop_dj_set()
    return {"status": "scheduler_stopped"}


def schedule_status():
    slot = current_slot()
    return {
        "scheduler_running": _sched_thread is not None and _sched_thread.is_alive(),
        "current_slot": slot,
        "schedule": load_schedule(),
        "dj_state": _read_state(),
    }


# ============================================================
# Daily Morning Countdown Radio Show
# ============================================================
import urllib.request
import json as _json

AIRTUNE_HOT = "https://airtune.fly.dev/api/charts/hot-today"


def fetch_yesterday_chart(limit=5):
    """Fetch yesterday's most-played songs, ranked by play count (desc)."""
    try:
        req = urllib.request.Request(
            f"{AIRTUNE_HOT}?limit=60",
            headers={"User-Agent": "radiomcp"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = _json.loads(r.read().decode())
        rows = data.get("data") or data.get("songs") or []
    except Exception:
        rows = []
    # rank by plays desc, then stations desc
    def _score(x):
        return (x.get("plays", 0), x.get("stations", 0))
    rows = [x for x in rows if x.get("title") and x.get("artist")]
    rows.sort(key=_score, reverse=True)
    return rows[:limit]


def _ordinal_ko(n):
    return f"{n}위"


def make_countdown_comment(song, rank, total):
    """DJ comment introducing a ranked song (countdown style)."""
    artist = song.get("artist", "")
    title = song.get("title", "")
    plays = song.get("plays", 0)
    stations = song.get("stations", 0)
    if rank == total:
        # highest rank (played first in reverse) or opening
        pass
    templates_top = [
        f"자, 대망의 {rank}위입니다. {artist}의 {title}. "
        f"어제 전 세계 {stations}개 방송국에서 사랑받은 곡이죠. 함께 들어보시죠.",
        f"오늘의 {rank}위, {artist}, {title}. 지금 바로 만나보세요.",
    ]
    templates_mid = [
        f"이어서 {rank}위. {artist}의 {title}입니다.",
        f"{rank}위 곡은 {artist}의 {title}. 어제 {plays}번이나 전파를 탔습니다.",
        f"다음 순위로 넘어가 볼까요. {rank}위, {artist} - {title}.",
    ]
    import random
    if rank <= 3:
        return random.choice(templates_top)
    return random.choice(templates_mid)


def build_countdown_show(limit=5, date_label=None):
    """Build the full DJ script + song queue for the morning countdown."""
    import datetime
    chart = fetch_yesterday_chart(limit=limit)
    if not chart:
        return None
    if not date_label:
        y = datetime.date.today() - datetime.timedelta(days=1)
        date_label = f"{y.month}월 {y.day}일"
    intro = (f"안녕하세요. 오늘 아침 라디오 카운트다운입니다. "
             f"지금부터 {date_label}, 어제 전 세계 방송에서 "
             f"가장 사랑받은 인기곡 톱 {limit}을 순위별로 소개해 드리겠습니다. "
             f"그럼 카운트다운, 시작합니다.")
    outro = ("지금까지 어제의 인기곡 카운트다운이었습니다. "
             "오늘 하루도 좋은 음악과 함께 활기차게 시작하세요. "
             "내일 아침 또 만나요.")
    # countdown: play from lowest rank up to #1
    items = []
    total = len(chart)
    # chart[0] is #1; we count DOWN so start at bottom rank -> #1
    for i in range(total - 1, -1, -1):
        rank = i + 1
        song = chart[i]
        comment = make_countdown_comment(song, rank, total)
        query = f"{song['artist']} {song['title']}"
        items.append({"rank": rank, "comment": comment, "query": query,
                      "artist": song["artist"], "title": song["title"]})
    return {"intro": intro, "outro": outro, "items": items,
            "date_label": date_label}


def _countdown_worker(show):
    """Play the countdown show: intro -> (comment -> song)* -> outro."""
    global _dj_stop
    _write_state({"mode": "countdown", "running": True,
                  "date": show["date_label"], "total": len(show["items"])})
    try:
        speak_dj(show["intro"], wait=True)
        for it in show["items"]:
            if _dj_stop.is_set():
                break
            speak_dj(it["comment"], wait=True)
            if _dj_stop.is_set():
                break
            play_youtube_track(it["query"], wait=True)
        if not _dj_stop.is_set():
            speak_dj(show["outro"], wait=True)
    finally:
        st = _read_state()
        st["running"] = False
        _write_state(st)


def start_countdown_show(limit=5, date_label=None):
    """Start the morning countdown radio show in a background thread."""
    global _dj_thread, _dj_stop
    show = build_countdown_show(limit=limit, date_label=date_label)
    if not show:
        return {"status": "error", "message": "차트 데이터를 가져올 수 없습니다."}
    stop_dj_set()
    _dj_stop.clear()
    _dj_thread = threading.Thread(target=_countdown_worker, args=(show,), daemon=True)
    _dj_thread.start()
    return {
        "status": "countdown_started",
        "date": show["date_label"],
        "total": len(show["items"]),
        "lineup": [{"rank": it["rank"], "artist": it["artist"],
                    "title": it["title"]} for it in show["items"]],
    }


# ---- Daily auto-broadcast scheduler (morning countdown) ----
_daily_thread = None
_daily_stop = threading.Event()
_daily_cfg = {"hour": 7, "minute": 0, "limit": 5}


def _daily_worker():
    import datetime
    last_run_date = None
    while not _daily_stop.is_set():
        now = datetime.datetime.now()
        if (now.hour == _daily_cfg["hour"] and now.minute == _daily_cfg["minute"]
                and last_run_date != now.date()):
            last_run_date = now.date()
            try:
                start_countdown_show(limit=_daily_cfg["limit"])
            except Exception:
                pass
        _daily_stop.wait(20)


def start_daily_broadcast(hour=7, minute=0, limit=5):
    """Schedule the morning countdown to auto-run every day at HH:MM."""
    global _daily_thread
    _daily_cfg["hour"] = int(hour)
    _daily_cfg["minute"] = int(minute)
    _daily_cfg["limit"] = int(limit)
    _daily_stop.set()
    if _daily_thread and _daily_thread.is_alive():
        _daily_thread.join(timeout=2)
    _daily_stop.clear()
    _daily_thread = threading.Thread(target=_daily_worker, daemon=True)
    _daily_thread.start()
    return {"status": "daily_broadcast_scheduled",
            "time": f"{hour:02d}:{minute:02d}", "top": limit}


def stop_daily_broadcast():
    global _daily_thread
    _daily_stop.set()
    return {"status": "daily_broadcast_stopped"}


def daily_broadcast_status():
    return {
        "running": _daily_thread is not None and _daily_thread.is_alive(),
        "config": dict(_daily_cfg),
    }
