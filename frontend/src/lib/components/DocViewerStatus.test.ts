import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync } from 'svelte';
import { createClassComponent } from 'svelte/legacy';
import DocViewerStatus from './DocViewerStatus.svelte';
import docViewerStatusSource from './DocViewerStatus.svelte?raw';
import type { DocumentFreshness } from '$lib/docFreshness';
import type { CommentFeedback } from '$lib/api/docs';

let host: HTMLElement;
let component: ({ commentCreated: () => void; sync: (id: string, feedback: CommentFeedback) => void } & { $destroy: () => void; $set: (props: Record<string, unknown>) => void }) | null = null;

function value(overrides: Partial<DocumentFreshness> = {}): DocumentFreshness {
	return {
		class: 'reference',
		verified_at: '2026-09-12T12:00:00Z',
		age_days: 18,
		due_at: '2026-10-20T12:00:00Z',
		state: 'current',
		owner_name: 'Structured Plans',
		...overrides,
	};
}

function render(freshness: DocumentFreshness, onMarkVerified = vi.fn().mockResolvedValue(undefined), overrides: Record<string, unknown> = {}) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = createClassComponent({
		target: host,
		component: DocViewerStatus,
		props: {
			title: 'Review document',
			ready: true,
			stale: false,
			readonly: false,
			commentCount: 0,
			orphanedCount: 0,
			artifactId: 'artifact-1',
			targetInstanceId: 'tab-1',
			onOpenComments: vi.fn(),
			freshness,
			onMarkVerified,
			...overrides,
		},
	}) as unknown as typeof component;
	flushSync();
	return onMarkVerified;
}

afterEach(() => {
	component?.$destroy();
	component = null;
	host?.remove();
	vi.useRealTimers();
});

describe('DocViewerStatus freshness metadata', () => {
	it('keeps document identity and zero-comment navigation in its header', () => {
		render(value());
		const header = host.querySelector('header');
		expect(header?.textContent).toContain('Review document');
		expect(header?.textContent).toContain('Owner: Structured Plans');
		expect(header?.querySelector('.comment-status')?.getAttribute('aria-label')).toBe('0 comments');
		const details = header?.querySelector<HTMLButtonElement>('[aria-label="Document details"]');
		expect(details?.getAttribute('aria-expanded')).toBe('false');
		details!.click();
		flushSync();
		expect(details?.getAttribute('aria-expanded')).toBe('true');
	});

	it('closes metadata on scoped Escape, restores its toggle, and preserves it from other header controls', () => {
		render(value({ state: 'overdue' }));
		const toggle = host.querySelector<HTMLButtonElement>('.identity-details')!;
		toggle.click(); flushSync();
		const escape = () => new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
		host.querySelector<HTMLButtonElement>('.comment-status')!.dispatchEvent(escape()); flushSync();
		expect(toggle.getAttribute('aria-expanded')).toBe('true');
		const event = escape();
		host.querySelector<HTMLButtonElement>('.verification-action button')!.dispatchEvent(event); flushSync();
		expect(event.defaultPrevented).toBe(true);
		expect(toggle.getAttribute('aria-expanded')).toBe('false');
		expect(document.activeElement).toBe(toggle);
	});

	it.each([
		[value({ state: 'current', due_at: '2026-10-20T12:00:00Z' }), false],
		[value({ state: 'overdue' }), true],
		[value({ state: 'never_verified', verified_at: null, age_days: null }), true],
	])('shows Mark verified only for eligible freshness states', (freshness, visible) => {
		render(freshness);
		expect(host.textContent?.includes('Mark verified')).toBe(visible);
	});

	it('shows Mark verified inside the seven-day window', () => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date('2026-09-30T12:00:00Z'));
		render(value({ due_at: '2026-10-07T12:00:00Z' }));
		expect(host.textContent).toContain('Mark verified');
	});

	it('announces success, then clears it when the parent switches documents', async () => {
		const mark = render(value({ state: 'overdue' }));
		host.querySelector<HTMLButtonElement>('button.btn.ghost')!.click();
		await vi.waitFor(() => expect(mark).toHaveBeenCalledOnce());
		await vi.waitFor(() => expect(host.querySelector('[role="status"]')?.textContent).toBe('Document marked verified.'));

		component!.$set({ artifactId: 'artifact-2', freshness: value({ age_days: 94 }) });
		flushSync();
		expect(host.textContent).toContain('verified 94 days ago');
		expect(host.querySelector('[role="status"]')?.textContent).toBe('');
	});

	it('announces a refused placement reload in the existing polite status region and re-enables it', () => {
		const reload = vi.fn(() => true);
		const live = { artifactId: 'artifact', subscribe: () => () => {} } as any;
		render(value(), undefined, { liveSession: live, orphanedCount: 1, onReloadPlacements: reload, placementReloading: false });
		const control = host.querySelector<HTMLButtonElement>('[aria-label="Reload comment placements"]')!;
		control.click(); expect(reload).toHaveBeenCalledOnce();
		component!.$set({ placementReloading: true }); flushSync();
		component!.$set({ placementReloading: false, placementReloadError: true }); flushSync();
		expect(host.querySelector('[role="status"]')?.textContent).toBe("Couldn't reload comment placements");
		expect(host.querySelector('[role="status"]')?.classList.contains('sr-only')).toBe(false);
		expect(host.querySelector<HTMLButtonElement>('[aria-label="Reload comment placements"]')?.disabled).toBe(false);
	});

	it('removes Mark verified when the parent replaces freshness after success', async () => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date('2026-09-30T12:00:00Z'));
		const mark = render(value({ due_at: '2026-10-03T12:00:00Z' }));
		host.querySelector<HTMLButtonElement>('button.btn.ghost')!.click();
		await vi.waitFor(() => expect(mark).toHaveBeenCalledOnce());

		component!.$set({ freshness: value({ age_days: 0, due_at: null, state: 'current' }) });
		flushSync();
		expect(host.textContent).not.toContain('Mark verified');
	});

	it('preserves the owner-label space and aligns the ghost action with its text', () => {
		render(value({ state: 'overdue' }));
		expect(host.querySelector('.metadata-owner')?.textContent).toBe('Owner: Structured Plans');
		expect(host.querySelector('.verification-action button')?.textContent?.trim()).toBe('Mark verified');
		expect(docViewerStatusSource).toContain('.verification-action { margin-left: -10px; }');
	});

	it('puts the absolute date in screen-reader time text', () => {
		render(value({ age_days: 94 }));
		expect(host.querySelector('time .sr-only')?.textContent).toContain('Sep 12');
	});

	it('renders the server refusal inline', async () => {
		render(value({ state: 'overdue' }), vi.fn().mockRejectedValue(new Error('Only Structured Plans may verify this document.')));
		host.querySelector<HTMLButtonElement>('button.btn.ghost')!.click();
		await vi.waitFor(() => expect(host.textContent).toContain('Only Structured Plans may verify this document.'));
	});
});

describe('DocViewerStatus toolbar', () => {
	it('announces the stale state through the status region once it starts, and clears it on reload', () => {
		render(value(), undefined, { stale: false });
		const status = () => host.querySelector('[role="status"]')?.textContent ?? '';
		expect(status()).not.toContain('File changed');

		component!.$set({ stale: true });
		flushSync();
		expect(status()).toContain('File changed since you opened it');
		expect(host.querySelector('.stale-control')?.textContent).toContain('File changed · Reload');

		component!.$set({ stale: false });
		flushSync();
		expect(status()).toBe('');
	});

	it('does not announce stale before the document is ready', () => {
		render(value(), undefined, { stale: true, ready: false });
		expect(host.querySelector('[role="status"]')?.textContent).toBe('');
	});

	it('keeps the stale announcement screen-reader only, since the visible control already shows it', () => {
		render(value(), undefined, { stale: true });
		const status = host.querySelector('[role="status"]')!;
		expect(status.textContent).toContain('File changed since you opened it');
		expect(status.classList.contains('sr-only')).toBe(true);
	});

	it('anchors the unplaced dot to the comment icon, not the control box', () => {
		render(value(), undefined, { commentCount: 2, orphanedCount: 1 });
		const button = host.querySelector('.comment-status')!;
		const dot = button.querySelector('.unplaced-dot')!;
		expect(dot.parentElement?.classList.contains('icon-anchor')).toBe(true);
		expect(dot.parentElement?.querySelector('svg.tool-icon')).not.toBeNull();
		expect(button.getAttribute('aria-describedby')).toBe(button.querySelector('.sr-only')?.id);
		expect(button.querySelector('.sr-only')?.id).toMatch(/^unplaced-/);
	});
});
