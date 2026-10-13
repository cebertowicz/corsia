import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import type { OperatorNode } from '$lib/api/plans';
import { node } from './fixtures/planTreeNode';
import { withFolds } from './fixtures/planFolds';
import rowFaceSource from '$lib/components/plans/PlanRowFace.svelte?raw';

/**
 * Owner decision, 2026-09-16: a plan row is one line. The icon carries status and the
 * subagent control carries a different owner, so the row names nothing under
 * its title unless there is something specific to say: a wait cause, a retry
 * forced by a failed try, or a request for the user's input. An attempt opened
 * by an edit or any other non-failure release is not friction and names
 * nothing. Settled work stays visible.
 */

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

function render(nodes: OperatorNode[], nextNodeIds: string[] = []): void {
	component = mount(PlanTree, { target: host, props: withFolds({ nodes, nextNodeIds }) });
	flushSync();
}

function row(title: string): Element {
	const found = [...host.querySelectorAll('.tree-row')]
		.find((item) => item.querySelector('.row-title')?.textContent === title);
	if (!found) throw new Error(`no row titled ${title}`);
	return found;
}

function subline(title: string): string | null {
	return row(title).querySelector('.row-subline')?.textContent?.trim() ?? null;
}

describe('PlanRowFace — one line unless something specific needs saying', () => {
	it('names no status or owner under a plain row', () => {
		render([
			node({ id: 'todo', title: 'To do step' }),
			node({ id: 'next', title: 'Next step' }),
			node({ id: 'running', title: 'Running step', state: 'active' }),
			node({ id: 'done', title: 'Done step', state: 'accepted' }),
		], ['next']);

		for (const title of ['To do step', 'Next step', 'Running step']) {
			expect(subline(title)).toBeNull();
			expect(row(title).textContent).not.toContain('Agent');
		}
		expect(subline('Done step')).toBe('Done');
		expect(row('Done step').textContent).not.toContain('Agent');
		// Status stays available to assistive technology without a visible subline.
		expect(row('Running step').querySelector('.row-status')?.textContent).toBe('Working');
		expect(row('Done step').querySelector('.state-icon')?.getAttribute('title')).toBe('Finished');
	});

	it('names a specific wait cause under the title', () => {
		render([node({
			id: 'ship', title: 'Ship the client',
			display: { label_key: 'waiting', tone_key: 'waiting' },
			status_reason: 'After Build the API',
		})]);

		expect(subline('Ship the client')).toBe('After Build the API');
		expect(row('Ship the client').querySelector('.state-icon.waiting')).toBeTruthy();
		expect(row('Ship the client').querySelector('.state-icon.blocked')).toBeNull();
	});

	it('names a retry after a failed try, and never a first try', () => {
		const second = node({ id: 'retry', title: 'Retried step', state: 'active' });
		const first = node({ id: 'once', title: 'First try', state: 'active' });
		render([
			{ ...second, execution: { ...second.execution, attempt_count: 2, friction_count: 1 } },
			{ ...first, execution: { ...first.execution, attempt_count: 1, friction_count: 0 } },
		]);

		expect(subline('Retried step')).toBe('Attempt 2');
		expect(subline('First try')).toBeNull();
	});

	it('names repeated send-backs as rounds, not generic attempts', () => {
		const roundOne = node({ id: 'round-one', title: 'First send-back', state: 'active' });
		const roundTwo = node({ id: 'round-two', title: 'Second send-back', state: 'active' });
		const retried = node({ id: 'retried', title: 'Sent back and retried', state: 'active' });
		// The backend counts each send-back round in friction_count too.
		render([
			{ ...roundOne, execution: { ...roundOne.execution, revision_round: 1, friction_count: 1 } },
			{ ...roundTwo, execution: { ...roundTwo.execution, revision_round: 2, friction_count: 2 } },
			{ ...retried, execution: { ...retried.execution, revision_round: 1, friction_count: 2 } },
		]);

		expect(subline('First send-back')).toBeNull();
		expect(subline('Second send-back')).toBe('Round 2');
		expect(subline('Sent back and retried')).toBe('Attempt 3');
	});

	it('names nothing for a second attempt an edit opened', () => {
		const edited = node({ id: 'edited', title: 'Edited step', state: 'active' });
		render([{ ...edited, execution: { ...edited.execution, attempt_count: 2, friction_count: 0 } }]);

		expect(subline('Edited step')).toBeNull();
	});

	it('numbers the try by failures only, however many other attempts there were', () => {
		const churned = node({ id: 'churned', title: 'Churned step', state: 'active' });
		render([{ ...churned, execution: { ...churned.execution, attempt_count: 7, friction_count: 1 } }]);

		expect(subline('Churned step')).toBe('Attempt 2');
	});

	it('names a request for the user\'s input', () => {
		render([node({
			id: 'decide', title: 'Pick a name', state: 'review_requested',
			requires_user_action: true,
			review: {
				kind: 'human_decision', label: 'Needs your decision',
				actor: { kind: 'user', id: null, label: 'You' },
			},
		})]);

		expect(subline('Pick a name')).toBe('Needs your decision');
	});

	it('joins every specific fact on the one subline', () => {
		const held = node({
			id: 'held', title: 'Held step',
			display: { label_key: 'needs_you', tone_key: 'human-review' },
			status_reason: 'Needs a deploy key',
		});
		render([{ ...held, execution: { ...held.execution, attempt_count: 4, friction_count: 2 } }]);

		expect(subline('Held step')).toBe('Attempt 3 · Needs a deploy key');
	});

	it('leads with the round so a long reason is what a narrow row cuts', () => {
		const parent = node({
			id: 'parent', title: 'Delegated step',
			display: { label_key: 'needs_agent_input', tone_key: 'waiting' },
			status_reason: "The delegated child needs your input: Can't open mockups.md",
		});
		render([{ ...parent, execution: { ...parent.execution, revision_round: 2, friction_count: 2 } }]);

		expect(subline('Delegated step'))
			.toBe("Round 2 · The delegated child needs your input: Can't open mockups.md");
		// The subline is a flex row, so its text needs its own block for the ellipsis to show.
		expect(row('Delegated step').querySelector('.row-subline .subline-text')?.textContent)
			.toBe("Round 2 · The delegated child needs your input: Can't open mockups.md");
	});
});

describe('PlanRowFace — an open row keeps its full title visible', () => {
	it('wraps an open title in its existing title slot while a closed row truncates', () => {
		const title = 'Hand off the migration verification and evidence review to the delegated worker';
		render([node({ id: 'delegated', title })]);
		const titleEl = row(title).querySelector('.row-title') as HTMLElement;
		expect(titleEl.classList.contains('details-open')).toBe(false);
		(row(title).querySelector('.row-body') as HTMLButtonElement).click();
		flushSync();
		expect(titleEl.classList.contains('details-open')).toBe(true);
		expect(rowFaceSource).toMatch(/\.row-title\.details-open[\s\S]*flex:\s*1\s+1\s+0/);
		expect(rowFaceSource).toMatch(/\.row-title\.details-open[\s\S]*white-space:\s*normal/);
		expect(host.querySelector('.node-details')?.textContent).not.toContain(title);
	});
});

describe('PlanTree — settled work stays visible as progress', () => {
	it('renders accepted nodes by default', () => {
		render([
			node({ id: 'a', title: 'Current step', state: 'active' }),
			node({ id: 'b', title: 'Done step', state: 'accepted' }),
		]);

		expect(row('Done step')).toBeTruthy();
		expect(host.textContent).not.toContain('Show 1 completed');
	});
});
