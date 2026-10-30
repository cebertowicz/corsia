import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import AssistantMessage from '$lib/components/AssistantMessage.svelte';
import { renderMarkdown } from '$lib/markdown';
import vectors from '../../../testdata/link-target-vectors.json';
import * as productEvents from '$lib/product_events';
import { installLinkTargetResolver, resolveLinkTarget, type LinkResolverDeps } from './linkTargetResolver';

const ROOTS = [{
	id: 'workspace', label: 'Workspace', kind: 'workspace' as const, path: '/workspace', nodes: [], capped: false,
}];

function deps(overrides: Partial<LinkResolverDeps> = {}): LinkResolverDeps {
	return {
		activeThreadId: () => 'thread-1',
		projectId: () => 'project-1',
		fetchTree: vi.fn().mockResolvedValue(ROOTS),
		fetchRevision: vi.fn().mockResolvedValue({
			exists: true, path: '/workspace/shared/linked.md', revision: 'rev-1', size: 1, writable: true, artifact_id: null,
		}),
		openArtifact: vi.fn(),
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
		});
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
		});
	});

	it('shows a visible error for an unresolvable workspace path', async () => {
		const d = deps();
		expect(await resolveLinkTarget('/outside/missing.md', d)).toBe('error');
		expect(d.showError).toHaveBeenCalledWith("Couldn't find the linked workspace document");
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
		expect(d.fetchTree).not.toHaveBeenCalled();
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
		expect(d.fetchTree).not.toHaveBeenCalled();
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

	it('does not navigate unsafe schemes and shows a visible error', async () => {
		const d = deps();
		expect(await resolveLinkTarget('javascript:alert(1)', d)).toBe('error');
		expect(d.showError).toHaveBeenCalledWith('This link uses an unsupported destination');
		expect(d.openExternal).not.toHaveBeenCalled();
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
		['caught document failure', 'shared/linked.md', { fetchTree: vi.fn().mockRejectedValue(new Error('boom')) },
			{ target: 'workspace_document', outcome: 'failed', reason: 'open_failed' }],
		['document success', 'shared/linked.md', {}, { target: 'workspace_document', outcome: 'succeeded' }],
	])('emits exactly one exact event for %s', async (_name, href, overrides, payload) => {
		const calls = await route(href, overrides as Partial<LinkResolverDeps>);
		expect(calls).toEqual([['link', 'open', payload]]);
		expect(JSON.stringify(calls)).not.toMatch(SENSITIVE);
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
		const d = deps({ fetchTree: vi.fn().mockResolvedValue(roots) });
		await resolveLinkTarget(href, d);
		if (expected === null) expect(d.fetchRevision).not.toHaveBeenCalled();
		else expect(d.fetchRevision).toHaveBeenCalledWith(expected, 'project-1');
	});
});
