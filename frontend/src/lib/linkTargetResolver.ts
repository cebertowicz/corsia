import { get } from 'svelte/store';
import { openUrl } from '@tauri-apps/plugin-opener';
import { fetchRoots, fetchRevision } from '$lib/api/docs';
import { linkedFile, targetPath, type LinkedFileReads } from '$lib/linkedFile';
import { markTabStatus, openLinkLoadingTab, settleLinkLoadingTab } from '$lib/stores/artifactWorkspace';
import type { ArtifactLink } from '$lib/artifactTabTypes';
import { defaultTabTitle } from '$lib/artifactTabTitle';
import { openSubagentRequest } from '$lib/stores/subagents';
import { activeThreadId } from '$lib/stores/threads';
import { emitProductEvent } from '$lib/product_events';
import { toasts } from '$lib/stores/toast';
import { documentLinkTarget, withoutLineSuffix } from '$lib/cm/markdown-link';
import { uiScope } from '$lib/stores/uiScope';
import { SETTINGS_SECTIONS, requestSettingsSection, type SettingsSection } from '$lib/stores/settingsNavigation';

export interface LinkResolverDeps {
	activeThreadId: () => string | null;
	projectId: () => string | null;
	fetchRoots: LinkedFileReads['fetchRoots'];
	fetchRevision: LinkedFileReads['fetchRevision'];
	/** Shows the pressed link's tab loading at once; null when the same link is already loading (its tab is selected). */
	openLoading: (threadId: string, link: ArtifactLink, title: string) => string | null;
	/** The loading tab becomes the resolved document's tab. */
	openArtifact: (threadId: string, input: { artifactId: string | null; path: string; title: string }, loadingTab: string) => void;
	/** The loading tab shows the unavailable or error state. */
	failLoading: (threadId: string, loadingTab: string, status: 'unavailable' | 'error') => void;
	showError: (message: string) => void;
	openExternal: (href: string) => Promise<void>;
	isTauri: () => boolean;
	origin: () => string;
	openSettings: (section: SettingsSection, anchor: string | null) => void;
	openThread: (threadId: string) => void;
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
	const match = /^\/settings\/([a-z-]+)\/?$/.exec(url.pathname);
	const section = SETTINGS_SECTIONS.find((id) => id === match?.[1]);
	if (!section) return null;
	const anchor = /^#[\w-]+$/.test(url.hash) ? url.hash.slice(1) : null;
	return { section, anchor };
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
	const scheme = /^([a-z][a-z\d+.-]*):/i.exec(href)?.[1];
	if (!scheme) return false;
	// `linked.md:143` (a file cited at a line) reads as the scheme `linked.md`; web and editor schemes have no dot.
	const path = href.split(/[?#]/, 1)[0];
	return !scheme.includes('.') || withoutLineSuffix(path) === path;
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
		deps.showError('Open a chat before opening a linked document');
		return failed('workspace_document', 'no_active_thread');
	}
	const link = { href, projectId: deps.projectId() };
	// The press shows its tab loading before any request: the roots and revision reads can wait on the database.
	const loadingTab = deps.openLoading(threadId, link, defaultTabTitle(targetPath(href) ?? href));
	if (loadingTab === null) return { result: 'document', target: 'workspace_document' };
	return openLinkedDocument(link, threadId, loadingTab, deps);
}

// A failure lands in the pressed link's own tab, which states the reason; a toast would repeat it, and on a phone cover it.
async function openLinkedDocument(link: ArtifactLink, threadId: string, loadingTab: string, deps: LinkResolverDeps): Promise<LinkRouted> {
	try {
		const linked = await linkedFile(link.href, deps, link.projectId);
		if (!linked) {
			deps.failLoading(threadId, loadingTab, 'unavailable');
			return failed('workspace_document', 'not_found');
		}
		const { path, revision } = linked;
		deps.openArtifact(threadId, {
			artifactId: revision.artifact_id,
			path: revision.path ?? path,
			title: defaultTabTitle(revision.path ?? path),
		}, loadingTab);
		return { result: 'document', target: 'workspace_document' };
	} catch {
		deps.failLoading(threadId, loadingTab, 'error');
		return failed('workspace_document', 'open_failed');
	}
}

/** Retry on a link tab that failed to open: the same tab loads again and resolves the same link. */
export async function retryLinkTab(threadId: string, loadingTab: string, link: ArtifactLink): Promise<void> {
	const deps = defaultDeps();
	markTabStatus(threadId, loadingTab, 'loading');
	const { target, reason } = await openLinkedDocument(link, threadId, loadingTab, deps);
	emitProductEvent('link', 'open', reason ? { target, outcome: 'failed', reason } : { target, outcome: 'succeeded' });
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
		fetchRoots,
		fetchRevision: (path, projectId) => fetchRevision(path, projectId),
		openLoading: openLinkLoadingTab,
		openArtifact: (threadId, input, loadingTab) => settleLinkLoadingTab(threadId, loadingTab, input),
		failLoading: markTabStatus,
		showError: (message) => toasts.show(message, 'error'),
		openExternal: openExternalUrl,
		isTauri: () => document.documentElement.dataset.shell === 'tauri',
		origin: () => window.location.origin,
		openSettings: requestSettingsSection,
		// The page serves this request through its one thread selector (source `link`).
		openThread: (threadId) => openSubagentRequest.set(threadId),
	};
}

/** Opens a link written in a document: a relative target resolves from that document's folder, not the workspace root. */
export function openDocumentLink(href: string, docPath: string | null) {
	return resolveLinkTarget(documentLinkTarget(docPath, href), defaultDeps());
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
		// A comment on a document resolves its links as that document's body does.
		const docPath = anchor.closest<HTMLElement>('[data-doc-path]')?.dataset.docPath;
		void resolveLinkTarget(docPath ? documentLinkTarget(docPath, href) : href, currentDeps);
	};
	root.addEventListener('click', handler);
	root.addEventListener('auxclick', handler);
	return () => {
		root.removeEventListener('click', handler);
		root.removeEventListener('auxclick', handler);
	};
}
