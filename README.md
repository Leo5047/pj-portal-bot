# pj-portal-bot (fork)

Fork of [madrhr/pj-portal-bot](https://github.com/madrhr/pj-portal-bot) with a
rewritten parser and batch support across all hospitals on your Merkliste.

## What’s different from upstream

- **Robust parser** — substring-based CSS class matching. No more silent
  `0/0` fallback when pj-portal.de tweaks a class name.
- **Whole-Merkliste scan** — one container monitors every hospital you
  watchlist at once. Put everything you care about for your subject on
  the Merkliste at pj-portal.de; the bot filters by `pj_tag`.
- **State diff** — pushes only on true transitions `0 → >0`. No more
  repeated alerts while a slot stays open.
- **Batched ntfy push** — one notification per run listing all new
  openings, with a click-through link back to pj-portal.de.
- **Single long-running process** — infinite loop with randomised sleep.
  No Docker restart dance needed.
- **Raw dump on parse failure** — if the parser ever returns an empty
  result or crashes, the raw HTML lands in `/data/last_raw.html` so you
  can adjust the selector in minutes.
- Minor: `logging.warnign` typo fixed, Python 3.12 base image, pinned
  deps.

## Setup

### 1. Prepare your Merkliste at pj-portal.de

Log in manually and add every hospital you want to monitor to your
Merkliste. The bot only sees what’s on that watchlist.

### 2. Extract your `ajax_uid` once

1. On pj-portal.de, open DevTools → Network tab (F12).
1. Click “PJ Angebot”, then hit “Merkliste aktualisieren”.
1. Find the `ajax.php` request → Payload tab → copy the `AJAX_ID`
   (7-digit number).

### 3. Set up ntfy

You already have ntfy running on your Pi. Pick a topic, e.g.
`https://ntfy.your-pi.local/pj-portal`, and subscribe to it in the
ntfy iOS app.

### 4. Run it

```bash
git clone https://github.com/<you>/pj-portal-bot.git
cd pj-portal-bot
cp .env.example .env
# edit .env

docker compose up -d --build
docker compose logs -f
```

## Environment variables

|Name                         |Required|Default                 |Notes                                   |
|-----------------------------|--------|------------------------|----------------------------------------|
|`pjportal_user`              |yes     |—                       |your pj-portal.de email                 |
|`pjportal_pwd`               |yes     |—                       |your pj-portal.de password              |
|`ajax_uid`                   |yes     |—                       |see step 2                              |
|`ntfy_url_topic`             |yes     |—                       |full URL incl. topic                    |
|`pj_tag`                     |no      |`Innere Medizin`        |exact Fach name as shown on pj-portal.de|
|`check_frequency_lower_limit`|no      |`180`                   |seconds                                 |
|`check_frequency_upper_limit`|no      |`420`                   |seconds                                 |
|`ntfy_click_url`             |no      |pj-portal PJ Angebot URL|what opens when you tap the notification|
|`cookie_filepath`            |no      |`/data/cookie.txt`      |                                        |
|`state_filepath`             |no      |`/data/state.json`      |                                        |
|`raw_dump_path`              |no      |`/data/last_raw.html`   |                                        |

## License

MIT (inherited from upstream).
