# Worker selection

Choose by task fit, consequence, required tools, capacity and cost within the workspace's allowed targets. Discover current selectors and capabilities from supported surfaces; never require an absent local policy file or guess model aliases. Model choice grants no authority and replaces no verification.

Use the least costly tier that can reliably perform the work: mechanical for clear transformations; a standard all-rounder for bounded execution or judgment; stronger reasoning for substantial ambiguity, interacting systems, difficult-to-detect failures, high consequence or a failed standard-tier attempt. Length alone is not complexity. Consider total cost per accepted outcome, including rework.

Before dispatching a stronger-reasoning worker, write one line in its brief naming the criterion the work meets and why a standard all-rounder will not do; without that line, use the standard tier. Apply the same threshold whatever model the parent uses. Verify the selected harness has the required tools, permissions and modalities; model capability alone proves none of those. Keep long execution bounded and delegate when it preserves responsiveness.

Read this instance's model catalog (`GET /api/models`) before dispatch and use its exact `litellm_model`. Choose permitted `ready` rows; `available` alone does not establish readiness. Read `status_reason` and any reset time for unavailable/usage-limited rows. Catalog capabilities describe the model, not the harness's complete tool surface. Missing quota or utilization data is unknown, never spare capacity. Respect workspace budgets and provider terms. Do not load or switch local models without authority. Shared concurrency limits are not reserved capacity.

Pass an explicit exact permitted selector. Require accepted dispatch and an actual assistant turn. If unavailable, limited or failed, record the failure, then choose an allowed alternative or wait; no silent fallback. Inspect a quiet child's binding and last-turn error before calling it idle. Recovery must use supported tools and preserve the task's return obligation.
