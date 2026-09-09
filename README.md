# About
This is a simple bash script to download galleries from nhentai, \
all the downloaded galleries will be stored at ~/nh/{the six digits code} folder

# Why do I need this instead of their BitTorrent Download?
We all knew, P2P is slow and vulnerable to Man-in-the-middle attack, \
You can get better download speed using our script since all it does is 
1. fetch the gallery html file
2. grep the image links
3. parallel download them via https requests

thus provides a faster and safer download experience, all you need to do is provide your cookie and \
browser agent version in the script

Current architecture, routes, metadata indexing, deployment state, and new-session
handoff notes are maintained in [`project.md`](project.md).
YAML configuration, Docker server deployment, and SQLite-only library restoration
are documented in [`doc/docker_server.md`](doc/docker_server.md).

# Dependencies
* aria2
* wget
* bash
* procps (for alpine linux)
* python3 (for the HTTP server)

# Cookie config

Copy `cookie.example.sh` to `cookie.sh`, then fill in the cookie and user agent from
your browser. `cookie.sh` is ignored by git because it contains local secrets.

```bash
cp cookie.example.sh cookie.sh
chmod 600 cookie.sh
```

# HTTP server

The server listens on port `8765`, accepts requests only from
`192.168.50.0/24`, `192.168.193.0/24`, and localhost, then queues downloads in
the background. It also starts a local browsing server on port `8766` by
default.

```bash
python3 server/nh_server.py
```

Queue a download:

```bash
curl -X POST http://127.0.0.1:8765/api/download \
  -H 'Content-Type: application/json' \
  -d '{"id":"123456"}'
```

The downloader stores the final archive at `~/nh/123456.cbz`. The raw
`~/nh/123456/` folder is removed only after the archive is created successfully.

Inspect the current queue:

```bash
curl http://127.0.0.1:8765/api/queue
```

Delete a downloaded gallery and its local browsing cache:

```bash
curl -X DELETE http://127.0.0.1:8765/api/galleries/123456
```

The local browsing server is available at:

```text
http://127.0.0.1:8766/
```

It mirrors nhentai's public home, search, taxonomy, gallery, reader, random,
info, and community pages while keeping navigation on the local server. Ads,
tracking, popups, and account-only routes are removed. Gallery cards and detail
pages get local download status plus download/delete controls. Thumbnails keep
their original CDN URLs; full reader images use local `{id}.cbz` archives when
available and otherwise use the temporary preview cache. Downloaded galleries use a fully local
reader: the CBZ is extracted once, a persistent image index avoids rescanning
the directory on every request, and the next page image is preloaded. Reader
navigation never fetches per-page HTML from nhentai.

`/downloads/` lists downloaded galleries newest-first with 50 galleries per
page, and `/downloads/random/` chooses five different downloaded galleries on
every request. `/downloads/search/` searches downloaded English, Japanese, and
Pretty titles plus tag, artist, character, parody, group, language, and category
names and slugs. `/downloads/{type}/{slug}/` browses those local classifications.
All cards open `/g/{id}/`: downloaded galleries use a local detail page,
and undownloaded galleries use the upstream proxy page. Old `/downloads/g/{id}/`
and `/downloads/g/{id}/{page}/` links redirect to their `/g/` equivalents.
Undownloaded galleries can also be read through the same local reader:
gallery metadata is fetched once and full-size pages are proxied into a
temporary 24-hour, 2 GiB LRU cache without creating a CBZ or marking the gallery
as downloaded.

Local detail pages show nhentai CDN content thumbnails that link to the local
CBZ reader. Their taxonomy links default to the local database and display
local downloaded-gallery counts; undownloaded detail pages continue to default to
nhentai taxonomy links. Each mode is remembered separately per browser tab.
Both local and temporary readers return to `/g/{id}/` after the last page,
including Next, clicks on the image's right half, and the right arrow key. On
page 1, Prev, clicks on the image's left half, and the left arrow key return
to the gallery. Deleting from a detail
page reloads that same gallery as an undownloaded page.

The seven taxonomy result pages and search results have a **Show all / Show
downloaded** scope switch. Switching keeps the category or search text and
resets pagination and sorting. An uncollected local category displays an empty
list with a switch back to the upstream results.

Download lists, search results, and taxonomy results offer **ID ↓** (numeric)
and **Downloaded ↓** sorting. `/downloads/` defaults to download time, all other
lists default to ID; equal timestamps use descending ID. The `sort=id` or
`sort=downloaded` query parameter persists through pagination, with no browser
preference storage. Changing sorting returns to page 1. Random 5 is unchanged.

The downloaded library also has seven classification directories at
`/downloads/tags/`, `artists/`, `characters/`, `parodies/`, `groups/`,
`languages/`, and `categories/`. They show local gallery counts, name/slug
search, count or name sorting, and 100 classifications per page. Library
navigation links to these local directories; **Show all / Show downloaded**
switches between the corresponding directories.

Gallery cards display full titles with equal heights within each row, including
after upstream hydration. Chinese language flags use 🇹🇼, English 🇬🇧, and
Japanese 🇯🇵. Downloaded cards derive their language prefixes from SQLite;
multiple known languages are shown together. Downloaded cards and details show
**Delete** without an additional green Downloaded label. The Firefox extension
retains its existing UI.

Both downloaded and temporary readers share a compact dark toolbar with
**Fit page** and **Original size** options. Fit page shows the entire image
within the remaining viewport without enlarging small images. Original size
allows scrolling at the image's natural dimensions. The last choice is saved
in localStorage per deployment path and shared across books and browser tabs.
Click the left half of the image to go back, or the right half to go forward;
the split follows the image even when scrolling in Original size. A numeric
page input immediately after the page counter accepts a page from 1 to the
total; press Enter or Go to jump. Card titles retain their original fonts.

Local search uses **OR** between keywords and can match different metadata
fields; double quotes preserve a complete phrase. Matching normalizes case,
full-width characters, whitespace and hyphens and supports partial strings.
Every search also includes close Latin spellings: words of 4–7 letters allow
one insertion, deletion, substitution or adjacent transposition, and words of
8+ letters allow two. Short words and Chinese/Japanese use substring matching
and explicit aliases. Results follow the selected sort, not relevance scores.

For cross-language names, copy [`search-aliases.example.yaml`](search-aliases.example.yaml)
to `<storage>/.nh-local/search-aliases.yaml` (normally `~/nh/.nh-local/search-aliases.yaml`),
fill in groups of interchangeable names, and restart the server. The default
alias table is empty; no translations are guessed. An optional
`NH_SEARCH_ALIASES_FILE` environment variable selects another file. The file in
the storage directory also works with the existing Docker storage mount.

Gallery cards across both scopes display a purple **Decensored** label at the
bottom left of the cover when their available titles contain `decensored`,
`uncensored`, `無碼`, `无码`, `無修正`, or `モザイクなし` (case insensitive).
This does not use tags and does not label detail covers or individual reader pages.

Downloaded metadata is indexed in `~/nh/.nh-local/library.sqlite3`. Existing
archives are indexed in the background without delaying server startup; CBZ
metadata is preferred and only incomplete records are repaired from upstream,
at no more than one request per second. Rebuild the index manually with
`python3 server/nh_server.py --reindex-library`. On first startup after upgrading,
the search documents and term index are built from existing SQLite metadata;
this can add startup time for a large library but does not read or alter CBZ files.

Cached HTML, read-only API responses, metadata, and extracted CBZ files live
under `~/nh/.nh-local/`. HTML is fresh for 15 minutes, public API JSON is
fresh for 60 seconds, stale responses are used if upstream is unavailable,
and entries unused for seven days are removed. HTML/metadata are capped at
512 MiB and extracted images use a 5 GiB LRU cache. The original CBZ is never
removed by extraction or cache cleanup. The limits can be changed with:

```text
NH_HTML_CACHE_TTL_SECONDS
NH_API_CACHE_TTL_SECONDS
NH_HTML_CACHE_MAX_AGE_SECONDS
NH_HTML_CACHE_MAX_BYTES
NH_EXTRACT_CACHE_MAX_BYTES
NH_PREVIEW_CACHE_MAX_AGE_SECONDS
NH_PREVIEW_CACHE_MAX_BYTES
NH_CACHE_SWEEP_INTERVAL_SECONDS
```

Equivalent command-line flags are shown by `python3 server/nh_server.py --help`.
The library UI uses same-origin endpoints under `/_nh-local/api/`; the existing
port 8765 API remains available to the Firefox extension and command-line
clients. Browser origins other than Firefox extension origins are rejected on
port 8765.

nhentai canonical redirects are preserved locally, so routes such as search,
tags, taxonomy, and random resolve to their canonical local URL. Only the
anonymous read-only `/api/v2/` endpoints required by those pages are proxied;
account, favorite, comment, voting, moderation, and other write operations are
not exposed.

Run the deterministic browser test with Playwright-managed Chromium:

```bash
deno run -A npm:playwright@1.62.1 install chromium
deno task e2e
```

With the local service running, the opt-in live smoke test is:

```bash
NH_RUN_LIVE_E2E=1 deno task e2e:live
```

To install the systemd service:

```bash
sudo cp systemd/nh-downloader.service /etc/systemd/system/nh-downloader.service
sudo systemctl daemon-reload
sudo systemctl enable --now nh-downloader.service
```

# Firefox extension

Load `firefox-extension/` temporarily from `about:debugging`. On
`https://nhentai.net/g/<id>/`, the extension injects a lower-right button that
queues the current gallery through `192.168.50.144:8765`, falling back to
`192.168.193.144:8765`. It also shows the current download queue on nhentai
pages and refreshes it once per second. Downloaded galleries get a delete
button; deletion opens an in-page confirmation dialog showing the gallery ID and
title before calling the local server.

# Using Docker

The gallery server has a separate `server.Dockerfile` and `compose.yaml`. Its
real `config.yaml` is mounted read-only and is never baked into the image. See
[`doc/docker_server.md`](doc/docker_server.md) for setup and SQLite-only library
restoration.

The original downloader-only image remains available:

For Windows users or who just want to use docker, simply build with Dockerfile we provided 
1. `docker build -t nh-project .`
2. `docker run --rm -v "${HOME}/nh:/root/nh" nh-project` (run `bash download.sh nhentai.txt` in docker)
3. (optional) `docker run --rm -it -v "${HOME}/nh:/root/nh" nh-project bash` (run interactively with our scripts)

## Disclaimer / 聲明

### English

This project is intended **solely for educational and research purposes**.  
We do not encourage, promote, or endorse any activity that violates copyright laws, licensing terms, or applicable regulations, including but not limited to piracy or unauthorized distribution of copyrighted material.  

By using this project, you agree to the following conditions:  
1. **Compliance with Laws** – You are solely responsible for ensuring that your use of this project complies with all laws and regulations in your jurisdiction.  
2. **Temporary Storage** – Any files obtained through this project must be **permanently deleted within 24 hours** of download.  
3. **Prohibition of Commercial Use** – Downloaded content must **not** be used for illegal, commercial, or profit-generating purposes.  
4. **No Liability** – The contributors of this project assume **no liability** for any misuse, damage, or legal consequences resulting from the use of this project.  

By continuing to use this project, you acknowledge and agree to the above terms in full.  

### 中文

本專案**僅供教育與研究用途**。  
我們不鼓勵、宣傳或支持任何違反著作權法、授權條款或相關法規以及侵犯他人智慧財產權之任何法律行為，包括但不限於盜版、未經授權散佈受著作權法保護之內容。 

使用本專案即表示您同意以下條款：  
1. **遵守法律** – 您必須自行確保使用本專案的行為符合您所在司法管轄區的所有法律與規定。  
2. **臨時儲存** – 透過本專案下載的任何檔案，必須在下載後 **24 小時內永久刪除**。  
3. **禁止商業用途** – 所下載的內容**不得**用於任何非法、商業或營利目的。  
4. **免責聲明** – 本專案貢獻者對於任何因使用本專案而導致的濫用、損害或法律後果，**不承擔任何責任**。  

繼續使用本專案即表示您已完整理解並同意上述所有條款。
