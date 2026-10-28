import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { UpdateCard } from '$lib/api/threadUpdates';
import PersonStepRow from './PersonStepRow.svelte';

const nav = vi.hoisted(() => ({ doStep: vi.fn(), plan: { nodes: [] as Array<{ id: string; state: string }> } }));
vi.mock('$lib/stores/planQuery.svelte', () => ({
	planQuery: () => ({ get payload() { return { plans: [{ nodes: nav.plan.nodes }] }; } }),
}));
vi.mock('$lib/personStepChip', async (original) => ({
	...(await original<typeof import('$lib/personStepChip')>()),
	doStep: nav.doStep,
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
	nav.plan.nodes = [];
});

const chip = () => host.querySelector<HTMLButtonElement>('[data-step-chip]');

describe('PersonStepRow', () => {
	it('one open task: a chip with its title, the amber "Needs you" and Do →, which opens that task', () => {
		render({ card: card(1) });
		expect(chip()!.textContent).toContain('Step 1');
		expect(chip()!.textContent).toContain('Needs you');
		expect(chip()!.textContent).toContain('·');
		// The lead is the plan row's needs-you mark, not a drawing of its own.
		expect(chip()!.querySelector('.state-icon.needs-you')?.textContent).toBe('!');
		expect(chip()!.textContent).toContain('Do →');
		expect(chip()!.getAttribute('aria-label')).toBe('Do: Step 1, Needs you');
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

	it('several waiting tasks: one line, the first task and "+N more"; Do → runs that first task, as a single chip does', () => {
		const cards = [card(1), card(2), card(3)];
		render({ card: cards[2], group: cards });
		expect(host.querySelectorAll('[data-step-chip]')).toHaveLength(1);
		expect(chip()!.querySelector('.title')!.textContent).toBe('Step 1');
		// "+N more" ends the state line, so the title's clamp can never cut it; the head line holds the title alone.
		expect(chip()!.querySelector('.when')).toBeNull();
		const ctx = chip()!.querySelector('.ctx')!;
		expect(ctx.lastElementChild!.textContent).toBe('+2 more');
		expect(ctx.textContent).toMatch(/Needs you\s*·.*·\s*\+2 more$/);
		expect(chip()!.getAttribute('aria-label')).toBe('Do: Step 1, 2 more, Needs you');
		chip()!.click();
		expect(nav.doStep).toHaveBeenCalledTimes(1);
		expect(nav.doStep).toHaveBeenCalledWith(expect.objectContaining({ id: 1 }));
	});

	it('the title clamps at two lines on the chip; the review tile keeps one', () => {
		render({ card: card(1) });
		expect(chip()!.querySelector('.title')!.classList.contains('two-line')).toBe(true);
	});

	it('as tasks settle, the count falls and each leaves its own receipt; the last one reads as a plain chip', () => {
		const cards = [card(1), card(2), card(3)];
		nav.plan.nodes = [{ id: 'n1', state: 'accepted' }, { id: 'n3', state: 'closed' }];
		render({ card: cards[2], group: cards });
		expect(chip()!.textContent).toContain('Step 2');
		expect(chip()!.querySelector('.when')).toBeNull();
		expect(host.querySelectorAll('.ev-chip')).toHaveLength(2);
		chip()!.click();
		expect(nav.doStep).toHaveBeenCalledWith(expect.objectContaining({ id: 2 }));
	});

	it('every task settled: only receipts remain', () => {
		const cards = [card(1), card(2)];
		nav.plan.nodes = [{ id: 'n1', state: 'accepted' }, { id: 'n2', state: 'accepted' }];
		render({ card: cards[1], group: cards });
		expect(chip()).toBeNull();
		expect(host.querySelectorAll('.ev-chip')).toHaveLength(2);
	});
});
