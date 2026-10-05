# External services: what we depend on, and what happens when one goes away

StreamLink runs on your own box, but several features lean on services other people
run. Any of them can change price, terms or limits, or shut down. This doc lists each
one, what breaks without it, and the backup plan.

**This is a planning document. Nothing described under "Backup plan" is built** unless
it says so. Provider facts were checked on **2026-10-04**; each is marked *(checked)*
with a source at the bottom, or *(unverified)* when it comes from memory and must be
re-checked before anyone builds on it. Prices and limits go stale: re-check before
acting.

Read this when a provider announces a change, when a feature that needs the internet
stops working for no local reason, or before adding a new outside dependency.

---

## Can we replace a service with our own code?

It depends on what the service gives us.

| The service provides | Replaceable by our code? | Examples here |
|---|---|---|
| **Computation** | Yes. | Subtitle timing (`subsync.py`, already ours), speech-to-text (`stt.py`, retired but intact) |
| **Data other people wrote** | No. We can only cache what we already have, or switch to another source of the same data. | TMDb titles/episodes/artwork, subtitle files, the anime season table |
| **Access to someone else's site** | No. | Jackett's indexers, YouTube, GitHub |

So the honest answer for TMDb and for subtitle *files* is no: nobody can compute an
episode name or a translator's work. What we control is (a) how much keeps working from
what is already on disk, and (b) how cheaply a second source can be plugged in.

---

## Summary

Ordered by how much damage a loss does.

| Service | Used for | Key / cost today | If it goes | Can we mitigate? |
|---|---|---|---|---|
| **TMDb** | Search, Explore, library names, artwork, episode numbering, bookmarks, Siri, IMDb ids | Free key per install | Existing library keeps its names and artwork; everything about *finding or adding* a title degrades to file names | Partly. Second source for TV; no single free equal for films + artwork |
| **Jackett + public indexers** | Every torrent search | Free, local | No new downloads at all | Swap the local aggregator; cannot replace the sites |
| **OpenSubtitles (legacy)** | Subtitle search + download | Free, no key | No fetched subtitles; embedded ones unaffected | Yes: new API with tiny quota, other sites, or local speech-to-text |
| **GitHub** | Updates, app channel, anime table, setup downloads | Free | No updates, no fresh installs | Mostly: mirror the repo and vendor the downloads |
| **Anime-Lists** (file on GitHub) | Anime season grids | Free, no key | Cached table keeps working, stops learning new shows | Yes: cached copy is used at any age |
| **YouTube IFrame API** | YouTube on TV, trailers | Free, no key | Those two features stop | No |
| **Tailwind CDN** | Admin panel styling only | Free | Admin panel unstyled when the box has no internet | Yes: vendor it, as the dashboard already does |
| **Mullvad CLI** | VPN check (default mode) | Paid VPN | Downloads refuse to start | Yes: `generic` mode already exists |
| **Apple / SideStore** | Installing the iOS app | Free Apple ID | App cannot be installed or refreshed | No. The dashboard in a browser still works |
| **Tailscale** | Reaching the box away from home | Free plan | No remote access | Yes: any other VPN into the LAN |

---

## TMDb

### How we use it

Every call goes through one function, `_tmdb_get` in `main.py`, which sits on the disk
cache in `tmdbcache.py`. There are about 35 call sites using these endpoint families:

- `/search/{tv,movie,collection}`, `/trending`, `/discover`, `/genre/*/list`: Explore and its search box
- `/tv/{id}` (+ `alternative_titles`, `videos`, `external_ids`), `/tv/{id}/season/{n}`, `/movie/{id}` (+ `release_dates`), `/collection/{id}`: library metadata
- `/tv/{id}/episode_groups`, `/tv/episode_group/{id}`: the View picker and attribution pass 4 (`epgroups.py`)
- `/{kind}/{id}/watch/providers`: "where to watch" (JustWatch data, redistributed by TMDb)
- `/find/{imdb}`, `/{kind}/{id}/external_ids`: id lookups
- `image.tmdb.org`: all posters, backdrops and stills, proxied through `/api/metadata/img`

Features that need it: Explore, search-as-you-type, the show page, library episode names
and stills, absolute-number resolution, anime remapping (`animemap.py` is keyed on TMDb
ids), episode groups, bookmarks and their release countdowns, the unreleased-film gate,
trailers, Siri film requests (`voicepick.py` ranks on TMDb vote counts), and subtitle
search (which looks titles up by the **IMDb id that TMDb gives us**).

### What already survives an outage

- **Per-item metadata** is stored in `library.json` under `item["metadata"]`. An
  existing library keeps its names, numbering and episode lists with TMDb gone forever.
- **The response cache** (`.tmdb_cache/`) serves fresh entries without a network call
  and serves stale entries at any age when a refetch fails. It is pruned at 180 days
  or 20,000 entries.
- **The artwork cache** (`.tmdb_img_cache/`) keeps every image ever shown, with no expiry.
- A 30 s offline backoff stops an outage from stacking timeouts.
- With no key at all, search falls back to raw Jackett results and episodes show file names.

So a TMDb outage, or a rate limit, is already handled. The gap is a **permanent** loss.

### What a permanent loss would cost

| Still works | Stops working |
|---|---|
| Playing everything already in the library, with its names and artwork | Explore, title search, the show page for anything not cached |
| Raw indexer search (type a title, press Enter) | Names, numbering and artwork for **newly added** items |
| Smart Skip, prep, streaming, the app's downloads | New seasons appearing on shows you already have |
| | Bookmarks' release tracking, the unreleased gate, trailers, where-to-watch |
| | Siri "download a film" (title choice needs TMDb) |
| | Subtitle search precision for new items (no IMDb id; falls back to title search) |
| | Anime remapping and episode groups for new items |

After 180 days the response cache would also prune entries it could no longer refresh,
so show pages for seasons you do not own would empty out.

### How likely, and in what form

- **Terms** *(checked)*: the API is free for non-commercial use with attribution. TMDb
  may "change, suspend, or discontinue any aspect of the TMDB APIs at any time, without
  notice or liability" and may end a licence "at any time for any reason". A commercial
  licence is reported at about $149/month for small companies.
- **Each install uses its own key.** There is no shared StreamLink key for TMDb to
  revoke, so the realistic failures are (1) one user's key revoked, (2) a new free-tier
  quota, (3) the free tier ending.
- **We are out of step with the terms in two places today.** These are the most likely
  reason for a key to be revoked, and both are cheap to fix:
  1. **No attribution.** The terms require the TMDb logo and the sentence "This product
     uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise approved by
     TMDB." The dashboard credits JustWatch on the where-to-watch row but credits TMDb
     nowhere.
  2. **Caching beyond 6 months.** The terms forbid caching TMDb information for longer
     than 6 months. The response cache prunes at 180 days, which fits. The artwork cache
     never expires, and `item["metadata"]` in `library.json` is kept indefinitely.
     The second one is in tension with our own offline design: resolving it strictly
     means a library goes blank six months after TMDb disappears. That is a decision for
     the owner, not a bug to fix quietly.

### Backup plan

**If a quota arrives (most likely case).** Mostly absorbed already by the cache. The one
expensive pattern is the season fan-out (a long show is ~25 requests on first open).
Levers, in order: lengthen `ttl_for` for settled data, stop prefetching seasons the
viewer has not opened, and make search-as-you-type wait for a longer pause.

**If the free tier ends.** There is no single free equal. Candidate sources:

| Source | Covers | Key / limits | Gaps |
|---|---|---|---|
| **TVmaze** *(checked)* | TV shows, seasons, episodes, air dates, some images | No key; about 20 requests / 10 s; CC BY-SA | **No films.** No episode groups. Its own season splits, which differ from TMDb's |
| **TheTVDB v4** *(checked, sources disagree)* | TV + films, artwork | Key required. Reported both as free under $50k revenue with attribution, and as a metered plan (500 calls/day free) | Terms have changed repeatedly; confirm on their site |
| **OMDb** *(checked)* | Films + TV basics, IMDb ratings | Free key, 1,000 requests/day | Thin episode data; posters *(unverified)* need a paid key |
| **IMDb non-commercial datasets** *(unverified)* | Titles, years, episode-to-series links, ratings, as daily bulk files | No key; personal, non-commercial use | No plots, no artwork. Bulk files, so we would index them locally |
| **Wikidata** *(checked)* | Id crosswalk: TMDb ↔ IMDb ↔ TVDB ↔ TVmaze | No key, open licence | Not a metadata source on its own; coverage is uneven |
| **AniList / Kitsu / Jikan** *(unverified)* | Anime | Free | Anime only; absolute numbering, no TMDb seasons |

A workable shape would be TVmaze for TV, OMDb or the IMDb files for films, AniList for
anime, each behind an adapter that returns TMDb-shaped dicts so the 35 call sites do not
change. Three things make that harder than it sounds:

1. **Ids.** `tmdb_id` is the key everywhere: `library.json`, bookmarks, the per-profile
   episode view, the Anime-Lists table (`tmdbtv`). A second source needs a crosswalk.
2. **Season grids differ between sources.** The attribution passes in `episodes.py`,
   `animemap.py` and `epgroups.py` were all tuned against TMDb's splits. A different
   grid moves files between seasons and would need the `tests/search_eval/` kit re-run.
3. **Artwork** has no free equal at TMDb's coverage. Fanart.tv *(unverified)* is the
   usual substitute.

**Cheap preparation worth doing while TMDb is free** (not done; each needs its own patch):

- Show the TMDb attribution. Removes the likeliest reason for a revoked key.
- Store `tvdb_id` and `imdb_id` on every item. `external_ids` is already requested for
  TV (since 17.7.0) but only `imdb_id` is kept. Once TMDb is gone the crosswalk for an
  existing library can no longer be fetched.
- Distinguish "TMDb refused us" (401, 429) from "TMDb is unreachable" in `_tmdb_get`
  and surface it in the admin panel. Today both silently serve stale data, so a revoked
  key would look like nothing is wrong until new items start showing file names.

---

## OpenSubtitles

### Where it stands

- We use the **legacy** endpoint `rest.opensubtitles.org` with no key and the vendor's
  test User-Agent (`TemporaryUserAgent`). See `subsearch.py` and `_os_get_json` /
  `_os_download` in `main.py`.
- The vendor posted a **final shutdown notice on 2026-01-29**: the OpenSubtitles.org
  API is to be shut down "completely" for all third-party applications, VIP or not, "in
  the coming weeks" *(checked)*. Earlier deadlines (end of 2023) slipped.
- **The endpoint still answered on 2026-10-04** (one search, HTTP 200 with results). It
  is running past its announced end and can stop without further warning.
- When it stops, `_os_get_json` will see errors or redirects and report no results. The
  UI will say there are no subtitles, which is the wrong message.

### The replacement and its limits

The successor is the **OpenSubtitles.com REST API** *(checked)*:

- **5 downloads/day** with no account, **20/day** with a free account. Paid tiers raise
  this; exact VIP numbers *(unverified)*.
- It needs an API key registered to the application, and a real User-Agent *(unverified
  detail; confirm in their docs before building)*.
- Search itself is not counted against the download quota *(unverified)*.

What 20/day means for us: today we budget **150/day** (`_OS_DAILY_CAP`), and the
automatic fetch may download up to **3 candidates per episode** because it keeps only a
subtitle the audio has verified (`_fetch_subtitle(max_tries=3)`). At 20/day that is as
few as 6 episodes a day. Automatic fetching for a whole season stops being possible on a
free account; manual one-off picks stay fine.

### What is ours and survives a provider change

- Everything about **embedded** subtitles. Most releases carry their own tracks, and
  those never touch a provider.
- `subsync.py`: aligning a subtitle to the speech and scoring confidence.
- `subsearch.sniff_format` / `retime`: reading and re-timing in the file's own format.
- Sidecars already downloaded (`.opensubs.` files) stay on disk and keep working.

What is tied to this provider: `legacy_url`, the result field names the filter and
ranking read (`SubFileName`, `MovieHash`, …), and the ranking weights, which were
measured on this provider's results with `tests/subs_eval/`.

### Backup plan

In the order we would try them:

1. **Move to the OpenSubtitles.com API** with a per-install login, entered in the admin
   panel like the TMDb key. Same catalogue, so the ranking should carry over; the eval
   kit must be re-run because field names and match flags differ. Change the automatic
   fetch to spend one download per episode (rank harder, verify, and stop) and to skip
   it entirely when the day's budget is low.
2. **Add a second provider** so one quota is not the ceiling. Candidates: SubDL (free
   key, reported about 2,000 calls/day *(unverified)*), Jimaku for anime (free key,
   mostly Japanese-language subtitles *(unverified)*), Podnapisi (no official API
   *(unverified)*). Each needs an adapter that returns the same candidate shape
   `subsearch` ranks. The audio check in `subsync.py` is what makes a less-curated
   source safe to use.
3. **Generate subtitles locally.** This is the one place "replace the service with our
   code" is literally possible. `stt.py` (whisper.cpp) is in the tree and working; it
   was retired in 17.0.0 because the transcripts "were not good enough to put in front
   of viewers" ([STT.md](STT.md)). It produces the original language, or English by
   translation only. It costs minutes of CPU per episode on the box. It would be a last
   resort for files with no embedded track and no download left, clearly labelled as
   machine-made, and worth re-testing with a larger model before relying on it.

**Cheap preparation** (not done):

- Put the provider behind one small interface (search → candidates, download → bytes)
  so `main.py` does not care which site answered.
- Make a dead provider say so. A shutdown should read "subtitle search is unavailable",
  never "no subtitles found".
- Re-run `tests/subs_eval/` against the new API **before** the old one dies, while both
  can be compared. Mind the quota: the kit spent a whole day's allowance once already
  (see [GOTCHAS.md](GOTCHAS.md) § Subtitle search).

---

## Jackett and the indexers

Jackett is local software, so it has no price to change. The exposure is one level down:
the public torrent sites it queries change domains, add Cloudflare challenges, or
disappear, and Jackett's definitions lag behind. FlareSolverr (optional) exists only to
get past those challenges and breaks whenever they change.

- **If an indexer dies:** add another in the admin panel. Nothing in our code changes.
- **If Jackett is abandoned:** Prowlarr speaks the same Torznab protocol
  *(unverified in this repo)*. Our calls are `/api/v2.0/indexers/...` in `main.py`,
  which is Jackett's own path layout, so the search and admin-management calls would
  need porting.
- **Cannot be mitigated:** the sites themselves. We cannot write an indexer's catalogue.

---

## GitHub

Four separate uses, with different consequences:

| Use | Where | If GitHub is unavailable or changes terms |
|---|---|---|
| Server updates (`git fetch origin`) | `updater.py` | Boxes stay on their current version. Mitigation: any git remote works; change `origin` |
| iOS app channel files and `.ipa` releases (`nmautz/streamlink-ios`) | `appchannel.py`, `ios-app/publish-ipa.sh` | The app shows no update; SideStore cannot fetch the build. Mitigation: host the JSON and `.ipa` anywhere static, and change `SOURCE_REPO` and the source URL users added |
| Anime-Lists table (`raw.githubusercontent.com`) | `animemap.py` | See below |
| Setup downloads: FlareSolverr, fpcalc (chromaprint), whisper.cpp | `setup.py` | **Fresh installs fail** at those steps. Existing boxes are unaffected |

`setup.py` also downloads ffmpeg from `gyan.dev` (one person's build site) and, when STT
is enabled, whisper models from Hugging Face. All of these are install-time only. The
mitigation is the same for each: keep a copy of the exact files we install in a release
asset we control, and have `setup.py` try that second.

A repository takedown is the scenario that hits everything at once. A second remote
kept in sync is the whole defence.

---

## Smaller dependencies

**Anime-Lists** (`anime-list-full.xml`). A community file, fetched weekly. `animemap.py`
uses the cached copy at any age, so a loss freezes the table: existing shows keep their
grids, new anime fall back to the structural parse. Nothing to build. Note it is keyed
on TMDb ids, so it shares TMDb's fate.

**YouTube IFrame API** (`static/tv.html`, trailers in `static/index.html`). No key. If
Google restricts embedding, YouTube on TV and trailers stop. We chose the browser over
yt-dlp because it does not break on signature changes ([YOUTUBE.md](YOUTUBE.md)); there
is no further fallback and none is worth building.

**Tailwind CDN.** The dashboard uses a vendored copy (`static/vendor/tailwind.js`).
**`static/admin.html` still loads `cdn.tailwindcss.com` live**, so the admin panel
renders unstyled when the box has no internet, which is exactly when an admin is most
likely to open it. Fix: point it at the vendored file.

**Mullvad.** The default VPN check runs the Mullvad CLI. If Mullvad changes its CLI or
the user changes provider, `VPN_MODE=generic` already covers any VPN adapter.

**Apple / SideStore.** The app is sideloaded with a free Apple ID. Apple can change
sideloading rules at any time and we have no say. The dashboard works in any browser, so
the loss is the native features (background play, downloads, AirPlay, Siri), not access.

**Tailscale.** Used for reaching the box remotely. Its free plan could change. Any VPN
that routes to the LAN replaces it; discovery (`discovery.py`) picks Tailscale out by
subnet and would need the new range added.

---

## Before adding a new outside dependency

1. Can it be computed locally, or vendored? Prefer that.
2. Route it through one function, the way `_tmdb_get` does, so a swap touches one place.
3. Cache what it returns, and serve the cached copy when it fails.
4. Make "the service refused us" visible and distinct from "nothing found".
5. Add it to the summary table above.

---

## Sources (checked 2026-10-04)

- [TMDB API Terms of Use](https://www.themoviedb.org/api-terms-of-use) (last updated 2023-10-20)
- [TMDB developer FAQ](https://developer.themoviedb.org/docs/faq)
- [TMDB commercial pricing thread](https://www.themoviedb.org/talk/622b91d0d236e60045f62782)
- [OpenSubtitles.org API: final shutdown notice](https://forum.opensubtitles.com/t/opensubtitles-org-api-final-shutdown-notice-for-non-vip-users/5045) (2026-01-29)
- [OpenSubtitles help: about the API](https://opensubtitles.tawk.help/article/about-the-api) (5 / 20 downloads per day)
- [TVmaze: using the API commercially](https://www.tvmaze.com/threads/6552/using-posters-and-data-from-tvmaze-api-on-a-commercial-website?page=1)
- [TheTVDB developer FAQ](https://support.thetvdb.com/kb/faq.php?cid=12)
