# Measuring CI

What `scripts/ci_audit.py` reports, how far to trust each number, and how to
dig further.

## What the script reads

Through `gh api`, read-only: the repository's completed workflow runs in the
window, the jobs and steps of each run, the logs of a few recent jobs, and
what the Actions cache holds.

GitHub has no way to read the jobs of many runs at once, so the script makes
one API request per run, plus a few to list the runs, two for the cache
store and about thirty for logs: roughly 330 requests for the default 300 runs, and it prints the count
when it finishes. A personal token allows 5,000 requests an hour on most
plans and the token a workflow gets allows 1,000, shared with everything else
that uses them, so:

- Keep the same `--save-dir` for every run of an audit. A run that is already
  saved is not requested again, so refocusing with `--workflow`, `--event` or
  `--branch`, or measuring again a few days later, costs a handful of requests
  instead of hundreds.
- On a busy repository the default 300 runs may cover only a day. Raise
  `--max-runs` until the window spans a working week, but in steps and with the
  same `--save-dir`: 3,000 runs is 3,000 requests and five or six minutes.
- If the limit runs out mid-fetch, the runs it could not read are dropped from
  every number *and from the window*, and the report says so and when the
  limit resets. Rerun then with the same `--save-dir` and it fetches only what
  is missing.

Point `--from-dir` at a saved directory to re-analyze with no requests at all,
or read the JSON files
yourself for a question the report doesn't answer. `--json` writes every
table plus one record per PR commit (branch, account, outcome, wait, billed
minutes, cost), which is the starting point for pricing a different pipeline
shape or splitting a number the report gives only in total.

## Reading the report

**Estimated savings** opens with two numbers and shows a table for each.

*Keeping your runners* prices the pipeline fixes the script can price from
the data alone, in a fixed order, each on the minutes the ones before it
leave:

1. push runs that duplicated a pull_request run (every job of them)
2. superseded runs (the billed minutes after the newer commit arrived)
3. jobs whose steps are all setup
4. flaky failures: executions that failed and then passed on a re-run of
   the same commit (see Flaky jobs below)
5. dependency caches that miss: 60% of install-step time in jobs whose
   sampled logs show a miss or no cache. This one rests on an assumption
   (a warm install takes 40% of a cold one) and says so
6. macOS/Windows matrix legs that never failed alone, on pull_request runs

*Switching runners too* adds three runner changes, priced after all of the
above so the first number never depends on them:

7. Linux jobs under 30 seconds on GitHub's 1-vCPU runner (`ubuntu-slim`).
   It runs unprivileged, so it can't take jobs that need Docker
8. Linux x64 jobs on arm64 at the provider they already use. This assumes
   every one of them runs on arm64, which is rarely true of all of them:
   native dependencies and container images need arm builds
9. every job at the lowest managed list price for its operating system,
   from the provider table. Linux, Windows and macOS are each taken at
   their own lowest price, which can mean more than one provider

The provider table lists each provider's monthly total if it took the jobs
it lists a machine for. Linux and Windows are matched by vCPU count. macOS
machines differ too much between providers for that, so they are matched by
class (standard, about 6 cores, about 12 cores); a `~` marks a size that was
scaled or replaced by the next one up. Rates come from a table inside the
script, dated when it was read. Everything is the window's usage scaled to
30 days.

What it can't know: whether a superseded run is a deploy that must finish,
whether a short job can be folded, whether a needs chain is real, how fast
another provider's machines are, and anything about plan fees, free tiers or
cache charges. Jobs on runners with no known price are left out entirely, so
pass `--price` first. Treat every row as a lead to confirm, exactly like a
signal, before it goes into the report.

The **Wait** line compares today's wait for a passing commit with its
longest single job, which is the floor if nothing had to wait for anything
else. Real dependencies (a deploy that needs a build) keep the actual figure
above that floor.

**PR feedback time** is per commit: from the push to the last job of every
`pull_request` workflow finishing. Commits whose runs were cancelled or
re-run are left out so they don't distort it. Timing starts at the first
run's start timestamp, which for `pull_request` events follows the push
within seconds; the time before workflows start and human review are not in
it.

**Required checks only** is the merge gate: the same commits, timed to the
last job whose check-run name is a required context. The script reads the
contexts from branch rules itself (rulesets, then classic protection), and
`--required-checks A,B` overrides them. "Finishes last" names the job that
sets this number — the one to speed up next. Quote this figure as the merge
wait and the headline as everything a push starts; a wide gap between the two
is previews and bots, which cost money but block nothing.

It is reported twice because passing and failing commits are different
populations. A commit that fails often stops early, so when many commits fail
the all-commits median understates the wait. Quote **commits that pass** as
the wait for a green PR, and the failing commits' "hear about it after" time
as how long a failure takes to surface. `--budget-minutes` compares against
the passing figure.

**Where a passing commit's wait goes** follows the job that finishes last on
each passing commit and averages each part, so the parts add up:

- *before that job is created*: time spent waiting on earlier jobs. Large
  values mean a `needs:` chain is making parallel work serial.
- *queue*: job created to runner picked it up. This is runner capacity, a
  provider's start time, or a `concurrency` group holding the job.
- *setup*: steps whose names look like checkout, tool setup, cache, install.
- *work*: everything else.

Setup versus work is a guess from step names. The "Slowest steps" table shows
the class of each step; if a step is misclassified, redo the split by hand
before quoting it.

**Billed minutes** round every job up to a whole minute, as GitHub does. "Raw
min" in the runner table is unrounded; the gap is what rounding costs. When
third-party runners are priced and the gap matters, the summary adds an "if
the provider bills by the second" line — the same rates on raw seconds.
Providers don't all publish their rounding rule; reconcile with an invoice
before leaning on either figure, and remember the rounding and tiny-job
savings rows exist only under per-minute billing.

**Duplicate work** measures three shapes inside each run: a job whose every
command (steps GitHub named by their literal command) also ran in a bigger
job of the same run, the same command in two jobs, and the same step name
twice inside one job. A name can hide different inputs — a deploy composite
invoked twice with different arguments is usually intentional — so every row
is a lead to confirm in the YAML. Only whole duplicated jobs and repeated
*setup* steps are priced in the savings.

**Estimated cost** uses GitHub's list price for standard GitHub-hosted
runners. It is not the bill:

- included plan minutes are not subtracted
- public repositories get standard runners free (the script detects this)
- larger runners, third-party runners and self-hosted runners have labels the
  owner chose, so they show as unpriced until you pass
  `--price LABEL=USD_PER_MIN`. Read the rate from the provider's pricing page
  today, say where each number came from, and check whether that provider
  rounds each job up to a minute as GitHub does; if you can't confirm it,
  say the rounding is assumed
- the monthly figure extrapolates the window; a window with a release or a
  holiday in it will mislead. When the last days of the window hold almost
  none of the minutes, the report says so: the repository may have gone
  quiet, in which case the month it projects is a pace that has stopped.
  Then report two figures, the active stretch and what still runs on a
  schedule, and say which one the savings are measured against

When the user can open the billing page (Settings → Billing → Usage), ask for
the Actions line or the usage CSV and reconcile. When they can't, present the
dollars as a list-price estimate.

**Work that bought nothing** rows overlap; never add them up. Failed runs
include the ones that caught real bugs, which is CI doing its job. The
superseded row is what cancelling each run at the moment a newer commit
arrived would have saved, in billed minutes and dollars, over `pull_request`
runs and `push` runs on non-default branches. Dollar figures here use each
job's own runner price, so they are not minutes times one rate.

**Jobs** shows a matrix job's failure rate per run (the job failed if any leg
did), not per leg; the Matrix legs table has the per-leg counts.

**Flaky jobs** reads the earlier attempts of every run that was re-run. A
job that failed in one attempt and passed in a later attempt of the same
commit, with no code change in between, is flaky by definition. The table
gives the job, how often, the step that usually failed, and the billed
minutes those failed executions used. Jobs that fail within seconds are left
out: those are gates reporting another job's failure. The step is as far as
the API goes; the test names are in the failed attempt's log
(`gh run view <run-id> --attempt 1 --log-failed`). Earlier attempts are real
billed minutes that no other table counts; the savings estimate includes
them. It sees only failures someone chose to re-run: a flaky failure that
was fixed by pushing again shows up as a CI round instead. When the earlier
attempts can't be read (expired, or the API quota ran out), the report says
flakiness is understated — say the same in the audit instead of calling the
pipeline flake-free.

**Matrix legs** lists each leg of the heaviest matrix jobs with its cost and
how often it *failed alone*: runs where that leg failed and every sibling
passed. That count is the evidence for whether a platform or version leg
earns its place on PRs. Zero in a two-week window is a lead, not proof; look
at a longer window (`--days 90 --workflow <name>`) before proposing to move a
leg.

**Who triggers PR runs** groups PR commits by the account that triggered
them, with rounds per PR and the share of commits whose CI failed. Agents
running under a bot account show up here; agents pushing with a person's
token are counted as that person.

**CI rounds per PR** counts distinct commits per PR branch that triggered CI
inside the window, so PRs that straddle the window edge are undercounted.

**Cache evidence** comes from log lines of a small sample of recent successful
jobs (`--logs` per job, for the `--log-jobs` heaviest jobs). It recognizes
`actions/cache` and the `setup-*` actions, `Swatinem/rust-cache`, Turborepo
and Nx task summaries, and Docker `CACHED` layers. No hits and no misses means
no recognized cache step ran, not that the job is uncached; check the YAML.
Logs expire (90 days by default) and need read access. Raw log bodies carry
terminal escape sequences, which `gh api` refuses to print, so the script
fetches them over plain HTTP with the CLI's own token and strips the codes
before matching; when none could be read, the report prints the first fetch
error rather than guessing at the cause.

**Cache storage on GitHub** is read directly (two requests): the store's
total size against the 10 GB per-repository limit, and its hundred biggest
entries. An entry that was never restored after it was saved is a key that
changes every run — a miss in disguise — and a store near its limit evicts
oldest-first, so caches can miss even with correct keys. Entries younger
than a day are left out of that count; they haven't had a fair chance yet.
Third-party runners often route `actions/cache` to their own backend, which
this list does not see, so an empty list there says nothing about hits.

**Signals** are thresholds over the numbers above. They are leads. Confirm
each one in the workflow file before it goes into a finding.

## Noise

The same job on the same commit can vary by tens of percent. Use medians over
many runs for every claim, and p90 for what a bad day looks like. Don't
compare a single run before a change to a single run after it. If a window
has fewer than about 20 PR commits, widen it (`--days`) before drawing
conclusions.

Split populations that differ. If some PRs run a small selected subset and
others run everything, one median over both describes neither; use
`--workflow`, `--branch` or the raw JSON to separate them.

## Digging further

- **One job's log:** `gh run view <run-id> --log --job <job-id>`, or the
  `logs/` directory of `--save-dir`.
- **Inside a composite action:** the report shows a composite action (a
  shared "setup" action, say) as one step. Its job log has a line per inner
  step, `##[end-action id=…;duration_ms=…]`, which splits the time into
  tool setup, cache restore and install. Inside an install, the timestamps
  around the package manager's "done" line and its final "Done in" line show
  how much is linking and how much is postinstall scripts.
- **Is a failure rate one bad PR:** the "What fails" table says how many of a
  job's failures are on a single branch. A bot or agent that keeps pushing
  to a failing PR can make a healthy job look flaky.
- **Which tests are slow:** the test runner's own timing output
  (`references/tests-and-tools.md`), not step durations.
- **Is a job CPU-bound or waiting:** add a temporary step that prints
  `nproc`, and wrap the heavy command with `/usr/bin/time -v` (Linux) to get
  CPU-seconds against wall time. CPU-seconds near wall × cores means more
  cores will help; far below means it is waiting on I/O, a service, or sleeps,
  and a bigger machine won't.
- **More of the cache store:** the report's "Cache storage on GitHub"
  section covers the hundred biggest entries; `gh cache list` pages through
  the rest.
- **Required checks:** `gh api repos/{owner}/{repo}/branches/{branch}/protection`
  or `gh api repos/{owner}/{repo}/rulesets`, which need admin read. Otherwise
  ask which checks are required before proposing to move or rename a job.

## No API access, or not GitHub Actions

The script needs either the `gh` CLI, signed in, or a token in `GH_TOKEN` or
`GITHUB_TOKEN` (a fine-grained token with read access to Actions and
metadata on the repository is enough). When neither can be arranged where
you are running, ask the user to run the script and paste the report, or to
export the data and share the directory:

```bash
python3 ci_audit.py --save-dir ci-export          # the user, where gh works
python3 ci_audit.py --from-dir ci-export --json /tmp/ci-audit.json   # you
```

With saved data every log is local, so raise `--logs` (for example
`--logs 50`) to read more of them for cache evidence.

For GitLab CI, CircleCI, Buildkite and others the script doesn't apply, but
the method does. Collect the same facts from that system's API or UI for the
last hundred or so pipelines on merge requests: wall time per pipeline, per
job the queue time, setup time and run time, the runner type and its price,
cache hit lines in logs, cancelled and failed pipelines, and pipelines per
merge request. Then continue from Step 2 of `SKILL.md`. The reference files
use GitHub Actions syntax in examples; translate the idea, not the YAML.
