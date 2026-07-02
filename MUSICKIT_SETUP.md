# Apple Music (MusicKit) setup

radiomcp can add songs to your Apple Music library and play them natively
(exact tracks, no YouTube fallback). This needs a one-time MusicKit setup.

## Requirements
- An Apple Developer Program membership ($99/yr)
- An active Apple Music subscription (to authorize your account)
- Python deps: `pip install "pyjwt[crypto]"`

## 1. Create a MusicKit key (developer.apple.com)
Certificates, Identifiers & Profiles:
1. **Identifiers → +** → **Media IDs** → register one (any description/identifier).
2. **Keys → +** → name it, enable **Media Services (MusicKit, ShazamKit, Apple Music Feed)** → Configure and select your Media ID → Continue → Register.
3. **Download** the `AuthKey_XXXXXXXXXX.p8` (one-time download; back it up).
   The **Key ID** is the `XXXXXXXXXX` part.
4. Find your **Team ID** in Membership details.

## 2. Run the setup script
```bash
python3 scripts/setup_musickit.py \
    --p8 ~/Downloads/AuthKey_XXXXXXXXXX.p8 \
    --key-id XXXXXXXXXX \
    --team-id YYYYYYYYYY
```
It generates the developer token, then prints a local URL (e.g.
`http://127.0.0.1:8790`). Open it, click **Sign in & Authorize Apple Music**,
sign in — your Music User Token is captured automatically.

Everything is stored in `~/.radiocli/musickit/config.json`. The developer token
auto-refreshes from the `.p8` when it nears expiry.

## 3. Use it
- `apple_search("term")` — search the catalog
- `apple_add_artist("BLACKPINK")` — add an artist's albums to your library
- `apple_add_album("artist album")` — add a specific album
- `dj_play_artist("혜은이")` — one call: add albums → wait for iCloud sync →
  play natively with DJ commentary (great for small local models)

If MusicKit isn't set up, `dj_play_artist` returns a clear message and you can
still use `dj_play_set(provider="youtube")`.

## Notes
- Adding **albums** is reliable; adding individual catalog **song IDs** often
  silently no-ops, so radiomcp adds albums.
- Multi-artist "hit collection" compilations are skipped so your library isn't
  polluted with other singers.
- iCloud Music Library / Sync Library must be ON in the Music app for added
  songs to appear on this device (sync can take up to ~1 minute).
