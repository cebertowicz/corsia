import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { EditorView } from '@codemirror/view';
import DocViewer from './DocViewer.svelte';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import type { ServerComment } from '$lib/api/docs';
import { connectionState } from '$lib/stores/connection';
import { docComments } from '$lib/stores/docComments';
import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { clearDraft } from '$lib/stores/docSaveQueue';

const api = vi.hoisted(() => ({ fetchContent: vi.fn(), fetchRevision: vi.fn(), fetchComments: vi.fn() }));
const room = vi.hoisted(() => ({ margin: true }));
vi.mock('$lib/api/docs', async importOriginal => ({ ...await importOriginal<typeof import('$lib/api/docs')>(), ...api }));
vi.mock('$lib/stores/deviceDocument', () => ({ DeviceDocument: class {
	ready = Promise.resolve(); saved = true; error = ''; destroy = async () => {};
	subscribe(listener: (copy: unknown) => void) { listener(this); return () => {}; }
} }));
vi.mock('$lib/stores/liveDocuments', async importOriginal => ({ ...await importOriginal<typeof import('$lib/stores/liveDocuments')>(), openLiveDocument: vi.fn(async () => null) }));
vi.mock('$lib/stores/viewport', async importOriginal => ({ ...await importOriginal<typeof import('$lib/stores/viewport')>(), isMobile: (await import('svelte/store')).writable(false) }));
vi.mock('$lib/stores/deviceRecovery', () => ({ readDeviceRecovery: async () => [] }));
// jsdom has no layout: the test says whether the canvas has room for the margin column.
vi.mock('$lib/docCommentSurfaceState.svelte', async importOriginal => ({ ...await importOriginal<typeof import('$lib/docCommentSurfaceState.svelte')>(), hasRoomForMargin: () => room.margin }));

const TEXT = 'Alpha opens the plan.\n\nBeta closes the plan.\n';
let tabs = 0;
let target: DocViewerTarget;
let host: HTMLElement;
let component: ReturnType<typeof mount> | undefined;
let innerWidth: number;

const thread = (id: string, from: number, to: number, anchor: string, resolved: boolean, artifact: string) => ({
	id, artifact_id: artifact, parent_id: null, author: 'you', author_kind: 'user', body: `ask ${id}`, anchor_text: anchor,
	from_off: from, to_off: to, created_at: '2026-11-16T01:24:00+00:00', review: false, resolved, replies: [],
}) as unknown as ServerComment;

async function open() {
	const artifact = `artifact-${++tabs}`;
	const comments = [thread('alpha', 0, 5, 'Alpha', false, artifact), thread('beta', TEXT.indexOf('Beta'), TEXT.indexOf('Beta') + 4, 'Beta', true, artifact)];
	api.fetchComments.mockResolvedValue({ comments, feedback: { open_count: 1, owner_label: null, undelivered_batches: [] } });
	api.fetchContent.mockResolvedValue({ text: TEXT, writable: true, artifact: { id: artifact }, freshness: null });
	api.fetchRevision.mockResolvedValue({ exists: true, writable: true, artifact_id: artifact, freshness: null });
	target = { instanceId: `resolved-entry-tab-${tabs}`, path: '/documents/plan.md', name: 'plan.md', projectId: 'project', threadId: 'thread', artifactId: artifact };
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(DocViewer, { target: host, props: { target } });
	flushSync();
	await vi.waitFor(() => expect(host.querySelector('.cm-content')?.textContent).toContain('Alpha opens the plan.'));
	await vi.waitFor(() => expect(host.querySelector('[data-resolved-tool]')).not.toBeNull());
}

const control = () => host.querySelector<HTMLButtonElement>('.document-status-row [data-resolved-tool]')!;
const pressed = () => document.body.querySelector('.drawer-switch button[aria-pressed="true"]')?.textContent?.trim();
const listUp = () => document.body.querySelector('.drawer-switch') !== null;

beforeEach(() => {
	innerWidth = window.innerWidth;
	Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 });
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.stubGlobal('IntersectionObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ open_count: 0, undelivered_batches: [], reviews: [], reviewers: [] }), { status: 200 })));
	HTMLElement.prototype.scrollIntoView = vi.fn();
	Range.prototype.getClientRects = vi.fn(() => [] as unknown as DOMRectList);
	Range.prototype.getBoundingClientRect = vi.fn(() => new DOMRect());
	vi.spyOn(EditorView.prototype, 'coordsAtPos').mockReturnValue(null);
	connectionState.set('connected');
});
afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	host?.remove();
	if (target) clearDraft(target);
	docComments.set({});
	focusedDocComment.set(null);
	Object.defineProperty(window, 'innerWidth', { configurable: true, value: innerWidth });
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	for (const mock of Object.values(api)) mock.mockReset();
});

describe('the document row\'s Resolved control opens the comments drawer on its Resolved view', () => {
	it('opens a closed popover list on Resolved, and a second press closes it', async () => {
		room.margin = false;
		await open();
		expect(control().textContent?.trim()).toBe('Resolved · 1');
		expect(listUp()).toBe(false);
		control().click();
		await vi.waitFor(() => expect(pressed()).toBe('Resolved · 1'));
		expect(control().getAttribute('aria-expanded')).toBe('true');
		control().click();
		await vi.waitFor(() => expect(listUp()).toBe(false));
		expect(control().getAttribute('aria-expanded')).toBe('false');
	});

	it('switches a list that is already open on Open to Resolved, and closes it from Resolved', async () => {
		room.margin = true;
		await open();
		await vi.waitFor(() => expect(pressed()).toBe('Open · 1'));
		control().click();
		await vi.waitFor(() => expect(pressed()).toBe('Resolved · 1'));
		control().click();
		await vi.waitFor(() => expect(listUp()).toBe(false));
	});
});
