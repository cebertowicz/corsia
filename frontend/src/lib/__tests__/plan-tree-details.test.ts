import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import type { OperatorNode } from '$lib/api/plans';
import { node } from './fixtures/planTreeNode';
import { withFolds } from './fixtures/planFolds';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

async function settle() {
	await tick();
	flushSync();
}

describe('PlanTree row details disclosure', () => {
	async function open(target: OperatorNode): Promise<string> {
		component = mount(PlanTree, { target: host, props: withFolds({ nodes: [target] }) });
		flushSync();
		(host.querySelector('.row-body') as HTMLButtonElement).click();
		await settle();
		return host.textContent ?? '';
	}

	const squash = (text: string | null | undefined) => text?.replace(/\s+/g, ' ').trim() ?? null;

	/** The visible text of one labelled detail fact. */
	function line(kind: string): string | null {
		const el = host.querySelector(`.node-details > .${kind}`);
		if (!el) return null;
		return squash((el.querySelector(':scope > summary') ?? el).textContent);
	}

	/** The lines the view renders, top to bottom; there is no label column. */
	function lines(): string[] {
		return [...host.querySelectorAll('.node-details > *')].map((el) => el.className.split(' ')[0]);
	}

	it('opens to labelled facts, keeps tries visible, and states no default mover', async () => {
		const detailed = node({
			id: 'details',
			title: 'Detailed task',
			requirements_revision: 4,
			executor: { thread_id: 'agent-1', label: 'Research agent' },
			execution: {
				condition: 'blocked',
				primary_reason: 'Dependency is incomplete',
				causes: [{
					kind: 'blocked', reason_key: 'dependency_incomplete',
					reason: 'Dependency is incomplete', owner_kind: 'thread',
					owner_id: 'agent-2', dependency_node_id: 'upstream',
				}],
				selection: null, conflict: null,
				attempt_count: 25,
				friction_count: 0,
				attempts: [{
					id: 'attempt-1', ordinal: 1,
					executor: { thread_id: 'agent-1', label: 'Research agent' },
					requirements_revision: 3, selected_at: 'earlier',
					released_at: 'later', release_reason: 'requirements_changed',
				}],
			},
			commitment_cost: {
				commitment_id: 'details',
				attempts: { total: 2, failed: 1, correction: 1, delegated: 0 },
				tokens: { known: 1200, unknown_rows: 0 },
				model_cost_usd: { known: 0.25, unknown_rows: 1 },
					usage_recording_failures: 0,
				elapsed_seconds: { known: 90, status: 'in_progress' },
				user_effort: { known_plan_actions: 2, status: 'partial' },
			},
		});
		// The row itself is the details control; there is no separate Details button.
		component = mount(PlanTree, { target: host, props: withFolds({ nodes: [detailed] }) });
		flushSync();
		expect([...host.querySelectorAll('button')].some((b) => b.textContent?.trim() === 'Details')).toBe(false);
		const button = host.querySelector('.row-body') as HTMLButtonElement;
		expect(button.tagName).toBe('BUTTON');
		expect(button.getAttribute('aria-controls')).toBe('plan-node-details-details');
		button.click();
		await settle();
		expect(button.getAttribute('aria-expanded')).toBe('true');

		// No label column: every fact is its own labelled line.
		expect(host.querySelector('.node-details dl, .node-details dt')).toBeNull();
		expect(lines()).toEqual(['status', 'tries', 'follow-ups']);
		// A dependency wait stays neutral; the exact cause remains beside the four-state word.
		expect(line('status')).toBe('Status: Waiting · Dependency is incomplete');
		const word = host.querySelector('.node-details .word')!;
		expect(word.classList.contains('waiting')).toBe(true);
		expect(word.classList.contains('failed')).toBe(false);

		const text = host.textContent ?? '';
		// An agent moving its own work is the default and is not stated; the worker lives on each try.
		for (const gone of ['Next', 'An agent', 'Worker', 'Whole plan so far', 'Evidence', 'upstream']) {
			expect(text).not.toContain(gone);
		}
		expect(line('tries')).toBe(
			'Tries: 25 (latest 1 shown) Try 1 · Research agent · requirements changed',
		);
		// Nothing ran on the task itself, so its follow-ups' known total is its own line.
		expect(line('follow-ups')).toBe('With revisions and follow-ups: at least $0.2500 API-equivalent · 1.2K tokens');
		expect(host.querySelector('.node-details details, .node-details button.reveal')).toBeNull();
	});

	it('names who moves next only when it is not an agent', async () => {
		await open(node({
			id: 'outside',
			execution: {
				condition: 'waiting', primary_reason: 'Waiting on CI', selection: null, conflict: null,
				causes: [{
					kind: 'waiting', reason_key: 'watch', reason: 'Waiting on CI',
					owner_kind: 'external', owner_id: null,
				}],
				attempt_count: 0, friction_count: 0, attempts: [],
			},
		}));
		expect(lines()).toEqual(['status', 'next']);
		expect(line('next')).toBe('Next: Outside CORSIA');
	});

	it('keeps a status reason static when it has no further detail', async () => {
		await open(node({
			id: 'one-reason',
			execution: {
				condition: 'waiting', primary_reason: 'Waiting on CI', selection: null, conflict: null,
				causes: [{
					kind: 'waiting', reason_key: 'watch', reason: 'Waiting on CI',
					owner_kind: 'external', owner_id: null,
				}],
				attempt_count: 0, friction_count: 0, attempts: [],
			},
		}));
		expect(line('status')).toBe('Status: Waiting · Waiting on CI');
		expect(host.querySelector('.node-details details.status, .node-details button.status')).toBeNull();
	});

	it('shows why a finished step is done and when its evidence is stale', async () => {
		await open(node({
			id: 'finished', state: 'accepted', requirements_revision: 3,
			executor: { thread_id: 'agent-1', label: 'Research agent' },
			execution: {
				condition: 'terminal', primary_reason: null, causes: [], selection: null, conflict: null,
				attempt_count: 1, friction_count: 0, attempts: [],
			},
			evidence: { evidence: 'Targeted proof passed', artifact_ids: ['proof'], requirements_revision: 2 },
		}));
		expect(lines()).toEqual(['status', 'tries', 'evidence', 'evidence-note', 'evidence-note']);
		expect(line('status')).toBe('Status: Done');
		expect(line('evidence')).toBe('Evidence: Targeted proof passed');
		expect([...host.querySelectorAll('.evidence-note')].map((el) => squash(el.textContent)))
			.toEqual(['Predates the latest requirements', '1 artifact linked']);
		expect(host.textContent).not.toContain('Research agent');
	});

	it.each([
		['accepted', 'complete', 'Done'],
		['closed', 'closed', 'Won’t do'],
		['failed', 'failed', 'Failed'],
	] as const)('uses the row outcome word in the details for a %s task', async (state, tone, label) => {
		await open(node({
			id: `settled-${state}`, state,
			display: { label_key: 'finished', tone_key: tone },
		}));
		expect(line('status')).toBe(`Status: ${label}`);
	});

	it('names who marked a finished step done beside its status', async () => {
		await open(node({
			id: 'marked', state: 'accepted', status_reason: 'Marked by Infrastructure',
			evidence: { evidence: 'You said the key is set', artifact_ids: [], requirements_revision: 1 },
		}));
		expect(line('status')).toBe('Status: Done · Marked by Infrastructure');
		expect(line('evidence')).toBe('Evidence: You said the key is set');
	});

	it('keeps evidence static when there are no hidden notes', async () => {
		await open(node({
			id: 'plain-evidence', state: 'accepted',
			evidence: { evidence: 'Targeted proof passed', artifact_ids: [], requirements_revision: 1 },
		}));
		expect(line('evidence')).toBe('Evidence: Targeted proof passed');
		expect(host.querySelector('.node-details details.evidence')).toBeNull();
	});

	it('says so when a finished step has no evidence', async () => {
		await open(node({ id: 'closed', state: 'accepted' }));
		expect(line('evidence')).toBe('Evidence: none recorded');
		expect(host.querySelector('.node-details details')).toBeNull();
	});

	it('separates working time and known cost, including known step-only facts', async () => {
		const turns = { completed: 3, error: 0, cancelled: 0, running: 0, waiting_retry: 0 };
		await open(node({
			id: 'costed',
			node_cost: {
				rolled: {
					tokens: { known: 1_800_000, unknown_rows: 2 },
					model_cost_usd: { known: null, unknown_rows: 5 },
					usage_recording_failures: 1,
					execution: { seconds: 720, unknown_turns: 1, turns },
					waiting: { seconds: 0 },
				},
				own: {
					tokens: { known: 300_000, unknown_rows: 0 },
					model_cost_usd: { known: null, unknown_rows: 1 },
					usage_recording_failures: 0,
					execution: { seconds: 120, unknown_turns: 0, turns },
					waiting: { seconds: 0 },
				},
			},
		}));
		expect(lines()).toEqual(['status', 'working', 'tokens', 'own-working', 'own-tokens']);
		expect(line('working')).toBe('Working: at least 12m 0s');
		expect(line('tokens')).toBe('Tokens: at least 1.8M');
		expect(line('own-working')).toBe('This step working: 2m 0s');
		expect(line('own-tokens')).toBe('This step tokens: 300K');
		expect(host.textContent).not.toMatch(/unknown|unpriced|not reported|not timed|usage missing/i);
	});

	it('renders working and waiting as separate facts and shows the step-only wait', async () => {
		const turns = { completed: 1, error: 0, cancelled: 0, running: 0, waiting_retry: 0 };
		const base = {
			tokens: { known: 1_000, unknown_rows: 0 },
			model_cost_usd: { known: 0.5, unknown_rows: 0 },
			usage_recording_failures: 0,
			execution: { seconds: 7_800, unknown_turns: 0, turns },
		};
		await open(node({
			id: 'waited',
			node_cost: {
				own: { ...base, waiting: { seconds: 600 } },
				rolled: { ...base, waiting: { seconds: 18_180 } },
			},
		}));
		expect(line('working')).toBe('Working: 2h 10m');
		expect(line('waiting')).toBe('Waiting: 5h 3m');
		expect(line('tokens')).toBe('Tokens: 1K');
		expect(line('cost')).toBe('Cost: $0.5000 API-equivalent');
		expect(line('own-waiting')).toBe('This step waiting: 10m 0s');
		expect(line('own-working')).toBeNull();
		expect(line('own-cost')).toBeNull();
	});

	it('omits missing usage diagnostics when no cost fact is known', async () => {
		const amount = {
			tokens: { known: null, unknown_rows: 0 },
			model_cost_usd: { known: null, unknown_rows: 0 },
			usage_recording_failures: 1,
			execution: {
				seconds: 0, unknown_turns: 0,
				turns: { completed: 0, error: 0, cancelled: 0, running: 0, waiting_retry: 0 },
			},
			waiting: { seconds: 0 },
		};
		await open(node({ id: 'missing-cost', node_cost: { own: amount, rolled: amount } }));
		expect(line('tokens')).toBe('Tokens: not recorded');
		expect(line('cost')).toBeNull();
	});

	it('omits an unpriced-call count when it is the only cost input', async () => {
		const amount = {
			tokens: { known: null, unknown_rows: 0 },
			model_cost_usd: { known: null, unknown_rows: 1 },
			usage_recording_failures: 0,
			execution: {
				seconds: 0, unknown_turns: 0,
				turns: { completed: 0, error: 0, cancelled: 0, running: 0, waiting_retry: 0 },
			},
			waiting: { seconds: 0 },
		};
		await open(node({ id: 'unpriced-only', node_cost: { own: amount, rolled: amount } }));
		expect(line('tokens')).toBeNull();
		expect(line('cost')).toBeNull();
		expect(host.textContent).not.toMatch(/unpriced|unknown/i);
	});

	it('does not repeat the cost as a follow-up total when there are no follow-ups', async () => {
		const turns = { completed: 1, error: 0, cancelled: 0, running: 0, waiting_retry: 0 };
		const amount = {
			tokens: { known: 5_000, unknown_rows: 0 },
			model_cost_usd: { known: 0.5, unknown_rows: 0 },
			usage_recording_failures: 0,
			execution: { seconds: 60, unknown_turns: 0, turns },
			waiting: { seconds: 0 },
		};
		await open(node({
			id: 'solo',
			node_cost: { own: amount, rolled: amount },
			commitment_cost: {
				commitment_id: 'solo',
				attempts: { total: 0, failed: 0, correction: 0, delegated: 0 },
				tokens: amount.tokens,
				model_cost_usd: amount.model_cost_usd,
					usage_recording_failures: 0,
				elapsed_seconds: { known: 60, status: 'in_progress' },
				user_effort: { known_plan_actions: 0, status: 'partial' },
			},
		}));
		expect(lines()).toEqual(['status', 'working', 'tokens', 'cost']);
		expect(host.querySelector('.node-details details')).toBeNull();
		expect(host.textContent).not.toContain('follow-ups');
	});

	it('shows the follow-up total only when it reads differently from the task total', async () => {
		const turns = { completed: 1, error: 0, cancelled: 0, running: 0, waiting_retry: 0 };
		const amount = (known: number) => ({
			tokens: { known, unknown_rows: 0 },
			model_cost_usd: { known: null, unknown_rows: 0 },
			usage_recording_failures: 0,
			execution: { seconds: 60, unknown_turns: 0, turns },
			waiting: { seconds: 0 },
		});
		const commitment = (known: number) => ({
			commitment_id: 'c',
			attempts: { total: 3, failed: 0, correction: 0, delegated: 0 },
			tokens: { known, unknown_rows: 0 },
			model_cost_usd: { known: null, unknown_rows: 0 },
			usage_recording_failures: 0,
			elapsed_seconds: { known: 60, status: 'in_progress' as const },
			user_effort: { known_plan_actions: 0, status: 'partial' as const },
		});
		// A few exact tokens apart, the same "18.8M tokens" on screen: one line, not two.
		await open(node({
			id: 'same-on-screen',
			node_cost: { own: amount(18_788_400), rolled: amount(18_788_400) },
			commitment_cost: commitment(18_788_517),
		}));
		expect(line('tokens')).toBe('Tokens: 18.8M');
		expect(host.textContent).not.toContain('follow-ups');
	});

	it('keeps the follow-up total when a later fix moves it', async () => {
		const turns = { completed: 1, error: 0, cancelled: 0, running: 0, waiting_retry: 0 };
		const amount = {
			tokens: { known: 18_788_400, unknown_rows: 0 },
			model_cost_usd: { known: null, unknown_rows: 0 },
			usage_recording_failures: 0,
			execution: { seconds: 60, unknown_turns: 0, turns },
			waiting: { seconds: 0 },
		};
		await open(node({
			id: 'differs-on-screen',
			node_cost: { own: amount, rolled: amount },
			commitment_cost: {
				commitment_id: 'c',
				attempts: { total: 3, failed: 0, correction: 0, delegated: 0 },
				tokens: { known: 25_000_000, unknown_rows: 0 },
				model_cost_usd: { known: null, unknown_rows: 0 },
				usage_recording_failures: 0,
				elapsed_seconds: { known: 60, status: 'in_progress' as const },
				user_effort: { known_plan_actions: 0, status: 'partial' as const },
			},
		}));
		expect(host.textContent).toContain('With revisions and follow-ups: 25M tokens');
	});

	it.each([
		['active', 'running'], ['review_requested', 'review'], ['accepted', 'terminal'], ['failed', 'terminal'],
	] as const)('keeps a single %s try reachable without repeating its count', async (state, condition) => {
		await open(node({
			id: `one-${state}`, state,
			execution: {
				condition, primary_reason: null, causes: [], selection: null, conflict: null,
				attempt_count: 1, friction_count: 0,
				attempts: [{
					id: 'try-1', ordinal: 1,
					executor: { thread_id: 'worker', label: 'Research agent' },
					requirements_revision: 1, selected_at: 'earlier', released_at: null, release_reason: null,
				}],
			},
		}));
		expect(line('status')).not.toContain('Research agent');
		expect(line('tries')).toBe('Try 1 · Research agent · current');
		expect(line('tries')).not.toContain('Tries: 1');
	});

	it('shows the count when a task has two tries', async () => {
		await open(node({
			id: 'two-tries',
			execution: {
				condition: 'running', primary_reason: null, causes: [], selection: null, conflict: null,
				attempt_count: 2, friction_count: 1,
				attempts: [
					{ id: 'try-2', ordinal: 2, executor: { thread_id: 'worker', label: 'Research agent' }, requirements_revision: 1, selected_at: 'later', released_at: null, release_reason: null },
					{ id: 'try-1', ordinal: 1, executor: { thread_id: 'worker', label: 'Research agent' }, requirements_revision: 1, selected_at: 'earlier', released_at: 'later', release_reason: 'requirements_changed' },
				],
			},
		}));
		expect(line('tries')).toContain('Tries: 2');
		expect(line('tries')).toContain('Try 1 · Research agent · requirements changed');
		expect(line('tries')).toContain('Try 2 · Research agent · current');
	});

	it('states only the missing portion of a partially listed try history', async () => {
		await open(node({
			id: 'partially-listed-tries',
			execution: {
				condition: 'terminal', primary_reason: null, causes: [], selection: null, conflict: null,
				attempt_count: 3, friction_count: 2,
				attempts: [{
					id: 'try-3', ordinal: 3, executor: { thread_id: 'worker', label: 'Research agent' },
					requirements_revision: 1, selected_at: 'later', released_at: null, release_reason: null,
				}],
			},
		}));
		expect(line('tries')).toBe('Tries: 3 (latest 1 shown) Try 3 · Research agent · current');
	});

	it('retains a one-try count with no listed try without a nonsense latest-zero suffix', async () => {
		await open(node({
			id: 'unlisted-single-try',
			execution: {
				condition: 'terminal', primary_reason: null, causes: [], selection: null, conflict: null,
				attempt_count: 1, friction_count: 0, attempts: [],
			},
		}));
		expect(line('tries')).toBe('Tries: 1');
		expect(line('tries')).not.toContain('latest 0 shown');
	});

	it('does not invent a subtree difference from unknown-only usage', async () => {
		const empty = {
			tokens: { known: 100, unknown_rows: 0 },
			model_cost_usd: { known: 0.1, unknown_rows: 0 },
			usage_recording_failures: 0,
			execution: { seconds: 60, unknown_turns: 0, turns: { completed: 1, error: 0, cancelled: 0, running: 0, waiting_retry: 0 } },
			waiting: { seconds: 0 },
		};
		const rolled = {
			...empty, tokens: { known: 100, unknown_rows: 1 },
			model_cost_usd: { known: 0.1, unknown_rows: 1 },
		};
		await open(node({ id: 'unknown-child', node_cost: { own: empty, rolled } }));
		expect(line('tokens')).toBe('Tokens: at least 100');
		expect(line('cost')).toBe('Cost: at least $0.1000 API-equivalent');
		expect(line('own-tokens')).toBeNull();
		expect(line('own-cost')).toBeNull();
		expect(host.textContent).not.toMatch(/not reported|unpriced|unknown/i);
	});

	it('omits follow-up rows that add only unknown usage or recording failures', async () => {
		const amount = {
			tokens: { known: 100, unknown_rows: 0 },
			model_cost_usd: { known: 0.1, unknown_rows: 0 },
			usage_recording_failures: 0,
			execution: { seconds: 60, unknown_turns: 0, turns: { completed: 1, error: 0, cancelled: 0, running: 0, waiting_retry: 0 } },
			waiting: { seconds: 0 },
		};
		await open(node({
			id: 'unknown-follow-up', node_cost: { own: amount, rolled: amount },
			commitment_cost: {
				commitment_id: 'unknown-follow-up', attempts: { total: 0, failed: 0, correction: 0, delegated: 0 },
				tokens: { known: 100, unknown_rows: 2 }, model_cost_usd: { known: 0.1, unknown_rows: 1 },
				usage_recording_failures: 1,
				elapsed_seconds: { known: 60, status: 'complete' },
				user_effort: { known_plan_actions: 0, status: 'partial' },
			},
		}));
		expect(line('follow-ups')).toBeNull();
		expect(host.textContent).not.toMatch(/unpriced|not reported|usage missing/i);
	});

	it('renders only the status line when nothing else has something to say', async () => {
		await open(node({ id: 'bare' }));
		expect(lines()).toEqual(['status']);
		expect(line('status')).toBe('Status: Waiting');
	});

	it('renders time and cost as stable labelled facts without unknown-value chrome', async () => {
		const turns = { completed: 3, error: 0, cancelled: 0, running: 0, waiting_retry: 0 };
		await open(node({
			id: 'scannable-details',
			node_cost: {
				rolled: {
					tokens: { known: 1_800_000, unknown_rows: 2 },
					model_cost_usd: { known: null, unknown_rows: 5 },
					usage_recording_failures: 1,
					execution: { seconds: 720, unknown_turns: 1, turns },
					waiting: { seconds: 18_180 },
				},
				own: {
					tokens: { known: 300_000, unknown_rows: 0 },
					model_cost_usd: { known: null, unknown_rows: 1 },
					usage_recording_failures: 0,
					execution: { seconds: 120, unknown_turns: 0, turns },
					waiting: { seconds: 600 },
				},
			},
		}));

		expect(lines()).toEqual(['status', 'working', 'waiting', 'tokens', 'own-working', 'own-waiting', 'own-tokens']);
		expect(line('working')).toBe('Working: at least 12m 0s');
		expect(line('waiting')).toBe('Waiting: 5h 3m');
		expect(line('tokens')).toBe('Tokens: at least 1.8M');
		expect(line('cost')).toBeNull();
		expect(line('own-working')).toBe('This step working: 2m 0s');
		expect(line('own-waiting')).toBe('This step waiting: 10m 0s');
		expect(line('own-tokens')).toBe('This step tokens: 300K');
		expect(line('own-cost')).toBeNull();
		expect(host.querySelector('.node-details details, .node-details button.reveal')).toBeNull();
		expect(host.textContent).not.toMatch(/unknown|unpriced|not reported|not timed|partly measured/i);
	});
});

describe('PlanTree branch rows', () => {
	it('opens details from the row and expands only from the count', async () => {
		const nodes = [
			node({ id: 'parent', title: 'Parent step', branch_progress: { done: 1, total: 2 } }),
			node({ id: 'child', title: 'Child step', parent_id: 'parent', depth: 1 }),
		];
		component = mount(PlanTree, { target: host, props: withFolds({ nodes }) });
		flushSync();
		const body = host.querySelector('.row-body') as HTMLButtonElement;
		const expand = host.querySelector('.branch-toggle') as HTMLButtonElement;
		expect(body.contains(expand)).toBe(false);
		expect(expand.textContent).toContain('1/2');
		expect(body.textContent).not.toContain('1/2');

		body.click();
		await settle();
		expect(body.getAttribute('aria-expanded')).toBe('true');
		expect(expand.getAttribute('aria-expanded')).toBe('false');
		expect(host.textContent).not.toContain('Child step');

		expand.click();
		await settle();
		expect(expand.getAttribute('aria-expanded')).toBe('true');
		expect(host.textContent).toContain('Child step');
	});
});

describe('PlanTree user task controls', () => {
	it('keeps lifecycle actions out of expanded details when the row has a task menu', async () => {
		component = mount(PlanTree, {
			target: host,
			props: withFolds({
				nodes: [node({
					id: 'mine', title: 'Sign the contract',
					user_actions: ['mark_done', 'pause_node', 'close_node'],
				})],
				onUserAction: () => {},
			}),
		});
		flushSync();

		const rowBody = host.querySelector('.row-body') as HTMLButtonElement;
		expect(rowBody.getAttribute('aria-haspopup')).toBe('menu');
		rowBody.click();
		await settle();

		expect(host.querySelector('.node-details')).toBeTruthy();
		expect(host.querySelector('.node-details .user-actions')).toBeNull();
		expect(host.querySelector('.node-details button')).toBeNull();
	});

	it('still says the user moves next on an agent task blocked on them', async () => {
		component = mount(PlanTree, {
			target: host,
			props: withFolds({
				nodes: [node({
					id: 'theirs', user_actions: ['pause_node', 'close_node'],
					execution: {
						condition: 'blocked', primary_reason: 'Needs your sign-off',
						causes: [{
							kind: 'blocked', reason_key: 'needs_user', reason: 'Needs your sign-off',
							owner_kind: 'user', owner_id: null,
						}],
						selection: null, conflict: null, attempt_count: 0, friction_count: 0, attempts: [],
					},
				})],
				onUserAction: () => {},
			}),
		});
		flushSync();
		(host.querySelector('.row-body') as HTMLButtonElement).click();
		await settle();

		expect(host.querySelector('.node-details > .next')?.textContent?.replace(/\s+/g, ' ').trim())
			.toBe('Next: You');
		expect(host.querySelector('.user-actions')).toBeNull();
	});

	it('passes the exact row indent to details at deep nested levels', async () => {
		const nodes = Array.from({ length: 9 }, (_, depth) => node({
			id: `depth-${depth}`,
			parent_id: depth === 0 ? null : `depth-${depth - 1}`,
			depth,
			kind: depth === 8 ? 'task' : 'phase',
			title: `Depth ${depth}`,
		}));
		component = mount(PlanTree, { target: host, props: withFolds({ nodes, maxDepth: 9, expandBranchesByDefault: true }) });
		flushSync();

		for (const depth of [1, 3, 8]) {
			const row = document.getElementById(`plan-row-depth-${depth}`)!;
			(row.querySelector('.row-body') as HTMLButtonElement).click();
			await settle();
			const details = document.getElementById(`plan-node-details-depth-${depth}`)!;
			expect(details.style.getPropertyValue('--indent')).toBe(row.style.getPropertyValue('--indent'));
		}
	});
});
