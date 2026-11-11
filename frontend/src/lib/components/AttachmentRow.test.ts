import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import AttachmentRow from './AttachmentRow.svelte';
import attachmentRowSource from './AttachmentRow.svelte?raw';
import type { MessageCard } from '$lib/api/messageCards';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

const LONG_TITLE = '3 new replies · launch-plan-for-the-Q4-onboarding-and-support-readiness-review.md';

function card(title: string): MessageCard {
	return { kind: 'artifact', artifact_id: 'artifact-1', title, type: 'document', icon: 'file-text', name: title, path: '/tmp/launch-plan.md', available: true, opens: true };
}

function render(props: { variant?: 'row' | 'chip'; chipWidth?: 'default' | 'content'; title?: string; middleEllipsis?: boolean; chipAside?: string }) {
	host = document.createElement('div');
	document.body.append(host);
	component = mount(AttachmentRow, { target: host, props: { card: card(props.title ?? LONG_TITLE), threadId: 'thread-1', variant: props.variant, chipWidth: props.chipWidth, middleEllipsis: props.middleEllipsis, chipAside: props.chipAside } });
	flushSync();
	return host.querySelector<HTMLElement>('.tile')!;
}

// The rule a selector sets, read from the component's own style block (jsdom does not apply Svelte's scoped CSS).
function rule(selector: string): string {
	const style = attachmentRowSource.slice(attachmentRowSource.indexOf('<style>'));
	const start = style.indexOf(`${selector} {`);
	expect(start, `${selector} rule exists`).toBeGreaterThan(-1);
	return style.slice(start, style.indexOf('}', start));
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
});

describe('AttachmentRow chip width', () => {
	it('keeps the 220px cap for every chip that does not opt in', () => {
		const chip = render({ variant: 'chip' });
		expect(chip.classList.contains('chip')).toBe(true);
		expect(chip.classList.contains('chip-content-width')).toBe(false);
		expect(rule('.tile.chip')).toContain('max-width: 220px');
	});

	it('lets only an opted-in chip grow to its container', () => {
		const chip = render({ variant: 'chip', chipWidth: 'content' });
		expect(chip.classList.contains('chip-content-width')).toBe(true);
		expect(rule('.tile.chip.chip-content-width')).toContain('max-width: 100%');
		// The opt-in does not change the default rule, so an ordinary chip still stops at 220px.
		expect(rule('.tile.chip')).toContain('max-width: 220px');
	});

	it('ignores the opt-in on a row, which has no cap to lift', () => {
		expect(render({ variant: 'row', chipWidth: 'content' }).classList.contains('chip-content-width')).toBe(false);
	});

	it('carries the full title in the tooltip and the accessible name even where the visible text truncates', () => {
		const chip = render({ variant: 'chip', chipWidth: 'content' });
		expect(chip.getAttribute('title')).toBe(LONG_TITLE);
		expect(chip.getAttribute('aria-label')).toBe(`${LONG_TITLE}, document`);
	});
});

describe('AttachmentRow chip aside', () => {
	it('keeps a file size inside the chip, after the name, and in its accessible name', () => {
		const chip = render({ variant: 'chip', title: 'volunteer-waiver.pdf', middleEllipsis: true, chipAside: '1.8 MB' });
		const aside = chip.querySelector('.chip-aside');
		expect(aside?.textContent).toBe('1.8 MB');
		expect(chip.querySelector('.text')?.nextElementSibling).toBe(aside);
		expect(chip.getAttribute('aria-label')).toBe('volunteer-waiver.pdf, document, 1.8 MB');
		expect(rule('.chip-aside')).toContain('white-space: nowrap');
	});

	it('adds nothing to a chip without one', () => {
		const chip = render({ variant: 'chip', title: 'volunteer-waiver.pdf' });
		expect(chip.querySelector('.chip-aside')).toBeNull();
		expect(chip.getAttribute('aria-label')).toBe('volunteer-waiver.pdf, document');
	});
});

describe('AttachmentRow middle ellipsis', () => {
	const NAME = 'north-trailhead-lot-and-church-lot-parking-comparison-with-shuttle-route-and-county-van-limits-final-v7.pdf';

	it('sets a chip name as a start that gives way and an end that stays, with the full name in title and aria-label', () => {
		const chip = render({ variant: 'chip', title: NAME, middleEllipsis: true });
		expect(chip.querySelector('.name-end')?.textContent).toBe('final-v7.pdf');
		expect(`${chip.querySelector('.name-start')?.textContent}${chip.querySelector('.name-end')?.textContent}`).toBe(NAME);
		expect(chip.getAttribute('title')).toBe(NAME);
		expect(chip.getAttribute('aria-label')).toBe(`${NAME}, document`);
		// The start is the only part that shrinks and the only one that draws an ellipsis; the end never shrinks.
		expect(rule('.name-start')).toContain('flex: 0 1 auto');
		expect(rule('.name-start')).toContain('text-overflow: ellipsis');
		expect(rule('.name-end')).toContain('flex: 0 0 auto');
		expect(rule('.name-end')).not.toContain('ellipsis');
	});

	it('leaves every chip that does not opt in as one plain title', () => {
		const chip = render({ variant: 'chip', title: NAME });
		expect(chip.querySelector('.name-start')).toBeNull();
		expect(chip.querySelector('.title')?.textContent).toBe(NAME);
	});
});
