import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import { EditorView } from '@codemirror/view';
import DocViewer from './DocViewer.svelte';
import { connectionState } from '$lib/stores/connection';
import { emit } from '$lib/ws/events';
import { startLiveDocuments } from '$lib/stores/liveDocuments';
import { clearDraft, existingDraft, flushDraft } from '$lib/stores/docSaveQueue';
import type { DocViewerTarget } from '$lib/docViewerTarget';

const api = vi.hoisted(() => ({ fetchContent: vi.fn(), writeContent: vi.fn(), fetchRevision: vi.fn(), fetchComments: vi.fn() }));
vi.mock('$lib/api/docs', async importOriginal => ({ ...await importOriginal<typeof import('$lib/api/docs')>(), ...api }));
vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));
const wire = vi.hoisted(() => ({ frames: [] as Record<string, any>[] }));
vi.mock('$lib/ws/state', () => ({ ws: { readyState: 1, send: (text: string) => wire.frames.push(JSON.parse(text)) } }));
vi.mock('$lib/stores/deviceDocument', () => ({ DeviceDocument: class {
	ready = Promise.resolve(); saved = true; error = '';
	destroy = async () => {};
	subscribe(listener: (copy: unknown) => void) { listener(this); return () => {}; }
} }));
vi.mock('$lib/stores/deviceRecovery', () => ({ readDeviceRecovery: async () => [] }));

const target: DocViewerTarget = { instanceId: 'stale-choice-viewer', path: '/documents/doc.md', name: 'doc.md', projectId: 'project', threadId: 'thread', artifactId: 'artifact' };
const missing = 'Couldn’t save because this document is deleted. Your text stays here until you close it.';
const staleRefusal = () => new Error(`409 ${JSON.stringify({ detail: { code: 'stale_base', message: 'Plain sentence.' } })}`);
const file = (text: string, revision: string) => ({ text, revision, writable: true, history: false, artifact: { id: 'artifact' }, freshness: null, live: false });
let component: ReturnType<typeof mount> | undefined;
let host: HTMLElement;
let stop: () => void;
const flush = async () => { for (let i = 0; i < 6; i++) { await Promise.resolve(); await tick(); flushSync(); } };
const buttons = (text: string) => [...document.querySelectorAll('button')].filter(button => button.textContent?.trim() === text);
const dialog = () => document.querySelector<HTMLElement>('[role="alertdialog"]');
const confirm = (label: string) => [...dialog()!.querySelectorAll('button')].find(button => button.textContent?.trim() === label)!.click();
// The alerts inside Details; the pane's own save bar (outside it) keeps saying what it always says.
const alerts = () => [...document.querySelectorAll('[role="alert"]')].filter(node => !node.closest('.save-error')).map(node => node.textContent ?? '').join(' | ');

beforeEach(() => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.spyOn(EditorView.prototype, 'coordsAtPos').mockReturnValue(null);
	vi.spyOn(console, 'error').mockImplementation(() => {});
	Element.prototype.scrollTo = vi.fn();
	connectionState.set('connected');
	stop = startLiveDocuments();
	api.fetchRevision.mockResolvedValue({ exists: true, writable: true, artifact_id: 'artifact', freshness: null });
	api.fetchComments.mockResolvedValue({ comments: [], feedback: { open_count: 0, owner_label: null, undelivered_batches: [] } });
});
afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	host?.remove();
	stop();
	clearDraft(target);
	document.body.innerHTML = '';
	wire.frames.length = 0;
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	for (const mock of Object.values(api)) mock.mockReset();
});

async function openRefused(): Promise<HTMLButtonElement> {
	api.fetchContent.mockResolvedValue(file('base text', 'r1'));
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(DocViewer, { target: host, props: { target } });
	flushSync();
	await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_open')).toBe(true));
	emit('doc_opened', { type: 'doc_opened', request_id: wire.frames.findLast(frame => frame.type === 'doc_open')!.request_id, live: false });
	await vi.waitFor(() => expect(host.querySelector('.cm-content')?.textContent).toBe('base text'));

	// The first save is refused because the file changed after the tab's base.
	const view = EditorView.findFromDOM(host.querySelector<HTMLElement>('.cm-editor')!)!;
	view.dispatch({ changes: { from: 9, insert: ' typed' }, userEvent: 'input.type' });
	await flush();
	api.fetchContent.mockResolvedValue(file('file changed outside', 'r2'));
	api.writeContent.mockRejectedValueOnce(staleRefusal());
	await flushDraft(target);
	await flush();
	const details = host.querySelector<HTMLButtonElement>('.stale-details');
	expect(details).not.toBeNull();
	return details!;

}

describe('the stale-save chooser inside the viewer', () => {
	it('stays up with its error when Keep your text fails for a reason other than a changed file, and retries', async () => {
		const details = await openRefused();

		// Keep your text, and the write fails on the network, not on the file.
		details.click();
		await vi.waitFor(() => expect(document.body.textContent).toContain('file changed outside'));
		api.writeContent.mockRejectedValueOnce(new TypeError('Failed to fetch'));
		buttons('Keep your text')[0].click(); flushSync();
		confirm('Keep your text');
		await vi.waitFor(() => expect(api.writeContent).toHaveBeenCalledTimes(2));
		await flush();

		// The chooser and its error are still on screen, and the same choice can be made again.
		expect(host.querySelector('.stale-details')).not.toBeNull();
		expect(buttons('Keep your text').length).toBeGreaterThan(0);
		// Details says the chooser's own sentence (who acts next), not the generic no-detail failure or the refusal's sentence.
		expect(alerts()).toContain('Couldn’t save the document. Your text stays here. Choose Keep your text to try again.');
		expect(alerts()).not.toContain('edits are still local');
		expect(alerts()).not.toContain('changed after you started editing');
		// The pane's own bar, outside Details, says what a no-detail failure says; it is the string a person reads there.
		expect(host.querySelector('.save-error')?.textContent).toMatch(/^Couldn't save the document \u2014 edits are still local\s*Details\s*$/);
		api.writeContent.mockResolvedValueOnce({ revision: 'r3' });
		buttons('Keep your text')[0].click(); flushSync();
		confirm('Keep your text');
		await vi.waitFor(() => expect(api.writeContent).toHaveBeenCalledTimes(3));
		await vi.waitFor(() => expect(host.querySelector('.stale-details')).toBeNull());
	});

	it('offers no choice once the file is gone: a Keep your text that finds it deleted reads it again and keeps his text', async () => {
		const details = await openRefused();
		details.click();
		await vi.waitFor(() => expect(document.body.textContent).toContain('file changed outside'));
		// The file is deleted after Details read it: the write is refused as missing, and reading it again fails too.
		api.writeContent.mockRejectedValueOnce(new Error(`404 ${JSON.stringify({ detail: missing })}`));
		api.fetchContent.mockRejectedValue(new Error('404 not found'));
		buttons('Keep your text')[0].click(); flushSync();
		confirm('Keep your text');
		await vi.waitFor(() => expect(api.writeContent).toHaveBeenCalledTimes(2));
		await vi.waitFor(() => expect(alerts()).toContain('Couldn’t load the file’s text. Your text stays here.'));
		// Neither choice is offered over a file that no longer reads, Retry is, and the deleted-file line is Details' own.
		expect(buttons('Use the file’s text')).toHaveLength(0);
		expect(buttons('Keep your text')).toHaveLength(0);
		expect(buttons('Retry')).toHaveLength(1);
		expect(alerts()).toContain(missing);
		// His text is still in the editor, the tab is still marked unsaved, and nothing has replaced it.
		expect(host.querySelector('.cm-content')?.textContent).toBe('base text typed');
		expect(existingDraft(target)).toMatchObject({ editText: 'base text typed', status: 'error', stale: true });
	});

	it('names no hidden button when Keep your text fails on the network and the file then cannot be read', async () => {
		const details = await openRefused();
		details.click();
		await vi.waitFor(() => expect(document.body.textContent).toContain('file changed outside'));
		// The write fails on the network, and the re-read that follows fails too: Details is left with Retry only.
		api.writeContent.mockRejectedValueOnce(new TypeError('Failed to fetch'));
		api.fetchContent.mockRejectedValue(new TypeError('Failed to fetch'));
		buttons('Keep your text')[0].click(); flushSync();
		confirm('Keep your text');
		await vi.waitFor(() => expect(api.writeContent).toHaveBeenCalledTimes(2));
		await vi.waitFor(() => expect(alerts()).toContain('Couldn’t load the file’s text. Your text stays here.'));
		await flush();
		expect(buttons('Retry')).toHaveLength(1);
		// The failed-save line points to nothing that is not on screen, and nothing in Details names the hidden choice.
		const inDetails = document.querySelector('[aria-label="Choose which text to keep"]')!;
		expect(inDetails).not.toBeNull();
		expect(alerts()).toContain('Couldn’t save the document. Your text stays here.');
		expect(inDetails.textContent).not.toContain('Keep your text');
		expect(inDetails.textContent).not.toContain('Choose Keep your text');
		expect(existingDraft(target)).toMatchObject({ editText: 'base text typed', status: 'error' });
	});
});
