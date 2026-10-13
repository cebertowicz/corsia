import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync } from 'svelte';
import { get } from 'svelte/store';
import { closeArtifactTab, openArtifactTab, threadWorkspaces } from './stores/artifactWorkspace';
import {
	clearTabDocumentImage, openTabDocumentImage, recordTabDocumentImageCamera, recordTabDocumentPlace,
	remapTabDocumentImage, tabDocumentImage, tabViewState,
} from './stores/tabViewState';

const THREAD = 'tab-place-thread';
const IMAGE = { src: '/img', alt: 'Mock', from: 9, to: 23, scrollTop: 40, top: 12 };

function open(path: string): string {
	openArtifactTab(THREAD, { path, title: path });
	return get(threadWorkspaces)[THREAD].activeTabId!;
}

describe('tab view place ownership', () => {
	beforeEach(() => {
		localStorage.clear();
		threadWorkspaces.set({});
	});

	it('keeps text anchors with their tab, never lets an incoming tab replace the outgoing one, and drops them on close', () => {
		const outgoing = open('/outgoing.md');
		const incoming = open('/incoming.md');
		const outgoingPlace = { from: 240, top: -8, scrollTop: 1548 };

		recordTabDocumentPlace(THREAD, outgoing, outgoingPlace);
		recordTabDocumentPlace(THREAD, incoming, { from: 0, top: 0, scrollTop: 0 });

		expect(tabViewState(THREAD, outgoing)?.documentPlace).toEqual(outgoingPlace);
		expect(tabViewState(THREAD, incoming)?.documentPlace).toEqual({ from: 0, top: 0, scrollTop: 0 });

		closeArtifactTab(THREAD, outgoing);
		expect(tabViewState(THREAD, outgoing)).toBeNull();
	});
});

describe('the document image a tab shows', () => {
	beforeEach(() => {
		localStorage.clear();
		threadWorkspaces.set({});
	});

	it('keeps only where the image is and what it opens on, starting each open at Fit', () => {
		const tab = open('/doc.md');
		const comments = { artifactId: 'a1', projectId: null, readOnly: false, post: vi.fn() };
		openTabDocumentImage(THREAD, tab, { ...IMAGE, focus: { id: 'pin' }, comments } as never);
		recordTabDocumentImageCamera(THREAD, tab, { scrollLeft: 5, scrollTop: 6, scale: 2 });
		expect(tabDocumentImage(THREAD, tab)).toEqual({ ...IMAGE, focus: { id: 'pin' }, camera: { scrollLeft: 5, scrollTop: 6, scale: 2 } });

		openTabDocumentImage(THREAD, tab, IMAGE);
		expect(tabDocumentImage(THREAD, tab)).toEqual({ ...IMAGE, focus: undefined });
	});

	it('follows its markdown through an edit of its own tab only, and changes nothing for an edit elsewhere', () => {
		const tab = open('/doc.md');
		const other = open('/other.md');
		openTabDocumentImage(THREAD, tab, IMAGE);
		const before = tabDocumentImage(THREAD, tab);
		remapTabDocumentImage(THREAD, other, (pos) => pos + 4);
		remapTabDocumentImage(THREAD, tab, (pos) => pos);
		remapTabDocumentImage('elsewhere', tab, (pos) => pos + 4);
		expect(tabDocumentImage(THREAD, tab)).toBe(before);

		remapTabDocumentImage(THREAD, tab, (pos) => pos + 4);
		expect(tabDocumentImage(THREAD, tab)).toMatchObject({ from: 13, to: 27 });
		expect(tabDocumentImage(THREAD, other)).toBeNull();
	});

	it('announces each open, move and close to an effect reading it, but not a camera change', () => {
		const tab = open('/doc.md');
		const seen: Array<number | null> = [];
		const stop = $effect.root(() => {
			$effect(() => { seen.push(tabDocumentImage(THREAD, tab)?.from ?? null); });
		});
		flushSync();
		openTabDocumentImage(THREAD, tab, IMAGE);
		flushSync();
		recordTabDocumentImageCamera(THREAD, tab, { scrollLeft: 1, scrollTop: 1, scale: null });
		flushSync();
		remapTabDocumentImage(THREAD, tab, (pos) => pos + 1);
		flushSync();
		clearTabDocumentImage(THREAD, tab);
		flushSync();
		openTabDocumentImage(THREAD, tab, IMAGE);
		flushSync();
		closeArtifactTab(THREAD, tab);
		flushSync();
		stop();
		expect(seen).toEqual([null, 9, 10, null, 9, null]);
		expect(tabViewState(THREAD, tab)).toBeNull();
	});
});
