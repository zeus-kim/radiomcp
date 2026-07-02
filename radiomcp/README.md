# radiomcp

MCP server for internet radio - search and play 55,000+ stations from 200+ countries.

Powered by [Airtune API](https://api.airtune.ai).

## Installation

```bash
pip install radiomcp
```

### Player (choose one)

| Player | macOS | Linux | Windows |
|--------|-------|-------|---------|
| **mpv** (recommended) | `brew install mpv` | `apt install mpv` | `winget install mpv` |
| **VLC** | `brew install vlc` | `apt install vlc` | [vlc.io](https://vlc.io) |
| **ffplay** | `brew install ffmpeg` | `apt install ffmpeg` | [ffmpeg.org](https://ffmpeg.org) |
| **browser** | No install needed | No install needed | No install needed |

Auto-detection: mpv > vlc > ffplay > browser

## Claude Desktop Setup

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

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

Restart Claude Desktop.

## Usage

Just ask Claude naturally:

- "Play some jazz radio"
- "Find Korean news stations"
- "What song is playing?"
- "I want relaxing music"
- "Set sleep timer for 30 minutes"
- "Wake me up at 7am with jazz"
- "Stop the radio"

## Features

- **51,000+ stations** from 200+ countries
- **Fast search** (~5ms local DB)
- **Multilingual** (50+ languages: Korean, Japanese, Chinese, Russian, etc.)
- **Korean broadcasters** (KBS, MBC, YTN - auto token refresh)
- **AI recommendations** (mood, time, weather, personalized)
- **Song recognition** (stream metadata + Whisper)
- **Sleep timer & alarm**
- **Favorites & history**
- **Volume control** (mpv)
- **Auto URL refresh** (handles token expiration)
- **Remote blocklist** (GitHub-based updates)
- **Daily updates** (new stations synced automatically)

## Tools

### Playback
| Tool | Description |
|------|-------------|
| `play` | Start playback |
| `stop` | Stop playback |
| `resume` | Resume last station |
| `now_playing` | Current song info |
| `set_volume` / `get_volume` | Volume control |

### Search
| Tool | Description |
|------|-------------|
| `search` | Search by keyword |
| `search_by_country` | Search by country code |
| `advanced_search` | Combined filters |
| `get_popular` | Popular stations |

### Recommendations
| Tool | Description |
|------|-------------|
| `recommend` | Mood-based (relaxing, energetic, focus) |
| `recommend_by_weather` | Weather-based |
| `recommend_by_time` | Time of day based |
| `personalized_recommend` | Based on history |
| `similar_stations` | Find similar |

### Timer
| Tool | Description |
|------|-------------|
| `set_sleep_timer` | Auto-stop after N minutes |
| `set_alarm` | Wake-up alarm |

### Favorites
| Tool | Description |
|------|-------------|
| `get_favorites` | List favorites |
| `add_favorite` | Add to favorites |
| `play_favorite` | Play from favorites |

### More
| Tool | Description |
|------|-------------|
| `recognize_song` | Song recognition |
| `get_history` | Listening history |
| `get_user_profile` | Taste analysis |
| `get_radio_guide` | Full guide for AI |

See [HELP.md](HELP.md) for complete documentation.

## Requirements

- Python 3.10+
- Audio player (mpv recommended)

## Data Sources

- [Radio Browser](https://www.radio-browser.info/) - station metadata ([ODbL](https://opendatacommons.org/licenses/odbl/))
- [Icecast Directory](https://dir.xiph.org/) - additional stations (open source)
- [Whisper](https://github.com/openai/whisper) - DJ speech recognition
- [wttr.in](https://wttr.in/) - weather data

## License

- **Code**: MIT - See [LICENSE](LICENSE)
- **Station Database**: ODbL 1.0 - See [DATA_LICENSE.md](DATA_LICENSE.md)
- **Attribution**: See [ATTRIBUTION.md](ATTRIBUTION.md)

## Disclaimer

See [DISCLAIMER.md](DISCLAIMER.md) for terms of use.
