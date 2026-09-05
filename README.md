# ReqTrace

A search index over Australian data / analytics / ML roles, pulled from
employers' Applicant Tracking Systems rather than from job boards.

Identity is the ATS's own requisition id, so one record per role and duplicates
cannot occur by construction. Apply links point at the employer. And because
every run pulls the *full* board and diffs it, a role that gets filled
disappears — the `first_seen_at` / `closed_at` trace the index accumulates is
the thing it is named for, and is worth more than the listings.

**353 boards · 51,232 jobs · 3,356 open Australian roles · 279 of them data
roles · 7 ATS adapters · 105 tests**

Browse a snapshot at <https://sidharthjoly.com/ReqTrace/>; ingest health at
<https://sidharthjoly.com/ReqTrace/runs.html>.

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

python scripts/install_autorun.py            # sweep every board daily at 05:30
python scripts/install_autorun.py --status   # is the schedule alive, and what did it do
python scripts/install_autorun.py --uninstall
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

> **The index currently runs on SQLite, not Postgres.** There's no Postgres on
> this machine and provisioning Neon needs your account, so `schema_postgres.sql`
> is written and wired but has never been executed. Treat the Postgres path as
> untested until someone points a `DATABASE_URL` at it — and note that ingestion
> honours it while the UI does not yet.

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

**Live: <https://sidharthjoly.com/ReqTrace/>** — served from the `gh-pages`
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

**Open Australian roles only** — 3,356 rows, ~3MB, well under a megabyte gzipped.
The full index is 51k rows and 28MB of JSON, which is not a page, it is a
download.

Two honest limits, both stated on the pages themselves rather than left to be
discovered:

- **Search is narrower.** The live index runs FTS5 over full descriptions; the
  export carries a 320-character preview, so a query matching only deep in a body
  finds nothing. Measured: `data scientist` returns 55 live and 31 static, and
  the 54 AU roles whose only match is past the preview are exactly the gap.
- **It is a snapshot.** Stale the moment the next sweep lands, so both pages
  carry the export timestamp, and `/runs` says outright that its "N hours ago"
  figures count from the export rather than from now.

The daily sweep re-exports `site/` when it finishes. It does **not** publish:
that pushes to a remote, and a daily unattended push is a bigger commitment than
a daily fetch. `REQTRACE_PUBLISH=1` in the plist opts in.

`--publish` force-pushes an orphan commit to `gh-pages` rather than committing
the export to `main` — the snapshot is regenerable, and 3MB of JSON a day would
be a gigabyte of git history a year. Pushing that branch is what enabled Pages
in the first place; there was no separate setup step. `jobs.json` is 3.1MB on
disk and **361KB over the wire**, since Pages gzips it.

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

## The UI

```bash
uv run python -m reqtrace.web        # http://127.0.0.1:8765
```

A stdlib HTTP server and two static HTML files — no framework, no bundler, no
Node. Three endpoints (`/api/search`, `/api/stats`, `/api/runs`) and vanilla JS.

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
waiting for it in `schema_postgres.sql`. Filters are the ones a job hunter actually narrows on:
city, remote type, published salary, freshness, employer, and a data-roles
toggle that is on by default.

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
scripts/probe_meta.py        one-off: Meta sitemap + JSON-LD sweep (3 AU roles)
src/reqtrace/search.py      FTS5 / tsvector query layer + filters
src/reqtrace/runs.py        reads board_runs back: freshness, coverage, failures
src/reqtrace/web.py         stdlib server, three JSON endpoints
src/reqtrace/static/        two vanilla HTML pages, no build step
scripts/install_autorun.py   installs/removes the daily launchd agent
scripts/autorun.sh           what the agent runs: one --vendor all sweep + export
scripts/export_static.py     site/ — the same pages with no Python behind them
data/discovered_boards.csv   newly found AU boards, ranked by AU data roles
fixtures/samples/            committed, test-sized
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
| NAB | Eightfold | tenant confirmed **offshore-only** — 0 AU |
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

**NAB is settled, and the answer is that its Eightfold board is not the one.**
All 267 postings are Vietnam (183), India (92) and Japan (1); `careers.nab.com.au`
points at exactly this tenant. So either NAB has no open Australian roles, which
is implausible, or its AU hiring is served somewhere this crawl has not found.

## Next

1. ~~**Scheduling.**~~ Done: `--vendor all` plus a launchd agent, daily at
   05:30, with `/runs` to show whether it is still happening. GitHub Actions
   still waits on a Postgres — the runners are ephemeral and cannot see
   `data/jobs.db`. The remaining gap is that nothing *tells* you when a sweep
   degrades; you have to open the page.
2. NAB / Macquarie / ANZ still unresolved for AU roles (Avature and
   SuccessFactors respectively; neither exposes a JSON feed found so far)
2. Deploy: point `DATABASE_URL` at Neon, then Actions can do ingestion too.
   Until then the static export is the only thing that leaves this machine, and
   it leaves as a snapshot rather than a service.
3. Title → seniority/function via a local model, gated on `content_hash`
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
