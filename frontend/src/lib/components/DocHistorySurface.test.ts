import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, tick } from 'svelte';
import { createClassComponent } from 'svelte/legacy';
import DocHistorySurface from './DocHistorySurface.svelte';
import { fetchHistory, fetchVersionText, type DocVersion } from '$lib/docHistory';
import { connectionState } from '$lib/stores/connection';
import { RequestRefused } from '$lib/api/docs';
import { emitProductEvent } from '$lib/product_events';

vi.mock('$lib/docHistory', async (importOriginal) => ({
	...await importOriginal<typeof import('$lib/docHistory')>(),
	fetchHistory: vi.fn(),
	fetchVersionText: vi.fn(),
}));
vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));

const target = { instanceId: 'tab', path: '/workspace/skills/demo/SKILL.md', name: 'SKILL.md', projectId: null, threadId: null, artifactId: null };
let versions: DocVersion[];
let truncated: boolean;
let host: HTMLElement;
let component: { $destroy: () => void } | undefined;
const rows = () => Array.from(document.querySelectorAll<HTMLElement>('.vrow'));
const buttons = (name: string) => Array.from(document.querySelectorAll<HTMLButtonElement>('button')).filter((button) => button.textContent?.trim() === name);
const flush = async () => { for (let i = 0; i < 6; i++) { await Promise.resolve(); await tick(); flushSync(); } };

function render(extra: Record<string, unknown> = {}) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = createClassComponent({ target: host, component: DocHistorySurface, props: {
		target, narrow: false, onClose: vi.fn(), applyRestore: vi.fn(), onRestored: vi.fn(), ...extra,
	} }) as unknown as typeof component;
	flushSync();
}
async function open(index: number) { rows()[index].click(); await flush(); }
async function press() { buttons('Restore')[0].click(); await flush(); }
function wide() {
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	const canvas = document.createElement('div');
	Object.defineProperty(canvas, 'clientWidth', { value: 1112 });
	return canvas;
}
const restoredRow = () => ({ ...versions[2], id: 'new', reason: 'Restored Author 2’s version of Nov 3, 2026 at 8:02 AM PST.' });

beforeEach(() => {
	connectionState.set('connected');
	Element.prototype.scrollTo = vi.fn();
	versions = Array.from({ length: 4 }, (_, index) => ({ id: `v${index}`, author: `Author ${index}`, savedAt: '2026-11-03T16:02:00Z', reason: index === 1 ? null : `Reason ${index}` }));
	truncated = false;
	vi.mocked(fetchHistory).mockImplementation(async () => ({ versions: [...versions], truncated }));
	vi.mocked(fetchVersionText).mockImplementation(async (_target, id) => `Version ${id}\n`);
});
afterEach(() => {
	component?.$destroy();
	host?.remove();
	vi.resetAllMocks();
	vi.unstubAllGlobals();
});

describe('document history', () => {
	it('explains why restore is unavailable for a read-only file and does not request it', async () => {
		const applyRestore = vi.fn();
		render({ within: wide(), readOnly: true, applyRestore }); await flush(); await open(2);
		const restore = buttons('Restore')[0];
		expect(restore.getAttribute('aria-disabled')).toBe('true');
		expect(restore.getAttribute('aria-label')).toBe('Restore: this file is read-only');
		await press();
		expect(applyRestore).not.toHaveBeenCalled();
	});
	it('loads newest first, shows author, time, reason and the current mark', async () => {
		render(); await flush();
		expect(rows()).toHaveLength(4);
		expect(rows()[0].textContent).toContain('Author 0');
		expect(rows()[0].textContent).toContain('Current');
		expect(rows()[1].textContent).toContain('No reason given');
		expect(fetchHistory).toHaveBeenCalledWith(target);
	});
	it('shows a lost connection on load and retries it', async () => {
		vi.mocked(fetchHistory).mockRejectedValueOnce(new TypeError('offline'));
		render(); await flush();
		expect(host.textContent).toContain('Couldn’t load history. Check the connection and try again.');
		buttons('Retry')[0].click(); await flush();
		expect(rows()).toHaveLength(4);
	});
	it('does not blame the connection when the server cannot read history', async () => {
		vi.mocked(fetchHistory).mockRejectedValueOnce(new RequestRefused('503 History is unavailable', null));
		render(); await flush();
		expect(host.textContent).toContain('Couldn’t load history. Try again.');
		expect(host.textContent).not.toContain('connection');
	});
	it('ignores a history response after the drawer closes', async () => {
		let finish!: (value: { versions: DocVersion[]; truncated: boolean }) => void;
		vi.mocked(fetchHistory).mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
		render(); await flush();
		component!.$destroy();
		component = undefined;
		finish({ versions: [], truncated: false }); await flush();
		expect(rows()).toHaveLength(0);
	});
	it('says when there are no earlier versions', async () => {
		versions = versions.slice(0, 1);
		render(); await flush();
		expect(host.textContent).toContain('No earlier versions.');
	});
	it('says the list is the most recent versions only when the server says older ones exist', async () => {
		render(); await flush();
		expect(host.textContent).not.toContain('most recent versions');
		component!.$destroy(); host.remove();
		truncated = true;
		render(); await flush();
		expect(host.textContent).toContain('Showing the 4 most recent versions.');
	});
	it('opens the selected and preceding version, and steps older and newer', async () => {
		render(); await flush(); await open(1);
		expect(fetchVersionText).toHaveBeenCalledWith(target, 'v1');
		expect(fetchVersionText).toHaveBeenCalledWith(target, 'v2');
		expect(host.textContent).toContain('Version v1');
		expect(emitProductEvent).toHaveBeenCalledWith('document', 'version_viewed');
		buttons('Older')[0].click(); await flush();
		expect(host.textContent).toContain('Author 2');
		buttons('Newer')[0].click(); await flush();
		expect(host.textContent).toContain('Author 1');
	});
	it('keeps the list beside an open version on a wide canvas', async () => {
		render({ within: wide() }); await flush(); await open(2);
		expect(rows()).toHaveLength(4);
		expect(rows()[2].getAttribute('aria-current')).toBe('true');
	});
	it('keeps Restore focusable and explains why current is unavailable', async () => {
		render(); await flush(); await open(0);
		expect(buttons('Restore')[0].getAttribute('aria-disabled')).toBe('true');
		expect(buttons('Restore')[0].getAttribute('aria-label')).toBe('Restore: this is the current version');
	});
	it('shows pending until acknowledged, then the current text and the recorded row', async () => {
		let done!: () => void;
		const applyRestore = vi.fn(() => new Promise<void>((resolve) => { done = resolve; }));
		const onRestored = vi.fn(async () => {});
		render({ within: wide(), applyRestore, onRestored }); await flush(); await open(2);
		await press();
		expect(buttons('Restoring…')[0].disabled).toBe(true);
		expect(rows()).toHaveLength(4);
		versions = [restoredRow(), ...versions];
		done(); await flush();
		expect(applyRestore).toHaveBeenCalledWith(expect.objectContaining({ id: 'v2' }));
		expect(onRestored).toHaveBeenCalledOnce();
		expect(rows()).toHaveLength(5);
		expect(rows()[0].textContent).toContain('Restored Author 2');
		expect(buttons('Restoring…')).toHaveLength(0);
		expect(emitProductEvent).toHaveBeenCalledWith('document', 'version_restored');
	});
	it('offers one Retry after a failure and sends the same restore again', async () => {
		const applyRestore = vi.fn().mockRejectedValueOnce(new RequestRefused('503 failed', 'failed')).mockResolvedValueOnce(undefined);
		render({ within: wide(), applyRestore }); await flush(); await open(2);
		await press();
		expect(host.textContent).toContain('Couldn’t restore this version. The document is unchanged.');
		expect(host.textContent).not.toContain('connection');
		expect(buttons('Retry')).toHaveLength(1);
		buttons('Retry')[0].click(); await flush();
		expect(applyRestore).toHaveBeenCalledTimes(2);
	});
	it('reloads the changed document and the list before offering Retry when it changed after the version was opened', async () => {
		const applyRestore = vi.fn().mockRejectedValueOnce(new RequestRefused('409 stale', 'stale')).mockResolvedValueOnce(undefined);
		let reloaded!: () => void;
		const onRestored = vi.fn().mockReturnValueOnce(new Promise<void>((resolve) => { reloaded = resolve; })).mockResolvedValue(undefined);
		render({ within: wide(), applyRestore, onRestored }); await flush(); await open(2);
		vi.mocked(fetchHistory).mockClear();
		versions = [{ ...versions[0], id: 'outside', author: 'Unknown editor', reason: null }, ...versions];
		await press();
		expect(host.textContent).toContain('Couldn’t restore this version. The document is unchanged.');
		// The document and the list are still reloading: Retry would send a restore against the old text.
		expect(buttons('Retry')).toHaveLength(0);
		reloaded(); await flush();
		expect(onRestored).toHaveBeenCalledOnce();
		expect(fetchHistory).toHaveBeenCalledOnce();
		expect(rows()).toHaveLength(5);
		expect(rows()[3].getAttribute('aria-current')).toBe('true');
		expect(buttons('Retry')).toHaveLength(1);
		buttons('Retry')[0].click(); await flush();
		expect(applyRestore).toHaveBeenCalledTimes(2);
		expect(applyRestore).toHaveBeenLastCalledWith(expect.objectContaining({ id: 'v2' }));
	});
	it('says what to do when history is off, with no Retry', async () => {
		const applyRestore = vi.fn().mockRejectedValue(new RequestRefused('409 refused', 'history_off'));
		render({ within: wide(), applyRestore }); await flush(); await open(2);
		await press();
		expect(host.textContent).toContain('Couldn’t restore this version. This document’s history is off, so copy any text you need from the changes below.');
		expect(buttons('Retry')).toHaveLength(0);
		expect(buttons('Reload history')).toHaveLength(0);
		expect(buttons('Restore')).toHaveLength(1);
	});
	it('offers Reload history when the version is not in the history, and reloads the list', async () => {
		const applyRestore = vi.fn().mockRejectedValue(new RequestRefused('404 refused', 'version_unavailable'));
		render({ within: wide(), applyRestore }); await flush(); await open(2);
		await press();
		expect(host.textContent).toContain('Couldn’t restore this version. This document’s history doesn’t include it. Reload history to see the versions it has.');
		expect(buttons('Retry')).toHaveLength(0);
		vi.mocked(fetchHistory).mockClear();
		versions = versions.filter((row) => row.id !== 'v2');
		buttons('Reload history')[0].click(); await flush();
		expect(fetchHistory).toHaveBeenCalledOnce();
		expect(rows()).toHaveLength(3);
		expect(host.textContent).not.toContain('doesn’t include it');
		expect(applyRestore).toHaveBeenCalledOnce();
	});
	it.each([
		['unsaved_typing', 'Couldn’t restore this version. Your latest typing isn’t saved yet, so nothing was restored.'],
		['not_loaded', 'Couldn’t restore this version. The document is still loading.'],
	])('names the known cause of a %s refusal and offers Retry', async (kind, line) => {
		const applyRestore = vi.fn().mockRejectedValueOnce(new RequestRefused('refused before sending', kind)).mockResolvedValueOnce(undefined);
		render({ within: wide(), applyRestore }); await flush(); await open(2);
		await press();
		expect(host.textContent).toContain(line);
		expect(host.textContent).not.toContain('The document is unchanged.');
		buttons('Retry')[0].click(); await flush();
		expect(applyRestore).toHaveBeenCalledTimes(2);
	});
	it.each([
		['invalid_skill', 'Couldn’t restore this version. Its text isn’t a valid skill. Copy any text you need from the changes below.'],
		['context_read_only', 'Couldn’t restore this version. This file is read-only. Copy any text you need from the changes below.'],
	])('names the cause of a %s refusal and offers no way to ask again', async (kind, line) => {
		const applyRestore = vi.fn().mockRejectedValue(new RequestRefused('422 refused', kind));
		render({ within: wide(), applyRestore }); await flush(); await open(2);
		await press();
		expect(host.textContent).toContain(line);
		expect(buttons('Retry')).toHaveLength(0);
		expect(buttons('Reload history')).toHaveLength(0);
		expect(applyRestore).toHaveBeenCalledOnce();
	});
	it('lets the reloaded list decide a lost answer: a new row holding the restored text is the restore', async () => {
		vi.mocked(fetchVersionText).mockImplementation(async (_target, id) => `Version ${id === 'new' ? 'v2' : id}\n`);
		const applyRestore = vi.fn().mockRejectedValue(new TypeError('disconnected'));
		const onRestored = vi.fn(async () => {});
		render({ within: wide(), applyRestore, onRestored }); await flush(); await open(2);
		await press();
		expect(buttons('Restoring…')[0].disabled).toBe(true);
		expect(host.textContent).toContain('Showing the last loaded history. Restore needs a connection to CORSIA.');
		expect(host.textContent).not.toContain('The document is unchanged.');
		expect(buttons('Retry')).toHaveLength(1);
		versions = [restoredRow(), ...versions];
		buttons('Retry')[0].click(); await flush();
		expect(applyRestore).toHaveBeenCalledTimes(1);
		expect(onRestored).toHaveBeenCalledOnce();
		expect(buttons('Restoring…')).toHaveLength(0);
		expect(rows()[0].textContent).toContain('Restored Author 2');
	});
	it('lets the reloaded list decide a lost answer: a new row with other text is not the restore', async () => {
		const applyRestore = vi.fn().mockRejectedValue(new TypeError('disconnected'));
		const onRestored = vi.fn(async () => {});
		render({ within: wide(), applyRestore, onRestored }); await flush(); await open(2);
		await press();
		versions = [{ ...versions[0], id: 'agent-edit', reason: 'An agent edit' }, ...versions];
		buttons('Retry')[0].click(); await flush();
		expect(applyRestore).toHaveBeenCalledTimes(1);
		expect(onRestored).not.toHaveBeenCalled();
		expect(emitProductEvent).not.toHaveBeenCalledWith('document', 'version_restored');
		expect(host.textContent).toContain('Couldn’t confirm the restore. The newest version doesn’t hold this version’s text.');
		expect(buttons('Restore')).toHaveLength(1);
	});
	it('lets the reloaded list decide a lost answer: no new row means unchanged', async () => {
		const applyRestore = vi.fn().mockRejectedValueOnce(new TypeError('disconnected'));
		render({ within: wide(), applyRestore }); await flush(); await open(2);
		await press();
		buttons('Retry')[0].click(); await flush();
		expect(applyRestore).toHaveBeenCalledTimes(1);
		expect(host.textContent).toContain('Couldn’t restore this version. The document is unchanged.');
		expect(buttons('Restore')).toHaveLength(1);
	});
	it('shows the long-diff message when a version is over 400 lines', async () => {
		vi.mocked(fetchVersionText).mockResolvedValue(Array.from({ length: 401 }, () => 'line').join('\n'));
		render(); await flush(); await open(1);
		expect(host.textContent).toContain('Changes are too long to show.');
	});
	it('goes back before closing the phone sheet on Escape', async () => {
		const onClose = vi.fn();
		render({ narrow: true, onClose }); await flush(); await open(1);
		const section = document.querySelector('[data-doc-history-drawer]')!;
		section.querySelector('button')!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
		await flush();
		expect(onClose).not.toHaveBeenCalled();
		expect(rows()).toHaveLength(4);
	});
});
