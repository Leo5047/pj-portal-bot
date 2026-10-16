#!/usr/bin/env python3
"""
pj-portal-bot: Monitor the pj-portal.de Merkliste for newly free slots and
push a notification to ntfy.

Refactored fork of madrhr/pj-portal-bot:

  - Login reads the login form from the portal on every login (field names,
    hidden fields, form action) instead of hardcoding them, and verifies that
    the login really succeeded.
  - Pure-Python parser (BeautifulSoup + html.parser), robust against the
    portal's invalid HTML.
  - Scans the whole Merkliste and filters by `pj_tag` (default:
    "Innere Medizin").
  - State in /data/state.json: pushes only on real 0 -> >0 transitions per
    (hospital, tertial). If a push fails, the state is not saved, so the
    opening is pushed again on the next run.
  - Failure alerts: after `alert_after_failures` failed runs in a row, one
    ntfy warning (repeated daily while the problem persists) and a recovery
    message afterwards. Failed runs back off so the portal is not hammered.
  - Optional heartbeat URL (e.g. an Uptime Kuma push monitor).
  - `python pjportal.py --check` runs one diagnostic pass without touching
    the state or sending pushes; `--test-push` sends a test notification.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S%z",
)
log = logging.getLogger("pjportal")

BASE = "https://www.pj-portal.de"
HTTP_TIMEOUT = 30
ALERT_REPEAT_S = 24 * 3600
MAX_SLEEP_S = 3600

TERMS = ("first_term", "second_term", "third_term")
TERM_LABELS = {
    "first_term": "1. Tertial",
    "second_term": "2. Tertial",
    "third_term": "3. Tertial",
}

REQUIRED_ENV = ["pjportal_user", "pjportal_pwd", "ajax_uid", "ntfy_url_topic"]


class BotError(Exception):
    """A failed run with a human-readable reason (used for logs and alerts)."""


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    val = os.getenv(name)
    return val if val not in (None, "") else default


def _short(text, limit: int = 150) -> str:
    return " ".join(str(text).split())[:limit]


def _merkliste_page() -> str:
    return f"{BASE}/index_uu.php?PAGE_ID=101"


def load_config() -> dict:
    missing = [v for v in REQUIRED_ENV if not os.getenv(v)]
    if missing:
        raise SystemExit(f"Fehlende ENV-Variablen: {', '.join(missing)}")

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
        "ntfy_click_url": _env("ntfy_click_url", _merkliste_page()),
        "alert_after": max(1, int(_env("alert_after_failures", "3"))),
        "heartbeat_url": _env("heartbeat_url"),
    }
    Path(cfg["state_filepath"]).parent.mkdir(parents=True, exist_ok=True)
    log.info(
        f"Überwache pj_tag={cfg['pj_tag']!r}, "
        f"Intervall={cfg['check_lower']}..{cfg['check_upper']}s, "
        f"ntfy_auth={'token' if cfg['ntfy_token'] else 'none'}, "
        f"Warnung nach {cfg['alert_after']} Fehlversuchen, "
        f"Heartbeat={'an' if cfg['heartbeat_url'] else 'aus'}"
    )
    return cfg


# ---------------------------------------------------------------------------
# Session + login
# ---------------------------------------------------------------------------

def _load_cookie(cfg) -> Optional[str]:
    p = Path(cfg["cookie_filepath"])
    if p.exists():
        val = p.read_text().strip()
        return val or None
    return None


def _save_cookie(cfg, value: str) -> None:
    p = Path(cfg["cookie_filepath"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(value)


def _clear_cookie(cfg) -> None:
    try:
        Path(cfg["cookie_filepath"]).unlink()
    except FileNotFoundError:
        pass


def _session_cookie(session: requests.Session) -> Optional[str]:
    value = None
    for c in session.cookies:
        if c.name == "PHPSESSID":
            value = c.value
    return value


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
    })
    return s


def _password_input(soup):
    return (
        soup.find("input", attrs={"type": re.compile(r"^password$", re.I)})
        or soup.find("input", attrs={"name": re.compile(r"^passwort$", re.I)})
    )


def _page_message(soup) -> str:
    """Best-effort: the error/notice text the portal shows next to the form."""
    for el in soup.find_all(class_=re.compile(r"fehler|error|warn|meldung", re.I)):
        text = _short(el.get_text(" ", strip=True), 200)
        if len(text) >= 3:
            return text
    return ""


def _find_login_form(session: requests.Session):
    """
    Load a portal page that contains the login form.
    Returns (page_url, container) where container is the <form> element, or
    the whole page if the portal's broken HTML hides the form boundaries.
    """
    for url in (f"{BASE}/index_uu.php", f"{BASE}/"):
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        pw = _password_input(soup)
        if pw is not None:
            form = pw.find_parent("form")
            return resp.url, (form if form is not None else soup)
    raise BotError("Login-Formular nicht gefunden – hat sich die Portal-Seite geändert?")


def _build_login_payload(container, user: str, pwd: str) -> Tuple[Dict[str, str], List[str]]:
    """
    Mirror what a browser sends when submitting the login form: all named
    fields with their (hidden) default values, the first submit button,
    plus our username and password.
    """
    data: Dict[str, str] = {}
    names: List[str] = []
    user_field: Optional[str] = None
    pw_field: Optional[str] = None
    submit_added = False

    for el in container.find_all(["input", "button"]):
        name = el.get("name")
        if not name:
            continue
        names.append(name)
        default_type = "submit" if el.name == "button" else "text"
        typ = (el.get("type") or default_type).lower()

        if typ == "password" or name.lower() == "passwort":
            pw_field = name
        elif typ == "submit":
            if not submit_added:
                data[name] = el.get("value", "")
                submit_added = True
        elif typ in ("image", "button", "reset", "file"):
            continue
        elif typ in ("checkbox", "radio"):
            if el.has_attr("checked"):
                data[name] = el.get("value", "on")
        else:
            if typ in ("text", "email") and user_field is None:
                user_field = name
            data[name] = el.get("value", "")

    if "USER_NAME" in names:
        user_field = "USER_NAME"
    if pw_field is None or user_field is None:
        raise BotError(f"Login-Formular unvollständig erkannt (Felder: {names})")

    data[user_field] = user
    data[pw_field] = pwd
    return data, names


def authenticate(session: requests.Session, cfg) -> None:
    log.info("Login bei pj-portal.de ...")
    session.cookies.clear()
    page_url, container = _find_login_form(session)
    data, names = _build_login_payload(container, cfg["user"], cfg["pwd"])

    action = container.get("action") if container.name == "form" else None
    if action:
        post_url = urljoin(page_url, action)
    elif container.name == "form":
        post_url = page_url
    else:
        post_url = f"{BASE}/index_uu.php"

    resp = session.post(
        post_url,
        data=data,
        headers={"Origin": BASE, "Referer": page_url},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    logged_in = "abmelden" in resp.text.lower() or _password_input(soup) is None
    if not logged_in:
        _clear_cookie(cfg)
        hint = _page_message(soup)
        raise BotError(
            "Login fehlgeschlagen – das Portal zeigt wieder das Login-Formular"
            + (f" („{hint}“)" if hint else "")
            + f". Formularfelder: {names}. Zugangsdaten prüfen."
        )

    cookie = _session_cookie(session)
    if cookie:
        _save_cookie(cfg, cookie)
    log.info("Login OK")


# ---------------------------------------------------------------------------
# Merkliste
# ---------------------------------------------------------------------------

def _ajax_merkliste(session: requests.Session, cfg) -> Tuple[Optional[str], str]:
    """One ajax.php call. Returns (html, problem); html is None if refused."""
    resp = session.post(
        f"{BASE}/ajax.php",
        data={"AJAX_ID": cfg["ajax_uid"]},
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": BASE,
            "Referer": _merkliste_page(),
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        return None, f"ajax.php HTTP {resp.status_code}"
    try:
        data = resp.json()
    except ValueError:
        return None, f"ajax.php lieferte kein JSON: {_short(resp.text)}"

    html = data.get("HTML") or ""
    err = data.get("ERRORCLASS")
    if str(err) not in ("0", "None") or not html.strip() or "kein Handler" in html:
        return None, (
            f"ERRORCLASS={err}, AJAX_ID={data.get('AJAX_ID')}, "
            f"HTML={_short(html, 80)!r}"
        )
    return html, ""


def fetch_merkliste(session: requests.Session, cfg) -> str:
    """Return the Merkliste HTML, logging in again if the cached session is stale."""
    cached = _load_cookie(cfg)
    if cached:
        session.cookies.set("PHPSESSID", cached, domain=urlsplit(BASE).hostname)
        html, problem = _ajax_merkliste(session, cfg)
        if html is not None:
            return html
        log.info(f"Gespeicherte Sitzung ungültig ({problem}) – neuer Login")
        _clear_cookie(cfg)

    authenticate(session, cfg)
    html, problem = _ajax_merkliste(session, cfg)
    if html is None:
        raise BotError(
            f"Login OK, aber die Merkliste wird abgelehnt ({problem}). "
            f"Vermutlich hat sich die AJAX_ID geändert – "
            f"'python pjportal.py --check' zeigt Kandidaten."
        )
    return html


# ---------------------------------------------------------------------------
# Parser - robust against CSS / whitespace changes
# ---------------------------------------------------------------------------

SLOT_RE = re.compile(r"(\d+)\s*/\s*(\d+)")

# Nested dict: {fach: {hospital: {term_key: (free, total)}}}
Parsed = Dict[str, Dict[str, Dict[str, Tuple[int, int]]]]


def _class_str(el) -> str:
    """Return the element's class attribute as a single string for substring matching."""
    cls = el.get("class")
    if cls is None:
        return ""
    if isinstance(cls, list):
        return " ".join(cls)
    return str(cls)


def _extract_hospital_name(td) -> str:
    """
    The hospital cell looks like:
        <td class="pj_info_bezeichnung_krankenhaus">
          <span class="infobox_inhalt_kurz">
            Berlin Charité<br>Berlin Bundeswehrkrankenhaus
            <span class="infobox_inhalt_lang">
              Charité - Universitätsmedizin Berlin<br>...
            </span>
          </span>
        </td>
    We want the visible short label, with the long-form tooltip removed.
    """
    short = td.find(class_="infobox_inhalt_kurz")
    if short is None:
        return td.get_text(separator=" / ", strip=True)
    long_span = short.find(class_="infobox_inhalt_lang")
    if long_span is not None:
        long_span.extract()
    return short.get_text(separator=" / ", strip=True)


def parse_merkliste(htmltable: str) -> Parsed:
    soup = BeautifulSoup(htmltable, "html.parser")
    result: Parsed = {}
    current_fach: Optional[str] = None

    for row in soup.find_all("tr"):
        cls = _class_str(row)

        # ----- Fach header row: <tr class="merkliste pj_info_fach"> -----
        if "pj_info_fach" in cls:
            # Row has a few empty <td>s plus one with the fach name.
            for td in row.find_all("td", recursive=False):
                text = td.get_text(strip=True)
                if text and 2 <= len(text) <= 80:
                    current_fach = text
                    result.setdefault(current_fach, {})
                    break
            continue

        # ----- Hospital row with three tertial cells -----
        if "merkliste_krankenhaus" in cls:
            if current_fach is None:
                continue

            hospital = ""
            terms: Dict[str, Tuple[int, int]] = {}
            term_idx = 0

            # NOTE: pj-portal serves invalid HTML - some <td> elements miss
            # their closing tag (e.g. "tertial_aktion aktion_leer"), so the
            # html.parser nests subsequent cells inside the unclosed one.
            # find_all without recursive=False walks all <td> descendants
            # in document order, which is exactly what we want here.
            for td in row.find_all("td"):
                td_cls = _class_str(td)

                if "pj_info_bezeichnung_krankenhaus" in td_cls:
                    hospital = _extract_hospital_name(td)

                elif "tertial_verfuegbarkeit" in td_cls:
                    if term_idx >= len(TERMS):
                        continue
                    if "_beendet" in td_cls:
                        terms[TERMS[term_idx]] = (0, 0)
                    else:
                        joined = " ".join(td.stripped_strings)
                        m = SLOT_RE.search(joined)
                        terms[TERMS[term_idx]] = (
                            (int(m.group(1)), int(m.group(2))) if m else (0, 0)
                        )
                    term_idx += 1

            if hospital:
                result[current_fach][hospital] = terms

    return result


# ---------------------------------------------------------------------------
# State diff
# ---------------------------------------------------------------------------

def load_state(cfg) -> Dict[str, int]:
    p = Path(cfg["state_filepath"])
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        log.warning("State-Datei unlesbar – starte neu")
        return {}


def save_state(cfg, state: Dict[str, int]) -> None:
    p = Path(cfg["state_filepath"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def diff_openings(
    parsed: Parsed,
    pj_tag: str,
    prev_state: Dict[str, int],
) -> Tuple[List[tuple], Dict[str, int]]:
    """
    Compare a new parse against the previous state.

    Returns:
        openings: (hospital, term_key, free, total) for each new 0 -> >0 transition
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


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def _header_value(text: str) -> str:
    """HTTP headers are ASCII; ntfy decodes RFC 2047 for everything else."""
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


def send_ntfy(cfg, title: str, message: str, priority: str = "default",
              tags: str = "", click: Optional[str] = None) -> bool:
    headers = {"Title": _header_value(title), "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if click:
        headers["Click"] = click
    if cfg.get("ntfy_token"):
        headers["Authorization"] = f"Bearer {cfg['ntfy_token']}"
    try:
        resp = requests.post(
            cfg["ntfy_url_topic"],
            data=message.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
    except requests.RequestException as e:
        log.warning(f"ntfy nicht erreichbar: {e}")
        return False
    if resp.status_code >= 300:
        log.warning(f"ntfy-Push fehlgeschlagen: {resp.status_code} {_short(resp.text, 200)}")
        return False
    return True


def notify_openings(cfg, openings: List[tuple]) -> bool:
    lines = [
        f"{h} — {TERM_LABELS.get(t, t)}: {free}/{total}"
        for (h, t, free, total) in openings
    ]
    if len(openings) == 1:
        title = f"PJ-Portal: neuer freier Platz ({cfg['pj_tag']})"
    else:
        title = f"PJ-Portal: {len(openings)} neue freie Plätze ({cfg['pj_tag']})"
    ok = send_ntfy(cfg, title, "\n".join(lines), priority="high",
                   tags="hospital", click=cfg["ntfy_click_url"])
    if ok:
        log.info(f"{len(openings)} neue freie Plätze per ntfy gemeldet")
    return ok


def heartbeat(cfg, ok: bool, msg: str) -> None:
    """Optional push monitor (e.g. Uptime Kuma): up after good runs, down after failed ones."""
    url = cfg.get("heartbeat_url")
    if not url:
        return
    try:
        requests.get(
            url.split("?", 1)[0],
            params={"status": "up" if ok else "down", "msg": _short(msg, 200), "ping": ""},
            timeout=10,
        )
    except requests.RequestException as e:
        log.warning(f"Heartbeat nicht erreichbar: {e}")


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------

def _dump_raw(cfg, htmltable: str) -> None:
    try:
        Path(cfg["raw_dump_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(cfg["raw_dump_path"]).write_text(htmltable)
        log.warning(f"Rohdaten gespeichert in {cfg['raw_dump_path']}")
    except Exception as e:
        log.warning(f"Rohdaten konnten nicht gespeichert werden: {e}")


def _free_lines(parsed: Parsed, pj_tag: str) -> List[str]:
    return [
        f"{h} — {TERM_LABELS.get(t, t)}: {free}/{total}"
        for h, terms in parsed.get(pj_tag, {}).items()
        for t, (free, total) in terms.items()
        if free > 0
    ]


def run_once(cfg) -> str:
    """One check. Returns a status line; raises on failure."""
    session = new_session()
    htmltable = fetch_merkliste(session, cfg)

    try:
        parsed = parse_merkliste(htmltable)
    except Exception as e:
        _dump_raw(cfg, htmltable)
        raise BotError(f"Parser-Fehler: {e!r}")

    if not parsed:
        _dump_raw(cfg, htmltable)
        raise BotError("Merkliste geladen, aber nichts erkannt – Seitenaufbau geändert? "
                       f"Rohdaten in {cfg['raw_dump_path']}")
    if cfg["pj_tag"] not in parsed:
        raise BotError(f"Fach {cfg['pj_tag']!r} nicht in der Merkliste "
                       f"(gefunden: {', '.join(parsed)})")

    prev = load_state(cfg)
    openings, new_state = diff_openings(parsed, cfg["pj_tag"], prev)

    if openings:
        log.info(f"Neue freie Plätze: {openings}")
        if not notify_openings(cfg, openings):
            # Keep the old state so these openings are pushed again next run.
            raise BotError("Neue freie Plätze gefunden, aber der ntfy-Push ist fehlgeschlagen")

    save_state(cfg, new_state)
    return (
        f"{cfg['pj_tag']}: {len(parsed[cfg['pj_tag']])} Kliniken, "
        f"{len(_free_lines(parsed, cfg['pj_tag']))} Tertiale mit freien Plätzen, "
        f"davon neu: {len(openings)}"
    )


# ---------------------------------------------------------------------------
# Main loop with failure tracking
# ---------------------------------------------------------------------------

class Health:
    def __init__(self) -> None:
        self.failures = 0
        self.first_failure = 0.0
        self.last_alert = 0.0


def _reason(e: Exception) -> str:
    if isinstance(e, BotError):
        return str(e)
    if isinstance(e, requests.RequestException):
        return _short(f"HTTP-/Netzwerkfehler: {e}", 300)
    return _short(f"Unerwarteter Fehler: {e!r}", 300)


def _ago(ts: float) -> str:
    mins = max(1, int((time.time() - ts) / 60))
    return f"{mins} Min." if mins < 120 else f"{mins // 60} Std."


def tick(cfg, health: Health) -> int:
    """Run one check, handle alerts, return the number of seconds to sleep."""
    base_sleep = random.randint(cfg["check_lower"], cfg["check_upper"])
    try:
        status = run_once(cfg)
    except Exception as e:
        reason = _reason(e)
        health.failures += 1
        if health.failures == 1:
            health.first_failure = time.time()
        if isinstance(e, (BotError, requests.RequestException)):
            log.error(f"Lauf fehlgeschlagen ({health.failures}x in Folge): {reason}")
        else:
            log.exception(f"Lauf fehlgeschlagen ({health.failures}x in Folge): {reason}")
        heartbeat(cfg, False, reason)

        if (health.failures >= cfg["alert_after"]
                and time.time() - health.last_alert >= ALERT_REPEAT_S):
            body = (f"{reason}\n\n{health.failures} Fehlversuche in Folge "
                    f"seit {_ago(health.first_failure)}\n"
                    f"Details: docker logs pjportalbot")
            if send_ntfy(cfg, "PJ-Bot gestört", body, priority="high", tags="warning"):
                health.last_alert = time.time()
                log.info("Störungsmeldung per ntfy verschickt")

        # Back off on repeated failures so the portal is not hammered.
        factor = 2 ** min(health.failures - 1, 3)
        return min(base_sleep * factor, MAX_SLEEP_S)

    log.info(status)
    if health.last_alert:
        send_ntfy(cfg, "PJ-Bot läuft wieder",
                  f"Nach {health.failures} Fehlversuchen klappt die Abfrage wieder.\n{status}",
                  priority="low", tags="white_check_mark")
    health.failures, health.first_failure, health.last_alert = 0, 0.0, 0.0
    heartbeat(cfg, True, status)
    return base_sleep


def main() -> None:
    cfg = load_config()
    log.info("pj-portal-bot gestartet")
    health = Health()
    while True:
        sleep_for = tick(cfg, health)
        log.info(f"Nächster Lauf in {sleep_for}s")
        time.sleep(sleep_for)


# ---------------------------------------------------------------------------
# Diagnostics:  python pjportal.py --check [--test-push]
# ---------------------------------------------------------------------------

def check(cfg, test_push: bool) -> int:
    rc = 0
    print("== Login ==")
    session = new_session()
    try:
        page_url, container = _find_login_form(new_session())
        fields, _ = _build_login_payload(container, "<benutzer>", "<passwort>")
        action = container.get("action") if container.name == "form" else None
        print(f"Formular auf {page_url}, action={action!r}")
        print(f"Gesendete Felder: {fields}")
        authenticate(session, cfg)
        print("OK")
        page = session.get(_merkliste_page(), timeout=HTTP_TIMEOUT).text
        ids = sorted(set(re.findall(r"ajax_?id\W{0,6}(\d{4,9})", page, re.I)))
        print(f"AJAX_ID konfiguriert: {cfg['ajax_uid']} | auf der Seite gefunden: {ids or '-'}")

        print("== Merkliste ==")
        html, problem = _ajax_merkliste(session, cfg)
        if html is None:
            print(f"FEHLER: {problem}")
            rc = 1
        else:
            parsed = parse_merkliste(html)
            for fach, hospitals in parsed.items():
                print(f"{fach}: {len(hospitals)} Kliniken")
            if cfg["pj_tag"] not in parsed:
                print(f"FEHLER: Fach {cfg['pj_tag']!r} nicht gefunden")
                rc = 1
            else:
                print(f"== Aktuell freie Plätze ({cfg['pj_tag']}) ==")
                for line in _free_lines(parsed, cfg["pj_tag"]) or ["keine"]:
                    print(f"  {line}")
    except Exception as e:
        print(f"FEHLER: {_reason(e)}")
        rc = 1

    if test_push:
        ok = send_ntfy(cfg, "PJ-Bot Test: Plätze & Umlaute ✓",
                       "Testnachricht von pjportal.py --check", tags="test_tube")
        print(f"== Test-Push: {'OK' if ok else 'FEHLER'} ==")
        rc = rc or (0 if ok else 1)
    return rc


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if args & {"--check", "--test-push"}:
        cfg = load_config()
        if "--check" in args:
            sys.exit(check(cfg, "--test-push" in args))
        ok = send_ntfy(cfg, "PJ-Bot Test: Plätze & Umlaute ✓", "Testnachricht von pjportal.py")
        print("Test-Push:", "OK" if ok else "FEHLER")
        sys.exit(0 if ok else 1)
    try:
        main()
    except KeyboardInterrupt:
        log.info("Beendet")
        sys.exit(0)
