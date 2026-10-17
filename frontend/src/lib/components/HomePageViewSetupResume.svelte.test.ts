import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import HomePageView from './HomePageView.svelte';
import { token } from '$lib/stores/auth';

const seen = vi.hoisted(() => ({ resume: [] as boolean[] }));

vi.mock('./SetupScreen.svelte', () => ({
	default: (_anchor: unknown, props: { resume?: boolean }) => { seen.resume.push(Boolean(props.resume)); },
}));
vi.mock('./LoginScreen.svelte', () => ({ default: () => {} }));
vi.mock('./HomeShell.svelte', () => ({ default: () => {} }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
	status, headers: { 'Content-Type': 'application/json' },
});

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let progress: Record<string, unknown> | null;
let reads: number;

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
	seen.resume = []; reads = 0; progress = null;
	token.set('session');
	vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true, addEventListener: () => {}, removeEventListener: () => {} })));
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		if (String(input) === '/api/setup/progress') { reads += 1; return progress ? json(progress) : json({}, 500); }
		return json({});
	}));
	host = document.createElement('div'); document.body.appendChild(host);
});

afterEach(async () => {
	if (component) await unmount(component);
	component = null;
	host.remove(); token.set(null); vi.restoreAllMocks(); vi.unstubAllGlobals();
});

describe('HomePageView setup resume', () => {
	it('reopens setup, as a resume, for a signed-in person whose setup is unfinished', async () => {
		progress = { setup_completed: false, step: 'models', local_ai: null };
		await open(true);

		expect(seen.resume).toContain(true);
	});

	it('never opens setup for a signed-in person whose setup is finished', async () => {
		progress = { setup_completed: true, step: null, local_ai: null };
		await open(true);

		expect(reads).toBe(1);
		expect(seen.resume).toEqual([]);
	});

	it('does not open setup when the record cannot be read', async () => {
		await open(true);

		expect(seen.resume).toEqual([]);
	});

	it('asks nothing of the backend while signed out', async () => {
		progress = { setup_completed: false, step: 'models', local_ai: null };
		await open(false);

		expect(reads).toBe(0);
		expect(seen.resume).toEqual([]);
	});
});
