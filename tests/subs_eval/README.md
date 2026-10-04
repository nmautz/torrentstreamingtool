# Subtitle-search evaluation kit

The labelled set and harness behind every rule in [subsearch.py](../../subsearch.py)
and every constant in [subsync.py](../../subsync.py). Keep it if you touch either
— it is the only way to tell an improvement from a plausible-sounding change.
Collected 2026-09-18 against the live box (17.7.0).

**Not part of `make test`.** Collecting needs a running StreamLink; the unit
tests next door (`tests/test_subsearch.py`) need nothing.

## The answer key

Every target is a library file that already HAS an English subtitle track, and
the search runs as if it didn't. That track is the ground truth: a download is
graded on whether it says the same things at the same moments.

| Grade | Means |
|---|---|
| `sync` | Right episode, and within 0.5 s of the embedded track at the start AND end |
| `shift` | Right episode, off by a constant (an aligner can fix it) |
| `drift` | Right episode, but the two ends disagree — different cut or frame rate |
| `wrong` | Not this episode (or unusable) |

Two graders, because not every track carries text:

* **text tracks** — `aligned_text`: for each reference line, how much of its
  vocabulary appears in candidate lines at the same moment. Different
  translations of the same episode still score ~0.5–1.0; a different episode of
  the same show tops out at 0.25 (measured across every pair in the corpus), so
  0.30 is the line. Plain word overlap is NOT enough: Death Note episodes share
  49 % of their vocabulary with each other.
* **image tracks** (PGS Blu-rays) — timing only, via the subtitle packs
  `subpack.py` renders. No text, but the offsets at the start and end of the
  file agreeing is enough: on the text-graded pool that rule let only 4 wrong
  candidates through out of 91.

## What is here

| File | What it is |
|---|---|
| `corpus.json` | **The targets.** 52 episodes/films over 12 shows (6 anime, 4 live-action series, 2 films), each with its show identity, numbering, runtime and the embedded track used as the answer key. |
| `collect.py` | Rebuilds `corpus.json` from a live box (bundles with an English text track) and downloads each answer key. IMDb ids come from Wikidata, since the box never exposes its TMDb key. |
| `collect_pgs.py` | Adds targets whose only English track is an image subtitle, by building `subpack` packs on the box. |
| `fetch_audio.py` | Pulls each target's ORIGINAL-language audio out of its HLS bundle (16 kHz mono FLAC) — what alignment is measured against. |
| `pools.py` | Runs every query shape per target and counts what comes back. How the URL rules were found. |
| `grade.py` | Downloads the candidate pool and grades every candidate. **Budgeted** — see below. |
| `features.py` | Per-feature outcome tables (last timestamp vs runtime, source match, format, fps, flags…). Where the ranking weights come from. |
| `rank.py`, `pipeline_eval.py` | Prototype scoring + the end-to-end simulation they were tuned on. |
| `align_eval.py`, `proto_sync.py`, `tune_vad.py`, `policy_eval.py` | The aligner's development harness, including the `alass` comparison. |
| **`verify.py`** | **The one to run.** Replays the whole pipeline through the SHIPPING `subsearch` / `subsync` and prints the table below. |
| `.cache/` | Searches, downloads, answer keys, audio, energy, grades. Gitignored (copyrighted subtitle text, GBs of audio). |

## Running it

```bash
SUBS_EVAL_OFFLINE=1 python tests/subs_eval/verify.py    # offline, from .cache/
python tests/subs_eval/collect.py                       # re-collect (needs the box)
python tests/subs_eval/grade.py                         # grade new candidates (spends downloads)
```

`verify.py` needs `.cache/`; without it, collect + fetch_audio + grade first.
The box's address and admin password are at the top of `collect.py`.

## Mind the download limit

OpenSubtitles allows **~200 downloads per IP per 24 h**, then blocks the IP for a
day and warns that continuing gets it firewalled. Searching is unaffected, so the
failure looks like "downloads broke". The kit caps itself (`common.DAILY_DOWNLOADS`,
per public IP) and stops at the first ban page, but **the box shares the house's
IP**: grading from home spends the box's allowance, and during development this
kit cost the house a day of subtitle downloads. Grade from a VPN, or sparingly.
`SUBS_EVAL_OFFLINE=1` guarantees no network at all.

## The numbers to beat

52 episodes, one automatic fetch each (verify.py):

| | In sync | <1 s off | Drifting | Wrong | Skipped |
|---|---|---|---|---|---|
| 17.6.0 | 0 | 0 | 0 | 0 | 52 — every search hit the dead-host redirect |
| Ranking by download count | 16 | 8 | 14 | 13 | 1 |
| **17.7.0** | **43** | 5 | 2 | 1 | 1 |

~1.1 downloads per episode. The one `wrong` is South Park S29E01, whose own
embedded track the aligner also moves by 72 s — that answer key is suspect, not
the pick.

Alignment alone, over all 290 graded candidates: in sync 103 → 156, one good
subtitle broken. `alass` on the same set: 132, with 25 broken.
