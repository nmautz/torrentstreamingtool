---
name: release-candidate
description: Find which alpha build is fit to promote to main (or beta), from the changelog, the fixes that shipped after each build, and the box's own logs; ask the owner only what no log can show; give them the final say; then promote. Use when the user says "find a release candidate", "what can go to main", "is anything ready for main", "promote to main", "cut a release", or similar.
---

# Release candidate

Work lands on `alpha`. `main` is what other people run. This is how a build gets
from one to the other: you gather the evidence and form a view, the owner decides.
Read docs/GOTCHAS.md § Release channels once if you have not this session.

The owner wants to say one sentence and get back: the candidate(s), why, what is
still unknown, and a yes/no. Do the reading yourself. Do not hand them raw output.

## 1. Where things stand

```bash
python3 promote.py              # what main / beta / alpha are on, server and app
STREAMLINK_ADMIN_PASSWORD='…' python3 promote.py candidates --box https://<box>
```

**Every build between `main` and the tip is a candidate, not only the tip.** The
tip is usually the least proven build there is. `candidates` lists them all,
newest first, and for each prints what it changed, what the changelog admits
was **not checked**, which app it would pair with, how long it ran on the box,
and **what was fixed after it**. That last list is what you would be shipping
without by stopping there.

Also read what `main` has NOW. Every "Fixed" in the range is a bug `main` users
have today. Waiting is not free when `main` is carrying known defects, and an
older build that fixes them can be the right answer while the tip is not ready.

## 2. What the box says

The box is the only place these builds have actually run. Its address and admin
password are in memory (`streamlink-client-logs`); never write them into the repo.

```bash
STREAMLINK_ADMIN_PASSWORD='…' python3 promote.py logs --box https://<box>
```

Per version: how long it ran, how many times it started, errors, tracebacks, and
the error signatures **no older version logged**. Then look closer where it matters:

- A build with new signatures or tracebacks: pull that run's log and read the
  lines (`/api/admin/logs`, the `logs_old_*.zip` whose time matches). Decide
  whether it is a defect or noise. Some `ERROR` lines are not errors (Smart Skip
  logs "no shared intro" at that level).
- Many starts in a short time is a crash loop or a day of deploys. The times say which.
- The app's side: `GET /api/admin/client-logs` (histogram), then
  `/api/admin/client-logs/<device>?errors_only=1`. Look for crash rows, download
  failures and stalls since the candidate's date. **Do not clear the client logs.**
- `GET /healthz` and `vitals.log` if the question is whether the box itself was well.

Know the limits and say them:
- The version in the log is the **backend's**. A static-only deploy leaves the old
  number, so a newer dashboard may have run under it.
- Hours running is not hours used. A build that ran overnight with nobody watching
  proved it does not crash idle, nothing more. The access log and the client log
  show whether anyone played, downloaded or searched.
- The box is one Windows machine and one phone. Linux, a second server, a fresh
  install: no log covers them.

## 3. Choose

Go down the list build by build and say, for each plausible stopping point, why
it qualifies or what rules it out. Natural stopping points are the last build
before each x-bump and the end of each run of z-bumps. Do not stop at "the tip
is not ready": the question is which build IS, and the answer is often several
versions back.

A candidate is a build where all of these hold. Prefer the newest that qualifies.

- **Nothing fixed after it matters.** Read each later fix. If it corrects
  something this build introduced, or something a `main` user would hit, the
  candidate is the later build. A fix to a feature the candidate does not have
  does not count against it.
- **It has run.** Ideally a few days on the box with real use and no new error
  class. `candidates --box` gives two numbers: hours as the backend, and hours
  counting every build after it. For an older build the second is the honest
  one: its code kept running inside its successors. A build that was "never the
  backend at a start" was deployed static-only, so its hours hide under the
  banner before it. If nothing has run long, say so plainly.
- **Its x/y features are finished.** The end of a run of z-bumps is usually a
  better stopping point than the feature release that started it.
- **The app it pairs with exists and was used.** `candidates` names it. A build
  whose app was never on a phone is not ready whatever the server logs say.
- **Setup still works.** If `setup.py`, `run.py`, `requirements.txt` or the README
  changed in the range, somebody has to have run an install or an update through
  it. A `main` user gets there by the auto-updater, on Windows.

- **Its app source can be made to match.** `promote.py` cuts a channel's source
  back when it offers an app newer than the build ("CUT BACK" in the plan). Say
  so in the report: phones that already took the newer app keep it.

It is fine to return no candidate, or two (a cautious older one and a newer one
that needs one thing checked). Say which you would pick, and what `main` users
keep suffering if the answer is to wait.

## 4. Ask the owner what the logs cannot say

One `AskUserQuestion` call, at most four questions, each one about a specific
"NOT CHECKED" line or a gap in the evidence, answerable from memory:
"Since 20.0.3, have you watched something offline in the app?" Not "is it stable?".
Skip anything the logs already answered. If a question needs a test rather than
a memory, offer to run it (the phone can be driven from the Mac; see memory
`drive-iphone-from-mac`).

## 5. Report, then the final say

Give the owner, in plain sentences:

1. The build you recommend (or that none is ready), in the first line.
2. Why: time on the box, what it logged, what shipped after it.
3. What is still unverified, and what a `main` user risks from it.
4. What promoting does: `main` (and `beta`) move to that commit, boxes on `main`
   update themselves, the named app appears in the `main` SideStore source.

Then ask for the decision with `AskUserQuestion`: promote the recommended build,
promote a different one, or wait. Promotion is outward-facing and cannot be
quietly undone. Never run `--go` without that answer in this session.

## 6. Promote

```bash
python3 promote.py main --version X.Y.Z                              # the plan; changes nothing
python3 promote.py main --version X.Y.Z --verified "…" --go
```

`--verified` is the permanent record in the `release/main/X.Y.Z` tag. Write what
was actually established: the soak ("ran 3 days on the box, 2 starts, no new
errors"), what the owner told you they did, and what was knowingly left
unchecked. No adjectives, nothing you did not see or were not told.

Afterwards run `python3 promote.py` again and confirm `main`, the tag and the app
source agree. Update memory `release-channels` with the date and the version.

## If it goes wrong

- `promote.py` refused: the message is the reason. Do not work around a refusal.
- The branch moved but the app did not: re-run the `publish-ipa.sh --promote`
  line it printed.
- A promoted build turns out bad: fix it on `alpha` and promote the fix. `main`
  is never moved backwards (boxes fast-forward; SideStore does not downgrade).
