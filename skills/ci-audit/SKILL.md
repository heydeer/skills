---
name: ci-audit
description: Audit this repository's CI for speed and cost. Measures recent GitHub Actions runs with a bundled read-only script, reports what could be saved each month with and without switching runners, and makes the fixes the user picks as small PRs. Runs only when the user invokes it.
disable-model-invocation: true
---

# CI audit

Make a repository's CI faster and cheaper without letting it verify less than
the team thinks it does.

Coding agents push far more commits than people do, and every push reruns the
pipeline, so CI has become both the wait and the bill. There are two levers,
and a good result uses both:

- **Where CI runs:** runner choice, machine size, queue time, price per minute.
- **What CI runs:** caching, parallelism, faster tools, and above all not
  running everything on every push.

The order below matters. Guesses about where CI time goes are usually wrong (a
"slow test job" turns out to be a serial chain, or a cache that has missed all
year), so measure first, rank by what the numbers say, and change one thing at
a time so each change can be shown to have worked.

## Before you start

**Run only when asked.** This skill is for when the user invokes it
(`/ci-audit`, or `$ci-audit` in Codex) or asks for a CI audit in so many words. An audit reads hundreds of workflow
runs and produces a long report, so don't start one because CI came up in
passing or a build happened to be slow.

**Default to audit only.** Measure, report and recommend; change files only
after the user has seen the report and picked what to do. If they asked you to
"just fix it", still write the report first, then make the changes in the
*safe* tier below and ask about the rest.

**Answer what was asked.** Invoked with no question, do the full audit and
open with the two savings numbers; that is what most people want, so don't
stop to ask anything first. Invoked with a question (why PR checks are slow,
which jobs are flaky, whether a macOS leg earns its cost), measure the same
way, answer that question first with its evidence, then give the two numbers
in a line so they know what else the audit found. Write the full report only
if they want it.

**Know the goal.** Wait time, cost, or both: take it from what they asked, and
when they didn't say, assume both matter and show each change's effect on
both. Some advice flips: a bigger machine, more shards, or running jobs in
parallel that used to stop at the first failure all cut the wait and can raise
the bill, so ask before making a change that trades one for the other.

**Check access.** The script reads GitHub through the `gh` CLI
(`gh auth status`), or, when `gh` isn't installed, with a token in `GH_TOKEN`
or `GITHUB_TOKEN`. Either needs read access to Actions. With neither it stops
and says so: tell the user the two ways to fix it and let them choose; don't
install tools on their machine unasked. For CI that isn't GitHub Actions, or
when access can't be arranged, see "No API access" in
`references/measure.md`; the rest of the method still applies.

## Step 1: Measure

Run the bundled script from the repository root. It is read-only.

```bash
python3 <skill-dir>/scripts/ci_audit.py --json /tmp/ci-audit.json --save-dir /tmp/ci-audit-raw
```

Useful options: `--days 30 --max-runs 600` for a quiet repo or a longer view,
`--price LABEL=USD_PER_MIN` for runners it can't price (larger, third-party or
self-hosted), `--workflow ci` to focus on one workflow, and `--from-dir DIR` to
analyze data someone saved earlier with `--save-dir`. `--help` lists the rest.

The report opens with **two savings numbers**: dollars a month from the
fixes it can price while keeping today's runners, and the total if the Linux
jobs also moved to the cheapest managed runner at list price. Under them is
the row-by-row breakdown, priced in order so no minute is counted twice, and
what the remaining minutes would cost at each provider. It then gives PR feedback time for commits that pass (the wait for a green
PR) and for all commits, how a passing commit's wait splits into time before
its last job is created, queueing, setup and work, a table of jobs and of
slowest steps, what each matrix leg costs and whether it ever failed alone,
minutes and estimated dollars by runner, work that bought nothing, what fails
and how late, jobs that failed and then passed on a re-run (flaky), CI rounds
per PR and who triggers them, and cache hits read from logs. It ends its summary with **Signals**: leads, each naming the reference
file to read. `references/measure.md` explains every number and its limits.

## Step 2: Read the pipeline

Read every file in `.github/workflows/` (and any composite actions and
reusable workflows they call) and confirm each signal against the YAML before
believing it. The script sees timings; only the files say why. A serial
chain can be real dependencies, and runs that weren't cancelled can be
deploys that must not be.

Work out which workflows are the checks a PR actually waits on. The headline
wait covers everything a push starts, including preview deploys and comment
bots; if those aren't gates, rerun with `--workflow <name>` for the ones that
are and report both numbers. While reading,
note for each workflow: triggers, `concurrency`, `needs` edges, matrix,
`runs-on`, `timeout-minutes`, cache steps and their keys, and which jobs branch
protection requires (`gh api repos/{owner}/{repo}/branches/{branch}/protection`
if you have access; otherwise ask).

Then work out which of these the numbers point to, and read only the
references you need:

| What the numbers show | Lever | Read |
|---|---|---|
| Wait is mostly "waiting on earlier jobs"; cheap checks fail late; superseded runs keep running; push and PR both trigger; no timeouts; many sub-minute jobs | Pipeline shape | `references/quick-wins.md` |
| Setup is a large share; cache misses in logs; installs take a minute or more | Caching | `references/caching.md` |
| Work dominates: slow tests, lint, typecheck or build | Faster tests and tools | `references/tests-and-tools.md` |
| Everything runs on every PR; OS matrix on PRs; still slow after the above | Do less per PR | `references/do-less-per-pr.md` |
| Queue time; cost concentrated in a runner type; machine over- or under-sized | Runners | `references/runners.md` |
| Many CI rounds per PR; most PR minutes go to commits later replaced | Fewer rounds | `references/fewer-rounds.md` |

## Step 3: Report

Give the user this report, in this shape, before changing anything. Every
number comes from the script output or from a calculation you show; if you
can't measure something, say so instead of estimating silently.

```markdown
# CI audit: <repo>

## Estimated savings
Two numbers, and nothing else, before any detail:
- Keeping your runners: about $X a month (Y% of $Z)
- Switching runners too: about $W a month (V%), at the lowest managed list price
Then the wait for a green PR, today and expected.
Then two tables, one per number: | Fix | Tier | Saves/mo | What is counted |

## Where it stands
PR feedback time p50 / p90, billed minutes and estimated cost per month,
CI rounds per PR. Window and number of runs. What dominates the wait, and
what dominates the cost; they often have different causes.

## Findings
A table, biggest payoff first:
| # | Finding | Evidence | Fix | Expected effect | Risk | Tier |
Evidence quotes the measurement (job, step, minutes, share). Expected effect is
a range in minutes and dollars with the arithmetic behind it. Tier is safe,
check-first, or decision.

## Recommended order
The first three to five changes and why that order. State the wait and the
monthly cost after each step, so no step is a surprise.

## What this would change about coverage
Anything that would stop running on PRs, and exactly where it runs instead.
"Nothing" is a fine answer.

## Not measured
What the audit could not see (billing actuals, unpriced runners, expired logs).
```

**Lead with the money, and make it hold up.** People act on a number they
can see, so the savings estimate goes first. They stop trusting the whole
report the moment the number doesn't survive a look at the bill, so build it
carefully. The script's "Estimated savings" section is the starting point,
not the answer:

- Drop rows that reading the YAML showed aren't real: deploys that queue on
  purpose can't be cancelled, a ten-second job may have to stay separate.
- Add rows for fixes the script can't price (a slow postinstall, an
  oversized runner, a stuck PR), each with its arithmetic from the job and
  step tables: jobs per month × seconds saved × that runner's rate.
- Cut the arm64 row to the jobs you have reason to think can move. The
  script prices it as if every Linux job did, which is a ceiling.
- Don't count a minute twice. Price each row on what the rows above leave.
- Present exactly two totals: what the pipeline fixes save on the runners
  they have, and what they save if they also switch runners. The first is
  every pipeline row, whatever its tier; the second adds the runner changes
  (a smaller machine for tiny jobs, arm64, a cheaper provider); the tier column says
  which rows need checking or a decision. Two numbers are easy to hold and
  to compare against a bill; a third subtotal or a single blended "up to"
  figure makes people ask what is in it.
- For the runner row, open the pricing pages of the two or three cheapest
  candidates now (the script's table is dated; `--providers` takes fresher
  numbers) and say what list price leaves out: plan fees, free tiers, cache
  charges, and machines that are faster or slower than today's.
- Don't advertise a vendor. Compute the second number at the lowest managed
  list price, call it that, and name no provider in the two numbers, the
  rows, the findings or your message. Providers appear in one place only:
  the price table, all of them side by side, so the user picks. Naming the
  runner the repository uses today is fine; it is a fact about the repo.
- Give the time as well as the money: the wait today and the wait expected.

When you tell the user the audit is done, lead your message with those same
two numbers and one sentence on what dominates. Everything else (the
breakdown, the caveats, the provider table, what wasn't measured) belongs in
the report, not in the message.

Keep it to what the data supports. A short report with three real findings is
better than twelve generic ones; leave out advice the measurements don't back.
Some pipelines are already in good shape. Say so plainly, give the few things
that are left with their honest size, and don't pad the list.

## Step 4: Change

One lever per pull request, biggest measured payoff first, each PR stating its
before number and the after number it expects. Small PRs are how you find out
which change did what, and they are easy to revert.

Levers interact, so order them so that no step makes things worse on the way.
The common case: running jobs in parallel removes the saving a serial chain
got from stopping at the first failure, so do it after removing duplicate
triggers and after the user has decided about expensive legs, or the bill goes
up before it comes down. Price the pipeline shapes against each other from
the per-commit data in the JSON (`quick-wins.md`, section 3).

**Safe (do after the user picks them):** cancel superseded PR runs, stop
double triggers, run independent jobs in parallel, add timeouts, fix cache
keys and paths, fold tiny jobs together. Safe means the same things are
verified as before. It doesn't mean free: say which way each one moves the
wait and the cost.

**Check first (prove equivalence, then switch):** swapping a linter,
typechecker, package manager or test runner; changing test isolation; sharding;
changing machine size. Run old and new side by side and compare results before
removing the old one.

**Decision (the user decides, you lay out the trade):** anything that changes
what a PR is checked against or what the team pays:

- moving a suite, an OS leg or the full test run off PRs
- test selection, or what runs when selection can't tell
- branch protection, required checks, or a merge queue
- cancellation or concurrency on the default branch or deploy workflows
- a new paid service or runner provider, or anything that bills while idle
- deleting or skipping tests

For these, say what stops running where, where it runs instead, and who finds
out when it fails there. A cheaper pipeline that quietly checks less is not an
improvement, and the user is the one who answers for it.

Don't push to the default branch, and don't change repository settings; open
PRs and let the team merge them.

## Step 5: Verify

After a change has merged and a day or two of runs have accumulated, run the
script again over the new window and report before and after for the numbers
the change was meant to move. Runner noise is large, so compare medians over
many runs, never one run against another.

Offer a guardrail so the gain lasts: `assets/ci-budget.yml` is a weekly
workflow that runs this script with `--budget-minutes` and opens an issue when
PR feedback time drifts over the budget (five minutes is a good target for
per-PR checks). Copy the script next to it as the file's header describes.
