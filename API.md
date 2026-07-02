# Radio API Reference

**Base URL:** `https://api.airtune.ai`

## Endpoints

### Search
```
GET /search?q={query}&limit={n}&countrycode={CC}
```
Full-text search across name, tags, country. Supports multilingual queries (Korean, Japanese, Chinese, etc.)

**Example:**
```bash
curl "https://api.airtune.ai/search?q=jazz&limit=10"
curl "https://api.airtune.ai/search?q=kpop&countrycode=KR"
```

### Stats
```
GET /stats
```
Database statistics: total stations, verified count, sources.

### Station by ID
```
GET /station/{uuid}
```

### Browse by Tag
```
GET /stations?tag={tag}&limit={n}
GET /stations/bytag/{tag}?limit={n}   # also works
```

### Browse by Country
```
GET /stations?countrycode={CC}&limit={n}
GET /stations/bycountrycode/{CC}?limit={n}   # also works
```

### List Stations (with filters)
```
GET /stations?countrycode=KR&tag=pop&limit=10
```

### Recommendations
```
GET /recommend/hq          # High quality (128kbps+)
GET /recommend/popular     # Most clicked
GET /recommend/voted       # Most voted
GET /recommend/genre/{genre}
```

### Blocklist
```
GET /blocklist
```
Returns blocked station IDs, URLs, domains, patterns.

### Analytics
```
GET /analytics
```
Usage statistics (searches, countries, popular queries).

---

## SVD API (Recommendations)

**Base URL:** `https://api.airtune.ai/svd`

### Similar Artist
```
GET /similar/artist/{name}
```
Returns artists with similar radio programming patterns.

**Example:**
```bash
curl "https://api.airtune.ai/svd/similar/artist/BTS"
```

### Similar Station
```
GET /similar/station/{uuid}
```

### Search Artist
```
GET /search/artist?q={query}
```

### Stats
```
GET /stats
```
Embedding dimensions: 38K artists × 6K stations.

---

## Response Format

All responses are JSON:
```json
{
  "total": 100,
  "limit": 30,
  "offset": 0,
  "data": [
    {
      "id": "uuid",
      "name": "Station Name",
      "url": "stream URL",
      "country": "US",
      "tags": "jazz,smooth",
      "bitrate": 128
    }
  ]
}
```

## Rate Limits
- 100 requests/minute per IP
- Cached for 5 minutes

## Usage
Free for personal use. Commercial use requires contact.
