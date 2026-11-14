import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import AssistantMessage from '$lib/components/AssistantMessage.svelte';
import { renderMarkdown } from '$lib/markdown';
import vectors from '../../../testdata/link-target-vectors.json';
import * as productEvents from '$lib/product_events';
import { RequestRefused } from '$lib/api/docs';
import { installLinkTargetResolver, resolveLinkTarget, type LinkResolverDeps } from './linkTargetResolver';

const ROOTS = [{
	id: 'workspace', label: 'Workspace', kind: 'workspace' as const, path: '/workspace',
}];

function deps(overrides: Partial<LinkResolverDeps> = {}): LinkResolverDeps {
	return {
		activeThreadId: () => 'thread-1',
		projectId: () => 'project-1',
		fetchRoots: vi.fn().mockResolvedValue(ROOTS),
		fetchRevision: vi.fn().mockResolvedValue({
			exists: true, path: '/workspace/shared/linked.md', revision: 'rev-1', size: 1, writable: true, artifact_id: null,
		}),
		openLoading: vi.fn(() => 'loading-1'),
		openArtifact: vi.fn(),
		failLoading: vi.fn(),
		showError: vi.fn(),
		openExternal: vi.fn().mockResolvedValue(undefined),
		isTauri: () => true,
		origin: () => 'http://corsia.test',
		openSettings: vi.fn(),
		openThread: vi.fn(),
		...overrides,
	};
}

let cleanup: (() => void) | undefined;
let component: Record<string, unknown> | undefined;
let host: HTMLElement | undefined;

afterEach(() => {
	cleanup?.();
	cleanup = undefined;
	if (component) unmount(component);
	component = undefined;
	host?.remove();
	host = undefined;
});

describe('resolveLinkTarget', () => {
	it('opens an external http(s) URL in the host browser', async () => {
		const d = deps();
		expect(await resolveLinkTarget('https://example.com/docs', d)).toBe('external');
		expect(d.openExternal).toHaveBeenCalledWith('https://example.com/docs');
	});

	it('opens a relative workspace path through the existing artifact tab store', async () => {
		const d = deps();
		expect(await resolveLinkTarget('shared/linked.md', d)).toBe('document');
		expect(d.fetchRevision).toHaveBeenCalledWith('/workspace/shared/linked.md', 'project-1');
		expect(d.openArtifact).toHaveBeenCalledWith('thread-1', {
			artifactId: null, path: '/workspace/shared/linked.md', title: 'linked.md',
		}, 'loading-1');
	});

	it('preserves the resolved registered artifact identity when opening its path', async () => {
		const d = deps({
			fetchRevision: vi.fn().mockResolvedValue({
				exists: true, path: '/workspace/shared/audit.md', revision: 'rev-1', size: 1, writable: true, artifact_id: 'artifact-1',
			}),
		});
		expect(await resolveLinkTarget('/workspace/shared/audit.md', d)).toBe('document');
		expect(d.openArtifact).toHaveBeenCalledWith('thread-1', {
			artifactId: 'artifact-1', path: '/workspace/shared/audit.md', title: 'audit.md',
		}, 'loading-1');
	});

	it('shows an unresolvable workspace path as unavailable in its own tab, with no toast', async () => {
		const d = deps();
		expect(await resolveLinkTarget('/outside/missing.md', d)).toBe('error');
		expect(d.failLoading).toHaveBeenCalledWith('thread-1', 'loading-1', 'unavailable');
		expect(d.showError).not.toHaveBeenCalled();
		expect(d.openArtifact).not.toHaveBeenCalled();
	});

	it.each([
		['/settings/providers', 'providers', null],
		['/settings/system#restart', 'system', 'restart'],
		['/settings/connected-apps', 'connected-apps', null],
		['http://corsia.test/settings/providers', 'providers', null],
	])('opens Settings in place for the same-origin link %s', async (href, section, anchor) => {
		const d = deps();
		expect(await resolveLinkTarget(href, d)).toBe('settings');
		expect(d.openSettings).toHaveBeenCalledWith(section, anchor);
		expect(d.openExternal).not.toHaveBeenCalled();
		expect(d.fetchRoots).not.toHaveBeenCalled();
	});

	it('leaves other-origin and unknown-section settings links to their usual routes', async () => {
		const d = deps();
		expect(await resolveLinkTarget('https://example.com/settings/system', d)).toBe('external');
		expect(await resolveLinkTarget('/settings/nope', d)).toBe('error');
		expect(d.openSettings).not.toHaveBeenCalled();
	});

	it.each([
		['/thread/abc123', 'abc123'],
		['http://corsia.test/thread/abc-123/', 'abc-123'],
	])('opens the conversation for the same-origin link %s', async (href, id) => {
		const d = deps();
		expect(await resolveLinkTarget(href, d)).toBe('thread');
		expect(d.openThread).toHaveBeenCalledWith(id);
		expect(d.openExternal).not.toHaveBeenCalled();
		expect(d.fetchRoots).not.toHaveBeenCalled();
	});

	it('leaves other-origin and malformed thread links to their usual routes', async () => {
		const d = deps();
		expect(await resolveLinkTarget('https://example.com/thread/abc', d)).toBe('external');
		expect(await resolveLinkTarget('/thread/', d)).toBe('error');
		expect(d.openThread).not.toHaveBeenCalled();
	});

	it.each(['settings/system', './settings/system'])(
		'keeps the relative href %s on the workspace-document route',
		async (href) => {
			const d = deps();
			expect(await resolveLinkTarget(href, d)).toBe('document');
			expect(d.openSettings).not.toHaveBeenCalled();
			expect(d.fetchRevision).toHaveBeenCalledWith('/workspace/settings/system', 'project-1');
		},
	);

	it.each([
		'javascript:alert(1)',
		'vscode://file/Users/x/foo.ts:12',
		'file:///workspace/shared/a.md:12',
		'ftp://files.example.com:21',
	])('does not navigate an unsupported scheme, even one ending like a line (%s), and shows a visible error', async (href) => {
		const d = deps();
		expect(await resolveLinkTarget(href, d)).toBe('error');
		expect(d.fetchRevision).not.toHaveBeenCalled();
		expect(d.showError).toHaveBeenCalledWith('This link uses an unsupported destination');
		expect(d.openExternal).not.toHaveBeenCalled();
	});
});

// One click, stated as literals: the path probed, the tab opened or failed, and the one `link.open`
// event. These are the outcomes a chat link must keep whichever read supplies the roots.
describe('chat link click outcomes', () => {
	const found = (path: string) => ({
		exists: true, path, revision: 'rev-1', size: 1, writable: true, artifact_id: null,
	});
	const gone = { exists: false, path: null, revision: null, size: 0, writable: false, artifact_id: null };
	const OK = { target: 'workspace_document', outcome: 'succeeded' };
	const MISSING = { target: 'workspace_document', outcome: 'failed', reason: 'not_found' };

	const clicks = [
		{ what: 'a relative link', href: 'shared/linked.md', revision: found('/workspace/shared/linked.md'),
			result: 'document', probed: '/workspace/shared/linked.md',
			opened: { artifactId: null, path: '/workspace/shared/linked.md', title: 'linked.md' },
			failed: null as string | null, event: OK },
		{ what: 'an absolute link inside the root', href: '/workspace/shared/linked.md', revision: found('/workspace/shared/linked.md'),
			result: 'document', probed: '/workspace/shared/linked.md',
			opened: { artifactId: null, path: '/workspace/shared/linked.md', title: 'linked.md' },
			failed: null, event: OK },
		{ what: 'a relative link cited at a line', href: 'shared/linked.md:12', revision: found('/workspace/shared/linked.md'),
			result: 'document', probed: '/workspace/shared/linked.md',
			opened: { artifactId: null, path: '/workspace/shared/linked.md', title: 'linked.md' },
			failed: null, event: OK },
		{ what: 'an absolute link cited at a line', href: '/workspace/shared/linked.md:12', revision: found('/workspace/shared/linked.md'),
			result: 'document', probed: '/workspace/shared/linked.md',
			opened: { artifactId: null, path: '/workspace/shared/linked.md', title: 'linked.md' },
			failed: null, event: OK },
		{ what: 'a bare file name cited at a line', href: 'file.md:12', revision: found('/workspace/file.md'),
			result: 'document', probed: '/workspace/file.md', opened: { artifactId: null, path: '/workspace/file.md', title: 'file.md' },
			failed: null, event: OK },
		{ what: 'a link outside every root', href: '/outside/missing.md', revision: found('/outside/missing.md'),
			result: 'error', probed: null, opened: null, failed: 'unavailable', event: MISSING },
		{ what: 'a relative link escaping the root', href: '../escape.md', revision: found('/escape.md'),
			result: 'error', probed: null, opened: null, failed: 'unavailable', event: MISSING },
		{ what: 'a link to a file that is gone', href: 'shared/gone.md', revision: gone,
			result: 'error', probed: '/workspace/shared/gone.md', opened: null, failed: 'unavailable', event: MISSING },
	];

	it.each(clicks)('$what', async ({ href, revision, result, probed, opened, failed, event }) => {
		const events = vi.spyOn(productEvents, 'emitProductEvent').mockImplementation(() => {});
		const d = deps({ fetchRevision: vi.fn().mockResolvedValue(revision) });

		expect(await resolveLinkTarget(href, d)).toBe(result);
		expect(d.fetchRevision.mock.calls).toEqual(probed === null ? [] : [[probed, 'project-1']]);
		expect(d.openArtifact.mock.calls).toEqual(opened === null ? [] : [['thread-1', opened, 'loading-1']]);
		expect(d.failLoading.mock.calls).toEqual(failed === null ? [] : [['thread-1', 'loading-1', failed]]);
		expect(d.showError).not.toHaveBeenCalled();
		expect(events.mock.calls).toEqual([['link', 'open', event]]);
		events.mockRestore();
	});

	it('keeps the same failure outcome when the roots read itself fails', async () => {
		const events = vi.spyOn(productEvents, 'emitProductEvent').mockImplementation(() => {});
		const d = deps({ fetchRoots: vi.fn().mockRejectedValue(new Error('boom')) });

		expect(await resolveLinkTarget('shared/linked.md', d)).toBe('error');
		expect(d.fetchRevision.mock.calls).toEqual([]);
		expect(d.openArtifact.mock.calls).toEqual([]);
		expect(d.failLoading.mock.calls).toEqual([['thread-1', 'loading-1', 'error']]);
		expect(d.showError).not.toHaveBeenCalled();
		expect(events.mock.calls).toEqual([['link', 'open',
			{ target: 'workspace_document', outcome: 'failed', reason: 'open_failed' }]]);
		events.mockRestore();
	});
});

// A project chat's link may be written from the project root or from the workspace root.
describe('chat links written from either root', () => {
	const PROJECT = [{ id: 'planning', label: 'Project', kind: 'workspace' as const, path: '/ws/projects/p' }];
	const WORKSPACE = [{ id: 'ws', label: 'Workspace', kind: 'workspace' as const, path: '/ws' }];
	const roots = vi.fn((projectId: string | null) => Promise.resolve(projectId ? PROJECT : WORKSPACE));
	const revisions = (files: string[], refused: string[] = [], failing: string[] = []) => vi.fn((path: string) => {
		if (refused.includes(path)) return Promise.reject(new RequestRefused('400 {"detail":"Path is outside this scope\'s roots"}', null));
		if (failing.includes(path)) return Promise.reject(new RequestRefused('500 Internal Server Error', null));
		const exists = files.includes(path);
		return Promise.resolve({ exists, path: exists ? path : null, revision: exists ? 'rev' : null, size: 1, writable: true, artifact_id: null });
	});

	it.each([
		{ what: 'project chat, project-relative path', projectId: 'project-1', href: 'shared/a.md', files: ['/ws/projects/p/shared/a.md'],
			probed: ['/ws/projects/p/shared/a.md'], opened: '/ws/projects/p/shared/a.md' },
		{ what: 'project chat, workspace-relative path', projectId: 'project-1', href: 'projects/p/.threads/x/report.md',
			files: ['/ws/projects/p/.threads/x/report.md'],
			probed: ['/ws/projects/p/projects/p/.threads/x/report.md', '/ws/projects/p/.threads/x/report.md'], opened: '/ws/projects/p/.threads/x/report.md' },
		{ what: 'project chat, path under both roots: the project file wins', projectId: 'project-1', href: 'shared/a.md',
			files: ['/ws/projects/p/shared/a.md', '/ws/shared/a.md'], probed: ['/ws/projects/p/shared/a.md'], opened: '/ws/projects/p/shared/a.md' },
		{ what: 'workspace chat, workspace-relative path', projectId: null, href: 'projects/p/shared/a.md', files: ['/ws/projects/p/shared/a.md'],
			probed: ['/ws/projects/p/shared/a.md'], opened: '/ws/projects/p/shared/a.md' },
		{ what: 'workspace chat, project-relative path has no project to try', projectId: null, href: 'shared/a.md', files: ['/ws/projects/p/shared/a.md'],
			probed: ['/ws/shared/a.md'], opened: null },
		{ what: 'project chat, missing under both roots', projectId: 'project-1', href: 'shared/none.md', files: [],
			probed: ['/ws/projects/p/shared/none.md', '/ws/shared/none.md'], opened: null },
		{ what: 'project chat, absolute path is never retried', projectId: 'project-1', href: '/ws/projects/p/none.md', files: [],
			probed: ['/ws/projects/p/none.md'], opened: null },
	])('$what', async ({ projectId, href, files, probed, opened }) => {
		const d = deps({ projectId: () => projectId, fetchRoots: roots, fetchRevision: revisions(files) });
		expect(await resolveLinkTarget(href, d)).toBe(opened ? 'document' : 'error');
		expect(vi.mocked(d.fetchRevision).mock.calls).toEqual(probed.map((path) => [path, projectId]));
		expect(vi.mocked(d.openArtifact).mock.calls).toEqual(opened
			? [['thread-1', { artifactId: null, path: opened, title: opened.split('/').at(-1) }, 'loading-1']] : []);
		expect(vi.mocked(d.failLoading).mock.calls).toEqual(opened ? [] : [['thread-1', 'loading-1', 'unavailable']]);
		expect(d.showError).not.toHaveBeenCalled();
	});

	it('treats a workspace path the project scope refuses as not found, as today', async () => {
		const d = deps({ fetchRoots: roots, fetchRevision: revisions([], ['/ws/shared/b.md']) });
		expect(await resolveLinkTarget('shared/b.md', d)).toBe('error');
		expect(vi.mocked(d.failLoading).mock.calls).toEqual([['thread-1', 'loading-1', 'unavailable']]);
		expect(d.showError).not.toHaveBeenCalled();
		expect(vi.mocked(d.openArtifact)).not.toHaveBeenCalled();
	});

	it('shows a failed workspace-root read as a failed read with Retry, never as no file', async () => {
		const events = vi.spyOn(productEvents, 'emitProductEvent').mockImplementation(() => {});
		try {
			const d = deps({ fetchRoots: roots, fetchRevision: revisions([], [], ['/ws/shared/b.md']) });
			expect(await resolveLinkTarget('shared/b.md', d)).toBe('error');
			expect(vi.mocked(d.failLoading).mock.calls).toEqual([['thread-1', 'loading-1', 'error']]);
			expect(events.mock.calls).toEqual([['link', 'open', { target: 'workspace_document', outcome: 'failed', reason: 'open_failed' }]]);
			expect(d.showError).not.toHaveBeenCalled();
		} finally {
			events.mockRestore();
		}
	});
});

// The roots and revision reads can wait seconds on the database; the press shows its tab first.
describe('a pressed link shows its tab before it resolves', () => {
	it('opens the loading tab before any read, then settles that same tab', async () => {
		let releaseRoots: (roots: typeof ROOTS) => void = () => {};
		const d = deps({ fetchRoots: vi.fn(() => new Promise<typeof ROOTS>((resolve) => { releaseRoots = resolve; })) });
		const routed = resolveLinkTarget('shared/linked.md', d);
		await Promise.resolve();
		expect(d.openLoading).toHaveBeenCalledWith('thread-1', { href: 'shared/linked.md', projectId: 'project-1' }, 'linked.md');
		expect(d.openArtifact).not.toHaveBeenCalled();
		releaseRoots(ROOTS);
		expect(await routed).toBe('document');
		expect(vi.mocked(d.openArtifact).mock.calls[0][2]).toBe('loading-1');
		expect(d.failLoading).not.toHaveBeenCalled();
	});

	it('leaves the tab in its unavailable state when the link names no file, with no toast over it', async () => {
		const d = deps({ fetchRevision: vi.fn().mockResolvedValue({ exists: false, path: null, revision: null, size: 0, writable: false, artifact_id: null }) });
		expect(await resolveLinkTarget('shared/none.md', d)).toBe('error');
		expect(d.failLoading).toHaveBeenCalledWith('thread-1', 'loading-1', 'unavailable');
		expect(d.showError).not.toHaveBeenCalled();
	});

	it('leaves the tab in its error state when a read fails, with no toast over it', async () => {
		const d = deps({ fetchRevision: vi.fn().mockRejectedValue(new Error('500')) });
		expect(await resolveLinkTarget('shared/linked.md', d)).toBe('error');
		expect(d.failLoading).toHaveBeenCalledWith('thread-1', 'loading-1', 'error');
		expect(d.showError).not.toHaveBeenCalled();
	});

	it('starts nothing new when the same link is still loading', async () => {
		const d = deps({ openLoading: vi.fn(() => null) });
		expect(await resolveLinkTarget('shared/linked.md', d)).toBe('document');
		expect(d.fetchRoots).not.toHaveBeenCalled();
		expect(d.openArtifact).not.toHaveBeenCalled();
		expect(d.showError).not.toHaveBeenCalled();
	});
});

describe('link.open product event', () => {
	const SENSITIVE = /example\.com|linked|missing|javascript|Couldn't|boom|thread-1|project-1/i;

	async function route(href: string, overrides: Partial<LinkResolverDeps> = {}) {
		const spy = vi.spyOn(productEvents, 'emitProductEvent').mockImplementation(() => {});
		await resolveLinkTarget(href, deps(overrides));
		const calls = spy.mock.calls;
		spy.mockRestore();
		return calls;
	}

	it.each([
		['external success', 'https://example.com/docs', {}, { target: 'external', outcome: 'succeeded' }],
		['external failure', 'https://example.com/docs', { openExternal: vi.fn().mockRejectedValue(new Error('boom')) },
			{ target: 'external', outcome: 'failed', reason: 'open_failed' }],
		['unsupported scheme', 'javascript:alert(1)', {},
			{ target: 'unsupported', outcome: 'failed', reason: 'unsupported_scheme' }],
		['no active thread', 'shared/linked.md', { activeThreadId: () => null },
			{ target: 'workspace_document', outcome: 'failed', reason: 'no_active_thread' }],
		['path outside the workspace', '/outside/missing.md', {},
			{ target: 'workspace_document', outcome: 'failed', reason: 'not_found' }],
		['missing revision', 'shared/linked.md', { fetchRevision: vi.fn().mockResolvedValue({ exists: false }) },
			{ target: 'workspace_document', outcome: 'failed', reason: 'not_found' }],
		['caught document failure', 'shared/linked.md', { fetchRoots: vi.fn().mockRejectedValue(new Error('boom')) },
			{ target: 'workspace_document', outcome: 'failed', reason: 'open_failed' }],
		['document success', 'shared/linked.md', {}, { target: 'workspace_document', outcome: 'succeeded' }],
	])('emits exactly one exact event for %s', async (_name, href, overrides, payload) => {
		const calls = await route(href, overrides as Partial<LinkResolverDeps>);
		expect(calls).toEqual([['link', 'open', payload]]);
		expect(JSON.stringify(calls)).not.toMatch(SENSITIVE);
	});

	it('emits nothing for a press on a link still loading: the first press reports the outcome', async () => {
		expect(await route('shared/linked.md', { openLoading: vi.fn(() => null) })).toEqual([]);
	});
});

describe('delegated rendered-markdown listener', () => {
	it('opens a chat settings link in place instead of a new tab', () => {
		const d = deps({ isTauri: () => false });
		host = document.createElement('section');
		host.className = 'md-content';
		host.innerHTML = renderMarkdown('[System](/settings/system)');
		document.body.appendChild(host);
		cleanup = installLinkTargetResolver(document, () => d);
		const click = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 });
		host.querySelector('a')!.dispatchEvent(click);
		expect(click.defaultPrevented).toBe(true);
		expect(d.openSettings).toHaveBeenCalledWith('system', null);
	});


	it('opens a chat thread link in place instead of a new tab', () => {
		const d = deps({ isTauri: () => false });
		host = document.createElement('section');
		host.className = 'md-content';
		host.innerHTML = renderMarkdown('[Your conductor](/thread/abc123)');
		document.body.appendChild(host);
		cleanup = installLinkTargetResolver(document, () => d);
		const click = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 });
		host.querySelector('a')!.dispatchEvent(click);
		expect(click.defaultPrevented).toBe(true);
		expect(d.openThread).toHaveBeenCalledWith('abc123');
	});

	function installSurface(markdown: string, surface: string, d: LinkResolverDeps) {
		host = document.createElement('section');
		host.className = 'md-content';
		host.dataset.surface = surface;
		host.innerHTML = renderMarkdown(markdown);
		document.body.appendChild(host);
		cleanup = installLinkTargetResolver(document, () => d);
		return host.querySelector<HTMLAnchorElement>('a')!;
	}

	it.each([
		['target.md', '/workspace/shared/conductor/x.md', '/workspace/shared/conductor/target.md'],
		['../target.md', '/workspace/shared/conductor/x.md', '/workspace/shared/target.md'],
		['/workspace/shared/target.md', '/workspace/shared/conductor/x.md', '/workspace/shared/target.md'],
		['shared/target.md', '/workspace/shared/conductor/x.md', '/workspace/shared/conductor/shared/target.md'],
		['shared/target.md', null, '/workspace/shared/target.md'],
	])('routes %s from document path %s', async (href, docPath, expected) => {
		const d = deps();
		const anchor = installSurface(`[Open](${href})`, 'comment', d);
		if (docPath) host!.dataset.docPath = docPath;
		anchor.click();
		await vi.waitFor(() => expect(d.fetchRevision).toHaveBeenCalledWith(expected, 'project-1'));
	});

	it('routes an AssistantMessage chat link through the one root listener', async () => {
		const d = deps();
		host = document.createElement('section');
		document.body.appendChild(host);
		component = mount(AssistantMessage, {
			target: host,
			props: {
				msg: { id: 'message-1', content: '[Open brief](shared/linked.md)', created_at: '2026-09-28T00:00:00Z' },
				expandedTools: new Set<string>(), expandedReasoning: new Set<string>(),
				onToggleToolBlock: () => {}, onToggleReasoning: () => {},
			},
		});
		flushSync();
		cleanup = installLinkTargetResolver(document, () => d);
		host.querySelector<HTMLAnchorElement>('a')!.click();
		await vi.waitFor(() => expect(d.openArtifact).toHaveBeenCalledOnce());
	});

	it.each(['/workspace/shared/linked.md:143', 'shared/linked.md:143'])('opens the document a chat link cites at a line (%s)', async (href) => {
		const d = deps();
		host = document.createElement('section');
		document.body.appendChild(host);
		component = mount(AssistantMessage, {
			target: host,
			props: {
				msg: { id: 'message-1', content: `[Readout](${href})`, created_at: '2026-09-28T00:00:00Z' },
				expandedTools: new Set<string>(), expandedReasoning: new Set<string>(),
				onToggleToolBlock: () => {}, onToggleReasoning: () => {},
			},
		});
		flushSync();
		cleanup = installLinkTargetResolver(document, () => d);
		host.querySelector<HTMLAnchorElement>('a')!.click();
		await vi.waitFor(() => expect(d.openArtifact).toHaveBeenCalledOnce());
		expect(d.fetchRevision).toHaveBeenCalledWith('/workspace/shared/linked.md', 'project-1');
		expect(d.showError).not.toHaveBeenCalled();
	});

	it('routes an artifact document table link through the same root listener', async () => {
		const d = deps();
		const anchor = installSurface('| Doc |\n| --- |\n| [Open](/workspace/shared/linked.md) |', 'artifact-document', d);
		anchor.click();
		await vi.waitFor(() => expect(d.openArtifact).toHaveBeenCalledOnce());
	});

	it('observes a normal web external-link action without intercepting navigation', () => {
		const d = deps({ isTauri: () => false });
		const spy = vi.spyOn(productEvents, 'emitProductEvent').mockImplementation(() => {});
		const anchor = installSurface('[Open](https://example.com/docs)', 'chat', d);
		const click = new MouseEvent('click', { bubbles: true, cancelable: true });

		anchor.dispatchEvent(click);

		expect(click.defaultPrevented).toBe(false);
		expect(d.openExternal).not.toHaveBeenCalled();
		expect(spy.mock.calls).toEqual([['link', 'open', { target: 'external', outcome: 'succeeded' }]]);
		spy.mockRestore();
	});

	it('routes a chat OAuth link through Tauri', async () => {
		const d = deps({ isTauri: () => true });
		const url = 'https://accounts.google.com/o/oauth2/v2/auth?client_id=client&redirect_uri=http%3A%2F%2Flocalhost%3A3000&response_type=code';
		document.documentElement.dataset.shell = 'tauri';
		const anchor = installSurface(`[Continue](${url})`, 'chat', d);
		const click = new MouseEvent('click', { bubbles: true, cancelable: true });

		anchor.dispatchEvent(click);

		await vi.waitFor(() => expect(d.openExternal).toHaveBeenCalledWith(url));
		expect(click.defaultPrevented).toBe(true);
	});

	it('preserves and opens the complete Google approval link in an assistant message', async () => {
		const d = deps();
		const url = 'https://accounts.google.com/o/oauth2/v2/auth?client_id=example.apps.googleusercontent.com&redirect_uri=http%3A%2F%2F127.0.0.1%3A1956%2Fapi%2Fgoogle%2Foauth%2Fcallback&response_type=code&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fgmail.modify+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fgmail.send+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcalendar+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdocuments+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fspreadsheets+openid&state=example&code_challenge=example&code_challenge_method=S256&access_type=offline&prompt=consent';
		host = document.createElement('section');
		document.body.appendChild(host);
		component = mount(AssistantMessage, {
			target: host,
			props: {
				msg: { id: 'message-1', content: `Ready for your click: [Open this link](${url}). It opens in your usual browser.`, created_at: '2026-10-27T17:11:00Z' },
				expandedTools: new Set<string>(), expandedReasoning: new Set<string>(),
				onToggleToolBlock: () => {}, onToggleReasoning: () => {},
			},
		});
		flushSync();
		cleanup = installLinkTargetResolver(document, () => d);
		const anchor = host.querySelector<HTMLAnchorElement>('a')!;
		expect(anchor.getAttribute('href')).toBe(url);
		anchor.click();
		await vi.waitFor(() => expect(d.openExternal).toHaveBeenCalledWith(url));
	});

	it.each([
		['modifier', 'click', { metaKey: true }],
		['middle', 'auxclick', { button: 1 }],
	] as const)('observes a native %s external-link action without intercepting navigation', (_name, eventName, init) => {
		const d = deps({ isTauri: () => false });
		const spy = vi.spyOn(productEvents, 'emitProductEvent').mockImplementation(() => {});
		const anchor = installSurface('[Open](https://example.com/docs)', 'chat', d);
		const click = new MouseEvent(eventName, { bubbles: true, cancelable: true, ...init });

		anchor.dispatchEvent(click);

		expect(click.defaultPrevented).toBe(false);
		expect(d.openExternal).not.toHaveBeenCalled();
		expect(spy.mock.calls).toEqual([['link', 'open', { target: 'external', outcome: 'succeeded' }]]);
		spy.mockRestore();
	});

	it('observes a native modified external action in Tauri without taking over navigation', () => {
		const d = deps({ isTauri: () => true });
		const spy = vi.spyOn(productEvents, 'emitProductEvent').mockImplementation(() => {});
		const anchor = installSurface('[Open](https://example.com/docs)', 'chat', d);
		const click = new MouseEvent('click', { bubbles: true, cancelable: true, metaKey: true });

		anchor.dispatchEvent(click);

		expect(click.defaultPrevented).toBe(false);
		expect(d.openExternal).not.toHaveBeenCalled();
		expect(spy.mock.calls).toEqual([['link', 'open', { target: 'external', outcome: 'succeeded' }]]);
		spy.mockRestore();
	});

	it('does not intercept a programmatic relative download link or show a toast', () => {
		const d = deps();
		host = document.createElement('section');
		const anchor = document.createElement('a');
		anchor.href = '/api/uploads/file';
		anchor.target = '_blank';
		anchor.setAttribute('download', 'file.md');
		host.appendChild(anchor);
		document.body.appendChild(host);
		cleanup = installLinkTargetResolver(document, () => d);
		const click = new MouseEvent('click', { bubbles: true, cancelable: true });
		anchor.dispatchEvent(click);
		expect(click.defaultPrevented).toBe(false);
		expect(d.showError).not.toHaveBeenCalled();
	});

	it('does not intercept a relative link outside rendered markdown', () => {
		const d = deps();
		host = document.createElement('section');
		const anchor = document.createElement('a');
		anchor.href = '/api/uploads/file';
		anchor.target = '_blank';
		host.appendChild(anchor);
		document.body.appendChild(host);
		cleanup = installLinkTargetResolver(document, () => d);
		const click = new MouseEvent('click', { bubbles: true, cancelable: true });
		anchor.dispatchEvent(click);
		expect(click.defaultPrevented).toBe(false);
		expect(d.showError).not.toHaveBeenCalled();
	});

	it('does not intercept a modifier-click on a markdown link', () => {
		const d = deps();
		const anchor = installSurface('[Open brief](shared/linked.md)', 'chat', d);
		const click = new MouseEvent('click', { bubbles: true, cancelable: true, metaKey: true });
		anchor.dispatchEvent(click);
		expect(click.defaultPrevented).toBe(false);
		expect(d.openArtifact).not.toHaveBeenCalled();
	});
});

// The server turns a message's text into reference cards with a Python port of this path
// logic (backend/registry/path_cards.py); both suites read the same vectors.
describe('workspace path vectors shared with the server', () => {
	const roots = vectors.roots.map((path) => ({ ...ROOTS[0], id: path, path }));

	it.each(vectors.cases)('resolves $href', async ({ href, expected }) => {
		const d = deps({ fetchRoots: vi.fn().mockResolvedValue(roots) });
		await resolveLinkTarget(href, d);
		if (expected === null) expect(d.fetchRevision).not.toHaveBeenCalled();
		else expect(d.fetchRevision).toHaveBeenCalledWith(expected, 'project-1');
	});
});
