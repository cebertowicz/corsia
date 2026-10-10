import { flushSync, mount, unmount, type ComponentProps } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import HomeShell from '$lib/components/HomeShell.svelte';
import InputBar from '$lib/components/InputBar.svelte';
import { toasts } from '$lib/stores/toast';
import { activeThreadId } from '$lib/stores/threads';
import { openArtifactTab, threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { paneOpen } from '$lib/stores/paneOpen';

let host: HTMLElement;
let realRO: typeof ResizeObserver;
let shell: Record<string, unknown> | null = null;
let composers: Record<string, unknown>[] = [];

const noop = () => {};
type ShellProps = ComponentProps<typeof HomeShell>;

function shellProps(activeView: string): ShellProps {
	return {
		activeView,
		sidebarOpen: false,
		chatPanelRef: undefined,
		conductorThreadId: null,
		systemStatus: null,
		isDraggingOver: false,
		paneConfig: { label: 'Workspace', sections: [] },
		onGoWorkspace: noop,
		onNewChat: noop, onConductor: noop, onDocuments: noop, onAttachProject: noop,
		onSelectThread: noop, onDeleteThread: noop, onArchiveThread: noop,
		onOpenSettings: noop, onToggleSidebar: noop, onTogglePane: noop,
		onOpenLightbox: noop, onAutoCreateThread: noop,
		onPickProject: noop, onPickProjectAndClosePane: noop,
		onCloseSettings: noop,
	} as unknown as ShellProps;
}

function mountComposer(target: HTMLElement) {
	const composer = mount(InputBar, {
		target,
		props: {
			onSend: noop,
		},
	});
	composers.push(composer);
	flushSync();
	return composer;
}

function dropFile(target = host.querySelector<HTMLElement>('.app')!): FileList {
	const files = [new File(['notes'], 'notes.txt', { type: 'text/plain' })] as unknown as FileList;
	const event = new Event('drop', { bubbles: true, cancelable: true });
	Object.defineProperty(event, 'dataTransfer', { value: { files } });
	target.dispatchEvent(event);
	return files;
}

function captureFiles(input: HTMLInputElement) {
	let received: FileList | null = null;
	Object.defineProperty(input, 'files', {
		configurable: true,
		get: () => received,
		set: (files: FileList) => { received = files; },
	});
	return () => received;
}

beforeEach(() => {
	realRO = globalThis.ResizeObserver;
	globalThis.ResizeObserver = class {
		observe() {}
		unobserve() {}
		disconnect() {}
	} as unknown as typeof ResizeObserver;
	activeThreadId.set(null);
	threadWorkspaces.set({});
	paneOpen.set(false);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	for (const composer of composers.reverse()) unmount(composer);
	composers = [];
	if (shell) unmount(shell);
	shell = null;
	globalThis.ResizeObserver = realRO;
	host.remove();
	for (const toast of get(toasts)) toasts.dismiss(toast.id);
	paneOpen.set(false);
	vi.restoreAllMocks();
});

describe('HomeShell file drops', () => {
	it('keeps the configured pane when chat has an active document tab', () => {
		activeThreadId.set('document-thread');
		openArtifactTab('document-thread', { path: '/notes.md', title: 'notes.md' });
		shell = mount(HomeShell, { target: host, props: shellProps('chat') });
		flushSync();
		expect(host.textContent).toContain('Workspace');
		expect(host.textContent).not.toContain('No comments yet.');
		expect(host.querySelector('.dcp-head')).toBeNull();
	});

	it('preserves the pane choice while a view without a pane is active', () => {
		paneOpen.set(true);
		shell = mount(HomeShell, { target: host, props: shellProps('chat') });
		flushSync();
		expect(host.querySelector('main')?.classList.contains('pane-open')).toBe(true);

		unmount(shell);
		shell = null;
		shell = mount(HomeShell, {
			target: host,
			props: { ...shellProps('settings'), paneConfig: null },
		});
		flushSync();
		expect(get(paneOpen)).toBe(true);
		expect(host.querySelector('main')?.classList.contains('pane-open')).toBe(false);

		unmount(shell);
		shell = null;
		shell = mount(HomeShell, { target: host, props: shellProps('chat') });
		flushSync();
		expect(host.querySelector('main')?.classList.contains('pane-open')).toBe(true);
	});

	it('tells the user when no composer is mounted', () => {
		const show = vi.spyOn(toasts, 'show');
		shell = mount(HomeShell, { target: host, props: shellProps('documents') });
		flushSync();

		dropFile();

		expect(show).toHaveBeenCalledWith('Open a chat to add files', 'info');
	});

	it('routes subagent drops locally and all other app drops to the mounted main composer', () => {
		shell = mount(HomeShell, { target: host, props: shellProps('chat') });
		flushSync();
		const app = host.querySelector<HTMLElement>('.app')!;
		const mainInput = app.querySelector<HTMLInputElement>('[data-composer-attachment-input]')!;
		const leafSurface = document.createElement('div');
		leafSurface.dataset.dropComposerSurface = '';
		app.appendChild(leafSurface);
		const leafComposer = mountComposer(leafSurface);
		const leafInput = leafSurface.querySelector<HTMLInputElement>('[data-composer-attachment-input]')!;
		const mainReceived = captureFiles(mainInput);
		const leafReceived = captureFiles(leafInput);

		expect(app.querySelectorAll('[data-composer-attachment-input]')).toHaveLength(2);
		expect(leafSurface.querySelectorAll('input[type="file"][multiple]')).toHaveLength(2);
		const leafFiles = dropFile(leafSurface);
		expect(leafReceived()).toBe(leafFiles);
		expect(mainReceived()).toBeNull();

		unmount(leafComposer);
		composers = composers.filter((composer) => composer !== leafComposer);
		const outsideComposer = document.createElement('div');
		app.appendChild(outsideComposer);
		const mainFiles = dropFile(outsideComposer);
		expect(mainReceived()).toBe(mainFiles);
		expect(leafReceived()).toBe(leafFiles);
	});
});
