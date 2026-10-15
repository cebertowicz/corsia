import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { EditorState } from '@codemirror/state';
import { EditorView, keymap } from '@codemirror/view';
import { defaultKeymap } from '@codemirror/commands';
import { markdown } from '@codemirror/lang-markdown';
import { forceParsing, syntaxTree, syntaxTreeAvailable } from '@codemirror/language';
import { GFM } from '@lezer/markdown';
import { commentMarks, setCommentRanges } from './comment-marks';
import { markdownLivePreview } from './markdown-live';
import { markdownTasks } from './markdown-tasks';
import { documentLinkTarget, linkOpener, linkTitle } from './markdown-link';
import * as productEvents from '$lib/product_events';
import { resolveLinkTarget, type LinkResolverDeps } from '$lib/linkTargetResolver';
import { openArtifactTab, setActiveTab, threadWorkspaces } from '$lib/stores/artifactWorkspace';

const DOC_PATH = '/workspace/shared/documents/INDEX.md';
const views: EditorView[] = [];

interface Options {
	opener?: ((href: string) => void) | null;
	readOnly?: boolean;
	cursor?: number;
	/** Called for a mousedown that reaches the handlers after the link's, i.e. one the link did not take. */
	passedOn?: () => void;
}

function mount(doc: string, { opener = vi.fn(), readOnly = false, cursor, passedOn }: Options = {}, finishParsing = true) {
	const state = EditorState.create({
		doc,
		selection: cursor != null ? { anchor: cursor } : undefined,
		extensions: [
			markdown({ extensions: GFM }), markdownLivePreview, markdownTasks, commentMarks,
			keymap.of(defaultKeymap),
			EditorState.readOnly.of(readOnly),
			...(opener ? [linkOpener.of(opener)] : []),
			EditorView.domEventHandlers({ mousedown: () => { passedOn?.(); return false; } }),
		],
	});
	const view = new EditorView({ state, parent: document.body });
	if (finishParsing && !forceParsing(view, view.state.doc.length, 1_000)) throw new Error('markdown parser did not finish');
	views.push(view);
	return view;
}

/** Counts the transactions a view applies after mount, so a click can be shown to write nothing. */
function countTransactions(view: EditorView) {
	const spy = vi.spyOn(view, 'dispatch');
	return () => spy.mock.calls.length;
}

function mouse(type: 'mousedown' | 'click', target: Element, init: MouseEventInit = {}) {
	const event = new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, ...init });
	target.dispatchEvent(event);
	return event;
}

/** A press and release on one element, as a click or a tap delivers them. */
function press(target: Element, init: MouseEventInit = {}) {
	return { down: mouse('mousedown', target, init), up: mouse('click', target, init) };
}

const keydown = (view: EditorView, init: KeyboardEventInit) =>
	view.contentDOM.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init }));

const link = (view: EditorView, text?: string) => {
	const marks = [...view.dom.querySelectorAll<HTMLElement>('.cm-md-link')];
	const found = text ? marks.find((mark) => mark.textContent === text) : marks[0];
	if (!found) throw new Error(`no link ${text ?? ''}`);
	return found;
};

afterEach(() => {
	while (views.length) views.pop()!.destroy();
	vi.useRealTimers();
});

describe('background Markdown parsing', () => {
	it('decorates a link when background parsing reaches it without a document edit', async () => {
		vi.useFakeTimers();
		const target = 'deferred link';
		// Paragraphs, not one long line: Markdown parses a block whole, so only separate blocks can lie past the
		// initial parse reach (3000 characters). jsdom has no layout, so the viewport spans these few long lines.
		const filler = Array.from({ length: 10 }, () => 'x'.repeat(600)).join('\n\n');
		const doc = `${filler}\n\n[${target}](./deferred.md)\n`;
		const linkFrom = doc.indexOf(`[${target}]`);
		const view = mount(doc, { opener: vi.fn() }, false);
		expect(view.viewport.to).toBeGreaterThan(linkFrom);
		const initialTree = syntaxTree(view.state);

		expect(syntaxTreeAvailable(view.state, linkFrom)).toBe(false);
		expect(view.dom.querySelector('.cm-md-link')).toBeNull();

		await vi.advanceTimersByTimeAsync(500);

		expect(syntaxTree(view.state)).not.toBe(initialTree);
		expect(syntaxTreeAvailable(view.state, linkFrom)).toBe(true);
		expect(view.state.doc.toString()).toBe(doc);
		expect(view.dom.querySelector('.cm-md-link')?.textContent).toBe(target);
	});
});

describe('a click on link text', () => {
	const doc = '# Index\n\nRead [the decisions](./DECISIONS.md) first.\n';

	it('opens the link and writes nothing: document and selection are unchanged', () => {
		const opener = vi.fn();
		const view = mount(doc, { opener, cursor: 3 });
		const transactions = countTransactions(view);
		const { down, up } = press(link(view));
		expect(opener).toHaveBeenCalledExactlyOnceWith('./DECISIONS.md');
		expect(down.defaultPrevented).toBe(true);
		expect(up.defaultPrevented).toBe(true);
		expect(transactions()).toBe(0);
		expect(view.state.doc.toString()).toBe(doc);
		expect(view.state.selection.main.head).toBe(3);
	});

	it('hands over the target as written, whatever its form', () => {
		const targets = ['../y.md', '/workspace/shared/z.md', 'https://example.com/a?b=1', '/thread/abc-123', '/settings/providers', 'mailto:a@b.c', 'notes/x.md#top'];
		for (const target of targets) {
			const opener = vi.fn();
			const view = mount(`[go](${target})\n`, { opener });
			press(link(view));
			expect(opener).toHaveBeenCalledExactlyOnceWith(target);
		}
	});

	it('opens a link in a table row, a list item and a quote as it does in prose', () => {
		const opener = vi.fn();
		const view = mount('- item [a](a.md)\n\n> quote [b](b.md)\n\n| h |\n|---|\n| [c](c.md) |\n', { opener });
		press(link(view, 'a'));
		press(link(view, 'b'));
		expect(opener.mock.calls).toEqual([['a.md'], ['b.md']]);
	});

	it('opens in a read-only document too', () => {
		const opener = vi.fn();
		const view = mount(doc, { opener, readOnly: true });
		const transactions = countTransactions(view);
		press(link(view));
		expect(opener).toHaveBeenCalledWith('./DECISIONS.md');
		expect(transactions()).toBe(0);
	});

	it('opens a link inside a comment highlight', () => {
		const opener = vi.fn();
		const view = mount(doc, { opener });
		const from = doc.indexOf('[the decisions]');
		view.dispatch({ effects: setCommentRanges.of([{ from, to: doc.indexOf(' first'), id: 'c1' }]) });
		expect(view.dom.querySelector('.cm-comment-anchor')).not.toBeNull();
		press(link(view));
		expect(opener).toHaveBeenCalledWith('./DECISIONS.md');
	});

	it('does not follow an in-document anchor: the press places the caret as before', () => {
		const opener = vi.fn();
		const passedOn = vi.fn();
		const view = mount('[top](#heading)\n', { opener, passedOn });
		const target = link(view);
		expect(target.classList.contains('cm-md-link-open')).toBe(false);
		const { up } = press(target);
		expect(opener).not.toHaveBeenCalled();
		expect(passedOn).toHaveBeenCalledOnce();
		expect(up.defaultPrevented).toBe(false);
	});

	it('leaves a Shift-press to extend the selection', () => {
		const opener = vi.fn();
		const passedOn = vi.fn();
		const view = mount('[a](a.md)\n', { opener, passedOn });
		const { up } = press(link(view), { shiftKey: true });
		expect(opener).not.toHaveBeenCalled();
		expect(passedOn).toHaveBeenCalledOnce();
		expect(up.defaultPrevented).toBe(false);
	});

	it('leaves a drag that starts outside the link to select across it', () => {
		const opener = vi.fn();
		const passedOn = vi.fn();
		const view = mount(doc, { opener, passedOn });
		mouse('mousedown', view.dom.querySelector('.cm-line')!);
		expect(passedOn).toHaveBeenCalledOnce();
		// The release lands on the link, but the click is delivered to the common ancestor, not to the link.
		mouse('click', view.contentDOM);
		expect(opener).not.toHaveBeenCalled();
	});

	it('ignores a secondary button', () => {
		const opener = vi.fn();
		const passedOn = vi.fn();
		const view = mount(doc, { opener, passedOn });
		press(link(view), { button: 2 });
		expect(opener).not.toHaveBeenCalled();
		expect(passedOn).toHaveBeenCalledOnce();
	});

	it('only styles a link when the host supplies no opener', () => {
		const passedOn = vi.fn();
		const view = mount(doc, { opener: null, passedOn });
		expect(link(view).classList.contains('cm-md-link-open')).toBe(false);
		expect(link(view).hasAttribute('title')).toBe(false);
		press(link(view));
		expect(passedOn).toHaveBeenCalledOnce();
	});
});

describe('the keyboard', () => {
	const doc = 'See [the decisions](./DECISIONS.md) now.\n';
	const inside = doc.indexOf('decisions');

	it('opens the link under the caret on Alt-Enter and writes nothing', () => {
		const opener = vi.fn();
		const view = mount(doc, { opener, cursor: inside });
		const transactions = countTransactions(view);
		keydown(view, { key: 'Enter', altKey: true });
		expect(opener).toHaveBeenCalledExactlyOnceWith('./DECISIONS.md');
		expect(transactions()).toBe(0);
		expect(view.state.doc.toString()).toBe(doc);
	});

	it('opens with the caret at either end of the link', () => {
		for (const cursor of [doc.indexOf('['), doc.indexOf(')') + 1]) {
			const opener = vi.fn();
			keydown(mount(doc, { opener, cursor }), { key: 'Enter', altKey: true });
			expect(opener).toHaveBeenCalledOnce();
		}
	});

	it('does nothing on Alt-Enter outside a link, and in an editor with no opener', () => {
		const opener = vi.fn();
		keydown(mount(doc, { opener, cursor: 1 }), { key: 'Enter', altKey: true });
		expect(opener).not.toHaveBeenCalled();
		const view = mount(doc, { opener: null, cursor: inside });
		keydown(view, { key: 'Enter', altKey: true });
		expect(view.state.doc.toString()).toBe(doc);
	});

	it('opens in a read-only document', () => {
		const opener = vi.fn();
		keydown(mount(doc, { opener, readOnly: true, cursor: inside }), { key: 'Enter', altKey: true });
		expect(opener).toHaveBeenCalledOnce();
	});

	it('leaves Enter inserting a newline, even inside a link', () => {
		const opener = vi.fn();
		const view = mount(doc, { opener, cursor: inside });
		keydown(view, { key: 'Enter' });
		expect(opener).not.toHaveBeenCalled();
		expect(view.state.doc.toString()).toBe(doc.slice(0, inside) + '\n' + doc.slice(inside));
	});

	it('leaves Mod-Enter as the task toggle', () => {
		const opener = vi.fn();
		const view = mount('- [ ] see [a](a.md)\n', { opener, cursor: 10 });
		keydown(view, { key: 'Enter', ctrlKey: true });
		expect(opener).not.toHaveBeenCalled();
		expect(view.state.doc.toString()).toBe('- [x] see [a](a.md)\n');
	});
});

describe('hover', () => {
	it('shows a path as written and a web link as host and path', () => {
		const view = mount('[a](../notes/a.md) [b](https://docs.example.com/guide/start?x=1#h) [c](https://example.com/)\n');
		expect(link(view, 'a').title).toBe('../notes/a.md');
		expect(link(view, 'b').title).toBe('docs.example.com/guide/start');
		expect(link(view, 'c').title).toBe('example.com');
		expect(link(view, 'a').classList.contains('cm-md-link-open')).toBe(true);
	});

	it('shows any other target as written', () => {
		expect(linkTitle('mailto:a@b.c')).toBe('mailto:a@b.c');
		expect(linkTitle('/thread/abc')).toBe('/thread/abc');
	});
});

describe('documentLinkTarget', () => {
	it('resolves a relative target from the document folder, query and fragment dropped', () => {
		expect(documentLinkTarget(DOC_PATH, './DECISIONS.md')).toBe('/workspace/shared/documents/DECISIONS.md');
		expect(documentLinkTarget(DOC_PATH, 'DECISIONS.md?v=2#top')).toBe('/workspace/shared/documents/DECISIONS.md');
		expect(documentLinkTarget(DOC_PATH, '../y.md')).toBe('/workspace/shared/y.md');
		expect(documentLinkTarget(DOC_PATH, 'my%20notes/a%25b.md')).toBe('/workspace/shared/documents/my%20notes/a%25b.md');
	});

	it('passes absolute paths, anchors and other schemes on as written', () => {
		for (const href of ['/workspace/a.md', '/thread/abc', '/settings/providers', '#top', 'https://example.com/a', 'mailto:a@b.c']) {
			expect(documentLinkTarget(DOC_PATH, href)).toBe(href);
		}
	});

	it('leaves a relative target alone when the document has no absolute path', () => {
		expect(documentLinkTarget(null, './a.md')).toBe('./a.md');
	});
});

describe('following a document link through the app router', () => {
	const ROOTS = [{ id: 'w', label: 'Workspace', kind: 'workspace' as const, path: '/workspace', nodes: [], capped: false }];
	const THREAD = 'thread-links';

	function deps(overrides: Partial<LinkResolverDeps> = {}): LinkResolverDeps {
		return {
			activeThreadId: () => THREAD,
			projectId: () => 'project-1',
			fetchTree: vi.fn().mockResolvedValue(ROOTS),
			fetchRevision: vi.fn().mockImplementation(async (path: string) => ({ exists: !path.endsWith('missing.md'), path, revision: 'r', size: 1, writable: true, artifact_id: null })),
			openArtifact: vi.fn(),
			showError: vi.fn(),
			openExternal: vi.fn().mockResolvedValue(undefined),
			isTauri: () => false,
			origin: () => 'http://corsia.test',
			openSettings: vi.fn(),
			openThread: vi.fn(),
			...overrides,
		};
	}

	const follow = (href: string, d: LinkResolverDeps) => resolveLinkTarget(documentLinkTarget(DOC_PATH, href), d);
	const opened = (d: LinkResolverDeps) => vi.mocked(d.openArtifact).mock.calls.map(([, input]) => input.path);

	it('opens a relative, a parent-relative and an absolute workspace document by its resolved path', async () => {
		const d = deps();
		expect(await follow('./DECISIONS.md', d)).toBe('document');
		expect(await follow('../y.md', d)).toBe('document');
		expect(await follow('/workspace/other/z.md', d)).toBe('document');
		expect(opened(d)).toEqual(['/workspace/shared/documents/DECISIONS.md', '/workspace/shared/y.md', '/workspace/other/z.md']);
	});

	it('does not find a path that leaves every workspace root, and tells the person', async () => {
		const d = deps();
		expect(await follow('../../../../outside.md', d)).toBe('error');
		expect(await follow('/etc/hosts', d)).toBe('error');
		expect(d.openArtifact).not.toHaveBeenCalled();
		expect(d.showError).toHaveBeenCalledTimes(2);
		expect(d.showError).toHaveBeenCalledWith("Couldn't find the linked workspace document");
	});

	it('tells the person when the document is missing', async () => {
		const d = deps();
		expect(await follow('./missing.md', d)).toBe('error');
		expect(d.showError).toHaveBeenCalledWith("Couldn't find the linked workspace document");
		expect(d.openArtifact).not.toHaveBeenCalled();
	});

	it('opens a web link in the browser, and routes thread and settings links in the app', async () => {
		const d = deps();
		expect(await follow('https://example.com/a', d)).toBe('external');
		expect(d.openExternal).toHaveBeenCalledWith('https://example.com/a');
		expect(await follow('/thread/abc-123', d)).toBe('thread');
		expect(d.openThread).toHaveBeenCalledWith('abc-123');
		expect(await follow('/settings/providers', d)).toBe('settings');
		expect(d.openSettings).toHaveBeenCalledWith('providers', null);
		expect(d.openArtifact).not.toHaveBeenCalled();
	});

	it('shows the unsupported-destination toast for another scheme', async () => {
		const d = deps();
		expect(await follow('mailto:a@b.c', d)).toBe('error');
		expect(d.showError).toHaveBeenCalledWith('This link uses an unsupported destination');
	});

	it('reports one link.open event per open, with no href or path', async () => {
		const emit = vi.spyOn(productEvents, 'emitProductEvent');
		const d = deps();
		await follow('./DECISIONS.md', d);
		await follow('./missing.md', d);
		expect(emit.mock.calls).toEqual([
			['link', 'open', { target: 'workspace_document', outcome: 'succeeded' }],
			['link', 'open', { target: 'workspace_document', outcome: 'failed', reason: 'not_found' }],
		]);
		emit.mockRestore();
	});

	describe('in the tab strip', () => {
		beforeEach(() => {
			localStorage.clear();
			threadWorkspaces.set({});
		});

		it('adds a tab and activates it, and focuses an open one instead of adding another', async () => {
			const d = deps({ openArtifact: openArtifactTab });
			openArtifactTab(THREAD, { artifactId: null, path: DOC_PATH, title: 'INDEX.md' });
			await follow('./DECISIONS.md', d);
			let ws = get(threadWorkspaces)[THREAD];
			expect(ws.tabs.map((tab) => tab.path)).toEqual([DOC_PATH, '/workspace/shared/documents/DECISIONS.md']);
			expect(ws.activeTabId).toBe(ws.tabs[1].instanceId);

			setActiveTab(THREAD, ws.tabs[0].instanceId);
			await follow('DECISIONS.md', d);
			ws = get(threadWorkspaces)[THREAD];
			expect(ws.tabs).toHaveLength(2);
			expect(ws.activeTabId).toBe(ws.tabs[1].instanceId);
		});
	});
});
