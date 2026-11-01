#!/usr/bin/env python3
"""Refresh vpn/servers.json from the public VPN Gate API (vpngate.net academic experiment).
The API returns a rotating sample, so we fetch several times, merge with servers seen in the
last 48h, drop relays that don't accept a TCP connection, and keep the best few per country.
Never writes a broken/too-small list (exits non-zero instead)."""
import base64, csv, io, json, re, socket, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

API = "https://www.vpngate.net/api/iphone/"
OUT = "vpn/servers.json"
POOL = "vpn/pool.json"  # every relay seen in the last 48h (the API only shows a rotating sample)
FETCHES, PER_COUNTRY, BIG_COUNTRY, KEEP_HOURS = 6, 5, 8, 48
MIN_SERVERS, MIN_COUNTRIES = 15, 5
now = datetime.now(timezone.utc)

def fetch():
    req = urllib.request.Request(API, headers={"User-Agent": "Mozilla/5.0 SafeSpot-updater"})
    return urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")

def parse(txt):
    out = {}
    for f in csv.reader(io.StringIO(txt)):
        if len(f) < 15 or f[0].startswith(("*", "#")): continue
        try: cfg = base64.b64decode(f[14]).decode("utf-8", "replace")
        except Exception: continue
        m = re.search(r"(?m)^remote\s+(\S+)\s+(\d+)", cfg)
        if not m or "<ca>" not in cfg: continue
        proto = (re.search(r"(?m)^proto\s+(\S+)", cfg) or [None, "tcp"])[1]
        cfg = "\n".join(l for l in cfg.replace("\r", "").split("\n") if l.strip() and not l.lstrip().startswith(("#", ";"))) + "\n"
        sid = f"vg-{f[1]}-{m[2]}"
        out[sid] = dict(id=sid, type="openvpn", host=f[1], port=int(m[2]), proto=proto, country=f[5], countryCode=f[6],
                        config=cfg, provider="vpngate", speedMbps=round(int(f[4] or 0) / 1e6, 1), pingMs=int(f[3] or 0) if f[3].isdigit() else 0,
                        score=int(f[2] or 0), uptimeMs=int(f[8] or 0) if f[8].isdigit() else 0, lastSeen=now.isoformat())
    return out

pool = {}
try:
    prev = json.load(open(POOL)) if __import__("os").path.exists(POOL) else json.load(open(OUT))
    for s in prev.get("servers", []):
        seen = s.get("lastSeen")
        if seen and now - datetime.fromisoformat(seen) < timedelta(hours=KEEP_HOURS): pool[s["id"]] = s
except Exception as e: print("no previous list:", e)
fresh = 0
for i in range(FETCHES):
    try: p = parse(fetch()); fresh += len(p); pool.update(p); print(f"fetch {i+1}: {len(p)}")
    except Exception as e: print(f"fetch {i+1} failed: {e}")
    if i < FETCHES - 1: time.sleep(20)
if fresh == 0: sys.exit("VPN Gate API returned nothing; keeping old list")

def alive(s):
    port = 443 if s["proto"].startswith("udp") else s["port"]
    try:
        with socket.create_connection((s["host"], port), timeout=4): return True
    except Exception:
        try:
            with socket.create_connection((s["host"], s["port"]), timeout=4): return s["proto"].startswith("tcp")
        except Exception: return False
json.dump(dict(updated=now.isoformat(), servers=list(pool.values())), open(POOL, "w"), separators=(",", ":"))
recent = (now - timedelta(hours=6)).isoformat()
# reachable from the runner, or listed by VPN Gate in the last 6h (runner networks block some relays)
with ThreadPoolExecutor(64) as ex: ok = [s for s, a in zip(pool.values(), ex.map(alive, pool.values())) if a or s["lastSeen"] >= recent]
print(f"pool {len(pool)}, reachable {len(ok)}")

by = {}
for s in ok: by.setdefault(s["countryCode"], []).append(s)
servers = []
for cc, lst in by.items():
    lst.sort(key=lambda s: (s["lastSeen"] >= now.isoformat()[:13], s["score"] + s["speedMbps"] * 1e4 + (2e5 if s["proto"] == "udp" else 0)), reverse=True)
    servers += lst[: BIG_COUNTRY if len(lst) > 15 else PER_COUNTRY]
servers.sort(key=lambda s: (s["country"], -s["score"]))
countries = len({s["countryCode"] for s in servers})
print(f"publishing {len(servers)} servers in {countries} countries")
if len(servers) < MIN_SERVERS or countries < MIN_COUNTRIES: sys.exit("sanity check failed; not publishing")
json.dump(dict(version=1, updated=now.isoformat().replace("+00:00", "Z"), source="VPN Gate Academic Experiment (vpngate.net)",
               countries=countries, servers=servers), open(OUT, "w"), separators=(",", ":"))
