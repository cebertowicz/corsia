import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/planNodeFocus', () => ({ focusPlanNode: vi.fn(async () => {}) }));

import MessageArtifactCards from './MessageArtifactCards.svelte';
import type { MessageCard } from '$lib/api/messageCards';
import { threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { token } from '$lib/stores/auth';

import { answerSubmissionFailed, answerSubmitted, submittedAnswers } from '$lib/stores/cardAnswers';
import { focusPlanNode } from '$lib/planNodeFocus';
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

	threadWorkspaces.set({});
	submittedAnswers.set({});
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
});

describe('MessageArtifactCards', () => {
	it('renders distinct unregistered files and opens the selected path', () => {
		const cards = Array.from({ length: 11 }, (_, i) => ask({
			kind: 'artifact', artifact_id: null, hold_id: undefined, state: undefined,
			title: `file${i}.md`, name: `file${i}.md`, path: `/workspace/file${i}.md`,
		}));
		render({ cards });
		expect(tiles()).toHaveLength(11);
		tiles()[10].click();
		const tab = get(threadWorkspaces).t1.tabs[0];
		expect(tab.path).toBe('/workspace/file10.md');
		expect(tab.artifactId).toBeNull();
	});

	it('names each tile by title, type and round state, and opens the artifact on click', () => {
		render({ cards: [ask()] });

		const [tile] = tiles();
		expect(tile.tagName).toBe('BUTTON');
		expect(tile.classList.contains('press-tint')).toBe(true);
		expect(tile.getAttribute('aria-label')).toBe('Launch plan, document, Needs your review');
		expect(tile.querySelector('.title')?.textContent).toBe('Launch plan');
		expect(tile.querySelector('.state')?.textContent).toBe('Needs your review');
		expect(host.querySelector('button.tile')?.closest('ul')?.getAttribute('aria-label')).toBe('Files to review');
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

	it.each([
		['closed', 'Task closed'],
		['replaced', 'Replaced'],
		['withdrawn', 'Withdrawn'],
	] as const)('keeps a %s round clickable and muted', (state, label) => {
		render({ cards: [ask({ state })] });

		expect(tiles()[0].tagName).toBe('BUTTON');
		expect(tiles()[0].querySelector('.state')?.textContent).toBe(label);
		expect(tiles()[0].querySelector('.state')?.classList.contains('muted')).toBe(true);
	});

	it.each([
		['rejected', 'Rejected'],
		['revision_requested', 'Revision requested'],
		['answered_internally', 'Answered'],
	] as const)('keeps the %s verdict visible rather than muted', (state, label) => {
		render({ cards: [ask({ state })] });

		expect(tiles()[0].querySelector('.state')?.textContent).toBe(label);
		expect(tiles()[0].querySelector('.state')?.classList.contains('muted')).toBe(false);
	});

	it('draws a retired artifact as a dashed tile that is not clickable', () => {
		render({ cards: [ask({ state: 'unavailable', available: false, opens: false })] });

		const [tile] = tiles();
		expect(tile.tagName).toBe('DIV');
		expect(tile.classList.contains('gone')).toBe(true);
		expect(tile.querySelector('.title')?.textContent).toBe('Launch plan');
		expect(tile.querySelector('.ctx')?.textContent).toContain('Unavailable');
		expect(tile.querySelector('.state')?.textContent).toBe('Unavailable');
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


	it('renders every supplied file card in the strip', () => {
		const cards = Array.from({ length: 9 }, (_, i) => ask({ state: undefined, kind: 'artifact', hold_id: undefined, artifact_id: `a${i}`, title: `file${i}.md` }));
		render({ cards });
		expect(tiles().map((tile) => tile.querySelector('.title')?.textContent)).toEqual(cards.map((card) => card.title));
	});

	it('uses no selected style on a tile', () => {
		render({ cards: [ask()] });

		expect(tiles()[0].getAttribute('aria-pressed')).toBeNull();
		expect(tiles()[0].getAttribute('aria-selected')).toBeNull();
		expect(tiles()[0].getAttribute('aria-current')).toBeNull();
	});
	it('flips an open ask to the verdict when the answer is sent, and back when the send fails', () => {
		render({ cards: [ask()] });
		const state = () => host.querySelector('.state')?.textContent;
		expect(state()).toBe('Needs your review');

		answerSubmitted(['h1'], { state: 'approved_with_changes', feedback: { comments: 2 } });
		flushSync();
		expect(state()).toBe('Approved with changes');
		expect(host.querySelector('.name')?.textContent).toBe('2 comments');
		expect(tiles()[0].getAttribute('aria-label')).toBe('Launch plan, document, Approved with changes, 2 comments');

		answerSubmissionFailed(['h1']);
		flushSync();
		expect(state()).toBe('Needs your review');
	});

	it('ignores a sent answer once the server state has moved on', () => {
		render({ cards: [ask({ state: 'changes_requested', feedback: { comments: 1 } })] });

		answerSubmitted(['h1'], { state: 'approved' });
		flushSync();
		expect(host.querySelector('.state')?.textContent).toBe('Changes requested');
	});

	it('does not apply an answered round to a later open ask for the same artifact', () => {
		render({ cards: [ask({ state: 'approved', hold_id: 'h1' }), ask({ hold_id: 'h2' })] });

		answerSubmitted(['h1'], { state: 'approved' });
		flushSync();
		expect(tiles()[1].querySelector('.state')?.textContent).toBe('Needs your review');
	});

	it('marks the chip strip as scrolled once it leaves its start, so the leading edge fades too', () => {
		render({ cards: [1, 2, 3].map((n) => ask({ artifact_id: `f${n}`, hold_id: undefined, kind: 'artifact', state: undefined, title: `File ${n}` })) });
		const strip = host.querySelector<HTMLElement>('ul.chips')!;
		expect(strip.hasAttribute('data-before')).toBe(false);

		strip.scrollLeft = 40;
		strip.dispatchEvent(new Event('scroll'));
		expect(strip.hasAttribute('data-before')).toBe(true);

		strip.scrollLeft = 0;
		strip.dispatchEvent(new Event('scroll'));
		expect(strip.hasAttribute('data-before')).toBe(false);
	});
});

describe('UpdateCard ask row', () => {
	const blocked: Card = {
		id: 7, thread_id: 't1', kind: 'blocked', node_id: 'n1', title: 'Ship the export', after_seq: 3,
		created_at: '2026-09-29T10:00:00.000000', node_exists: true, asks_user: true, blocked_asks_user: true, ask: ask(),
	};
	const mountCard = (card: Card, earlier = 0): void => {
		component = mount(UpdateCard, { target: host, props: { card, earlier } });
		flushSync();
	};

	it('merges the ask into its blocked row: one row, named by the artifact, with the task in its context line', () => {
		mountCard(blocked);

		const row = host.querySelector('.update-card')!;
		expect(row.classList.contains('merged')).toBe(true);
		expect(row.nextElementSibling).toBeNull();
		expect(host.querySelectorAll('.tile')).toHaveLength(1);
		expect(row.querySelector('.tile')?.getAttribute('aria-label')).toContain('Needs your review');
		expect(row.querySelector('.tile .title')?.textContent).toBe('Launch plan');
		expect(row.querySelector('.name')?.textContent).toBe('Ship the export');
		expect(row.querySelector('time')).not.toBeNull();
	});

	it('keeps a merged ask to one artifact row without duplicated update metadata', () => {
		mountCard(blocked, 2);

		expect(host.querySelector('.update-meta')).toBeNull();
		expect(host.querySelector('.earlier')).toBeNull();
		expect(host.querySelectorAll('.update-card.merged .tile')).toHaveLength(1);
		expect(focusPlanNode).not.toHaveBeenCalled();
	});

	it('puts the time on the title line and the feedback counts in the task\'s place, in the accessible name too', () => {
		mountCard({ ...blocked, blocked_asks_user: false, ask: ask({ state: 'approved_with_changes', feedback: { comments: 2, suggestions: 1 } }) });

		const tile = host.querySelector('.update-card.merged .tile')!;
		expect(tile.querySelector('.head time')).not.toBeNull();
		expect(tile.querySelector('.ctx time')).toBeNull();
		expect(tile.querySelector('.name')?.textContent).toBe('2 comments, 1 suggestion');
		expect(tile.getAttribute('aria-label')).toBe('Launch plan, document, Approved with changes, 2 comments, 1 suggestion');
	});

	it('keeps the task for a verdict that carries no feedback counts', () => {
		mountCard({ ...blocked, blocked_asks_user: false, ask: ask({ state: 'approved', feedback: { comments: 2 } }) });

		expect(host.querySelector('.update-card.merged .name')?.textContent).toBe('Ship the export');
	});

	it('keeps the merged row once answered, with its new round state', () => {
		mountCard({ ...blocked, blocked_asks_user: false, ask: ask({ state: 'answered' }) });

		expect(host.querySelector('.update-card.merged .tile')?.getAttribute('aria-label')).toContain('Answered');
	});

	it('shows no ask on a row that recorded none', () => {
		mountCard({ ...blocked, ask: null });

		expect(host.querySelector('.update-ask')).toBeNull();
	});
});
