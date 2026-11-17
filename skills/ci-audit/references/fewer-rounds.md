# Fewer CI rounds per PR

Every other lever makes one CI run cheaper. This one reduces how many runs a
PR needs. With agents in a push → wait → read the failure → fix → push loop,
a PR that takes six rounds costs six pipelines, and five of them tested
commits that were thrown away.

The report measures it: "CI rounds per PR" (distinct commits that triggered
CI per PR branch) and the share of PR minutes spent on commits that were
later replaced, split by whether that commit's CI failed. A median of one or
two rounds is healthy. Three or more, or over half of PR minutes on replaced
commits, makes this lever worth as much as any pipeline change.

Some rounds are the point of CI. The aim is to remove the ones that existed
only to discover something that was knowable before pushing.

## 1. Catch the cheap failures before the push

Look at the "What fails" table. Format, lint, type and compile errors that
fail in CI were detectable in seconds on the author's machine or in the
agent's sandbox.

- **Tell the agents.** In `AGENTS.md` / `CLAUDE.md`, state the exact commands
  to run before pushing (format, lint, typecheck, and the tests for the files
  touched) and that a push should not happen while they fail. Agents follow
  this far more reliably than people do, and it is the cheapest fix here.
- **Hooks for people.** A pre-commit hook that formats and lints staged
  files, and a short pre-push check (lefthook, husky, pre-commit). Keep it
  under about ten seconds or it will be skipped.
- **Make the local commands the same ones CI runs**, through one script or
  task, so "passes locally" predicts "passes in CI".

## 2. Make each round answer everything at once

A PR that fails lint, gets fixed, then fails typecheck, gets fixed, then
fails a test has paid three rounds for one batch of problems.

- Run independent checks in parallel and don't let one failure cancel the
  others on PRs (`fail-fast: false` on matrices, no `needs` chains between
  checks; see `quick-wins.md`), so one round reports every failure.
- Ask agents to read all failing checks and all review comments, fix them
  together, and push once, instead of one push per finding.

## 3. Make the early rounds cheap

Rounds that remain should cost little: cancel superseded runs
(`quick-wins.md`) and keep the per-push pipeline to rules plus
change-specific tests (`do-less-per-pr.md`). Then a six-round PR costs six
small runs and one full one.

## 4. Find problems by reading the diff, not by running the suite

CI finds a bug by executing code for minutes on rented machines. A reviewer
finds many of the same bugs, and others no test covers, by reading the
change. Review that arrives in the same round as the first CI result lets
the author fix both in one push; review that arrives after CI is green
starts another round.

An automated reviewer on every PR does this without waiting for a person. It
matters more once PR checks get lighter: when the full suite moves to a
merge queue or to main, review is the check that still sees every change
before it merges.

What to look for in one, whichever it is:

- it verifies findings against the code before posting them (false positives
  cause pushes too)
- it can fix what it finds, tested, rather than only commenting
- it reads the repository's own rules (`AGENTS.md`, review guidelines)
- it reports as a check, so it can be required like any other

**When the data supports suggesting a reviewer, and when it doesn't.** Look
at what the rounds are made of ("CI rounds per PR" in the report):

- Replaced commits that had **failed CI** on lint, format, type or test
  errors are fixed by sections 1 to 3. A reviewer doesn't obviously help
  with those, so don't suggest one on their account.
- Replaced commits that had **passed CI** and were replaced anyway are
  rounds caused by something CI didn't say: review feedback, a bug someone
  spotted, a second look, or simply work pushed in small steps. Read a
  sample of their commit messages (`gh api repos/{owner}/{repo}/commits/{sha}`)
  before deciding which: follow-up commits minutes apart are a working
  style, merges from main are a branch-update policy, and neither is fixed
  by a reviewer. When the sample shows review-driven fixes arriving after
  CI was green, review arriving earlier and in one batch is the lever.
- If the plan moves the full suite, a platform leg or other checks off PRs
  and no automated review runs on every PR, one is worth suggesting as the
  compensating check.

When neither holds, leave reviewers out of the report. Don't name a product;
describe what to look for and let the team choose. If the repository already
uses a reviewer, there is nothing to add.

An automated reviewer's own pushes trigger CI like anyone's, so the
cancellation and layering above apply to it too.

## 5. Count again

After these changes, the rounds numbers in the next audit should move. If
rounds per PR stay high while CI failures fall, the rounds are coming from
review feedback or changing requirements, which is not a CI problem.
