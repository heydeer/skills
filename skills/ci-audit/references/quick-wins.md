# Pipeline shape: the quick wins

Changes to when and in what order work runs. They don't change what is
verified, they are small diffs, and on most repositories they are the largest
gain per line changed. Check all of them before anything harder.

## Contents

1. Cancel superseded PR runs
2. Stop triggering twice
3. Run independent jobs in parallel
4. Let cheap checks fail first
5. Path filters, without blocking merges
6. Timeouts on every job
7. Tiny jobs and per-minute rounding
8. Checkout depth
9. Schedules and default-branch runs

## 1. Cancel superseded PR runs

When a new commit lands on a PR, the run for the previous commit is answering
a question nobody is asking any more. The report's "kept going after a newer
commit superseded them" row is the size of this.

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```

Cancel only on pull requests. On the default branch every commit's result
matters (it is how you find which merge broke main), and cancelling a deploy
halfway is worse than letting it finish. If the workflow already has a
`concurrency` block and runs are still not cancelled, the group key is usually
too specific (it includes the SHA or run id) or `cancel-in-progress` is
missing.

## 2. Stop triggering twice

```yaml
on:
  push:
  pull_request:
```

runs the whole workflow twice for every commit on a PR branch: once for the
push, once for the PR. The report's "run on both push and pull_request" row
shows it. Limit `push` to the branches that need it:

```yaml
on:
  push:
    branches: [main]
  pull_request:
```

Check first that nothing depends on the push run (a required check named after
it, a deploy keyed to branch pushes, coverage uploads).

## 3. Run independent jobs in parallel

Jobs start as soon as their `needs` are done, so a chain
`install → lint → typecheck → test` makes the PR wait for the sum instead of
the longest. A large "waiting on earlier jobs" share is this. Keep a `needs`
edge only where the later job consumes something the earlier one produced (an
artifact, an output, a deployment). "Don't bother testing if lint fails" is
not a reason: it saves a few runner minutes on failing commits and costs every
passing commit the whole chain.

A common leftover is an `install` job that everything `needs`. Jobs run on
separate machines, so nothing it installs carries over unless it is passed as
a cache or artifact; each later job installs again anyway. Delete the job and
let each job restore from the dependency cache (`caching.md`).

**Price it before recommending it.** A chain has one virtue: a commit that
fails lint never pays for the test matrix. Run everything in parallel and
every commit pays for everything, pass or fail. When many commits fail (see
the failing share in the summary) and something downstream is expensive (a
macOS leg, a long end-to-end suite), going fully parallel can raise PR cost
by half while cutting the wait. So compare shapes with numbers, from the
per-commit records in the `--json` output and the job table:

| Shape | Wait for a passing commit | What a failing commit pays |
|---|---|---|
| Today's chain | Sum of the chain | Jobs up to the failure |
| Two stages: cheap static checks in parallel, then the expensive jobs in parallel | Longest cheap check + longest expensive job | Cheap checks only, when one of them fails |
| Fully parallel | Longest single job | Everything |

Estimate the monthly cost of each from the commits in the window (how many
failed at which job, times the billed minutes and runner price of the jobs
that would newly run) and put the table in the report. Two stages usually
keeps most of the wait reduction at close to today's cost. Fully parallel is
the better choice once the expensive legs are off PRs or failures are rare.
This is also why order matters: remove duplicate triggers and settle what
runs on PRs first, then choose the shape.

## 4. Let cheap checks fail first

The "What fails" table shows how long an author waits to learn about each
failure. Lint, format and type errors should report in the first minute or
two. Parallel jobs do this by themselves. Inside one job, order steps from
cheapest to most expensive.

Better still is never spending a CI run on them: see `fewer-rounds.md` for
pre-push checks.

## 5. Path filters, without blocking merges

Skip work a change can't affect: docs-only changes don't need the test suite,
a backend change doesn't need the mobile build.

Workflow-level filters are the simplest:

```yaml
on:
  pull_request:
    paths-ignore: ["**.md", "docs/**"]
```

**The trap:** if a workflow is skipped by a path filter and one of its jobs is
a required check, the check stays "Pending" forever and the PR can't merge. A
job skipped by its own `if:` reports success instead. So when required checks
are involved, filter at the job level and require one gate job:

```yaml
jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      code: ${{ steps.filter.outputs.code }}
    steps:
      - uses: actions/checkout@v5
      - uses: dorny/paths-filter@v3
        id: filter
        with:
          filters: |
            code:
              - 'src/**'
              - 'package.json'
              - 'pnpm-lock.yaml'
              - '.github/workflows/**'

  test:
    needs: changes
    if: needs.changes.outputs.code == 'true'
    # ...

  ci-ok:            # the only required check
    if: always()
    needs: [changes, lint, test]
    runs-on: ubuntu-latest
    steps:
      - if: contains(needs.*.result, 'failure') || contains(needs.*.result, 'cancelled')
        run: exit 1
```

Changing which check is required is a branch-protection change: the user does
it, in the same sitting as the merge, or PRs block.

When a filter can't tell (CI config, lockfile, root config, shared tooling
changed), run everything. A filter that guesses wrong in the cheap direction
is how broken code reaches main.

Also worth a look: skipping the expensive jobs on draft PRs
(`if: github.event.pull_request.draft == false`, plus the `ready_for_review`
type on the trigger). It suits teams whose agents open drafts early, and
annoys teams who use drafts to get CI feedback, so ask.

## 6. Timeouts on every job

The default job timeout is six hours. One hung test at the wrong moment costs
360 minutes and holds a runner. Set `timeout-minutes` on every job to roughly
two to three times its p90 from the report, and tighter on jobs that are
known to hang.

## 7. Tiny jobs and per-minute rounding

GitHub-hosted runners bill each job rounded up to a whole minute, and every
job pays its own runner start and checkout. Ten 15-second jobs bill ten
minutes. The report's rounding row and its count of jobs under 30 seconds
show whether this matters here. Fold trivial jobs (labelers, notifiers,
one-line checks) into a job that already runs, or make them steps of one job.

The opposite also happens: one long job that runs lint, then typecheck, then
tests in sequence. Split it when the wait matters more than the few extra
billed minutes.

Light jobs that must stay separate don't need a full-size machine. GitHub's
1-vCPU `ubuntu-slim` runner costs a third of the standard one; the savings
estimate prices this move for every Linux job under 30 seconds
(`runners.md`).

## 8. Checkout depth

`actions/checkout` fetches one commit by default, which is right for most
jobs. `fetch-depth: 0` clones all history and all branches, and on a large
repository costs tens of seconds per job. Keep it only on jobs that need
history (affected-project detection, changelog, version from tags), and
consider `filter: blob:none` there. On the "Slowest steps" table, a checkout
step over about ten seconds is worth a look.

## 9. Schedules and default-branch runs

Look at the Workflows table for `schedule` and `push` rows:

- A cron that runs more often than its result is read, or keeps running on a
  repository nobody is changing, is pure cost. Lower the frequency or skip
  when there are no new commits since the last run.
- Runs on the default branch after every merge are the safety net once PR
  checks get lighter (`do-less-per-pr.md`). Don't cancel them, and make sure
  a failure there reaches a person.
