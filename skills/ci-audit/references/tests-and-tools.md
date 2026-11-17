# Faster tests and tools

For jobs where the work itself dominates. Two different moves: make the same
checks cost less (this file), or run fewer of them per PR
(`do-less-per-pr.md`). Do this one first; it has no coverage trade-off.

## Contents

1. Find what the time is spent on
2. Tests: common causes and fixes
3. Sharding
4. Flaky tests
5. Faster tools, and how to swap safely

## 1. Find what the time is spent on

The report says which step is slow. It can't say why. Before changing
anything, run the exact command CI runs and get the tool's own breakdown:

| Runner | Slowest tests |
|---|---|
| Vitest | `vitest run --reporter=json --outputFile=out.json`, sort by duration |
| Jest | `jest --json --outputFile=out.json`, or `--verbose` |
| Playwright | `--reporter=json`; the HTML report sorts by duration |
| pytest | `pytest --durations=25` |
| Go | `go test -json ./...`, sum `elapsed` per test and package |
| Rust | `cargo nextest run` prints per-test times |
| JUnit XML (most others) | sort `<testcase time="…">` |

Then answer two questions:

- **Is it a few slow tests or a slow average?** If the top 5% of tests hold
  half the time, fix those tests. If thousands of fast tests add up, the cost
  is per-file or per-process overhead, and the fix is in the runner's
  configuration.
- **Is it using the machine?** Compare CPU-seconds with wall time
  (`references/measure.md`). A suite at 100% of one core on a four-core
  runner needs parallelism; a suite at 20% CPU is waiting on something.

## 2. Tests: common causes and fixes

Numbers in brackets are reported results from public write-ups, to show the
scale; they are not promises.

- **A slow real dependency in unit tests** (network call, real database
  start, container boot per file). Mock or fake it in the unit layer and
  keep one integration test that uses the real thing. [592 s → 30 s]
- **Process overhead.** A fresh process or module graph per test file means
  every file pays startup again. Reuse workers: Vitest `isolate: false` (and
  `pool: 'threads'`) where tests don't leak global state, Jest
  `--runInBand` or fewer workers on a two-core runner, pytest-xdist with
  `--dist loadfile` and session-scoped fixtures. [100 s → 35 s per shard;
  8m49s → 21 s]
- **Too many workers for the machine.** Defaults assume a laptop. On a
  two-core runner, eight workers thrash. Set workers to the core count the
  job prints with `nproc`.
- **Real sleeps and polling.** Replace `sleep`-and-check with fake timers or
  waiting on the exact condition.
- **Transpiling on the fly.** A TypeScript loader that compiles every import
  for every test file can cost more than the tests. Use the runner's fast
  transform (esbuild, SWC, oxc) or native type stripping.
- **Shared database.** Give each worker its own database cloned from a
  migrated template instead of migrating per test file or serializing on one.
- **Setup repeated per test** that could run once per file or per session.

Turning isolation off exposes tests that depended on it. Run the suite
several times in random order before and after; tests that start failing
were leaking state, and they need fixing, not re-isolating everything.

## 3. Sharding

Splitting a suite across machines cuts the wait and raises billed minutes
(each shard pays setup, and rounding). Do it when the wait matters and the
suite is still long after section 2.

```yaml
strategy:
  fail-fast: false
  matrix:
    shard: [1, 2, 3, 4]
steps:
  - run: pnpm vitest run --shard=${{ matrix.shard }}/4
```

Jest and Playwright take the same `--shard=i/n`. These split by file count,
so one shard often runs twice as long as another. Balance by measured time
instead: record per-file durations from recent runs on the default branch and
assign files so shards finish together (pytest-split's `--store-durations` /
`--splits`, Playwright and Jest via a small script that reads the JSON report,
Nx and Turborepo task distribution, CircleCI-style timing splits). [22m26s →
7m29s]

Choose the shard count from the numbers: total test time ÷ target wall time,
then check that setup per shard doesn't dominate. If setup is two minutes and
each shard tests for one, fix setup first.

## 4. Flaky tests

The report's "Flaky jobs" table lists every job that failed and then passed
on a re-run of the same commit, with the step that failed and what those
executions cost. That is the evidence; nothing else distinguishes a flaky
failure from a real one. Open the failed attempt's log for the top rows
(`gh run view <run-id> --attempt 1 --log-failed`) to get the test names,
then fix them or quarantine them with an owner and an expiry date.

The dollar cost of the re-runs is usually small. The larger cost is the
wait, and that people stop believing a red check. Automatic retries hide
both instead of removing them; if you add them, report every retried test.

Many "flaky tests" are timeouts on an overloaded runner. If flakes cluster
on the busiest jobs, look at worker count and machine size before the tests.

## 5. Faster tools, and how to swap safely

Rewrites in Rust and Go have made several everyday tools many times faster.
Whether a given swap fits depends on the repository's plugins and config, and
these projects move quickly, so read the tool's current documentation before
proposing one; don't rely on memory for flags or feature coverage.

| Slow step | Candidates | What to check |
|---|---|---|
| ESLint | Oxlint, Biome | Which of the repo's rules and plugins are covered; many teams run the fast linter on PRs and keep ESLint for the uncovered rules only |
| Prettier | Biome, oxfmt, dprint | Formatting differences produce one large reformat commit |
| `tsc --noEmit` | The native TypeScript compiler (TypeScript 7 / `tsgo`); project references with incremental builds | Same diagnostics on the default branch |
| `npm ci` | pnpm, Bun, or a correct cache first | Lockfile migration; postinstall scripts |
| Jest | Vitest | A real migration, not a flag; worth it mainly when transform time dominates |
| flake8, black, isort, pylint | Ruff | Rule coverage |
| `pip install` | uv | Resolver differences; lockfile |
| `cargo test`, slow Rust builds | cargo-nextest; sccache; a faster linker | Doctests need a separate `cargo test --doc` |
| Gradle | Build cache, configuration cache, `gradle/actions/setup-gradle` | Cache correctness |
| Webpack or Babel in test and build | esbuild, SWC, Rspack, Vite | Plugin parity |

Reported scale: ESLint → Oxlint 2 min → 5 s; `tsc` → native typecheck 70 s →
under 5 s (Boardy, 2026).

**Swap by proving equivalence:**

1. Run the new tool next to the old one on the default branch, without
   failing the build.
2. Compare what they report: same errors on the same files. For linters,
   list the rules the new tool doesn't cover and decide rule by rule (drop,
   keep under the old tool, or accept the gap) with the user.
3. Make the new tool the required one. Keep the old one on a schedule for a
   week or two if any rules were kept under it.
4. Remove the old tool and its config.

A swap that silently drops rules is a coverage reduction. Say which rules.
