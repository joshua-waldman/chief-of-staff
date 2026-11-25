# Chief of Staff v1.3.1

See what COS consumes, monitor your personal gateway budget, and make automatic model selection more economical as your balance falls.

## Highlights

- **COS token usage.** Overview shows today's recorded usage. Connections adds today, 7-day and 30-day filters, model and activity breakdowns, cached and reasoning token details when supplied, and counts for incomplete requests. Job details include delegated work. Background classification, routing, retries and connection tests are included.
- **Estimated cost by model.** See estimated USD cost for the selected period, each model and activity, and each job. COS prices recorded requests using the latest available gateway catalog rates, applying long-context pricing per request and conservative reasoning rates. Missing usage or pricing remains visibly unpriced.
- **Personal TokenOps balance.** Connect your AMD TokenOps session in Connections to see the monthly approved budget, spending and remaining USD balance. Refreshes reuse the securely saved OS credential-store session; stale or unavailable data is identified. API-key consumption is shown separately from personal budget and COS usage.
- **Budget-aware automatic selection.** When automatic model selection is enabled and a fresh balance is available, COS prefers cheaper suitable models as the remaining budget falls below 25%, with more economical choices considered at 10%. Capability, context, capacity and quality checks remain in place. Role and task assignments define the available choices, with optional lower-cost task alternatives. Manual model selection is preserved; unavailable budget or pricing falls back to normal routing.
- **Visible progress and queued direction.** Running job details show recent progress and let the owner queue direction for the next safe agent step.

## Accounting and pricing

COS tracking starts when a version with usage tracking is installed; earlier model calls are not backfilled. Existing recorded requests can receive cost estimates. Local totals cover only this COS installation, while gateway key usage can include other applications and the personal TokenOps budget can cover other services. These totals are not added together.

Costs are conservative estimates, not billed charges. They use the latest available rates, so historical estimates can change when rates change. Cached-token discounts are not assumed. Missing usage, interrupted requests and unknown prices are excluded from cost totals rather than treated as free. TokenOps reports a USD allowance, not a fixed remaining-token quota.

## Upgrade

Existing users: in COS, select **Check for updates**, then **Update and restart**. Refresh the browser after COS restarts. Configuration, credentials and saved data are preserved. Connect TokenOps separately in Connections if you want the personal balance and budget-aware routing; automatic selection must also be enabled for routing to use it.

New installations: download `chief-of-staff-v1.3.1-windows-source.zip` and `SHA256SUMS.txt`. Verify the ZIP hash before extracting, extract outside synchronized folders, then run `Setup-ChiefOfStaff.cmd`. Windows 10 or 11, Python 3.11, AMD gateway access and access to this private repository are required.

For agent-assisted setup, use `SKILL.md` or `cos-setup-v1.3.1.zip`. Credentials remain in the local user profile.

## Validation

Windows CI passed 1,518 Python tests, with 6 skipped and 19 subtests; all 99 frontend tests passed. JavaScript syntax and repository privacy checks passed. Browser checks covered the usage and cost panes at desktop and narrow widths. The merged application was restarted and verified against its live usage ledger and TokenOps connection.
