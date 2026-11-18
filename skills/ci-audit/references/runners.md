# Runners: queue time, machine size, price

Where CI runs. It is the lever with the fastest payoff and the one most
likely to be oversold: a cheaper or faster minute helps, but a pipeline that
runs four times more than it needs to is still expensive on any runner.
Tidy what runs first (or alongside), then price where it runs.

Anything here that adds a paid service, changes who can see the repository's
secrets, or bills while idle is the user's decision.

## Contents

1. Cost per run, not per minute
2. Queue time
3. Right-sizing a machine
4. GitHub-hosted prices
5. Comparing providers
6. Self-hosting

## 1. Cost per run, not per minute

```
cost of a job  = ceil(minutes) × price per minute of its runner
cost of a push = sum over its jobs
monthly cost   = cost of a push × pushes per month (+ default-branch and scheduled runs)
```

The report's runner table gives billed minutes by runner type; multiply by
the rate to compare options. Three consequences:

- A machine with twice the cores at twice the price is cost-neutral if the
  job gets twice as fast, cheaper if rounding or setup shrink, and more
  expensive if the job doesn't use the cores.
- Splitting a job into N shards multiplies setup and rounding by N.
- macOS and Windows minutes cost several times Linux minutes, so one macOS
  leg can outweigh the rest of the pipeline.

Providers that bill per second remove rounding; providers that keep machines
warm between jobs trade setup time for idle cost.

## 2. Queue time

Queue is the gap between a job being created and a runner picking it up. The
report shows p50 and p90 per job and per matrix leg. Seconds are normal.
Tens of seconds to minutes means one of:

- **The plan's concurrency limit.** GitHub-hosted runners cap concurrent
  jobs per account (lower on small plans, much lower for macOS). Agents
  opening many PRs at once hit it. The symptom is a queue that grows at busy
  hours and affects every job.
- **Scarce machine types.** macOS and large runners queue longer than
  standard Linux.
- **A `concurrency` group** serializing jobs that could run together.
- **A self-hosted pool that is too small** or slow to scale up.
- **Provider cold starts**, which show as a constant per-job delay.

Fixes, cheapest first: run fewer jobs (everything else in this skill), move
light jobs off the scarce machine type, raise the limit (plan), or move the
queued jobs to a provider with more capacity. Boardy reported queue time
falling from 11 minutes to 8 seconds from a provider change alone, taking PR
wait from 40 to about 10 minutes before any pipeline work.

## 3. Right-sizing a machine

Decide from utilization, not from the feeling that bigger is faster. Add a
temporary step around the heavy command that prints `nproc` and total
memory, and measure CPU-seconds against wall time (`measure.md`).

- CPU near 100% of all cores for most of the job, and the tool can use more
  workers: a bigger machine cuts wall time, often at similar cost. Raise
  worker count to match.
- CPU well under the core count: the job is serial or waiting. A bigger
  machine changes nothing; a smaller one may cost less for the same time.
- Memory pressure or OOM kills (exit code 137, "lost communication with the
  server"): the machine is too small for the parallelism; fewer workers or
  more memory.
- Light jobs (labelers, small scripts, lint on a small repo): the smallest
  size on offer.

Arm runners are usually cheaper per minute, by about a sixth on
GitHub-hosted runners and by more at some providers. The savings estimate
prices this as if every Linux x64 job moved, which is an upper bound: jobs
that build x64 images or binaries, or depend on packages with no arm build,
stay where they are. Try one workflow, compare billed cost per run, and
count only the jobs that moved.

Jobs that finish in seconds and must stay separate (a required aggregate
check, a change detector, a labeler) can run on GitHub's 1-vCPU runner,
`ubuntu-slim`, at a third of the standard rate. It runs as an unprivileged
container, so it can't run Docker or anything else that needs elevated
privileges.

## 4. GitHub-hosted prices

Per minute, USD, each job rounded up to a whole minute. Read on 2026-11 from
https://docs.github.com/en/billing/reference/actions-runner-pricing. Prices
change, so open that page before quoting numbers to the user.

| Runner | USD/min |
|---|---|
| Linux 1-core (`ubuntu-slim`) | 0.002 |
| Linux 2-core arm64 | 0.005 |
| Linux 2-core x64 (`ubuntu-latest`) | 0.006 |
| Windows 2-core | 0.010 |
| macOS (standard) | 0.062 |
| macOS large / xlarge | 0.077 / 0.102 |
| Larger Linux x64: 4 / 8 / 16 / 32 / 64 cores | 0.012 / 0.022 / 0.042 / 0.082 / 0.162 |
| Larger Windows: 4 / 8 / 16 / 32 / 64 cores | 0.022 / 0.042 / 0.082 / 0.162 / 0.322 |
| Larger Linux arm64: 4 / 8 / 16 / 32 / 64 cores | 0.008 / 0.014 / 0.026 / 0.050 / 0.098 |

Standard runners are free on public repositories. Private repositories get
a monthly allowance of included minutes that depends on the plan; the
report's cost is list price before that allowance.

## 5. Comparing providers

Several companies sell drop-in GitHub Actions runners, usually by changing
the `runs-on` label, competing on price per minute, faster hardware, shorter
queues and built-in caching.

The script's report includes "The same Linux minutes at other runners' list
prices": this repository's billed minutes, by machine size, at each
provider's public rate. The rates are in the script, read from the pricing
pages on the date it prints. As read on 2026-11-18 (rows marked * on
2026-11-17), Linux x64, USD per minute:

| Provider | 2 vCPU | 4 vCPU | 8 vCPU | 16 vCPU | Worth knowing |
|---|---|---|---|---|---|
| GitHub-hosted | 0.006 | 0.012 | 0.022 | 0.042 | each job rounded up to a minute |
| Depot * | 0.006 | not listed | not listed | not listed | plans from $20/mo with included minutes |
| Blacksmith | 0.004 | 0.008 | 0.016 | 0.032 | half of GitHub's rate per their docs; 3,000 free minutes a month; rounding unpublished |
| WarpBuild * | 0.004 | 0.008 | 0.016 | 0.032 | cache storage billed separately |
| Namespace * | 0.002 | 0.008 | 0.016 | | prepaid rates, plans from $100/mo; 2 vCPU has 4 GB |
| Ubicloud (premium) | 0.002 | 0.004 | 0.008 | 0.016 | standard tier closed to new customers since 2026-05; 1,250 free minutes a month |
| Sengi (Basic) | 0.001 | 0.002 | 0.004 | not listed | rounds each job up to a minute; 3,000 free base minutes a month |
| RunsOn, in your AWS account, spot * | 0.0009 | 0.0013 | 0.0024 | 0.0034 | plus a licence and AWS overhead; you operate it |

The script's table also holds the Windows and macOS rates each provider
lists (macOS between 0.04 and 0.16 a minute depending on the machine, against
0.062 to 0.102 on GitHub-hosted). macOS machines are not alike: one
provider's smallest Mac may be twice the size of another's, so the script
matches by class and marks approximations, and a trial matters even more
than for Linux. The savings figure takes the lowest managed price for each
operating system separately, since the cheapest Linux provider may not sell
Macs at all.

Prices move several times a year and new low-price providers keep
appearing, so before a number goes into a report:

1. Open the pricing page of each candidate you intend to name and check the
   rate for the sizes this repository uses. If it changed, write the new
   table to a JSON file in the same shape as `PROVIDER_PRICES` in the script
   and rerun with `--providers FILE`.
2. Say that it is list price for the same minutes. Not included: plan fees
   and free tiers, cache and storage charges, and whether its machines are
   faster or slower than today's. A provider that is half the price and a
   third slower saves a third, not a half.
3. Confirm for each provider whether it bills by the second or rounds each
   job up to a whole minute; when its page doesn't say, write "rounding
   unknown" next to it. Rounding is 20–30% of billed minutes on pipelines
   with many short jobs, so this one fact moves the comparison more than
   most rate differences — and the tiny-job and rounding savings rows only
   exist under per-minute billing.
4. Recommend a trial, not a migration: run one representative workflow on
   the candidate for a week (a copy of the workflow with a different
   `runs-on`), then run the audit script on those runs with `--price` and
   compare cost per run, PR feedback time, queue p90 and cache hit rate.

Check before recommending: how the provider's cache works with the
repository's cache steps, concurrency limits, arm and macOS availability,
where the machines are (data residency), what access the provider's GitHub
App asks for, and whether the team's compliance needs allow a third party to
run jobs that hold secrets.

Don't recommend a provider by name. The savings figure uses the lowest
managed list price and says so; the table shows every provider side by side
and the user chooses. Present a runner change as the user's decision. It is
often the largest dollar figure in the report and the one with the most
unknowns. Do the pipeline fixes first: they are certain, and they shrink the
minutes any provider would bill.

Faster runners can also raise the bill while cutting the wait (Boardy: 20×
faster PR checks, monthly bill from $230 to $4,300, and judged worth it for
agent throughput). State which way the proposal moves each number.

## 6. Self-hosting

Running your own machines (cloud spot instances with an autoscaler, rented
bare metal, Mac minis, microVMs) can be the cheapest per minute for heavy,
steady load and for macOS builds. Teams have reported replacing four- and
five-figure annual bills this way. The costs are not on the invoice:

- **Operations.** Autoscaling, image builds, disk cleanup, runner upgrades,
  and being paged when the pool is down. One team cut CI spend 68% by
  *deleting* 3,000 lines of self-hosted runner operator code in favor of a
  managed provider.
- **Idle time.** Machines that wait for work still cost money; size the warm
  pool to working hours.
- **Security.** Never attach persistent self-hosted runners to a public
  repository, where a fork's pull request can run code on them. Use
  ephemeral, single-use machines for anything that runs untrusted code, and
  keep deploy credentials off machines that run PR jobs.
- **Platform fees.** Check GitHub's current billing documentation for any
  per-minute charge on self-hosted runner usage before assuming the minutes
  are free.

Recommend self-hosting only when the load is large and steady enough to
repay the operations work, and the team has someone who wants to own it.
