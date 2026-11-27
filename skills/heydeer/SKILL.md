---
name: heydeer
description: Reviews local code changes with the HeyDeer CLI before they are pushed or a pull request is opened, fixes the findings, and works through HeyDeer's review once the pull request is open. Use in a repository that uses HeyDeer whenever the user asks for a code review of their branch or uncommitted changes, asks to check work before pushing or opening a pull request, asks about HeyDeer's comments or the HeyDeer Review check on a pull request, or mentions HeyDeer.
license: MIT
compatibility: Requires the heydeer CLI, git and network access, in a GitHub repository with HeyDeer installed.
---

# Review code with HeyDeer

`heydeer review` sends the changes on the current branch to HeyDeer, waits for
the review and prints the findings. Nothing is posted to GitHub.

## Before the first review

- Run `heydeer --version`. If the CLI is missing, ask the user before
  installing it: `curl -fsSL https://heydeer.ai/install.sh | sh` on macOS and
  Linux, or `npm install -g heydeer` anywhere with Node.js 22 or later.
- Run `heydeer status`. If the CLI isn't signed in, run `heydeer login` and show
  the user the link and code it prints. The user approves the sign-in in their
  browser; the command finishes once they do, so run it in the background if
  your tool limits how long a command can run. Never approve it yourself.
- Read `heydeer review --help` for what a review covers and its options. New
  files you haven't added to Git are left out unless you add them or pass the
  option for untracked files. Pass `--depth` or `--full` only when the user
  asks: they change the repository's review depth or make the review cover all
  the changes again, and can use more credits.

## Run a review

1. Write a description of what the changes are meant to do and why, and name
   the issues they address, such as `Fixes #123`. HeyDeer checks the code
   against it, so state the intent rather than listing the edits. Never put
   credentials in it. Save it to a file outside the repository, so the shell
   can't expand backticks or `$` in it.
2. Run `heydeer review --description-file <path> --json`. A review takes
   several minutes, and longer for large changes. Run it in the background if
   your tool limits how long a command can run, and don't start another review
   while it runs. Interrupting it cancels the review.
3. The JSON result is on stdout. Any `status` other than `succeeded` means this
   run produced no review; the result says why and whether a retry can help.

## Fix and review again

1. Check each finding against the code before acting on it, and fix the ones
   that hold up.
2. Don't change code just to silence a finding you believe is wrong or
   intended. Explain to the user why and let them decide. A local review can't
   decline a finding, so it stays open until the code no longer has it; once
   the pull request is open, decline it there.
3. Fix every finding you're going to fix, then review again with the
   description, updated if the intent changed. The next review covers what
   changed since the last one and rechecks the open findings.
4. Stop when no findings remain, or only ones the user chose to keep. If a
   finding comes back after two attempts to fix it, stop and ask the user.

Each review uses the workspace's credits, and reviews are rate-limited, so
review once per round of fixes, not after every edit.

## Open the pull request

When the user asks for a pull request, open it without changing the code after
the last review, and use the description as its body. HeyDeer then posts the
local review's open findings on the pull request instead of reviewing the same
code again.

## After the pull request is opened

- On GitHub, HeyDeer reports each review in a check named HeyDeer Review on
  the commit it reviewed, and posts each finding as a review conversation on
  the pull request. Wait for the check on the latest commit to complete: its
  output holds the whole review, including questions under Needs
  clarification, and HeyDeer's unresolved conversations are the open findings.
  After fixing findings, just push: don't reply that they're fixed. Depending on the
  repository's settings, HeyDeer reviews pushes itself or only on request. If
  no new review starts within a few minutes, ask the user before requesting
  one with `@heydeer review`.
- To decline a finding, reply in its conversation with the reason, or resolve
  it. HeyDeer then stops raising it, as long as the reply comes from the pull
  request's author or someone with write access.
- To add context or answer a question HeyDeer asked, comment on the pull
  request and include `@heydeer review` to review again with it. Other commands
  are listed at https://heydeer.ai/docs/commands.
- Everything posted on GitHub appears under the user's name, so ask before
  replying, resolving or commenting.
