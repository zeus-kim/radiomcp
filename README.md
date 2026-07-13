# radiomcp

<!-- mcp-name: io.github.zeus-kim/radiomcp -->

[![MCP](https://glama.ai/mcp/servers/zeus-kim/radiomcp/badge)](https://glama.ai/mcp/servers/zeus-kim/radiomcp)
[![PyPI](https://img.shields.io/pypi/v/radiomcp)](https://pypi.org/project/radiomcp/)
[![Python](https://img.shields.io/pypi/pyversions/radiomcp)](https://pypi.org/project/radiomcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Internet radio + AI DJ for Claude and your terminal. ~55,000 stations, 197 countries.**

```bash
pip install radiomcp && radiomcp
```

First run auto-registers with Claude Desktop / Claude Code. Restart Claude and you're done.

Powered by [Airtune API](https://api.airtune.ai) (55,000+ radio stations worldwide)

---

## Features

- **MCP Server** — Control radio with natural language through Claude
- **TUI Player** — Interactive terminal player with search and favorites
- **AI DJ Broadcast** — 24/7 personalized DJ with voice commentary, news, and music
- **Apple Music Integration** — Native library playback via persistent ID
- **Video Playback** — Watch YouTube/live streams in a window on your Mac
- **Multi-provider Music** — YouTube, Apple Music, Spotify support

---

## Quick Start

### MCP Server (Claude Integration)

```bash
pip install radiomcp && radiomcp
```

Auto-detected and registered on first run. Or manually add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "radio": { "command": "radiomcp" }
  }
}
```

Then just ask Claude:

> "Play some late-night jazz"  
> "Start a DJ set with K-pop hits"  
> "Play Bruno Mars from my Apple Music library"  
> "Show me a live news stream"  
> "What's playing right now?"

### TUI Player

```bash
radio
```

Interactive terminal player. Type to search, numbers to play.

---

## DJ Broadcast System

The killer feature: AI-powered personalized radio broadcast.

```
Claude: "Start a morning DJ set with upbeat K-pop"
```

### What it does:
- **Voice DJ commentary** between songs (edge-tts, 10+ languages)
- **Live news integration** from RSS feeds
- **24-hour scheduling** with genre/mood slots
- **Cross-instance playback lock** — only one plays at a time
- **Apple Music native playback** — no clicking, uses persistent ID
- **YouTube fallback** — if track not in library

### DJ Tools

| Tool | Description |
|---|---|
| `dj_start` | Start a DJ set with songs/provider |
| `dj_stop` | Stop the current DJ set |
| `dj_health` | Check DJ system status |
| `dj_play_video` | Play video in a window (YouTube, live streams) |
| `dj_stop_video` | Stop video playback |
| `dj_current_slot` | Current broadcast slot info |
| `dj_schedule` | View/edit 24h broadcast schedule |

---

## Radio Tools

| Tool | Description |
|---|---|
| `play` | Play by URL or search query |
| `stop` | Stop playback |
| `now_playing` | Current station and track |
| `search` | Search by keyword, genre, country |
| `recommend` | AI recommendations by mood or context |
| `get_favorites` / `add_favorite` | Saved stations |
| `get_history` | Listening history |
| `set_volume` / `get_volume` | Volume control |
| `search_by_country` | Stations by country code |
| `recognize_song` | Identify currently playing song |

---

## TUI Keyboard Shortcuts

### Search
| Key | Function |
|---|---|
| `/` | Search |
| `g` | Genre browser |
| `c` | Country browser |
| `p` | Popular stations |

### Playback
| Key | Function |
|---|---|
| `1`–`9` | Play station from list |
| `r` | Resume last station |
| `s` | Stop |
| `v` / `v+` / `v-` | Volume control |
| `q` | Quit |

### Favorites
| Key | Function |
|---|---|
| `f` | View favorites |
| `+` / `-` | Add / remove current |
| `<` / `>` | Prev / next favorite |

---

## Player Backends

Auto-detected: **mpv → vlc → ffplay → browser**

```bash
brew install mpv      # macOS (recommended)
apt install mpv       # Linux
```

---

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `RADIOCLI_DJ` | Enable DJ mode | `0` |
| `RADIOCLI_LANG` | UI language | auto-detect |
| `ANTHROPIC_API_KEY` | Claude-powered features | — |
| `OPENAI_API_KEY` | OpenAI-powered features | — |

---

## License

- **Code**: MIT — [LICENSE](LICENSE)
- **Station data**: ODbL 1.0 — [DATA_LICENSE.md](DATA_LICENSE.md)

---

# 한국어 / Korean

## radiomcp — AI DJ 인터넷 라디오

**Claude와 터미널을 위한 인터넷 라디오 + AI DJ. 55,000+ 방송국, 197개국.**

```bash
pip install radiomcp && radiomcp
```

첫 실행 시 Claude Desktop/Claude Code에 자동 등록됩니다.

---

## 주요 기능

- **MCP 서버** — Claude에서 자연어로 라디오 제어
- **TUI 플레이어** — 터미널에서 검색 + 즐겨찾기
- **AI DJ 방송** — 24시간 맞춤형 DJ (음성 코멘트, 뉴스, 음악)
- **Apple Music 연동** — 라이브러리에서 바로 재생 (클릭 없이)
- **비디오 재생** — YouTube/라이브 스트림을 창으로 시청
- **멀티 프로바이더** — YouTube, Apple Music, Spotify 지원

---

## 사용 예시

Claude에게 말하기:

> "재즈 틀어줘"  
> "심수봉 노래로 DJ 셋 시작해"  
> "내 애플 뮤직에서 BTS 틀어줘"  
> "YTN 라이브 뉴스 보여줘"  
> "지금 뭐 나와?"

### DJ 방송 시스템

핵심 기능: AI 기반 맞춤형 라디오 방송

- **음성 DJ 코멘트** — 곡 사이에 AI 목소리 (한국어 포함 10개 언어)
- **실시간 뉴스** — RSS 피드에서 자동 수집
- **24시간 스케줄** — 시간대별 장르/분위기 설정
- **크로스-인스턴스 잠금** — 여러 인스턴스 중 하나만 재생
- **Apple Music 네이티브** — persistent ID로 정확한 재생
- **YouTube 폴백** — 라이브러리에 없으면 자동 전환

---

## DJ 도구

| 도구 | 설명 |
|---|---|
| `dj_start` | DJ 셋 시작 (노래 목록 + 프로바이더) |
| `dj_stop` | DJ 셋 정지 |
| `dj_health` | DJ 시스템 상태 확인 |
| `dj_play_video` | 비디오 창 재생 (YouTube, 라이브) |
| `dj_stop_video` | 비디오 정지 |
| `dj_schedule` | 24시간 방송 스케줄 보기/편집 |

---

## 라디오 도구

| 도구 | 설명 |
|---|---|
| `play` | URL 또는 검색어로 재생 |
| `stop` | 정지 |
| `now_playing` | 현재 방송국/곡 |
| `search` | 키워드/장르/국가 검색 |
| `recommend` | AI 추천 (분위기, 상황별) |
| `get_favorites` | 즐겨찾기 보기 |
| `recognize_song` | 현재 곡 인식 (Shazam 스타일) |

---

## 설치

```bash
pip install radiomcp

# 플레이어 백엔드 (하나만 있으면 됨)
brew install mpv      # macOS 권장
```

---

## v1.2.7 새 기능

- **Apple Music 네이티브 재생** — GUI 클릭 없이 persistent ID로 정확한 재생
- **DJ 라이브러리 큐잉** — 재생 전 라이브러리 곡 미리 확인
- **크로스-인스턴스 잠금** — 여러 radiomcp 인스턴스 간 재생 충돌 방지
- **창 비디오 재생** — `dj_play_video`로 YouTube/라이브 스트림 시청
- **dj_health 개선** — Apple Music 재생도 정상 상태로 인식

---

## 라이선스

- **코드**: MIT
- **방송국 데이터**: ODbL 1.0
