# RadioCli / radiomcp - Changelog

## v1.2.0 (2026-07-02)

### Apple Music Native Library Playback
- **Native playback via persistent ID**: `resolve_library_track` / `play_library_track_by_id` - no GUI search/click, plays tracks directly by persistent ID for accurate, instant playback
- **play_apple_music**: Native library playback first, GUI search as fallback; verifies actual playback via player state

### DJ Worker Improvements
- **Library track pre-queuing**: Resolves all Apple Music tracks before playback starts for instant switching
- **Real playback verification**: Checks actual player state before reporting success; fixes false-success bug where non-YouTube playback was always marked successful
- **YouTube fallback**: Automatically falls back to YouTube if track not in library or playback fails

### Cross-Instance Playback Lock (Singleton)
- **Shared stop token** (`~/.radiocli/playback.stop`): Global coordination file ensures only one instance plays at a time
- **Watcher thread** (`_watch_global_stop`): Monitors for stop signals from any instance; stops playback and prevents respawn conflicts

### Windowed Video Playback
- **dj_play_video / dj_stop_video**: Play videos with visible window on Mac desktop
- **GUI session launch**: Uses `launchctl asuser` to spawn mpv in Aqua session (fixes background-only audio issue)
- **Auto-refresh for live streams**: Supervisor thread re-resolves expired stream URLs for continuous live playback

### Health Check Enhancements
- **dj_health**: Added `music_playing` and `audio_active` fields to properly detect Apple Music playback as healthy state

---

## v1.0.0 (2026-03-02)

### MCP Server (radiomcp)

#### Core Features
- **24,671stations** DB includes (197items across countries)
- **search**: keyword, country, genre, mood based
- **playback**: mpv based, URL auto update (token expiration handling)
- **song recognition**: ICY metadata + Whisper
- **AI recommendation**: time of day, weather, listening patterns based

#### AI Helper Tools
- `get_radio_guide()` - AI  
- `get_categories()` -  rock (/news/sports)
- `get_listening_stats(period)` -   
- `check_stream(url)` - stream  
- `similar_stations()` -  broadcast recommendation
- `expand_search(query)` - search 

#### search items
- **country detect**: "korea news" → KR + news 
- **  search**: "news" + "news"   search
- **country  **: API  country  

#### rock
- `blocklist.json`  
- GitHub/Cloudflare   
- KBS/MBC/SBS rock (token based URL expiration)

#### auto 
- MCP   Radio Browser popular broadcast 
- URL changed  auto update

### CLI (`radio` command)

- rock `blocklist.json` 
- KBS/MBC/SBS rock 

---

## 

### Claude Desktop (`claude_desktop_config.json`)
```json
{
  "mcpServers": {
    "radio": {
      "command": "python3",
      "args": ["-m", "radiomcp"]
    }
  }
}
```

### PyPI  (rain)
```bash
pip install radiomcp
```

---

##  

```
RadioCli/
├── radiomcp/
│   ├── __init__.py
│   ├── server.py          # MCP  (103KB)
│   ├── blocklist.json     # rock
│   └── radio_stations.db  # broadcast DB (12MB)
├── radiomcp/tui.py       # CLI (radio command)
├── blocklist.json         # rock ()
├── pyproject.toml         # PyPI 
├── LICENSE                # MIT
├── DISCLAIMER.md          # disclaimer
└── README.md
```

---

## rock

###    (v1.0.1)
|  |  |
|------|------|
| Pyongyang, pyongyang, north korea, dprk, Korean Central | blocked content |
| KBS, MBC, SBS | token based URL expiration |

### rock  
GitHub Issues: https://github.com/zeus-kim/radiomcp/issues

---

##   (2026-03-02)

### MCP
|  |  |
|------|------|
| DB  | 24,671items broadcast, 197items across countries |
| jazz search | ✅ 101 Smooth Jazz  |
| korea search | ✅ CBS, Gugak FM, OBS  |
| rock | ✅ 8items  |
| KBS/MBC/SBS rock | ✅ |
| YTN/CBS  | ✅ |

### CLI
|  |  |
|------|------|
| rock  | ✅ 8items  |
| jazz search | ✅ |
| korea search | ✅ |
| KBS/MBC rock | ✅ |
| CLI  | ✅ |

---

## distribution rain

###  
- `dist/radiomcp-1.0.0-py3-none-any.whl` (3.8MB)
- `dist/radiomcp-1.0.0.tar.gz` (3.8MB)

### package 
|  |  |
|------|------|
| radio_stations.db | 11.5MB |
| server.py | 103KB |
| blocklist.json | 1KB |

### PyPI  
```bash
pip install twine
twine upload dist/*
```

---

##  

- [ ] PyPI  /
- [ ] PyPI 
- [ ] GitHub   (zeus-kim/radiomcp)
- [ ] Cloudflare Pages  (blocklist )
- [ ] MCP Registry rock
