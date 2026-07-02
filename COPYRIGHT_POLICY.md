# Copyright Takedown Policy

## Overview
RadioMCP is a search and playback tool for publicly available internet radio streams.
We do not host, store, or redistribute any audio content.
Stream URLs are provided by third-party radio stations and public directories.

## Takedown Process

### Step 1: Submit Request
Copyright holders can submit takedown requests via:
- GitHub Issues: https://github.com/meshpop/radiomcp/issues
- Email: mpop@mpop.dev

Required information:
- Station name or stream URL
- Proof of copyright ownership
- Contact information

### Step 2: Immediate Action (within 24 hours)
Upon receiving a valid takedown request:
1. **API Server** — Station is marked `is_blocked=1` in the database. Blocked stations are immediately excluded from all search results and API responses.
2. **GitHub Blocklist** — Station is added to the public blocklist (`blocklist.json`).

### Step 3: Client Propagation (automatic)
- All RadioMCP clients automatically sync the blocklist on every app launch.
- Blocked stations are purged from local databases during sync.
- No user action is required — removal is automatic.

## Technical Implementation

### API-Level Blocking
- All search endpoints filter out `is_blocked=1` stations
- Browse, random, and category endpoints also exclude blocked stations
- Blocked stations cannot appear in any API response

### Client-Level Blocking
- On startup, clients fetch the latest blocklist from GitHub
- `purge_blocked_from_db()` removes matching stations from local SQLite DB
- Stations can be blocked by: name pattern, stream URL, or station UUID
- `refresh_blocklist()` MCP tool allows manual sync at any time

### Blocklist Format
```json
{
  "blocked": [{"pattern": "Station Name", "reason": "Copyright takedown"}],
  "blocked_urls": ["http://example.com/stream"],
  "station_ids": ["uuid-here"],
  "domains": ["example.com"]
}
```

## Response Timeline
| Action | Timeline |
|--------|----------|
| Acknowledge request | Within 24 hours |
| API server blocking | Within 24 hours |
| GitHub blocklist update | Within 48 hours |
| Client propagation | Next app launch |

## Contact
- GitHub: https://github.com/meshpop/radiomcp/issues
- Email: mpop@mpop.dev

## Data Sources
- [Radio Browser](https://www.radio-browser.info/) — ODbL 1.0 License
- [Icecast Directory](https://dir.xiph.org/) — Open Source
- Stream metadata verified independently from station streams