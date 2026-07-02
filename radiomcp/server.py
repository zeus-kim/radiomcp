#!/usr/bin/env python3
"""
Radio MCP Server - Internet radio search and playback
SQLite DB first, RadioGraph API fallback

Station database derived from Radio Browser (https://www.radio-browser.info/)
Data licensed under ODbL 1.0. See DATA_LICENSE.md and ATTRIBUTION.md.\n"""
from __future__ import annotations

import json
import os
import subprocess
import socket
import sqlite3
import urllib.request
import urllib.parse
import time
import shutil
import atexit
import signal
import threading
import webbrowser
import sys
from typing import Any
from datetime import datetime

try:
    from mcp.server.fastmcp import FastMCP
    _HAS_MCP = True
except ImportError:
    _HAS_MCP = False
    # Dummy FastMCP for CLI-only mode (mcp package not installed)
    class FastMCP:
        def __init__(self, name=""):
            self.name = name
        def tool(self, *a, **kw):
            def decorator(fn):
                return fn
            return decorator
        def run(self):
            raise RuntimeError(
                "MCP package not installed. Install with: pip install 'mcp[cli]>=1.0.0'\n"
                "CLI commands (search/play/stop) work without MCP."
            )

# ============================================================
# Platform Detection
# ============================================================
IS_WINDOWS = os.name == "nt"

def _subprocess_detach_kwargs():
    """Get platform-specific kwargs to detach subprocess from parent"""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        return {"preexec_fn": os.setpgrp}

# ============================================================
# Player Backend Abstraction
# ============================================================
# Priority: mpv > vlc > ffplay > browser
# ============================================================

PLAYER_BACKEND = None  # 'mpv', 'vlc', 'ffplay', 'browser'

def detect_player_backend():
    """Detect available player backend"""
    global PLAYER_BACKEND

    # 1. mpv (best)
    if shutil.which("mpv"):
        PLAYER_BACKEND = "mpv"
        return "mpv"

    # 2. VLC (widely installed)
    if shutil.which("vlc") or shutil.which("cvlc"):
        PLAYER_BACKEND = "vlc"
        return "vlc"

    # 3. ffplay (included with ffmpeg)
    if shutil.which("ffplay"):
        PLAYER_BACKEND = "ffplay"
        return "ffplay"

    # 4. Browser fallback (always available)
    PLAYER_BACKEND = "browser"
    return "browser"

# Detect backend on init
detect_player_backend()

# ============================================================
# Miniaudio player (used when mpv unavailable)
# ============================================================
class MiniaudioPlayer:
    """Miniaudio-based streaming player"""

    def __init__(self):
        self.stream_thread = None
        self.playing = False
        self.device = None

    def play(self, url):
        """Play URL stream"""
        self.stop()
        self.playing = True

        def stream_worker():
            try:
                import miniaudio
                import urllib.request

                # Open HTTP stream
                req = urllib.request.Request(url, headers={
                    'User-Agent': 'RadioMCP/1.0',
                    'Icy-MetaData': '1'
                })
                response = urllib.request.urlopen(req, timeout=30)

                # Setup miniaudio decoder
                def read_data(num_bytes):
                    if not self.playing:
                        return b''
                    return response.read(num_bytes)

                # Play stream
                self.device = miniaudio.PlaybackDevice()
                stream = miniaudio.stream_any(
                    source=response,
                    source_format=miniaudio.FileFormat.MP3
                )
                self.device.start(stream)

                while self.playing:
                    time.sleep(0.1)

            except Exception as e:
                pass
            finally:
                self.playing = False
                if self.device:
                    self.device.close()

        self.stream_thread = threading.Thread(target=stream_worker, daemon=True)
        self.stream_thread.start()
        return True

    def stop(self):
        """Stop playback"""
        self.playing = False
        if self.device:
            try:
                self.device.close()
            except:
                pass
            self.device = None

    def is_playing(self):
        return self.playing

# Global miniaudio player instance
_miniaudio_player = None

def get_miniaudio_player():
    global _miniaudio_player
    if _miniaudio_player is None:
        _miniaudio_player = MiniaudioPlayer()
    return _miniaudio_player

# ============================================================
# VLC Player
# ============================================================
class VLCPlayer:
    """VLC-based player (using cvlc)"""

    def __init__(self):
        self.process = None
        self.pid_file = os.path.join(os.path.expanduser("~/.radiocli"), "vlc.pid")

    def play(self, url):
        """Play with VLC"""
        self.stop()
        try:
            # Use cvlc (console VLC)
            vlc_cmd = shutil.which("cvlc") or shutil.which("vlc")
            self.process = subprocess.Popen(
                [vlc_cmd, "--intf", "dummy", "--no-video", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_subprocess_detach_kwargs()
            )
            with open(self.pid_file, 'w') as f:
                f.write(str(self.process.pid))
            return True
        except Exception:
            return False

    def stop(self):
        """Stop VLC"""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except:
                try:
                    self.process.kill()
                except:
                    pass
            self.process = None
        # Stop via PID file
        if os.path.exists(self.pid_file):
            try:
                with open(self.pid_file) as f:
                    pid = int(f.read().strip())
                os.kill(pid, signal.SIGTERM)
            except:
                pass
            try:
                os.remove(self.pid_file)
            except:
                pass

    def is_playing(self):
        return self.process is not None and self.process.poll() is None

_vlc_player = None

def get_vlc_player():
    global _vlc_player
    if _vlc_player is None:
        _vlc_player = VLCPlayer()
    return _vlc_player

# ============================================================
# FFplay Player
# ============================================================
class FFplayPlayer:
    """ffplay-based player (included with ffmpeg)"""

    def __init__(self):
        self.process = None
        self.pid_file = os.path.join(os.path.expanduser("~/.radiocli"), "ffplay.pid")

    def play(self, url):
        """Play with ffplay"""
        self.stop()
        try:
            self.process = subprocess.Popen(
                ["ffplay", "-nodisp", "-loglevel", "quiet", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_subprocess_detach_kwargs()
            )
            with open(self.pid_file, 'w') as f:
                f.write(str(self.process.pid))
            return True
        except Exception:
            return False

    def stop(self):
        """Stop ffplay"""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except:
                try:
                    self.process.kill()
                except:
                    pass
            self.process = None
        # Stop via PID file
        if os.path.exists(self.pid_file):
            try:
                with open(self.pid_file) as f:
                    pid = int(f.read().strip())
                os.kill(pid, signal.SIGTERM)
            except:
                pass
            try:
                os.remove(self.pid_file)
            except:
                pass

    def is_playing(self):
        return self.process is not None and self.process.poll() is None

_ffplay_player = None

def get_ffplay_player():
    global _ffplay_player
    if _ffplay_player is None:
        _ffplay_player = FFplayPlayer()
    return _ffplay_player

# ============================================================
# Browser Player (last resort fallback)
# ============================================================
class BrowserPlayer:
    """Open stream in browser"""

    def __init__(self):
        self.current_url = None

    def play(self, url):
        """Open URL in browser"""
        self.current_url = url
        webbrowser.open(url)
        return True

    def stop(self):
        """Browser must be closed manually"""
        self.current_url = None
        return {"note": "Please close the browser tab manually"}

    def is_playing(self):
        return self.current_url is not None

_browser_player = None

def get_browser_player():
    global _browser_player
    if _browser_player is None:
        _browser_player = BrowserPlayer()
    return _browser_player

# Create MCP server
mcp = FastMCP("radio")

# Configuration
DATA_DIR = os.path.expanduser("~/.radiocli")
FAVORITES_FILE = os.path.join(DATA_DIR, "favorites.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
RECOGNIZED_FILE = os.path.join(DATA_DIR, "recognized_songs.json")
RECORD_FILE = os.path.join(DATA_DIR, "record.mp3")
MPV_SOCKET = r"\\.\pipe\radiomcp_mpv" if IS_WINDOWS else os.path.join(DATA_DIR, "mpv.sock")
MPV_PID_FILE = os.path.join(DATA_DIR, "mpv.pid")  # Shared with CLI
LOCK_FILE = os.path.join(DATA_DIR, "server.lock")
# ============================================================
# Configuration System
# ============================================================
# Priority: env var > config file > default
# Config file: ~/.radiocli/config.json
# ============================================================
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

def load_config():
    """Load config from file, merge with defaults"""
    defaults = {
        "radiograph_url": "https://api.airtune.ai",  # Airtune Radio API
        "radiograph_api_key": "",         # optional API key
        "serve_port": 8100,
        "serve_host": "0.0.0.0",
        "mpv_path": "",                   # auto-detect if empty
        "db_path": "",                    # auto-detect if empty
        "lightweight": False,             # Default: local DB + API (DB ships with package)
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                user_config = json.load(f)
            defaults.update(user_config)
        except Exception:
            pass
    # Env overrides
    if os.environ.get("RADIOGRAPH_URL"):
        defaults["radiograph_url"] = os.environ["RADIOGRAPH_URL"]
    if os.environ.get("RADIOMCP_LIGHTWEIGHT"):
        defaults["lightweight"] = os.environ["RADIOMCP_LIGHTWEIGHT"].lower() == "true"
    return defaults

def save_config(config):
    """Save config to file"""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

CONFIG = load_config()

# API base URL
RADIOGRAPH_BASE = CONFIG["radiograph_url"]
API_BASE = RADIOGRAPH_BASE

# G3 URL Validator API (optional, for detailed stream info)
G3_VALIDATOR_URL = os.environ.get("G3_VALIDATOR_URL", "")  # Set via env: G3_VALIDATOR_URL=http://yourserver:8100/api/validate
G3_VALIDATOR_ENABLED = os.environ.get("G3_VALIDATOR_ENABLED", "false").lower() == "true"


def g3_validate_url(url: str, timeout: int = 5) -> dict:
    """
    Validate URL using G3 URL Validator API.
    Returns detailed stream info (bitrate, format, etc.)
    """
    if not G3_VALIDATOR_ENABLED:
        return {"valid": False, "error": "G3 validator disabled"}
    
    try:
        api_url = f"{G3_VALIDATOR_URL}?url={urllib.parse.quote(url)}"
        req = urllib.request.Request(api_url, headers={"User-Agent": "RadioMCP/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"valid": False, "error": str(e)}




# SVD Recommendation API
SVD_API_BASE = CONFIG.get("radiograph_url", "https://api.airtune.ai") + "/svd"

def _svd_api_get(endpoint: str, timeout: int = 10) -> dict:
    """Call SVD API endpoint and return JSON response."""
    try:
        url = f"{SVD_API_BASE}{endpoint}"
        req = urllib.request.Request(url, headers={"User-Agent": "RadioMCP/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

def g3_batch_validate(urls: list, timeout: int = 10) -> list:
    """
    Batch validate URLs using G3 API.
    """
    if not G3_VALIDATOR_ENABLED:
        return [{"url": u, "valid": False, "error": "G3 validator disabled"} for u in urls]
    
    try:
        req = urllib.request.Request(
            f"{G3_VALIDATOR_URL.replace('/validate', '/validate/batch')}",
            data=json.dumps({"urls": urls, "timeout": timeout}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "RadioMCP/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return [{"url": u, "valid": False, "error": str(e)} for u in urls]


def submit_station_to_api(station: dict):
    """
    Submit station to central API for collection.
    Called automatically on play/favorite - runs in background, silent on failure.
    """
    try:
        url = station.get("url") or station.get("url_resolved")
        if not url:
            return

        data = {
            "url": url,
            "name": station.get("name", ""),
            "tags": station.get("tags", ""),
            "country": station.get("country", ""),
            "countrycode": station.get("countrycode", ""),
            "bitrate": station.get("bitrate", 0),
        }

        req = urllib.request.Request(
            f"{API_BASE}/submit",
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "RadioMCP/1.0"},
            method="POST"
        )
        # Fire and forget - don't wait for response
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass  # Silent failure - don't interrupt user flow


# DB path (priority: local > package > project)
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATHS = [
    os.path.join(DATA_DIR, "radio_stations.db"),
    os.path.join(PACKAGE_DIR, "radio_stations.db"),  # DB in package
    os.path.join(os.getcwd(), "radio_stations.db"),   # Current directory (dev/Codex)
    os.path.expanduser("~/RadioCli/radio_stations.db"),
]

# Global state
current_station = None
player_proc = None
db_conn = None
sleep_timer = None  # Sleep timer
lock_fd = None  # Singleton lock file descriptor

LAST_STATION_FILE = os.path.join(DATA_DIR, "last_station.json")

import fcntl  # For file locking

def acquire_singleton_lock():
    """Acquire singleton lock - terminate existing process if running"""
    global lock_fd
    os.makedirs(DATA_DIR, exist_ok=True)

    lock_fd = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Lock acquired - record PID
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        return True
    except BlockingIOError:
        # Another server running - force terminate
        try:
            with open(LOCK_FILE, 'r') as f:
                old_pid = int(f.read().strip())
            # Try SIGTERM
            try:
                os.kill(old_pid, signal.SIGTERM)
                time.sleep(0.3)
            except:
                pass
            # SIGKILL if still alive
            try:
                os.kill(old_pid, 0)  # Check if exists
                os.kill(old_pid, signal.SIGKILL)
                time.sleep(0.3)
            except ProcessLookupError:
                pass
            # Retry
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_fd.seek(0)
            lock_fd.truncate()
            lock_fd.write(str(os.getpid()))
            lock_fd.flush()
            return True
        except:
            return False

def release_singleton_lock():
    """Release singleton lock"""
    global lock_fd
    if lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
            os.remove(LOCK_FILE)
        except:
            pass
        lock_fd = None

def mpv_ipc_send(command: dict, timeout: float = 1.0) -> dict | None:
    """Send command to mpv via IPC (cross-platform)"""
    try:
        data = (json.dumps(command) + "\n").encode()
        if IS_WINDOWS:
            # Windows: Named Pipe
            with open(MPV_SOCKET, "r+b", buffering=0) as pipe:
                pipe.write(data)
                pipe.flush()
                response = pipe.readline()
                return json.loads(response) if response else None
        else:
            # Unix: Domain Socket
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(MPV_SOCKET)
            sock.send(data)
            response = sock.recv(4096).decode()
            sock.close()
            return json.loads(response) if response else None
    except:
        return None


def kill_existing_mpv():
    """Stop existing mpv process (shared with CLI)"""
    # 1. Try IPC quit command
    mpv_ipc_send({"command": ["quit"]})
    time.sleep(0.5)

    # 2. Terminate via PID file
    if os.path.exists(MPV_PID_FILE):
        try:
            with open(MPV_PID_FILE, 'r') as f:
                pid = int(f.read().strip())
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
            else:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.5)
                try:
                    os.kill(pid, 0)  # Still alive?
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except:
            pass
        try:
            os.remove(MPV_PID_FILE)
        except:
            pass

    # 3. Last resort: kill all mpv
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/IM", "mpv.exe", "/F"], capture_output=True, timeout=2)
        else:
            subprocess.run(["pkill", "-f", "mpv.*radiocli"], timeout=2)
        time.sleep(0.3)
    except:
        pass

    # 4. Clean up socket file (Unix only)
    if not IS_WINDOWS and os.path.exists(MPV_SOCKET):
        try:
            os.remove(MPV_SOCKET)
        except:
            pass

def save_last_station():
    """Save last played station"""
    if current_station:
        try:
            with open(LAST_STATION_FILE, "w", encoding="utf-8") as f:
                json.dump(current_station, f, ensure_ascii=False)
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

def cleanup():
    """Save state on exit (mpv keeps playing)"""
    global player_proc
    # Save last station
    save_last_station()
    # Don't kill mpv - keep playing after server restart
    # Only stop mpv when stop() is called
    player_proc = None
    # Release singleton lock
    release_singleton_lock()

# Register exit handler only (let anyio handle signals)
atexit.register(cleanup)
# Note: Don't override SIGTERM/SIGINT - anyio needs them for graceful shutdown
# The atexit handler will run cleanup on normal exit

# Watchdog: Monitor Claude Desktop in separate process
def start_mpv_watchdog():
    """Start watchdog process when mpv starts (cross-platform)"""
    import sys
    platform = sys.platform

    watchdog_script = f'''
import time, os, subprocess, signal, sys

mpv_pid_file = "{os.path.join(DATA_DIR, "mpv.pid")}"
mpv_sock = "{MPV_SOCKET}"
platform = "{platform}"

def is_claude_running():
    """Check if Claude Desktop is running (cross-platform)"""
    try:
        if platform == "darwin":  # macOS
            result = subprocess.run(
                ["osascript", "-e", 'tell application "System Events" to (name of processes) contains "Claude"'],
                capture_output=True, text=True
            )
            return "true" in result.stdout.lower()
        elif platform == "win32":  # Windows
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Claude.exe"],
                capture_output=True, text=True
            )
            return "Claude.exe" in result.stdout
        else:  # Linux
            result = subprocess.run(
                ["pgrep", "-f", "claude-desktop|Claude"],
                capture_output=True
            )
            return result.returncode == 0
    except:
        return True  # Assume running if check fails

def kill_mpv():
    """Stop mpv"""
    if os.path.exists(mpv_pid_file):
        try:
            with open(mpv_pid_file) as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            try: os.kill(pid, signal.SIGKILL)
            except: pass
        except: pass
        try: os.remove(mpv_pid_file)
        except: pass
    if os.path.exists(mpv_sock):
        try: os.remove(mpv_sock)
        except: pass
    if platform != "win32":
        subprocess.run(["pkill", "-f", "mpv.*mpv.sock"], capture_output=True)
    else:
        subprocess.run(["taskkill", "/F", "/IM", "mpv.exe"], capture_output=True)

while True:
    time.sleep(5)
    if not is_claude_running():
        kill_mpv()
        break
'''
    # Kill existing watchdog
    subprocess.run(["pkill", "-f", "mpv_pid_file"], capture_output=True)
    # Start new watchdog (independent process)
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if platform != "win32":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(["python3", "-c", watchdog_script], **kwargs)

# Synonym mapping (tag expansion)
TAG_SYNONYMS = {
    "lounge": ["lounge", "chillout", "cafe", "ambient", "easy listening"],
    "jazz": ["jazz", "smooth jazz", "jazz lounge", "bossa nova", "bebop", "swing", "fusion"],
    "classical": ["classical", "classic", "orchestra", "symphony", "piano", "chamber"],
    "rock": ["rock", "classic rock", "hard rock", "alternative", "indie rock"],
    "pop": ["pop", "top 40", "hits", "chart", "mainstream"],
    "electronic": ["electronic", "edm", "dance", "house", "techno", "trance", "dubstep"],
    "ambient": ["ambient", "chillout", "relaxing", "meditation", "sleep", "drone"],
    "hiphop": ["hiphop", "hip-hop", "hip hop", "rap", "r&b", "rnb", "trap"],
    "bossa nova": ["bossa nova", "bossa", "brazilian", "latin jazz", "mpb"],
    "chillout": ["chillout", "chill", "lounge", "ambient", "downtempo"],
    "cafe": ["cafe", "coffee", "lounge", "acoustic", "bossa nova"],
    "sleep": ["sleep", "ambient", "relaxing", "meditation", "nature", "calm"],
    "focus": ["focus", "study", "concentration", "instrumental", "classical", "lo-fi", "lofi"],
    "workout": ["workout", "gym", "exercise", "dance", "electronic", "energetic"],
    "morning": ["morning", "wake up", "breakfast", "pop", "acoustic"],
    "night": ["night", "late night", "jazz", "lounge", "ambient"],
    "rain": ["rain", "nature", "ambient", "relaxing", "piano"],
    "summer": ["summer", "tropical", "beach", "latin", "reggae"],
    "winter": ["winter", "christmas", "cozy", "acoustic", "classical"],
    "blues": ["blues", "soul", "r&b", "rhythm and blues"],
    "country": ["country", "americana", "folk", "bluegrass"],
    "metal": ["metal", "heavy metal", "death metal", "black metal", "thrash"],
    "reggae": ["reggae", "ska", "dub", "dancehall", "roots"],
    "soul": ["soul", "r&b", "motown", "funk", "neo soul"],
    "folk": ["folk", "acoustic", "singer-songwriter", "americana"],
    "latin": ["latin", "salsa", "merengue", "bachata", "cumbia", "reggaeton"],
    "world": ["world", "world music", "ethnic", "traditional", "folk"],
    "news": ["news", "talk", "information", "current affairs", "spoken"],
    "kpop": ["kpop", "k-pop", "korean pop", "korean"],
    "jpop": ["jpop", "j-pop", "japanese pop", "japanese"],
    "anime": ["anime", "japanese", "soundtrack", "ost"],
}

# ============================================================
# Multilingual search mapping (v2.0)
# ============================================================

# Multilingual -> English tag mapping
LANG_MAP = {
    # Korean
    "jazz": "jazz", "classical": "classical", "rock": "rock", "pop": "pop",
    "news": "news", "hip hop": "hip hop", "ballad": "ballad", "korean traditional music": "korean traditional",
    "trot": "trot", "indie": "indie", "lounge": "lounge", "ambient": "ambient",
    "electronic": "electronic", "bossa nova": "bossa nova", "kpop": "kpop",
    "kpop": "kpop", "korea": "korean", "club": "club", "dance": "dance",
    "r&b": "r&b", "soul": "soul", "blues": "blues", "country": "country",
    "metal": "metal", "funk": "punk", "reggae": "reggae", "folk": "folk",
    "acoustic": "acoustic", "piano": "piano", "sleep": "sleep", "meditation": "meditation",
    "focus": "focus", "study": "study", "workout": "workout", "cafe": "cafe",
    "morning": "morning", "evening": "evening", "night": "night", "christmas": "christmas",
    "summer": "summer", "winter": "winter", "rain": "rain", "nature": "nature",
    "classical": "classical", "orchestra": "orchestra", "symphony": "symphony",
    "": "opera", "musical": "musical", "soundtrack": "soundtrack",
    "game music": "game", "animation": "anime", "children song": "children",
    "": "religious", "": "gospel", "buddhist": "buddhist",

    # Japanese
    "ジャズ": "jazz", "クラシック": "classical", "ロック": "rock", "ポップ": "pop",
    "ニュース": "news", "ヒップホップ": "hip hop", "演歌": "enka",
    "アニメ": "anime", "Jポップ": "jpop", "邦楽": "japanese",
    "洋楽": "western", "ラウンジ": "lounge", "アンビエント": "ambient",
    "エレクトロニック": "electronic", "ボサノバ": "bossa nova",
    "カフェ": "cafe", "睡眠": "sleep", "瞑想": "meditation", "勉強": "study",
    "朝": "morning", "夜": "night", "夏": "summer", "冬": "winter",
    "ソウル": "soul", "ブルース": "blues", "レゲエ": "reggae",
    "フォーク": "folk", "メタル": "metal", "パンク": "punk",
    "ゲーム": "game", "映画": "soundtrack", "童謡": "children",

    # Chinese (Simplified)
    "爵士乐": "jazz", "爵士": "jazz", "古典音乐": "classical", "古典": "classical",
    "摇滚": "rock", "流行": "pop", "新闻": "news", "嘻哈": "hip hop",
    "电子": "electronic", "电子音乐": "electronic", "舞曲": "dance",
    "轻音乐": "easy listening", "休闲": "lounge", "咖啡": "cafe",
    "睡眠": "sleep", "冥想": "meditation", "学习": "study", "工作": "focus",
    "早晨": "morning", "夜晚": "night", "夏天": "summer", "冬天": "winter",
    "灵魂乐": "soul", "蓝调": "blues", "雷鬼": "reggae", "民谣": "folk",
    "金属": "metal", "朋克": "punk", "动漫": "anime", "游戏": "game",
    "华语": "chinese", "粤语": "cantonese", "国语": "mandarin",

    # Chinese (Traditional)
    "爵士樂": "jazz", "古典音樂": "classical", "搖滾": "rock", "流行音樂": "pop",
    "電子音樂": "electronic", "輕音樂": "easy listening",

    # Spanish
    "música clásica": "classical", "música pop": "pop", "música rock": "rock",
    "noticias": "news", "jazz latino": "latin jazz", "salsa": "salsa",
    "reggaeton": "reggaeton", "bachata": "bachata", "merengue": "merengue",
    "cumbia": "cumbia", "flamenco": "flamenco", "latina": "latin",
    "relajante": "relaxing", "dormir": "sleep", "estudiar": "study",

    # German
    "klassische musik": "classical", "nachrichten": "news", "schlager": "schlager",
    "volksmusik": "folk", "deutsche musik": "german",

    # French
    "musique classique": "classical", "musique pop": "pop", "actualités": "news",
    "chanson française": "chanson", "musique française": "french",

    # Portuguese
    "música brasileira": "brazilian", "samba": "samba", "forró": "forro",
    "sertanejo": "sertanejo", "mpb": "mpb", "axé": "axe",

    # Russian
    "джаз": "jazz", "классика": "classical", "рок": "rock", "поп": "pop",
    "новости": "news", "электронная": "electronic", "русская": "russian",

    # Arabic
    "جاز": "jazz", "كلاسيكي": "classical", "أخبار": "news",
    "موسيقى عربية": "arabic", "عربي": "arabic",

    # Hindi
    "जैज़": "jazz", "शास्त्रीय": "classical", "समाचार": "news",
    "बॉलीवुड": "bollywood", "हिंदी": "hindi",

    # Vietnamese
    "nhạc jazz": "jazz", "nhạc cổ điển": "classical", "tin tức": "news",
    "nhạc việt": "vietnamese", "nhạc trẻ": "vpop",

    # Thai
    "แจ๊ส": "jazz", "คลาสสิก": "classical", "ข่าว": "news",
    "เพลงไทย": "thai", "ลูกทุ่ง": "luk thung",

    # Indonesian
    "berita": "news", "musik indonesia": "indonesian", "dangdut": "dangdut",
}

# Compound genres (for token merge)
COMPOUND_GENRES = {
    ("bossa", "nova"): "bossa nova",
    ("hip", "hop"): "hip hop",
    ("smooth", "jazz"): "smooth jazz",
    ("deep", "house"): "deep house",
    ("classic", "rock"): "classic rock",
    ("hard", "rock"): "hard rock",
    ("heavy", "metal"): "heavy metal",
    ("death", "metal"): "death metal",
    ("neo", "soul"): "neo soul",
    ("lo", "fi"): "lo-fi",
    ("easy", "listening"): "easy listening",
    ("world", "music"): "world music",
    ("new", "age"): "new age",
    ("drum", "bass"): "drum and bass",
    ("drum", "n", "bass"): "drum and bass",
    ("r", "b"): "r&b",
    ("rhythm", "blues"): "rhythm and blues",
    ("k", "pop"): "kpop",
    ("j", "pop"): "jpop",
    ("top", "40"): "top 40",
    ("old", "school"): "old school",
    ("latin", "jazz"): "latin jazz",
    ("acid", "jazz"): "acid jazz",
    ("nu", "jazz"): "nu jazz",
}

# Country name -> code mapping (for country-first sorting)
COUNTRY_NAMES = {
    # Korean
    "korea": "KR", "country": "KR", "usa": "US", "japan": "JP", "china": "CN",
    "uk": "GB", "france": "FR", "germany": "DE", "italy": "IT", "spain": "ES",
    "canada": "CA", "australia": "AU", "brazil": "BR", "mexico": "MX", "russia": "RU",
    "india": "IN", "thailand": "TH", "vietnam": "VN", "indonesia": "ID", "philippines": "PH",
    "taiwan": "TW", "hong kong": "HK", "singapore": "SG", "malaysia": "MY",
    # English
    "korea": "KR", "korean": "KR", "usa": "US", "america": "US", "american": "US",
    "japan": "JP", "japanese": "JP", "china": "CN", "chinese": "CN",
    "uk": "GB", "british": "GB", "england": "GB", "france": "FR", "french": "FR",
    "germany": "DE", "german": "DE", "italy": "IT", "italian": "IT",
    "spain": "ES", "spanish": "ES", "canada": "CA", "canadian": "CA",
    "australia": "AU", "australian": "AU", "brazil": "BR", "brazilian": "BR",
    "mexico": "MX", "mexican": "MX", "russia": "RU", "russian": "RU",
    "india": "IN", "indian": "IN", "thailand": "TH", "thai": "TH",
    "vietnam": "VN", "vietnamese": "VN", "indonesia": "ID", "indonesian": "ID",
    "philippines": "PH", "filipino": "PH", "taiwan": "TW", "taiwanese": "TW",
    "hongkong": "HK", "singapore": "SG", "malaysia": "MY", "malaysian": "MY",
    # Japanese
    "韓国": "KR", "アメリカ": "US", "日本": "JP", "中国": "CN",
    "イギリス": "GB", "フランス": "FR", "ドイツ": "DE",
}

# Known tags list (for fuzzy search)
KNOWN_TAGS = [
    "jazz", "classical", "rock", "pop", "electronic", "ambient", "lounge",
    "chillout", "hip hop", "r&b", "soul", "blues", "country", "folk",
    "latin", "reggae", "bossa nova", "indie", "alternative", "metal",
    "punk", "edm", "techno", "house", "trance", "dubstep", "acoustic",
    "piano", "instrumental", "meditation", "sleep", "news", "talk",
    "kpop", "jpop", "anime", "enka", "trot", "world music", "folk",
    "gospel", "christian", "religious", "christmas", "soundtrack",
    "80s", "90s", "70s", "60s", "oldies", "retro", "disco", "funk",
    "smooth jazz", "acid jazz", "nu jazz", "fusion", "bebop", "swing",
    "lo-fi", "lofi", "study", "focus", "workout", "gym", "morning",
    "night", "cafe", "coffee", "dinner", "romantic", "relax", "chill",
    "dance", "club", "party", "summer", "tropical", "beach", "nature",
    "rain", "spa", "yoga", "new age", "deep house", "progressive",
    "drum and bass", "breakbeat", "downtempo", "trip hop", "shoegaze",
    "post rock", "math rock", "grunge", "emo", "hardcore", "ska",
    "dub", "dancehall", "roots", "afrobeat", "highlife", "afropop",
    "flamenco", "fado", "celtic", "irish", "scottish", "french",
    "german", "italian", "spanish", "brazilian", "mexican", "cuban",
    "korean", "japanese", "chinese", "arabic", "indian", "bollywood",
    "turkish", "greek", "russian", "polish", "czech", "hungarian",
]

# Weather/season -> tag mapping
WEATHER_TAGS = {
    "rainy": ["jazz", "lounge", "piano", "ambient"],
    "sunny": ["pop", "bossa nova", "tropical", "summer"],
    "cloudy": ["indie", "acoustic", "folk", "ambient"],
    "snowy": ["classical", "christmas", "cozy", "piano"],
    "hot": ["tropical", "latin", "reggae", "summer"],
    "cold": ["jazz", "classical", "lounge", "cozy"],
}

# Time of day -> tag mapping
TIME_TAGS = {
    "morning": ["pop", "acoustic", "breakfast", "morning"],      # 6-10
    "daytime": ["pop", "rock", "hits", "energetic"],             # 10-17
    "evening": ["jazz", "lounge", "dinner", "relaxing"],         # 17-21
    "night": ["ambient", "chillout", "sleep", "lounge"],         # 21-6
}


# ============================================================
# Search engine helper functions (v2.0)
# ============================================================

def translate_query(query: str) -> str:
    """Convert multilingual query to English tags"""
    query_lower = query.lower().strip()

    # 1. Check exact mapping
    if query in LANG_MAP:
        return LANG_MAP[query]
    if query_lower in LANG_MAP:
        return LANG_MAP[query_lower]

    # 2. Convert each word
    words = query.split()
    translated = []
    for word in words:
        if word in LANG_MAP:
            translated.append(LANG_MAP[word])
        elif word.lower() in LANG_MAP:
            translated.append(LANG_MAP[word.lower()])
        else:
            translated.append(word)

    return " ".join(translated)


def levenshtein_distance(storage1: str, storage2: str) -> int:
    """Calculate edit distance between two strings"""
    if len(storage1) < len(storage2):
        return levenshtein_distance(storage2, storage1)
    if len(storage2) == 0:
        return len(storage1)

    previous_row = range(len(storage2) + 1)
    for i, c1 in enumerate(storage1):
        current_row = [i + 1]
        for j, c2 in enumerate(storage2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def fuzzy_match(query: str, threshold: int = 2) -> str:
    """Typo correction - return closest known tag"""
    query_lower = query.lower().strip()

    # Return as-is if exact match
    if query_lower in KNOWN_TAGS:
        return query_lower

    # Skip fuzzy for short words (<3 chars) to prevent fm->edm
    if len(query_lower) < 3:
        return query_lower

    # Exclude generic radio words from fuzzy
    SKIP_WORDS = {"radio", "fm", "am", "hd", "the", "and", "or", "with"}
    if query_lower in SKIP_WORDS:
        return query_lower

    # Find closest tag
    best_match = None
    best_distance = threshold + 1

    for tag in KNOWN_TAGS:
        # Skip tags too short or long
        if abs(len(tag) - len(query_lower)) > threshold:
            continue

        distance = levenshtein_distance(query_lower, tag)
        if distance < best_distance:
            best_distance = distance
            best_match = tag

    return best_match if best_match else query_lower


def merge_compound_tokens(tokens: list) -> list:
    """Merge compound genres from token list"""
    if len(tokens) < 2:
        return tokens

    result = []
    i = 0
    while i < len(tokens):
        merged = False

        # Check 3-word combinations
        if i + 2 < len(tokens):
            key3 = (tokens[i], tokens[i+1], tokens[i+2])
            if key3 in COMPOUND_GENRES:
                result.append(COMPOUND_GENRES[key3])
                i += 3
                merged = True
                continue

        # Check 2-word combinations
        if i + 1 < len(tokens):
            key2 = (tokens[i], tokens[i+1])
            if key2 in COMPOUND_GENRES:
                result.append(COMPOUND_GENRES[key2])
                i += 2
                merged = True
                continue

        if not merged:
            result.append(tokens[i])
            i += 1

    return result


def parse_search_query(query: str) -> dict:
    """
    Parse search query (with operators)

    Supported operators:
    - AND: default (space)
    - OR: '|' or 'OR'
    - NOT: '-' prefix
    - "exact": quotes for exact phrase

    Returns:
        {
            "must": [...],      # AND condition (all must match)
            "should": [...],    # OR condition (at least one)
            "must_not": [...],  # NOT condition (exclude)
            "exact": [...],     # Exact phrase
        }
    """
    result = {
        "must": [],
        "should": [],
        "must_not": [],
        "exact": [],
    }

    # Extract exact phrases in quotes
    import re
    exact_matches = re.findall(r'"([^"]+)"', query)
    for match in exact_matches:
        result["exact"].append(match.lower())
    query = re.sub(r'"[^"]+"', '', query)

    # Split by OR
    if ' OR ' in query or '|' in query:
        query = query.replace(' OR ', '|')
        or_parts = [p.strip() for p in query.split('|') if p.strip()]
        for part in or_parts:
            if part.startswith('-'):
                result["must_not"].append(part[1:].lower())
            else:
                result["should"].append(part.lower())
    else:
        # Split by space (AND)
        tokens = query.split()
        for token in tokens:
            token = token.strip()
            if not token:
                continue
            if token.startswith('-'):
                result["must_not"].append(token[1:].lower())
            else:
                result["must"].append(token.lower())

    return result


def score_station(station: dict, query_parts: dict, matched_tags: set) -> float:
    """Calculate station score"""
    score = 0.0

    # Base popularity score (log scale)
    import math
    votes = station.get("votes", 0)
    if votes > 0:
        score += math.log10(votes + 1)

    # Bitrate bonus
    bitrate = station.get("bitrate", 0)
    if bitrate >= 320:
        score += 3
    elif bitrate >= 256:
        score += 2
    elif bitrate >= 192:
        score += 1

    # Matching tags count bonus
    station_tags = station.get("tags", "").lower()
    match_count = sum(1 for tag in matched_tags if tag in station_tags)
    score += match_count * 2

    # Exact phrase match bonus
    for exact in query_parts.get("exact", []):
        if exact in station_tags or exact in station.get("name", "").lower():
            score += 5

    # must_not penalty
    for exclude in query_parts.get("must_not", []):
        if exclude in station_tags:
            score -= 100  # Effectively exclude

    return score


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def get_db():
    """SQLite DB connection (singleton). Returns None in lightweight mode."""
    global db_conn
    if CONFIG.get("lightweight"):
        return None  # Lightweight mode: API only, no local DB

    if db_conn:
        return db_conn

    # Check custom path from config first
    custom_path = CONFIG.get("db_path", "")
    if custom_path and os.path.exists(custom_path):
        db_conn = sqlite3.connect(custom_path, check_same_thread=False)
        db_conn.row_factory = sqlite3.Row
        return db_conn

    for path in DB_PATHS:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            db_conn = sqlite3.connect(path, check_same_thread=False)
            db_conn.row_factory = sqlite3.Row
            # Verify it actually has stations (not a corrupt/empty schema)
            try:
                count = db_conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
                if count > 0:
                    return db_conn
            except Exception:
                pass
            db_conn.close()
            db_conn = None

    # No valid DB found — copy package DB to DATA_DIR as first-run setup
    package_db = os.path.join(PACKAGE_DIR, "radio_stations.db")
    if os.path.exists(package_db) and os.path.getsize(package_db) > 0:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            dest = os.path.join(DATA_DIR, "radio_stations.db")
            shutil.copy2(package_db, dest)
            db_conn = sqlite3.connect(dest, check_same_thread=False)
            db_conn.row_factory = sqlite3.Row
            return db_conn
        except Exception:
            # Fallback: use package DB directly
            db_conn = sqlite3.connect(package_db, check_same_thread=False)
            db_conn.row_factory = sqlite3.Row
            return db_conn

    return None


# ============================================================
# Memory index (ultra-fast search)
# ============================================================

# Global index
_stations_cache = None      # All stations list
_tag_index = None           # {tag: [indices...]}
_name_words_index = None    # {word: [indices...]}


def build_memory_index():
    """Load DB to memory and build index (once)"""
    global _stations_cache, _tag_index, _name_words_index

    if _stations_cache is not None:
        return  # Already loaded

    db = get_db()
    if not db:
        _stations_cache = []
        _tag_index = {}
        _name_words_index = {}
        return

    try:
        cursor = db.cursor()
        cursor.execute("""
            SELECT * FROM stations
            WHERE is_alive = 1 OR is_alive IS NULL
            ORDER BY clickcount DESC
        """)
        rows = cursor.fetchall()

        _stations_cache = format_stations(rows)
        _tag_index = {}
        _name_words_index = {}

        for idx, station in enumerate(_stations_cache):
            # Tag index
            tags = station.get("tags", "").lower()
            for tag in tags.split(","):
                tag = tag.strip()
                if tag:
                    if tag not in _tag_index:
                        _tag_index[tag] = []
                    _tag_index[tag].append(idx)

            # Name word index
            name = station.get("name", "").lower()
            for word in name.split():
                word = word.strip()
                if len(word) >= 2:
                    if word not in _name_words_index:
                        _name_words_index[word] = []
                    _name_words_index[word].append(idx)

        import sys
        print(f"Memory index built: {len(_stations_cache)} stations, {len(_tag_index)} tags", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"Index build error: {e}", flush=True)
        _stations_cache = []
        _tag_index = {}
        _name_words_index = {}


def fast_search_by_name(query: str, limit: int = 20) -> list:
    """Search by name - uses DB query directly (no memory index needed)"""
    db = get_db()
    if db:
        try:
            cursor = db.cursor()
            cursor.execute("""
                SELECT * FROM stations
                WHERE LOWER(name) LIKE ? AND (is_alive = 1 OR is_alive IS NULL)
                ORDER BY votes DESC, clickcount DESC
                LIMIT ?
            """, (f"%{query.lower()}%", limit))
            return format_stations(cursor.fetchall())
        except Exception:
            pass

    # Fallback to memory index
    build_memory_index()

    if not _stations_cache:
        return []

    query_lower = query.lower()
    query_words = query_lower.split()

    # 1. Exact name matching
    exact_matches = []
    partial_matches = []

    for idx, station in enumerate(_stations_cache):
        name_lower = station.get("name", "").lower()

        # Full query in name
        if query_lower in name_lower:
            exact_matches.append(idx)
        # All words in name
        elif all(w in name_lower for w in query_words):
            partial_matches.append(idx)

    result_indices = (exact_matches + partial_matches)[:limit]
    return [_stations_cache[i] for i in result_indices]


def fast_search_by_tag(tags: list, limit: int = 20) -> list:
    """Search by tag - uses DB query directly (no memory index needed)"""
    db = get_db()
    if db and tags:
        try:
            cursor = db.cursor()
            conditions = []
            params = []
            for tag in tags:
                conditions.append("LOWER(tags) LIKE ?")
                params.append(f"%{tag.lower()}%")
            sql = f"""
                SELECT * FROM stations
                WHERE ({' OR '.join(conditions)}) AND (is_alive = 1 OR is_alive IS NULL)
                ORDER BY votes DESC, clickcount DESC
                LIMIT ?
            """
            params.append(limit)
            cursor.execute(sql, params)
            return format_stations(cursor.fetchall())
        except Exception:
            pass

    # Fallback to memory index
    build_memory_index()

    if not _stations_cache or not _tag_index:
        return []

    # Collect indices for each tag
    all_indices = set()
    for tag in tags:
        tag_lower = tag.lower()
        # Exact match
        if tag_lower in _tag_index:
            all_indices.update(_tag_index[tag_lower])
        # Partial match (word in tag)
        else:
            for idx_tag, indices in _tag_index.items():
                if tag_lower in idx_tag or idx_tag in tag_lower:
                    all_indices.update(indices[:50])  # Limit partial matches

    # Sort by votes (already sorted, just get by index order)
    sorted_indices = sorted(all_indices)[:limit]
    return [_stations_cache[i] for i in sorted_indices]


def load_json(filepath: str) -> list:
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []


def save_json(filepath: str, data: list):
    ensure_data_dir()
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _http_get(base_url: str, endpoint: str, params: dict = None, timeout: int = 10) -> list:
    """Generic HTTP GET → JSON list"""
    url = f"{base_url}/{endpoint}"
    if params:
        query = urllib.parse.urlencode(params)
        url = f"{url}?{query}"
    try:
        headers = {"User-Agent": "RadioMCP/1.0"}
        api_key = CONFIG.get("radiograph_api_key", "")
        if api_key and base_url == RADIOGRAPH_BASE:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            # RadioGraph wraps in {"data": [...]} for /search
            if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                return data["data"]
            if isinstance(data, list):
                return data
            return [data] if data else []
    except Exception:
        return []


def api_get(endpoint: str, params: dict = None) -> list:
    """RadioGraph API GET call"""
    return _http_get(RADIOGRAPH_BASE, endpoint, params)


def api_search(query: str, limit: int = 20) -> list:
    """Search via RadioGraph API"""
    results = []
    rg_results = api_get("search", {"q": query, "limit": limit})
    for s in rg_results:
        station = format_station(s)
        if station:
            results.append(station)
    return results[:limit]


def api_country(country_code: str, limit: int = 20) -> list:
    """Search by country via RadioGraph API"""
    cc = country_code.upper()
    results = []
    # Use fast /stations?countrycode= endpoint
    stations = api_get("stations", {"countrycode": cc, "limit": limit})
    for s in stations:
        station = format_station(s)
        if station:
            results.append(station)
    return results[:limit]


def api_popular(limit: int = 20) -> list:
    """Get popular stations via RadioGraph API"""
    results = []
    for s in api_get("stations/toplisteners", {"limit": limit}):
        station = format_station(s)
        if station:
            results.append(station)
    return results[:limit]


def get_fresh_url(name: str) -> str:
    """Get latest URL from RadioGraph API by name (handle token expiry)"""
    if not name:
        return ""
    # Use fast /search endpoint
    resp = api_get("search", {"q": name, "limit": 5})
    results = resp.get("data", []) if isinstance(resp, dict) else resp
    # Exact match first
    for s in results:
        if s.get("name", "").lower() == name.lower():
            return s.get("url_resolved") or s.get("url", "")
    # If none, first result
    if results:
        return results[0].get("url_resolved") or results[0].get("url", "")
    return ""


# Blocklist (runtime state, loaded from blocklist.json)
BLOCK_LIST = []
BLOCKED_URLS = set()
BLOCKED_UUIDS = set()

LOCAL_BLOCKLIST_PATHS = [
    os.path.join(PACKAGE_DIR, "blocklist.json"),
    os.path.expanduser("~/.radiocli/blocklist.json"),
]

BLOCKLIST_URLS = [
    "https://raw.githubusercontent.com/meshpop/radiomcp/main/blocklist.json",
]

def load_local_blocklist():
    """Load from local blocklist.json"""
    global BLOCK_LIST, BLOCKED_URLS, BLOCKED_UUIDS
    for path in LOCAL_BLOCKLIST_PATHS:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                BLOCK_LIST = [b["pattern"] for b in data.get("blocked", [])]
                BLOCKED_URLS = set(data.get("blocked_urls", []))
                BLOCKED_UUIDS = set(data.get("blocked_uuids", []))
                return
            except Exception:
                pass

load_local_blocklist()

def fetch_remote_blocklist():
    """Fetch blocklist from GitHub"""
    global BLOCK_LIST, BLOCKED_URLS, BLOCKED_UUIDS
    for url in BLOCKLIST_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "RadioMCP/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for b in data.get("blocked", []):
                if b["pattern"] not in BLOCK_LIST:
                    BLOCK_LIST.append(b["pattern"])
            BLOCKED_URLS.update(data.get("blocked_urls", []))
            BLOCKED_UUIDS.update(data.get("blocked_uuids", []))

            # Also load patterns/urls/station_ids/domains from blocklist.json format
            for p in data.get("patterns", []):
                if p not in BLOCK_LIST:
                    BLOCK_LIST.append(p)
            BLOCKED_URLS.update(data.get("urls", []))
            BLOCKED_UUIDS.update(data.get("station_ids", []))
            for domain in data.get("domains", []):
                if domain not in BLOCK_LIST:
                    BLOCK_LIST.append(domain)
            return
        except Exception:
            continue


def purge_blocked_from_db():
    """Remove all blocked stations from local DB. Called on startup."""
    conn = get_db()
    if not conn:
        return 0
    removed = 0
    try:
        cur = conn.cursor()
        # Purge by name pattern
        for pattern in BLOCK_LIST:
            cur.execute("SELECT COUNT(*) FROM stations WHERE LOWER(name) LIKE ?", (f"%{pattern.lower()}%",))
            count = cur.fetchone()[0]
            if count > 0:
                cur.execute("DELETE FROM stations WHERE LOWER(name) LIKE ?", (f"%{pattern.lower()}%",))
                removed += count
        # Purge by URL
        for url in BLOCKED_URLS:
            cur.execute("DELETE FROM stations WHERE url = ? OR url_resolved = ?", (url, url))
            removed += cur.rowcount
        # Purge by UUID
        for uuid in BLOCKED_UUIDS:
            cur.execute("DELETE FROM stations WHERE stationuuid = ?", (uuid,))
            removed += cur.rowcount
        if removed > 0:
            conn.commit()
    except Exception:
        pass
    return removed


def _auto_blocklist_sync():
    """Background: fetch remote blocklist and purge blocked stations from DB."""
    import sys
    try:
        fetch_remote_blocklist()
        removed = purge_blocked_from_db()
        total = len(BLOCK_LIST) + len(BLOCKED_URLS) + len(BLOCKED_UUIDS)
        if total > 0 or removed > 0:
            sys.stderr.write(f"[radiomcp] Blocklist synced: {total} rules, {removed} stations purged\n")
            sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"[radiomcp] Blocklist sync failed: {e}\n")

def is_blocked(name: str, url: str = "", uuid: str = "") -> bool:
    """Check blocklist"""
    if uuid and uuid in BLOCKED_UUIDS:
        return True
    if url and url in BLOCKED_URLS:
        return True
    if name:
        name_lower = name.lower()
        if any(b.lower() in name_lower for b in BLOCK_LIST):
            return True
    return False


def sync_popular_stations():
    """Sync popular stations on startup (RadioGraph API -> DB)"""
    db = get_db()
    if not db:
        return

    # Sync popular stations from major countries
    countries = ["KR", "US", "JP", "GB", "DE", "FR"]
    total_added = 0

    for country in countries:
        try:
            url = f"{API_BASE}/stations/bycountrycode/{country}?limit=50"
            req = urllib.request.Request(url, headers={"User-Agent": "RadioMCP/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                stations = json.loads(resp.read().decode())

            cursor = db.cursor()
            for s in stations:
                uuid = s.get("stationuuid", "")
                if not uuid:
                    continue

                # If exists, update URL only (token refresh)
                cursor.execute("SELECT url_resolved FROM stations WHERE stationuuid = ?", (uuid,))
                existing = cursor.fetchone()

                new_url = s.get("url_resolved") or s.get("url", "")
                if existing:
                    # Update if URL changed
                    if existing[0] != new_url:
                        cursor.execute("""
                            UPDATE stations SET url_resolved = ?, is_alive = 1 WHERE stationuuid = ?
                        """, (new_url, uuid))
                else:
                    # Add new
                    name = s.get("name", "")

                    # Auto-set tags (Korean stations)
                    tags = s.get("tags", "")
                    if not tags and country == "KR":
                        if any(x in name for x in ["Classic", "classical"]):
                            tags = "classical,classical"
                        elif any(x in name for x in ["1R", "1Radio", "", "news", "News"]):
                            tags = "news,talk,news"
                        elif any(x in name for x in ["Cool", "FM4U", "Power", "Love"]):
                            tags = "music,pop,kpop"
                        elif "FM" in name:
                            tags = "music,pop"

                    cursor.execute("""
                        INSERT OR IGNORE INTO stations
                        (stationuuid, name, url, url_resolved, country, countrycode, tags, bitrate, votes, clickcount, is_alive)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """, (
                        uuid, name, s.get("url", ""), new_url,
                        s.get("country", ""), country, tags,
                        s.get("bitrate", 0), s.get("votes", 0), s.get("clickcount", 0)
                    ))
                    total_added += 1

            db.commit()
        except Exception:
            pass

# Sync popular stations on startup (called from main)

def format_station(s) -> dict:
    """Format station info (dict or sqlite Row). None if blocked or invalid."""
    if not s:
        return None
    if isinstance(s, sqlite3.Row):
        s = dict(s)
    name = s.get("name", "Unknown")
    url = s.get("url_resolved") or s.get("url", "")
    uuid = s.get("stationuuid", "")
    if is_blocked(name, url, uuid):
        return None
    return {
        "id": s.get("stationuuid", ""),
        "name": name,
        "url": s.get("url_resolved") or s.get("url", ""),
        "country": s.get("country", ""),
        "countrycode": s.get("countrycode", ""),
        "tags": s.get("tags", ""),
        "bitrate": s.get("bitrate", 0),
        "votes": s.get("votes", 0),
    }


def format_stations(items) -> list:
    """Format multiple stations (filter blocked)"""
    return [s for s in (format_station(x) for x in items) if s]


def expand_tags(query: str) -> list:
    """Expand query to multiple tags (compound + synonyms)"""
    # Split by space
    words = query.lower().strip().split()
    all_tags = set()

    # Include original query
    all_tags.add(query.lower().strip())

    # Expand synonyms for each word
    for word in words:
        all_tags.add(word)
        if word in TAG_SYNONYMS:
            all_tags.update(TAG_SYNONYMS[word])

    # Check 2-word combos (e.g. "bossa nova")
    if len(words) >= 2:
        for i in range(len(words) - 1):
            combo = f"{words[i]} {words[i+1]}"
            all_tags.add(combo)
            if combo in TAG_SYNONYMS:
                all_tags.update(TAG_SYNONYMS[combo])

    return list(all_tags)


def get_time_of_day() -> str:
    """Return current time slot"""
    hour = datetime.now().hour
    if 6 <= hour < 10:
        return "morning"
    elif 10 <= hour < 17:
        return "daytime"
    elif 17 <= hour < 21:
        return "evening"
    else:
        return "night"


def db_search(query: str, field: str = "tags", limit: int = 20) -> list:
    """Search from DB"""
    db = get_db()
    if not db:
        return []

    try:
        cursor = db.cursor()
        sql = f"""
            SELECT * FROM stations
            WHERE {field} LIKE ? AND (is_alive = 1 OR is_alive IS NULL)
            ORDER BY clickcount DESC
            LIMIT ?
        """
        cursor.execute(sql, (f"%{query}%", limit))
        return format_stations(cursor.fetchall())
    except Exception as e:
        pass  # DB error
        return []


def db_search_country(code: str, limit: int = 20) -> list:
    """Search by country from DB"""
    db = get_db()
    if not db:
        return []

    try:
        cursor = db.cursor()
        sql = """
            SELECT * FROM stations
            WHERE countrycode = ? AND (is_alive = 1 OR is_alive IS NULL)
            ORDER BY clickcount DESC
            LIMIT ?
        """
        cursor.execute(sql, (code.upper(), limit))
        return format_stations(cursor.fetchall())
    except Exception as e:
        pass  # DB error
        return []


def db_get_popular(limit: int = 20) -> list:
    """Popular stations from DB"""
    db = get_db()
    if not db:
        return []

    try:
        cursor = db.cursor()
        sql = """
            SELECT * FROM stations
            WHERE is_alive = 1 OR is_alive IS NULL
            ORDER BY clickcount DESC
            LIMIT ?
        """
        cursor.execute(sql, (limit,))
        return format_stations(cursor.fetchall())
    except Exception as e:
        pass  # DB error
        return []


def mark_station_dead(url: str):
    """Mark station as dead"""
    db = get_db()
    if not db:
        return

    try:
        cursor = db.cursor()
        cursor.execute("""
            UPDATE stations
            SET is_alive = 0, fail_count = COALESCE(fail_count, 0) + 1,
                last_checked_at = ?
            WHERE url = ? OR url_resolved = ?
        """, (datetime.now().isoformat(), url, url))
        db.commit()
        print(f"Marked dead: {url}", flush=True)
    except Exception as e:
        pass  # DB update error


def is_valid_station(station: dict) -> bool:
    """Validate if station can be added to DB"""
    url = station.get("url_resolved") or station.get("url", "")

    # Exclude URLs with token/session params
    if "?" in url or "&" in url:
        return False

    # Exclude suspicious domains
    blocked_domains = [
        "duckdns.org", "no-ip.org", "ddns.net", "iptime.org",
        "zstream.win", "bsod.kr", "localhost", "127.0.0.1"
    ]
    url_lower = url.lower()
    for domain in blocked_domains:
        if domain in url_lower:
            return False

    # Exclude direct IP addresses
    import re
    if re.search(r"https?://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", url_lower):
        return False

    # Minimum quality criteria
    if station.get("votes", 0) < 5:
        return False

    return True


def add_station_to_db(station: dict):
    """Add new station to DB (if validation passes)"""
    if not is_valid_station(station):
        return

    db = get_db()
    if not db:
        return

    try:
        cursor = db.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO stations
            (stationuuid, name, url, url_resolved, country, countrycode, tags, bitrate, votes, clickcount, is_alive, last_checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
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
            datetime.now().isoformat()
        ))
        db.commit()
        # print(f"Added to DB: {station.get('name')}", flush=True)
    except Exception as e:
        pass  # DB insert error


def db_advanced_search(
    tags: list = None,
    country: str = None,
    language: str = None,
    min_bitrate: int = 0,
    codec: str = None,
    limit: int = 50
) -> list:
    """Search with compound filters from DB"""
    db = get_db()
    if not db:
        return []

    try:
        cursor = db.cursor()
        conditions = ["(is_alive = 1 OR is_alive IS NULL)"]
        params = []

        # Tag filter (OR)
        if tags:
            tag_conditions = []
            for tag in tags:
                tag_conditions.append("tags LIKE ?")
                params.append(f"%{tag}%")
            conditions.append(f"({' OR '.join(tag_conditions)})")

        # Country filter
        if country:
            conditions.append("countrycode = ?")
            params.append(country.upper())

        # Language filter
        if language:
            conditions.append("language LIKE ?")
            params.append(f"%{language}%")

        # Bitrate filter
        if min_bitrate > 0:
            conditions.append("bitrate >= ?")
            params.append(min_bitrate)

        # Codec filter
        if codec:
            conditions.append("codec LIKE ?")
            params.append(f"%{codec}%")

        sql = f"""
            SELECT * FROM stations
            WHERE {' AND '.join(conditions)}
            ORDER BY clickcount DESC
            LIMIT ?
        """
        params.append(limit)
        cursor.execute(sql, params)
        return format_stations(cursor.fetchall())
    except Exception as e:
        pass  # DB search error
        return []


def _api_only_search(query: str, detected_country: str = None, limit: int = 20) -> list:
    """API-only search for lightweight mode (no local DB)."""
    all_results = []
    seen_urls = set()

    # Translate query for multilingual support
    translated = translate_query(query)
    words = translated.lower().split()
    corrected_words = [fuzzy_match(w) for w in words]
    merged = merge_compound_tokens(corrected_words)

    # 1. Country-specific search
    if detected_country:
        code = urllib.parse.quote(detected_country.upper())
        api_results = api_get(f"stations/bycountrycode/{code}", {"limit": limit})
        for s in api_results:
            url = s.get("url_resolved") or s.get("url", "")
            if url and url not in seen_urls:
                station = format_station(s)
                if station:
                    seen_urls.add(url)
                    station["source"] = "api"
                    all_results.append(station)

    # 2. Search by name
    encoded = urllib.parse.quote(query)
    name_results = api_get(f"stations/byname/{encoded}", {"limit": limit})
    for s in name_results:
        url = s.get("url_resolved") or s.get("url", "")
        if url and url not in seen_urls:
            station = format_station(s)
            if station:
                seen_urls.add(url)
                station["source"] = "api"
                station["match_type"] = "name"
                all_results.append(station)

    # 3. Search by tag (expanded)
    if len(all_results) < limit:
        for tag in merged[:3]:
            if tag.lower() in {"fm", "am", "radio", "the", "and", "or"}:
                continue
            encoded_tag = urllib.parse.quote(tag)
            tag_results = api_get(f"stations/bytag/{encoded_tag}", {"limit": limit // 2})
            for s in tag_results:
                url = s.get("url_resolved") or s.get("url", "")
                if url and url not in seen_urls:
                    station = format_station(s)
                    if station:
                        seen_urls.add(url)
                        station["source"] = "api"
                        station["match_type"] = "tag"
                        all_results.append(station)
            if len(all_results) >= limit:
                break

    all_results.sort(key=lambda x: x.get("votes", 0), reverse=True)
    return all_results[:limit]


@mcp.tool()
def search(query: str, limit: int = 20) -> list:
    """
    Search radio stations by keyword. Fast local DB search (~5ms).

    SEARCH TIPS:
    - Genre: jazz, rock, classical, electronic, lounge, ambient, news, talk
    - Combine terms: "smooth jazz", "korean pop", "japanese news"
    - Station names: "BBC", "NPR", "KBS"
    - For country-specific: use search_by_country(country_code)
    - For high quality: use advanced_search(min_bitrate=192)
    - For mood-based: use recommend(mood)

    EXPAND SEARCH: If few results, try related terms:
    - jazz → smooth jazz, bebop, swing
    - news → talk, information
    - relaxing → lounge, ambient, chillout

    Multilingual supported: jazz, ジャズ, 爵士 all work.

    Args:
        query: Search term (genre, station name, keyword)
        limit: Number of results (default 20)

    Returns:
        List of stations with name, url, country, tags, bitrate
    """
    # Detect country name (korea, japan, etc.)
    detected_country = None
    query_lower = query.lower()
    for name, code in COUNTRY_NAMES.items():
        if name in query_lower:
            detected_country = code
            break

    # Lightweight mode: dual API search
    if not get_db():
        return api_search(query, limit)

    name_results = []
    tag_results = []
    country_results = []
    seen_urls = set()

    # 1. Search by name first (most accurate)
    for r in fast_search_by_name(query, limit):
        if r["url"] not in seen_urls:
            seen_urls.add(r["url"])
            r["source"] = "db"
            r["match_type"] = "name"
            name_results.append(r)

    # Return early if name match sufficient (skip tag search)
    if len(name_results) >= limit:
        return name_results[:limit]

    # 2. Multilingual + tag search (if name match insufficient)
    translated = translate_query(query)
    words = translated.lower().split()
    corrected_words = [fuzzy_match(w) for w in words]
    merged = merge_compound_tokens(corrected_words)

    # Expand synonyms (genre queries only)
    # Skip tag expansion for generic words (fm, radio, beach)
    SKIP_TAG_EXPAND = {"fm", "am", "radio", "beach", "music", "the", "and", "or"}
    all_tags = []
    for word in merged:
        if word.lower() not in SKIP_TAG_EXPAND:
            all_tags.append(word)
            if word in TAG_SYNONYMS:
                all_tags.extend(TAG_SYNONYMS[word][:2])

    # 2-1. If country detected, search that country first (priority!)
    if detected_country:
        # Search country + tag/name combo
        db = get_db()
        if db:
            try:
                cursor = db.cursor()
                # Search both original + translated tags
                original_words = [w for w in query.split() if w.lower() not in COUNTRY_NAMES]
                search_terms = list(set(original_words + [t for t in all_tags if t.lower() not in COUNTRY_NAMES]))

                if search_terms:
                    # Search in tags OR name
                    conditions = []
                    params = []
                    for term in search_terms:
                        conditions.append("(LOWER(tags) LIKE ? OR LOWER(name) LIKE ?)")
                        params.extend([f"%{term.lower()}%", f"%{term.lower()}%"])

                    sql = f"""
                        SELECT * FROM stations
                        WHERE countrycode = ? AND (is_alive = 1 OR is_alive IS NULL)
                        AND ({' OR '.join(conditions)})
                        ORDER BY votes DESC, clickcount DESC
                        LIMIT ?
                    """
                    cursor.execute(sql, [detected_country] + params + [limit])
                    for r in format_stations(cursor.fetchall()):
                        if r["url"] not in seen_urls:
                            seen_urls.add(r["url"])
                            r["source"] = "db"
                            r["match_type"] = "country_tag"
                            country_results.append(r)
            except Exception:
                pass

    # Search only if tags exist (when country results insufficient)
    if all_tags and len(country_results) < limit // 2:
        for r in fast_search_by_tag(all_tags, limit):
            if r["url"] not in seen_urls:
                seen_urls.add(r["url"])
                r["source"] = "db"
                r["match_type"] = "tag"
                tag_results.append(r)

    # Return country results first if available
    if country_results:
        # Country matches first, fill with name/tag matches if needed
        remaining = limit - len(country_results)
        if remaining > 0:
            # Add name matches (country filter)
            for r in name_results:
                if r.get("countrycode", "").upper() == detected_country and r["url"] not in seen_urls:
                    country_results.append(r)
                    if len(country_results) >= limit:
                        break
        country_results.sort(key=lambda x: x.get("votes", 0), reverse=True)
        return country_results[:limit]

    # Name matches first + fill with tag matches
    all_results = name_results + tag_results

    # 3. RadioGraph API fallback (only if no name match and results insufficient)
    has_name_match = any(r.get("match_type") == "name" for r in all_results)
    if not has_name_match and len(all_results) < limit // 2:
        rg_results = api_get("search", {"q": query, "limit": limit})
        for s in rg_results:
            url = s.get("url_resolved") or s.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                station = format_station(s)
                if station:
                    all_results.append(station)

    # Sort: country match > name match > votes
    def sort_key(r):
        country_match = 1 if detected_country and r.get("countrycode", "").upper() == detected_country else 0
        name_match = 1 if r.get("match_type") == "name" else 0
        votes = r.get("votes", 0)
        return (country_match, name_match, votes)

    all_results.sort(key=sort_key, reverse=True)
    return all_results[:limit]


@mcp.tool()
def advanced_search(
    query: str = None,
    country: str = None,
    language: str = None,
    tags: str = None,
    min_bitrate: int = 0,
    codec: str = None,
    sort_by: str = "votes",
    limit: int = 20
) -> list:
    """
    Advanced search with multiple filters combined.

    Supports:
    - Multilingual queries (Korean, Japanese, Chinese, etc.)
    - Search operators: "exact phrase", -exclude, term1 OR term2
    - Fuzzy matching for typos
    - Combined filters (country + genre + bitrate)

    Args:
        query: Search keywords (supports operators like "smooth jazz" -vocal)
        country: Country code filter (KR, US, JP, etc.)
        language: Language filter (korean, english, japanese, etc.)
        tags: Comma-separated tags (jazz,lounge,chill)
        min_bitrate: Minimum bitrate in kbps (128, 192, 256, 320)
        codec: Audio codec filter (MP3, AAC, OGG, FLAC)
        sort_by: Sort by: votes, bitrate, name (default: votes)
        limit: Number of results

    Returns:
        List of matching radio stations

    Examples:
        - advanced_search(query="jazz")  # Korean → jazz
        - advanced_search(query="lounge -vocal", min_bitrate=256)
        - advanced_search(country="KR", tags="pop,kpop")
        - advanced_search(query='"smooth jazz"', sort_by="bitrate")
    """
    all_results = []
    seen_urls = set()
    search_tags = []

    # 1. Query processing
    if query:
        # Multilingual translation
        translated = translate_query(query)

        # Parse search operators
        parsed = parse_search_query(translated)

        # Fuzzy matching + compound word merge
        must_tags = []
        for term in parsed["must"]:
            corrected = fuzzy_match(term)
            must_tags.append(corrected)

        # Merge compound words
        merged = merge_compound_tokens(must_tags)

        # Expand synonyms
        for tag in merged:
            search_tags.append(tag)
            if tag in TAG_SYNONYMS:
                search_tags.extend(TAG_SYNONYMS[tag][:3])

        # should (OR) condition
        for term in parsed["should"]:
            corrected = fuzzy_match(term)
            search_tags.append(corrected)

        # exact condition (filter later)
        exact_phrases = parsed["exact"]

        # must_not condition (filter later)
        exclude_terms = parsed["must_not"]
    else:
        exact_phrases = []
        exclude_terms = []

    # 2. Process tag parameters
    if tags:
        for tag in tags.split(","):
            tag = tag.strip().lower()
            if tag:
                search_tags.append(tag)

    # 3. DB search
    db_results = db_advanced_search(
        tags=search_tags if search_tags else None,
        country=country,
        language=language,
        min_bitrate=min_bitrate,
        codec=codec,
        limit=limit * 2
    )

    for r in db_results:
        if r["url"] not in seen_urls:
            # exact phrase filter
            if exact_phrases:
                station_text = f"{r.get('name', '')} {r.get('tags', '')}".lower()
                if not all(phrase in station_text for phrase in exact_phrases):
                    continue

            # exclude filter
            if exclude_terms:
                station_tags = r.get("tags", "").lower()
                if any(term in station_tags for term in exclude_terms):
                    continue

            seen_urls.add(r["url"])
            r["source"] = "db"
            all_results.append(r)

    # 4. API search (if results insufficient)
    if len(all_results) < limit and search_tags:
        for tag in search_tags[:3]:
            params = {"limit": limit}
            if country:
                params["countrycode"] = country.upper()
            if min_bitrate > 0:
                params["bitrateMin"] = min_bitrate

            encoded_tag = urllib.parse.quote(tag)
            api_results = api_get(f"stations/bytag/{encoded_tag}", params)

            for s in api_results:
                url = s.get("url_resolved") or s.get("url", "")
                if url and url not in seen_urls:
                    # Force country filter (API may ignore)
                    if country and s.get("countrycode", "").upper() != country.upper():
                        continue

                    station = format_station(s)
                    if not station:
                        continue

                    # exact/exclude filter
                    station_text = f"{station.get('name', '')} {station.get('tags', '')}".lower()
                    if exact_phrases and not all(p in station_text for p in exact_phrases):
                        continue
                    if exclude_terms and any(t in station.get("tags", "").lower() for t in exclude_terms):
                        continue

                    seen_urls.add(url)
                    station["source"] = "api"
                    all_results.append(station)

                    if is_valid_station(s):
                        add_station_to_db(s)

    # 5. Sorting
    if sort_by == "bitrate":
        all_results.sort(key=lambda x: x.get("bitrate", 0), reverse=True)
    elif sort_by == "name":
        all_results.sort(key=lambda x: x.get("name", "").lower())
    else:  # votes (default)
        all_results.sort(key=lambda x: x.get("votes", 0), reverse=True)

    return all_results[:limit]


@mcp.tool()
def search_by_country(country_code: str, limit: int = 20) -> list:
    """
    Search radio stations by country.
    Merges results from local DB and RadioGraph API.

    Args:
        country_code: Country code (KR, US, JP, DE, FR, etc.)
        limit: Number of results

    Returns:
        List of radio stations
    """
    # Lightweight mode: dual API
    if not get_db():
        return api_country(country_code, limit)

    all_results = []
    seen_urls = set()

    # 1. Search DB (verified stations)
    db_results = db_search_country(country_code, limit)
    for r in db_results:
        if r["url"] not in seen_urls:
            seen_urls.add(r["url"])
            r["source"] = "db"
            all_results.append(r)

    # 2. RadioGraph API
    api_results_merged = api_country(country_code, limit)
    for r in api_results_merged:
        if r["url"] not in seen_urls:
            seen_urls.add(r["url"])
            all_results.append(r)

    all_results.sort(key=lambda x: x.get("votes", 0), reverse=True)
    return all_results[:limit]


@mcp.tool()
def search_by_language(language: str, limit: int = 20) -> list:
    """
    Search radio stations by language.

    Args:
        language: Language name or code (korean, english, japanese, ko, en, ja)
        limit: Number of results

    Returns:
        List of radio stations
    """
    # Language code -> full name mapping
    LANG_CODES = {
        "ko": "korean", "en": "english", "ja": "japanese", "de": "german",
        "fr": "french", "es": "spanish", "pt": "portuguese", "it": "italian",
        "ru": "russian", "zh": "chinese", "ar": "arabic", "hi": "hindi",
        "nl": "dutch", "pl": "polish", "tr": "turkish", "vi": "vietnamese",
        "th": "thai", "id": "indonesian", "ms": "malay", "sv": "swedish",
    }

    lang = language.lower().strip()
    if lang in LANG_CODES:
        lang = LANG_CODES[lang]

    all_results = []
    seen_urls = set()

    # DB search
    db = get_db()
    if db:
        try:
            cursor = db.cursor()
            cursor.execute("""
                SELECT * FROM stations
                WHERE language LIKE ? AND (is_alive = 1 OR is_alive IS NULL)
                ORDER BY clickcount DESC
                LIMIT ?
            """, (f"%{lang}%", limit))
            for row in cursor.fetchall():
                r = format_station(row)
                if r and r["url"] not in seen_urls:
                    seen_urls.add(r["url"])
                    r["source"] = "db"
                    all_results.append(r)
        except Exception as e:
            pass  # DB error

    # API search (if results insufficient)
    if len(all_results) < limit:
        encoded = urllib.parse.quote(lang)
        api_results = api_get("search", {"q": lang, "limit": limit})

        for s in api_results:
            url = s.get("url_resolved") or s.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                station = format_station(s)
                if station:
                    station["source"] = "api"
                    all_results.append(station)
                if is_valid_station(s):
                    add_station_to_db(s)

    all_results.sort(key=lambda x: x.get("votes", 0), reverse=True)
    return all_results[:limit]


@mcp.tool()
def get_popular(limit: int = 20) -> list:
    """
    Get popular radio stations from local DB.

    Args:
        limit: Number of results

    Returns:
        List of popular stations
    """
    # 1. Get from DB
    results = db_get_popular(limit)

    # 2. Dual API fallback
    if not results:
        results = api_popular(limit)

    return results


@mcp.tool()
def play(url: str, name: str = "") -> dict:
    """
    Play a radio station.

    Args:
        url: Stream URL
        name: Station name (optional)

    Returns:
        Playback status
    """
    global current_station, player_proc

    # Stop existing playback
    stop()

    # Get latest URL from API (handle token expiry)
    play_url = url
    url_refreshed = False
    if name:
        fresh_url = get_fresh_url(name)
        if fresh_url:
            play_url = fresh_url
            url_refreshed = (fresh_url != url)

    # Play by backend
    try:
        if PLAYER_BACKEND == "mpv":
            # === mpv backend ===
            kill_existing_mpv()

            mpv_log = open(os.path.join(DATA_DIR, "mpv.log"), "w")
            player_proc = subprocess.Popen(
                ["mpv", "--no-video", "--no-terminal",
                 "--cache=yes",
                 "--cache-secs=30",
                 "--demuxer-max-bytes=50M",
                 "--demuxer-readahead-secs=20",
                 "--stream-buffer-size=1M",
                 "--network-timeout=30",
                 "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=5",
                 f"--input-ipc-server={MPV_SOCKET}", play_url],
                stdout=subprocess.DEVNULL,
                stderr=mpv_log,
                **_subprocess_detach_kwargs()
            )

            with open(MPV_PID_FILE, 'w') as f:
                f.write(str(player_proc.pid))

            start_mpv_watchdog()

            time.sleep(1)
            if player_proc.poll() is not None:
                mark_station_dead(url)
                return {"status": "error", "message": "Stream failed to start"}

        elif PLAYER_BACKEND == "vlc":
            # === VLC backend ===
            player = get_vlc_player()
            if not player.play(play_url):
                return {"status": "error", "message": "VLC failed to start"}
            time.sleep(1)
            if not player.is_playing():
                mark_station_dead(url)
                return {"status": "error", "message": "Stream failed to start"}

        elif PLAYER_BACKEND == "ffplay":
            # === ffplay backend ===
            player = get_ffplay_player()
            if not player.play(play_url):
                return {"status": "error", "message": "ffplay failed to start"}
            time.sleep(1)
            if not player.is_playing():
                mark_station_dead(url)
                return {"status": "error", "message": "Stream failed to start"}

        elif PLAYER_BACKEND == "browser":
            # === Browser backend ===
            player = get_browser_player()
            player.play(play_url)
            # Browser cannot confirm playback

        else:
            return {"status": "error", "message": f"Unknown player backend: {PLAYER_BACKEND}"}

        # Get station details from DB
        station_info = {"name": name, "url": play_url}
        db = get_db()
        if db and name:
            try:
                cursor = db.cursor()
                cursor.execute(
                    "SELECT country, countrycode, tags, bitrate, votes FROM stations WHERE name = ? LIMIT 1",
                    (name,)
                )
                row = cursor.fetchone()
                if row:
                    station_info["country"] = row[0] or ""
                    station_info["countrycode"] = row[1] or ""
                    station_info["tags"] = row[2] or ""
                    station_info["bitrate"] = row[3] or 0
                    station_info["votes"] = row[4] or 0
            except:
                pass

        current_station = station_info
        save_last_station()  # Save immediately (for resume)

        # Submit to central API for collection (background, silent)
        submit_station_to_api(station_info)

        # Return detailed info for AI
        result = {
            "status": "playing",
            "name": name,
            "url": play_url,
            "country": station_info.get("country", ""),
            "countrycode": station_info.get("countrycode", ""),
            "tags": station_info.get("tags", ""),
            "bitrate": station_info.get("bitrate", 0),
            "votes": station_info.get("votes", 0),
            "tip": "You can describe: genre, country, audio quality to the user"
        }
        if url_refreshed:
            result["url_refreshed"] = True
        if PLAYER_BACKEND == "browser":
            result["warning"] = "Playing in browser. Install mpv for better experience: brew install mpv (macOS) / apt install mpv (Linux) / winget install mpv (Windows)"
        return result
    except Exception as e:
        mark_station_dead(url)
        return {"status": "error", "message": str(e)}


@mcp.tool()
def stop() -> dict:
    """
    Stop radio playback.

    Returns:
        Stop status
    """
    global player_proc, current_station

    result = {"status": "stopped", "backend": PLAYER_BACKEND}

    # Always kill all possible players regardless of current backend
    # (previous sessions may have left orphaned processes)
    kill_existing_mpv()
    player_proc = None

    # Also kill VLC and ffplay if running
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/IM", "vlc.exe", "/F"], capture_output=True, timeout=2)
            subprocess.run(["taskkill", "/IM", "ffplay.exe", "/F"], capture_output=True, timeout=2)
        else:
            subprocess.run(["pkill", "-f", "VLC.*--intf dummy"], capture_output=True, timeout=2)
            subprocess.run(["pkill", "-f", "ffplay.*http"], capture_output=True, timeout=2)
    except Exception:
        pass

    if PLAYER_BACKEND == "browser":
        player = get_browser_player()
        player.stop()
        result["note"] = "Please close the browser tab manually"

    current_station = None
    return result


@mcp.tool()
def get_player_backend() -> dict:
    """
    Get current player backend info.

    Returns:
        Current backend and available options
    """
    available = []
    install_guide = []

    if shutil.which("mpv"):
        available.append("mpv")
    else:
        install_guide.append("mpv: brew install mpv (macOS) / apt install mpv (Linux)")

    if shutil.which("vlc") or shutil.which("cvlc"):
        available.append("vlc")
    else:
        install_guide.append("vlc: brew install vlc (macOS) / apt install vlc (Linux)")

    if shutil.which("ffplay"):
        available.append("ffplay")
    else:
        install_guide.append("ffplay: brew install ffmpeg (macOS) / apt install ffmpeg (Linux)")

    available.append("browser")  # Always available

    result = {
        "current": PLAYER_BACKEND,
        "available": available,
        "recommendation": available[0] if available else "browser"
    }

    # Show install guide if only browser available
    if len(available) == 1:
        result["install_guide"] = install_guide
        result["note"] = "Install mpv, vlc, or ffmpeg for better playback quality"

    return result


@mcp.tool()
def set_player_backend(backend: str) -> dict:
    """
    Set player backend.

    Args:
        backend: 'mpv', 'vlc', 'ffplay', or 'browser'

    Returns:
        New backend status
    """
    global PLAYER_BACKEND

    valid_backends = ["mpv", "vlc", "ffplay", "browser"]
    if backend not in valid_backends:
        return {"status": "error", "message": f"Invalid backend. Choose from: {valid_backends}"}

    # Check backend availability
    if backend == "mpv" and not shutil.which("mpv"):
        return {"status": "error", "message": "mpv not installed. Install: brew install mpv (macOS) / apt install mpv (Linux)"}
    if backend == "vlc" and not (shutil.which("vlc") or shutil.which("cvlc")):
        return {"status": "error", "message": "VLC not installed. Install: brew install vlc (macOS) / apt install vlc (Linux)"}
    if backend == "ffplay" and not shutil.which("ffplay"):
        return {"status": "error", "message": "ffplay not installed. Install: brew install ffmpeg (macOS) / apt install ffmpeg (Linux)"}

    PLAYER_BACKEND = backend
    return {"status": "ok", "backend": PLAYER_BACKEND}


@mcp.tool()
def resume() -> dict:
    """
    Resume last playing station.

    Returns:
        Playback status or error if no last station
    """
    last = load_last_station()
    if not last:
        return {"status": "error", "message": "No last station found"}

    url = last.get("url", "")
    name = last.get("name", "")
    if not url:
        return {"status": "error", "message": "No URL in last station"}

    return play(url, name)


@mcp.tool()
def now_playing() -> dict:
    """
    Get current song info.

    Returns:
        Current song info (title, artist)
    """
    station = current_station or load_last_station()
    if not station:
        return {"status": "not_playing"}

    try:
        data = mpv_ipc_send({"command": ["get_property", "media-title"]}, timeout=2)
        title = data.get("data", "") if data else ""

        if " - " in title:
            artist, song = title.split(" - ", 1)
            return {
                "status": "playing",
                "station": station.get("name", ""),
                "artist": artist.strip(),
                "title": song.strip(),
                "raw": title
            }

        return {
            "status": "playing",
            "station": station.get("name", ""),
            "title": title,
            "raw": title
        }
    except Exception as e:
        return {
            "status": "playing",
            "station": station.get("name", ""),
            "error": str(e)
        }


def record_stream(url: str, duration: int = 12) -> bool:
    """Record audio from stream (using ffmpeg)"""
    if not shutil.which("ffmpeg"):
        return False

    try:
        if os.path.exists(RECORD_FILE):
            os.remove(RECORD_FILE)

        subprocess.run(
            ["ffmpeg", "-y", "-t", str(duration), "-i", url,
             "-ac", "1", "-ar", "16000", "-acodec", "libmp3lame",
             "-loglevel", "quiet", RECORD_FILE],
            timeout=duration + 10
        )
        return os.path.exists(RECORD_FILE)
    except:
        return False




def recognize_with_whisper(audio_file: str) -> dict:
    """Speech recognition with Whisper"""
    try:
        # Try mlx-whisper (Apple Silicon)
        result = subprocess.run(
            ["mlx_whisper", audio_file, "--language", "auto", "--output-format", "json"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0 and result.stdout.strip():
            return {"transcription": result.stdout.strip(), "method": "mlx-whisper"}
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


@mcp.tool()
def recognize_song(duration: int = 12) -> dict:
    """
    Recognize current playing song using multiple methods.

    Methods tried in order:
    1. ICY metadata (instant, from stream)
    2. Whisper (speech-to-text for DJ mentions)

    Args:
        duration: Recording duration in seconds (default 12)

    Returns:
        Recognized song info (title, artist, method)

    Requirements:
        - ffmpeg: brew install ffmpeg
        - whisper: pip install openai-whisper (optional)
    """
    if not current_station:
        return {"status": "not_playing"}

    url = current_station.get("url_resolved") or current_station.get("url")
    if not url:
        return {"error": "no_stream_url"}

    # 1. Check metadata first (fastest)
    metadata = now_playing()
    if metadata.get("title") and metadata.get("status") == "playing":
        result = {
            "status": "recognized",
            "method": "metadata",
            "title": metadata.get("title", ""),
            "artist": metadata.get("artist", ""),
            "station": current_station.get("name", "")
        }
        save_recognized(result)
        return result

    # 2. Record audio
    if not shutil.which("ffmpeg"):
        return {"error": "ffmpeg_not_installed", "hint": "brew install ffmpeg"}

    if not record_stream(url, duration):
        return {"error": "recording_failed"}

    # 3. Try Whisper
    if shutil.which("whisper") or shutil.which("mlx_whisper"):
        whisper_result = recognize_with_whisper(RECORD_FILE)
        if whisper_result and whisper_result.get("transcription"):
            result = {
                "status": "transcribed",
                "method": whisper_result.get("method", "whisper"),
                "transcription": whisper_result.get("transcription", ""),
                "station": current_station.get("name", "")
            }
            save_recognized(result)
            return result

    return {"status": "not_recognized", "hint": "Install ffmpeg for audio recording, whisper for speech recognition"}


def save_recognized(result: dict):
    """Save recognition result"""
    songs = load_json(RECOGNIZED_FILE)
    result["recognized_at"] = datetime.now().isoformat()
    songs.append(result)
    save_json(RECOGNIZED_FILE, songs[-100:])  # Keep last 100


@mcp.tool()
def get_recognized_songs(limit: int = 20) -> list:
    """
    Get history of recognized songs.

    Returns list of previously recognized songs with:
    - title, artist (if available)
    - method (metadata, whisper)
    - station name
    - timestamp

    Args:
        limit: Number of recent songs to return

    Returns:
        List of recognized songs (newest first)
    """
    songs = load_json(RECOGNIZED_FILE)
    return list(reversed(songs[-limit:]))


@mcp.tool()
def get_favorites() -> list:
    """Get favorite stations list."""
    return load_json(FAVORITES_FILE)


@mcp.tool()
def add_favorite(station: dict) -> dict:
    """Add station to favorites."""
    favorites = load_json(FAVORITES_FILE)

    for fav in favorites:
        if fav.get("url") == station.get("url"):
            return {"status": "already_exists", "name": station.get("name")}

    favorites.append(station)
    save_json(FAVORITES_FILE, favorites)

    # Submit to central API for collection (background, silent)
    submit_station_to_api(station)

    return {"status": "added", "name": station.get("name")}


@mcp.tool()
def remove_favorite(index: int) -> dict:
    """Remove station from favorites by index (0-based)."""
    favorites = load_json(FAVORITES_FILE)

    if 0 <= index < len(favorites):
        removed = favorites.pop(index)
        save_json(FAVORITES_FILE, favorites)
        return {"status": "removed", "name": removed.get("name")}

    return {"status": "error", "message": "Invalid index"}


@mcp.tool()
def play_favorite(index: int = 0) -> dict:
    """
    Play a station from favorites.

    Args:
        index: Index of the favorite station (0-based, default: 0 = first)

    Returns:
        Playback status
    """
    favorites = load_json(FAVORITES_FILE)

    if not favorites:
        return {"status": "error", "message": "No favorites yet"}

    if not 0 <= index < len(favorites):
        return {"status": "error", "message": f"Invalid index. You have {len(favorites)} favorites (0-{len(favorites)-1})"}

    station = favorites[index]
    url = station.get("url_resolved") or station.get("url")
    name = station.get("name", "Unknown")

    result = play(url)
    if result.get("status") == "playing":
        return {
            "status": "playing",
            "index": index,
            "name": name,
            "url": url
        }
    return result


@mcp.tool()
def get_history(limit: int = 20) -> list:
    """Get listening history."""
    history = load_json(HISTORY_FILE)
    return history[-limit:][::-1]


@mcp.tool()
def recommend(mood: str = "relaxing") -> list:
    """
    Get mood-based recommendations.

    Args:
        mood: Mood keyword (relaxing, energetic, focus, sleep, morning, workout, romantic)

    Returns:
        Recommended stations
    """
    mood_tags = {
        "relaxing": ["lounge", "ambient", "classical", "jazz"],
        "energetic": ["dance", "electronic", "pop", "rock"],
        "focus": ["classical", "ambient", "instrumental"],
        "sleep": ["ambient", "classical"],
        "morning": ["pop", "jazz"],
        "workout": ["electronic", "dance", "rock"],
        "romantic": ["jazz", "classical"],
    }

    tags = mood_tags.get(mood.lower(), [mood])
    all_results = []
    seen = set()

    for tag in tags[:2]:
        # DB search
        db_results = db_search(tag, "tags", 15)
        for r in db_results:
            if r["url"] not in seen:
                seen.add(r["url"])
                r["source"] = "db"
                all_results.append(r)

        # API search
        encoded_tag = urllib.parse.quote(tag)
        api_results = api_get(f"stations/bytag/{encoded_tag}", {"limit": 15})
        for s in api_results:
            url = s.get("url_resolved") or s.get("url", "")
            if url and url not in seen:
                seen.add(url)
                station = format_station(s)
                station["source"] = "api"
                all_results.append(station)

    all_results.sort(key=lambda x: x.get("votes", 0), reverse=True)
    return all_results[:20]


@mcp.tool()
def get_db_stats() -> dict:
    """
    Get database statistics.

    Returns:
        DB stats (total stations, alive, dead, etc.)
    """
    db = get_db()
    if not db:
        return {
            "status": "lightweight_mode" if CONFIG.get("lightweight") else "no_db",
            "message": "Running in lightweight mode (API only)" if CONFIG.get("lightweight") else "No local database found",
            "mode": "lightweight" if CONFIG.get("lightweight") else "normal",
            "api_backend": "radiograph",
        }

    try:
        cursor = db.cursor()

        cursor.execute("SELECT COUNT(*) FROM stations")
        total = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM stations WHERE is_alive = 1")
        alive = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM stations WHERE is_alive = 0")
        dead = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(DISTINCT countrycode) FROM stations")
        countries = cursor.fetchone()[0]

        return {
            "total": total,
            "alive": alive,
            "dead": dead,
            "unknown": total - alive - dead,
            "countries": countries,
            "db_path": [p for p in DB_PATHS if os.path.exists(p)][0] if any(os.path.exists(p) for p in DB_PATHS) else None
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
def purge_dead() -> dict:
    """
    Delete all dead stations from database.

    Returns:
        Number of deleted stations
    """
    db = get_db()
    if not db:
        return {"status": "no_db"}

    try:
        cursor = db.cursor()
        cursor.execute("SELECT COUNT(*) FROM stations WHERE is_alive = 0")
        count = cursor.fetchone()[0]

        cursor.execute("DELETE FROM stations WHERE is_alive = 0")
        db.commit()

        return {"status": "success", "deleted": count}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
def health_check(limit: int = 100) -> dict:
    """
    Check health of stations by testing URLs.
    Updates is_alive status in database.

    Args:
        limit: Number of stations to check (default 100)

    Returns:
        Health check results
    """
    db = get_db()
    if not db:
        return {"status": "no_db"}

    try:
        cursor = db.cursor()
        # Prioritize old verified or unverified stations
        cursor.execute("""
            SELECT stationuuid, name, url, url_resolved
            FROM stations
            WHERE is_alive = 1 OR is_alive IS NULL
            ORDER BY last_checked_at ASC NULLS FIRST
            LIMIT ?
        """, (limit,))

        stations = cursor.fetchall()
        alive = 0
        dead = 0

        for s in stations:
            url = s[3] or s[2]  # url_resolved or url
            try:
                req = urllib.request.Request(url, method='HEAD',
                    headers={"User-Agent": "RadioMCP/1.0"})
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status < 400:
                        cursor.execute("""
                            UPDATE stations SET is_alive = 1, fail_count = 0,
                                last_checked_at = ? WHERE stationuuid = ?
                        """, (datetime.now().isoformat(), s[0]))
                        alive += 1
                    else:
                        raise Exception("Bad status")
            except:
                cursor.execute("""
                    UPDATE stations SET is_alive = 0,
                        fail_count = COALESCE(fail_count, 0) + 1,
                        last_checked_at = ? WHERE stationuuid = ?
                """, (datetime.now().isoformat(), s[0]))
                dead += 1

        db.commit()
        return {"checked": len(stations), "alive": alive, "dead": dead}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
def sync_with_api(country_code: str = None, tag: str = None, limit: int = 100) -> dict:
    """
    Sync database with RadioGraph API.
    Fetches new/updated stations and compares with local DB.

    Args:
        country_code: Filter by country (optional)
        tag: Filter by tag/genre (optional)
        limit: Max stations to fetch (default 100)

    Returns:
        Sync results (new, updated, unchanged)
    """
    db = get_db()
    if not db:
        return {"status": "no_db"}

    # Get from API
    if country_code:
        code = urllib.parse.quote(country_code.upper())
        api_results = api_get(f"stations/bycountrycode/{code}", {"limit": limit})
    elif tag:
        encoded_tag = urllib.parse.quote(tag)
        api_results = api_get(f"stations/bytag/{encoded_tag}", {"limit": limit})
    else:
        api_results = api_get("stations/toplisteners", {"limit": limit})

    if not api_results:
        return {"status": "error", "message": "API returned no results"}

    try:
        cursor = db.cursor()
        new_count = 0
        updated = 0
        skipped = 0

        for s in api_results:
            uuid = s.get("stationuuid", "")
            url = s.get("url_resolved") or s.get("url", "")

            # Validate
            if not is_valid_station(s):
                skipped += 1
                continue

            # Check if in DB
            cursor.execute("SELECT stationuuid, url_resolved FROM stations WHERE stationuuid = ?", (uuid,))
            existing = cursor.fetchone()

            if not existing:
                # Add new
                add_station_to_db(s)
                new_count += 1
            elif existing[1] != url:
                # URL changed - update
                cursor.execute("""
                    UPDATE stations SET url = ?, url_resolved = ?,
                        is_alive = 1, last_checked_at = ?
                    WHERE stationuuid = ?
                """, (s.get("url"), url, datetime.now().isoformat(), uuid))
                updated += 1

        db.commit()
        return {
            "status": "success",
            "fetched": len(api_results),
            "new": new_count,
            "updated": updated,
            "skipped": skipped
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
def set_sleep_timer(minutes: int) -> dict:
    """
    Set sleep timer to stop radio after specified minutes.

    Args:
        minutes: Minutes until auto-stop (0 to cancel)

    Returns:
        Timer status
    """
    global sleep_timer
    import threading

    # Cancel existing timer
    if sleep_timer:
        sleep_timer.cancel()
        sleep_timer = None

    if minutes <= 0:
        return {"status": "cancelled"}

    def auto_stop():
        global sleep_timer
        stop()
        sleep_timer = None
        print(f"Sleep timer: stopped after {minutes} minutes", flush=True)

    sleep_timer = threading.Timer(minutes * 60, auto_stop)
    sleep_timer.start()

    return {"status": "set", "minutes": minutes, "stop_at": (datetime.now() + __import__('datetime').timedelta(minutes=minutes)).strftime("%H:%M")}


@mcp.tool()
def set_alarm(hour: int, minute: int = 0, station_query: str = "pop") -> dict:
    """
    Set alarm to start playing radio at specified time.

    Args:
        hour: Hour (0-23)
        minute: Minute (0-59)
        station_query: What to play (default "pop")

    Returns:
        Alarm status
    """
    import threading
    from datetime import timedelta

    now = datetime.now()
    alarm_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # If time passed, set for tomorrow
    if alarm_time <= now:
        alarm_time += timedelta(days=1)

    delay = (alarm_time - now).total_seconds()

    def alarm_play():
        stations = search(station_query, 5)
        if stations:
            s = stations[0]
            play(s["url"], s["name"])
            print(f"Alarm: playing {s['name']}", flush=True)

    timer = threading.Timer(delay, alarm_play)
    timer.start()

    return {
        "status": "set",
        "alarm_time": alarm_time.strftime("%Y-%m-%d %H:%M"),
        "station_query": station_query,
        "delay_minutes": int(delay / 60)
    }


@mcp.tool()
def set_volume(level: int) -> dict:
    """
    Set playback volume.

    Args:
        level: Volume level (0-100)

    Returns:
        Volume status
    """
    level = max(0, min(100, level))

    try:
        mpv_ipc_send({"command": ["set_property", "volume", level]}, timeout=2)
        return {"status": "success", "volume": level}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
def get_volume() -> dict:
    """
    Get current volume level.

    Returns:
        Current volume
    """
    try:
        data = mpv_ipc_send({"command": ["get_property", "volume"]}, timeout=2)
        return {"volume": int(data.get("data", 100)) if data else 100}
    except:
        return {"volume": 100, "error": "Could not get volume"}


@mcp.tool()
def similar_stations(limit: int = 10) -> list:
    """
    Find similar stations based on currently playing station's tags.
    Uses SVD embeddings API for high-quality recommendations when available,
    falls back to tag-based search.

    Args:
        limit: Number of results

    Returns:
        List of similar stations
    """
    station = current_station or load_last_station()
    if not station:
        return []

    # Try SVD API first (uses collaborative filtering embeddings)
    station_id = station.get("stationuuid") or station.get("id")
    if station_id:
        result = _svd_api_get(f"/similar/station/{station_id}?top={limit}")
        if "similar" in result and result["similar"]:
            # SVD returns station IDs with scores, look up details from local DB
            db = get_db()
            if db:
                cursor = db.cursor()
                enriched = []
                for item in result["similar"][:limit]:
                    sid = item.get("station_id") or item.get("id")
                    if sid:
                        cursor.execute("SELECT * FROM stations WHERE stationuuid = ?", (sid,))
                        row = cursor.fetchone()
                        if row:
                            s = format_stations([row])[0]
                            s["svd_score"] = item.get("score", 0)
                            enriched.append(s)
                if enriched:
                    return enriched

    # Fallback: tag-based search
    db = get_db()
    if db:
        cursor = db.cursor()
        cursor.execute("SELECT tags FROM stations WHERE url = ? OR url_resolved = ?",
                      (station.get("url"), station.get("url")))
        row = cursor.fetchone()
        if row and row[0]:
            tags = row[0].split(",")
            if tags:
                main_tag = tags[0].strip()
                results = search(main_tag, limit + 1)
                return [r for r in results if r["url"] != station.get("url")][:limit]

    return []


@mcp.tool()
def similar_artists(artist: str, limit: int = 10) -> dict:
    """
    Find artists with similar radio programming patterns using SVD embeddings.
    Based on collaborative filtering: artists that appear together on similar stations.

    Args:
        artist: Artist name (e.g., "BTS", "Taylor Swift", "Miles Davis")
        limit: Number of similar artists to return

    Returns:
        Dict with artist name and list of similar artists with scores
    """
    result = _svd_api_get(f"/similar/artist/{urllib.parse.quote(artist)}?top={limit}")
    if "error" in result and "not found" in result.get("error", "").lower():
        # Try fuzzy search
        search_result = _svd_api_get(f"/search/artist?q={urllib.parse.quote(artist)}&limit=1")
        if search_result.get("artists"):
            best = search_result["artists"][0]
            result = _svd_api_get(f"/similar/artist/{urllib.parse.quote(best)}?top={limit}")
            if "similar" in result:
                result["note"] = f"Exact match not found. Showing results for \"{best}\""
    return result


@mcp.tool()
def search_artist(query: str, limit: int = 20) -> dict:
    """
    Search for artists in the SVD embeddings database.
    Useful to find exact artist names before using similar_artists().

    Args:
        query: Search query (partial name match)
        limit: Max results

    Returns:
        Dict with matching artist names
    """
    return _svd_api_get(f"/search/artist?q={urllib.parse.quote(query)}&limit={limit}")


@mcp.tool()
def recommend_by_weather(city: str = "") -> dict:
    """
    Recommend stations based on current weather.

    Args:
        city: City name (auto-detect from IP if empty)

    Returns:
        Weather-based recommendations
    """
    lat, lon = 37.5665, 126.978  # Seoul default
    
    # Get location from IP (ip-api.com: 45 req/min free)
    try:
        req = urllib.request.Request("http://ip-api.com/json/?fields=city,lat,lon", 
                                     headers={"User-Agent": "RadioMCP/1.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            loc_data = json.loads(resp.read().decode())
            if not city:
                city = loc_data.get("city", "Seoul")
            lat = loc_data.get("lat", lat)
            lon = loc_data.get("lon", lon)
    except:
        if not city:
            city = "Seoul"
    
    # Open-Meteo API (free, no API key, 10k req/day)
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
        req = urllib.request.Request(url, headers={"User-Agent": "RadioMCP/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())

        current = data.get("current_weather", {})
        weather_code = int(current.get("weathercode", 0))
        temp = current.get("temperature", 20)
        is_day = current.get("is_day", 1)

        # WMO Weather codes -> mood
        # 0: Clear, 1-3: Cloudy, 45-48: Fog
        # 51-67: Drizzle/Rain, 71-77: Snow, 80-82: Showers, 85-86: Snow showers
        # 95-99: Thunderstorm
        if weather_code in [51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82]:
            mood = "rainy"
        elif weather_code in [71, 73, 75, 77, 85, 86]:
            mood = "snowy"
        elif weather_code in [95, 96, 99]:
            mood = "stormy"
        elif weather_code == 0:
            mood = "sunny" if temp > 20 else ("cold" if temp < 10 else "clear")
        elif weather_code in [1, 2, 3]:
            mood = "cloudy"
        elif weather_code in [45, 48]:
            mood = "foggy"
        else:
            mood = "cloudy"

        # Temperature adjustment
        if temp > 28:
            mood = "hot"
        elif temp < 0:
            mood = "cold"

        # Night time adjustment
        if not is_day and mood in ["sunny", "clear"]:
            mood = "night"

        tags = WEATHER_TAGS.get(mood, ["pop", "jazz"])

        # Search
        all_results = []
        seen = set()
        for tag in tags[:2]:
            results = search(tag, 10)
            for r in results:
                if r["url"] not in seen:
                    seen.add(r["url"])
                    all_results.append(r)

        return {
            "city": city,
            "weather": mood,
            "temp_c": round(temp, 1),
            "stations": all_results[:10]
        }
    except Exception as e:
        # Fallback: time-based recommendation
        try:
            time_result = recommend_by_time()
            fallback_stations = time_result.get("stations", [])
        except:
            fallback_stations = []
        return {
            "city": city,
            "weather": "unknown",
            "temp_c": None,
            "error": str(e),
            "stations": fallback_stations
        }


@mcp.tool()
def get_user_profile() -> dict:
    """
    Analyze listening history to build user preference profile.

    Returns:
        User profile with top tags, time preferences, day preferences
    """
    history = load_json(HISTORY_FILE)
    if not history:
        return {"status": "no_history", "message": "Listen to some radio first"}

    # Tag weights (duration based)
    tag_weights = {}

    # Time of day preferences
    time_prefs = {
        "morning": {},    # 6-10
        "daytime": {},    # 10-17
        "evening": {},    # 17-21
        "night": {},      # 21-6
    }

    # Day of week preferences
    day_prefs = {i: {} for i in range(7)}  # 0=Monday

    total_duration = 0
    total_listens = len(history)

    for entry in history:
        tags_str = entry.get("tags", "")
        duration = entry.get("duration", 60)  # Default 1 min
        timestamp = entry.get("timestamp", "")

        total_duration += duration

        # Parse tags
        tags = [t.strip().lower() for t in tags_str.split(",") if t.strip()]
        if not tags:
            continue

        # Duration weight (in minutes, max 10)
        weight = min(duration / 60, 10)

        # Parse time
        try:
            dt = datetime.fromisoformat(timestamp)
            hour = dt.hour
            weekday = dt.weekday()

            # Determine time slot
            if 6 <= hour < 10:
                time_slot = "morning"
            elif 10 <= hour < 17:
                time_slot = "daytime"
            elif 17 <= hour < 21:
                time_slot = "evening"
            else:
                time_slot = "night"
        except:
            time_slot = "daytime"
            weekday = 0

        for tag in tags:
            # Total weight
            tag_weights[tag] = tag_weights.get(tag, 0) + weight

            # By time slot
            time_prefs[time_slot][tag] = time_prefs[time_slot].get(tag, 0) + weight

            # By day
            day_prefs[weekday][tag] = day_prefs[weekday].get(tag, 0) + weight

    # Sort
    top_tags = sorted(tag_weights.items(), key=lambda x: -x[1])[:10]

    # Top tags by time slot
    time_top = {}
    for slot, tags in time_prefs.items():
        if tags:
            time_top[slot] = sorted(tags.items(), key=lambda x: -x[1])[:5]

    # Top tags by day
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    day_top = {}
    for day, tags in day_prefs.items():
        if tags:
            day_top[day_names[day]] = sorted(tags.items(), key=lambda x: -x[1])[:3]

    return {
        "total_listens": total_listens,
        "total_minutes": round(total_duration / 60, 1),
        "top_tags": top_tags,
        "time_preferences": time_top,
        "day_preferences": day_top,
    }


@mcp.tool()
def personalized_recommend(limit: int = 10) -> dict:
    """
    Recommend stations based on user's listening patterns.
    Considers time of day, day of week, and overall preferences.

    Args:
        limit: Number of results

    Returns:
        Personalized recommendations
    """
    profile = get_user_profile()
    if profile.get("status") == "no_history":
        # If no history, recommend by time slot
        return recommend_by_time()

    # Current context
    now = datetime.now()
    hour = now.hour
    weekday = now.weekday()
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    # Time slot
    if 6 <= hour < 10:
        time_slot = "morning"
    elif 10 <= hour < 17:
        time_slot = "daytime"
    elif 17 <= hour < 21:
        time_slot = "evening"
    else:
        time_slot = "night"

    # Collect tags (priority: slot+day > slot > all)
    recommended_tags = []

    # 1. Preferred tags for this day
    day_prefs = profile.get("day_preferences", {}).get(day_names[weekday], [])
    for tag, _ in day_prefs[:2]:
        recommended_tags.append(tag)

    # 2. Preferred tags for this time slot
    time_prefs = profile.get("time_preferences", {}).get(time_slot, [])
    for tag, _ in time_prefs[:3]:
        if tag not in recommended_tags:
            recommended_tags.append(tag)

    # 3. Overall preferred tags
    for tag, _ in profile.get("top_tags", [])[:5]:
        if tag not in recommended_tags:
            recommended_tags.append(tag)

    # Search
    all_results = []
    seen = set()

    for tag in recommended_tags[:4]:
        results = search(tag, limit // 2)
        for r in results:
            if r["url"] not in seen:
                seen.add(r["url"])
                r["matched_tag"] = tag
                all_results.append(r)

    all_results.sort(key=lambda x: x.get("votes", 0), reverse=True)

    return {
        "context": {
            "time_slot": time_slot,
            "day": day_names[weekday],
            "recommended_tags": recommended_tags[:5],
        },
        "stations": all_results[:limit]
    }


@mcp.tool()
def recommend_by_time() -> list:
    """
    Recommend stations based on current time of day.

    Returns:
        Time-based recommendations
    """
    time_of_day = get_time_of_day()
    tags = TIME_TAGS.get(time_of_day, ["pop"])

    all_results = []
    seen = set()
    for tag in tags[:2]:
        results = search(tag, 10)
        for r in results:
            if r["url"] not in seen:
                seen.add(r["url"])
                all_results.append(r)

    return {
        "time_of_day": time_of_day,
        "hour": datetime.now().hour,
        "stations": all_results[:10]
    }


@mcp.tool()
def get_blocklist() -> dict:
    """
    Get current blocklist status.

    Returns:
        Blocklist patterns, URLs, UUIDs and source URL
    """
    return {
        "patterns": BLOCK_LIST,
        "blocked_urls": list(BLOCKED_URLS),
        "blocked_uuids": list(BLOCKED_UUIDS),
        "sources": BLOCKLIST_URLS
    }


@mcp.tool()
def refresh_blocklist() -> dict:
    """
    Refresh blocklist from GitHub and purge blocked stations from DB.

    Returns:
        Refresh status
    """
    old_count = len(BLOCK_LIST) + len(BLOCKED_URLS) + len(BLOCKED_UUIDS)
    fetch_remote_blocklist()
    new_count = len(BLOCK_LIST) + len(BLOCKED_URLS) + len(BLOCKED_UUIDS)
    purged = purge_blocked_from_db()
    return {
        "status": "refreshed",
        "patterns": len(BLOCK_LIST),
        "blocked_urls": len(BLOCKED_URLS),
        "blocked_uuids": len(BLOCKED_UUIDS),
        "new_entries": new_count - old_count,
        "stations_purged": purged
    }




@mcp.tool()
def add_to_blocklist(pattern: str = "", url: str = "", uuid: str = "", reason: str = "takedown request") -> dict:
    """
    Add a station to the blocklist by name pattern, URL, or UUID.
    Also removes matching stations from the local DB immediately.
    Updates the local blocklist.json file.

    Args:
        pattern: Station name pattern to block (e.g., "Radio XYZ")
        url: Specific stream URL to block
        uuid: Station UUID to block
        reason: Reason for blocking (e.g., "DMCA takedown", "owner request")

    Returns:
        Block result with number of stations removed from DB
    """
    if not pattern and not url and not uuid:
        return {"error": "Provide at least one of: pattern, url, uuid"}

    removed = 0
    conn = get_db()

    if pattern:
        if pattern not in BLOCK_LIST:
            BLOCK_LIST.append(pattern)
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM stations WHERE LOWER(name) LIKE ?", (f"%{pattern.lower()}%",))
                removed += cur.fetchone()[0]
                cur.execute("DELETE FROM stations WHERE LOWER(name) LIKE ?", (f"%{pattern.lower()}%",))
                conn.commit()
            except Exception:
                pass

    if url:
        BLOCKED_URLS.add(url)
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM stations WHERE url = ? OR url_resolved = ?", (url, url))
                removed += cur.fetchone()[0]
                cur.execute("DELETE FROM stations WHERE url = ? OR url_resolved = ?", (url, url))
                conn.commit()
            except Exception:
                pass

    if uuid:
        BLOCKED_UUIDS.add(uuid)
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM stations WHERE stationuuid = ?", (uuid,))
                removed += cur.fetchone()[0]
                cur.execute("DELETE FROM stations WHERE stationuuid = ?", (uuid,))
                conn.commit()
            except Exception:
                pass

    _save_blocklist(pattern=pattern, url=url, uuid=uuid, reason=reason)

    return {
        "status": "blocked",
        "pattern": pattern or None,
        "url": url or None,
        "uuid": uuid or None,
        "reason": reason,
        "stations_removed_from_db": removed
    }


@mcp.tool()
def remove_from_blocklist(pattern: str = "", url: str = "", uuid: str = "") -> dict:
    """
    Remove an entry from the blocklist.

    Args:
        pattern: Station name pattern to unblock
        url: Stream URL to unblock
        uuid: Station UUID to unblock

    Returns:
        Unblock result
    """
    if not pattern and not url and not uuid:
        return {"error": "Provide at least one of: pattern, url, uuid"}

    removed_items = []

    if pattern and pattern in BLOCK_LIST:
        BLOCK_LIST.remove(pattern)
        removed_items.append(f"pattern: {pattern}")

    if url and url in BLOCKED_URLS:
        BLOCKED_URLS.discard(url)
        removed_items.append(f"url: {url}")

    if uuid and uuid in BLOCKED_UUIDS:
        BLOCKED_UUIDS.discard(uuid)
        removed_items.append(f"uuid: {uuid}")

    _save_blocklist_full()

    return {
        "status": "unblocked" if removed_items else "not_found",
        "removed": removed_items
    }


def _save_blocklist(pattern="", url="", uuid="", reason=""):
    """Append new entry to local blocklist.json"""
    bl_path = None
    for p in LOCAL_BLOCKLIST_PATHS:
        if os.path.exists(p):
            bl_path = p
            break
    if not bl_path:
        bl_path = LOCAL_BLOCKLIST_PATHS[0]

    try:
        if os.path.exists(bl_path):
            with open(bl_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"version": "1.0.0", "updated": "", "blocked": [], "blocked_urls": [], "blocked_uuids": []}

        data["updated"] = datetime.now().strftime("%Y-%m-%d")

        if pattern:
            existing = [b["pattern"] for b in data.get("blocked", [])]
            if pattern not in existing:
                data["blocked"].append({
                    "pattern": pattern,
                    "reason": reason,
                    "added": datetime.now().strftime("%Y-%m-%d")
                })

        if url:
            if url not in data.get("blocked_urls", []):
                data.setdefault("blocked_urls", []).append(url)

        if uuid:
            if uuid not in data.get("blocked_uuids", []):
                data.setdefault("blocked_uuids", []).append(uuid)

        with open(bl_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    except Exception:
        pass


def _save_blocklist_full():
    """Rewrite blocklist.json from current runtime state"""
    bl_path = None
    for p in LOCAL_BLOCKLIST_PATHS:
        if os.path.exists(p):
            bl_path = p
            break
    if not bl_path:
        bl_path = LOCAL_BLOCKLIST_PATHS[0]

    try:
        if os.path.exists(bl_path):
            with open(bl_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"version": "1.0.0", "blocked": [], "blocked_urls": [], "blocked_uuids": []}

        data["updated"] = datetime.now().strftime("%Y-%m-%d")
        existing_patterns = {b["pattern"] for b in data.get("blocked", [])}
        runtime_patterns = set(BLOCK_LIST)
        data["blocked"] = [b for b in data.get("blocked", []) if b["pattern"] in runtime_patterns]
        data["blocked_urls"] = list(BLOCKED_URLS)
        data["blocked_uuids"] = list(BLOCKED_UUIDS)

        with open(bl_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    except Exception:
        pass


# ============================================================
# AI Helper Tools - Easy for AI to use
# ============================================================

@mcp.tool()
def get_radio_guide() -> dict:
    """
    IMPORTANT: Call this first when user asks about radio.
    Returns complete guide for AI to use radio tools effectively.

    Returns:
        Guide with available tools, search tips, examples
    """
    mode = "lightweight (API only)" if CONFIG.get("lightweight") else "normal (local DB + API)"
    has_db = get_db() is not None
    return {
        "overview": "Internet radio with 24,000+ stations from 197 countries",
        "mode": mode,
        "has_local_db": has_db,
        "api_backend": "radiograph",
        "quick_start": [
            "1. search('jazz') → find jazz stations",
            "2. play(url, name) → start playback",
            "3. now_playing() → see current song",
            "4. stop() → stop playback"
        ],
        "search_tools": {
            "search(query)": "General keyword search (genre, name, etc.)",
            "search_by_country(code)": "Country-specific (KR, US, JP, DE, FR...)",
            "advanced_search(...)": "Filters: country + tag + bitrate",
            "get_popular()": "Top stations by popularity",
            "recommend(mood)": "Mood-based: relaxing, energetic, focus, sleep"
        },
        "playback_tools": {
            "play(url, name)": "Start playing (auto-refreshes URL)",
            "stop()": "Stop playback",
            "resume()": "Resume last station",
            "now_playing()": "Current song info",
            "set_volume(0-100)": "Adjust volume"
        },
        "user_tools": {
            "add_favorite(station)": "Save to favorites",
            "get_favorites()": "List favorites",
            "get_history()": "Listening history"
        },
        "search_tips": {
            "genres": ["jazz", "rock", "classical", "electronic", "pop", "lounge", "ambient", "news", "talk"],
            "moods": ["relaxing", "energetic", "focus", "sleep", "romantic", "workout"],
            "quality": "Use advanced_search(min_bitrate=192) for HQ",
            "multilingual": "Korean(jazz), Japanese(ジャズ), Chinese(爵士) supported"
        }
    }


@mcp.tool()
def expand_search(query: str) -> dict:
    """
    Get related search terms to expand search results.
    Use when initial search returns few results.

    Args:
        query: Original search term

    Returns:
        Related terms to try
    """
    expansions = {
        # Genres
        "jazz": ["smooth jazz", "bebop", "swing", "bossa nova", "jazz fusion"],
        "rock": ["classic rock", "hard rock", "alternative", "indie rock"],
        "classical": ["orchestra", "symphony", "chamber", "opera", "baroque"],
        "electronic": ["edm", "techno", "house", "trance", "ambient"],
        "pop": ["top 40", "hits", "chart", "contemporary"],
        "lounge": ["chillout", "cafe", "easy listening", "smooth"],
        "ambient": ["chillout", "new age", "meditation", "sleep"],
        "news": ["talk", "information", "current affairs", "public radio"],

        # Moods
        "relaxing": ["lounge", "ambient", "chillout", "smooth jazz"],
        "energetic": ["dance", "electronic", "rock", "pop hits"],
        "focus": ["classical", "ambient", "instrumental", "lo-fi"],
        "sleep": ["ambient", "nature", "meditation", "classical"],

        # Languages
        "korean": ["kpop", "korea", "korea"],
        "japanese": ["jpop", "日本", "japan"],
        "chinese": ["cpop", "中国", "china"],
    }

    query_lower = query.lower()
    related = []

    # Direct match
    if query_lower in expansions:
        related = expansions[query_lower]
    else:
        # Partial match
        for key, terms in expansions.items():
            if key in query_lower or query_lower in key:
                related.extend(terms)

    return {
        "original": query,
        "related_terms": list(set(related))[:8],
        "tip": "Try searching with these related terms for more results"
    }


@mcp.tool()
def get_radio_status() -> dict:
    """
    Get current radio system status.
    Useful for AI to understand current state.

    Returns:
        Current playback status, station info, system state
    """
    db = get_db()

    status = {
        "playback": "stopped",
        "current_station": None,
        "current_song": None,
        "volume": 100,
        "favorites_count": 0,
        "history_count": 0,
        "db_stations": 0
    }

    # Playback status (check in-memory first, then saved state for CLI)
    station = current_station or load_last_station()
    if station:
        status["playback"] = "playing"
        status["current_station"] = station

        # Current song
        try:
            data = mpv_ipc_send({"command": ["get_property", "media-title"]}, timeout=1)
            if data and "data" in data and data["data"]:
                status["current_song"] = data["data"]
        except:
            pass

    # Favorites/history
    favs = load_json(FAVORITES_FILE)
    history = load_json(HISTORY_FILE)
    status["favorites_count"] = len(favs) if favs else 0
    status["history_count"] = len(history) if history else 0

    # DB status
    if db:
        try:
            count = db.execute("SELECT COUNT(*) FROM stations WHERE is_alive = 1").fetchone()[0]
            status["db_stations"] = count
        except:
            pass

    # Mode info
    status["mode"] = "lightweight" if CONFIG.get("lightweight") else "normal"
    status["api_backend"] = "radiograph"

    # Player backend info
    status["player"] = {
        "backend": PLAYER_BACKEND,
        "available": []
    }
    if shutil.which("mpv"):
        status["player"]["available"].append("mpv")
    if shutil.which("vlc") or shutil.which("cvlc"):
        status["player"]["available"].append("vlc")
    if shutil.which("ffplay"):
        status["player"]["available"].append("ffplay")
    status["player"]["available"].append("browser")

    # Warning if only browser available
    if len(status["player"]["available"]) == 1:
        status["player"]["warning"] = "No media player found. Install mpv for best experience: brew install mpv (macOS) / apt install mpv (Linux) / winget install mpv (Windows)"

    return status


@mcp.tool()
def check_stream(url: str) -> dict:
    """
    Check if a stream URL is alive before playing.
    Use this to avoid playing dead streams.

    Args:
        url: Stream URL to check

    Returns:
        Stream status (alive, dead, or error)
    """
    try:
        req = urllib.request.Request(url, method='HEAD')
        req.add_header('User-Agent', 'RadioMCP/1.0')
        req.add_header('Icy-MetaData', '1')

        with urllib.request.urlopen(req, timeout=5) as resp:
            content_type = resp.headers.get('Content-Type', '')
            icy_name = resp.headers.get('icy-name', '')

            # Check stream type
            is_stream = any(t in content_type.lower() for t in
                          ['audio/', 'application/ogg', 'mpegurl', 'x-scpls'])

            return {
                "status": "alive",
                "url": url,
                "content_type": content_type,
                "icy_name": icy_name,
                "is_stream": is_stream
            }
    except urllib.error.HTTPError as e:
        return {"status": "dead", "url": url, "error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"status": "dead", "url": url, "error": str(e.reason)}
    except Exception as e:
        return {"status": "error", "url": url, "error": str(e)}


@mcp.tool()
def check_stream_detailed(url: str) -> dict:
    """
    Check stream with detailed info via G3 validator (if enabled).
    Returns bitrate, audio format, stream name, server location.
    
    Requires: G3_VALIDATOR_ENABLED=true environment variable

    Args:
        url: Stream URL to check

    Returns:
        Detailed stream info
    """
    if not G3_VALIDATOR_ENABLED:
        # Fallback to basic check
        return check_stream(url)
    
    result = g3_validate_url(url)
    if result.get("valid"):
        return {
            "status": "alive",
            "url": url,
            "is_media_stream": result.get("is_media_stream", False),
            "bitrate": result.get("bitrate"),
            "audio_format": result.get("audio_format"),
            "stream_name": result.get("stream_name"),
            "server": result.get("server"),
            "server_location": result.get("server_location"),
            "response_time_ms": result.get("response_time_ms")
        }
    else:
        return {
            "status": "dead",
            "url": url,
            "error": result.get("error", "Validation failed")
        }


@mcp.tool()
def get_categories() -> dict:
    """
    Get major station categories for quick navigation.

    Returns:
        Categories with example search queries
    """
    return {
        "music": {
            "description": "Music stations",
            "genres": ["pop", "rock", "jazz", "classical", "electronic", "hip hop",
                      "country", "r&b", "metal", "indie", "ambient", "lounge"],
            "search_tip": "search('jazz') or recommend('relaxing')"
        },
        "news": {
            "description": "News & Talk radio",
            "types": ["news", "talk", "public radio", "npr"],
            "search_tip": "search('news') or search_by_country('US', 'news')"
        },
        "sports": {
            "description": "Sports radio",
            "types": ["sports", "football", "baseball"],
            "search_tip": "search('sports')"
        },
        "culture": {
            "description": "Culture & Entertainment",
            "types": ["culture", "entertainment", "comedy"],
            "search_tip": "search('culture')"
        },
        "regional": {
            "description": "By country/region",
            "examples": ["KR (Korea)", "US (USA)", "JP (Japan)", "DE (Germany)", "FR (France)"],
            "search_tip": "search_by_country('KR') or search_by_country('JP', 'jazz')"
        },
        "mood": {
            "description": "By mood/activity",
            "moods": ["relaxing", "energetic", "focus", "sleep", "workout", "romantic"],
            "search_tip": "recommend('relaxing') or recommend('focus')"
        }
    }


@mcp.tool()
def get_listening_stats(period: str = "week") -> dict:
    """
    Get listening statistics for a specific period.

    Args:
        period: Time period - "today", "week", "month", "all"

    Returns:
        Listening stats: total time, top stations, top genres, daily breakdown
    """
    from datetime import timedelta

    history = load_json(HISTORY_FILE)
    if not history:
        return {"status": "no_history", "message": "No listening history yet"}

    now = datetime.now()

    # Period filter
    if period == "today":
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        cutoff = now - timedelta(days=7)
    elif period == "month":
        cutoff = now - timedelta(days=30)
    else:  # all
        cutoff = datetime.min

    # Filter
    filtered = []
    for entry in history:
        try:
            ts = datetime.fromisoformat(entry.get("timestamp", ""))
            if ts >= cutoff:
                filtered.append(entry)
        except:
            if period == "all":
                filtered.append(entry)

    if not filtered:
        return {"status": "no_data", "period": period, "message": f"No listening data for {period}"}

    # Calculate stats
    total_duration = sum(e.get("duration", 60) for e in filtered)

    # Listens per station
    station_times = {}
    for e in filtered:
        name = e.get("name", "Unknown")
        station_times[name] = station_times.get(name, 0) + e.get("duration", 60)

    top_stations = sorted(station_times.items(), key=lambda x: -x[1])[:5]

    # Listens per genre
    tag_times = {}
    for e in filtered:
        tags = e.get("tags", "").split(",")
        duration = e.get("duration", 60)
        for tag in tags:
            tag = tag.strip().lower()
            if tag:
                tag_times[tag] = tag_times.get(tag, 0) + duration

    top_tags = sorted(tag_times.items(), key=lambda x: -x[1])[:5]

    # Daily listening time (last 7 days)
    daily = {}
    for e in filtered:
        try:
            ts = datetime.fromisoformat(e.get("timestamp", ""))
            day = ts.strftime("%Y-%m-%d")
            daily[day] = daily.get(day, 0) + e.get("duration", 60)
        except:
            pass

    # Last 7 days only
    recent_days = sorted(daily.items(), reverse=True)[:7]

    return {
        "period": period,
        "total_listens": len(filtered),
        "total_minutes": round(total_duration / 60, 1),
        "total_hours": round(total_duration / 3600, 1),
        "top_stations": [{"name": n, "minutes": round(m/60, 1)} for n, m in top_stations],
        "top_genres": [{"tag": t, "minutes": round(m/60, 1)} for t, m in top_tags],
        "daily_minutes": [{"date": d, "minutes": round(m/60, 1)} for d, m in recent_days],
        "average_per_day": round(total_duration / 60 / max(len(daily), 1), 1)
    }


# ============================================================
# Station health check
# ============================================================
@mcp.tool()
def check_station(url: str) -> dict:
    """
    Check if a radio station URL is alive.

    Args:
        url: Stream URL to check

    Returns:
        Station status (alive, dead, or error)
    """
    try:
        req = urllib.request.Request(url, method='HEAD', headers={
            'User-Agent': 'RadioMCP/1.0'
        })
        response = urllib.request.urlopen(req, timeout=10)
        content_type = response.headers.get('Content-Type', '')

        return {
            "status": "alive",
            "url": url,
            "content_type": content_type,
            "is_audio": "audio" in content_type.lower() or "mpegurl" in content_type.lower()
        }
    except urllib.error.HTTPError as e:
        return {"status": "dead", "url": url, "error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"status": "dead", "url": url, "error": str(e.reason)}
    except Exception as e:
        return {"status": "error", "url": url, "error": str(e)}


# ============================================================
# Station sharing
# ============================================================
@mcp.tool()
def share_station(name: str = "") -> dict:
    """
    Get shareable info for current or specified station.

    Args:
        name: Station name (optional, uses current if empty)

    Returns:
        Shareable station info
    """
    station = None

    if name:
        # Search from DB
        db = get_db()
        if db:
            try:
                cursor = db.cursor()
                cursor.execute(
                    "SELECT name, url, country, tags, homepage FROM stations WHERE name LIKE ? LIMIT 1",
                    (f"%{name}%",)
                )
                row = cursor.fetchone()
                if row:
                    station = {
                        "name": row[0],
                        "url": row[1],
                        "country": row[2],
                        "tags": row[3],
                        "homepage": row[4] or ""
                    }
            except:
                pass
    else:
        # Currently playing station
        station = current_station

    if not station:
        return {"status": "error", "message": "No station found"}

    return {
        "status": "ok",
        "share": {
            "name": station.get("name", ""),
            "url": station.get("url", ""),
            "country": station.get("country", ""),
            "tags": station.get("tags", ""),
            "homepage": station.get("homepage", ""),
            "text": f"🎵 {station.get('name', '')} - {station.get('tags', '')}"
        }
    }


# ============================================================
# DJ Broadcast Tools (multi-provider + 24h scheduler)
# ============================================================
from radiomcp import dj_broadcast as _dj


@mcp.tool()
def dj_providers() -> dict:
    """List available music providers (spotify, apple_music, youtube).

    Returns which providers are installed/usable on this machine.
    YouTube is always available as fallback.
    """
    return {"available": _dj.available_providers()}


@mcp.tool()
def dj_play_set(artist: str = "", songs: list | None = None,
                provider: str = "", with_comments: bool = True,
                slot_name: str = "") -> dict:
    """Play a DJ set: songs with edge-tts DJ commentary between tracks.

    Args:
        artist: Artist name to auto-build a song list (optional)
        songs: Explicit list of "artist - title" queries (optional)
        provider: Preferred provider (spotify/apple_music/youtube). Empty = auto
        with_comments: Insert edge-tts DJ comments between songs
        slot_name: Optional label for this set
    """
    queue = list(songs) if songs else []
    if artist and not queue:
        queue = [f"{artist}" for _ in range(8)]
    if not queue:
        return {"status": "error", "message": "Provide artist or songs"}
    prov = provider or None
    return _dj.start_dj_set(queue, provider=prov,
                            slot_name=(slot_name or None),
                            with_comments=with_comments)


@mcp.tool()
def dj_stop() -> dict:
    """Stop the current DJ set / all DJ playback."""
    _dj.stop_dj_set()
    return {"status": "stopped"}


@mcp.tool()
def dj_play_video(source: str, fullscreen: bool = False,
                  ontop: bool = True) -> dict:
    """Play a VIDEO in a window on the Mac's screen (with sound).

    Unlike audio playback, this opens a real mpv window on the logged-in
    desktop. Works for music videos, clips, or live streams (e.g. 24/7 news).

    Args:
        source: YouTube watch URL, a channel /live URL, or a search query
                (e.g. "https://www.youtube.com/@ytnnews24/live",
                 "a-ha take on me", or a full watch URL).
        fullscreen: Open fullscreen instead of a window.
        ontop: Keep the window above other windows.
    """
    return _dj.play_video(source, fullscreen=fullscreen, ontop=ontop)


@mcp.tool()
def dj_stop_video() -> dict:
    """Stop the windowed video player."""
    return _dj.stop_video()


@mcp.tool()
def dj_song_info(query: str, lang: str = "ko") -> dict:
    """Search for song/artist information. Returns easy-to-use data.

    Use this to get background info before generating DJ comments.
    Results are pre-formatted so even small local models can use them.

    Args:
        query: Song or artist name (e.g. "우타고코로 리에", "아즈마 아키")
        lang: Language for results (ko, en, ja)

    Returns:
        Dict with:
        - artist: 가수명
        - song: 곡명
        - hint: 바로 사용 가능한 한 줄 설명 (예: "한일가왕전 출연 가수입니다")
        - facts: 짧은 팩트 리스트
        - related_shows: 관련 프로그램 (한일가왕전, 복면가왕 등)
        - youtube_titles: 관련 YouTube 영상 제목들

    Example usage for small models:
        info = dj_song_info("우타고코로 리에")
        # info["hint"] = "우타고코로 리에는 한일가왕전에 출연한 가수입니다."
        # 이 hint를 DJ 멘트에 바로 사용 가능
    """
    return _dj.search_song_info_for_local(query, lang=lang)


@mcp.tool()
def dj_play_with_info(songs: list, lang: str = "ko", provider: str = "youtube") -> dict:
    """Play DJ set with auto-generated rich comments from song info.

    This tool:
    1. Searches info for each song/artist
    2. Auto-generates informative DJ comments
    3. Starts playback with those comments

    Args:
        songs: List of song queries (e.g. ["우타고코로 리에 어릿광대의 소네트", ...])
        lang: Language (ko, en, ja)
        provider: Music provider (youtube, apple_music, spotify)

    Returns:
        Playback status with generated comments
    """
    comments = []
    total = len(songs)

    for i, song in enumerate(songs):
        # 곡 정보 검색
        info = _dj.search_song_info(song, lang=lang)

        # 풍부한 멘트 생성
        comment = _dj.generate_rich_comment(song, info, i, total, lang)
        comments.append(comment)

    # DJ 방송 시작
    result = _dj.start_dj_set(
        songs=songs,
        provider=provider,
        with_comments=True,
        comments=comments,
        lang=lang
    )
    result["generated_comments"] = comments
    return result


@mcp.tool()
def dj_one_hour(genre: str = "k-pop",
                songs: list = None,
                comments: list = None,
                provider: str = "apple_music",
                lang: str = "ko") -> dict:
    """Start a 1-hour DJ broadcast.

    AI should:
    1. Ask user what genre/mood they want
    2. Select 12-15 songs for 1 hour
    3. Generate DJ comments for each song
    4. Call this tool with songs and comments

    Args:
        genre: Genre to search if songs not provided (e.g. "k-pop", "jazz")
        songs: AI-selected list of songs (12-15 songs recommended)
               Example: ["아이유 - 좋은날", "뉴진스 - Hype Boy", ...]
        comments: AI-generated DJ comments for each song
                  Example: ["안녕하세요, 첫 곡은...", "다음 곡은...", ...]
        provider: "apple_music", "youtube", or "spotify"
        lang: Language code for TTS (ko, en, ja, zh, es, fr)
    """
    return _dj.start_one_hour_broadcast(
        genre=genre,
        provider=provider,
        songs=songs,
        comments=comments,
        lang=lang
    )


@mcp.tool()
def dj_get_schedule() -> dict:
    """Get the current 24-hour broadcast schedule."""
    return {"schedule": _dj.load_schedule()}


@mcp.tool()
def dj_set_schedule(schedule: list) -> dict:
    """Set the 24-hour broadcast schedule. AI creates the schedule.

    AI should:
    1. Ask user about their daily routine and music preferences
    2. Design time slots based on user's lifestyle
    3. Select songs and write DJ comments for each slot
    4. Call this tool with the full schedule

    Args:
        schedule: List of slots, each with:
            - start: "HH:MM" (required)
            - name: Show name (required)
            - songs: List of songs (or use query to search)
            - comments: DJ comments for each song
            - query: Search query if songs not provided
            - count: Number of songs to search (default 10)
            - provider: "apple_music", "youtube", "spotify"
            - lang: Language code (ko, en, ja, etc.)

    Example slot with AI-generated content:
        {
            "start": "07:00",
            "name": "모닝 카페",
            "songs": ["아이유 - 좋은날", "볼빨간사춘기 - 여행", ...],
            "comments": ["좋은 아침이에요...", "다음 곡은...", ...],
            "provider": "apple_music",
            "lang": "ko"
        }
    """
    return _dj.create_schedule_from_ai(schedule)


@mcp.tool()
def dj_start_scheduler() -> dict:
    """Start the 24-hour automatic broadcast scheduler.

    Plays the right music for the current time slot and switches
    automatically as time passes. Uses the schedule set by dj_set_schedule.
    """
    return _dj.start_scheduler()


@mcp.tool()
def dj_stop_scheduler() -> dict:
    """Stop the 24-hour automatic broadcast scheduler."""
    return _dj.stop_scheduler()


@mcp.tool()
def dj_scheduler_status() -> dict:
    """Get status of the 24-hour scheduler (running, current slot)."""
    return _dj.schedule_status()


@mcp.tool()
def dj_stats() -> dict:
    """Get broadcast statistics for monitoring.

    Returns:
        Dict with:
        - songs_played: Total songs played
        - comments_spoken: Total DJ comments
        - errors: Error count
        - last_updated: Last update time
    """
    return _dj.get_broadcast_stats()


@mcp.tool()
def dj_logs(lines: int = 30) -> dict:
    """Get recent broadcast logs for debugging.

    Args:
        lines: Number of log lines to retrieve (default 30)

    Returns:
        Dict with recent log entries
    """
    logs = _dj.get_broadcast_logs(lines)
    return {"logs": logs, "count": len(logs)}


@mcp.tool()
def dj_rss_list() -> dict:
    """List all available RSS feeds and current subscriptions.

    Returns:
        - presets: Available news/Reddit/Telegram presets
        - subscriptions: Current user subscriptions
    """
    return _dj.list_available_feeds()


@mcp.tool()
def dj_rss_subscribe(source: str = "", url: str = "", name: str = "",
                     category: str = "custom") -> dict:
    """Subscribe to an RSS feed.

    Args:
        source: Preset source name (e.g. "BBC", "r/kpop", "연합뉴스")
        url: Custom RSS URL (for blogs, Telegram, etc.)
        name: Display name for custom feeds
        category: Category (news, reddit, telegram, blog, podcast)

    Examples:
        - dj_rss_subscribe(source="BBC")
        - dj_rss_subscribe(url="https://blog.example.com/rss", name="Tech Blog", category="blog")
        - dj_rss_subscribe(url="https://t.me/s/channelname", name="@channelname", category="telegram")
    """
    if source:
        subs = _dj.get_news_subscriptions()
        if source not in subs.get("sources", []):
            subs["sources"] = subs.get("sources", []) + [source]
            _dj.set_news_subscriptions(sources=subs["sources"])
        return {"status": "subscribed", "source": source}

    if url:
        return _dj.add_custom_feed(name or url, url, category)

    return {"error": "Provide source or url"}


@mcp.tool()
def dj_rss_unsubscribe(source: str = "", url: str = "") -> dict:
    """Unsubscribe from an RSS feed.

    Args:
        source: Preset source name to remove
        url: Custom feed URL to remove
    """
    if source:
        subs = _dj.get_news_subscriptions()
        if source in subs.get("sources", []):
            subs["sources"].remove(source)
            _dj.set_news_subscriptions(sources=subs["sources"])
        return {"status": "unsubscribed", "source": source}

    if url:
        return _dj.remove_custom_feed(url)

    return {"error": "Provide source or url"}


@mcp.tool()
def dj_rss_telegram(channel: str, name: str = "") -> dict:
    """Subscribe to a Telegram channel via RSS.

    Args:
        channel: Channel username (e.g. "duaborams" or "@channelname")
        name: Display name (optional)
    """
    return _dj.add_telegram_channel(channel, name)


@mcp.tool()
def dj_fetch_content(category: str = "all", limit: int = 5) -> dict:
    """Fetch content from all subscribed feeds.

    Use this to get material for DJ comments.
    AI can use these headlines/posts to create interesting talk.

    Args:
        category: Filter by category (all, news, reddit, telegram, blog)
        limit: Max items per feed

    Returns:
        Dict with content organized by source, ready for DJ comment generation
    """
    return _dj.get_feed_content(category, limit)


@mcp.tool()
def dj_news_lang(lang: str = "") -> dict:
    """Get or set the news output language.

    News from any source will be translated to this language.
    Uses local LLM (ollama) for translation.

    Args:
        lang: Language code to set (ko, en, ja, zh, es, fr, de, etc.)
              If empty, returns current setting.

    Examples:
        dj_news_lang()           → Get current language
        dj_news_lang(lang="ko")  → Korean output (BBC → 한국어로)
        dj_news_lang(lang="ja")  → Japanese output
    """
    if lang:
        return _dj.set_news_output_lang(lang)
    return {"lang": _dj.get_news_output_lang()}


@mcp.tool()
def dj_news_brief(lang: str = "") -> dict:
    """Get a pre-formatted news brief for DJ to read.

    Returns a ready-to-speak news summary from subscribed sources.
    News will be translated to the preferred language if needed.

    Args:
        lang: Override output language (optional, uses setting if empty)
    """
    output_lang = lang or _dj.get_news_output_lang()
    brief = _dj.make_news_brief(max_items=3, lang=output_lang)
    return {"brief": brief, "lang": output_lang, "ready_to_speak": bool(brief)}


@mcp.tool()
def dj_top_headlines(max_items: int = 5, fresh: bool = False) -> dict:
    """Get top headlines prioritized by source importance.

    Returns headlines from major wire services first (Reuters, AP, AFP),
    then major broadcasters (BBC, CNN), then regional sources.

    AI can use these to create news segments.

    Args:
        max_items: Maximum headlines to return
        fresh: Force fetch fresh news (ignore cache)

    Returns:
        List of headlines with source and priority level
    """
    if fresh:
        _dj.fetch_fresh_news()
    return {"headlines": _dj.get_top_headlines(max_items)}


@mcp.tool()
def dj_fresh_news(sources: list = None, lang: str = "") -> dict:
    """Fetch fresh news right now (no cache).

    Always gets the latest news from RSS feeds.
    Use this for live broadcasts that need real-time news.

    Args:
        sources: Specific sources (default: Reuters, BBC, 연합뉴스)
        lang: Output language for translation

    Returns:
        Fresh news headlines, optionally translated
    """
    news = _dj.fetch_fresh_news(sources=sources, limit=5)

    output_lang = lang or _dj.get_news_output_lang()
    result = {
        "fetched_at": _dj.datetime.now().isoformat(),
        "lang": output_lang,
        "news": {}
    }

    for source, headlines in news.items():
        translated = []
        for h in headlines[:3]:
            if output_lang != "en" and any(ord(c) < 128 for c in h[:10]):
                h = _dj.translate_text(h, output_lang)
            translated.append(h)
        result["news"][source] = translated

    return result


@mcp.tool()
def dj_reddit_talk(subreddits: list = None) -> dict:
    """Get interesting Reddit content for DJ talk.

    Args:
        subreddits: List of subreddits (default: Showerthoughts, todayilearned)

    Returns:
        Ready-to-speak DJ talk material from Reddit
    """
    talk = _dj.make_reddit_talk(subreddits)
    return {"talk": talk, "ready_to_speak": bool(talk)}


@mcp.tool()
def dj_get_content_for_comments(songs: list = None) -> dict:
    """Get all available content for AI to create DJ comments.

    AI should use this raw material to freely compose DJ comments.
    No fixed templates - AI decides style, tone, and what to include.

    Args:
        songs: List of songs being played (for song info lookup)

    Returns:
        Dict with all available content:
        - news: Latest headlines from subscribed sources
        - reddit: Interesting posts for casual talk
        - song_info: Info about requested songs
        - time_context: Current time/date for greetings
        - settings: Language preferences

    AI workflow:
        1. Call dj_get_content_for_comments(songs=["가수 - 곡명", ...])
        2. AI reads the content and freely writes DJ comments
        3. AI calls dj_play_set(songs=[...], comments=[AI가 작성한 멘트들])
    """
    from datetime import datetime

    result = {
        "time_context": {
            "datetime": datetime.now().isoformat(),
            "hour": datetime.now().hour,
            "greeting_hint": "morning" if datetime.now().hour < 12 else
                           "afternoon" if datetime.now().hour < 18 else "evening"
        },
        "settings": {
            "lang": _dj.get_news_output_lang(),
            "dj_lang": _dj.get_dj_lang(),
        },
        "news": {},
        "reddit": [],
        "song_info": [],
    }

    # 뉴스 가져오기
    try:
        result["news"] = _dj.get_feed_content(category="all", limit=3)
    except Exception:
        pass

    # 레딧 가져오기
    try:
        for sub in ["Showerthoughts", "todayilearned"]:
            posts = _dj.fetch_reddit_posts(sub, limit=2)
            result["reddit"].extend(posts)
    except Exception:
        pass

    # 곡 정보 가져오기
    if songs:
        for song in songs[:5]:  # 최대 5곡
            try:
                info = _dj.search_song_info_for_local(song)
                result["song_info"].append(info)
            except Exception:
                pass

    return result


@mcp.tool()
def dj_health() -> dict:
    """Health check for broadcast system.

    Returns system status for monitoring:
    - mpv_running: Is mpv process active
    - scheduler_running: Is 24h scheduler active
    - current_state: Current broadcast state
    - last_error: Most recent error (if any)
    """
    import subprocess

    # Check mpv
    try:
        r = subprocess.run(["pgrep", "-f", "mpv"], capture_output=True, timeout=2)
        mpv_running = r.returncode == 0
    except Exception:
        mpv_running = False

    # Check app-based players (Apple Music / Spotify) — these don't use mpv.
    try:
        music_playing = _dj._music_player_state() == "playing"
    except Exception:
        music_playing = False

    # Get state
    state = _dj._read_state()
    sched_status = _dj.schedule_status()

    # Get last error from logs
    logs = _dj.get_broadcast_logs(10)
    last_error = None
    for line in reversed(logs):
        if "[ERROR]" in line:
            last_error = line.strip()
            break

    audio_active = mpv_running or music_playing

    return {
        "healthy": audio_active or state.get("status") == "idle",
        "mpv_running": mpv_running,
        "music_playing": music_playing,
        "audio_active": audio_active,
        "scheduler_running": sched_status.get("scheduler_running", False),
        "current_state": state,
        "last_error": last_error,
    }


def cpu_watchdog():
    """Monitor CPU usage and auto-terminate if spinning"""
    import sys
    import resource
    last_cpu = resource.getrusage(resource.RUSAGE_SELF).ru_utime
    high_cpu_count = 0
    while True:
        time.sleep(60)  # Check every minute
        current_cpu = resource.getrusage(resource.RUSAGE_SELF).ru_utime
        cpu_delta = current_cpu - last_cpu
        if cpu_delta > 30:  # More than 30s CPU in 1 minute = spinning
            high_cpu_count += 1
            sys.stderr.write(f"[WATCHDOG] High CPU: {cpu_delta:.1f}s ({high_cpu_count}/3)\n")
            if high_cpu_count >= 3:  # 3 consecutive = terminate
                sys.stderr.write("[WATCHDOG] CPU spinning, terminating\n")
                os._exit(1)  # Force exit
        else:
            high_cpu_count = 0  # Reset counter
        last_cpu = current_cpu


def _init_background():
    """Start background threads (watchdog, sync, blocklist)"""
    watchdog = threading.Thread(target=cpu_watchdog, daemon=True)
    watchdog.start()
    sync_thread = threading.Thread(target=sync_popular_stations, daemon=True)
    sync_thread.start()
    # Auto-sync blocklist on startup — removes takedown'd stations from local DB
    blocklist_thread = threading.Thread(target=_auto_blocklist_sync, daemon=True)
    blocklist_thread.start()


def main_mcp(transport="stdio", port=8000):
    """Run as MCP server (stdio or HTTP)
    
    Args:
        transport: "stdio" (default) or "sse"/"streamable-http"
        port: Port for HTTP transport (default 8000)
    """
    import sys
    if not _HAS_MCP:
        sys.stderr.write("[radiomcp] ERROR: MCP package not installed.\n")
        sys.stderr.write("Install with: pip install 'mcp[cli]>=1.0.0'\n")
        sys.stderr.write("CLI commands still work: radiomcp search jazz\n")
        sys.exit(1)
    
    _init_background()
    
    if transport == "stdio":
        sys.stderr.write(f"[radiomcp] Starting MCP stdio server PID={os.getpid()}\n")
        sys.stderr.flush()
        try:
            mcp.run()
        except Exception as e:
            sys.stderr.write(f"[radiomcp] Fatal error: {e}\n")
            raise
        finally:
            sys.stderr.write(f"[radiomcp] Shutting down PID={os.getpid()}\n")
    
    elif transport in ("sse", "streamable-http"):
        sys.stderr.write(f"[radiomcp] Starting MCP HTTP/SSE server on port {port} PID={os.getpid()}\n")
        sys.stderr.flush()
        try:
            mcp.run(transport="sse", port=port)
        except Exception as e:
            sys.stderr.write(f"[radiomcp] Fatal error: {e}\n")
            raise
        finally:
            sys.stderr.write(f"[radiomcp] Shutting down PID={os.getpid()}\n")
    
    else:
        sys.stderr.write(f"[radiomcp] ERROR: Unknown transport '{transport}'. Use 'stdio', 'sse', or 'streamable-http'\n")
        sys.exit(1)


def _get_openapi_spec(host="localhost:8100"):
    """OpenAPI 3.1 spec for ChatGPT Custom GPTs / Gemini Extensions / Swagger UI"""
    base_url = f"http://{host}" if "://" not in host else host
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "RadioMCP API",
            "description": "Internet radio search, playback, and recommendations. 24,000+ stations from 197 countries.",
            "version": "1.0.0"
        },
        "servers": [{"url": base_url}],
        "paths": {
            "/search": {
                "get": {
                    "operationId": "searchStations",
                    "summary": "Search radio stations by keyword",
                    "parameters": [
                        {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}, "description": "Search query (e.g., jazz, bbc, korean pop)"},
                        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}, "description": "Max results"}
                    ],
                    "responses": {"200": {"description": "List of stations", "content": {"application/json": {"schema": {"type": "array", "items": {"$ref": "#/components/schemas/Station"}}}}}}
                }
            },
            "/search/country": {
                "get": {
                    "operationId": "searchByCountry",
                    "summary": "Search stations by country code",
                    "parameters": [
                        {"name": "code", "in": "query", "required": True, "schema": {"type": "string"}, "description": "ISO country code (KR, US, JP, DE...)"},
                        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}}
                    ],
                    "responses": {"200": {"description": "Stations in country"}}
                }
            },
            "/search/advanced": {
                "get": {
                    "operationId": "advancedSearch",
                    "summary": "Advanced search with filters",
                    "parameters": [
                        {"name": "q", "in": "query", "schema": {"type": "string"}, "description": "Keywords"},
                        {"name": "country", "in": "query", "schema": {"type": "string"}, "description": "Country code"},
                        {"name": "language", "in": "query", "schema": {"type": "string"}, "description": "Language (english, korean...)"},
                        {"name": "tags", "in": "query", "schema": {"type": "string"}, "description": "Comma-separated tags"},
                        {"name": "min_bitrate", "in": "query", "schema": {"type": "integer"}, "description": "Minimum bitrate (128, 192, 256, 320)"},
                        {"name": "codec", "in": "query", "schema": {"type": "string"}, "description": "Audio codec (MP3, AAC, OGG)"},
                        {"name": "sort_by", "in": "query", "schema": {"type": "string", "enum": ["votes", "bitrate", "name"]}, "description": "Sort order"},
                        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}}
                    ],
                    "responses": {"200": {"description": "Filtered stations"}}
                }
            },
            "/popular": {
                "get": {
                    "operationId": "getPopular",
                    "summary": "Get popular stations",
                    "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}}],
                    "responses": {"200": {"description": "Popular stations"}}
                }
            },
            "/play": {
                "get": {
                    "operationId": "playStation",
                    "summary": "Play a radio station",
                    "parameters": [
                        {"name": "url", "in": "query", "required": True, "schema": {"type": "string"}, "description": "Stream URL"},
                        {"name": "name", "in": "query", "schema": {"type": "string"}, "description": "Station name"}
                    ],
                    "responses": {"200": {"description": "Playback status"}}
                }
            },
            "/stop": {
                "get": {
                    "operationId": "stopPlayback",
                    "summary": "Stop radio playback",
                    "responses": {"200": {"description": "Stop result"}}
                }
            },
            "/now-playing": {
                "get": {
                    "operationId": "nowPlaying",
                    "summary": "Get currently playing song info",
                    "responses": {"200": {"description": "Current song/station info"}}
                }
            },
            "/status": {
                "get": {
                    "operationId": "getStatus",
                    "summary": "Get radio system status",
                    "responses": {"200": {"description": "System status"}}
                }
            },
            "/favorites": {
                "get": {
                    "operationId": "getFavorites",
                    "summary": "Get favorite stations",
                    "responses": {"200": {"description": "Favorites list"}}
                }
            },
            "/history": {
                "get": {
                    "operationId": "getHistory",
                    "summary": "Get listening history",
                    "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}}],
                    "responses": {"200": {"description": "Listening history"}}
                }
            },
            "/recommend": {
                "get": {
                    "operationId": "getRecommendations",
                    "summary": "Get mood-based recommendations",
                    "parameters": [{"name": "mood", "in": "query", "schema": {"type": "string", "enum": ["relaxing", "energetic", "focus", "sleep", "morning", "workout", "romantic"]}, "description": "Mood keyword"}],
                    "responses": {"200": {"description": "Recommended stations"}}
                }
            },
            "/volume": {
                "get": {
                    "operationId": "volume",
                    "summary": "Get or set volume",
                    "parameters": [{"name": "level", "in": "query", "schema": {"type": "integer", "minimum": 0, "maximum": 100}, "description": "Volume level (omit to get current)"}],
                    "responses": {"200": {"description": "Volume info"}}
                }
            },
            "/recognize": {
                "get": {
                    "operationId": "recognizeSong",
                    "summary": "Recognize currently playing song (Shazam-like)",
                    "parameters": [{"name": "duration", "in": "query", "schema": {"type": "integer", "default": 12}, "description": "Recording seconds"}],
                    "responses": {"200": {"description": "Recognition result"}}
                }
            },
            "/categories": {
                "get": {
                    "operationId": "getCategories",
                    "summary": "Get station categories",
                    "responses": {"200": {"description": "Available categories"}}
                }
            },
            "/health": {
                "get": {
                    "operationId": "healthCheck",
                    "summary": "API health check",
                    "responses": {"200": {"description": "Server status"}}
                }
            }
        },
        "components": {
            "schemas": {
                "Station": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "url": {"type": "string"},
                        "url_resolved": {"type": "string"},
                        "country": {"type": "string"},
                        "countrycode": {"type": "string"},
                        "tags": {"type": "string"},
                        "bitrate": {"type": "integer"},
                        "codec": {"type": "string"},
                        "language": {"type": "string"},
                        "votes": {"type": "integer"},
                        "favicon": {"type": "string"}
                    }
                }
            }
        }
    }


def main_serve(host="0.0.0.0", port=8100):
    """Run as HTTP API server (for Codex, GPT, web apps)"""
    try:
        from http.server import HTTPServer, BaseHTTPRequestHandler
        from urllib.parse import urlparse, parse_qs
    except ImportError:
        print("HTTP server modules not available")
        return

    _init_background()

    class RadioHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            params = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed.query).items()}

            try:
                result = self._route(path, params)
                self._respond(200, result)
            except Exception as e:
                self._respond(500, {"error": str(e)})

        def _route(self, path, params):
            if path == "/search":
                return search(params.get("q", ""), int(params.get("limit", "20")))
            elif path == "/search/country":
                return search_by_country(params.get("code", "US"), int(params.get("limit", "20")))
            elif path == "/search/language":
                return search_by_language(params.get("lang", "english"), int(params.get("limit", "20")))
            elif path == "/search/advanced":
                return advanced_search(
                    query=params.get("q"),
                    country=params.get("country"),
                    language=params.get("language"),
                    tags=params.get("tags"),
                    min_bitrate=int(params.get("min_bitrate", "0")),
                    codec=params.get("codec"),
                    sort_by=params.get("sort_by", "votes"),
                    limit=int(params.get("limit", "20"))
                )
            elif path == "/popular":
                return get_popular(int(params.get("limit", "20")))
            elif path == "/play":
                return play(params.get("url", ""), params.get("name", ""))
            elif path == "/stop":
                return stop()
            elif path == "/now-playing":
                return now_playing()
            elif path == "/status":
                return get_radio_status()
            elif path == "/favorites":
                return get_favorites()
            elif path == "/history":
                return get_history(int(params.get("limit", "20")))
            elif path == "/recommend":
                return recommend(params.get("mood", "relaxing"))
            elif path == "/recommend/weather":
                return recommend_by_weather(params.get("city", ""))
            elif path == "/recommend/time":
                return recommend_by_time()
            elif path == "/volume":
                level = params.get("level")
                if level:
                    return set_volume(int(level))
                return get_volume()
            elif path == "/recognize":
                return recognize_song(int(params.get("duration", "12")))
            elif path == "/similar":
                return similar_stations(int(params.get("limit", "10")))
            elif path == "/blocklist":
                return get_blocklist()
            elif path == "/categories":
                return get_categories()
            elif path == "/stats":
                return get_db_stats()
            elif path == "/guide":
                return get_radio_guide()
            elif path == "/health":
                return {"status": "ok", "mode": "http", "pid": os.getpid()}
            elif path == "/openapi.json":
                return _get_openapi_spec(self.headers.get("Host", "localhost:8100"))
            elif path == "/" or path == "":
                return {
                    "name": "RadioMCP HTTP API",
                    "version": "1.0.0",
                    "docs": "/openapi.json",
                    "endpoints": [
                        "GET /search?q=jazz", "GET /search/country?code=KR",
                        "GET /search/advanced?q=lounge&min_bitrate=128",
                        "GET /popular", "GET /play?url=...&name=...",
                        "GET /stop", "GET /now-playing", "GET /status",
                        "GET /favorites", "GET /history", "GET /recommend?mood=relaxing",
                        "GET /volume?level=80", "GET /recognize",
                        "GET /similar", "GET /blocklist", "GET /categories",
                        "GET /stats", "GET /health", "GET /openapi.json"
                    ]
                }
            else:
                return {"error": f"Unknown endpoint: {path}"}

        def _respond(self, code, data):
            import json as _json
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(_json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))

        def log_message(self, format, *args):
            pass  # Suppress default logging

    server = HTTPServer((host, port), RadioHandler)
    print(f"[radiomcp] HTTP API server on http://{host}:{port}")
    print(f"[radiomcp] Try: curl http://localhost:{port}/search?q=jazz")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[radiomcp] Shutting down HTTP server")
        server.shutdown()


def _handle_config(args):
    """Handle config subcommands"""
    import json as _json
    if not args:
        return _json.dumps(CONFIG, indent=2, ensure_ascii=False)

    subcmd = args[0]
    if subcmd == "show":
        return _json.dumps(CONFIG, indent=2, ensure_ascii=False)
    elif subcmd == "set" and len(args) >= 3:
        key, value = args[1], args[2]
        if key in CONFIG:
            # Type coerce
            if isinstance(CONFIG[key], bool):
                CONFIG[key] = value.lower() in ("true", "1", "yes")
            elif isinstance(CONFIG[key], int):
                CONFIG[key] = int(value)
            else:
                CONFIG[key] = value
            # Update API_BASE if URL changed
            global API_BASE
            if CONFIG.get("radiograph_url"):
                API_BASE = CONFIG["radiograph_url"]
            save_config(CONFIG)
            return _json.dumps({"status": "saved", key: CONFIG[key]}, indent=2)
        return _json.dumps({"error": f"Unknown key: {key}"})
    elif subcmd == "path":
        return CONFIG_FILE
    elif subcmd == "reset":
        import os as _os
        if _os.path.exists(CONFIG_FILE):
            _os.remove(CONFIG_FILE)
        return _json.dumps({"status": "reset to defaults"})
    else:
        return """Config commands:
  radiomcp config              Show current config
  radiomcp config set KEY VAL  Set a config value
  radiomcp config path         Show config file path
  radiomcp config reset        Reset to defaults

Keys:
  radiograph_url    RadioGraph API URL
  radiograph_api_key  API key (optional)
  lightweight       true = API only, no local DB
  serve_port        HTTP server port (default: 8100)

Examples:
  radiomcp config set radiograph_url https://api.airtune.ai
  radiomcp config set lightweight true"""


def _handle_update():
    """Update local DB from RadioGraph API"""
    import time as _time

    user_db = os.path.join(DATA_DIR, "radio_stations.db")
    pkg_db = os.path.join(PACKAGE_DIR, "radio_stations.db")
    dev_db = os.path.expanduser("~/RadioCli/radio_stations.db")

    # Find best existing DB to use as base
    db_path = user_db
    import shutil as _shutil
    best_src = None
    best_size = 0
    for src in [pkg_db, dev_db]:
        if os.path.exists(src) and os.path.getsize(src) > best_size:
            best_src = src
            best_size = os.path.getsize(src)
    user_size = os.path.getsize(user_db) if os.path.exists(user_db) else 0
    if best_src and best_size > user_size * 2:
        _shutil.copy2(best_src, user_db)
        print(f"  [i] Using bundled DB ({best_size//1024//1024}MB) as base for update")

    print(f"  Updating stations from RadioGraph API...")
    print(f"  API: {RADIOGRAPH_BASE}")
    t0 = _time.time()

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Ensure table exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stations (
                stationuuid TEXT PRIMARY KEY,
                name TEXT, url TEXT, url_resolved TEXT,
                country TEXT, countrycode TEXT, tags TEXT,
                bitrate INTEGER DEFAULT 0, codec TEXT DEFAULT '',
                language TEXT DEFAULT '', votes INTEGER DEFAULT 0,
                clickcount INTEGER DEFAULT 0, favicon TEXT DEFAULT '',
                is_alive INTEGER DEFAULT 1, fail_count INTEGER DEFAULT 0
            )
        """)

        # Fetch from RadioGraph API — top countries
        countries = ["KR", "US", "JP", "GB", "DE", "FR", "BR", "CA", "AU", "IN"]
        total_new = 0
        total_updated = 0

        for cc in countries:
            try:
                stations = api_get(f"stations/bycountrycode/{cc}", {"limit": 200})
                for s in stations:
                    uuid = s.get("stationuuid", s.get("id", ""))
                    if not uuid:
                        continue
                    name = s.get("name", "")
                    url = s.get("url", "")
                    url_resolved = s.get("url_resolved", url)

                    existing = cursor.execute(
                        "SELECT url_resolved FROM stations WHERE stationuuid = ?", (uuid,)
                    ).fetchone()

                    if existing:
                        if existing[0] != url_resolved:
                            cursor.execute(
                                "UPDATE stations SET url_resolved=?, is_alive=1 WHERE stationuuid=?",
                                (url_resolved, uuid)
                            )
                            total_updated += 1
                    else:
                        cursor.execute("""
                            INSERT OR IGNORE INTO stations
                            (stationuuid, name, url, url_resolved, country, countrycode,
                             tags, bitrate, codec, language, votes, clickcount, favicon, is_alive)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                        """, (
                            uuid, name, url, url_resolved,
                            s.get("country", ""), s.get("countrycode", cc),
                            s.get("tags", ""), s.get("bitrate", 0),
                            s.get("codec", ""), s.get("language", ""),
                            s.get("votes", 0), s.get("clickcount", 0),
                            s.get("favicon", "")
                        ))
                        total_new += 1
            except Exception:
                pass

        # Also fetch popular stations globally
        try:
            popular = api_get("stations/toplisteners", {"limit": 200})
            for s in popular:
                uuid = s.get("stationuuid", s.get("id", ""))
                if not uuid:
                    continue
                existing = cursor.execute(
                    "SELECT 1 FROM stations WHERE stationuuid = ?", (uuid,)
                ).fetchone()
                if not existing:
                    cursor.execute("""
                        INSERT OR IGNORE INTO stations
                        (stationuuid, name, url, url_resolved, country, countrycode,
                         tags, bitrate, codec, language, votes, clickcount, favicon, is_alive)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                    """, (
                        uuid, s.get("name", ""), s.get("url", ""),
                        s.get("url_resolved", s.get("url", "")),
                        s.get("country", ""), s.get("countrycode", ""),
                        s.get("tags", ""), s.get("bitrate", 0),
                        s.get("codec", ""), s.get("language", ""),
                        s.get("votes", 0), s.get("clickcount", 0),
                        s.get("favicon", "")
                    ))
                    total_new += 1
        except Exception:
            pass

        conn.commit()
        total = cursor.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
        conn.close()

        elapsed = _time.time() - t0
        print(f"\n  ✓ Update complete ({elapsed:.1f}s)")
        print(f"    New: {total_new}, Updated: {total_updated}")
        print(f"    Total stations: {total:,}")
        print(f"    DB: {db_path}")

    except Exception as e:
        print(f"\n  ✗ Update failed: {e}")


def _handle_setup(args):
    """Setup radiomcp for different AI platforms"""
    import json as _json

    target = args[0] if args else "auto"

    # Find radiomcp binary path
    radiomcp_bin = shutil.which("radiomcp")
    if not radiomcp_bin:
        # Try venv path
        venv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".venv_radiomcp", "bin", "radiomcp")
        if os.path.exists(venv_path):
            radiomcp_bin = venv_path

    python_bin = sys.executable

    results = []

    if target in ("auto", "claude"):
        # Claude Desktop MCP setup
        claude_config_paths = [
            os.path.expanduser("~/.claude/settings.json"),                          # Claude Code global
            os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json"),  # Claude Desktop macOS
            os.path.expanduser("~/.config/Claude/claude_desktop_config.json"),      # Claude Desktop Linux
        ]

        for config_path in claude_config_paths:
            config_dir = os.path.dirname(config_path)
            if not os.path.exists(config_dir):
                continue

            try:
                existing = {}
                if os.path.exists(config_path):
                    with open(config_path, "r") as f:
                        existing = json.load(f)

                if "mcpServers" not in existing:
                    existing["mcpServers"] = {}

                # Use python -m radiomcp for reliability
                existing["mcpServers"]["radio"] = {
                    "command": python_bin,
                    "args": ["-m", "radiomcp"],
                }

                with open(config_path, "w") as f:
                    json.dump(existing, f, indent=2, ensure_ascii=False)

                results.append(f"[ok] Claude config: {config_path}")
            except Exception as e:
                results.append(f"[!] Claude config failed ({config_path}): {e}")

        if not any("[ok]" in r and "Claude" in r for r in results):
            results.append("[i] Claude Desktop not found. Manual setup:")
            results.append(f'    Add to claude_desktop_config.json:')
            results.append(f'    {{"mcpServers": {{"radio": {{"command": "{python_bin}", "args": ["-m", "radiomcp"]}}}}}}')

    if target in ("auto", "codex", "http"):
        # Codex / GPT / HTTP API setup
        port = CONFIG.get("serve_port", 8100)
        results.append("")
        results.append(f"[i] For Codex/GPT/other AI tools:")
        results.append(f"    Start HTTP API server:")
        results.append(f"      radiomcp serve --port {port}")
        results.append(f"    Then use: http://localhost:{port}/search?q=jazz")
        results.append("")

        # Create launchd plist for macOS auto-start
        import platform
        if platform.system() == "Darwin":
            plist_path = os.path.expanduser("~/Library/LaunchAgents/com.radiomcp.serve.plist")
            bin_path = radiomcp_bin or f"{python_bin} -m radiomcp"
            results.append(f"    Auto-start on login (macOS):")
            results.append(f"      radiomcp setup service")

            if len(args) > 1 and args[1] == "service":
                plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.radiomcp.serve</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_bin}</string>
        <string>-m</string>
        <string>radiomcp</string>
        <string>serve</string>
        <string>--port</string>
        <string>{port}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{os.path.expanduser("~/.radiocli/serve.log")}</string>
    <key>StandardErrorPath</key>
    <string>{os.path.expanduser("~/.radiocli/serve.err")}</string>
</dict>
</plist>"""
                os.makedirs(os.path.dirname(plist_path), exist_ok=True)
                with open(plist_path, "w") as f:
                    f.write(plist_content)
                results.append(f"    [ok] Created: {plist_path}")
                results.append(f"    Run: launchctl load {plist_path}")

        elif platform.system() == "Linux":
            results.append(f"    Auto-start (Linux systemd):")
            results.append(f"      radiomcp setup service")

            if len(args) > 1 and args[1] == "service":
                service_path = os.path.expanduser("~/.config/systemd/user/radiomcp.service")
                service_content = f"""[Unit]
Description=RadioMCP HTTP API Server
After=network.target

[Service]
ExecStart={python_bin} -m radiomcp serve --port {port}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target\n"""
                os.makedirs(os.path.dirname(service_path), exist_ok=True)
                with open(service_path, "w") as f:
                    f.write(service_content)
                results.append(f"    [ok] Created: {service_path}")
                results.append(f"    Run: systemctl --user enable --now radiomcp")

    if target in ("auto",):
        # Check mpv
        if shutil.which("mpv"):
            results.append("[ok] mpv found")
        else:
            results.append("[!] mpv not found - needed for playback")
            import platform
            if platform.system() == "Darwin":
                results.append("    Install: brew install mpv")
            else:
                results.append("    Install: sudo apt install mpv")

        # DB status
        db = get_db()
        if db:
            results.append("[ok] Local database ready")
        else:
            results.append("[i] No local DB - using API mode (lightweight)")

    if not results:
        results.append("""Usage:
  radiomcp setup              Auto-detect and configure everything
  radiomcp setup claude       Setup for Claude Desktop/Code
  radiomcp setup codex        Setup for Codex/GPT (HTTP API)
  radiomcp setup service      Install as background service""")

    return "\n".join(results)


def _check_first_run():
    """Silently initialize data directory on first run. Auto-registers MCP on first launch."""
    os.makedirs(DATA_DIR, exist_ok=True)
    first_run_flag = os.path.join(DATA_DIR, ".mcp_registered")
    if not os.path.exists(first_run_flag):
        try:
            _handle_setup(["auto"])
        except Exception:
            pass
        try:
            with open(first_run_flag, "w") as f:
                f.write("1")
        except Exception:
            pass


def _run_doctor():
    """Diagnose system setup and show install guidance"""
    import platform
    results = []
    results.append("RadioMCP System Diagnostics")
    results.append("=" * 40)

    # Player status
    results.append("\n[Player Backends]")
    players_found = 0

    system = platform.system()

    if shutil.which("mpv"):
        results.append("  mpv: INSTALLED (recommended)")
        players_found += 1
    else:
        results.append("  mpv: NOT FOUND")
        if system == "Darwin":
            results.append("       Install: brew install mpv")
        elif system == "Windows":
            results.append("       Install: winget install mpv OR choco install mpv")
        else:
            results.append("       Install: sudo apt install mpv")

    if shutil.which("vlc") or shutil.which("cvlc"):
        results.append("  vlc: INSTALLED")
        players_found += 1
    else:
        results.append("  vlc: NOT FOUND")
        if system == "Darwin":
            results.append("       Install: brew install vlc")
        elif system == "Windows":
            results.append("       Install: winget install VideoLAN.VLC OR https://videolan.org")
        else:
            results.append("       Install: sudo apt install vlc")

    if shutil.which("ffplay"):
        results.append("  ffplay: INSTALLED")
        players_found += 1
    else:
        results.append("  ffplay: NOT FOUND")
        if system == "Darwin":
            results.append("       Install: brew install ffmpeg")
        elif system == "Windows":
            results.append("       Install: winget install ffmpeg OR choco install ffmpeg")
        else:
            results.append("       Install: sudo apt install ffmpeg")

    results.append("  browser: ALWAYS AVAILABLE (fallback)")

    if players_found == 0:
        results.append("\n  WARNING: No media player installed!")
        results.append("  Playback will open streams in browser.")
        results.append("  For best experience, install mpv.")
    else:
        results.append(f"\n  Current backend: {PLAYER_BACKEND}")

    # Database status
    results.append("\n[Database]")
    db = get_db()
    if db:
        try:
            count = db.execute("SELECT COUNT(*) FROM stations WHERE is_alive = 1").fetchone()[0]
            if count == 0:
                results.append("  Stations: No stations yet")
                results.append("  Run: radiomcp update")
            else:
                results.append(f"  Stations: {count:,}")
        except Exception:
            results.append("  Stations: No stations yet — Run: radiomcp update")
    else:
        results.append("  Database: NOT FOUND")
        results.append("  Run: radiomcp update")

    # Data directory
    results.append("\n[Data Directory]")
    results.append(f"  Path: {DATA_DIR}")
    if os.path.exists(DATA_DIR):
        results.append("  Status: EXISTS")
    else:
        results.append("  Status: WILL BE CREATED ON FIRST USE")

    # Optional tools
    results.append("\n[Optional Tools]")
    if shutil.which("ffmpeg"):
        results.append("  ffmpeg: INSTALLED (for song recognition)")
    else:
        results.append("  ffmpeg: NOT FOUND (needed for song recognition)")

    results.append("\n" + "=" * 40)
    if players_found > 0:
        results.append("System OK - Ready to play radio!")
    else:
        results.append("Install a media player for the best experience.")

    return "\n".join(results)


def main_cli(args):
    """Run as CLI tool"""
    import json as _json

    cmd = args[0] if args else "help"
    rest = args[1:]

    # Ensure data directory exists
    _check_first_run()

    commands = {
        "setup": lambda: _handle_setup(rest),
        "install": lambda: _handle_setup(["claude"]),
        "config": lambda: _handle_config(rest),
        "search": lambda: _json.dumps(search(" ".join(rest) if rest else "jazz", 10), indent=2, ensure_ascii=False),
        "play": lambda: _json.dumps(play(rest[0], rest[1] if len(rest) > 1 else ""), indent=2, ensure_ascii=False) if rest else '{"error": "Usage: radiomcp play <url> [name]"}',
        "stop": lambda: _json.dumps(stop(), indent=2, ensure_ascii=False),
        "now": lambda: _json.dumps(now_playing(), indent=2, ensure_ascii=False),
        "status": lambda: _json.dumps(get_radio_status(), indent=2, ensure_ascii=False),
        "favorites": lambda: _json.dumps(get_favorites(), indent=2, ensure_ascii=False),
        "history": lambda: _json.dumps(get_history(int(rest[0]) if rest else 10), indent=2, ensure_ascii=False),
        "popular": lambda: _json.dumps(get_popular(int(rest[0]) if rest else 10), indent=2, ensure_ascii=False),
        "recommend": lambda: _json.dumps(recommend(rest[0] if rest else "relaxing"), indent=2, ensure_ascii=False),
        "recognize": lambda: _json.dumps(recognize_song(), indent=2, ensure_ascii=False),
        "similar": lambda: _json.dumps(similar_stations(), indent=2, ensure_ascii=False),
        "volume": lambda: _json.dumps(set_volume(int(rest[0])) if rest else get_volume(), indent=2, ensure_ascii=False),
        "country": lambda: _json.dumps(search_by_country(rest[0] if rest else "US", 10), indent=2, ensure_ascii=False),
        "blocklist": lambda: _json.dumps(get_blocklist(), indent=2, ensure_ascii=False),
        "resume": lambda: _json.dumps(resume(), indent=2, ensure_ascii=False),
        "stats": lambda: _json.dumps(get_db_stats(), indent=2, ensure_ascii=False),
        "doctor": lambda: _run_doctor(),
    }

    if cmd == "help" or cmd == "--help" or cmd == "-h":
        print("""radiomcp - Internet Radio for AI and Humans

MODES:
  radiomcp              MCP server (for Claude)
  radiomcp serve        HTTP API server (for Codex/GPT/web)
  radiomcp <command>    CLI mode

SETUP:
  setup                 Auto-detect and configure for Claude/Codex
  setup claude          Setup for Claude Desktop/Code (MCP)
  setup codex           Setup for Codex/GPT (HTTP API)
  setup service         Install as background service (macOS/Linux)

COMMANDS:
  search <query>        Search stations (e.g., radiomcp search jazz)
  play <url> [name]     Play a station
  stop                  Stop playback
  resume                Resume last station
  now                   Show current song
  status                Player status
  favorites             List favorites
  history [n]           Listening history
  popular [n]           Popular stations
  recommend [mood]      Get recommendations (relaxing/energetic/focus/sleep)
  recognize             Identify current song
  similar               Find similar stations
  volume [0-100]        Get/set volume
  country <code>        Search by country (KR/US/JP...)
  blocklist             Show blocklist
  stats                 Database statistics
  doctor                System diagnostics and install guidance
  config [show|set|reset]  Manage configuration
  update                Update station database from RadioGraph API
  serve [--port N]      Start HTTP API server

EXAMPLES:
  radiomcp search "smooth jazz"
  radiomcp play "https://stream.example.com/jazz" "Jazz FM"
  radiomcp now
  radiomcp recommend focus
  radiomcp serve --port 8100
  radiomcp update
  radiomcp config set radiograph_url https://api.airtune.ai\n""")
        return

    if cmd == "update":
        _handle_update()
        return

    if cmd == "serve":
        port = 8100
        for i, a in enumerate(rest):
            if a in ("--port", "-p") and i + 1 < len(rest):
                port = int(rest[i + 1])
        main_serve(port=port)
        return

    if cmd in commands:
        try:
            result = commands[cmd]()
            print(result)
        except Exception as e:
            print(_json.dumps({"error": str(e)}, indent=2))
    else:
        print(f"Unknown command: {cmd}")
        print("Run 'radiomcp help' for usage")


def main():
    """Entry point - detects mode from arguments"""
    import sys

    if len(sys.argv) > 1:
        # Parse for --transport and --port flags
        transport = "stdio"
        port = 8000
        args = []
        
        i = 0
        while i < len(sys.argv[1:]):
            arg = sys.argv[i + 1]
            if arg == "--transport":
                if i + 2 < len(sys.argv):
                    transport = sys.argv[i + 2]
                    i += 2
                else:
                    sys.stderr.write("ERROR: --transport requires a value\n")
                    sys.exit(1)
            elif arg == "--port":
                if i + 2 < len(sys.argv):
                    try:
                        port = int(sys.argv[i + 2])
                    except ValueError:
                        sys.stderr.write(f"ERROR: --port must be an integer, got '{sys.argv[i + 2]}'\n")
                        sys.exit(1)
                    i += 2
                else:
                    sys.stderr.write("ERROR: --port requires a value\n")
                    sys.exit(1)
            else:
                args.append(arg)
                i += 1
            i += 1
        
        # Check if we're running as MCP with transport/port flags (no other args)
        if not args:
            # MCP mode with explicit transport
            main_mcp(transport=transport, port=port)
        else:
            # CLI mode
            main_cli(args)
    else:
        # Default: MCP stdio server
        main_mcp()

if __name__ == "__main__":
    main()
