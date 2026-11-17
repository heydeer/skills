# Do less per PR

Once the pipeline is parallel, cached and using fast tools, the remaining
lever is the largest one: stop running everything on every push. Buying
faster minutes alone doesn't keep up with agents that push dozens of commits
a day.

Everything here changes what a PR is checked against. These are the user's
decisions. Your job is to lay out exactly what moves, where it runs instead,
and what catches a failure there.

## Contents

1. Layered validation
2. Affected-only builds and tests
3. Test selection
4. Matrix and platform legs
5. Merge queue or post-merge runs
6. What must be true before PR coverage shrinks

## 1. Layered validation

Not every check needs to answer the same question at the same moment. Split
them by the question:

| Layer | Question | Runs | Examples |
|---|---|---|---|
| 1. Rules | Does it follow the rules? | Every push, seconds | Format, lint, typecheck, policy |
| 2. This change works | Does what changed still work? | Every push, minutes | Tests for affected code, a smoke test |
| 3. Main is good | Does everything still work together? | Merge queue, or after merge on the default branch | Full suite, all platforms, long end-to-end |
| 4. Ready to release | Is it safe for users? | Before or during release | Staged rollout, feature flags, canaries |

Per PR, run layers 1 and 2 inside a time budget (about five minutes is a
good target), and move layer 3 to where it runs once per merge instead of
once per push. When the PR pipeline goes over budget, tighten it again; the
budget workflow in `assets/ci-budget.yml` raises the flag.

The report shows the prize: CI rounds per PR, and the share of PR minutes
spent on commits that were later replaced. Those early rounds only need
layers 1 and 2.

Preview deployments belong in this picture too. A full preview on every push
is often a large share of PR minutes (and of hosting costs outside the CI
bill), most of it for commits that are replaced within the hour. Deploying
after the checks pass, on demand, or not for bot commits are options; which
one is a decision about how the team reviews, so present the numbers and let
them choose.

## 2. Affected-only builds and tests

In a repository with separable parts, test only what the change can reach.

- **Nx:** `nx affected -t lint test build --base=origin/main`
- **Turborepo:** `turbo run test --affected`
- **pnpm workspaces:** `pnpm --filter "...[origin/main]" test`
- **Bazel, Pants, Buck:** the build graph does this natively
- **Go:** changed packages plus their reverse dependencies (`go list -deps`)
- **No workspace tool:** job-level path filters (`quick-wins.md`), one filter
  per independent area

These need the base commit: fetch enough history for the diff
(`fetch-depth: 0` with `filter: blob:none`, or fetch the merge base), and
compare against the merge base, not the tip of main.

The dependency graph must be honest. Code that reaches another package
through a runtime path the graph doesn't see (dynamic imports, codegen, a
shared database schema, config files) will be skipped wrongly. Declare those
as inputs, and treat changes to lockfiles, root config, CI config and shared
tooling as "everything is affected".

If the repository is one large package, affected-only has nothing to work
with until it is split into modules. That is a larger project; say so
instead of pretending path filters will do it.

## 3. Test selection

Finer than affected projects: pick the tests a diff can affect.

- **Built into runners:** Vitest `--changed origin/main`, Jest
  `--changedSince=origin/main` and `--findRelatedTests`, Playwright
  `--only-changed=origin/main`, pytest with pytest-testmon. These follow the
  import graph or recorded coverage.
- **Coverage-based maps:** record which tests touch which files on the
  default branch; select from the map.
- **Model-assisted selection:** an LLM or classifier picks tests from the
  diff and the graph. One team reports CI on a large codebase about 20×
  faster than running every test, with a nightly job that retunes the
  selection. It is a system to build and keep honest, not a setting.

Whichever is used, selection must fail open: if it errors, times out, or
can't classify a file, run everything. And it must be measured: replay a set
of past PRs whose CI caught a real bug and check that selection would still
have run the failing test. Keep that replay as a recurring job, because
selection drifts as code moves.

## 4. Matrix and platform legs

The report's matrix table shows what each leg costs. macOS and Windows
minutes cost several times Linux minutes and often queue longer. If Linux
exercises the same code paths, run only Linux on PRs and the full matrix on
the default branch or nightly:

```yaml
strategy:
  matrix:
    os: ${{ github.event_name == 'pull_request' && fromJSON('["ubuntu-latest"]') || fromJSON('["ubuntu-latest","macos-latest","windows-latest"]') }}
```

Keep a platform on PRs when the change touches platform-specific code (path
filters can decide this) or when that platform has caught real regressions:
count how often each leg failed alone in the last few months before
dropping it. The same reasoning applies to language-version matrices: test
the oldest and newest on PRs, the rest after merge.

## 5. Merge queue or post-merge runs

Layer 3 has to run somewhere. Two patterns:

**Merge queue.** The full suite runs on the exact commit that will become
main, before it lands; a failure never reaches main. Workflows need the
`merge_group` trigger, and the queue is enabled in branch protection. Costs:
every merge pays the full suite, slow or flaky suites back up the queue, and
batching (testing several PRs together) trades fewer runs for bisecting when
a batch fails. Fits teams where a red main is expensive.

**Post-merge on the default branch.** The full suite runs after each merge
(or on a rolling schedule, coalescing merges). Cheaper and never blocks
merging, but main can be red for a while, so it needs the alarm and the
response described below. Fits teams with fast reverts and staged releases.

Either way, never cancel in-progress runs on the default branch, or the
result for the commit that broke it disappears.

## 6. What must be true before PR coverage shrinks

Each of these is how a "faster" pipeline turns into one that checks nothing.
Put them in place in the same change that moves coverage, not later.

- **The full suite still runs somewhere on a schedule you can name**, and its
  history shows it running.
- **A failure there reaches a person or an agent quickly**: a chat alert, an
  issue, or an auto-revert. Someone owns a red main.
- **A required check can't pass by running nothing.** Selection that selects
  zero tests, a glob that matches no files, or a skipped shard all look
  green. Have required test jobs fail when zero tests executed, unless
  selection explicitly said there was nothing to run.
- **Selection and filters fail open**, as above.
- **The change is written down**: what moved off PRs, where it runs now, and
  how to run it on a PR on demand (a label or a `workflow_dispatch` input)
  when someone wants the full check before merging.

Lighter PR checks also raise the value of the other check that runs on every
PR: review. See `fewer-rounds.md`.
