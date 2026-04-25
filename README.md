# pj-portal-bot (fork)

Fork of [madrhr/pj-portal-bot](https://github.com/madrhr/pj-portal-bot) with a
rewritten parser and batch support across all hospitals on your Merkliste.

## What’s different from upstream

- **Pure-Python parser** — BeautifulSoup with the built-in `html.parser`,
  no `lxml`, no C compiler needed. Builds in seconds on a Raspberry Pi.
- **Robust class matching** — substring-based, so the bot survives CSS
  tweaks on pj-portal.de instead of silently returning `0/0`.
- **Whole-Merkliste scan** — one container monitors every hospital on
  your Merkliste; filter by `pj_tag` (default: Innere Medizin).
- **State diff** — pushes only on real `0 → >0` transitions per
  (hospital, term). No spam while a slot stays open.
- **Batched ntfy push with token auth** — one notification per run with
  click-through to pj-portal.de; supports Bearer-token auth.
- **Single long-running process** — internal loop with randomised sleep.
- **Raw dump on parse failure** — empty/broken parse drops the raw HTML
  to `/data/last_raw.html` for quick selector fixes.
- Minor: `logging.warnign` typo fixed, named volume, pinned deps.

## Setup

### 1. Prepare your Merkliste at pj-portal.de

Log in manually and add every hospital you want to monitor to your
Merkliste. The bot only sees what’s on that list.

### 2. Extract your `ajax_uid` (one-time)

1. On pj-portal.de open DevTools → Network tab (F12).
1. Click “PJ Angebot”, then “Merkliste aktualisieren”.
1. Find the `ajax.php` request → Payload → copy the `AJAX_ID`.

### 3. Set up ntfy

You already have ntfy on your Pi. Pick a topic name and subscribe to it
in the iOS app. If your server requires auth, generate a Bearer token
in the ntfy web UI (Account → Access Tokens).

### 4. Run it

#### Option A: Local docker compose

```bash
git clone https://github.com/<you>/pj-portal-bot.git
cd pj-portal-bot
cp .env.example .env
# edit .env

docker compose up -d --build
docker compose logs -f
```

#### Option B: Portainer Stack from Git

- Stacks → Add stack → Repository
- Repository URL: `https://github.com/<you>/pj-portal-bot`
- Compose path: `docker-compose.yml`
- Environment variables: set `PJPORTAL_USER`, `PJPORTAL_PWD`, `AJAX_UID`,
  `NTFY_URL_TOPIC`, `NTFY_TOKEN` (and any optional overrides)
- Deploy

The compose file uses a Docker named volume (`pjportalbot_data`), so no
host paths leak into the public repo and data still persists across
restarts.

## Environment variables

|Name                         |Required|Default                 |Notes                                 |
|-----------------------------|--------|------------------------|--------------------------------------|
|`pjportal_user`              |yes     |—                       |your pj-portal.de email               |
|`pjportal_pwd`               |yes     |—                       |your pj-portal.de password            |
|`ajax_uid`                   |yes     |—                       |see step 2                            |
|`ntfy_url_topic`             |yes     |—                       |full URL incl. topic                  |
|`ntfy_token`                 |no      |—                       |Bearer token for protected ntfy topics|
|`pj_tag`                     |no      |`Innere Medizin`        |exact Fach name on pj-portal.de       |
|`check_frequency_lower_limit`|no      |`180`                   |seconds                               |
|`check_frequency_upper_limit`|no      |`420`                   |seconds                               |
|`ntfy_click_url`             |no      |pj-portal PJ Angebot URL|URL opened on tap                     |
|`cookie_filepath`            |no      |`/data/cookie.txt`      |                                      |
|`state_filepath`             |no      |`/data/state.json`      |                                      |
|`raw_dump_path`              |no      |`/data/last_raw.html`   |                                      |

## Inspecting the data volume

```bash
docker run --rm -it -v pjportalbot_data:/data alpine ls -la /data
docker run --rm -it -v pjportalbot_data:/data alpine cat /data/state.json
```

## License

MIT (inherited from upstream).
