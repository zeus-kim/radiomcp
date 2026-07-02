"""Apple Music API (MusicKit) helper for radiomcp.

Uses a developer token (ES256 JWT, auto-refreshed from the .p8) plus a stored
Music User Token to search the Apple Music catalog and add songs/albums to the
user's library. Once songs are in the library, dj_broadcast's native playback
(resolve_library_track / play_library_track_by_id) can play them exactly.

Config file: ~/.radiocli/musickit/config.json
  { team_id, key_id, p8_path, dev_token, dev_token_exp, music_user_token,
    storefront? }
"""

import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error

try:
    import jwt as _jwt
except Exception:
    _jwt = None

API = "https://api.music.apple.com"
MK_DIR = os.path.expanduser("~/.radiocli/musickit")
MK_CONFIG = os.path.join(MK_DIR, "config.json")


def _load():
    with open(MK_CONFIG) as f:
        return json.load(f)


def _save(c):
    os.makedirs(MK_DIR, exist_ok=True)
    with open(MK_CONFIG, "w") as f:
        json.dump(c, f)


def is_configured():
    try:
        c = _load()
        return bool(c.get("dev_token") or c.get("p8_path")) and bool(c.get("music_user_token"))
    except Exception:
        return False


def _dev_token(c):
    """Return a valid developer token, regenerating from the .p8 if near expiry."""
    if c.get("dev_token") and (c.get("dev_token_exp", 0) - time.time() > 86400):
        return c["dev_token"]
    if _jwt and c.get("p8_path") and os.path.exists(c["p8_path"]):
        key = open(c["p8_path"]).read()
        iat = int(time.time())
        exp = iat + 15552000  # ~180 days
        tok = _jwt.encode({"iss": c["team_id"], "iat": iat, "exp": exp},
                          key, algorithm="ES256",
                          headers={"kid": c["key_id"], "alg": "ES256"})
        c["dev_token"] = tok
        c["dev_token_exp"] = exp
        _save(c)
        return tok
    return c.get("dev_token")


def _request(method, path_or_url, user=True, data=None, timeout=25):
    c = _load()
    url = path_or_url if path_or_url.startswith("http") else API + path_or_url
    headers = {"Authorization": "Bearer " + _dev_token(c)}
    if user:
        headers["Music-User-Token"] = c.get("music_user_token", "")
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=headers, method=method, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            txt = resp.read().decode()
            return resp.status, (json.loads(txt) if txt.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:300]}
    except Exception as e:
        return 0, {"error": str(e)}


def storefront():
    try:
        c = _load()
        if c.get("storefront"):
            return c["storefront"]
    except Exception:
        pass
    st, d = _request("GET", "/v1/me/storefront")
    sf = "us"
    if st == 200 and d.get("data"):
        sf = d["data"][0]["id"]
        try:
            c = _load(); c["storefront"] = sf; _save(c)
        except Exception:
            pass
    return sf


def search_catalog(term, types="songs,artists,albums", limit=10, sf=None):
    """Search the Apple Music catalog. Returns the 'results' dict."""
    sf = sf or storefront()
    q = urllib.parse.urlencode({"term": term, "types": types, "limit": limit})
    st, d = _request("GET", f"/v1/catalog/{sf}/search?{q}", user=False)
    if st != 200:
        return {"error": d.get("error", f"http {st}")}
    return d.get("results", {})


def add_to_library(song_ids=None, album_ids=None):
    """Add catalog songs and/or albums to the user's library."""
    params = []
    for s in (song_ids or []):
        params.append(("ids[songs]", s))
    for a in (album_ids or []):
        params.append(("ids[albums]", a))
    if not params:
        return {"status": "error", "error": "no ids given"}
    q = urllib.parse.urlencode(params)
    st, d = _request("POST", f"/v1/me/library?{q}")
    ok = st in (200, 201, 202, 204)
    return {"status": "ok" if ok else "error", "http": st,
            "added_songs": len(song_ids or []), "added_albums": len(album_ids or []),
            **({} if ok else {"error": d.get("error")})}


def add_artist(artist, albums=5, sf=None):
    """Add an artist's ALBUMS to the user's library (most reliable path).

    Adding whole albums lands tracks in the library reliably, where adding
    individual catalog song IDs often silently no-ops. After this and a short
    iCloud sync, dj_play_set(provider='apple_music') can play them natively.
    """
    sf = sf or storefront()
    res = search_catalog(artist, types="albums", limit=albums, sf=sf)
    if res.get("error"):
        return {"status": "error", "error": res["error"]}
    albs = res.get("albums", {}).get("data", [])
    if not albs:
        return {"status": "error", "error": f"no albums found for: {artist}"}
    ids = [a["id"] for a in albs]
    names = [a["attributes"].get("name") for a in albs]
    aname = albs[0]["attributes"].get("artistName", artist)
    r = add_to_library(album_ids=ids)
    return {"status": r.get("status"), "artist": aname,
            "added_albums": len(ids), "albums": names, "http": r.get("http")}


def library_songs(artist, limit=25):
    """Return names of the artist's songs currently in the user's library."""
    q = urllib.parse.urlencode({"term": artist, "types": "library-songs",
                                "limit": limit})
    st, d = _request("GET", f"/v1/me/library/search?{q}")
    if st != 200:
        return []
    data = d.get("results", {}).get("library-songs", {}).get("data", [])
    out, seen = [], set()
    for s in data:
        nm = s.get("attributes", {}).get("name")
        if nm and nm not in seen:
            seen.add(nm)
            out.append(nm)
    return out


def top_song_names(artist, limit=10, sf=None):
    """Catalog top-song names for an artist (used as play queries / fallback)."""
    sf = sf or storefront()
    res = search_catalog(artist, types="artists", limit=1, sf=sf)
    arts = res.get("artists", {}).get("data", []) if not res.get("error") else []
    if not arts:
        return []
    aid = arts[0]["id"]
    st, d = _request("GET",
                     f"/v1/catalog/{sf}/artists/{aid}/view/top-songs?limit={limit}",
                     user=False)
    if st != 200:
        return []
    return [s["attributes"]["name"] for s in d.get("data", [])]


def add_album(album_query, sf=None):
    """Find an album by 'artist album' query and add the whole album to library."""
    sf = sf or storefront()
    res = search_catalog(album_query, types="albums", limit=1, sf=sf)
    if res.get("error"):
        return {"status": "error", "error": res["error"]}
    albums = res.get("albums", {}).get("data", [])
    if not albums:
        return {"status": "error", "error": f"album not found: {album_query}"}
    alb = albums[0]
    r = add_to_library(album_ids=[alb["id"]])
    return {"status": r.get("status"), "album": alb["attributes"].get("name"),
            "artist": alb["attributes"].get("artistName"), "http": r.get("http")}
