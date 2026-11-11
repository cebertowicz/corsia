<script lang="ts">
 import { onMount, tick } from 'svelte';
 import { get } from 'svelte/store';
 import { openSubagentRequest } from '$lib/stores/subagents';
 import { measurementScope, writeMeasurementScope } from '$lib/stores/measurement';
 import { grainScopeParams, measurementGrain } from '$lib/stores/measurementGrain';
 import { periodName } from '$lib/stores/measurementPeriod';
 import { focusPlanNode } from '$lib/planNodeFocus';
 import { emitProductEvent } from '$lib/product_events';
 import MeasurementBarChart from './MeasurementBarChart.svelte';
 import Popover from './ui/Popover.svelte';
 import Button from './ui/Button.svelte';
 import { agentName, caughtText, chartPoints, dayName, format, measures, value, versions, workflowName, type Measure, type WorkflowEntry, type WorkflowResponse, type WorkflowRun } from './workflowView';
 import { apiFetch } from '$lib/api/request';

 let response = $state<WorkflowResponse | null>(null);
 let loading = $state(true);
 let error = $state(false);
 let open = $state<string | null>(null);
 let runList = $state<{ title: string; runs: WorkflowRun[]; anchor: HTMLElement } | null>(null);
 let navigating = $state(false);
 let navigationError = $state(false);
 let controller: AbortController | null = null;
 let loadedScope = '';
 const active = $derived(response?.workflows.find((row) => row.ref === open));
 const windowLabel = $derived(response ? periodName(response.window) : 'this window');
 const activeVersions = $derived(active ? versions(active).slice(-2) : []);
 // The owner's measure shows only once a run of this workflow records one; until then its row could only read "—".
 const shownMeasures = $derived(active?.history.some((run) => run.owner_measure !== null) ? measures : measures.filter((measure) => measure.key !== 'owner_measure'));
 const runCount = (count: number) => `${count} ${count === 1 ? 'run' : 'runs'}`;
 let returnFocus = $state<number | null>(null);
 let returnScroll = $state<number | null>(null);

 async function load() {
  controller?.abort();
  const next = new AbortController(); controller = next;
  const scope = get(measurementScope);
  const nextScope = `${scope.project}:${scope.agent ?? 'none'}:${get(measurementGrain)}`;
  if (loadedScope && loadedScope !== nextScope) { response = null; open = null; runList = null; }
  loading = true; error = false;
  try {
   const result = await apiFetch(`/api/measurement/workflows?${grainScopeParams(get(measurementGrain), get(measurementScope))}`, { signal: next.signal });
   if (!result.ok) throw new Error(`workflows request failed: ${result.status}`);
   const data = await result.json() as WorkflowResponse;
   if (!Array.isArray(data.workflows) || !data.window || !data.coverage) throw new Error('workflows response shape');
   if (controller !== next) return;
   response = data; loadedScope = nextScope;
   if (open && !data.workflows.some((row) => row.ref === open)) open = null;
   if (!open && data.workflows.length === 1) open = data.workflows[0].ref;
  } catch (failure) {
   if (controller !== next) return;
   console.warn('[workflows] load failed', failure); error = true;
  } finally { if (controller === next) loading = false; }
 }
 $effect(() => { $measurementScope; $measurementGrain; void load(); return () => controller?.abort(); });
 function showRuns(event: MouseEvent, title: string, runs: WorkflowRun[]) {
  navigationError = false;
  runList = { title, runs, anchor: event.currentTarget as HTMLElement };
  emitProductEvent('measurement', 'workflow_runs_listed', { runs: runs.length });
 }
 async function openRun(run: WorkflowRun) {
  if (!run.agent || !run.plan_target) return;
  navigating = true;
  const scroller = document.querySelector<HTMLElement>('.measurement-scroller');
  const section = document.querySelector<HTMLElement>('[data-testid="workflow-overview"]');
  const openerIndex = runList && section?.parentElement ? [...section.parentElement.querySelectorAll('button.count')].indexOf(runList.anchor) : -1;
  sessionStorage.setItem('workflows-return', JSON.stringify({ scope: get(measurementScope), open, scroll: scroller?.scrollTop ?? 0, openerIndex }));
  try {
   if (!run.plan_target) throw new Error('run has no visible plan attachment');
   const { thread_id: thread, step_id: step } = run.plan_target;
   await new Promise<void>((resolve, reject) => openSubagentRequest.set({ threadId: thread, onOpened: resolve, onError: reject }));
   await tick();
   await focusPlanNode(thread, run.focus_node_id, step);
   emitProductEvent('measurement', 'workflow_run_opened', { outcome: 'succeeded' });
  } catch (failure) {
   emitProductEvent('measurement', 'workflow_run_opened', { outcome: 'failed' });
   console.warn('[workflows] plan navigation failed', failure);
   sessionStorage.removeItem('workflows-return');
   navigating = false; navigationError = true;
  }
 }
 onMount(() => {
  const raw = sessionStorage.getItem('workflows-return');
  if (!raw) return;
  sessionStorage.removeItem('workflows-return');
  const saved = JSON.parse(raw) as { scope: typeof $measurementScope; open: string; scroll: number; openerIndex: number };
  open = saved.open; writeMeasurementScope(saved.scope); returnFocus = saved.openerIndex; returnScroll = saved.scroll;
 });
 $effect(() => {
  if (returnFocus === null || !active || loading) return;
  const index = returnFocus; const scroll = returnScroll; returnFocus = null; returnScroll = null;
  void tick().then(() => {
   const scroller = document.querySelector<HTMLElement>('.measurement-scroller');
   if (scroller && scroll !== null) scroller.scrollTop = scroll;
   document.querySelectorAll<HTMLButtonElement>('[data-testid="measurement-workflows"] button.count')[index]?.focus({ preventScroll: true });
  });
 });
 function labelVersion(version: string) {
  const item = response?.workflows.flatMap((entry) => entry.versions).find((entry) => entry.id === version);
  return item ? `version ${item.label} (${dayName(item.started)})` : 'version';
 }
 function status(entry: WorkflowEntry) {
  const count = versions(entry).length;
  if (count < 2 || entry.verdict?.reason === 'no_baseline') return `${runCount(entry.history.length)} so far. Nothing to compare yet.`;
  if (entry.verdict?.word === 'collecting') return `Comparing ${labelVersion(versions(entry).at(-1)!)}: ${runCount(entry.runs.length)} in ${windowLabel}.`;
  return `${runCount(entry.runs.length)} in ${windowLabel}.`;
 }
 function chartMarks(entry: WorkflowEntry, version: string, measure: Measure) {
  const points = chartPoints(entry, version, measure);
  const versionData = entry.versions.find((item) => item.id === version);
  const first = versionData?.started;
  const testStart = entry.test_start;
  return [first && versionData && { date: first, label: `Version ${versionData.label} starts ${dayName(first)}` },
   activeVersions.length > 1 && testStart && { date: testStart, label: `Test starts ${dayName(testStart)}` }]
   .filter((mark): mark is { date: string; label: string } => Boolean(mark))
   .map((mark) => ({ index: points.findIndex((point) => {
    const start = Date.parse(`${point.start}T00:00:00Z`);
    const date = Date.parse(`${mark.date}T00:00:00Z`);
    return start <= date && date < start + 7 * 86_400_000;
   }), label: mark.label }))
   .filter((mark) => mark.index >= 0);
 }
</script>

{#snippet count(runs: WorkflowRun[], of: number, title: string)}
 {#if runs.length}<button class="count" type="button" onclick={(event) => showRuns(event, title, runs)}>{runs.length} of {of}</button>{:else}<span>0 of {of}</span>{/if}
{/snippet}
{#snippet chart(entry: WorkflowEntry, measure: Measure, label: string, version: string)}
 {@const points = chartPoints(entry, version, measure)}
 {@const all = activeVersions.flatMap((item) => chartPoints(entry, item, measure))}
 {#if points.length}<div class="trend"><h5>{labelVersion(version)}</h5><MeasurementBarChart {points} percent100={measure === 'first_try'} scaleMax={Math.max(0, ...all.map((point) => point.value ?? 0)) || 1} axisTitle={`${label}${measure === 'minutes' ? ' (minutes)' : ''}`} averageName="4-week average" caption="Weekly runs; n is runs per week." missingText="not recorded" minN={0} minNText="fewer than 5 runs" ariaLabel={`${label} by week, ${labelVersion(version)}`} tickFormat={(v,d) => measure === 'cost' ? `$${v.toFixed(d)}` : measure === 'first_try' ? `${v.toFixed(d)}%` : v.toFixed(d)} valueFormat={(v) => measure === 'minutes' ? v.toFixed(1) : format(v, measure)} marks={chartMarks(entry, version, measure)} /></div>{/if}
{/snippet}
<div class="workflows" data-testid="measurement-workflows">
 <section aria-labelledby="workflows-title" data-testid="workflow-overview">
  <h3 id="workflows-title">Workflows</h3>
  {#if loading && !response}<div class="skeleton" role="status" aria-label="Loading workflows"></div>
  {:else if error}<p role="alert">Workflows couldn't load. <Button variant="ghost" size="sm" onclick={() => void load()}>Retry</Button></p>
  {:else if response}
   {#if loading}<p role="status" aria-live="polite">Refreshing…</p>{/if}
   {#if !response.workflows.length}<p>No work has run under a workflow yet: {response.coverage.under_workflow} of {response.coverage.all_tasks} tasks, {windowLabel}.</p>
   {:else}
    <p>{response.coverage.all_tasks ? `${Math.round(100 * response.coverage.under_workflow / response.coverage.all_tasks)}%` : '—'} of tasks ran under a workflow, {windowLabel} ({response.coverage.under_workflow} of {response.coverage.all_tasks}).</p>
    <!-- A scrollable region needs focus for keyboard panning, as in MeasurementAgentsTable. -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <div class="table-wrap" role="region" aria-label="Workflows table, scrolls sideways when it is wider than the screen" tabindex="0"><table class="list">
     <thead><tr><th>Workflow</th><th>Owner</th><th>Runs</th><th>What's happening</th><th>First-try success</th><th>Your corrections per run</th><th>Cost per run</th></tr></thead>
     <tbody>{#each response.workflows as row (row.ref)}<tr class:selected={open === row.ref}>
      <th scope="row"><button class="row-action" aria-expanded={open === row.ref} onclick={() => { open = open === row.ref ? null : row.ref; if (open) emitProductEvent('measurement', 'workflow_opened'); }}>{workflowName(row)}</button></th>
      <td data-label="Owner">{row.owner?.label ?? '—'}</td><td data-label="Runs">{row.runs.length}</td><td data-label="What's happening">{status(row)}</td>
      <td data-label="First-try success">{format(value(row.measures, 'first_try'), 'first_try')}</td><td data-label="Your corrections per run">{format(value(row.measures, 'corrections'), 'corrections')}</td><td data-label="Cost per run">{format(value(row.measures, 'cost'), 'cost')}{row.runs.some((run) => run.cost === null) && row.runs.some((run) => run.cost !== null) ? '*' : ''}</td>
     </tr>{/each}</tbody>
    </table></div>
    {#if response.workflows.some((row) => row.runs.some((run) => run.cost === null) && row.runs.some((run) => run.cost !== null))}<p class="note">* Some calls were unpriced; the figure covers priced calls only.</p>{/if}
    <p class="note">Your corrections per run: what you asked to change, sent back or corrected after approving, divided by runs.</p>
   {/if}
  {/if}
 </section>
 {#if active}<section class="detail" aria-labelledby="detail-title" data-testid="workflow-detail">
  <h3 id="detail-title">{workflowName(active)}</h3>
  {#if activeVersions.length > 1}
   <p>{labelVersion(activeVersions[1])} against {labelVersion(activeVersions[0])}.</p>
   <div class="table-wrap"><table><thead><tr><th>Measure</th>{#each activeVersions as version}<th>{labelVersion(version)}</th>{/each}</tr></thead><tbody>
    <tr><th scope="row">Runs</th>{#each activeVersions as version}<td>{active.versions.find((item) => item.id === version)?.runs}</td>{/each}</tr>
    {#each shownMeasures as measure}<tr><th scope="row">{measure.label}</th>{#each activeVersions as version}{@const versionData = active.versions.find((item) => item.id === version)!}<td>{format(value(versionData.measures, measure.key), measure.key)}{#if versionData.measures[measure.key].n && ['first_try', 'corrections', 'gate_rounds'].includes(measure.key)}<span class="sub">{versionData.measures[measure.key].numerator} in {versionData.measures[measure.key].n} runs</span>{/if}</td>{/each}</tr>{/each}
   </tbody></table></div>
   {#if active.summary.length}<ul class="result">{#each active.summary as line}<li>{line}</li>{/each}</ul>{/if}
  {:else}<p>{status(active)}</p>{/if}
  <div class="trends">{#each shownMeasures as measure}{#each activeVersions as version}{@render chart(active, measure.key, measure.label, version)}{/each}{/each}</div>
  <h4>Which step fails, {windowLabel}</h4>
  <div class="table-wrap"><table class="run-counts"><thead><tr><th>Step</th>{#each activeVersions as version}<th>{labelVersion(version)}</th>{/each}</tr></thead><tbody>{#each active.steps as step}<tr><th scope="row">{step}</th>{#each activeVersions as version}{@const runs = active.runs.filter((run) => run.version_id === version)}<td>{@render count(runs.filter((run) => run.failed_step === step), runs.length, `${step} failed, ${labelVersion(version)}`)}</td>{/each}</tr>{/each}</tbody></table></div>
  <h4>Who ran it, {windowLabel}</h4>
  <div class="table-wrap"><table class="run-counts"><thead><tr><th>Agent</th><th>Runs</th><th>Runs with a failed step</th><th>Cost per run</th></tr></thead><tbody>{#each [...new Set(active.runs.map((run) => run.agent?.id ?? ''))] as agent}{@const runs = active.runs.filter((run) => (run.agent?.id ?? '') === agent)}<tr><th scope="row">{agentName(runs[0])}</th><td><button class="count" onclick={(event) => showRuns(event, `${agentName(runs[0])}, all runs`, runs)}>{runs.length}</button></td><td>{@render count(runs.filter((run) => run.failed_step !== null), runs.length, `${agentName(runs[0])}, runs with a failed step`)}</td><td>{format(value(active.agent_measures[agent], 'cost'), 'cost')}</td></tr>{/each}</tbody></table></div>
  <p><button class="count" onclick={(event) => showRuns(event, `${workflowName(active!)}, all runs`, active!.runs)}>Show all {runCount(active.runs.length)}</button></p>
 </section>{/if}
 {#if runList}<Popover anchor={runList.anchor} open onClose={() => runList = null} placement="bottom-end" surfaceRole={null}>
  {#snippet children()}<div class="picker-panel" data-testid="workflow-runs"><p class="list-title">{runList!.title}: {runList!.runs.length} {runList!.runs.length === 1 ? 'run' : 'runs'}</p>
   <div class="picker-list">{#each runList!.runs as run}<button class="picker-option" onclick={() => void openRun(run)} disabled={navigating || !run.agent || !run.plan_target}><span>{dayName(run.started)} · {agentName(run)} · {labelVersion(run.version_id)}</span><span class="sub">{run.failed_step ? `${run.failed_step} failed${run.caught ? `: ${caughtText[run.caught]}` : ''}` : 'No failed step'}</span></button>{/each}</div>
   {#if navigating}<p role="status">Opening plan…</p>{:else if navigationError}<p role="alert">Couldn't open the plan. Try again.</p>{:else}<p class="sub">Each run opens in its plan, at the run.</p>{/if}
  </div>{/snippet}
 </Popover>{/if}
</div>
<style>
 .workflows,section.detail {display:grid;gap:var(--space-4);min-width:0} section{min-width:0} h3,h4,h5,p{margin:0;overflow-wrap:anywhere} h5{color:var(--text-secondary);font-size:var(--text-caption);font-weight:var(--fw-semibold)} h3{color:var(--text-primary);font-size:var(--text-body);font-weight:var(--fw-semibold)} h4{color:var(--text-tertiary);font-size:var(--text-footnote);font-weight:var(--fw-semibold)}
 p,table,.result{color:var(--text-primary);font-size:var(--text-subhead)} .result{margin:0;padding-left:1.5em} .detail tbody th,.detail tbody td{vertical-align:middle} .note,.sub{color:var(--text-tertiary);font-size:var(--text-caption)} .sub{display:block}
 .table-wrap{overflow-x:auto;min-width:0} table{width:100%;border-collapse:collapse} th,td{text-align:left;vertical-align:top;padding:var(--space-2)} tbody th{overflow-wrap:anywhere} thead th{color:var(--text-tertiary);font-size:var(--text-footnote);font-weight:var(--fw-semibold)} tbody th{font-weight:var(--fw-semibold)}
 .detail table:not(.run-counts){table-layout:fixed} .detail table:not(.run-counts) th:first-child{width:55%} .detail table:not(.run-counts) th{overflow-wrap:anywhere}
 .run-counts tbody tr{height:calc(var(--touch-target) + 2 * var(--space-2))} .list tr{min-height:var(--list-row-min-height)} .list thead th:nth-child(3){white-space:nowrap} .list tbody tr:hover{background:var(--bg-hover)} .list tbody tr.selected{background:var(--accent-wash)}
 button.row-action,button.count{font:inherit;cursor:pointer;background:none;border:0;min-width:var(--touch-target);min-height:var(--touch-target);padding:var(--space-1);text-align:left} button.row-action{display:flex;align-items:flex-start;color:var(--text-primary);overflow-wrap:anywhere} button.count{color:var(--accent-text);text-decoration:underline;margin-inline-start:calc(-1 * var(--space-1))} button:focus-visible,button.count:focus{outline:2px solid var(--accent);outline-offset:2px}
 .trends{display:grid;grid-template-columns:1fr;gap:var(--space-5)} .trend{min-width:0} .picker-panel,.picker-list{display:flex;flex-direction:column;gap:var(--space-2)} .picker-list{max-height:45vh;overflow:auto} .picker-option{min-height:var(--list-row-min-height);border:0;border-radius:var(--radius-sm);background:transparent;color:var(--text-primary);font:inherit;text-align:left;cursor:pointer;padding:var(--space-2)} .picker-option:hover{background:var(--bg-hover)} .list-title{font-size:var(--text-footnote);font-weight:var(--fw-semibold);color:var(--text-tertiary)} .skeleton{height:var(--space-8);background:var(--bg-hover)}
 @media(min-width:56.25rem){.trends{grid-template-columns:repeat(2,minmax(0,1fr))}} @media(max-width:37.5rem){.list thead{display:none}.list,.list tbody,.list tr,.list th,.list td{display:block;width:auto}.list tr{padding:var(--space-2)}.list th,.list td{max-width:none;padding:var(--space-1)}.list td::before{content:attr(data-label) ': ';color:var(--text-tertiary)}}
</style>
