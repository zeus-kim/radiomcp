# ============================================================
# Chart Tools - Spotify & Radio Charts
# ============================================================

@mcp.tool()
def spotify_chart(country: str = "us", limit: int = 10) -> str:
    """
    Get Spotify streaming chart by country from kworb.net
    
    Args:
        country: Country code (us, de, kr, fr, gb, jp, etc) - lowercase
        limit: Number of results (default 10, max 50)
    
    Returns:
        Top streamed songs in the specified country
    """
    import re
    cc = country.lower()
    url = f"https://kworb.net/spotify/country/{cc}_daily.html"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="ignore")
        songs = []
        for m in re.finditer(r'<td class="text mp"><div><a[^>]*>([^<]+)</a> - <a[^>]*>([^<]+)</a>', html):
            songs.append({"rank": len(songs)+1, "artist": m.group(1).strip(), "title": m.group(2).strip()})
            if len(songs) >= limit: break
        return json.dumps({"source": "Spotify Daily Chart via kworb.net", "country": cc.upper(), "date": datetime.now().strftime("%Y-%m-%d"), "songs": songs}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e), "url": url})

@mcp.tool()
def radio_chart(country: str = "US", days: int = 7, limit: int = 10) -> str:
    """
    Get radio airplay chart by country - shows what songs are actually playing on radio stations worldwide.
    Uses AirTune API which monitors 40,000+ radio stations in real-time.
    
    Args:
        country: Country code (US, DE, KR, FR, GB, JP, etc) - uppercase
        days: Time period (1, 7, or 30 days)
        limit: Number of results (default 10)
    
    Returns:
        Top songs by radio airplay in the specified country
    """
    cc = country.upper()
    url = f"https://api.airtune.ai/charts/country?countrycode={cc}&days={days}&limit={limit*3}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode())
        if isinstance(data, list):
            # Filter out station names (require 3+ stations)
            filtered = [d for d in data if d.get("stations", 0) >= 3][:limit]
            songs = [{"rank": i+1, "artist": s["artist"], "title": s["title"], "plays": s.get("plays",0), "stations": s.get("stations",0)} for i,s in enumerate(filtered)]
            return json.dumps({"source": "AirTune Radio Airplay", "country": cc, "period": f"last {days} days", "songs": songs}, indent=2)
        return json.dumps(data)
    except Exception as e:
        return json.dumps({"error": str(e)})

@mcp.tool()
def global_radio_chart(days: int = 7, limit: int = 10) -> str:
    """
    Get global radio airplay chart - songs playing on the most stations worldwide.
    
    Args:
        days: Time period (1, 7, or 30 days)
        limit: Number of results (default 10)
    
    Returns:
        Top songs by global radio airplay
    """
    url = f"https://api.airtune.ai/charts/songs?days={days}&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode())
        if isinstance(data, list):
            songs = [{"rank": i+1, "artist": s["artist"], "title": s["title"], "plays": s.get("plays",0), "stations": s.get("stations",0)} for i,s in enumerate(data)]
            return json.dumps({"source": "AirTune Global Radio", "period": f"last {days} days", "songs": songs}, indent=2)
        return json.dumps(data)
    except Exception as e:
        return json.dumps({"error": str(e)})

@mcp.tool()
def rising_songs(limit: int = 10) -> str:
    """
    Get songs gaining momentum on radio today - tracks that are rapidly increasing in airplay.
    
    Args:
        limit: Number of results (default 10)
    
    Returns:
        Songs with the biggest airplay gains today
    """
    url = f"https://api.airtune.ai/charts/rising?limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode())
        if isinstance(data, dict) and "songs" in data:
            songs = data["songs"][:limit]
            return json.dumps({"source": "AirTune Rising", "songs": songs}, indent=2)
        return json.dumps(data)
    except Exception as e:
        return json.dumps({"error": str(e)})

