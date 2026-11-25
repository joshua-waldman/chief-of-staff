# Chief of Staff v1.3.2

Automatic model selection can now choose from the full eligible AMD gateway catalog, using task suitability and remaining budget instead of a fixed list of assigned task models.

## Highlights

- **Catalog-wide model selection.** COS considers all eligible chat and Responses models, including models you have never assigned to a role. Candidates must fit the task's image, tool, context, output and capacity requirements. Large catalogs are assessed in bounded batches.
- **Task-specific model advice.** A configurable chat advisor evaluates known model strengths, limitations and task fit, and recommends the best fit plus economical alternatives. It defaults to the chief's assigned model. The selected model executes the task; the advisor only recommends models. Role assignments remain the manual choice and the fallback when selection is uncertain or unavailable.
- **Budget-aware choices across the catalog.** Every proposed model must meet a suitability score of at least 0.85, or the configured minimum if higher. At 25% or less remaining budget, COS prefers qualified alternatives offering at least 20% estimated savings; at 10% or less it also considers savings of at least 5%. Budget pressure never lowers the suitability threshold.
- **Resilience to unusable catalog entries.** Some gateway-listed models reject requests because they are retiring or unavailable to the key. Definite HTTP 403, 404, 410 and 426 rejections are temporarily excluded for one hour, scoped to the API key and installation. Automatic selection can reassess once and then use the assigned role fallback. Authentication failures, invalid requests, server errors and timeouts do not trigger cross-model replay.
- **Clearer controls and diagnostics.** Connections now shows the full-catalog selection pool and a Model selection advisor setting instead of task-model mappings. Job details show eligible and qualified model counts, advisor suitability, budget tier and estimated savings. Advisor calls are included in COS token and cost tracking.

## Model assessments and compatibility

Suitability scores are advisor assessments, not measured accuracy guarantees, calibrated probabilities or benchmark results. The advisor uses model knowledge and available gateway metadata; unfamiliar models may remain uncertain. Missing prices are not treated as free, and image tasks do not use incomplete text-only cost comparisons.

Successful choices stay with a conversation while compatible. New directions and relevant settings, catalog, price or budget-tier changes trigger reassessment. Free-form advisor rationale is not retained in routing diagnostics. Existing action approvals and execution safeguards remain in force.

Older task mappings and the decision-only router setting are retained for configuration compatibility but no longer constrain automatic selection. Manual model selection and roles excluded from automatic selection retain their assigned models. Existing COS usage, cost and TokenOps balance features from v1.3.1 remain available.

## Upgrade

Existing users: select **Check for updates**, then **Update and restart**, and refresh the browser. Configuration, credentials and saved data are preserved. In **Connections → Automatic routing settings**, review the Model selection advisor and enabled roles. If automatic selection is already enabled, the broader catalog becomes available after the update. Personal TokenOps budget consideration remains optional.

New installations: download `chief-of-staff-v1.3.2-windows-source.zip` and `SHA256SUMS.txt`. Verify the ZIP hash before extracting, extract outside synchronized folders, then run `Setup-ChiefOfStaff.cmd`. Windows 10 or 11, Python 3.11, AMD gateway access and access to this private repository are required.

For agent-assisted setup, use `SKILL.md` or `cos-setup-v1.3.2.zip`. Credentials remain in the local user profile.

## Validation

Windows CI passed 1,539 Python tests, with 6 skipped and 19 subtests; all 99 frontend tests passed. JavaScript syntax, repository privacy and desktop/narrow browser checks passed. Live validation considered 75 eligible models and successfully executed an unassigned GPT-oss-20B model, recording advisor and execution usage. The merged application was restarted and verified healthy with routing enabled, the queue connected, monitoring enabled and TokenOps ready.
