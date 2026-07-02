#!/usr/bin/env python3
"""One-shot MusicKit (Apple Music API) setup for radiomcp.

Generates the developer token from your MusicKit .p8 key, then opens a tiny
local page so you can authorize Apple Music once and capture your Music User
Token. Everything is written to ~/.radiocli/musickit/config.json, which radiomcp
reads for apple_search / apple_add_artist / apple_add_album / dj_play_artist.

Prerequisites (from https://developer.apple.com, Certificates/IDs & Profiles):
  1. Create a Media ID (Identifiers -> Media IDs).
  2. Create a Key with "Media Services (MusicKit...)" enabled; download the
     AuthKey_XXXXXXXXXX.p8 and note the Key ID.
  3. Note your Team ID (Membership details).
  4. You need an active Apple Music subscription to authorize.

Usage:
  python3 scripts/setup_musickit.py \
      --p8 /path/to/AuthKey_XXXXXXXXXX.p8 \
      --key-id XXXXXXXXXX \
      --team-id YYYYYYYYYY

Then open the printed http://127.0.0.1:<port> URL, click Authorize, sign in.
Requires: pyjwt, cryptography  (pip install "pyjwt[crypto]")
"""

import argparse
import json
import os
import time
import http.server
import socketserver

import jwt  # pip install "pyjwt[crypto]"

MK_DIR = os.path.expanduser("~/.radiocli/musickit")
MK_CONFIG = os.path.join(MK_DIR, "config.json")


def make_dev_token(p8_path, key_id, team_id):
    key = open(os.path.expanduser(p8_path)).read()
    iat = int(time.time())
    exp = iat + 15552000  # ~180 days (Apple max)
    tok = jwt.encode({"iss": team_id, "iat": iat, "exp": exp},
                     key, algorithm="ES256",
                     headers={"kid": key_id, "alg": "ES256"})
    return tok, exp


def save_config(**kw):
    os.makedirs(MK_DIR, exist_ok=True)
    cfg = {}
    if os.path.exists(MK_CONFIG):
        try:
            cfg = json.load(open(MK_CONFIG))
        except Exception:
            cfg = {}
    cfg.update(kw)
    json.dump(cfg, open(MK_CONFIG, "w"))
    return cfg


AUTH_HTML = """<!doctype html><html><head><meta charset="utf-8">
<script src="https://js-cdn.music.apple.com/musickit/v3/musickit.js" data-web-components async></script>
<style>body{font-family:-apple-system,sans-serif;padding:48px;text-align:center}
button{font-size:22px;padding:14px 28px;border-radius:10px;border:0;background:#fa2d48;color:#fff;cursor:pointer}
#out{margin-top:24px;font-size:18px;color:#333;white-space:pre-wrap}</style></head>
<body><h2>radiomcp - Apple Music Authorization</h2>
<button id="go">Sign in &amp; Authorize Apple Music</button>
<div id="out"></div>
<script>
let mk=null;
document.addEventListener('musickitloaded', async function(){
  await MusicKit.configure({developerToken:"__DEV__", app:{name:"radiomcp", build:"1.0"}});
  mk=MusicKit.getInstance();
  document.getElementById('out').textContent="Ready. Click the button.";
});
document.getElementById('go').onclick=async function(){
  try{
    if(!mk) mk=MusicKit.getInstance();
    const ut=await mk.authorize();
    const r=await fetch('/save',{method:'POST',body:ut});
    document.getElementById('out').textContent = r.ok ? "Done! Token saved. You can close this window." : "Save failed: "+r.status;
  }catch(e){ document.getElementById('out').textContent="Error: "+e; }
};
</script></body></html>"""


def serve_auth(dev_token, port):
    html = AUTH_HTML.replace("__DEV__", dev_token)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(n).decode("utf-8")
            save_config(music_user_token=body)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            print("\n✅ Music User Token saved to", MK_CONFIG)
            print("   Setup complete. You can Ctrl-C this script now.")

    socketserver.TCPServer.allow_reuse_address = True
    print(f"\n👉 Open this in your browser, click Authorize, sign in:\n   http://127.0.0.1:{port}\n")
    socketserver.TCPServer(("127.0.0.1", port), H).serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--p8", required=True, help="Path to AuthKey_XXXX.p8")
    ap.add_argument("--key-id", required=True)
    ap.add_argument("--team-id", required=True)
    ap.add_argument("--port", type=int, default=8790)
    a = ap.parse_args()

    tok, exp = make_dev_token(a.p8, a.key_id, a.team_id)
    # Copy the key into place so radiomcp can auto-refresh the dev token later.
    os.makedirs(MK_DIR, exist_ok=True)
    dst_p8 = os.path.join(MK_DIR, os.path.basename(a.p8))
    if os.path.abspath(os.path.expanduser(a.p8)) != os.path.abspath(dst_p8):
        open(dst_p8, "w").write(open(os.path.expanduser(a.p8)).read())
        os.chmod(dst_p8, 0o600)
    save_config(team_id=a.team_id, key_id=a.key_id, p8_path=dst_p8,
                dev_token=tok, dev_token_exp=exp)
    print("✅ Developer token generated (valid ~180 days).")
    serve_auth(tok, a.port)


if __name__ == "__main__":
    main()
