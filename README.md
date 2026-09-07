# ReqTrace

A search index over Australian data / analytics / ML roles, pulled from
employers' Applicant Tracking Systems rather than from job boards.

Identity is the ATS's own requisition id, so one record per role and duplicates
cannot occur by construction. Apply links point at the employer. And because
every run pulls the *full* board and diffs it, a role that gets filled
disappears — the `first_seen_at` / `closed_at` trace the index accumulates is
the thing it is named for, and is worth more than the listings.

**353 boards · 51,232 jobs · 3,356 open Australian roles · 279 of them data
roles · 7 ATS adapters · 129 tests**

Browse a snapshot at <https://reqtrace.sidharthjoly.com/>; ingest health at
<https://reqtrace.sidharthjoly.com/runs.html>.

## Step 0 — the audit that decided the build order

`data/step0_ats_audit.csv` — 63 AU employers, 45 resolved to an ATS. Tokens were
verified by calling the vendor's JSON feed, not by reading careers pages, so a
row with `verified_via=endpoint-200` is a board that actually returned jobs.

| vendor | companies | jobs | AU | AU data roles |
|---|---|---|---|---|
| greenhouse | 7 | 433 | 107 | 25 |
| ashby | 6 | 646 | 79 | 9 |
| smartrecruiters | 6 | 411 | 190 | 27 |
| lever | 5 | 70 | 53 | 9 |
| workable | 1 | 20 | 2 | 0 |
| workday | 5 | — | — | — |
| teamtailor / successfactors / phenom / avature / oracle / eightfold | 14 | — | — | — |
| unresolved | 19 | | | |

### What the audit settled

**Workday is not adapter #1.** The brief anticipated that AU enterprise might
concentrate on Workday and force it early. It doesn't. The enterprise segment is
*fragmented* — 20 companies scattered across 8 different HR suites (Workday 5,
Teamtailor 4, SuccessFactors 3, Phenom 2, Avature 2, Oracle 2, Eightfold 1). No
single enterprise adapter pays for itself, and none of them is a clean public
JSON feed. The brief's v1 scope survives contact with the data.

**SmartRecruiters is promoted into v1.** 6 companies, 411 jobs, 46% of them in
Australia — the highest AU density of any vendor, and it carries Canva, SEEK and
carsales.

**Greenhouse is adapter #1**, on three grounds:
- most audited employers (7, tied with Ashby but with 25 AU data roles to Ashby's 9 —
  Ashby's job count is inflated by Airwallex's 579-role global board)
- `?content=true` returns the **whole board with descriptions in one request**.
  SmartRecruiters returns no description in its listing at all: you need an extra
  call per job, so a 411-job board costs 412 requests.
- unknown tokens 404 cleanly, which makes token discovery cheap. SmartRecruiters
  answers `200 {"totalFound": 0}` for a board that doesn't exist.

### Endpoint reference (all verified)

| vendor | endpoint | unknown token |
|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | 404 |
| Lever | `api.lever.co/v0/postings/{site}?mode=json` | 404 |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true` | 404 |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/{co}/postings?limit=100&offset=` | **200, totalFound 0** |
| Workable | `apply.workable.com/api/v1/widget/accounts/{acct}?details=true` | 404 |

Two traps worth remembering: **Lever tokens are case-sensitive** (`Zeller` resolves,
`zeller` 404s), and slug-guessing produces false positives — `athena`, `zip` and
`kpmgaustralia` all returned healthy boards belonging to someone else or to a
vendor sandbox. Every token here was identity-checked against the board's own
company name and location mix.

---

## Running it

```bash
uv sync
uv run python -m reqtrace.run --vendor all           # every adapter, one after another
uv run python -m reqtrace.run --vendor greenhouse    # ingest every board for one vendor
uv run python -m reqtrace.run --vendor oracle --token 'ebuu.fa.ap1.oraclecloud.com/CX_1'
uv run python -m reqtrace.run --vendor ashby --from-fixtures   # offline replay
uv run python -m reqtrace.web                        # browse at 127.0.0.1:8765
uv run pytest -q

python scripts/install_autorun.py --publish  # sweep daily at 05:30, then publish
python scripts/install_autorun.py            # same, without pushing the export
python scripts/install_autorun.py --status   # is the schedule alive, and what did it do
python scripts/install_autorun.py --uninstall

python scripts/crawl_careers.py crawl --seeds unresolved --max-pages 400
python scripts/crawl_careers.py report        # -> data/discovery/crawled_<vendor>.json
```

Vendors: `greenhouse`, `ashby`, `smartrecruiters`, `lever`, `workday`,
`eightfold`, `oracle`; `--vendor all` sweeps them in turn. Vendors run one after
another rather than concurrently — `CONCURRENCY` is a per-vendor politeness
budget, and fanning seven adapters out at once would make it 28 requests in
flight against seven unrelated APIs.

`--vendor all` also rebuilds the FTS index when it finishes. That used to happen
only when the web server started, which was harmless while every run was typed
by hand; with a daily sweep landing jobs it would have left the index fresh and
the *search* stale.

Storage defaults to SQLite at `data/jobs.db` so this runs with no setup. Set
`DATABASE_URL` to a Neon/Supabase Postgres and it applies `schema_postgres.sql`
instead — TIMESTAMPTZ, a weighted tsvector index over title+body, and pg_trgm on
title for fuzzy matching.

### Storage

Descriptions dominate the index: the first full Greenhouse sweep stored 351MB of
them, of which 12MB (3.5%) were Australian roles. So bodies are kept **only for
AU jobs** (`Store(descriptions="au-only")`, the default; pass `"all"` to keep
everything). Non-AU rows are still inserted in full otherwise — closure detection
diffs against the entire open set, so dropping those rows would break it.

That takes 27k jobs from ~368MB to well inside a Neon/Supabase free tier. Worth
re-checking once Ashby is added: `bjakcareer` alone is 3,084 jobs.

> **The index still runs on SQLite day to day, but the Postgres path is no
> longer untested.** `schema_postgres.sql` applies cleanly, ingestion and
> closure detection are verified against a real server, and
> `tests/test_postgres.py` pins it down — see "Running it on GitHub instead".
> Executing it for the first time is what surfaced the BOOLEAN mismatch that
> would have killed every Postgres ingest on its first board. The live search
> UI is still SQLite-only; the published site does its searching in the
> browser, so that no longer blocks deployment.

Schema changes are migrated in place (`Store.MIGRATIONS`), not by recreating the
database. That matters more than it sounds: the `first_seen_at`/`closed_at`
series is the thing the brief says is worth more than the listings, and
`CREATE TABLE IF NOT EXISTS` silently skips new columns on an existing file, so
without a migration the only recovery would be deleting that history.

## Scheduling

Closure detection is the headline feature and it is only ever as truthful as the
last run — a role filled yesterday still reads as open until something diffs the
board again. Until now that something was a person typing a command.

`scripts/install_autorun.py` installs a **launchd agent** that runs
`scripts/autorun.sh` daily at 05:30 local, which is one `--vendor all` sweep plus
the FTS rebuild, appended to `data/logs/ingest.log`.

launchd rather than GitHub Actions, deliberately: Actions is gated on a Postgres
that does not exist yet (see Storage), and an ephemeral runner cannot see
`data/jobs.db`. When a `DATABASE_URL` is provisioned this becomes the fallback
rather than the plan. `--at HH:MM` moves the time, `--dry-run` prints the plist
without touching anything, `--status` reports what launchd thinks and tails the
log, `--uninstall` removes the schedule and leaves the database alone.

Two things that are easy to get wrong here. launchd hands a job a near-empty
`PATH`, so the absolute path to `uv` is baked into the plist rather than looked
up — a bare `uv` in the wrapper would work from a shell and fail from the agent.
And a sweep missed because the Mac was asleep is not skipped the way cron would
skip it: `launchd.plist(5)` says a missed `StartCalendarInterval` fires on wake,
with multiple missed intervals coalesced into one. That matters because a
skipped day is a hole in the `first_seen_at` / `closed_at` series that no later
run can fill; coalescing is the right trade, since the sweep diffs whatever the
boards say now rather than replaying each missed day.

`REQTRACE_ARGS` is appended to the sweep, so the whole launchd path can be
smoke-tested without waiting for 05:30 or pulling 357 boards:

```bash
env -i HOME="$HOME" PATH=/usr/bin:/bin UV="$(command -v uv)" \
  REQTRACE_ARGS="--vendor lever --max-boards 1" sh scripts/autorun.sh
```

The store now opens SQLite in **WAL**. A full sweep holds write transactions for
minutes at a time, and under the default rollback journal that locks readers out
entirely — the UI would fail for the length of every scheduled run.

## Static export

**Live: <https://reqtrace.sidharthjoly.com/>** — served from the `gh-pages`
branch. The repo stays private; the *site* is public, because private Pages is
Enterprise Cloud only. It lands on the personal domain rather than
`github.io` because the account has an org-level custom domain, so every project
site inherits it.

`scripts/export_static.py` writes `site/` — the same two pages, no Python behind
them.

```bash
python scripts/export_static.py            # build site/
python scripts/export_static.py --serve    # build, then browse it on :8766
python scripts/export_static.py --publish  # force-push site/ to the gh-pages branch
```

Neither page is a fork of the live UI. They check for `data/manifest.json` on
load: found means static, and they filter in the browser; absent (the stdlib
server does not serve it) means live, and they call `/api/*` as before. One
renderer, one set of filters, two backends. The data-role term lists are
*exported into the manifest* rather than retyped in JS, so the filter the README
already got wrong once ("Senior Tax Analyst") cannot drift into two versions.
The weekly pulse is the one figure the browser cannot derive and so ships
precomputed in the manifest: `jobs.json` is open roles only, so a closure has
left the file by the time the page could count it.

**Open Australian roles only** — 3,422 rows, 4.7MB, 0.82MB gzipped. The full
index is 51k rows and 28MB of JSON, which is not a page, it is a download.

Descriptions do not ship; their **vocabulary** does. Each role carries a `kw`
blob: its deduplicated tokens, minus any carried by more than 4% of the corpus.
Those are most of the bytes and can narrow nothing — "experience", "team",
"role", "working" are in nearly every ad — while a token in three ads is exactly
the one worth typing.

This replaced a 320-character preview, which was the worst possible selection.
Ads open with boilerplate and name their tools at the end, so on the published
site `pytorch`, `causal` and `terraform` matched **nothing at all** while
matching 22, 15 and 80 roles in the index. Recall now equals the server's on
every term measured (`snowflake` 56, `kubernetes` 124, `dbt` 30), for 0.82MB
gzipped against 0.35MB. The two are still not the same mechanism — FTS5 stems
and prefix-matches where the browser takes substrings — so they can still differ
on a word's other forms; they no longer differ on whether the word was read at
all. The field costs nothing in fidelity because the row stopped rendering an
excerpt when it became a single line: it only has to match now, never to read.

One honest limit remains, stated on the page rather than left to be discovered:
**it is a snapshot**. Stale the moment the next sweep lands, so both pages carry
the export timestamp, and `/runs` says outright that its "N hours ago" figures
count from the export rather than from now.

The daily sweep re-exports `site/` when it finishes, and — with
`install_autorun.py --publish`, which is how it is currently installed — pushes
that export to `gh-pages`, so the public site tracks the index instead of
freezing at whenever someone last ran it by hand. Publishing is a separate
opt-in from scheduling because it pushes to a remote, and an unattended daily
push is a bigger commitment than an unattended daily fetch;
`--no-publish` turns it back off, and reinstalling to change the time inherits
whatever the installed plist already says rather than silently resetting it.
`--status` reports both. The mechanism is `REQTRACE_PUBLISH=1` in the plist,
tested end to end
from a bare launchd-style environment, twice in a row, which is how the two bugs
in that path were found. The first was that `checkout --orphan` refuses a branch
name that already exists, so the *second* publish failed and every one after it
would have; the scratch branch is now per-process and deleted afterwards. The
second was the dirty-tree guard: it is a courtesy for the interactive case, not
a correctness one (the orphan worktree is built from `site/` and never reads the
working tree), so the scheduled path passes `--allow-dirty` rather than skipping
the publish whenever there is unrelated work in progress.

`--publish` force-pushes an orphan commit to `gh-pages` rather than committing
the export to `main` — the snapshot is regenerable, and 3MB of JSON a day would
be a gigabyte of git history a year. Pushing that branch is what enabled Pages
in the first place; there was no separate setup step. `jobs.json` is 3.1MB on
disk and **361KB over the wire**, since Pages gzips it.

## Running it on GitHub instead

`.github/workflows/sweep.yml` does what the launchd agent does — sweep, export,
publish — on GitHub's runners, so the index no longer depends on one laptop
being awake. **It is written and tested as far as it can be without a hosted
database; it has never run on a runner**, because it needs a `DATABASE_URL`
secret that only you can add.

### Why a database is not optional here

A runner is ephemeral. Closure detection diffs today's full board against the
**stored** open set, so with no persistent database every job is new every day,
nothing ever closes, and `first_seen_at` resets — the feature the project is
named for stops working *while the run still reports success*. That failure
would then be published over a working site.

So the workflow's first step refuses to start without the secret. Better a red
X than a green tick over a reset index.

### What you have to do

1. Provision a Postgres (Neon or Supabase free tier is plenty — the index is
   ~30MB with the AU-only description policy).
2. Add it as a repo secret named `DATABASE_URL`
   (Settings → Secrets and variables → Actions).
3. Seed it once from the laptop, so the history carries over instead of
   starting from zero:
   ```bash
   DATABASE_URL='postgres://…' uv run python -m reqtrace.run --vendor all
   ```
   Skip this and the first cloud sweep marks all 51k jobs as new and the
   `first_seen_at` series restarts.
4. `gh workflow run "Sweep and publish" -f max_boards=1` to prove it end to end
   on a few boards before trusting the nightly.
5. **Then turn off local publishing**, or the two will fight:
   ```bash
   python scripts/install_autorun.py --no-publish   # keep sweeping locally
   python scripts/install_autorun.py --uninstall    # or stop entirely
   ```

That last step matters more than it looks. The laptop sweeps into SQLite and
the runner sweeps into Postgres; if both publish, the site alternates between
two different databases with two different `first_seen_at` histories, and the
series stops meaning anything. Exactly one of them should be authoritative.

**Move the local schedule too.** The installer defaults to 05:30 local and the
workflow cron is `30 19 * * *` UTC — which *is* 05:30 in Sydney. Follow both
defaults and the two sweeps fire simultaneously, pulling all 357 boards from
seven third-party APIs twice at once, which is the shape of a rate-limit that
breaks both. `install_autorun.py --at 12:30` moves the local one clear.

Once the runner is trusted, the honest answer is to stop the local sweep
altogether (`install_autorun.py --uninstall`): keeping it means fetching every
board twice a day for a SQLite copy that drifts further from Postgres with each
run. `data/jobs.db` remains as the pre-migration snapshot either way.

### Seeding it: copy, do not re-sweep

`scripts/migrate_to_postgres.py` carries the SQLite rows over with their
timestamps. Sweeping into an empty Postgres would *look* like seeding and is
not: it stamps `first_seen_at` with today on every row, drops every
`closed_at`, and replaces the run log with one fresh entry per board —
restarting the exact series the project exists to accumulate. The migration
copies 51,232 jobs, 407 run rows and 387 companies by `COPY`, then asserts the
`first_seen_at` range and the closed count match the source before declaring
success.

Type conversions that only surface against a real server: `complete` is
INTEGER here and BOOLEAN there, `posted_at` is TEXT here and TIMESTAMPTZ there
(an empty string is a valid TEXT value and an invalid timestamptz), `function`
needs quoting, the BIGSERIAL `id` columns must be left for the sequence, and a
stray NUL in any of 3,362 vendor-supplied bodies aborts the whole COPY.

### A sweep has to survive the database hanging up

The first real Actions run died on `psycopg.errors.AdminShutdown: terminating
connection due to administrator command`. Not a misconfiguration: a sweep holds
one connection for hours while spending nearly all of that time waiting on
vendor HTTP APIs, and a serverless compute suspends when idle — Neon's default
is five minutes, which one slow Workday board clears comfortably.

`Store.reconcile` now reconnects and replays the board. Replaying is safe
because a board is upserts plus a diff against the stored open set, so it lands
on the same state; retrying at board granularity rather than per statement
means a half-applied board is re-applied whole rather than left torn. Any
serverless Postgres does this, so the fix belongs in the code rather than in a
provider setting.

### What the port actually needed

`store.py` already spoke both dialects, but the Postgres half had never been
executed. Running it turned up:

- `_record_run` passed `1`/`0` into a column declared `BOOLEAN NOT NULL`, so
  **every** Postgres ingest died on its first board. Now passes the bool, which
  SQLite stores as 1/0 anyway.
- `runs.py` was SQLite-only in five places, now a dialect table: `rowid` vs the
  `BIGSERIAL id`, `complete = 1` vs a real boolean, `sum(predicate)` vs
  `count(*) FILTER`, `julianday()` vs `EXTRACT(EPOCH …)`, and `substr()` on a
  text timestamp vs `to_char()` on a `TIMESTAMPTZ`.
- That last one is pinned to `AT TIME ZONE 'UTC'`. `to_char` otherwise buckets
  by the *session* timezone, so the same run landed on 2026-09-04 in a UTC-12
  session and 2026-09-05 in SQLite. A runner is UTC and a laptop is not.
- `SELECT *` in the window CTE returned different shapes per backend, since the
  Postgres table has an `id` column the SQLite one lacks. Columns are named now.

`tests/test_postgres.py` covers all of it against a real server, including a
test that runs the same snapshots through both backends and diffs the health
payloads. It skips unless `REQTRACE_TEST_DSN` is set, so `pytest` stays green on
a machine with no Postgres:

```bash
brew install postgresql@17 && brew services start postgresql@17
createdb reqtrace_test
REQTRACE_TEST_DSN=postgresql:///reqtrace_test uv run pytest    # 113 tests
uv run pytest                                                  # 105 + 8 skipped
```

The server is only needed to run those eight; it is left stopped, so `pytest`
skips them by default rather than failing on a machine without one.

The live search UI is still SQLite-only and that is now mostly moot: the
published site does its searching in the browser, so the export only has to
*read rows*, which both backends do.

## Closure detection

The headline feature. Every run pulls the *full* board and diffs it against the
stored open set; anything that fell off is stamped `closed_at`. A re-listed job
reopens (`closed_at` back to NULL) with `first_seen_at` preserved, so the
hiring-signal time series stays intact.

**An empty board retires nothing on the first sight of it.** SmartRecruiters
answers `200 {"totalFound": 0}` both for a board whose roles were all filled and
for a token that no longer exists, so a board must come back empty twice in a row
before anything is closed. That trades a small, bounded ghost-job window — one
extra cycle for a board that genuinely emptied — against irreversible corruption
of the `first_seen_at`/`closed_at` history, which the brief values above the
listings.

The guard that makes it safe: **only a complete board may retire jobs.** A
truncated or failed fetch is indistinguishable from mass closures, so
`BoardSnapshot.complete` is load-bearing — the Greenhouse adapter sets it only
when `meta.total` matches the number of jobs parsed, and `reconcile()` skips
closures otherwise. `test_closure_detection.py` pins this down.

## Board discovery (hard problem #1)

Coverage is capped entirely by how many ATS tokens we know about, and there's no
registry mapping companies to tokens. `scripts/discover_boards.py` harvests them.

**The unit of discovery is a board, never a job.** Crawling job links would
rebuild a job board — duplicates, dead links, no closure detection, and HTML
parsing across thousands of sites. One board token, found once, hands the
pipeline that employer's entire requisition history forever. It's also a far
smaller problem: thousands of tokens to find once, versus millions of job URLs
to re-crawl continuously.

It's barely a crawler, either. Common Crawl already crawled the web; we query
their URL index for the ATS domains and validate candidates against the vendors'
own JSON feeds.

```bash
uv run python scripts/discover_boards.py harvest  --vendor greenhouse
uv run python scripts/discover_boards.py validate --vendor greenhouse
uv run python scripts/discover_boards.py report     # -> data/discovered_boards.csv
```

Harvest → filter obvious URL noise → validate against the vendor endpoint (404
means junk) → keep only boards with AU roles. Validation uses the *light*
Greenhouse endpoint, since existence and locations are all that's needed to
decide whether a board is worth adopting.

Two things learned building it:

- **Common Crawl barely indexes `jobs.lever.co`** — a full sweep returns
  essentially just `robots.txt`. Lever tokens have to come from elsewhere (VC
  portfolio pages, certificate transparency logs).
- **AU detection has to be conservative here.** A false positive costs a
  permanently wrong board in the seed list, so ambiguous city names don't count:
  `Newcastle` is also England and `Perth` also Scotland, so they need explicit AU
  evidence. Discovery surfaced this — the first smoke test proposed a UK coffee
  chain as an Australian employer.

### Results of the first sweep

One Common Crawl collection yielded 6,832 candidate tokens; validation kept the
boards that actually list Australian roles:

| vendor | AU boards | AU jobs | AU data roles |
|---|---|---|---|
| greenhouse | 198 | 967 | 73 |
| ashby | 106 | 525 | 110 |
| smartrecruiters | 30 | 266 | 24 |
| **total (deduped)** | **317** | **1,758** | **207** |

308 of those were not in the hand-curated audit. Ingesting all 200 configured
Greenhouse boards: **27,085 jobs, 958 open in Australia, 0 board failures.**

Notable finds the manual audit missed: Xero, Lendi Group, DoorDash ANZ, Prezzee,
Neara, Firmus, Maincode — and `zipcolimited`, the *real* Zip Co, correcting the
false positive that slug-guessing produced.

**Adoption is append-only.** A board whose AU roles all get filled drops out of
the next sweep; if that removed it from the CSV the pipeline would stop fetching
it and every job it left behind would sit open forever — the exact ghost-job
problem this project exists to kill. Once adopted, a board is fetched forever.

Cadence: discovery is a **monthly batch** (new employers appear slowly);
ingestion stays on the daily cron. Public datasets and documented endpoints
only — if discovery ever needs proxies or bot evasion, stop.

### Token case sensitivity, which bites twice

Greenhouse, Ashby and SmartRecruiters resolve tokens case-insensitively, so a
crawl returns `OpenAI` and `openai` as separate candidates for one board — 17
such pairs in the first sweep, deduped in the report step. **Lever is the
exception**: `jobs.lever.co/Zeller` resolves and `/zeller` 404s, so Lever tokens
keep their case and must never be lowercased. Both `discover_boards.py` and
`run.py` encode this split.

## Board discovery, part two: an actual crawler

Common Crawl can only find a board it already fetched a URL for **on the ATS's
own domain**. Three kinds of board are invisible to it by construction, and
`src/reqtrace/crawl.py` plus `scripts/crawl_careers.py` exist to reach them:

- **Lever.** `candidates_lever.json` from the Common Crawl sweep contains
  **zero tokens**. That is not a tuning problem; CC barely indexes
  `jobs.lever.co` at all.
- **Embeds.** An employer who iframes their board
  (`boards.greenhouse.io/embed/job_board/js?for=<token>`) never produces a
  crawlable ATS URL. The token exists only inside *their* HTML, in a query
  string — and the CC-index regex reads `embed` out of that path and throws the
  real token away.
- **Two-part identities.** Workday is `tenant.wdN/Site`, Oracle is
  `host/CX_1`, Eightfold is `tenant/domain`. None is a path segment, so a URL
  index has nothing to lift out. `adapters/workday.py` already said these
  "come from careers-page crawling, never from slug guessing" — this is that
  crawling. Eightfold is the sharpest case: its identity needs the *employer's*
  domain, which a URL index never knows and a crawl always does, because it
  arrived from that employer's own site.

```bash
uv run python scripts/crawl_careers.py crawl --seeds unresolved --max-pages 400
uv run python scripts/crawl_careers.py crawl --domain canva.com --max-pages 20
uv run python scripts/crawl_careers.py crawl --seeds au,global --resume
uv run python scripts/crawl_careers.py report   # -> data/discovery/crawled_<vendor>.json
uv run python scripts/discover_boards.py validate --vendor lever   # reads both sources
```

`report` writes `crawled_<vendor>.json` in exactly the shape `harvest` produces,
and `validate` reads the **union** of the two. Deliberately separate files: one
sweep is 6,832 CC tokens and the other a few hundred crawled ones, and neither
may clobber the other. Nothing here touches `data/discovered_boards.csv`, so the
append-only adoption rule is protected by construction.

### What makes it finite

A general crawler has no natural stopping point. This one is *focused* in
Chakrabarti's sense, and three rules bound it:

1. **It starts from employers we already know** — the `domain` and `careers_url`
   columns Step 0 already collected. `--seeds unresolved` seeds only the 27 rows
   the audit never resolved, which is where the value is.
2. **The frontier is a priority queue, not a queue.** `score_link` decides which
   of a homepage's 300 links is worth one of a finite number of requests:
   `/about/careers` scores 195, `/blog/2024/why-we-are-hiring` scores 0 despite
   the word "hiring" in it, and off-site links score 0 because the fingerprints
   already read them out of the HTML without a request.
3. **A host is done the moment a fingerprint hits.** One token per employer is
   the goal, not a site map. This is the rule that turns "crawl the web" into
   "163 requests for 27 employers".

The unit of discovery is still a board, never a job. Crawling job links would
rebuild a job board — duplicates, dead links, no closure detection — which is
the thing this project exists not to be. The crawler finds the door; the
adapters walk through it.

When a homepage yields no careers-ish link at all (a JS shell), it falls back to
the conventional paths (`/careers`, `/join-us`, …) and to `sitemap.xml`, which
robots.txt often declares. Conventions, not guesses at private URLs.

### Politeness is structural, not a flag

robots.txt is fetched and obeyed per host including `Crawl-delay`; requests to
one host are serialised behind a minimum delay (`--concurrency` is across
*different* hosts); the User-Agent names the project; responses are
content-type filtered and byte-capped at 2 MB, streamed so an unexpectedly
enormous body is truncated rather than downloaded. `--max-pages` is a hard stop
and defaults low. In the run below, robots.txt disallowed one path and the
crawler simply did not fetch it. Same line `discover_boards.py` draws: public
pages, declared identity, **no proxies and no evasion, ever**.

Crawler state is resumable — the seen-set is the part that matters, because a
resumed crawl that re-fetches what it already has is rude twice over. robots.txt
is deliberately *not* persisted: it can change, and a fresh process re-reading
it is the correct behaviour. `--resume` restores per-host page counts too, so it
picks up unexplored *frontier*, not unexplored hosts: a host that already spent
its `--max-per-host` budget stays spent. Raise the budget to go deeper on one.

### First live run: the employers Step 0 could never resolve

27 seeds, 163 pages, 13 hosts resolved to a board:

| employer | vendor | token | verdict |
|---|---|---|---|
| AustralianSuper | oracle | `ejjl.fa.ap1.oraclecloud.com/CX_1` | **ingested: 39 AU roles** |
| Suncorp | oracle | `fa-evew-saasfaprod1.fa.ocs.oraclecloud.com/CX_1` | **ingested: 37 AU roles** |
| ANZ | successfactors | `anzbanking` | no adapter |
| Macquarie Group | avature | `mgl` | no adapter |
| CSIRO | successfactors | `CSIRO` | no adapter |
| Athena Home Loans | bamboohr | `athena` | no adapter |
| Marketplacer | bamboohr | `marketplacer` | no adapter |
| National Australia Bank | eightfold | `nab/nab.com.au` | real board, **0 AU roles** |

Both Oracle tokens went straight through the existing adapter — 76 Australian
roles from two employers Step 0 had left blank, on hosts (`ejjl`,
`fa-evew-saasfaprod1`) nobody could have guessed.

**NAB is the instructive row, and it is a warning about this table.** The crawl
found a genuine Eightfold tenant on `careers.nab.com.au`, and it is the wrong
board: that tenant is the India delivery centre, 0 of 267 roles in Australia.
NAB's Australian roles sit on a Clinch site behind an AWS WAF challenge, which
is where this project stops by its own rule. A found token is a *proposal* —
`validate` drops boards with no AU roles precisely so a confident fingerprint
cannot become a permanently wrong board in the seed list.

So of eight findings: two are coverage, five are intelligence about which suite
an employer runs (the precondition for ever writing that adapter), and one is a
real board that must not be adopted. The crawler cannot tell those apart, and
does not try to.

And on eight Lever/Greenhouse employers, the crawl produced `Zeller`,
`immutable`, `q-ctrl` and `eucalyptus` — **`data/discovery/validated_lever.json`
now exists**, and Common Crawl had produced zero Lever tokens to validate.
`Zeller` came back with its capital Z intact, which is the whole ballgame:
`jobs.lever.co/Zeller` resolves and `/zeller` 404s.

### False positives are still validation's job, not the crawler's

KPMG's careers page yielded `smartrecruiters:ni`, which answers
`200 {"totalFound": 0}` — the vendor's documented trap. `validate` drops boards
with no jobs, so it never reached the CSV. The crawler's job is to *propose*;
only a live vendor feed gets to decide a board is real. `plausible_token` is a
junk filter, not a judgement — it exists to avoid spending a request on
`bundle.js`, and it keeps its own blocklist separate from
`discover_boards.py`'s, because HTML junk (asset paths, framework chunks) and
URL-index junk are different populations.

Findings are keyed on **(vendor, token, seed)**, not on the token alone. `ni` is
the reason: two employers resolving to one token is either the same employer
reached under two hosts, or a careers page pointing at a board that isn't
theirs, and collapsing the rows would make those indistinguishable. The cost is
that `crawled_<vendor>.json` accumulates monotonically — a junk token proposed
once costs one validation request on every future sweep. `candidates_*.json`
from Common Crawl has had the same property since day one; if either file ever
gets expensive, prune against `validated_*.json`.

## The UI

```bash
uv run python -m reqtrace.web        # http://127.0.0.1:8765
```

A stdlib HTTP server and two static HTML files — no framework, no bundler, no
Node. Four endpoints (`/api/search`, `/api/stats`, `/api/runs`, `/api/pulse`)
and vanilla JS.

The two pages are aimed at two different readers, and the split is deliberate.
The front page is the **product**: someone looking for work, who does not care
how the index is fed. No board or adapter counts, no failure states, no ATS
vendor anywhere in the filters. Two pipeline facts did earn a place there, and
only because the page now makes claims that depend on them: a **swept N hours
ago** stamp in the masthead, and the date the index started watching for
closures. A chart of the last sixteen weeks has to say how current it is, and a
closures series has to say how far back it can see. Every other operational
figure still lives on `/runs`, which the search page does not link to.

Narrowing is a search box, a **dial**, and two lists. The dial is the sixteen-week
openings-and-closures chart, and dragging across it *is* the date filter — the
chart and the control are one object, which is why there is no separate "posted
this week" pill. The window it brushes is a pair of absolute dates rather than
an age, so an export read three days after it was built still filters to the
bars it was drawn against. The rail carries city and work type. The
data-roles/all-roles toggle is gone: the page states its scope in the masthead
and links out to the full AU set, instead of offering a control that halves the
index's identity on click. The current search is still written to the URL, so a
search is still a link, and links written before the dial existed still open the
way they were shared.

The pulse is role-level, from `posted_at` and `closed_at` — deliberately not
`board_runs`, whose churn series counts a board's first sight as new and would
therefore draw the week the index booted as the biggest hiring week on record.
The closures half carries a second caveat that the chart states outright:
`closed_at` records when *this* index noticed a role gone, so it cannot predate
the first sweep. Weeks that ended before then are hatched rather than drawn as
zero, because the absence of a measurement is not a measurement of zero. Fourteen
of the sixteen bars are hatched today, and they fill in as the log accrues.

`/runs` is the **ingest health** page. `board_runs` has logged a row per board
per pass since the first commit and nothing ever read it back; once ingestion is
scheduled rather than typed, that log is the only evidence the index is still
being fed. A board that started 404ing six weeks ago looks identical, from the
search page, to a board that genuinely has no open roles. It leads with how long
ago the last board was fetched, then rolls up each board's most recent run per
adapter, and separates three states that are easy to conflate: **failed** (the
fetch errored), **incomplete** (parsed fewer jobs than the vendor claimed, so it
is forbidden from closing anything — being read, but not retiring filled roles),
and **stale** (last run succeeded; nothing has run it since, which is how a dead
schedule shows up). Status colour is always paired with the word, since
green/amber/red is exactly the palette a colourblind reader cannot separate.

Search is FTS5 over title + body + company name. **The UI is SQLite-only for
now** — `search.py` is sqlite3 throughout, so the server refuses to start with a
clear message if `DATABASE_URL` is set. Ingestion already honours Postgres; the
query layer needs a tsvector path before the UI can follow, and the indexes are
waiting for it in `schema_postgres.sql`. Filters are the four a job hunter
actually narrows on — data roles (on by default) or all, city, work type, and
posted this week — and the current search is written to the URL, so a search is
a link.

Three filters were cut rather than restyled. **Published salary**: 57 of 3,341
open AU roles carry a structured band, so both the filter and the salary sort
returned a screenful and then silently fell back to dates — a control that
looks broken is worse than one that is absent. Salary still renders on the
roles that have it. **Employer** and **ATS vendor**: the search box already
matches on company name, and which applicant tracking system an employer
happens to license is not something a candidate is shopping for. The city
picker is a fixed metro list intersected with the index's own values, because
the raw column holds `Barangaroo`, `North Ryde` and `MOUNT WAVERLEY` alongside
the capitals.

Two things the first screenshot exposed, both now fixed:

- **Every role read "today"**, because `first_seen_at` records when *this index*
  first saw a job, not when the employer posted it. Vendors all publish their own
  date (`first_published`, `publishedAt`, `releasedDate`), so there is now a
  `posted_at` column and the UI sorts and labels on it. `first_seen_at` keeps its
  original meaning. Of 2,724 open AU roles: 87 posted today, 384 this week,
  1,334 this month.
- **The data-roles filter matched "Senior Tax Analyst" and "Cyber Security
  Analyst".** Bare "analyst" is far too broad for an index this size, so it now
  only counts when it is not one of ~20 excluded qualifiers (tax, payroll,
  cyber, procurement, audit…). Strong signals — data scientist, ML, analytics,
  quantitative, econometric — always match.

## Layout

```
data/companies_seed.csv      hand-curated employer list (edit me)
data/step0_ats_audit.csv     the Step 0 deliverable
scripts/audit_ats.py         slug probe + careers-page fingerprinting
scripts/audit_followup.py    deep crawl for stragglers, false-positive rejects
scripts/fetch_fixtures.py    complete board dumps + trimmed test samples
scripts/discover_boards.py   Common Crawl -> candidate tokens -> validated AU boards
scripts/crawl_careers.py     focused careers-page crawl -> the tokens CC cannot see
src/reqtrace/crawl.py       the crawler: robots, frontier, scoring, ATS fingerprints
scripts/probe_meta.py        one-off: Meta sitemap + JSON-LD sweep (3 AU roles)
scripts/probe_nab.py         one-off: is NAB's AU board ingestible (no — WAF)
src/reqtrace/search.py      FTS5 / tsvector query layer + filters
src/reqtrace/runs.py        reads board_runs back: freshness, coverage, failures
src/reqtrace/web.py         stdlib server, three JSON endpoints
src/reqtrace/static/        two vanilla HTML pages, no build step
scripts/install_autorun.py   installs/removes the daily launchd agent
scripts/autorun.sh           what the agent runs: one --vendor all sweep + export
scripts/export_static.py     site/ — the same pages with no Python behind them
scripts/migrate_to_postgres.py  carries the SQLite history into Postgres
.github/workflows/sweep.yml  the same sweep on a runner; needs DATABASE_URL
tests/test_postgres.py       the Postgres path, against a real server
data/discovered_boards.csv   newly found AU boards, ranked by AU data roles
fixtures/samples/            committed, test-sized
fixtures/careers/            the embed shapes careers pages use, for the crawler
fixtures/raw/                full dumps, git-ignored
src/reqtrace/adapters/      one module per vendor; failures are isolated
```

## The seven adapters

| | boards | jobs | AU open | AU data | AU w/ salary |
|---|---|---|---|---|---|
| greenhouse | 200 | 27,091 | 947 | 73 | 0 |
| ashby | 95 | 10,581 | 499 | 107 | 57 |
| smartrecruiters | 32 | 6,028 | 1,278 | 45 | 0 |
| workday | 12 | 6,796 | 333 | 37 | 0 |
| lever | 7 | 458 | 58 | 8 | 0 |
| **total** | **346** | **50,954** | **3,115** | **270** | **57** |

Workday is what makes this an index of the Australian job market rather than of
Australian tech scaleups: CommBank alone contributes 175 AU roles including
"Senior Data Scientist — ML & GenAI" and "Lead Data Engineer (Snowflake, dbt)",
plus Telstra, Accenture, Cochlear, Nine, and Visa/J&J/Pfizer/Shell/Unilever/
Coca-Cola/Novartis globally. Lever adds Palantir, Spotify, Deputy, Zeller,
Immutable, Kogan and Q-CTRL.

Each vendor needed a different completeness signal, and getting this right is
the whole ballgame for closure detection:

- **Greenhouse** — `meta.total` reconciled against the parsed count.
- **SmartRecruiters** — pages at 100; `len(content) == totalFound`. Never the
  status code: an unknown company answers `200 {"totalFound": 0}`.
- **Ashby** — not paginated, so one request *is* the whole board. The envelope
  shape is the signal; a malformed response raises rather than parsing as an
  empty (i.e. fully-closed) board.
- **Lever** — returns a bare JSON array, unpaginated; a list is the whole board.
- **Workday** — pages at 20 (asking for 50 returns *nothing*, it does not clamp),
  reconciled against `total`... except `total` cannot be trusted, see below.

**Workday completeness took two attempts to get right, and the first attempt
was wrong in an instructive way.** Asking for a page past the reported end
returns postings — so the obvious conclusion is "the total was a cap". That is
false: Workday *clamps* an out-of-range offset and re-serves a page you already
have. Judging on "the probe returned rows" marked 10 of 12 boards incomplete and
silently disabled closure detection for the whole vendor. The probe now compares
`externalPath` values and only treats genuinely **new** postings as evidence of
truncation.

Separately, Workday's public search stops counting at 2,000. A board reporting
exactly that (Accenture) cannot be shown to be whole, and if it is truncated the
visible window shifts as roles are posted — retiring jobs that are still open.
Such boards are ingested and updated but never close. Stale rows are
recoverable; a corrupted `closed_at` history is not.

Vendor quirks worth knowing:

- **Ashby's `isRemote` is a trap.** It is `True` on 260 of the recorded jobs
  while only 11 have `workplaceType: "Remote"` — it counts hybrid as remote.
  `workplaceType` is the honest field, and separating true remote from
  hybrid-labelled-remote is the highest-value filter in the whole index.
- **Ashby publishes structured compensation** (`minValue`/`maxValue`/
  `currencyCode`/`interval`), so hard problem #3 needs no regex — 57 AU roles
  carry a real salary band. Only the `Salary` component is used; folding in
  Bonus/Equity/Commission would corrupt the range.
- **SmartRecruiters listings carry no description at all** — each is a separate
  request. Bodies are fetched only for AU roles, which mirrors the storage
  policy and turns an N+1 over the whole board into an N+1 over ~3% of it.
- **Workday tokens are not guessable** — a board needs tenant + `wd{N}` host +
  site path, so `board_token` encodes all three (`cba.wd3/CommBank_Careers`).
  They come from careers-page crawling only. Facet ids are tenant-specific too:
  the country GUID that filters CommBank to Australia returns nothing on NVIDIA,
  which is why the adapter pages the whole board and filters locally.
- **Some Workday boards publish no location at all.** Accenture returns an empty
  `locationsText` for all ~2,000 postings, which silently hid every Australian
  role it has. Unknown location now counts as "worth opening", bounded by a
  per-board detail cap so one host does not get an impolite fan-out.
- **Lever tokens are case-sensitive** — `/Zeller` resolves, `/zeller` 404s — and
  it is the only vendor here where that is true.
- **SmartRecruiters gives seniority and function for free** (`experienceLevel`,
  `function`), which the local-model enrichment step won't need to infer.

## Global employers

`data/global_ats_audit.csv` audits 50 large multinationals. Only 6 sit on the
four "clean JSON" ATSs — enterprises buy HR suites: Eightfold 7, Workday 7,
Avature 6, Phenom 4, SuccessFactors 3, iCIMS 1, and 16 run bespoke portals
(Apple, Meta, Google, Amazon, Microsoft, IBM, Tesla, Rio Tinto…).

Adding Workday and Lever converted 9 of those into indexed boards.

**Correction on Apple and Meta.** An earlier pass concluded both were
unreachable without defeating bot checks. That was wrong for Meta, and the
mistake was mine: `metacareers.com/jobsearch/sitemap.xml` returns 400 to a
browser-style `Accept` header and **200 to `Accept: application/xml`**. It is
also declared in their own robots.txt as a sitemap, whose only `Disallow` for
`*` is `/*cursor=`. It serves 900 job URLs, and each job page carries a full
JSON-LD `JobPosting` block — title, `datePosted`, `jobLocation` with a postal
address. That is the sanctioned aggregator route, and it works with an honest
user agent; no spoofing is involved.

The catch is yield, and the full sweep settled it. All 900 pages were fetched
(`scripts/probe_meta.py`, 897 parsed cleanly): **3 Australian roles** — an
Enterprise Technical Sales Specialist, a Signals Intelligence Specialist and an
Agency Partner. None is a data role. The country spread is US 1,972, GB 40,
SG 39, IN 24, IE 23. So Meta is reachable by a fully sanctioned route and worth
almost nothing for this index; no adapter was written.

Worth noting either way: the same robots.txt carries a prose notice
that "collection of data on Facebook through automated means is prohibited
unless you have express written permission". The machine-readable directives
permit these paths and advertise the sitemap; the prose is a broader claim. That
tension is a judgement call, not a technical one.

**Apple remains out of reach by this route** — but for ordinary reasons, not
bot-blocking. `jobs.apple.com` publishes no sitemap (apple.com/robots.txt
declares shop, newsroom, retail and today only), its role pages carry no JSON-LD,
and the AU search page server-renders just 5 roles, all retail. Everything else
is client-rendered.

Worth knowing: having the adapter is not the same as having the roles. X, LVMH
and McDonald's *are* on SmartRecruiters, but those boards are corporate-HQ only
and carry zero Australian jobs; their local operations hire elsewhere.

### Eightfold / Avature / Phenom — probed

**Eightfold: a clean public feed exists.** Its `robots.txt` explicitly allows
`/api/apply` and `/api/pcsx`, and the working endpoints are

```
list   https://{tenant}.eightfold.ai/api/pcsx/search?domain={domain}&start=&num=   (num caps at 10)
detail https://{tenant}.eightfold.ai/api/apply/v2/jobs/{id}?domain={domain}
```

`data.count` gives the total, `location=Australia` filters server-side, and the
detail response carries `job_description`, `location`, `t_create` and
`department`. Note `/api/apply/v2/jobs` (the list form) 403s with "Not
authorized for PCSX" — only `/api/pcsx/search` works for listing.

The AU yield is the catch:

| tenant | total | AU |
|---|---|---|
| citi | 3,366 | 18 |
| nvidia | 2,698 | 10 (already indexed via Workday) |
| astrazeneca | 837 | 10 |
| paypal | 107 | 2 |
| qualcomm | 1,957 | 1 |
| **nab** | 267 | **0** |

So an adapter is ~31 net-new AU roles. And NAB — a major Australian bank — has
**zero** Australian roles on its Eightfold tenant: that board is their India
delivery centre, and their AU hiring runs on something else entirely.

**Phenom and Avature: no clean feed found.** Both are SPAs that load jobs by
XHR. Phenom embeds a `phApp.ddo` blob but its jobs array is empty in the
server-rendered page, and Avature's portal paths differ per tenant with no JSON
variant responding. Reaching either would mean reverse-engineering private
endpoints, which is the same line as Apple and Meta.

**Eightfold was built, and it was not worth much.** It works correctly — 31
Australian roles across Citi, AstraZeneca, PayPal and Qualcomm, all complete,
all with descriptions. But almost none of them are data roles: what the filter
catches at Citi is "Loan Doc & Proc Analyst" and "Metals & Mining Research
Analyst", i.e. finance-operations titles. Two boards were deliberately left
unregistered — NVIDIA (already indexed via Workday, so registering both would
duplicate every requisition) and NAB (its Eightfold tenant is the India delivery
centre: 0 of 267 roles in Australia).

The lesson repeats the SmartRecruiters one: reachability is not relevance. Three
platforms probed, one clean feed found, and its yield of genuine data roles is
roughly zero.

## The big four banks — investigated

| bank | system | status |
|---|---|---|
| **Westpac** | Oracle Recruiting Cloud | **clean public REST API, 140 AU roles, 16 of them data** |
| NAB | Clinch (PageUp) site; Eightfold offshore | AU board found — 79 roles, 3 of them data — reachable only at ~90s/page behind an AWS WAF |
| Macquarie | Avature | HTML only, no JSON/RSS variant responds |
| ANZ | SuccessFactors | not probed for a feed (its Workday tenant does not exist) |

**Oracle Recruiting Cloud is the best remaining target by a distance.**

```
list https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
       ?onlyData=true&expand=requisitionList
       &finder=findReqs;siteNumber={site},limit=50,offset={n}
```

Westpac's host is `ebuu.fa.ap1.oraclecloud.com`, site `CX_1`. It paginates at 50
(Workday manages 20, Eightfold 10), reports `TotalJobsCount` for the
completeness check, and every row carries `PrimaryLocationCountry`, a real
`PostedDate`, `Department`, `JobFamily` and `WorkplaceType`. 140 of its 148 roles
are Australian — the best AU density of any board found so far — including
*Senior Data Scientist – DDAI*, *Senior Quantitative Analyst, AI Models*,
*Manager, AI Models* and *Data Analytics Manager – Financial Crime Intelligence*.
TPG Telecom is on the same platform.

Not yet solved: the per-job detail finder. Every `recruitingCEJobRequisitionDetails`
syntax tried returns 400, so an Oracle adapter would index title, location, date
and department without a description body until that is cracked.

**NAB, revisited: the Australian board exists, and it is walled off.** The
Eightfold tenant really is offshore-only — 267 postings, all Vietnam (183), India
(92) and Japan (1) — but the claim that `careers.nab.com.au` points at that
tenant was wrong. It serves a **Clinch** board (Clinch is PageUp's career-site
product; the challenge page's own `awsWafCookieDomainList` names
`clinchtalent.com` and `career-pages.com`), and it links to Eightfold only for
"career opportunities in China, France, Hong-Kong, Japan, Singapore, UK, India,
Vietnam and the US".

`scripts/probe_nab.py` settles what can be taken from it, reading only what
robots.txt declares (`Sitemap: /sitemap.xml`, `Crawl-delay: 5`, `Disallow:
/api/`) under an honest `reqtrace/0.1` user agent:

| check | result |
|---|---|
| sitemap | 79 job URLs, served 200 |
| is that the whole board | yes — the site's own pagination is 3 pages x 30, and every URL it shows is in the sitemap |
| job pages at the declared `Crawl-delay: 5` | **0 of 8 served** — HTTP 202 and an AWS WAF JS challenge (`gokuProps`) |
| job pages at 90s spacing, after a five-minute cool-off | **3 of 3 served**, 200 and ~100 KB each |
| data roles | 3 of the 77 title-bearing slugs: *AI Scientist*, *Senior AI Scientist*, *Principal AI Scientist*, Melbourne/Sydney (the other 2 URLs are opaque UUIDs) |

The WAF is stricter than the site's own robots.txt: 5 seconds is what NAB asks
for and 5 seconds is what gets challenged. Once tripped it stays tripped for
minutes — the first `--slow-retest`, run straight after a challenged sweep,
returned 202 three times; the same three URLs an hour later returned 200 three
times. So the board is not sealed, it is *expensive*: at one request every 90
seconds a full sweep of 79 pages is two hours of wall clock, against a daily
`--vendor all` run that does 353 boards.

The sitemap on its own carries a URL and a `lastmod` and nothing else — no title
except a location-padded slug, no requisition id, no description. Identity would
have to come from the job page, and it is there: one page fetched before the WAF
closed carries both a JSON-LD `identifier.value` (`107817cf…`, the platform's own
uid) and a visible requisition number (`798283`). The slug is not a substitute —
it is title+location-derived, so a re-post with a different location set mints a
new one, and *one record per role* would stop being true by construction.

So NAB stays out on economics rather than on principle — the same
reachability-is-not-relevance verdict SmartRecruiters and Eightfold already
earned. Two hours of crawl a day, for a board whose entire data yield is three
AI Scientist roles, is worse value than any adapter already written. What would
change it: NAB publishing to a feed that wants to be read, or enough Clinch
tenants turning up in the discovery crawl that one adapter amortises across
several boards.

## Next

1. ~~**Scheduling.**~~ Done: `--vendor all` plus a launchd agent, daily at
   05:30, with `/runs` to show whether it is still happening. GitHub Actions
   still waits on a Postgres — the runners are ephemeral and cannot see
   `data/jobs.db`. The remaining gap is that nothing *tells* you when a sweep
   degrades; you have to open the page.
2. NAB / Macquarie / ANZ: the *tokens* are now found — `nab/nab.com.au`
   (Eightfold, and there is already an adapter for it), `mgl` (Avature),
   `anzbanking` (SuccessFactors). NAB's token is the *offshore* Eightfold board
   — 0 AU roles — so it stays unregistered; its 79 Australian roles sit on a
   Clinch site behind an AWS WAF that only serves at ~90s per page, which the
   section above prices out. Avature and SuccessFactors still expose no
   JSON feed found so far, so those two need adapters, not discovery.
3. Deploy: the Actions workflow and the Postgres port are done and tested;
   what is left is provisioning Neon and adding the `DATABASE_URL` secret,
   which needs your account. Until then the laptop is authoritative.
4. Title → seniority/function via a local model, gated on `content_hash`
   (SmartRecruiters already supplies both, so this is Greenhouse/Ashby only)
5. Search API over the tsvector index, then the thinnest possible UI
6. `board_runs` logs per-run totals but not per-job churn; the `first_seen_at` /
   `closed_at` columns already carry the hiring-signal series, so a time-to-fill
   view is a query away rather than a schema change

Known gaps: `remote_type` is `unknown` for many Greenhouse roles because its
location strings rarely say; Ashby and SmartRecruiters expose it properly (of
2,735 open AU roles: 331 remote, 479 hybrid, 997 onsite, 928 unknown).

Known inefficiency: every Greenhouse description is sanitised and text-extracted
on each run, then ~96% are discarded at the store boundary by the AU-only policy.
Harmless today; if the daily cron gets slow, that is where the time goes, and the
fix is to thread the AU decision into the mapper instead of deciding in
`_upsert`. It was left alone deliberately so `Store(descriptions="all")` keeps
working. `AU - HQ - NSW`
resolves to country AU but no city — a state column would fix it, but that's a
schema change, so it's deferred to the normalisation pass.
