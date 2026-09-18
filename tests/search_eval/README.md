# Search-accuracy evaluation kit

The labelled set and harness behind the episode-matching rules in
[docs/GOTCHAS.md](../../docs/GOTCHAS.md) (§ "Matching an episode's numbers is not
matching the show"). Keep it if you touch `_ssEpisodeAccept`, `_ssSortSources`,
`_title_relevance` or `parse_torrent_title` — it is the only way to tell an
improvement from a plausible-sounding change.

**Not part of `make test`.** These scripts need a running StreamLink with working
indexers; the unit tests next door need nothing.

## What is here

| File | What it is |
|---|---|
| `labels.json` | **The ground truth.** 69 shows chosen to be confusable (remakes, UK/US/AU versions, sequels, same-name shows, anime reboots), each with the indexer results that parsed as the requested episode, hand-checked one by one: `true` = that release really is that episode of that show, `false` = it isn't, `null` = undecidable from the name. Collected 2026-09-17. |
| `collect.py` | Re-runs the same queries against a live box and writes a fresh `cases.json`. Releases come and go, so re-collect rather than trusting old rows. |
| `verify.py` | **The one to run.** Replays what the show page would show for every target, live, and scores it against `labels.json`. |
| `score.py`, `rule2.py`, `rule3.py` | Offline scoring of rule variants over a collected set, including the ones that shipped and the ones that didn't. |
| `bench.py` | Runs a local LLM (llama-server, OpenAI-compatible) over the same set, for the periodic "would a model do better?" question. |
| `pickdiff.py` | **Ranking, not matching.** Prints old pick vs new pick — season pack and per-episode — over live results for 11 shows. A differ, not a scorer; see below. |

## Running it

```bash
python tests/search_eval/verify.py fresh_labelled.json      # live, against the box
python tests/search_eval/collect.py fresh.json all 30       # re-collect candidates first
```

`verify.py` and `collect.py` have the box's address at the top; point them at yours.
New releases appear that `labels.json` has never seen — they score as `null`
(ignored), so re-label them when the unlabelled count gets big, or the numbers
slowly stop meaning anything.

## The numbers to beat

Measured live on 69 shows / ~1700 results:

| | Right shown | Wrong shown | Wrong top row |
|---|---|---|---|
| Numbers only (pre-16.5.0) | 402 | 63 | 10 of 69 |
| 16.5.0 (name + year + country) | — | — | 10 |
| 16.6.0 (+ episode title, season year, evidence sort, article) | 387 | 41 | 4 |
| **16.6.0 measured live on the box** | **469** | **38** | **1** |

The **wrong top row** column is the one that matters: it is what Play now and
Auto download without asking.

## The local-LLM answer (2026-09-17)

Nine runs — Qwen3 8B / 4B / 4B-thinking / 1.7B, Llama 3.2 3B, Phi-4-mini,
Gemma 3 4B, plus few-shot and rules-first prompts — judged the same results with
the show's TMDb facts in the prompt. **None improved the top row over the rules
above.** The best (Qwen3-4B, rules-first) matched them at 4 wrong top rows while
trimming wrong rows further (92.7% vs 90.4% precision), for ~20 s per show on the
box's GTX 1060 and 2.5 GB of VRAM.

**Qwen3-4B-Thinking**, re-run on the 16 hardest shows with a token cap big enough
for its reasoning (15,933 generated tokens per show — ~100 s each on an RTX 5070,
so 8-10 minutes per show on the box's 1060), was the only run to beat the rules on
anything: one more show found, 86.7% vs 75.7% precision on that subset. Both
differences trace to `Hunter.x.Hunter.S01E015` — the three-digit episode parse bug
fixed in 16.6.0, which this data predates. Against the fixed parser it adds nothing.

Two findings worth keeping:

- **Prompt shape beat model size.** Making the model write the show name it saw
  before answering took per-result accuracy from 79% to 87%. Bigger models were
  *worse* left to themselves: Qwen3-8B scored 61% precision and Qwen3-1.7B 41%,
  against the 4B's 77% — they say yes too often.
- **The mistakes were mechanical** — extra words in a name, a wrong year, a
  country tag — which is why rules fixed them for free.

**If you re-run a thinking model, size `--maxtok` for its reasoning.** The first
attempt used the ordinary cap (~1,050 tokens for a batch of 10), so every answer
came back empty and the harness scored them all "no" — a 1.9-hour run that
measured nothing. `bench.py --maxtok 6000 --batch 5` is what worked.

Worth re-testing only for a judgement no rule can express.

## What `pickdiff.py` measures, and what it can't (17.3.0)

Everything else here grades against `labels.json`, which can exist because "is this
release episode 7 of that show" has a right answer a person can check. **"Is this the
*better* release" does not.** Ground truth for it would be seventeen hundred subjective
calls encoding one person's taste, and a score computed from that would look
authoritative while meaning nothing.

So `pickdiff.py` prints the diff and leaves the judgement where it belongs. Two lines in
its summary are objective and worth watching:

- **`bucket downgrades` must be 0.** Track richness is capped at the availability bucket
  by construction, so it can reorder two equally-available copies and can never promote a
  Low one over a Good one. Non-zero means `_pickCmp` is mis-wired — a bug, not a trade-off.
- **`seeder drop >10x`** is the cost line. It is what "richness dragged in a worse-seeded
  release" actually looks like, with the titles printed beside it so they can be read.

It mirrors `_pickCmp`, `_ssAutoPickFrom`, `_packCoversScope` and `_bgAbsBatchOk` from
`static/index.html`. When one of those changes, change it here too, or it stops measuring
the shipping code.

**The 17.3.0 baseline**, live on 11 shows / 171 episode picks: 16 changed (9%), **0 bucket
downgrades**, **0 picks losing more than 10x in seeders**, 11 gaining an audio track and 11
gaining subtitles. All four English-original controls (Breaking Bad, The Bear, Futurama, Hacks)
moved not at all, which is the shape to expect — a single-track release scores 0.

**It has already decided one thing.** Accepting absolute-numbered batches (`Episodes
1-148`) as season coverage was built for Hunter x Hunter and dropped on this harness's
evidence: across all 11 targets it found no pack the ordinary season/multi-season rule
hadn't already found. `abs_batch_ok` is still in the file, so re-testing is cheap.
