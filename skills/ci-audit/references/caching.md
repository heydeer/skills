# Caching

Setup that repeats on every job (downloading dependencies, rebuilding what
didn't change, pulling browsers and base images) is often the largest share of
a short job. Caches fix that, and caches fail silently: a job with a broken
cache still passes, only slower, so nobody notices. One team found half its
PR test jobs had missed the dependency cache for a year.

## Contents

1. Find out whether caches hit
2. Why caches miss
3. Dependency caches
4. Build and task caches
5. Docker layers
6. Browsers and other large downloads
7. When not to cache
8. Trust

## 1. Find out whether caches hit

Don't infer from the YAML; a correct-looking cache step can miss every time.

- The report's "Cache evidence from logs" table counts restores that hit and
  missed in recent jobs.
- In a job log, `Cache restored from key: …` is a hit and
  `Cache not found for input keys: …` is a miss. A post step saying
  `Cache hit occurred on the primary key …, not saving cache` means the exact
  key existed.
- The report's "Cache storage on GitHub" section shows the store's size
  against the 10 GB limit and its biggest entries, with how many were never
  restored after being saved — entries created on every run and never read
  again are a key that changes on every run. `gh cache list` pages through
  the rest.
- An install step that takes the same time on every run, with a cache step
  before it, is a miss whatever the log says.

## 2. Why caches miss

Check these against the workflow, most common first.

**The key changes every run.** `key: deps-${{ github.sha }}` or a key
containing `github.run_id` never matches a previous run. Key on what
determines the content (the lockfile hash), and add `restore-keys` so a near
match is used when the exact one is absent:

```yaml
- uses: actions/cache@v4
  with:
    path: ~/.cache/pip
    key: pip-${{ runner.os }}-${{ hashFiles('**/requirements*.txt') }}
    restore-keys: pip-${{ runner.os }}-
```

**Nothing saves it where PRs can read it.** A run can restore caches created
on its own branch or on the default branch, not on other PR branches. If the
workflow never runs on the default branch (see "Stop triggering twice" in
`quick-wins.md`: the fix there must keep `push: branches: [main]`), every new
PR starts cold and only its second push hits. Make sure some workflow that
builds the same cache runs on pushes to the default branch.

**The hash covers nothing, or the wrong file.** `hashFiles('package-lock.json')`
in a pnpm repo, or a lockfile that lives in a subdirectory, hashes to an empty
or constant string. The `setup-*` actions look for a lockfile at the root
unless given `cache-dependency-path`.

**The path isn't what the tool uses.** Caching `~/.npm` while installing with
pnpm or yarn; caching a store directory that the tool was configured to put
elsewhere; caching `node_modules` across operating systems or runtime
versions. Print the tool's own cache location in CI (`pnpm store path`,
`pip cache dir`, `go env GOCACHE`, `yarn cache dir`) and compare.

**The built-in cache was never switched on.** Some setup actions cache only
when asked (`cache: pnpm` on `actions/setup-node`, `cache: pip` on
`actions/setup-python`), some by default (`actions/setup-go`). Read the
action's current README rather than assuming; the defaults have changed
between major versions.

**It is evicted.** GitHub removes entries not read for seven days and the
oldest entries once the repository passes its cache size limit. A few
multi-gigabyte entries per branch can push out everything else, so entries
are recreated constantly. `gh cache list --sort size_in_bytes` shows the
culprits.

**The job failed before saving.** By default the cache is saved in a post
step only when the job succeeds. A job that fails often never seeds its
cache.

**The exact key hit, so nothing new was saved.** Keys are immutable: after an
exact hit the cache is not updated. That is right for dependency stores and
wrong for caches that grow (build output, incremental compiler state), which
then go stale. See section 4.

**The runner isn't GitHub's.** Third-party and self-hosted runners may route
`actions/cache` to their own storage, need their own cache action, or have no
cache backend at all. Check the provider's documentation.

## 3. Dependency caches

Prefer the setup action's built-in cache where it exists; it knows the
package manager's store path and lockfile:

```yaml
- uses: pnpm/action-setup@v4
- uses: actions/setup-node@v4
  with:
    node-version-file: .nvmrc
    cache: pnpm
- run: pnpm install --frozen-lockfile
```

These cache the download store, not the installed tree, so an install still
links packages (seconds, not minutes). If install is still slow on a hit,
measure what remains: postinstall scripts, native builds, or a package
manager worth swapping (`tests-and-tools.md`).

To keep PRs from filling the cache with near-identical entries, restore
everywhere and save only on the default branch:

```yaml
- uses: actions/cache/restore@v4
  id: deps
  with: { path: ..., key: ... }
# install
- uses: actions/cache/save@v4
  if: github.ref == 'refs/heads/main' && steps.deps.outputs.cache-hit != 'true'
  with: { path: ..., key: ${{ steps.deps.outputs.cache-primary-key }} }
```

## 4. Build and task caches

Compilers and task runners can skip work whose inputs didn't change, if the
previous output is there.

- **Turborepo, Nx, Bazel, Gradle, sccache:** use a remote cache shared by CI
  (and by developers and agent sandboxes where the tool supports it). In the
  report, a Turbo or Nx task-cache ratio near zero means the cache isn't
  shared between runs. A local cache directory stored with `actions/cache`
  works as a starting point.
- **Caches that grow** (`.next/cache`, `.turbo`, `target/`, incremental
  compiler state): key on the commit and fall back to the newest earlier
  entry, so each run starts from the last one and saves its own:

  ```yaml
  - uses: actions/cache@v4
    with:
      path: .next/cache
      key: next-${{ runner.os }}-${{ hashFiles('pnpm-lock.yaml') }}-${{ github.sha }}
      restore-keys: |
        next-${{ runner.os }}-${{ hashFiles('pnpm-lock.yaml') }}-
        next-${{ runner.os }}-
  ```

  This writes an entry per run, so watch the size limit and save only on the
  default branch if it thrashes.
- **Rust:** `Swatinem/rust-cache` handles `target/` and the registry with
  sensible keys.

Verify a build cache by correctness as well as speed: a cached build that
serves stale output is worse than a slow one. After enabling one, change a
source file and confirm the affected task reruns.

## 5. Docker layers

A `docker build` on a fresh runner rebuilds every layer. With Buildx:

```yaml
- uses: docker/setup-buildx-action@v3
- uses: docker/build-push-action@v6
  with:
    cache-from: type=gha
    cache-to: type=gha,mode=max
```

`type=gha` shares the repository's Actions cache and its size limit; for large
images use a registry cache (`type=registry,ref=…`). Layer order matters more
than the backend: copy the lockfile and install dependencies before copying
the source, keep values that change per build (commit SHA, build time) out of
early layers, and keep the build context small with `.dockerignore`. In the
log, `CACHED` on a step means its layer was reused.

## 6. Browsers and other large downloads

Playwright and Cypress browsers, Android SDK components, ML model files and
`apt-get install` lists are re-downloaded on every run unless cached. Cache
the download directory keyed on the tool's version:

```yaml
- id: pw
  run: echo "v=$(pnpm exec playwright --version)" >> "$GITHUB_OUTPUT"
- uses: actions/cache@v4
  with:
    path: ~/.cache/ms-playwright
    key: playwright-${{ runner.os }}-${{ steps.pw.outputs.v }}
```

For a long list of system packages, a prebuilt container image or runner
image is the alternative, but pulling a multi-gigabyte image has its own
cost. Measure both.

## 7. When not to cache

Restoring and saving take time too. A cache whose restore plus save costs
more than the work it replaces (small dependency sets, fast compilers, a
22-second post step saving a cache that never hits) should be removed. The
"Slowest steps" table shows cache and post-cache steps with their totals.

## 8. Trust

A cache is a file one job writes and another runs. GitHub's own cache keeps
PR branches from writing entries the default branch reads. Third-party cache
backends, remote build caches and persistent disks on self-hosted runners
may not. Where a deploy or release job restores a cache, make sure only
trusted branches can write it (read-only credentials for PRs, separate cache
namespaces, or fresh machines for privileged jobs).
