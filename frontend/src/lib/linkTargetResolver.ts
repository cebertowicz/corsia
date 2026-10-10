import { get } from 'svelte/store';
import { openUrl } from '@tauri-apps/plugin-opener';
import { fetchRevision, fetchTree, type DocRevision, type TreeRoot } from '$lib/api/docs';
import { openArtifactTab } from '$lib/stores/artifactWorkspace';
import { openSubagentRequest } from '$lib/stores/subagents';
import { activeThreadId } from '$lib/stores/threads';
import { emitProductEvent } from '$lib/product_events';
import { toasts } from '$lib/stores/toast';
import { uiScope } from '$lib/stores/uiScope';
import { SETTINGS_SECTIONS, requestSettingsSection, type SettingsSection } from '$lib/stores/settingsNavigation';

export interface LinkResolverDeps {
	activeThreadId: () => string | null;
	projectId: () => string | null;
	fetchTree: (projectId: string | null) => Promise<TreeRoot[]>;
	fetchRevision: (path: string, projectId: string | null) => Promise<DocRevision>;
	openArtifact: (threadId: string, input: { artifactId: string | null; path: string; title: string }) => void;
	showError: (message: string) => void;
	openExternal: (href: string) => Promise<void>;
	isTauri: () => boolean;
	origin: () => string;
	openSettings: (section: SettingsSection, anchor: string | null) => void;
	openThread: (threadId: string) => void;
}

function targetPath(href: string): string | null {
	const withoutQuery = href.split(/[?#]/, 1)[0];
	if (!withoutQuery) return null;
	try {
		return decodeURIComponent(withoutQuery);
	} catch {
		return null;
	}
}

function normalizePath(root: string, target: string): string | null {
	const rootParts = root.split('/').filter(Boolean);
	const parts = target.startsWith('/') ? [] : [...rootParts];
	for (const part of target.split('/')) {
		if (!part || part === '.') continue;
		if (part === '..') {
			if (parts.length <= rootParts.length) return null;
			parts.pop();
			continue;
		}
		parts.push(part);
	}
	const resolved = `/${parts.join('/')}`;
	return resolved === root || resolved.startsWith(`${root}/`) ? resolved : null;
}

function workspacePath(href: string, roots: TreeRoot[]): string | null {
	const target = targetPath(href);
	if (!target) return null;
	const workspaces = roots.filter((root) => root.kind === 'workspace');
	if (target.startsWith('/')) {
		const root = workspaces.find((candidate) => target === candidate.path || target.startsWith(`${candidate.path}/`));
		return root ? normalizePath(root.path, target) : null;
	}
	return workspaces[0] ? normalizePath(workspaces[0].path, target) : null;
}

function externalHref(href: string): string | null {
	try {
		const url = new URL(href);
		return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
	} catch {
		return null;
	}
}

/** A same-origin `/settings/<section>[#anchor]` link, as an agent writes it in chat. */
function settingsLink(href: string, origin: string): { section: SettingsSection; anchor: string | null } | null {
	if (!href.startsWith('/settings/') && !href.startsWith(`${origin}/settings/`)) return null;
	let url: URL;
	try {
		url = new URL(href, origin);
	} catch {
		return null;
	}
	if (url.origin !== origin) return null;
	const match = /^\/settings\/([a-z]+)\/?$/.exec(url.pathname);
	const section = SETTINGS_SECTIONS.find((id) => id === match?.[1]);
	if (!section) return null;
	return { section, anchor: /^#[\w-]+$/.test(url.hash) ? url.hash.slice(1) : null };
}

/** A same-origin `/thread/<id>` link, as an agent writes it to hand the person to a conversation. */
function threadLink(href: string, origin: string): string | null {
	if (!href.startsWith('/thread/') && !href.startsWith(`${origin}/thread/`)) return null;
	let url: URL;
	try {
		url = new URL(href, origin);
	} catch {
		return null;
	}
	if (url.origin !== origin) return null;
	return /^\/thread\/([\w-]+)\/?$/.exec(url.pathname)?.[1] ?? null;
}

function hasScheme(href: string): boolean {
	return /^[a-z][a-z\d+.-]*:/i.test(href);
}

function titleFor(path: string): string {
	return path.split('/').filter(Boolean).at(-1) ?? path;
}

/** Opens an external link in the correct host without changing web navigation. */
export async function openExternalUrl(href: string): Promise<void> {
	if (document.documentElement.dataset.shell === 'tauri') {
		await openUrl(href);
		return;
	}
	window.open(href, '_blank', 'noopener,noreferrer');
}

type LinkOpenTarget = 'external' | 'workspace_document' | 'settings' | 'thread' | 'unsupported';
type LinkOpenFailure = 'open_failed' | 'unsupported_scheme' | 'no_active_thread' | 'not_found';
interface LinkRouted {
	result: 'external' | 'document' | 'settings' | 'thread' | 'error';
	target: LinkOpenTarget;
	reason?: LinkOpenFailure;
}

function failed(target: LinkOpenTarget, reason: LinkOpenFailure): LinkRouted {
	return { result: 'error', target, reason };
}

async function routeLinkTarget(href: string, deps: LinkResolverDeps): Promise<LinkRouted> {
	const settings = settingsLink(href, deps.origin());
	if (settings) {
		deps.openSettings(settings.section, settings.anchor);
		return { result: 'settings', target: 'settings' };
	}
	const thread = threadLink(href, deps.origin());
	if (thread) {
		deps.openThread(thread);
		return { result: 'thread', target: 'thread' };
	}
	const external = externalHref(href);
	if (external) {
		try {
			await deps.openExternal(external);
			return { result: 'external', target: 'external' };
		} catch {
			deps.showError("Couldn't open the external link");
			return failed('external', 'open_failed');
		}
	}
	if (hasScheme(href)) {
		deps.showError('This link uses an unsupported destination');
		return failed('unsupported', 'unsupported_scheme');
	}

	const threadId = deps.activeThreadId();
	if (!threadId) {
		deps.showError('Open a conversation before opening a linked document');
		return failed('workspace_document', 'no_active_thread');
	}
	try {
		const projectId = deps.projectId();
		const path = workspacePath(href, await deps.fetchTree(projectId));
		if (!path) {
			deps.showError("Couldn't find the linked workspace document");
			return failed('workspace_document', 'not_found');
		}
		const revision = await deps.fetchRevision(path, projectId);
		if (!revision.exists) {
			deps.showError("Couldn't find the linked workspace document");
			return failed('workspace_document', 'not_found');
		}
		deps.openArtifact(threadId, {
			artifactId: revision.artifact_id,
			path: revision.path ?? path,
			title: titleFor(revision.path ?? path),
			...(revision.media_type ? { isImage: true } : {}),
		});
		return { result: 'document', target: 'workspace_document' };
	} catch {
		deps.showError("Couldn't open the linked workspace document");
		return failed('workspace_document', 'open_failed');
	}
}

/**
 * Routes one rendered-markdown target to an external browser or the artifact leaf.
 * Reports one `link.open` event per call: target class, outcome and a bounded
 * failure reason only; never the href, a path, a title or error text.
 */
export async function resolveLinkTarget(href: string, deps: LinkResolverDeps): Promise<'external' | 'document' | 'settings' | 'thread' | 'error'> {
	const { result, target, reason } = await routeLinkTarget(href, deps);
	emitProductEvent('link', 'open', reason
		? { target, outcome: 'failed', reason }
		: { target, outcome: 'succeeded' });
	return result;
}

function defaultDeps(): LinkResolverDeps {
	return {
		activeThreadId: () => get(activeThreadId),
		projectId: () => get(uiScope)?.id ?? null,
		fetchTree,
		fetchRevision: (path, projectId) => fetchRevision(path, projectId),
		openArtifact: openArtifactTab,
		showError: (message) => toasts.show(message, 'error'),
		openExternal: openExternalUrl,
		isTauri: () => document.documentElement.dataset.shell === 'tauri',
		origin: () => window.location.origin,
		openSettings: requestSettingsSection,
		// The page serves this request through its one thread selector (source `link`).
		openThread: (threadId) => openSubagentRequest.set(threadId),
	};
}

function preservesNativeLinkBehavior(event: MouseEvent, anchor: HTMLAnchorElement, href: string): boolean {
	return event.defaultPrevented
		|| event.button !== 0
		|| event.metaKey
		|| event.ctrlKey
		|| event.shiftKey
		|| event.altKey
		|| anchor.hasAttribute('download')
		|| href.startsWith('#');
}

/** Installs delegated primary and auxiliary click listeners for rendered markdown and external links. */
export function installLinkTargetResolver(root: Document = document, deps = defaultDeps): () => void {
	const handler = (event: MouseEvent) => {
		const source = event.target instanceof Element ? event.target : null;
		const anchor = source?.closest<HTMLAnchorElement>('a[href]');
		const href = anchor?.getAttribute('href');
		if (!anchor || !href) return;
		const external = externalHref(href);
		if (!preservesNativeLinkBehavior(event, anchor, href) && anchor.closest('.md-content')
			&& (settingsLink(href, deps().origin()) || threadLink(href, deps().origin()))) {
			event.preventDefault();
			void resolveLinkTarget(href, deps());
			return;
		}
		if (preservesNativeLinkBehavior(event, anchor, href)) {
			if (external
				&& !event.defaultPrevented
				&& (event.button === 1 || event.metaKey || event.ctrlKey || event.shiftKey)
				&& !event.altKey
				&& !anchor.hasAttribute('download')
				&& !href.startsWith('#')) {
				// A modified or middle click is still an external open; observe it
				// without taking ownership of the browser's native navigation.
				emitProductEvent('link', 'open', { target: 'external', outcome: 'succeeded' });
			}
			return;
		}

		const currentDeps = deps();
		if (external) {
			if (!currentDeps.isTauri()) {
				// The browser owns this navigation; observing it must not intercept it.
				emitProductEvent('link', 'open', { target: 'external', outcome: 'succeeded' });
				return;
			}
		} else if (!anchor.closest('.md-content')) {
			return;
		}
		event.preventDefault();
		void resolveLinkTarget(href, currentDeps);
	};
	root.addEventListener('click', handler);
	root.addEventListener('auxclick', handler);
	return () => {
		root.removeEventListener('click', handler);
		root.removeEventListener('auxclick', handler);
	};
}
