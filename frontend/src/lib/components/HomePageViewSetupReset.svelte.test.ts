import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import HomePageView from './HomePageView.svelte';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';

const seen = vi.hoisted(() => ({ mounts: 0 }));

vi.mock('./SetupScreen.svelte', () => ({
	default: () => { seen.mounts += 1; },
}));
vi.mock('./LoginScreen.svelte', () => ({ default: () => {} }));
vi.mock('./HomeShell.svelte', () => ({ default: () => {} }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
	status, headers: { 'Content-Type': 'application/json' },
});

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let answer: Record<string, unknown> | null;
let posts: number;

async function settle() { await tick(); await new Promise((done) => setTimeout(done)); await tick(); flushSync(); }

async function open(isAuthenticated: boolean) {
	const noop = () => {};
	component = mount(HomePageView, {
		target: host,
		props: {
			isAuthenticated, activeView: 'chat', sidebarOpen: false, chatPanelRef: undefined, conductorThreadId: null,
			systemStatus: {}, isDraggingOver: false, paneConfig: {}, lightboxUrl: '', lightboxOpen: false, deleteConfirmId: null,
			onNewChat: noop, onActivity: noop, onConductor: noop, onDocuments: noop, onAttachProject: noop, onSelectThread: noop,
			onDeleteThread: noop, onArchiveThread: noop, onOpenSettings: noop, onMeasurement: noop, onCloseMeasurement: noop,
			onToggleSidebar: noop, onTogglePane: noop, onOpenLightbox: noop, onAutoCreateThread: async () => {},
			onPickProject: noop, onPickProjectAndClosePane: noop, onCloseSettings: noop, onPickConversation: noop,
			onGoWorkspace: noop, onProjectCreated: noop, onConfirmDeleteThread: noop, onCancelDeleteThread: noop,
		} as never,
	});
	flushSync();
	await settle();
}

beforeEach(() => {
	seen.mounts = 0; posts = 0; answer = null;
	token.set('session');
	vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true, addEventListener: () => {}, removeEventListener: () => {} })));
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		if (String(input) === '/api/setup/reset-unfinished') { posts += 1; return answer ? json(answer) : json({}, 500); }
		return json({});
	}));
	host = document.createElement('div'); document.body.appendChild(host);
});

afterEach(async () => {
	if (component) await unmount(component);
	component = null;
	host.remove(); token.set(null); vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe('HomePageView unfinished setup', () => {
	it('starts over at the setup flow, signed out, when the backend reset an unfinished app-made setup', async () => {
		answer = { reset: true, refused: null };
		await open(true);

		expect(seen.mounts).toBeGreaterThan(0);
		expect(get(token)).toBeNull();
	});

	it.each(['not_created_by_app', 'in_use', 'no_unfinished_setup'])('leaves the app and the session alone when the backend refuses (%s)', async (refused) => {
		answer = { reset: false, refused };
		await open(true);

		expect(posts).toBe(1);
		expect(seen.mounts).toBe(0);
		expect(get(token)).toBe('session');
	});

	it('leaves the app alone when the answer cannot be read, so a failed read never locks out a finished install', async () => {
		await open(true);

		expect(seen.mounts).toBe(0);
		expect(get(token)).toBe('session');
	});

	it('asks nothing of the backend while signed out', async () => {
		answer = { reset: true, refused: null };
		await open(false);

		expect(posts).toBe(0);
		expect(seen.mounts).toBe(0);
	});
});
