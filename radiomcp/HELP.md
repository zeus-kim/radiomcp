# Radio MCP Server - Help

Internet radio search and playback MCP server for Claude Desktop.

## Available Tools

### Search

| Tool | Description | Example |
|------|-------------|---------|
| `search` | Search by genre, name, keyword | "jazz", "BBC", "lounge" |
| `search_by_country` | Search by country code | "KR", "US", "JP", "DE" |
| `search_by_language` | Search by language | "korean", "japanese", "german" |
| `advanced_search` | Combined filters (country + tag + bitrate) | country="KR", tag="jazz", min_bitrate=128 |
| `get_popular` | Get top stations by clicks | - |
| `get_categories` | Get available genres and countries | - |
| `expand_search` | Get related search terms | "jazz" → smooth jazz, bebop |

### Playback

| Tool | Description |
|------|-------------|
| `play` | Play station (auto-fetches fresh URL) |
| `stop` | Stop playback |
| `resume` | Resume last station |
| `now_playing` | Get current song info |
| `set_volume` | Set volume (0-100) |
| `get_volume` | Get current volume |
| `get_player_backend` | Get current player (mpv/vlc/ffplay/browser) |
| `set_player_backend` | Change player backend |
| `get_radio_status` | Get full playback status |
| `check_stream` | Check if stream URL is alive |

### Recommendations

| Tool | Description |
|------|-------------|
| `recommend` | Mood-based (relaxing, energetic, focus) |
| `recommend_by_weather` | Weather-based (city name) |
| `recommend_by_time` | Time of day based |
| `personalized_recommend` | Based on listening history |
| `similar_stations` | Find similar to current |

### Timer & Alarm

| Tool | Description |
|------|-------------|
| `set_sleep_timer` | Auto-stop after N minutes |
| `set_alarm` | Wake-up alarm with radio |

### Song Recognition

| Tool | Description |
|------|-------------|
| `recognize_song` | Shazam-like recognition |
| `get_recognized_songs` | Recognition history |

### Favorites

| Tool | Description |
|------|-------------|
| `get_favorites` | List all favorites |
| `add_favorite` | Add station to favorites |
| `remove_favorite` | Remove by index (0-based) |
| `play_favorite` | Play from favorites by index |

### History & Profile

| Tool | Description |
|------|-------------|
| `get_history` | Listening history |
| `get_user_profile` | Analyze listening patterns |
| `get_listening_stats` | Statistics (week/month/all) |

### Station Info

| Tool | Description |
|------|-------------|
| `check_station` | Check station health |
| `share_station` | Get share info for current station |
| `get_radio_guide` | Get usage guide |

### Database

| Tool | Description |
|------|-------------|
| `get_db_stats` | DB statistics |
| `purge_dead` | Delete dead stations |
| `health_check` | Verify station URLs |
| `sync_with_api` | Sync with Radio Browser API |

### Blocklist

| Tool | Description |
|------|-------------|
| `get_blocklist` | View blocked stations |
| `refresh_blocklist` | Update blocklist from GitHub |

## Usage Examples

### Basic Playback

```
"Play some jazz"
→ search("jazz") → play(url, name)

"Stop the radio"
→ stop()

"What's playing now?"
→ now_playing()

"Resume where I left off"
→ resume()
```

### Search

```
"Find Korean news stations"
→ advanced_search(country="KR", tag="news")

"High quality classical"
→ advanced_search(tag="classical", min_bitrate=192)

"Japanese stations"
→ search_by_country("JP")
```

### Favorites

```
"Add to favorites"
→ add_favorite(station)

"Play my first favorite"
→ play_favorite(0)

"Show my favorites"
→ get_favorites()
```

### Recommendations

```
"I want relaxing music"
→ recommend("relaxing")

"What's good for this weather?"
→ recommend_by_weather("Seoul")

"Recommend based on my taste"
→ personalized_recommend()

"Find similar stations"
→ similar_stations()
```

### Timer & Alarm

```
"Set sleep timer for 30 minutes"
→ set_sleep_timer(30)

"Cancel sleep timer"
→ set_sleep_timer(0)

"Wake me up at 7am with jazz"
→ set_alarm(7, 0, "jazz")

"Set alarm for 6:30 with classical"
→ set_alarm(6, 30, "classical")
```

### Volume

```
"Set volume to 50"
→ set_volume(50)

"What's the current volume?"
→ get_volume()
```

## Multilingual Search

Supports 50+ languages:

| Language | Example | Translated |
|----------|---------|------------|
| Korean | "jazz", "classical", "news" | jazz, classical, news |
| Japanese | "ジャズ", "クラシック" | jazz, classical |
| Chinese | "爵士乐", "古典音乐" | jazz, classical |
| Russian | "джаз", "классика" | jazz, classical |
| Arabic | "موسيقى", "أخبار" | music, news |
| Hindi | "संगीत", "समाचार" | music, news |

## Quality Filters

| Keyword | Filter |
|---------|--------|
| "HQ", "high quality" | 192kbps+ |
| "HD" | 256kbps+ |
| "LQ", "low quality" | 96kbps or less |

## Mood Keywords

| Mood | Tags |
|------|------|
| relaxing | lounge, ambient, classical, jazz |
| energetic | dance, electronic, pop, rock |
| focus | classical, ambient, instrumental |
| sleep | ambient, classical |
| morning | pop, jazz |
| workout | electronic, dance, rock |
| romantic | jazz, classical |

## Country Codes

| Code | Country |
|------|---------|
| KR | South Korea |
| US | United States |
| JP | Japan |
| GB | United Kingdom |
| DE | Germany |
| FR | France |
| CN | China |
| BR | Brazil |
| AU | Australia |
| CA | Canada |

## Player Backends

| Backend | Description |
|---------|-------------|
| mpv | Best quality, volume control, metadata |
| vlc | Widely installed, stable |
| ffplay | Lightweight, included with ffmpeg |
| browser | No installation needed, fallback |

Auto-detection priority: mpv > vlc > ffplay > browser

## Data Storage

All data in `~/.radiocli/`:

| File | Description |
|------|-------------|
| `favorites.json` | Favorite stations |
| `history.json` | Listening history |
| `recognized_songs.json` | Song recognition history |
| `last_station.json` | Last played (for resume) |
| `mpv.sock` | mpv IPC socket |

Database: `~/RadioCli/radio_stations.db` (51k+ stations)

## Requirements

- **mpv** (recommended): Best audio quality
  ```bash
  brew install mpv        # macOS
  apt install mpv         # Linux
  winget install mpv      # Windows
  ```

- **chromaprint** (optional): For song recognition
  ```bash
  brew install chromaprint ffmpeg
  ```

## Troubleshooting

### Radio won't play
- Check mpv: `which mpv`
- Kill existing: `pkill mpv`
- Try different backend: `set_player_backend("vlc")`

### No song info
- Not all stations provide metadata
- Try premium/high-quality stations

### Connection errors
- Check internet connection
- Radio Browser API may be temporarily down

### Dead stations
- Run `health_check()` to verify
- Run `purge_dead()` to clean up
