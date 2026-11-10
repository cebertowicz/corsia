import type { BarPoint } from './measurementBarChart';

export interface WorkflowRun {
  run_id: string;
  focus_node_id: string;
  plan_target: { thread_id: string; step_id: string | null } | null;
  started: string;
  version: string;
  version_id: string;
  agent: { id: string; label: string | null } | null;
  cost: number | null;
  minutes: number | null;
  failed_step: string | null;
  caught: 'agent_closed' | 'sent_back' | 'corrected_later' | null;
  gate_rounds: number | null;
  corrections: number | null;
  ended: boolean;
  first_try: boolean | null;
  owner_measure: number | null;
}
export interface WeeklyWorkflow {
  start: string;
  version: string;
  version_id: string;
  runs: number;
  failed: number;
  measures: Record<Measure, MeasureValue>;
}
export interface MeasureValue { numerator: number | null; n: number; value: number | null }
export interface WorkflowVersion { id: string; label: string; started: string; runs: number; measures: Record<Measure, MeasureValue> }
export interface WorkflowEntry {
  ref: string;
  name: string | null;
  owner: { id: string; label: string | null } | null;
  runs: WorkflowRun[];
  history: WorkflowRun[];
  weekly: WeeklyWorkflow[];
  versions: WorkflowVersion[];
  measures: Record<Measure, MeasureValue>;
  agent_measures: Record<string, Record<Measure, MeasureValue>>;
  summary: string[];
  steps: string[];
  verdict: { word: string; reason: string; look: number; inputs: Record<string, unknown> } | null;
  test_start: string | null;
}
export interface WorkflowResponse {
  window: { from: string; to: string };
  coverage: { under_workflow: number; all_tasks: number };
  workflows: WorkflowEntry[];
}
export type Measure = 'corrections' | 'gate_rounds' | 'first_try' | 'cost' | 'minutes' | 'owner_measure';
export const measures: { key: Measure; label: string }[] = [
  { key: 'corrections', label: 'Your corrections per run' },
  { key: 'gate_rounds', label: 'Gate rounds per run' },
  { key: 'first_try', label: 'First-try success' },
  { key: 'cost', label: 'Cost per run' },
  { key: 'minutes', label: 'Agent working time per run' },
  { key: 'owner_measure', label: "Workflow owner's measure" },
];
export const caughtText: Record<NonNullable<WorkflowRun['caught']>, string> = {
  agent_closed: 'the agent closed it as failed', sent_back: 'you sent it back', corrected_later: 'corrected later',
};
export const dayName = (value: string) => new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' }).format(new Date(`${value}T00:00:00Z`));
export const agentName = (run: WorkflowRun) => run.agent?.label ?? '—';
export const workflowName = (entry: WorkflowEntry) => entry.name ?? entry.ref;
export function value(measures: Record<Measure, MeasureValue>, measure: Measure): number | null {
  const result = measures[measure].value;
  return result === null ? null : measure === 'first_try' ? result * 100 : result;
}
export function format(value: number | null, measure: Measure): string {
  if (value === null) return '—';
  if (measure === 'cost') return `$${value.toFixed(2)}`;
  if (measure === 'first_try') return `${Math.round(value)}%`;
  return measure === 'minutes' ? `${value.toFixed(1)} min` : value.toFixed(1);
}
export function versions(entry: WorkflowEntry): string[] {
  return entry.versions.map((version) => version.id);
}
/** A shared weekly axis; values themselves are served by Measurement. */
export function chartPoints(entry: WorkflowEntry, version: string, measure: Measure): BarPoint[] {
  const starts = [...new Set(entry.weekly.map((point) => point.start))].sort();
  if (!starts.length) return [];
  const days: string[] = [];
  let at = new Date(`${starts[0]}T00:00:00Z`);
  const end = starts[starts.length - 1];
  while (at.toISOString().slice(0, 10) <= end) {
    days.push(at.toISOString().slice(0, 10));
    at = new Date(at.getTime() + 7 * 24 * 60 * 60 * 1000);
  }
  return days.map((start) => {
    const point = entry.weekly.find((item) => item.start === start && item.version_id === version);
    const observation = point?.measures[measure];
    const raw = observation?.value ?? null;
    return { start, n: observation?.n ?? 0, value: raw === null ? null : measure === 'first_try' ? raw * 100 : raw, ma: null };
  });
}
