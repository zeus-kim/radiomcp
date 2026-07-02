#!/usr/bin/env python3
"""
RadioCli TUI - Interactive terminal radio player
Part of the radiomcp package: pip install radiomcp
"""

import subprocess
import sys
import json
import urllib.request
import urllib.parse
import shutil
import signal
import os
import time
import sqlite3
import threading
from datetime import datetime
from collections import Counter

# Package directory (where this file lives)
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))

# Data file paths
DATA_DIR = os.path.expanduser("~/.radiocli")
FAVORITES_FILE = os.path.join(DATA_DIR, "favorites.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
SONGS_FILE = os.path.join(DATA_DIR, "songs.json")  # Song history
LAST_STATION_FILE = os.path.join(DATA_DIR, "last_station.json")  # Last station
PREFERENCES_FILE = os.path.join(DATA_DIR, "preferences.json")

# SQLite DB - user working DB takes priority; bundle is copied on first run
def _init_user_db():
    """Copy bundled DB to ~/.radiocli/ on first run or if bundle is significantly larger."""
    user_db = os.path.join(DATA_DIR, "radio_stations.db")
    bundle_db = os.path.join(_PKG_DIR, "radio_stations.db")
    if not os.path.exists(bundle_db):
        return
    user_size = os.path.getsize(user_db) if os.path.exists(user_db) else 0
    bundle_size = os.path.getsize(bundle_db)
    if bundle_size > user_size * 2:
        try:
            shutil.copy2(bundle_db, user_db)
        except Exception:
            pass

def _find_db():
    candidates = [
        os.path.join(DATA_DIR, "radio_stations.db"),          # ~/.radiocli/ (user's working DB)
        os.path.join(_PKG_DIR, "radio_stations.db"),          # pip install bundle (fallback)
        os.path.expanduser("~/RadioCli/radio_stations.db"),   # dev environment
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]  # fallback to user dir path

_init_user_db()
DB_PATH = _find_db()

# API mode: True=DB+API, False=DB only (fast)
USE_API = False  # Default: DB only (0.1s) — API is slow, DB has 24K+ stations

# Create data directory
os.makedirs(DATA_DIR, exist_ok=True)

# === UI Language Setting ===
UI_LANG = os.environ.get("RADIOCLI_LANG", "en")

# Load multilingual strings from languages.json (70+ languages)
def load_languages():
    """Load UI strings from languages.json"""
    # Search: 1) package dir, 2) script dir (for symlink compat)
    lang_file = os.path.join(_PKG_DIR, "languages.json")
    if not os.path.exists(lang_file):
        script_dir = os.path.dirname(os.path.realpath(__file__))
        lang_file = os.path.join(script_dir, "languages.json")

    if os.path.exists(lang_file):
        try:
            with open(lang_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass

    # Fallback: English only
    return {
        "en": {"name": "English", "title": "RadioCli Radio", "search_hint": "Search", "search_examples": "jazz", "ai_recommend": "AI Recommend", "my_taste": "My Taste", "mood_now": "Mood", "song_recognize": "Recognize", "popular": "Popular", "hq": "Hi-Quality", "genre": "Genre", "country": "Country", "favorites": "Favorites", "playlist": "Playlist", "premium": "Premium", "dj_mode": "DJ Mode", "stop": "Stop", "quit": "Quit", "playing": "Playing", "added_fav": "Added", "removed_fav": "Removed", "already_fav": "Already exists", "no_fav": "None", "searching": "Searching", "loading": "Loading", "no_results": "No results", "invalid_num": "Invalid", "help_after_play": "+ fav | s stop | m menu", "ad_playing": "Ad", "history": "History", "llm": "LLM"}
    }

UI_STRINGS = load_languages()

# Auto-generate LANG_NAMES
LANG_NAMES = {code: data.get("name", code) for code, data in UI_STRINGS.items()}


def t(key):
    """Translate UI string"""
    strings = UI_STRINGS.get(UI_LANG, UI_STRINGS.get("en", {}))
    return strings.get(key, UI_STRINGS.get("en", {}).get(key, key))

def show_languages(page=1):
    """Show language selection (20 per page)"""
    per_page = 20
    langs = list(LANG_NAMES.items())
    total = len(langs)
    total_pages = (total + per_page - 1) // per_page
    page = max(1, min(page, total_pages))

    start = (page - 1) * per_page
    end = min(start + per_page, total)

    print(f"\n  Language ({page}/{total_pages}) - {total}:")
    for code, name in langs[start:end]:
        marker = "●" if code == UI_LANG else "○"
        print(f"    {marker} {code}: {name}")
    if total_pages > 1:
        print(f"  [lang 2] {t('next_page')} / [lang code] {t('select_lang')}")
    print()

def change_language(code):
    """Change language"""
    global UI_LANG
    code = code.lower().strip()
    if code in UI_STRINGS:
        UI_LANG = code
        # Save preference
        try:
            with open(os.path.join(DATA_DIR, "lang.txt"), "w") as f:
                f.write(code)
        except:
            pass
        print(f"  ✓ {LANG_NAMES.get(code, code)}\n")
        return True
    else:
        # If page number
        if code.isdigit():
            show_languages(int(code))
            return False
        print(f"  ? {t('supported_langs')}: {len(UI_STRINGS)} (lang)\n")
        return False

def init_language():
    """Initialize language (env > saved > default English)"""
    global UI_LANG
    # 1) Use environment variable if set
    env_lang = os.environ.get("RADIOCLI_LANG", "")
    if env_lang and env_lang in UI_STRINGS:
        UI_LANG = env_lang
        return

    # 2) Use saved preference (only if user explicitly set it via 'lang' command)
    try:
        pref_file = os.path.join(DATA_DIR, "lang.txt")
        if os.path.exists(pref_file):
            saved = open(pref_file).read().strip()
            if saved in UI_STRINGS:
                UI_LANG = saved
                return
    except:
        pass

    # 3) Default: English
    UI_LANG = "en"

# Song recognition settings
AUDD_API_KEY = os.environ.get("AUDD_API_KEY", "")  # Backup
SHAZAM_API_KEY = os.environ.get("SHAZAM_API_KEY", "")  # Backup
RECOGNIZED_SONGS_FILE = os.path.join(DATA_DIR, "recognized_songs.json")
RECORD_FILE = os.path.join(DATA_DIR, "record_sample.mp3")
RECORD_WAV_FILE = os.path.join(DATA_DIR, "record_sample.wav")

# === LLM Settings ===
# RADIOCLI_LLM: none(default), auto, ollama, claude, openai
LLM_PROVIDER = os.environ.get("RADIOCLI_LLM", "none")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

API_BASE = "https://api.airtune.ai"
PLAYER = None
PLAYER_PROC = None
CURRENT_SONG_FILE = os.path.join(DATA_DIR, "current_song.txt")
MPV_SOCKET = os.path.join(DATA_DIR, "mpv.sock")
MPV_PID_FILE = os.path.join(DATA_DIR, "mpv.pid")  # Shared MCP/CLI

def kill_existing_mpv():
    """Kill existing mpv process (shared MCP/CLI)"""
    # 1. Try to quit via IPC socket
    if os.path.exists(MPV_SOCKET):
        try:
            import socket as sock_module
            s = sock_module.socket(sock_module.AF_UNIX, sock_module.SOCK_STREAM)
            s.settimeout(1)
            s.connect(MPV_SOCKET)
            s.send(b'{"command": ["quit"]}\n')
            s.close()
            time.sleep(0.5)
        except:
            pass

    # 2. Kill via PID file
    if os.path.exists(MPV_PID_FILE):
        try:
            with open(MPV_PID_FILE, 'r') as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            try:
                os.kill(pid, 0)  # still alive?
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except:
            pass
        try:
            os.remove(MPV_PID_FILE)
        except:
            pass

    # 3. Last resort: pkill radiocli mpv
    try:
        subprocess.run(["pkill", "-f", "mpv.*radiocli"], timeout=2)
        time.sleep(0.3)
    except:
        pass

    # 4. Clean up socket file
    if os.path.exists(MPV_SOCKET):
        try:
            os.remove(MPV_SOCKET)
        except:
            pass

# === SQLite DB Search (fast) ===
_db_cache = None

def db_search(query=None, country=None, tag=None, limit=30):
    """Search from DB (uses memory cache)"""
    global _db_cache

    if not os.path.exists(DB_PATH):
        return []

    # Load cache (first time only)
    if _db_cache is None:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM stations
                WHERE is_alive = 1 OR is_alive IS NULL
                ORDER BY clickcount DESC
            """)
            _db_cache = [dict(row) for row in cursor.fetchall()]
            conn.close()
        except:
            _db_cache = []

    results = []
    for s in _db_cache:
        # Country filter
        if country and s.get("countrycode", "").upper() != country.upper():
            continue
        # Tag filter
        if tag and tag.lower() not in s.get("tags", "").lower():
            continue
        # Name search
        if query and query.lower() not in s.get("name", "").lower():
            continue

        results.append(s)
        if len(results) >= limit:
            break

    return results

def mark_station_failed(url):
    """Record failed station (mark dead after 3 fails)"""
    if not os.path.exists(DB_PATH):
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE stations
            SET fail_count = COALESCE(fail_count, 0) + 1,
                is_alive = CASE WHEN COALESCE(fail_count, 0) >= 2 THEN 0 ELSE is_alive END,
                last_checked_at = datetime('now')
            WHERE url = ? OR url_resolved = ?
        """, (url, url))
        conn.commit()
        conn.close()
        global _db_cache
        _db_cache = None  # Invalidate cache
    except:
        pass

def cleanup_dead_stations():
    """Clean up dead stations (delete is_alive=0)"""
    if not os.path.exists(DB_PATH):
        return 0
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM stations WHERE is_alive = 0")
        count = cursor.fetchone()[0]
        if count > 0:
            cursor.execute("DELETE FROM stations WHERE is_alive = 0")
            conn.commit()
        conn.close()
        global _db_cache
        _db_cache = None
        return count
    except:
        return 0

# Popular genres (tag, translation_key)
GENRES = {
    "1": ("pop", "genre_pop"),
    "2": ("rock", "genre_rock"),
    "3": ("jazz", "genre_jazz"),
    "4": ("classical", "genre_classical"),
    "5": ("kpop", "genre_kpop"),
    "6": ("hiphop", "genre_hiphop"),
    "7": ("electronic", "genre_electronic"),
    "8": ("lounge", "genre_lounge"),
    "9": ("news", "genre_news"),
    "0": ("talk", "genre_talk"),
}

# Major countries (code, translation_key)
COUNTRIES = {
    "kr": ("KR", "country_kr"),
    "us": ("US", "country_us"),
    "jp": ("JP", "country_jp"),
    "gb": ("GB", "country_gb"),
    "de": ("DE", "country_de"),
    "fr": ("FR", "country_fr"),
    "cn": ("CN", "country_cn"),
}

# Multilingual -> English mapping
LANG_MAP = {
    # === Countries (multilingual: ko, en, ja, zh, de, fr, es) ===
    # Korea
    "korea": "KR", "korea": "KR", "korean": "KR", "south korea": "KR",
    "韓国": "KR", "かんこく": "KR", "韩国": "KR", "corea": "KR", "corée": "KR",
    # USA
    "usa": "US", "america": "US", "american": "US", "usa": "US", "united states": "US",
    "アメリカ": "US", "美国": "US", "amerika": "US", "états-unis": "US", "estados unidos": "US",
    # Japan
    "japan": "JP", "japan": "JP", "japanese": "JP",
    "日本": "JP", "にほん": "JP", "japón": "JP", "japon": "JP",
    # UK
    "uk": "GB", "uk": "GB", "britain": "GB", "british": "GB", "england": "GB",
    "イギリス": "GB", "英国": "GB", "reino unido": "GB", "royaume-uni": "GB",
    # Germany
    "germany": "DE", "germany": "DE", "german": "DE", "deutschland": "DE",
    "ドイツ": "DE", "德国": "DE", "alemania": "DE", "allemagne": "DE",
    # France
    "france": "FR", "france": "FR", "french": "FR", "frankreich": "FR",
    "フランス": "FR", "法国": "FR", "francia": "FR",
    # China
    "china": "CN", "china": "CN", "chinese": "CN",
    "中国": "CN", "ちゅうごく": "CN", "chine": "CN",
    # Brazil
    "brazil": "BR", "brazil": "BR", "brasil": "BR", "brasilien": "BR",
    "ブラジル": "BR", "巴西": "BR", "brésil": "BR",
    # Australia
    "australia": "AU", "australia": "AU", "australian": "AU", "australien": "AU",
    "オーストラリア": "AU", "澳大利亚": "AU", "australie": "AU",
    # Canada
    "canada": "CA", "canada": "CA", "canadian": "CA", "kanada": "CA",
    "カナダ": "CA", "加拿大": "CA",
    # Italy
    "italy": "IT", "italy": "IT", "italian": "IT", "italien": "IT", "italia": "IT",
    "イタリア": "IT", "意大利": "IT", "italie": "IT",
    # Spain
    "spain": "ES", "spain": "ES", "spanish": "ES", "spanien": "ES", "españa": "ES",
    "スペイン": "ES", "西班牙": "ES", "espagne": "ES",
    # Russia
    "russia": "RU", "russia": "RU", "russian": "RU", "russland": "RU",
    "ロシア": "RU", "俄罗斯": "RU", "russie": "RU", "rusia": "RU",
    # India
    "india": "IN", "india": "IN", "indian": "IN", "indien": "IN",
    "インド": "IN", "印度": "IN", "inde": "IN",
    # Mexico
    "mexico": "MX", "mexico": "MX", "mexican": "MX", "mexiko": "MX",
    "メキシコ": "MX", "墨西哥": "MX", "mexique": "MX", "méxico": "MX",
    # Netherlands
    "netherlands": "NL", "netherlands": "NL", "dutch": "NL", "holland": "NL",
    "オランダ": "NL", "荷兰": "NL", "pays-bas": "NL", "países bajos": "NL",
    # Switzerland
    "switzerland": "CH", "switzerland": "CH", "swiss": "CH", "schweiz": "CH",
    "スイス": "CH", "瑞士": "CH", "suisse": "CH", "suiza": "CH",

    # === Additional languages: Hindi, Arabic, Russian, Portuguese, Italian, Turkish, Thai, Vietnamese, Indonesian ===
    # Hindi (Hindi)
    "भारत": "IN", "इंडिया": "IN",  # India
    "अमेरिका": "US", "अमेरीका": "US",  # America
    "जापान": "JP", "कोरिया": "KR", "चीन": "CN", "रूस": "RU",
    # Arabic (Arabic)
    "الهند": "IN", "أمريكا": "US", "اليابان": "JP", "كوريا": "KR",
    "الصين": "CN", "روسيا": "RU", "مصر": "EG", "السعودية": "SA",
    # Russian (Russian)
    "индия": "IN", "америка": "US", "япония": "JP", "корея": "KR",
    "китай": "CN", "россия": "RU", "германия": "DE", "франция": "FR",
    # Portuguese (Portuguese)
    "índia": "IN", "américa": "US", "japão": "JP", "coreia": "KR",
    "rússia": "RU", "alemanha": "DE", "frança": "FR", "itália": "IT",
    # Italian (Italian)
    "giappone": "JP", "corea": "KR", "cina": "CN", "germania": "DE",
    "spagna": "ES", "portogallo": "PT", "svizzera": "CH",
    # Turkish (Turkish)
    "hindistan": "IN", "amerika": "US", "japonya": "JP", "kore": "KR",
    "çin": "CN", "rusya": "RU", "almanya": "DE", "fransa": "FR",
    # Thai (Thai)
    "อินเดีย": "IN", "อเมริกา": "US", "ญี่ปุ่น": "JP", "เกาหลี": "KR",
    "จีน": "CN", "รัสเซีย": "RU", "ไทย": "TH",
    # Vietnamese (Vietnamese)
    "ấn độ": "IN", "mỹ": "US", "nhật bản": "JP", "hàn quốc": "KR",
    "trung quốc": "CN", "nga": "RU", "việt nam": "VN",
    # Indonesian (Indonesian)
    "india": "IN", "jepang": "JP", "tiongkok": "CN",

    # === European languages ===
    # Polish (Polish)
    "polska": "PL", "stany zjednoczone": "US", "japonia": "JP", "niemcy": "DE",
    "francja": "FR", "hiszpania": "ES", "rosja": "RU",
    # Dutch (Dutch)
    "nederland": "NL", "duitsland": "DE", "frankrijk": "FR",
    "spanje": "ES", "rusland": "RU",
    # Swedish (Swedish)
    "sverige": "SE", "storbritannien": "GB", "tyskland": "DE", "frankrike": "FR",
    # Norwegian (Norwegian)
    "norge": "NO", "storbritannia": "GB",
    # Danish (Danish)
    "danmark": "DK", "frankrig": "FR",
    # Finnish (Finnish)
    "suomi": "FI", "yhdysvallat": "US", "japani": "JP", "saksa": "DE",
    "ranska": "FR", "espanja": "ES", "venäjä": "RU",
    # Czech (Czech)
    "česko": "CZ", "japonsko": "JP", "německo": "DE", "francie": "FR", "rusko": "RU",
    # Hungarian (Hungarian)
    "magyarország": "HU", "japán": "JP", "németország": "DE", "franciaország": "FR",
    "oroszország": "RU", "kína": "CN",
    # Romanian (Romanian)
    "românia": "RO", "japonia": "JP", "germania": "DE", "rusia": "RU",
    # Bulgarian (Bulgarian)
    "българия": "BG", "сащ": "US", "япония": "JP", "испания": "ES",
    # Ukrainian (Ukrainian)
    "україна": "UA", "сша": "US", "японія": "JP", "німеччина": "DE", "росія": "RU",
    # Greek (Greek)
    "ελλάδα": "GR", "ιαπωνία": "JP", "γερμανία": "DE", "γαλλία": "FR", "ρωσία": "RU",
    # Croatian/Serbian
    "hrvatska": "HR", "srbija": "RS", "njemačka": "DE", "francuska": "FR",
    # Slovenian/Slovak
    "slovenija": "SI", "slovensko": "SK", "nemecko": "DE",

    # === Baltic languages ===
    # Estonian (Estonian)
    "eesti": "EE", "ameerika": "US", "jaapan": "JP", "saksamaa": "DE",
    # Latvian (Latvian)
    "latvija": "LV", "japāna": "JP", "vācija": "DE",
    # Lithuanian (Lithuanian)
    "lietuva": "LT", "japonija": "JP", "vokietija": "DE",

    # === Caucasus/Central Asian languages ===
    # Georgian (Georgian)
    "საქართველო": "GE", "ამერიკა": "US", "იაპონია": "JP",
    # Azerbaijani (Azerbaijani)
    "azərbaycan": "AZ", "yaponiya": "JP", "almaniya": "DE",
    # Kazakh (Kazakh)
    "қазақстан": "KZ", "жапония": "JP",
    # Uzbek (Uzbek)
    "oʻzbekiston": "UZ",
    # Mongolian (Mongolian)
    "монгол": "MN", "америк": "US", "япон": "JP",

    # === Middle Eastern languages ===
    # Hebrew (Hebrew)
    "ישראל": "IL", "יפן": "JP", "גרמניה": "DE", "צרפת": "FR", "רוסיה": "RU",
    # Persian (Persian/Farsi)
    "ایران": "IR", "آمریکا": "US", "ژاپن": "JP", "آلمان": "DE", "روسیه": "RU",
    # Urdu (Urdu)
    "پاکستان": "PK", "جاپان": "JP", "جرمنی": "DE",

    # === South Asian languages ===
    # Bengali (Bengali)
    "বাংলাদেশ": "BD", "ভারত": "IN", "আমেরিকা": "US", "জাপান": "JP",
    # Tamil
    "இந்தியா": "IN", "அமெரிக்கா": "US", "ஜப்பான்": "JP",
    # Telugu
    "భారతదేశం": "IN", "అమెరికా": "US", "జపాన్": "JP",
    # Gujarati
    "ભારત": "IN", "અમેરિકા": "US", "જાપાન": "JP",
    # Punjabi
    "ਭਾਰਤ": "IN", "ਅਮਰੀਕਾ": "US", "ਜਾਪਾਨ": "JP",
    # Nepali (Nepali)
    "नेपाल": "NP",
    # Sinhala (Sinhala)
    "ශ්‍රී ලංකාව": "LK", "ඉන්දියාව": "IN",

    # === Southeast Asian languages ===
    # Malay (Malay)
    "malaysia": "MY", "jepun": "JP", "jerman": "DE", "perancis": "FR",
    # Tagalog/Filipino
    "pilipinas": "PH", "hapon": "JP", "alemanya": "DE", "pransya": "FR",
    # Khmer (Khmer)
    "កម្ពុជា": "KH", "ជប៉ុន": "JP",
    # Burmese (Burmese)
    "မြန်မာ": "MM", "ဂျပန်": "JP",
    # Lao (Lao)
    "ລາວ": "LA", "ຍີ່ປຸ່ນ": "JP",

    # === African languages ===
    # Swahili (Swahili)
    "kenya": "KE", "tanzania": "TZ", "japani": "JP", "ujerumani": "DE",
    # Amharic (Amharic)
    "ኢትዮጵያ": "ET", "ጃፓን": "JP",
    # Afrikaans (Afrikaans)
    "suid-afrika": "ZA",

    # === Genres (multilingual: ko, en, ja, zh, de, fr, es) ===
    # Jazz
    "jazz": "jazz", "jazz": "jazz", "ジャズ": "jazz", "爵士": "jazz", "爵士乐": "jazz",
    # Classical
    "classical": "classical", "classical": "classical", "classic": "classical",
    "クラシック": "classical", "古典": "classical", "古典音乐": "classical",
    "klassik": "classical", "classique": "classical", "clásica": "classical",
    # Pop
    "pop": "pop", "pop": "pop", "pops": "pop", "ポップ": "pop", "流行": "pop",
    # Rock
    "rock": "rock", "rock": "rock", "ロック": "rock", "摇滚": "rock",
    # Hip-hop
    "hip hop": "hiphop", "hiphop": "hiphop", "hip-hop": "hiphop", "hip hop": "hiphop",
    "ヒップホップ": "hiphop", "嘻哈": "hiphop", "rap": "hiphop", "": "hiphop",
    # K-pop
    "kpop": "kpop", "kpop": "kpop", "k-pop": "kpop", "-pop": "kpop",
    "韓国ポップ": "kpop", "韩流": "kpop",
    # News (expanded)
    "news": "news", "news": "news", "ニュース": "news", "新闻": "news",
    "nachrichten": "news", "nouvelles": "news", "noticias": "news",
    "": "news", "": "news", "": "news", "": "news",
    "information": "news", "current affairs": "news",
    # Talk (expanded)
    "": "talk", "talk": "talk", "トーク": "talk", "谈话": "talk",
    "radio": "talk", "radio show": "talk", "talkshow": "talk", "": "talk",
    # Lounge
    "lounge": "lounge", "lounge": "lounge", "ラウンジ": "lounge",
    "chillout": "lounge", "chill": "lounge", "": "lounge",
    # Blues
    "blues": "blues", "blues": "blues", "ブルース": "blues", "蓝调": "blues",
    # Country
    "country": "country", "country": "country", "カントリー": "country", "乡村": "country",
    # Electronic
    "": "electronic", "electronic": "electronic", "electro": "electronic",
    "エレクトロ": "electronic", "电子": "electronic", "électronique": "electronic",
    "electronica": "electronic", "": "electronic", "techno": "electronic",
    # Dance
    "dance": "dance", "dance": "dance", "ダンス": "dance", "舞曲": "dance",
    # Ballad
    "ballad": "ballad", "ballad": "ballad", "バラード": "ballad",
    # R&B
    "r&b": "rnb", "rnb": "rnb", "r&b": "rnb", "r and b": "rnb",
    # Reggae
    "reggae": "reggae", "reggae": "reggae", "レゲエ": "reggae",
    # Soul
    "soul": "soul", "soul": "soul", "ソウル": "soul",
    # Funk
    "funk": "funk", "funk": "funk", "ファンク": "funk",
    # Metal
    "metal": "metal", "metal": "metal", "メタル": "metal", "heavy metal": "metal",
    # Ambient
    "ambient": "ambient", "ambient": "ambient", "アンビエント": "ambient",
    # Trot
    "trot": "trot", "trot": "trot", "トロット": "trot", "": "trot",
    # Religious
    "": "religious", "religious": "religious", "christian": "religious",
    "gospel": "religious", "christian": "religious", "": "religious",
    # Kids
    "": "children", "children": "children", "kids": "children",
    "子供": "children", "儿童": "children", "": "children",
    # Oldies
    "oldies": "oldies", "oldies": "oldies", "オールディーズ": "oldies",
    "80": "80s", "80s": "80s", "90": "90s", "90s": "90s",
    "70": "70s", "70s": "70s", "60": "60s", "60s": "60s",

    # === Additional language genres ===
    # Hindi (Hindi)
    "संगीत": "music", "जैज़": "jazz", "पॉप": "pop", "रॉक": "rock",
    "समाचार": "news", "शास्त्रीय": "classical",
    # Arabic (Arabic)
    "موسيقى": "music", "جاز": "jazz", "بوب": "pop", "روك": "rock",
    "أخبار": "news", "كلاسيكي": "classical",
    # Russian (Russian)
    "музыка": "music", "джаз": "jazz", "поп": "pop", "рок": "rock",
    "новости": "news", "классика": "classical", "классическая": "classical",
    # Portuguese (Portuguese)
    "música": "music", "notícias": "news", "clássica": "classical",
    "notícia": "news", "eletrônica": "electronic",
    # Italian (Italian)
    "musica": "music", "notizie": "news", "classica": "classical",
    "elettronica": "electronic",
    # Turkish (Turkish)
    "müzik": "music", "caz": "jazz", "haber": "news", "klasik": "classical",
    # Thai (Thai)
    "เพลง": "music", "แจ๊ส": "jazz", "ป๊อป": "pop", "ร็อค": "rock",
    "ข่าว": "news", "คลาสสิก": "classical",
    # Vietnamese (Vietnamese)
    "nhạc": "music", "tin tức": "news", "cổ điển": "classical",
    # Indonesian (Indonesian)
    "musik": "music", "berita": "news", "klasik": "classical",
}

# Tag expansion (search related tags together)
TAG_EXPAND = {
    "news": ["news", "talk", "information"],
    "talk": ["talk", "news", "spoken word"],
    "classical": ["classical", "classic", "orchestra", "symphony"],
    "jazz": ["jazz", "smooth jazz", "bebop", "swing"],
    "rock": ["rock", "classic rock", "alternative", "indie"],
    "pop": ["pop", "top 40", "hits", "charts"],
    "electronic": ["electronic", "edm", "techno", "house", "trance"],
    "lounge": ["lounge", "chillout", "ambient", "easy listening"],
    "hiphop": ["hiphop", "hip-hop", "rap", "urban"],
    "kpop": ["kpop", "k-pop", "korean pop"],
}

# Quality filters
QUALITY_MAP = {
    # Korean
    "high quality": {"min_bitrate": 192},
    "": {"max_bitrate": 96},
    "high quality": {"min_bitrate": 256},
    "hd": {"min_bitrate": 256},
    # English
    "high quality": {"min_bitrate": 192},
    "hq": {"min_bitrate": 192},
    "low quality": {"max_bitrate": 96},
    "lq": {"max_bitrate": 96},
    # Specific
    "128k": {"min_bitrate": 128, "max_bitrate": 160},
    "192k": {"min_bitrate": 192, "max_bitrate": 224},
    "256k": {"min_bitrate": 256, "max_bitrate": 320},
    "320k": {"min_bitrate": 320},
}

# Block list (load from blocklist.json)
BLOCK_LIST = []

def load_blocklist():
    """blocklist.json rock """
    global BLOCK_LIST
    paths = [
        os.path.join(_PKG_DIR, "blocklist.json"),               # pip install package embedded
        os.path.join(DATA_DIR, "blocklist.json"),                # ~/.radiocli/
        os.path.expanduser("~/RadioCli/blocklist.json"),         # dev environment
    ]
    for path in paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                BLOCK_LIST = [b["pattern"] for b in data.get("blocked", [])]
                return
            except Exception:
                pass

# Load blocklist on startup
load_blocklist()

def is_blocked(name):
    """ rock  """
    if not name:
        return False
    name_lower = name.lower()
    for blocked in BLOCK_LIST:
        if blocked.lower() in name_lower:
            return True
    return False

def _auto_install_player():
    """Try to auto-install mpv via brew (macOS) or apt (Linux)."""
    import platform
    sys_name = platform.system()
    try:
        if sys_name == "Darwin":
            if shutil.which("brew"):
                print("  mpv not found — installing via brew...")
                r = subprocess.run(["brew", "install", "mpv"],
                                   capture_output=False, timeout=120)
                return r.returncode == 0
            else:
                print("  mpv not found. Install with: brew install mpv")
        elif sys_name == "Linux":
            pkg_mgr = shutil.which("apt") or shutil.which("apt-get")
            if pkg_mgr:
                print("  mpv not found — installing via apt...")
                r = subprocess.run(
                    ["sudo", pkg_mgr, "install", "-y", "mpv"],
                    capture_output=False, timeout=120)
                return r.returncode == 0
            else:
                print("  mpv not found. Install with: sudo apt install mpv")
    except Exception as e:
        print(f"  Auto-install failed: {e}")
    return False


def get_player():
    for p in ["mpv", "ffplay", "vlc"]:
        if shutil.which(p):
            return p
    # Not found — try auto-install
    if _auto_install_player():
        for p in ["mpv", "ffplay", "vlc"]:
            if shutil.which(p):
                return p
    return None

# === LLM Integration ===
def llm_parse_query(query):
    """Parse natural language query with LLM → {"country": "KR", "tags": ["jazz"], "mood": "relaxing"}"""
    prompt = f"""User is searching for radio stations. Analyze the request and respond in JSON.

Request: "{query}"

Response format (JSON only, no explanation):
{{"country": "country code or null", "tags": ["genre tags"], "mood": "mood", "time_of_day": "time of day"}}

Examples:
- "Korean jazz" → {{"country": "KR", "tags": ["jazz"], "mood": null, "time_of_day": null}}
- "energetic music for commute" → {{"country": null, "tags": ["pop", "dance"], "mood": "energetic", "time_of_day": "morning"}}
- "relaxing classical before sleep" → {{"country": null, "tags": ["classical", "ambient"], "mood": "relaxing", "time_of_day": "night"}}

JSON response:"""

    result = None

    # 1. Ollama (local)
    if LLM_PROVIDER in ["auto", "ollama"]:
        result = call_ollama(prompt)
        if result:
            return result

    # 2. Claude API
    if LLM_PROVIDER in ["auto", "claude"] and ANTHROPIC_API_KEY:
        result = call_claude(prompt)
        if result:
            return result

    # 3. OpenAI API
    if LLM_PROVIDER in ["auto", "openai"] and OPENAI_API_KEY:
        result = call_openai(prompt)
        if result:
            return result

    return None

def call_ollama(prompt):
    """Ollama  LLM """
    try:
        data = json.dumps({
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1}
        }).encode()

        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"}
        )

        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            response_text = result.get("response", "")
            # Extract JSON
            return extract_json(response_text)
    except Exception as e:
        return None

def call_claude(prompt):
    """Claude API """
    try:
        data = json.dumps({
            "model": CLAUDE_MODEL,
            "max_tokens": 200,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=data,
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            }
        )

        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            response_text = result.get("content", [{}])[0].get("text", "")
            return extract_json(response_text)
    except Exception as e:
        return None

def call_openai(prompt):
    """OpenAI API """
    try:
        data = json.dumps({
            "model": OPENAI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 200,
            "temperature": 0.1
        }).encode()

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {OPENAI_API_KEY}"
            }
        )

        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            response_text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            return extract_json(response_text)
    except Exception as e:
        return None

def extract_json(text):
    """ JSON """
    try:
        # Find JSON block
        text = text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        # Find { }
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except:
        pass
    return None

def llm_search(query, limit=30):
    """LLM-based natural language search"""
    parsed = llm_parse_query(query)
    if not parsed:
        return None

    country = parsed.get("country")
    tags = parsed.get("tags", [])

    if country and tags:
        params = {
            "countrycode": country,
            "tag": tags[0],
            "limit": limit,
            "order": "votes",
            "reverse": "true"
        }
        return api_request("stations/search", params)
    elif tags:
        return search_by_tag(tags[0], limit)
    elif country:
        return search_by_country(country, limit)

    return None

# === Favorites ===
def load_favorites():
    try:
        with open(FAVORITES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

def save_favorites(favs):
    with open(FAVORITES_FILE, "w", encoding="utf-8") as f:
        json.dump(favs, f, ensure_ascii=False, indent=2)

# === Last Station ===
def save_last_station(station):
    """Save last played station"""
    if station:
        try:
            with open(LAST_STATION_FILE, "w", encoding="utf-8") as f:
                json.dump(station, f, ensure_ascii=False)
        except:
            pass

def load_last_station():
    """Load last played station"""
    if os.path.exists(LAST_STATION_FILE):
        try:
            with open(LAST_STATION_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return None

# === Listening History ===
def load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-500:], f, ensure_ascii=False, indent=2)  # Keep last 500

def add_history(station, duration_sec):
    """ rock """
    if duration_sec < 10:  # Ignore if less than 10 seconds
        return
    history = load_history()
    history.append({
        "name": station.get("name", ""),
        "url": station.get("url_resolved") or station.get("url", ""),
        "country": station.get("countrycode") or station.get("country", ""),
        "tags": station.get("tags", ""),
        "timestamp": datetime.now().isoformat(),
        "hour": datetime.now().hour,
        "weekday": datetime.now().weekday(),
        "duration": duration_sec
    })
    save_history(history)

def show_listening_history(limit=20):
    """ rock """
    history = load_history()
    if not history:
        print(f"  {t('no_history')}\n")
        return

    print(f"\n  {t('history')} ({len(history)}):\n")
    for i, h in enumerate(reversed(history[-limit:]), 1):
        name = h.get("name", "?")[:30]
        country = h.get("country", "")[:3]
        duration = h.get("duration", 0)
        mins = duration // 60
        timestamp = h.get("timestamp", "")[:10]
        print(f"  {i:2}. {name:<30} {country:>3} {mins:>3} ({timestamp})")
    print()

# === Taste Analysis ===
def analyze_preferences():
    """ rock   """
    history = load_history()
    if not history:
        return None

    # Tag frequency (weighted by listen time)
    tag_scores = Counter()
    country_scores = Counter()
    hour_tags = {}  # Preferred tags by time

    for h in history:
        weight = min(h.get("duration", 60) / 60, 10)  # Max 10min weight
        tags = h.get("tags", "").split(",")
        hour = h.get("hour", 12)

        for tag in tags:
            tag = tag.strip().lower()
            if tag:
                tag_scores[tag] += weight
                if hour not in hour_tags:
                    hour_tags[hour] = Counter()
                hour_tags[hour][tag] += weight

        country = h.get("country", "")
        if country:
            country_scores[country] += weight

    return {
        "top_tags": tag_scores.most_common(10),
        "top_countries": country_scores.most_common(5),
        "hour_preferences": {h: dict(tags.most_common(3)) for h, tags in hour_tags.items()},
        "total_listens": len(history),
        "total_minutes": sum(h.get("duration", 0) for h in history) // 60
    }

def get_mood_recommendations(limit=20):
    """/mood based """
    hour = datetime.now().hour
    weekday = datetime.now().weekday()  # 0=Mon, 6=Sun

    # Mood by time of day
    if 5 <= hour < 9:  # Early morning
        tags = ["classical", "ambient", "lofi"]
        mood = "morning "
    elif 9 <= hour < 12:  # Morning
        tags = ["pop", "jazz", "acoustic"]
        mood = " "
    elif 12 <= hour < 14:  # Lunch
        tags = ["lounge", "pop", "jazz"]
        mood = " "
    elif 14 <= hour < 18:  # Afternoon
        tags = ["pop", "rock", "electronic"]
        mood = "focus "
    elif 18 <= hour < 21:  # Evening
        tags = ["jazz", "soul", "lounge"]
        mood = " evening"
    elif 21 <= hour < 24:  # Night
        tags = ["ambient", "lounge", "classical"]
        mood = " night"
    else:  # Late night
        tags = ["ambient", "sleep", "classical"]
        mood = " "

    # More energetic on weekends
    if weekday >= 5:  # Sat/Sun
        tags = ["pop", "dance", "rock"] + tags
        mood += " ()"

    print(f"  {t('mood')}: {mood}")

    # Search multiple tags and merge
    all_results = []
    seen_urls = set()
    for tag in tags[:3]:
        results = search_by_tag(tag, limit)
        for s in results:
            url = s.get("url")
            if url not in seen_urls:
                seen_urls.add(url)
                all_results.append(s)
            if len(all_results) >= limit:
                break
        if len(all_results) >= limit:
            break

    return all_results[:limit]

def get_personalized_recommendations(limit=20):
    """ based items """
    prefs = analyze_preferences()
    if not prefs or not prefs["top_tags"]:
        return get_popular(limit)

    # Current time slot preferred tags
    current_hour = datetime.now().hour
    hour_prefs = prefs.get("hour_preferences", {})

    # Time slot matching (±2 hours)
    best_tags = []
    for h in range(current_hour - 2, current_hour + 3):
        h = h % 24
        if h in hour_prefs:
            best_tags.extend(hour_prefs[h].keys())

    # Use overall popular tags if no time slot tags
    if not best_tags:
        best_tags = [t[0] for t in prefs["top_tags"][:3]]

    # Search by most listened tags
    if best_tags:
        tag = best_tags[0]
        results = search_by_tag(tag, limit * 2)
        # Put already listened stations at end
        history_urls = {h.get("url") for h in load_history()}
        new_stations = [s for s in results if s.get("url") not in history_urls]
        old_stations = [s for s in results if s.get("url") in history_urls]
        return (new_stations + old_stations)[:limit]

    return get_popular(limit)

def show_my_taste():
    """  """
    prefs = analyze_preferences()
    if not prefs:
        print(f"\n  {t('no_history')}. {t('listen_first')}!\n")
        return

    print(f"\n  ═══ {t('taste_analysis')} ═══")
    print(f"  {t('total_listens').format(prefs['total_listens'], prefs['total_minutes'])}")

    print(f"\n  {t('fav_genres')}:")
    for tag, score in prefs["top_tags"][:5]:
        bar = "█" * min(int(score / 5), 20)
        print(f"    {tag:<15} {bar}")

    print(f"\n  {t('fav_countries')}:")
    for country, score in prefs["top_countries"][:3]:
        print(f"    {country}: {int(score)}{t('points')}")

    print()

def add_favorite(station):
    favs = load_favorites()
    # Duplicate check
    for f in favs:
        if f.get("url") == station.get("url"):
            return False
    favs.append({
        "name": station.get("name", ""),
        "url": station.get("url_resolved") or station.get("url", ""),
        "country": station.get("countrycode", ""),
        "tags": station.get("tags", ""),
        "bitrate": station.get("bitrate", 0)
    })
    save_favorites(favs)
    return True

def remove_favorite(idx):
    favs = load_favorites()
    if 0 <= idx < len(favs):
        removed = favs.pop(idx)
        save_favorites(favs)
        return removed
    return None

def print_favorites():
    favs = load_favorites()
    if not favs:
        print(f"\n  {t('no_fav')} (+ )\n")
        return []
    print(f"\n  {'#':<3} {t('station'):<30} {t('country'):<4} {t('genre'):<20} {t('quality'):<6}")
    print("  " + "-" * 70)
    for i, s in enumerate(favs, 1):
        name = s.get("name", "")[:28]
        country = s.get("country", "")[:3]
        tags = s.get("tags", "")[:18]
        bitrate = s.get("bitrate", 0)
        quality = f"{bitrate}k" if bitrate else ""
        print(f"  {i:<3} {name:<30} {country:<4} {tags:<20} {quality:<6}")
    print()
    return favs

def api_request(endpoint, params=None):
    url = f"{API_BASE}/{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "RadioCli/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  {t('error')}: {e}")
        return []

def save_station_to_db(station):
    """Save successfully played station to DB"""
    if not station or not os.path.exists(DB_PATH):
        return False

    # Check blocklist
    if is_blocked(station.get("name", "")):
        return False

    url = station.get("url_resolved") or station.get("url", "")
    if not url or "?" in url:  # Exclude token URLs
        return False

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO stations
            (stationuuid, name, url, url_resolved, country, countrycode,
             tags, bitrate, votes, clickcount, is_alive, fail_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
        """, (
            station.get("stationuuid", ""),
            station.get("name", ""),
            station.get("url", ""),
            station.get("url_resolved", ""),
            station.get("country", ""),
            station.get("countrycode", ""),
            station.get("tags", ""),
            station.get("bitrate", 0),
            station.get("votes", 0),
            station.get("clickcount", 0),
        ))
        conn.commit()
        conn.close()
        global _db_cache
        _db_cache = None
        return True
    except:
        return False

def merge_results(db_results, api_results, limit=30):
    """DB + API   ( ,  )"""
    seen = set()
    merged = []

    # DB first (verified)
    for s in db_results:
        if is_blocked(s.get("name", "")):
            continue
        url = s.get("url_resolved") or s.get("url", "")
        if url and url not in seen:
            seen.add(url)
            s["source"] = "db"
            merged.append(s)

    # Add API results
    for s in api_results:
        if is_blocked(s.get("name", "")):
            continue
        url = s.get("url_resolved") or s.get("url", "")
        if url and url not in seen:
            seen.add(url)
            s["source"] = "api"
            merged.append(s)

    return merged[:limit]

def search(query, limit=20):
    """DB + API search (USE_API=False DB)"""
    db_results = db_search(query=query, limit=limit)
    if not USE_API:
        return db_results[:limit]
    # Use unified search for Korean (/search)
    if any(ord(c) >= 0xAC00 and ord(c) <= 0xD7A3 for c in query):
        api_results = api_request("search", {"q": query, "limit": limit})
        if isinstance(api_results, dict) and "data" in api_results:
            api_results = api_results.get("data", [])
    else:
        api_results = api_request("stations/byname/" + urllib.parse.quote(query), {
            "limit": limit, 
        })
    return merge_results(db_results, api_results, limit)

def search_by_tag(tag, limit=20):
    """DB + API tag search"""
    db_results = db_search(tag=tag, limit=limit)
    if not USE_API:
        return db_results[:limit]
    api_results = api_request("stations/bytag/" + urllib.parse.quote(tag), {
        "limit": limit, 
    })
    return merge_results(db_results, api_results, limit)

def search_by_country(code, limit=20):
    """DB + API country search"""
    db_results = db_search(country=code, limit=limit)
    if not USE_API:
        return db_results[:limit]
    api_results = api_request("stations/bycountrycode/" + urllib.parse.quote(code.upper()), {
        "limit": limit, 
    })
    return merge_results(db_results, api_results, limit)

def get_popular(limit=20):
    """Popular stations (DB )"""
    if not USE_API:
        # DB by clickcount
        db_results = db_search(limit=limit)
        return sorted(db_results, key=lambda x: x.get("clickcount", 0), reverse=True)[:limit]
    return api_request("stations/toplisteners?limit=" + str(limit))

def get_top_voted(limit=20):
    """popular  """
    return api_request("stations/topvote/" + str(limit))

def get_high_quality(limit=30):
    """High quality stations (256kbps )"""
    params = {
        "bitrateMin": 256,
        "limit": limit,
        "order": "votes",
        "reverse": "true",
        
    }
    return api_request("stations/search", params)

def get_premium(limit=30):
    """Premium stations (high quality + popular) - metadata rich possibility """
    params = {
        "bitrateMin": 192,
        "order": "votes",
        "reverse": "true",
        "limit": limit,
        
    }
    results = api_request("stations/search", params)
    # Filter high votes only
    return [s for s in results if s.get("votes", 0) >= 100][:limit]

# Natural language -> tag mapping (mood, situation)
MOOD_MAP = {
    # Energetic/upbeat
    "": ["dance", "electronic", "pop"], "": ["dance", "electronic"],
    "": ["dance", "pop", "rock"], "": ["electronic", "dance"],
    "upbeat": ["dance", "pop"], "energetic": ["electronic", "rock"],
    "exciting": ["dance", "electronic"], "lively": ["pop", "dance"],
    # Relaxing/calm
    "": ["lounge", "ambient", "classical"], "": ["ambient", "classical", "piano"],
    "relaxing": ["lounge", "ambient"], "calm": ["classical", "ambient"],
    "peaceful": ["classical", "ambient"], "soothing": ["lounge", "piano"],
    "": ["classical", "ambient"], "": ["ambient", "nature", "classical"],
    # Sad/emotional
    "": ["ballad", "blues"], "": ["ballad", "soul", "jazz"],
    "": ["blues", "ambient"], "": ["blues", "classical"],
    "sad": ["blues", "ballad"], "emotional": ["soul", "ballad"],
    # Focus/study
    "focus": ["classical", "ambient", "lofi"], "study": ["classical", "lofi", "ambient"],
    "focus": ["classical", "ambient"], "study": ["lofi", "classical"],
    "work": ["lofi", "ambient"], "concentration": ["classical", "ambient"],
    # Sleep
    "sleep": ["ambient", "classical", "nature"], "": ["ambient", "sleep"],
    "sleep": ["ambient", "sleep", "nature"], "": ["ambient", "sleep"],
    # Workout
    "workout": ["electronic", "dance", "rock"], "workout": ["electronic", "dance"],
    "gym": ["electronic", "rock"], "exercise": ["dance", "electronic"],
    "": ["electronic", "dance"], "running": ["electronic", "dance"],
    # Morning/commute
    "morning": ["pop", "classical", "jazz"], "": ["pop", "news", "jazz"],
    "morning": ["pop", "classical"], "commute": ["news", "pop"],
    # Evening/night
    "evening": ["jazz", "lounge", "classical"], "night": ["lounge", "ambient", "jazz"],
    "evening": ["jazz", "lounge"], "night": ["lounge", "ambient"],
    # Party
    "": ["dance", "electronic", "pop"], "party": ["dance", "electronic"],
    "club": ["electronic", "dance"], "club": ["electronic", "dance"],
    # Romantic
    "": ["jazz", "ballad", "classical"], "romantic": ["jazz", "ballad"],
    "": ["ballad", "pop"], "love": ["ballad", "pop"],
    # Fast/slow
    "": ["electronic", "dance", "rock"], "fast": ["electronic", "dance"],
    "": ["ambient", "classical", "lounge"], "slow": ["ambient", "lounge"],
}

def natural_language_search(query, limit=30):
    """Analyze natural language query and search appropriate stations"""
    query_lower = query.lower()
    found_tags = []
    found_country = None

    # Find mood/situation keywords
    for keyword, tags in MOOD_MAP.items():
        if keyword in query_lower:
            found_tags.extend(tags)

    # Find country keywords
    for keyword, code in LANG_MAP.items():
        if keyword.lower() in query_lower:
            if len(code) == 2 and code.isupper():
                found_country = code
                break

    # Find genre keywords
    for keyword, val in LANG_MAP.items():
        if keyword.lower() in query_lower and not (len(val) == 2 and val.isupper()):
            found_tags.append(val)

    # If only country found, search by country
    if found_country and not found_tags:
        return search_by_country(found_country, limit)

    # If tag found, search by tag
    if found_tags:
        from collections import Counter
        tag_counts = Counter(found_tags)
        best_tag = tag_counts.most_common(1)[0][0]

        if found_country:
            params = {
                "countrycode": found_country,
                "tag": best_tag,
                "limit": limit,
                "order": "votes",
                "reverse": "true"
            }
            return api_request("stations/search", params)
        else:
            return search_by_tag(best_tag, limit)

    # If no keyword found, general search
    return None

def search_advanced(query, limit=50):
    """Smart search: country + genre +   """
    query_lower = query.lower().strip()

    country = None
    tags = []
    quality = {}  # e.g. {"min_bitrate": 192}
    name_parts = []

    # 1. Extract quality filters
    remaining = query_lower
    for phrase, q in sorted(QUALITY_MAP.items(), key=lambda x: -len(x[0])):
        if phrase.lower() in remaining:
            quality.update(q)
            remaining = remaining.replace(phrase.lower(), " ")

    # 2. Check multi-word mappings (e.g. "south korea", "hip hop")
    for phrase, val in sorted(LANG_MAP.items(), key=lambda x: -len(x[0])):
        phrase_lower = phrase.lower()
        if phrase_lower in remaining:
            if len(val) == 2 and val.isupper():  # Country code
                if not country:
                    country = val
            else:  # Genre
                if val not in tags:
                    tags.append(val)
            remaining = remaining.replace(phrase_lower, " ")

    # 3. Process remaining words
    words = remaining.split()
    for w in words:
        w = w.strip()
        if not w:
            continue
        # Direct 2-letter country code input
        if len(w) == 2 and w.upper().isalpha():
            if not country:
                country = w.upper()
        elif len(w) > 1:
            name_parts.append(w)

    # 4. Tag expansion (include related tags)
    expanded_tags = []
    for tag in tags:
        if tag in TAG_EXPAND:
            expanded_tags.extend(TAG_EXPAND[tag])
        else:
            expanded_tags.append(tag)
    # Remove duplicates, keep order
    seen = set()
    expanded_tags = [t for t in expanded_tags if not (t in seen or seen.add(t))]

    # 5. Execute search
    all_results = []

    # Country + tag combination
    if country and tags:
        if USE_API:
            params = {
                "countrycode": country,
                "tag": tags[0],
                "limit": limit,
                "order": "clickcount",
                "reverse": "true",
                
            }
            all_results = api_request("stations/search", params)
        else:
            # DB only: country + tag filter
            all_results = [s for s in db_search(country=country, limit=limit*2)
                          if tags[0].lower() in s.get("tags", "").lower()]
    elif country:
        all_results = search_by_country(country, limit)
    elif tags:
        all_results = search_by_tag(tags[0], limit)
    elif name_parts:
        all_results = search(" ".join(name_parts), limit)
    else:
        all_results = search(query, limit)

    # 6. Dedupe + blocklist filter (by URL)
    seen_urls = set()
    unique_results = []
    for s in all_results:
        if is_blocked(s.get("name", "")):
            continue
        url = s.get("url_resolved") or s.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_results.append(s)

    # 7. Apply quality filter (if results)
    if quality and unique_results:
        filtered = []
        for s in unique_results:
            bitrate = s.get("bitrate", 0)
            if bitrate:
                if "min_bitrate" in quality and bitrate < quality["min_bitrate"]:
                    continue
                if "max_bitrate" in quality and bitrate > quality["max_bitrate"]:
                    continue
            filtered.append(s)
        # Use filter results if available, else keep original
        if filtered:
            unique_results = filtered
        else:
            # If quality filter yields no results, just sort (high bitrate first)
            pass

    # 8. Sort: bitrate desc → votes desc
    unique_results.sort(key=lambda x: (x.get("bitrate", 0), x.get("votes", 0)), reverse=True)

    return unique_results[:limit]

def print_stations(stations):
    if not stations:
        print(f"\n  {t('no_results')}\n")
        return
    print(f"\n  {'#':<3} {t('station'):<30} {t('country'):<4} {t('genre'):<20} {t('quality'):<6}")
    print("  " + "-" * 70)
    for i, s in enumerate(stations, 1):
        name = s.get("name", "")[:28]
        country = s.get("countrycode", "")[:3]
        tags = s.get("tags", "")[:18]
        bitrate = s.get("bitrate", 0)
        quality = f"{bitrate}k" if bitrate else ""
        print(f"  {i:<3} {name:<30} {country:<4} {tags:<20} {quality:<6}")
    print()
    print(f"  {t('press_num')} | m={t('menu')} | g={t('genre')} | c={t('country')} | /{t('searching')}")

def get_fresh_url(name):
    """Get fresh URL from API by station name"""
    if not name:
        return None
    results = api_request("stations/byname/" + urllib.parse.quote(name), {
        "limit": 5, 
    })
    for s in results:
        if s.get("name", "").lower() == name.lower():
            return s.get("url_resolved") or s.get("url")
    # If no exact match, return first result
    if results:
        return results[0].get("url_resolved") or results[0].get("url")
    return None

def update_station_url(old_url, new_url):
    """Update station URL in DB"""
    if not os.path.exists(DB_PATH) or not new_url:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE stations SET url_resolved = ?, fail_count = 0, is_alive = 1
            WHERE url = ? OR url_resolved = ?
        """, (new_url, old_url, old_url))
        conn.commit()
        conn.close()
        global _db_cache
        _db_cache = None
    except:
        pass

def play(url, name="", use_fresh_url=True):
    """
    Play radio
    use_fresh_url=True: API  URL  fetched (token expiration handling)
    """
    global PLAYER_PROC
    stop()
    if not PLAYER:
        print(f"  {t('no_player')}. brew install mpv")
        return False

    # Get fresh URL from API (only if API mode enabled)
    play_url = url
    if USE_API and use_fresh_url and name:
        fresh_url = get_fresh_url(name)
        if fresh_url:
            play_url = fresh_url
            if fresh_url != url:
                print(f"  ↻  URL ")
                update_station_url(url, fresh_url)

    print(f"\n  ▶ {t('playing')}: {name}")
    print(f"    {play_url[:70]}{'...' if len(play_url) > 70 else ''}")
    print(f"    (n: {t('view_current')})\n")

    try:
        # Kill existing mpv (shared MCP/CLI)
        kill_existing_mpv()

        if PLAYER == "mpv":
            PLAYER_PROC = subprocess.Popen(
                ["mpv", "--no-video", "--really-quiet",
                 "--cache=yes",
                 "--cache-secs=30",
                 "--demuxer-max-bytes=50M",
                 "--demuxer-readahead-secs=20",
                 "--stream-buffer-size=1M",
                 "--network-timeout=30",
                 "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=5",
                 f"--input-ipc-server={MPV_SOCKET}", play_url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                preexec_fn=os.setpgrp  # Separate process group
            )
            # Save PID file (shared MCP/CLI)
            with open(MPV_PID_FILE, 'w') as f:
                f.write(str(PLAYER_PROC.pid))
        elif PLAYER == "ffplay":
            PLAYER_PROC = subprocess.Popen(
                ["ffplay", "-nodisp", "-loglevel", "quiet", play_url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        elif PLAYER == "vlc":
            PLAYER_PROC = subprocess.Popen(
                ["vlc", "--intf", "dummy", play_url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )

        # Wait 2 sec and check if process alive
        time.sleep(2)
        if PLAYER_PROC and PLAYER_PROC.poll() is not None:
            # Process terminated = playback failed
            mark_station_failed(url)
            print(f"  ✗ Playback failed. Stream ended or unavailable.")
            return False

        # Start song monitoring
        start_song_monitor(name)
        return True

    except Exception as e:
        print(f"  {t('play_error')}: {e}")
        return False

# Ad/filter keywords
AD_KEYWORDS = [
    "advertisement", "advertising", "commercial", "werbung", "publicité",
    "", "公告", "広告", "reklam", "anuncio", "pubblicità",
    "ad break", "spot", "promo", "jingle", "station id", "station identification",
    "news", "news", "weather", "", "traffic", "",
]

def is_advertisement(title):
    """/rain   """
    if not title:
        return False
    title_lower = title.lower()
    for kw in AD_KEYWORDS:
        if kw.lower() in title_lower:
            return True
    return False

def get_current_song():
    """Get current playing song info fetch (mpv IPC)"""
    if PLAYER != "mpv" or not os.path.exists(MPV_SOCKET):
        return None

    try:
        import socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(MPV_SOCKET)

        # Get icy-title (radio stream metadata)
        cmd = '{"command": ["get_property", "media-title"]}\n'
        sock.send(cmd.encode())
        response = sock.recv(4096).decode()
        sock.close()

        data = json.loads(response)
        title = data.get("data", "")
        if title:
            # Ad filtering
            if is_advertisement(title):
                return {"title": title, "is_ad": True}
            return {"title": title, "is_ad": False}
    except Exception as e:
        pass

    return None

def show_current_song():
    """ song """
    if not PLAYER_PROC:
        print(f"  {t('no_playing')}\n")
        return

    song = get_current_song()
    if song and song.get("title"):
        if song.get("is_ad"):
            print(f"\n  📢 {t('ad_playing')}: {song['title']}\n")
        else:
            print(f"\n  ♪ {t('current_song')}: {song['title']}\n")
    else:
        print(f"\n  {t('no_song_info')}\n")

# === Song History ===
_last_song_title = None

def load_songs():
    """song rock """
    if os.path.exists(SONGS_FILE):
        try:
            with open(SONGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return []

def save_songs(songs):
    """song rock storage"""
    with open(SONGS_FILE, "w", encoding="utf-8") as f:
        json.dump(songs[-1000:], f, ensure_ascii=False, indent=2)  # Max 1000 songs

def parse_song_info(raw_title):
    """'Artist - Title'  """
    if not raw_title:
        return None, None
    if " - " in raw_title:
        parts = raw_title.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    return None, raw_title.strip()

def add_song_to_history(raw_title, station_name):
    """song rock """
    global _last_song_title
    if not raw_title or raw_title == _last_song_title:
        return
    _last_song_title = raw_title

    artist, title = parse_song_info(raw_title)
    songs = load_songs()
    songs.append({
        "artist": artist or "",
        "title": title or raw_title,
        "station": station_name,
        "timestamp": datetime.now().isoformat(),
        "raw": raw_title
    })
    save_songs(songs)

def check_song_change(station_name):
    """song  detect rock"""
    song = get_current_song()
    if song and song.get("title") and not song.get("is_ad"):
        add_song_to_history(song["title"], station_name)

def show_song_history(limit=20):
    """song rock """
    songs = load_songs()
    if not songs:
        print(f"\n  song rock \n")
        return
    print(f"\n    song ({len(songs)}items  {min(limit, len(songs))}items)")
    print(f"  {'Time':<6} {'Station':<20} {'Artist':<20} {'Song':<25}")
    print("  " + "-" * 75)
    for s in reversed(songs[-limit:]):
        ts = s.get("timestamp", "")
        time_str = ts[11:16] if ts else ""
        station = s.get("station", "")[:18]
        artist = s.get("artist", "-")[:18]
        title = s.get("title", "")[:23]
        print(f"  {time_str:<6} {station:<20} {artist:<20} {title:<25}")
    print()

# Song monitoring settings
_song_monitor_thread = None
_song_monitor_running = False
SONG_MONITOR_ENABLED = True  # Song record on/off

def start_song_monitor(station_name):
    """song    ()"""
    global _song_monitor_thread, _song_monitor_running
    if not SONG_MONITOR_ENABLED:
        return

    def monitor():
        while _song_monitor_running and PLAYER_PROC:
            check_song_change(station_name)
            time.sleep(10)  # Check every 10 seconds

    _song_monitor_running = True
    _song_monitor_thread = threading.Thread(target=monitor, daemon=True)
    _song_monitor_thread.start()

def stop_song_monitor():
    """song  """
    global _song_monitor_running
    _song_monitor_running = False

def clear_song_history():
    """song rock  """
    if os.path.exists(SONGS_FILE):
        os.remove(SONGS_FILE)
    global _last_song_title
    _last_song_title = None
    print("  song rock \n")

# === DJ Feature (TTS) ===
DJ_ENABLED = os.environ.get("RADIOCLI_DJ", "0") == "1"
TTS_AUDIO_FILE = os.path.join(DATA_DIR, "tts_output.mp3")

# Voice and DJ comments by language
DJ_LANGUAGES = {
    "ko": {
        "voice": "ko-KR-SunHiNeural",
        "station_intros": [
            ",  {name} ?",
            "{name}.   .",
            " {name}! .",
            "{tags}   {name}.",
        ],
        "song_intros": [
            "  song {artist} {song}.",
            "{artist}, {song}  .",
            "{song}, {artist}.",
        ],
        "song_intros_no_artist": [
            "  song {title}.",
            "{title}  .",
        ],
    },
    "en": {
        "voice": "en-US-JennyNeural",
        "station_intros": [
            "Now let's go to {name}!",
            "This is {name}. Enjoy the music!",
            "Next up, {name}!",
            "Welcome to {name}, your {tags} station.",
        ],
        "song_intros": [
            "Now playing: {song} by {artist}.",
            "You're listening to {artist} with {song}.",
            "That was {song} by {artist}.",
        ],
        "song_intros_no_artist": [
            "Now playing: {title}.",
            "You're listening to {title}.",
        ],
    },
    "ja": {
        "voice": "ja-JP-NanamiNeural",
        "station_intros": [
            "さあ、{name}に行きましょう！",
            "{name}です。音楽をお楽しみください。",
            "次は{name}です！",
            "{tags}音楽いっぱいの{name}です。",
        ],
        "song_intros": [
            "今流れているのは{artist}の{song}です。",
            "{artist}で{song}をお聴きいただいています。",
            "{song}、{artist}でした。",
        ],
        "song_intros_no_artist": [
            "今流れているのは{title}です。",
            "{title}をお聴きいただいています。",
        ],
    },
    "fr": {
        "voice": "fr-FR-DeniseNeural",
        "station_intros": [
            "Allons maintenant sur {name}!",
            "Voici {name}. Profitez de la musique!",
            "Et maintenant, {name}!",
            "Bienvenue sur {name}, votre station {tags}.",
        ],
        "song_intros": [
            "Vous écoutez {song} par {artist}.",
            "C'était {song} de {artist}.",
            "{artist} avec {song}.",
        ],
        "song_intros_no_artist": [
            "Vous écoutez {title}.",
            "C'était {title}.",
        ],
    },
    "de": {
        "voice": "de-DE-KatjaNeural",
        "station_intros": [
            "Jetzt geht's zu {name}!",
            "Das ist {name}. Genießen Sie die Musik!",
            "Als nächstes: {name}!",
            "Willkommen bei {name}, Ihr {tags} Sender.",
        ],
        "song_intros": [
            "Jetzt läuft: {song} von {artist}.",
            "Sie hören {artist} mit {song}.",
            "Das war {song} von {artist}.",
        ],
        "song_intros_no_artist": [
            "Jetzt läuft: {title}.",
            "Sie hören {title}.",
        ],
    },
    "es": {
        "voice": "es-ES-ElviraNeural",
        "station_intros": [
            "¡Ahora vamos a {name}!",
            "Esto es {name}. ¡Disfruta la música!",
            "¡A continuación, {name}!",
            "Bienvenido a {name}, tu estación de {tags}.",
        ],
        "song_intros": [
            "Ahora suena: {song} de {artist}.",
            "Estás escuchando {artist} con {song}.",
            "Eso fue {song} de {artist}.",
        ],
        "song_intros_no_artist": [
            "Ahora suena: {title}.",
            "Estás escuchando {title}.",
        ],
    },
    "zh": {
        "voice": "zh-CN-XiaoxiaoNeural",
        "station_intros": [
            "现在让我们去{name}！",
            "这里是{name}，请欣赏音乐！",
            "接下来是{name}！",
            "欢迎来到{name}，您的{tags}电台。",
        ],
        "song_intros": [
            "正在播放：{artist}的{song}。",
            "您正在收听{artist}的{song}。",
            "刚才播放的是{artist}的{song}。",
        ],
        "song_intros_no_artist": [
            "正在播放：{title}。",
            "您正在收听{title}。",
        ],
    },
    "pt": {
        "voice": "pt-BR-FranciscaNeural",
        "station_intros": [
            "Agora vamos para {name}!",
            "Esta é {name}. Aproveite a música!",
            "A seguir, {name}!",
            "Bem-vindo à {name}, sua estação de {tags}.",
        ],
        "song_intros": [
            "Tocando agora: {song} de {artist}.",
            "Você está ouvindo {artist} com {song}.",
            "Essa foi {song} de {artist}.",
        ],
        "song_intros_no_artist": [
            "Tocando agora: {title}.",
            "Você está ouvindo {title}.",
        ],
    },
    "ru": {
        "voice": "ru-RU-SvetlanaNeural",
        "station_intros": [
            "А теперь переходим на {name}!",
            "Это {name}. Наслаждайтесь музыкой!",
            "Далее - {name}!",
            "Добро пожаловать на {name}.",
        ],
        "song_intros": [
            "Сейчас играет: {song} от {artist}.",
            "Вы слушаете {artist} с песней {song}.",
            "Это была {song} от {artist}.",
        ],
        "song_intros_no_artist": [
            "Сейчас играет: {title}.",
            "Вы слушаете {title}.",
        ],
    },
    "it": {
        "voice": "it-IT-ElsaNeural",
        "station_intros": [
            "Ora andiamo su {name}!",
            "Questa è {name}. Godetevi la musica!",
            "E ora, {name}!",
            "Benvenuti su {name}, la vostra stazione {tags}.",
        ],
        "song_intros": [
            "In onda ora: {song} di {artist}.",
            "State ascoltando {artist} con {song}.",
            "Quella era {song} di {artist}.",
        ],
        "song_intros_no_artist": [
            "In onda ora: {title}.",
            "State ascoltando {title}.",
        ],
    },
}

# Country code -> language mapping
COUNTRY_TO_LANG = {
    "KR": "ko", "KP": "ko",
    "US": "en", "GB": "en", "AU": "en", "CA": "en", "NZ": "en", "IE": "en",
    "JP": "ja",
    "FR": "fr", "BE": "fr", "CH": "fr",
    "DE": "de", "AT": "de",
    "ES": "es", "MX": "es", "AR": "es", "CO": "es", "CL": "es",
    "CN": "zh", "TW": "zh", "HK": "zh",
    "BR": "pt", "PT": "pt",
    "RU": "ru", "UA": "ru",
    "IT": "it",
}

def get_dj_language(station):
    """Determine DJ language by station country"""
    country = station.get("countrycode") or station.get("country", "")
    return COUNTRY_TO_LANG.get(country.upper(), "en")  # Default: English

# === Song Recognition (Shazam-like) ===
def load_recognized_songs():
    try:
        with open(RECOGNIZED_SONGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

def save_recognized_songs(songs):
    with open(RECOGNIZED_SONGS_FILE, "w", encoding="utf-8") as f:
        json.dump(songs[-100:], f, ensure_ascii=False, indent=2)  # Last 100 songs

def record_stream(url, duration=10):
    """stream   (ffmpeg )"""
    if not shutil.which("ffmpeg"):
        print(f"  {t('ffmpeg_needed')}: brew install ffmpeg")
        return False

    try:
        # Delete existing file
        if os.path.exists(RECORD_FILE):
            os.remove(RECORD_FILE)

        print(f"  {t('recording')}... ({duration}s)")
        result = subprocess.run(
            ["ffmpeg", "-y", "-t", str(duration), "-i", url,
             "-ac", "1", "-ar", "16000", "-acodec", "libmp3lame",
             "-loglevel", "quiet", RECORD_FILE],
            timeout=duration + 10
        )
        return os.path.exists(RECORD_FILE)
    except Exception as e:
        print(f"  {t('record_error')}: {e}")
        return False

def recognize_with_whisper(audio_file):
    """Whisper DJ  recognition (,  )"""
    # Requires whisper or mlx-whisper
    try:
        # Try mlx-whisper (Apple Silicon optimized)
        result = subprocess.run(
            ["mlx_whisper", audio_file, "--language", "auto", "--output-format", "json"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            # Parse results
            text = result.stdout.strip()
            if text:
                return {"transcription": text, "method": "mlx-whisper"}
    except:
        pass

    try:
        # Try openai-whisper
        result = subprocess.run(
            ["whisper", audio_file, "--language", "auto", "--output_format", "txt"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            txt_file = audio_file.rsplit(".", 1)[0] + ".txt"
            if os.path.exists(txt_file):
                with open(txt_file, "r") as f:
                    text = f.read().strip()
                os.remove(txt_file)
                return {"transcription": text, "method": "whisper"}
    except:
        pass

    return None

def parse_song_from_text(text):
    """ song   (LLM )"""
    if not text:
        return None

    # Try parsing with LLM
    prompt = f""" radio DJ  song  .
: "{text}"

JSON  :
{{"title": "song", "artist": ""}}
 : {{"title": null, "artist": null}}"""

    parsed = llm_parse_query(prompt)  # Reuse existing LLM function
    if parsed and parsed.get("title"):
        return parsed
    return None

def recognize_song(station=None):
    """Recognize current playing song ( )"""
    if not PLAYER_PROC:
        print(f"  {t('no_playing')}\n")
        return None

    url = None
    if station:
        url = station.get("url_resolved") or station.get("url")

    if not url:
        print(f"  {t('no_stream')}\n")
        return None

    # 1. Check ICY metadata first (fastest)
    song = get_current_song()
    if song and song.get("title"):
        if song.get("is_ad"):
            print(f"\n  📢 {t('ad_playing')}\n")
            return None
        print(f"\n  🎵 {t('metadata')}: {song['title']}\n")
        # Use already saved info
        result = {"title": song["title"], "method": "metadata"}
        if " - " in song["title"]:
            parts = song["title"].split(" - ", 1)
            result["artist"] = parts[0].strip()
            result["title"] = parts[1].strip()
        save_song_result(result, station)
        return result

    # 2. Record and recognize with Whisper
    print(f"  {t('no_metadata')}. {t('analyzing')}...")

    if not record_stream(url, duration=12):
        return None

    # Try Whisper (DJ speech recognition)
    if shutil.which("whisper") or shutil.which("mlx_whisper"):
        print(f"  {t('recognizing')} (Whisper)...")
        whisper_result = recognize_with_whisper(RECORD_FILE)
        if whisper_result and whisper_result.get("transcription"):
            print(f"  DJ: \"{whisper_result['transcription'][:100]}...\"")
            # Extract song info with LLM
            parsed = parse_song_from_text(whisper_result["transcription"])
            if parsed and parsed.get("title"):
                parsed["method"] = "whisper+llm"
                save_song_result(parsed, station)
                print(f"\n  🎵 {t('result')}:")
                print(f"     {t('title_label')}: {parsed.get('title', '?')}")
                print(f"     {t('artist')}: {parsed.get('artist', '?')}\n")
                return parsed

    print(f"  {t('no_results')}")
    print(f"  {t('tip')}: pip install openai-whisper\n")
    return None

def save_song_result(result, station):
    """recognition  storage ( )"""
    # Do not save if ad
    title = result.get("title", "")
    if is_advertisement(title):
        return

    songs = load_recognized_songs()
    result["recognized_at"] = datetime.now().isoformat()
    result["station"] = station.get("name", "") if station else ""
    songs.append(result)
    save_recognized_songs(songs)

def recognize_song_whisper(station=None):
    """Whisper  """
    if not PLAYER_PROC:
        print(f"  {t('no_playing')}\n")
        return None

    url = station.get("url_resolved") or station.get("url") if station else None
    if not url:
        print(f"  {t('no_stream')}\n")
        return None

    if not shutil.which("whisper") and not shutil.which("mlx_whisper"):
        print(f"  {t('whisper_needed')}: pip install openai-whisper\n")
        return None

    print(f"  [Whisper {t('testing')}] {t('recording')}...")
    if not record_stream(url, duration=15):
        return None

    print(f"  {t('recognizing')} ({t('takes_time')})...")
    result = recognize_with_whisper(RECORD_FILE)

    if result and result.get("transcription"):
        print(f"\n  🎤 {t('voice_result')}:")
        print(f"     \"{result['transcription'][:200]}\"")

        # Try extracting song info with LLM
        parsed = parse_song_from_text(result["transcription"])
        if parsed and parsed.get("title"):
            print(f"\n  🎵 {t('extracted_info')}:")
            print(f"     {t('title_label')}: {parsed.get('title', '?')}")
            print(f"     {t('artist')}: {parsed.get('artist', '?')}\n")
            parsed["method"] = "whisper+llm"
            save_song_result(parsed, station)
            return parsed
        else:
            print(f"  {t('extract_failed')}\n")
    else:
        print(f"  Whisper: {t('recognition_failed')}\n")

    return None

def show_recognized_songs():
    """recognition song rock """
    songs = load_recognized_songs()
    if not songs:
        print(f"\n  {t('no_recognized')} (i)\n")
        return

    print(f"\n  ═══ {t('recognized_list')} ({len(songs)} {t('songs')}) ═══")
    for i, s in enumerate(reversed(songs[-20:]), 1):
        title = s.get("title", "?")[:25]
        artist = s.get("artist", "?")[:20]
        station = s.get("station", "")[:15]
        print(f"  {i:2}. {title:<25} - {artist:<20} ({station})")
    print()

def mpv_command(cmd):
    """mpv IPC  """
    if not os.path.exists(MPV_SOCKET):
        return False
    try:
        import socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(MPV_SOCKET)
        sock.send((json.dumps({"command": cmd}) + "\n").encode())
        sock.close()
        return True
    except:
        return False

def mpv_get_property(prop):
    """mpv IPC  fetch"""
    if not os.path.exists(MPV_SOCKET):
        return None
    try:
        import socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(MPV_SOCKET)
        sock.send((json.dumps({"command": ["get_property", prop]}) + "\n").encode())
        response = sock.recv(1024).decode()
        sock.close()
        data = json.loads(response)
        return data.get("data")
    except:
        return None

# ============================================================
# Volume control
# ============================================================
def set_volume(level):
    """Set volume (0-100)"""
    if PLAYER != "mpv":
        print(f"  {t('volume_mpv_only')}\n")
        return False
    if not 0 <= level <= 100:
        print(f"  {t('volume_range')}\n")
        return False
    if mpv_command(["set_property", "volume", level]):
        print(f"  🔊 {t('volume_label')}: {level}%\n")
        return True
    return False

def get_volume():
    """Get current volume fetch"""
    if PLAYER != "mpv":
        return None
    return mpv_get_property("volume")

def volume_up(step=10):
    """Volume up"""
    vol = get_volume()
    if vol is not None:
        new_vol = min(100, int(vol) + step)
        set_volume(new_vol)

def volume_down(step=10):
    """Volume down"""
    vol = get_volume()
    if vol is not None:
        new_vol = max(0, int(vol) - step)
        set_volume(new_vol)

def show_volume():
    """Get current volume """
    vol = get_volume()
    if vol is not None:
        print(f"  🔊 {t('volume_label')}: {int(vol)}%\n")
    else:
        print(f"  {t('volume_error')}\n")

# ============================================================
# Station status check
# ============================================================
def check_station_url(url):
    """ URL  """
    try:
        req = urllib.request.Request(url, method='HEAD', headers={
            'User-Agent': 'RadioCli/1.0'
        })
        response = urllib.request.urlopen(req, timeout=10)
        content_type = response.headers.get('Content-Type', '')
        is_audio = "audio" in content_type.lower() or "mpegurl" in content_type.lower()
        return {"status": "alive", "content_type": content_type, "is_audio": is_audio}
    except urllib.error.HTTPError as e:
        return {"status": "dead", "error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"status": "dead", "error": str(e.reason)}
    except Exception as e:
        return {"status": "error", "error": str(e)}

def check_current_station(station):
    """Check current station status"""
    if not station:
        print(f"  {t('no_playing')}\n")
        return
    url = station.get("url_resolved") or station.get("url")
    print(f"  {t('checking')}: {station.get('name', '')}...")
    result = check_station_url(url)
    if result["status"] == "alive":
        if result.get("is_audio"):
            print(f"  ✓ {t('station_alive')}\n")
        else:
            print(f"  ⚠ {t('station_response')} ({result.get('content_type', '')})\n")
    else:
        print(f"  ✗ {t('station_dead')}: {result.get('error', '')}\n")

# ============================================================
# Station sharing
# ============================================================
def share_station(station):
    """Get station share info"""
    if not station:
        print(f"  {t('no_playing')}\n")
        return
    name = station.get("name", "")
    url = station.get("url_resolved") or station.get("url", "")
    tags = station.get("tags", "")
    country = station.get("country", "")
    homepage = station.get("homepage", "")

    print(f"\n  📻 {name}")
    print(f"  ├─ {t('share_url')}: {url}")
    if tags:
        print(f"  ├─ {t('genre')}: {tags}")
    if country:
        print(f"  ├─ {t('country')}: {country}")
    if homepage:
        print(f"  ├─ {t('share_homepage')}: {homepage}")
    print(f"  └─ {t('share_label')}: 🎵 {name} - {tags}")
    print()

# ============================================================
# Sleep timer / Alarm
# ============================================================
_sleep_timer = None

def set_sleep_timer(minutes):
    """Set sleep timer"""
    global _sleep_timer

    if minutes <= 0:
        if _sleep_timer:
            _sleep_timer.cancel()
            _sleep_timer = None
            print(f"  ⏰ {t('sleep_timer_off')}\n")
        else:
            print(f"  {t('sleep_timer_not_set')}\n")
        return

    # Cancel existing timer
    if _sleep_timer:
        _sleep_timer.cancel()

    def auto_stop():
        global _sleep_timer
        stop()
        print(f"\n  💤 {t('sleep_timer_stopped')}\n")
        _sleep_timer = None

    _sleep_timer = threading.Timer(minutes * 60, auto_stop)
    _sleep_timer.start()
    print(f"  ⏰ {t('sleep_timer_set')}: {minutes}{t('minutes')}\n")

def show_sleep_timer():
    """Sleep timer status"""
    if _sleep_timer and _sleep_timer.is_alive():
        print(f"  ⏰ {t('sleep_timer_active')}\n")
    else:
        print(f"  {t('sleep_timer_not_set')}\n")

_alarm_timer = None

def set_alarm(hour, minute=0, station_query="pop"):
    """Set alarm"""
    global _alarm_timer

    if _alarm_timer:
        _alarm_timer.cancel()

    now = datetime.now()
    alarm_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if alarm_time <= now:
        alarm_time = alarm_time.replace(day=alarm_time.day + 1)

    delay = (alarm_time - now).total_seconds()

    def alarm_play():
        global _alarm_timer
        print(f"\n  ⏰ {t('alarm_triggered')}\n")
        # Search popular stations and play
        stations = search_by_tag(station_query, 5)
        if stations:
            s = stations[0]
            url = s.get("url_resolved") or s.get("url")
            play(url, s.get("name", ""))
        _alarm_timer = None

    _alarm_timer = threading.Timer(delay, alarm_play)
    _alarm_timer.start()
    print(f"  ⏰ {t('alarm_set')}: {alarm_time.strftime('%H:%M')} ({station_query})\n")

def cancel_alarm():
    """Cancel alarm"""
    global _alarm_timer
    if _alarm_timer:
        _alarm_timer.cancel()
        _alarm_timer = None
        print(f"  ⏰ {t('alarm_cancelled')}\n")
    else:
        print(f"  {t('alarm_not_set')}\n")

def pause_radio():
    """radio """
    mpv_command(["set_property", "pause", True])

def resume_radio():
    """radio items"""
    mpv_command(["set_property", "pause", False])

def speak(text, voice=None, pause_radio_playback=True):
    """TTS  (Edge TTS)"""
    voice = voice or TTS_VOICE
    try:
        # 1. Pause if playing radio
        radio_was_playing = PLAYER_PROC is not None and pause_radio_playback
        if radio_was_playing:
            pause_radio()
            time.sleep(0.3)

        # 2. Generate speech with edge-tts
        subprocess.run(
            ["edge-tts", "--voice", voice, "--text", text, "--write-media", TTS_AUDIO_FILE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10
        )

        # 3. Play TTS (wait until complete)
        if shutil.which("afplay"):
            subprocess.run(["afplay", TTS_AUDIO_FILE],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("mpv"):
            subprocess.run(["mpv", "--no-video", "--really-quiet", TTS_AUDIO_FILE],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("ffplay"):
            subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", TTS_AUDIO_FILE],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 4. Resume radio
        if radio_was_playing:
            time.sleep(0.2)
            resume_radio()

        return True
    except Exception as e:
        if radio_was_playing and PLAYER_PROC:
            resume_radio()
        return False

def dj_announce_station(station):
    """broadcast countries items - country DJ"""
    if not DJ_ENABLED:
        return

    import random

    name = station.get("name", "")
    tags = station.get("tags", "").split(",")[0] if station.get("tags") else "music"

    # Determine language
    lang = get_dj_language(station)
    lang_data = DJ_LANGUAGES.get(lang, DJ_LANGUAGES["en"])

    # Select and format template
    template = random.choice(lang_data["station_intros"])
    text = template.format(name=name, tags=tags)

    speak(text, voice=lang_data["voice"])

def dj_announce_song(title, station=None):
    """ song items - country DJ"""
    if not DJ_ENABLED or not title:
        return

    import random

    # Determine language
    lang = "en"  # Default
    if station:
        lang = get_dj_language(station)
    lang_data = DJ_LANGUAGES.get(lang, DJ_LANGUAGES["en"])

    # Parse artist - title format
    if " - " in title:
        parts = title.split(" - ", 1)
        artist, song = parts[0].strip(), parts[1].strip()
        template = random.choice(lang_data["song_intros"])
        text = template.format(artist=artist, song=song)
    else:
        template = random.choice(lang_data["song_intros_no_artist"])
        text = template.format(title=title)

    speak(text, voice=lang_data["voice"])

def toggle_dj():
    """DJ  """
    global DJ_ENABLED
    DJ_ENABLED = not DJ_ENABLED
    if DJ_ENABLED:
        print(f"  🎙 {t('dj_on')}")
        speak(t('dj_on_speak'))
    else:
        print(f"  🎙 {t('dj_off')}")
    print()

# === Playlists ===
PLAYLIST_FILE = os.path.join(DATA_DIR, "playlists.json")

def load_playlists():
    try:
        with open(PLAYLIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_playlists(playlists):
    with open(PLAYLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(playlists, f, ensure_ascii=False, indent=2)

def create_smart_playlist(name, criteria):
    """  """
    stations = []

    if criteria == "favorites":
        # Based on favorites
        stations = load_favorites()
    elif criteria == "history":
        # Based on recent listens (deduped)
        history = load_history()
        seen = set()
        for h in reversed(history):
            url = h.get("url")
            if url and url not in seen:
                seen.add(url)
                stations.append(h)
            if len(stations) >= 20:
                break
    elif criteria == "mood":
        # Based on current mood
        stations = get_mood_recommendations(20)
    elif criteria == "ai":
        # AI recommendation based
        stations = get_personalized_recommendations(20)
    elif criteria.startswith("tag:"):
        # Specific tag
        tag = criteria[4:]
        stations = search_by_tag(tag, 20)
    elif criteria.startswith("country:"):
        # Specific country
        code = criteria[8:]
        stations = search_by_country(code, 20)

    if stations:
        playlists = load_playlists()
        playlists[name] = {
            "created": datetime.now().isoformat(),
            "criteria": criteria,
            "stations": stations[:20]
        }
        save_playlists(playlists)
        return len(stations)
    return 0

def show_playlists():
    """ rock"""
    playlists = load_playlists()
    if not playlists:
        print(f"\n  {t('no_playlist')}")
        print(f"  ({t('create_pl')}: pl name type)")
        print(f"  {t('pl_types')}: favorites, history, mood, ai, tag:jazz, country:KR")
        print()
        return None

    print(f"\n  ═══ {t('playlist')} ═══")
    for i, (name, pl) in enumerate(playlists.items(), 1):
        count = len(pl.get("stations", []))
        criteria = pl.get("criteria", "")
        print(f"  {i}. {name} ({count} {t('songs')}) - {criteria}")
    print()
    return list(playlists.keys())

def get_playlist_stations(name):
    """ broadcast countries rock"""
    playlists = load_playlists()
    if name in playlists:
        return playlists[name].get("stations", [])
    # Access by number
    try:
        idx = int(name) - 1
        keys = list(playlists.keys())
        if 0 <= idx < len(keys):
            return playlists[keys[idx]].get("stations", [])
    except:
        pass
    return []

def delete_playlist(name):
    """ """
    playlists = load_playlists()
    # Delete by number
    try:
        idx = int(name) - 1
        keys = list(playlists.keys())
        if 0 <= idx < len(keys):
            name = keys[idx]
    except:
        pass

    if name in playlists:
        del playlists[name]
        save_playlists(playlists)
        return True
    return False

def stop():
    global PLAYER_PROC
    stop_song_monitor()  # Stop song monitoring
    # Kill mpv via shared function (MCP compatible)
    kill_existing_mpv()
    PLAYER_PROC = None
    return True

def get_llm_status():
    """LLM  """
    if LLM_PROVIDER == "none":
        return "off"
    if LLM_PROVIDER == "ollama" or LLM_PROVIDER == "auto":
        try:
            req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
            with urllib.request.urlopen(req, timeout=2) as resp:
                return f"ollama:{OLLAMA_MODEL}"
        except:
            pass
    if ANTHROPIC_API_KEY and LLM_PROVIDER in ["auto", "claude"]:
        return "claude"
    if OPENAI_API_KEY and LLM_PROVIDER in ["auto", "openai"]:
        return "openai"
    return "keyword"

def display_width(s):
    """  rain  (CJK/ 2)"""
    import unicodedata
    width = 0
    for c in s:
        ea = unicodedata.east_asian_width(c)
        if ea in ('F', 'W'):
            width += 2
        elif ord(c) > 0x1F000:  # Emoji
            width += 2
        else:
            width += 1
    return width

def pad_right(s, total_width):
    """  ( rain )"""
    current = display_width(s)
    padding = max(0, total_width - current)
    return s + ' ' * padding

def truncate(s, max_width):
    """ rain """
    width = 0
    result = []
    for c in s:
        cw = 2 if display_width(c) == 2 else 1
        if width + cw > max_width:
            break
        result.append(c)
        width += cw
    return ''.join(result)

def show_menu():
    fav_count = len(load_favorites())
    songs_count = len(load_songs())
    mode = "DB" if not USE_API else "API"
    llm = get_llm_status()

    history_count = len(load_history())

    # Search hint based on LLM status
    if llm != "off" and llm != "keyword":
        search_hint = f"  🤖 {t('ai_search')} ({llm}): {t('type_anything')}"
    else:
        search_hint = f"  🔍 {t('search_hint_menu')}"

    print(f"""
  a {t('ai_recommend'):<6} t {t('my_taste'):<5} p {t('popular'):<5} h {t('hq')}
  g {t('genre'):<6} c {t('country'):<5} f {t('favorites')}({fav_count})  l {t('playlist')}
  w {t('mood_now'):<6} i {t('song_recognize'):<5} n {t('current_song'):<5} sl {t('songs')}({songs_count})
  r {t('resume'):<6} s {t('stop'):<5} < {t('prev'):<5} > {t('next')}
  v {t('volume'):<6} v+/v-   check   share
  hl {t('history')}({history_count})  d DJ   ! {t('mode')}  q {t('quit')}

  ⏰ sleep N  alarm HH:MM  ⚙️ lang ({UI_LANG})

{search_hint}
""")

def show_genres():
    print(f"\n  {t('genre_select')}:")
    for k, (tag, name_key) in GENRES.items():
        print(f"    {k}. {t(name_key)}")
    print()

def show_countries():
    print(f"\n  {t('country_select')}:")
    for k, (code, name_key) in COUNTRIES.items():
        print(f"    {k}. {t(name_key)} ({code})")
    print(f"    ({t('press_num')}: us, jp, de ...)")
    print()

def main():
    global PLAYER

    # Auto-detect language
    init_language()

    PLAYER = get_player()
    if not PLAYER:
        print(t('no_results'))  # No player message
        print("  brew install mpv")
        sys.exit(1)

    stations = []
    current_station = None  # Currently playing station
    play_start_time = None  # Play start time
    fav_index = -1  # Current favorite index (for prev/next)
    mode = "menu"  # menu, genre, country, search, list, fav

    def signal_handler(sig, frame):
        stop()
        print("\n")
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)

    show_menu()

    while True:
        try:
            if mode == "menu":
                prompt = "> "
            elif mode == "genre":
                prompt = f"{t('prompt_genre')}> "
            elif mode == "country":
                prompt = f"{t('prompt_country')}> "
            elif mode == "search":
                prompt = f"{t('prompt_search')}> "
            elif mode == "list":
                prompt = f"{t('prompt_number')}> "
            else:
                prompt = "> "

            cmd = input(prompt).strip().lower()
        except EOFError:
            break

        if not cmd:
            if mode != "menu":
                mode = "menu"
                show_menu()
            continue

        # Quit
        if cmd == "q":
            stop()
            break

        # Toggle API mode
        if cmd == "!":
            global USE_API
            USE_API = not USE_API
            mode_str = "DB+API" if USE_API else "DB (fast)"
            print(f"  search : {mode_str}\n")
            continue

        # Resume (last station)
        if cmd == "r":
            last = load_last_station()
            if last:
                # Save existing play record
                if PLAYER_PROC and current_station and play_start_time:
                    duration = int(time.time() - play_start_time)
                    add_history(current_station, duration)
                current_station = last
                record_click(last)  # Track click
                url = last.get("url_resolved") or last.get("url")
                dj_announce_station(last)
                play(url, last.get("name", ""))
                play_start_time = time.time()
                print(f"  {t('help_after_play')}")
            else:
                print("   playback broadcast .\n")
            continue

        # Stop
        if cmd == "s":
            if PLAYER_PROC and current_station and play_start_time:
                duration = int(time.time() - play_start_time)
                add_history(current_station, duration)
            if stop():
                print(f"  ■ {t('stopped_playing')}\n")
                current_station = None
                play_start_time = None
            continue

        # View current song
        if cmd == "n":
            show_current_song()
            # Introduce song if DJ mode
            song = get_current_song()
            if song and song.get("title"):
                dj_announce_song(song["title"], current_station)
            continue

        # Volume control
        if cmd == "v" or cmd == "v?":
            show_volume()
            continue
        if cmd == "v+":
            volume_up()
            continue
        if cmd == "v-":
            volume_down()
            continue
        if cmd.startswith("v") and cmd[1:].isdigit():
            set_volume(int(cmd[1:]))
            continue

        # Station status check
        if cmd == "check":
            check_current_station(current_station)
            continue

        # Station sharing
        if cmd == "share":
            share_station(current_station)
            continue

        # Song recognition (Shazam-like)
        if cmd == "i":
            recognize_song(current_station)
            continue

        # Force Whisper test
        if cmd == "i2":
            recognize_song_whisper(current_station)
            continue

        # Recognized songs list
        if cmd == "il":
            show_recognized_songs()
            continue

        # View song history
        if cmd == "sl":
            show_song_history()
            continue

        # View listening history (stations)
        if cmd == "hl":
            show_listening_history()
            continue

        # Toggle song recording (on/off)
        if cmd == "st":
            global SONG_MONITOR_ENABLED
            SONG_MONITOR_ENABLED = not SONG_MONITOR_ENABLED
            status = "ON" if SONG_MONITOR_ENABLED else "OFF"
            print(f"  song rock: {status}\n")
            continue

        # Delete song records
        if cmd == "sc":
            clear_song_history()
            continue

        # Toggle DJ mode
        if cmd == "d":
            toggle_dj()
            continue

        # Sleep timer: sleep 30
        if cmd.startswith("sleep"):
            parts = cmd.split()
            if len(parts) >= 2:
                try:
                    minutes = int(parts[1])
                    set_sleep_timer(minutes)
                except ValueError:
                    print(f"  {t('usage')}: sleep 30\n")
            else:
                show_sleep_timer()
            continue

        # Alarm: alarm 7:00 jazz
        if cmd.startswith("alarm"):
            parts = cmd.split()
            if len(parts) >= 2:
                if parts[1] == "off":
                    cancel_alarm()
                else:
                    try:
                        time_parts = parts[1].split(":")
                        hour = int(time_parts[0])
                        minute = int(time_parts[1]) if len(time_parts) > 1 else 0
                        query = parts[2] if len(parts) > 2 else "pop"
                        set_alarm(hour, minute, query)
                    except (ValueError, IndexError):
                        print(f"  {t('usage')}: alarm 7:00 jazz | alarm off\n")
            else:
                if _alarm_timer and _alarm_timer.is_alive():
                    print(f"  ⏰ {t('alarm_active')}\n")
                else:
                    print(f"  {t('alarm_not_set')}\n")
            continue

        # Change language
        if cmd == "lang":
            show_languages()
            mode = "lang"
            continue

        # Select language
        if mode == "lang":
            if change_language(cmd):
                mode = "menu"
                show_menu()
            continue

        # Playlist
        if cmd == "l":
            pl_names = show_playlists()
            if pl_names:
                mode = "playlist"
                print(f"  ({t('enter_num_play')}, -{t('enter_num_del')})")
            continue

        # Create playlist: pl name type
        if cmd.startswith("pl "):
            parts = cmd[3:].split()
            if len(parts) >= 2:
                name = parts[0]
                criteria = parts[1]
                count = create_smart_playlist(name, criteria)
                if count:
                    print(f"  ✓ '{name}' {t('pl_created')} ({count} {t('songs')})\n")
                else:
                    print(f"  ✗ {t('create_failed')}\n")
            else:
                print(f"  {t('usage')}: pl name type")
                print(f"  {t('pl_types')}: favorites, history, mood, ai, tag:jazz, country:KR\n")
            continue

        # Selection in playlist mode
        if mode == "playlist":
            if cmd.startswith("-"):
                # Delete
                if delete_playlist(cmd[1:]):
                    print(f"  ✗ {t('deleted')}\n")
                    show_playlists()
                continue
            # Show playlist
            pl_stations = get_playlist_stations(cmd)
            if pl_stations:
                stations = pl_stations
                print_stations(stations)
                mode = "list"
            else:
                print(f"  {t('invalid_num')}\n")
            continue

        # Menu
        if cmd == "m":
            mode = "menu"
            show_menu()
            continue

        # Genre mode
        if cmd == "g":
            mode = "genre"
            show_genres()
            continue

        # Country mode
        if cmd == "c":
            mode = "country"
            show_countries()
            continue

        # Popular
        if cmd == "p":
            print(f"  {t('popular_loading')}...")
            stations = get_popular()
            print_stations(stations)
            if stations:
                mode = "list"
            continue

        # High quality
        if cmd == "h":
            print(f"  {t('hq_loading')} (256k+)...")
            stations = get_high_quality()
            print_stations(stations)
            if stations:
                mode = "list"
            continue

        # Recommend (premium)
        if cmd == "r":
            print(f"  {t('recommend_loading')}...")
            stations = get_premium()
            print_stations(stations)
            if stations:
                mode = "list"
            continue

        # AI recommendation (based on taste)
        if cmd == "a":
            print(f"  {t('ai_recommend_loading')}...")
            stations = get_personalized_recommendations()
            print_stations(stations)
            if stations:
                mode = "list"
            continue

        # Mood recommend (time-based)
        if cmd == "w":
            print(f"  {t('mood_recommend')} ({t('time_based')})...")
            stations = get_mood_recommendations()
            print_stations(stations)
            if stations:
                mode = "list"
            continue

        # My taste analysis
        if cmd == "t":
            show_my_taste()
            continue

        # View favorites
        if cmd == "f":
            favs = print_favorites()
            if favs:
                stations = favs
                mode = "fav"
                print(f"  ({t('enter_num_play')}, -{t('enter_num_del')})")
            continue

        # Add to favorites
        if cmd == "+" and current_station:
            if add_favorite(current_station):
                print(f"  ★ {t('added_fav')}: {current_station.get('name', '')}\n")
            else:
                print(f"  {t('already_fav')}\n")
            continue

        # Remove from favorites (current station)
        if cmd == "-" and current_station:
            url = current_station.get("url_resolved") or current_station.get("url", "")
            favs = load_favorites()
            new_favs = [f for f in favs if f.get("url") != url]
            if len(new_favs) < len(favs):
                save_favorites(new_favs)
                print(f"  ✗ {t('removed_fav')}: {current_station.get('name', '')}\n")
            else:
                print(f"  {t('no_fav')}\n")
            continue

        # Previous favorite station
        if cmd == "<" or cmd == ",":
            favs = load_favorites()
            if not favs:
                print("   rain.\n")
                continue
            if PLAYER_PROC and current_station and play_start_time:
                duration = int(time.time() - play_start_time)
                add_history(current_station, duration)
            fav_index = (fav_index - 1) % len(favs)
            s = favs[fav_index]
            current_station = s
            url = s.get("url_resolved") or s.get("url")
            dj_announce_station(s)
            play(url, s.get("name", ""))
            play_start_time = time.time()
            save_last_station(s)
            print(f"  [{fav_index + 1}/{len(favs)}] {s.get('name', '')}\n")
            continue

        # Next favorite station
        if cmd == ">" or cmd == ".":
            favs = load_favorites()
            if not favs:
                print("   rain.\n")
                continue
            if PLAYER_PROC and current_station and play_start_time:
                duration = int(time.time() - play_start_time)
                add_history(current_station, duration)
            fav_index = (fav_index + 1) % len(favs)
            s = favs[fav_index]
            current_station = s
            url = s.get("url_resolved") or s.get("url")
            dj_announce_station(s)
            play(url, s.get("name", ""))
            play_start_time = time.time()
            save_last_station(s)
            print(f"  [{fav_index + 1}/{len(favs)}] {s.get('name', '')}\n")
            continue

        # Search mode
        if cmd == "/":
            mode = "search"
            print(f"  {t('enter_search')} ({t('enter_cancel')})")
            continue

        # Genre selection
        if mode == "genre":
            if cmd in GENRES:
                tag, name_key = GENRES[cmd]
                print(f"  '{t(name_key)}' {t('searching_for')}...")
                stations = search_by_tag(tag)
                print_stations(stations)
                if stations:
                    mode = "list"
            else:
                # Directly entered genre
                print(f"  '{cmd}' {t('searching_for')}...")
                stations = search_by_tag(cmd)
                print_stations(stations)
                if stations:
                    mode = "list"
            continue

        # Select country
        if mode == "country":
            if cmd in COUNTRIES:
                code, name_key = COUNTRIES[cmd]
                display_name = t(name_key)
            else:
                code = cmd.upper()
                display_name = code
            print(f"  '{display_name}' {t('searching_for')}...")
            stations = search_by_country(code)
            print_stations(stations)
            if stations:
                mode = "list"
            continue

        # Search
        if mode == "search":
            print(f"  '{cmd}' {t('searching_for')}...")
            stations = search_advanced(cmd)
            print_stations(stations)
            if stations:
                mode = "list"
            continue

        # Delete favorite (-number)
        if cmd.startswith("-") and cmd[1:].isdigit():
            idx = int(cmd[1:]) - 1
            removed = remove_favorite(idx)
            if removed:
                print(f"  ✗ {t('deleted')}: {removed.get('name', '')}\n")
                favs = print_favorites()
                stations = favs if favs else []
            else:
                print(f"  {t('invalid_num')}")
            continue

        # Play by number
        if cmd.isdigit():
            idx = int(cmd) - 1
            if 0 <= idx < len(stations):
                # Save previous listen record
                if PLAYER_PROC and current_station and play_start_time:
                    duration = int(time.time() - play_start_time)
                    add_history(current_station, duration)

                s = stations[idx]
                current_station = s
                url = s.get("url_resolved") or s.get("url")

                # DJ intro first → start radio
                dj_announce_station(s)
                play(url, s.get("name", ""))
                play_start_time = time.time()
                save_last_station(s)  # Save last station

                # Save to DB on success (API results only)
                if s.get("source") == "api":
                    save_station_to_db(s)

                print(f"  {t('help_after_play')}")
                mode = "menu"  # Back to menu mode for search
            else:
                print(f"  {t('invalid_num')}")
            continue

        # Smart search directly from menu
        if mode == "menu" and len(cmd) > 0:
            print(f"  '{cmd}' {t('searching_for')}...")
            stations = search_advanced(cmd)
            print_stations(stations)
            if stations:
                mode = "list"
            continue

        print(f"  ? {t('help_hint')}: g={t('genre')}, c={t('country')}, p={t('popular')}, /={t('searching')}, s={t('stop')}, q={t('quit')}")

if __name__ == "__main__":
    # --cleanup: Remove dead stations
    if len(sys.argv) > 1 and sys.argv[1] == "--cleanup":
        count = cleanup_dead_stations()
        print(f" broadcast {count}items ")
        sys.exit(0)

    # --db-stats: DB statistics
    if len(sys.argv) > 1 and sys.argv[1] == "--db-stats":
        if os.path.exists(DB_PATH):
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM stations")
            total = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM stations WHERE is_alive = 1")
            alive = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM stations WHERE is_alive = 0")
            dead = cursor.fetchone()[0]
            conn.close()
            print(f"DB :  {total}items,  {alive}items,  {dead}items")
        else:
            print("DB  ")
        sys.exit(0)

    main()

# === Click tracking (our API) ===
def record_click(station):
    """playback  click rock"""
    if not station:
        return
    station_id = station.get("stationuuid") or station.get("id")
    if not station_id:
        return
    try:
        url = f"{API_BASE}/stations/{station_id}/click"
        req = urllib.request.Request(url, method="POST")
        req.add_header("User-Agent", "RadioCli/1.0")
        urllib.request.urlopen(req, timeout=3)
    except:
        pass  # Continue playing even on failure
