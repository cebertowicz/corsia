import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { UpdateCard } from '$lib/api/threadUpdates';
import PersonStepRow from './PersonStepRow.svelte';

const nav = vi.hoisted(() => ({ doStep: vi.fn(), doFirstOf: vi.fn(), plan: { nodes: [] as Array<{ id: string; state: string }> } }));
vi.mock('$lib/stores/planQuery.svelte', () => ({
	planQuery: () => ({ get payload() { return { plans: [{ nodes: nav.plan.nodes }] }; } }),
}));
vi.mock('$lib/personStepChip', async (original) => ({
	...(await original<typeof import('$lib/personStepChip')>()),
	doStep: nav.doStep,
	doFirstOf: nav.doFirstOf,
}));

const card = (id: number, over: Partial<UpdateCard> = {}): UpdateCard => ({
	id, kind: 'person_step', thread_id: 't1', node_id: `n${id}`, node_exists: true, asks_user: true,
	blocked_asks_user: false, after_seq: 1, title: `Step ${id}`, created_at: '2026-10-26T10:00:00.000000', step_state: 'open', ...over,
});

vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });

let component: Record<string, unknown> | undefined;
let host: HTMLDivElement;
function render(props: { card: UpdateCard; group?: UpdateCard[] }) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(PersonStepRow, { target: host, props });
	flushSync();
}
afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	host?.remove();
	nav.doStep.mockClear();
	nav.doFirstOf.mockClear();
	nav.plan.nodes = [];
});

const chip = () => host.querySelector<HTMLButtonElement>('[data-step-chip]');

describe('PersonStepRow', () => {
	it('one open step: a chip with its title, the amber state and Do →, which opens that step', () => {
		render({ card: card(1) });
		expect(chip()!.textContent).toContain('Step 1');
		expect(chip()!.textContent).toContain('Ready for you');
		expect(chip()!.textContent).toContain('Do →');
		expect(chip()!.getAttribute('aria-label')).toBe('Do: Step 1, Ready for you');
		chip()!.click();
		expect(nav.doStep).toHaveBeenCalledWith(expect.objectContaining({ id: 1 }));
		expect(host.querySelector('[data-person-step]')!.getAttribute('data-step-state')).toBe('open');
	});

	it('does not let the press reach the chat column, which would put the chat back over the opened document', () => {
		render({ card: card(1) });
		// Svelte delegates `onclick` at the mount target, so the chat column's handler is reached by its walk or by bubbling past it.
		const above = vi.fn();
		document.body.addEventListener('click', above);
		chip()!.click();
		document.body.removeEventListener('click', above);
		expect(above).not.toHaveBeenCalled();
	});

	it('the plan settles it: the chip becomes the receipt and the hidden face is hidden from assistive tech', () => {
		nav.plan.nodes = [{ id: 'n1', state: 'accepted' }];
		render({ card: card(1) });
		const row = host.querySelector('[data-person-step]')!;
		expect(row.getAttribute('data-step-state')).toBe('done');
		const [chipLayer, receiptLayer] = row.querySelectorAll('.slot-layer');
		// `inert` is a property jsdom does not reflect; the render pass proves it in a real browser.
		expect(chipLayer.getAttribute('aria-hidden')).toBe('true');
		expect(receiptLayer.getAttribute('aria-hidden')).toBe('false');
		expect(receiptLayer.textContent).toContain('Done');
		expect(receiptLayer.textContent).toContain('Step 1');
	});

	it('Won’t do reads as its own receipt', () => {
		nav.plan.nodes = [{ id: 'n1', state: 'closed' }];
		render({ card: card(1) });
		expect(host.querySelector('[data-person-step]')!.getAttribute('data-step-state')).toBe('closed');
		expect(host.querySelectorAll('.slot-layer')[1].textContent).toContain('Won’t do');
	});

	it('several ready steps: one line with the count; Do → opens the plan at the first', () => {
		const cards = [card(1), card(2), card(3)];
		render({ card: cards[2], group: cards });
		expect(host.querySelectorAll('[data-step-chip]')).toHaveLength(1);
		expect(chip()!.textContent).toContain('3 steps');
		chip()!.click();
		expect(nav.doFirstOf).toHaveBeenCalledWith(cards);
		expect(nav.doStep).not.toHaveBeenCalled();
	});

	it('as steps settle, the count falls and each leaves its own receipt; the last one reads by title', () => {
		const cards = [card(1), card(2), card(3)];
		nav.plan.nodes = [{ id: 'n1', state: 'accepted' }, { id: 'n3', state: 'closed' }];
		render({ card: cards[2], group: cards });
		expect(chip()!.textContent).toContain('Step 2');
		expect(host.querySelectorAll('.ev-chip')).toHaveLength(2);
		chip()!.click();
		expect(nav.doStep).toHaveBeenCalledWith(expect.objectContaining({ id: 2 }));
	});

	it('every step settled: only receipts remain', () => {
		const cards = [card(1), card(2)];
		nav.plan.nodes = [{ id: 'n1', state: 'accepted' }, { id: 'n2', state: 'accepted' }];
		render({ card: cards[1], group: cards });
		expect(chip()).toBeNull();
		expect(host.querySelectorAll('.ev-chip')).toHaveLength(2);
	});
});
