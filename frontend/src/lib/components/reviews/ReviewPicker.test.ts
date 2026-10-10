import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync } from 'svelte';
import { createClassComponent } from 'svelte/legacy';
import ReviewPicker from './ReviewPicker.svelte';
import { createDocumentReviewState } from '$lib/docReviewState.svelte';

vi.mock('$lib/api/docReviews', () => ({
	fetchDocumentReviews: vi.fn(async () => ({ reviews: [], reviewers: [] })),
	requestDocumentReview: vi.fn(async () => undefined),
	cancelDocumentReview: vi.fn(),
	reviewFailure: (_error: unknown, fallback: string) => fallback,
}));

let host: HTMLElement;
let component: { $destroy(): void };
const reviewer = (id: string, extras = {}) => ({ id, name: `Agent ${id}`, responsibility: `Owns the ${id} lane and a long responsibility sentence`, requested: false, unavailable_reason: null, unavailable_text: null, ...extras });
function render() {
	const model = createDocumentReviewState();
	model.setTarget('doc', 'thread', 'key');
	// The state loads through the mocked API; tests call model.load() to fill the roster.
	host = document.createElement('div'); document.body.appendChild(host);
	component = createClassComponent({ target: host, component: ReviewPicker, props: { model, id: 'rp', onSent: vi.fn() } });
	flushSync();
	return { model };
}
afterEach(() => { component?.$destroy(); host?.remove(); });

describe('review picker rows', () => {
	it('keeps the name as the primary line with the responsibility as one clipped secondary line, in UI type', async () => {
		const api = await import('$lib/api/docReviews');
		vi.mocked(api.fetchDocumentReviews).mockResolvedValueOnce({ reviews: [], reviewers: [reviewer('quality'), reviewer('product')] } as never);
		const { model } = render();
		await model.load(); flushSync();
		host.querySelector<HTMLInputElement>('[role="combobox"]')!.focus(); flushSync();
		const options = [...host.querySelectorAll<HTMLElement>('[role="option"]')];
		expect(options).toHaveLength(2);
		for (const option of options) {
			const text = option.querySelector<HTMLElement>('.option-text');
			expect([...text!.children].map((child) => child.classList[0])).toEqual(['option-name', 'option-detail']);
			expect(text!.querySelector<HTMLElement>('.option-detail')!.title).toContain('responsibility sentence');
		}
	});
	it('puts Send outside the suggestion list', () => {
		render();
		const footer = host.querySelector('footer')!;
		expect(footer.closest('[role="listbox"]')).toBeNull();
		expect(host.querySelector('form')!.contains(footer)).toBe(true);
		expect(footer.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(true);
	});
	it('enables Send once a reviewer is chosen, and names the reason while it is disabled', async () => {
		const api = await import('$lib/api/docReviews');
		vi.mocked(api.fetchDocumentReviews).mockResolvedValueOnce({ reviews: [], reviewers: [reviewer('quality')] } as never);
		const { model } = render();
		await model.load(); flushSync();
		const send = host.querySelector<HTMLButtonElement>('footer button[type="submit"]')!;
		expect(send.disabled).toBe(true);
		expect(host.querySelector(`#${send.getAttribute('aria-describedby')}`)?.textContent).toContain('Choose a reviewer');
		model.select(model.choices()[0]); flushSync();
		expect(send.disabled).toBe(false);
		expect(send.hasAttribute('aria-describedby')).toBe(false);
	});
	it('says so when a search matches no agent', async () => {
		const api = await import('$lib/api/docReviews');
		vi.mocked(api.fetchDocumentReviews).mockResolvedValueOnce({ reviews: [], reviewers: [reviewer('quality')] } as never);
		const { model } = render();
		await model.load(); flushSync();
		const field = host.querySelector<HTMLInputElement>('[role="combobox"]')!;
		field.value = 'zzz'; field.dispatchEvent(new Event('input', { bubbles: true })); flushSync();
		expect(host.querySelectorAll('[role="option"]')).toHaveLength(0);
		expect(host.textContent).toContain('No matching agents.');
	});
});
