#!/usr/bin/env python3
“””
pj-portal-bot: Monitor pj-portal.de Merkliste for newly free slots and push
a notification to ntfy.

Refactored fork of madrhr/pj-portal-bot:

- Pure-Python parser (BeautifulSoup + html.parser, no lxml/no C compile).
- Substring-based CSS class matching (no silent “0/0” fallback when the
  portal tweaks its class names).
- Iterates the ENTIRE Merkliste and filters by `pj_tag` (default:
  “Innere Medizin”), so one container covers all hospitals you put on
  your Merkliste at pj-portal.de.
- State persistence in /data/state.json: pushes only on true transitions
  0 -> >0 per (hospital, term). No more spam while slots stay open.
- One ntfy push per run, batching all new openings.
- ntfy bearer-token auth via optional `ntfy_token` env.
- Long-running: infinite loop with randomised sleep between checks.
- Parse failures dump the raw HTML to /data/last_raw.html so you can
  fix the selector in minutes instead of guessing.
  “””

from **future** import annotations

import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

# —————————————————————————

# Config

# —————————————————————————

logging.basicConfig(
format=”%(asctime)s - %(levelname)s - %(message)s”,
level=logging.INFO,
datefmt=”%Y-%m-%d %H:%M:%S%z”,
)
log = logging.getLogger(“pjportal”)

BASE = “https://www.pj-portal.de”
TERMS = (“first_term”, “second_term”, “third_term”)
TERM_LABELS = {
“first_term”: “1. Tertial”,
“second_term”: “2. Tertial”,
“third_term”: “3. Tertial”,
}

REQUIRED_ENV = [“pjportal_user”, “pjportal_pwd”, “ajax_uid”, “ntfy_url_topic”]

def _env(name: str, default: Optional[str] = None) -> Optional[str]:
val = os.getenv(name)
return val if val not in (None, “”) else default

def load_config() -> dict:
missing = [v for v in REQUIRED_ENV if not os.getenv(v)]
if missing:
raise SystemExit(f”Missing required ENV variables: {’, ’.join(missing)}”)

```
cfg = {
    "user": os.environ["pjportal_user"],
    "pwd": os.environ["pjportal_pwd"],
    "ajax_uid": os.environ["ajax_uid"],
    "ntfy_url_topic": os.environ["ntfy_url_topic"],
    "ntfy_token": _env("ntfy_token"),
    "pj_tag": _env("pj_tag", "Innere Medizin"),
    "cookie_filepath": _env("cookie_filepath", "/data/cookie.txt"),
    "state_filepath": _env("state_filepath", "/data/state.json"),
    "raw_dump_path": _env("raw_dump_path", "/data/last_raw.html"),
    "check_lower": int(_env("check_frequency_lower_limit", "180")),
    "check_upper": int(_env("check_frequency_upper_limit", "420")),
    "ntfy_click_url": _env("ntfy_click_url", f"{BASE}/index_uu.php?PAGE_ID=101"),
}
Path(cfg["state_filepath"]).parent.mkdir(parents=True, exist_ok=True)
log.info(
    f"Watching pj_tag={cfg['pj_tag']!r}, "
    f"interval={cfg['check_lower']}..{cfg['check_upper']}s, "
    f"ntfy_auth={'token' if cfg['ntfy_token'] else 'none'}"
)
return cfg
```

# —————————————————————————

# Cookie + session helpers

# —————————————————————————

def _load_cookie(cfg) -> Optional[str]:
p = Path(cfg[“cookie_filepath”])
if p.exists():
val = p.read_text().strip()
return val or None
return None

def _save_cookie(cfg, value: str) -> None:
p = Path(cfg[“cookie_filepath”])
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(value)

def new_session() -> requests.Session:
s = requests.Session()
s.headers.update({
“User-Agent”: (
“Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 “
“(KHTML, like Gecko) Chrome/124.0 Safari/537.36”
),
“Accept-Language”: “de-DE,de;q=0.9,en;q=0.7”,
“Accept-Encoding”: “gzip, deflate, br”,
})
return s

def authenticate(session: requests.Session, cfg) -> str:
log.info(“Authenticating at pj-portal.de …”)
session.get(f”{BASE}/”)  # prime PHPSESSID
session.headers.update({
“Origin”: BASE,
“Referer”: f”{BASE}/index_uu.php”,
})
resp = session.post(
f”{BASE}/index_uu.php”,
data={
“name_Login”: “Login”,
“USER_NAME”: cfg[“user”],
“PASSWORT”: cfg[“pwd”],
“form_login_submit”: “anmelden”,
},
)
resp.raise_for_status()
cookie = session.cookies.get(“PHPSESSID”)
if not cookie:
raise RuntimeError(“Login failed - no PHPSESSID cookie received”)
_save_cookie(cfg, cookie)
log.info(“Authentication successful”)
return cookie

def fetch_merkliste(session: requests.Session, cfg) -> str:
“”“Return the Merkliste HTML table, re-authenticating once if needed.”””
headers = {
“Accept”: “application/json, text/javascript, */*; q=0.01”,
“Origin”: BASE,
“Referer”: f”{BASE}/index_uu.php?PAGE_ID=101”,
“Content-Type”: “application/x-www-form-urlencoded; charset=UTF-8”,
“X-Requested-With”: “XMLHttpRequest”,
}
payload = {“AJAX_ID”: cfg[“ajax_uid”], “TAB_ID”: “Tab_Merkliste”}

```
cached = _load_cookie(cfg)
if cached:
    session.cookies.set("PHPSESSID", cached)

for attempt in (1, 2):
    resp = session.post(f"{BASE}/ajax.php", data=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"ajax.php HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"ajax.php did not return JSON: {resp.text[:200]!r}")

    htmltable = data.get("HTML", "") or ""
    err = data.get("ERRORCLASS")
    if "Antwort kein Handler" in htmltable or err == 2 or not htmltable:
        if attempt == 1:
            log.info("Session invalid - re-authenticating")
            session.cookies.clear()
            authenticate(session, cfg)
            continue
        raise RuntimeError(f"ajax.php rejected after re-auth (ERRORCLASS={err})")

    return htmltable

raise RuntimeError("ajax.php unreachable")
```

# —————————————————————————

# Parser - robust against CSS / whitespace changes

# —————————————————————————

SLOT_RE = re.compile(r”(\d+)\s*/\s*(\d+)”)

# Nested dict: {fach: {hospital: {term_key: (free, total)}}}

Parsed = Dict[str, Dict[str, Dict[str, Tuple[int, int]]]]

def _class_str(el) -> str:
“”“Return the element’s class attribute as a single string for substring matching.”””
cls = el.get(“class”)
if cls is None:
return “”
if isinstance(cls, list):
return “ “.join(cls)
return str(cls)

def parse_merkliste(htmltable: str) -> Parsed:
soup = BeautifulSoup(htmltable, “html.parser”)
result: Parsed = {}
current_fach: Optional[str] = None

```
for row in soup.find_all("tr"):
    cls = _class_str(row)

    # Fach header row (e.g. "Innere Medizin")
    if "pj_info_fach" in cls:
        texts = [t.strip() for t in row.stripped_strings if t.strip()]
        if texts:
            # Fach name is usually the only/longest non-empty text in the row
            current_fach = max(texts, key=len)
            result.setdefault(current_fach, {})
        continue

    # Hospital row with three tertial cells
    if "merkliste_krankenhaus" in cls and current_fach is not None:
        hospital = ""
        terms: Dict[str, Tuple[int, int]] = {}
        term_idx = 0

        for td in row.find_all("td", recursive=False):
            td_cls = _class_str(td)

            if "bezeichnung_krankenhaus" in td_cls:
                texts = [t.strip() for t in td.stripped_strings if t.strip()]
                # Hospital name is usually the longest text in the cell
                # (ignores single-character icons, short labels etc.)
                hospital = max(texts, key=len) if texts else ""

            elif "tertial_verfuegbarkeit" in td_cls:
                if term_idx >= len(TERMS):
                    continue
                joined = " ".join(td.stripped_strings)
                m = SLOT_RE.search(joined)
                terms[TERMS[term_idx]] = (
                    (int(m.group(1)), int(m.group(2))) if m else (0, 0)
                )
                term_idx += 1

        if hospital:
            result[current_fach][hospital] = terms

return result
```

# —————————————————————————

# State diff + notifications

# —————————————————————————

def load_state(cfg) -> Dict[str, int]:
p = Path(cfg[“state_filepath”])
if not p.exists():
return {}
try:
return json.loads(p.read_text())
except Exception:
log.warning(“State file unreadable - starting fresh”)
return {}

def save_state(cfg, state: Dict[str, int]) -> None:
p = Path(cfg[“state_filepath”])
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(state, ensure_ascii=False, indent=2))

def diff_openings(
parsed: Parsed,
pj_tag: str,
prev_state: Dict[str, int],
) -> Tuple[List[tuple], Dict[str, int]]:
“””
Compare new parse against previous state.

```
Returns:
    openings: list of (hospital, term_key, free, total) tuples for new
              0->>0 transitions
    new_state: complete snapshot of free counts to persist
"""
new_state: Dict[str, int] = {}
openings: List[tuple] = []

for hospital, terms in parsed.get(pj_tag, {}).items():
    for term_key, (free, total) in terms.items():
        k = f"{pj_tag}|{hospital}|{term_key}"
        new_state[k] = free
        if free > 0 and prev_state.get(k, 0) == 0:
            openings.append((hospital, term_key, free, total))

return openings, new_state
```

def notify(cfg, openings: List[tuple]) -> None:
if not openings:
return
lines = [
f”{h} — {TERM_LABELS.get(t, t)}: {free}/{total}”
for (h, t, free, total) in openings
]
body = “\n”.join(lines)
title = f”PJ-Portal: {len(openings)} freie Plätze ({cfg[‘pj_tag’]})”

```
headers = {
    "Title": title,
    "Priority": "high",
    "Tags": "hospital",
    "Click": cfg["ntfy_click_url"],
}
if cfg.get("ntfy_token"):
    headers["Authorization"] = f"Bearer {cfg['ntfy_token']}"

try:
    resp = requests.post(
        cfg["ntfy_url_topic"],
        data=body.encode("utf-8"),
        headers=headers,
        timeout=15,
    )
    if resp.status_code >= 300:
        log.warning(f"ntfy push failed: {resp.status_code} {resp.text[:200]}")
    else:
        log.info(f"Pushed {len(openings)} openings via ntfy")
except requests.RequestException as e:
    log.warning(f"ntfy unreachable: {e}")
```

# —————————————————————————

# Main loop

# —————————————————————————

def _dump_raw(cfg, htmltable: str) -> None:
try:
Path(cfg[“raw_dump_path”]).parent.mkdir(parents=True, exist_ok=True)
Path(cfg[“raw_dump_path”]).write_text(htmltable)
log.warning(f”Raw response dumped to {cfg[‘raw_dump_path’]}”)
except Exception as e:
log.warning(f”Could not dump raw response: {e}”)

def run_once(cfg) -> None:
session = new_session()

```
try:
    htmltable = fetch_merkliste(session, cfg)
except Exception as e:
    log.error(f"Fetch failed: {e}")
    return

try:
    parsed = parse_merkliste(htmltable)
except Exception as e:
    log.error(f"Parser crashed: {e}")
    _dump_raw(cfg, htmltable)
    return

if not parsed:
    log.warning("Parser returned an empty result")
    _dump_raw(cfg, htmltable)
    return

total_entries = sum(len(h) for h in parsed.values())
log.info(f"Parsed {len(parsed)} Fächer / {total_entries} hospital entries")

if cfg["pj_tag"] not in parsed:
    log.warning(
        f"pj_tag {cfg['pj_tag']!r} not found in Merkliste. "
        f"Available: {list(parsed.keys())}. "
        f"Put matching entries on your Merkliste at pj-portal.de."
    )
    return

prev = load_state(cfg)
openings, new_state = diff_openings(parsed, cfg["pj_tag"], prev)

if openings:
    log.info(f"New openings: {openings}")
    notify(cfg, openings)
else:
    log.info("No new openings")

save_state(cfg, new_state)
```

def main() -> None:
cfg = load_config()
log.info(“pj-portal-bot started”)
while True:
try:
run_once(cfg)
except Exception as e:
log.exception(f”Unexpected error in run loop: {e}”)
sleep_for = random.randint(cfg[“check_lower”], cfg[“check_upper”])
log.info(f”Sleeping {sleep_for}s”)
time.sleep(sleep_for)

if **name** == “**main**”:
try:
main()
except KeyboardInterrupt:
log.info(“Stopped by user”)
sys.exit(0)
