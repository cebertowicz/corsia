import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/planNodeFocus', () => ({ focusPlanNode: vi.fn(async () => {}) }));

import MessageArtifactCards from './MessageArtifactCards.svelte';
import type { MessageCard } from '$lib/api/messageCards';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { token } from '$lib/stores/auth';
import { paneOpen } from '$lib/stores/paneOpen';
import UpdateCard from './UpdateCard.svelte';
import type { UpdateCard as Card } from '$lib/api/threadUpdates';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

const ask = (over: Partial<MessageCard> = {}): MessageCard => ({
	kind: 'ask', artifact_id: 'a1', title: 'Launch plan', type: 'document', icon: 'file-text', name: 'launch-plan.md',
	path: 'launch-plan.md', available: true, opens: true, hold_id: 'h1', state: 'needs_review', ...over,
});

function render(props: Record<string, unknown>): void {
	component = mount(MessageArtifactCards, { target: host, props: { threadId: 't1', cards: [], ...props } });
	flushSync();
}

const tiles = () => [...host.querySelectorAll<HTMLElement>('.tile')];

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	paneOpen.set(false);
	threadWorkspaces.set({});
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

describe('MessageArtifactCards', () => {
	it('names each tile by title, type and round state, and opens the artifact on click', () => {
		render({ cards: [ask()] });

		const [tile] = tiles();
		expect(tile.tagName).toBe('BUTTON');
		expect(tile.getAttribute('aria-label')).toBe('Launch plan, document, Needs your review');
		expect(tile.querySelector('.title')?.textContent).toBe('Launch plan');
		expect(tile.querySelector('.state')?.textContent).toBe('Needs your review');
		expect(host.querySelector('button.tile')?.closest('ul')?.getAttribute('aria-label')).toBe('Artifacts');
		tile.click();
		const opened = get(threadWorkspaces).t1.tabs.map((tab) => tab.artifactId);
		expect(opened).toContain('a1');
	});

	it('omits the file-name line when it equals the title and keeps it when they differ', () => {
		render({ cards: [
			ask({ title: 'launch-plan.md', name: 'launch-plan.md' }),
			ask({ artifact_id: 'a2', hold_id: 'h2', title: 'Launch plan', name: 'launch-plan.md' }),
			ask({ artifact_id: 'a3', hold_id: 'h3', title: 'launch-plan.md', name: 'Launch-plan.md', state: 'answered' }),
		] });

		const [same, differs, caseOnly] = tiles();
		expect(same.querySelector('.name')).toBeNull();
		expect(same.querySelector('.state')?.textContent).toBe('Needs your review');
		expect(differs.querySelector('.name')?.textContent).toBe('launch-plan.md');
		expect(caseOnly.querySelector('.name')?.textContent).toBe('Launch-plan.md');
	});

	it('shows the clearing round as text beside the title, with the feedback counts as a second line', () => {
		render({ cards: [ask({ state: 'changes_requested', feedback: { comments: 2, suggestions: 1 } })] });

		expect(tiles()[0].querySelector('.state')?.textContent).toBe('Changes requested');
		expect(tiles()[0].querySelector('.name')?.textContent).toBe('2 comments, 1 suggestion');
	});

	it('keeps a replaced round clickable', () => {
		render({ cards: [ask({ state: 'replaced' })] });

		expect(tiles()[0].tagName).toBe('BUTTON');
		expect(tiles()[0].querySelector('.state')?.textContent).toBe('Replaced');
	});

	it('draws a retired artifact as a dashed tile that is not clickable', () => {
		render({ cards: [ask({ state: 'unavailable', available: false, opens: false })] });

		const [tile] = tiles();
		expect(tile.tagName).toBe('DIV');
		expect(tile.classList.contains('gone')).toBe(true);
		expect(tile.querySelector('.title')?.textContent).toBe('Launch plan');
		expect(tile.querySelector('.state')?.textContent).toBe('No longer available');
		tile.click();
		expect(get(threadWorkspaces).t1?.tabs ?? []).toEqual([]);
	});

	it('draws a file the pane cannot open as a plain tile', () => {
		render({ cards: [{ ...ask(), kind: 'artifact', state: undefined, hold_id: undefined, opens: false }] });

		expect(tiles()[0].tagName).toBe('DIV');
		expect(tiles()[0].classList.contains('gone')).toBe(false);
		expect(tiles()[0].querySelector('.state')).toBeNull();
	});

	it('shows an image artifact as a lazy thumbnail from the raw file route, not the glyph', () => {
		token.set('test-session');
		render({ cards: [ask({ type: 'image', icon: 'image', name: 'shot.png', title: 'Shot', path: '/ws/shot.png' })] });

		const img = tiles()[0].querySelector<HTMLImageElement>('img.thumb')!;
		expect(img.getAttribute('loading')).toBe('lazy');
		const url = new URL(img.getAttribute('src')!, 'http://localhost');
		expect(url.pathname).toBe('/api/files/content');
		expect(url.searchParams.get('path')).toBe('/ws/shot.png');
		expect(url.searchParams.get('raw')).toBe('true');
		expect(tiles()[0].querySelector('.ic')).toBeNull();
	});

	it('decides image-ness from the artifact type, never from the file name', () => {
		render({ cards: [ask({ type: 'document', name: 'diagram.png', path: '/ws/diagram.png' })] });

		expect(tiles()[0].querySelector('img')).toBeNull();
		expect(tiles()[0].querySelector('.ic')).not.toBeNull();
	});

	it('falls back to the glyph when the thumbnail cannot load, and for a retired image', () => {
		token.set('test-session');
		render({ cards: [
			ask({ type: 'image', icon: 'image', path: '/ws/a.png' }),
			ask({ artifact_id: 'a2', hold_id: 'h2', type: 'image', icon: 'image', path: '/ws/b.png', available: false, opens: false }),
		] });

		expect(tiles()[1].querySelector('img')).toBeNull();
		tiles()[0].querySelector('img')!.dispatchEvent(new Event('error'));
		flushSync();
		expect(tiles()[0].querySelector('img')).toBeNull();
		expect(tiles()[0].querySelector('.ic')).not.toBeNull();
	});

	it('hides an ask whose hold is homed on a blocked update row', () => {
		render({ cards: [ask(), ask({ artifact_id: 'a2', title: 'Other', hold_id: 'h2' })], hiddenHolds: new Set(['h1']) });

		expect(tiles().map((tile) => tile.querySelector('.title')?.textContent)).toEqual(['Other']);
	});

	it('renders nothing when every card is homed elsewhere', () => {
		render({ cards: [ask()], hiddenHolds: new Set(['h1']) });

		expect(host.querySelector('ul')).toBeNull();
	});

	it('ends the row with a "+N more" tile that opens the artifact pane', () => {
		render({ cards: [ask({ state: undefined, kind: 'artifact', hold_id: undefined })], more: 3 });

		const last = tiles().at(-1)!;
		expect(last.textContent).toBe('+3 more');
		last.click();
		expect(get(paneOpen)).toBe(true);
	});

	it('uses no selected style on a tile', () => {
		render({ cards: [ask()] });

		expect(tiles()[0].getAttribute('aria-pressed')).toBeNull();
		expect(tiles()[0].getAttribute('aria-selected')).toBeNull();
		expect(tiles()[0].getAttribute('aria-current')).toBeNull();
	});
});

describe('UpdateCard ask row', () => {
	const blocked: Card = {
		id: 7, thread_id: 't1', kind: 'blocked', node_id: 'n1', title: 'Ship the export', after_seq: 3,
		created_at: '2026-09-29T10:00:00.000000', node_exists: true, asks_user: true, blocked_asks_user: true, ask: ask(),
	};
	const mountCard = (card: Card): void => {
		component = mount(UpdateCard, { target: host, props: { card } });
		flushSync();
	};

	it('puts the ask directly under its blocked row while the block asks for the user', () => {
		mountCard(blocked);

		const row = host.querySelector('.update-card')!;
		expect(row.nextElementSibling?.classList.contains('update-ask')).toBe(true);
		expect(host.querySelector('.update-ask .tile')?.getAttribute('aria-label')).toContain('Needs your review');
	});

	it('shows no ask once the block no longer asks for the user', () => {
		mountCard({ ...blocked, blocked_asks_user: false });

		expect(host.querySelector('.update-ask')).toBeNull();
	});

	it('shows no ask on a row that recorded none', () => {
		mountCard({ ...blocked, ask: null });

		expect(host.querySelector('.update-ask')).toBeNull();
	});
});
