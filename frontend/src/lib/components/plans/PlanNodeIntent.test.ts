import { afterEach, expect, it } from 'vitest';
import { flushSync } from 'svelte';
import { createClassComponent } from 'svelte/legacy';
import { operatorNode } from '$lib/__tests__/fixtures/operatorPlan';
import type { OperatorNode } from '$lib/api/plans';
import PlanNodeDetails from './PlanNodeDetails.svelte';

let host: HTMLElement;
let component: { $destroy: () => void };
function render(over: Partial<OperatorNode>) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = createClassComponent({ target: host, component: PlanNodeDetails, props: { node: operatorNode(over), id: 'details' } });
	flushSync();
	return host.textContent ?? '';
}
afterEach(() => { component?.$destroy(); host?.remove(); });

it('shows reading and done test above status', () => {
	const text = render({ intent: 'The goal', acceptance_criteria: 'The result' });
	expect(text).toContain('Reading:');
	expect(text).toContain('The goal');
	expect(text).toContain('Done when: The result');
	expect(text.indexOf('The goal')).toBeLessThan(text.indexOf('Status:'));
	expect(text.indexOf('Done when:')).toBeLessThan(text.indexOf('Status:'));
});
it('names the delegated parent and the owner to tell', () => {
	const text = render({ intent: 'Parent reading', intent_parent_title: 'Parent task', intent_owner_name: 'New Chat' });
	expect(text).toContain('From New Chat · Parent task');
	expect(text).toContain('To change it, tell New Chat.');
});
it('labels legacy person comments without rewriting them', () => {
	const text = render({ intent: 'User document feedback: original', intent_is_person_comment: true });
	expect(text).toContain('Reading (your comment, as given):');
	expect(text).toContain('User document feedback: original');
});
it('heads a worker reading with its provenance in place of the reading label', () => {
	const text = render({ intent: 'User document feedback: exact words', intent_is_person_comment: true, intent_parent_title: 'Feedback', intent_owner_name: 'Plan owner', acceptance_criteria: 'Child done' });
	expect(text).not.toContain('Reading');
	const parts = ['From Plan owner · Feedback', 'User document feedback: exact words', 'To change it, tell Plan owner.', 'Done when: Child done'];
	let previous = -1;
	for (const part of parts) { const index = text.indexOf(part); expect(index).toBeGreaterThan(previous); previous = index; }
});
it('shows no reading block for an open worker task with no reading', () => {
	const text = render({ intent: null, intent_parent_title: 'Parent task', intent_owner_name: 'New Chat', acceptance_criteria: 'Child done' });
	expect(text).not.toContain('No reading recorded.');
	expect(text).not.toContain('From New Chat');
	expect(text).toContain('Done when: Child done');
});
it('shows only the done test for a settled task with no reading', () => {
	const text = render({ state: 'closed', intent: null, acceptance_criteria: 'Finished result' });
	expect(text).not.toContain('Reading:');
	expect(text).not.toContain('No reading recorded.');
	expect(text).toContain('Done when: Finished result');
});
it('shows the empty reading for an open task and no edit caption for a step', () => {
	expect(render({ intent: null })).toContain('No reading recorded.');
	component.$destroy(); host.remove();
	const step = render({ intent: 'Run goal', intent_run_title: 'The run' });
	expect(step).toContain('Run goal');
	expect(step).not.toContain('Changes the reading');
});
