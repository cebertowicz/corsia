/**
 * The HOST CONTRACT, on the real `HomeShell`.
 *
 * `HomeShell.activeView` is mutually exclusive, so a component-level test can
 * never establish where the dock is reachable from. These mount the actual
 * shell and drive the actual view switch.
 *
 * Red at `19ea844a`: the chat view has no decision surface at all — a decision
 * raised by a subagent is answerable only by leaving the conversation.
 */
import { get } from 'svelte/store';
import { flushSync, mount, tick, unmount, type ComponentProps } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import HomeShell from '$lib/components/HomeShell.svelte';
import { token } from '$lib/stores/auth';
import { activeThreadId, threads } from '$lib/stores/threads';
import { __reset, collapse, expandedId, focusTarget, openDecisionFromActivity } from '$lib/stores/attentionDock.svelte';
import type { AttentionQueue } from '$lib/api/attentionQueue';
import type { HostAccessLease } from '$lib/api/hostAccess';
import { decisionItem, externalReviewItem, queueBody } from './fixtures/attentionQueue';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let body: AttentionQueue;
let leases: HostAccessLease[];

/** A real lease awaiting the user, so the real control publishes the real fact. */
function awaitingUserLease(): HostAccessLease {
	return {
		id: 'lease-1', root_thread_id: 'thread-1', root_thread_title: 'Parent work',
		state: 'awaiting_user', purpose: 'Read one file outside the workspace',
		plan: ['stat the path'], requested_duration_seconds: 120,
		expected_resources: null, persistent_effects: null, rollback_plan: null,
		forbidden_effects: null, requesting_project_id: 'project-1',
		repo_policy_snapshot: [], repo_policy_hash: 'h', requested_policy_exceptions: [],
		application_hash: 'a', revision: 1, reviewer_model: null, reviewer: null,
		application_review_result: null, expires_at: null, terminal_reason: null,
		created_at: 't', updated_at: 't',
	} as HostAccessLease;
}

const EMPTY_RESPONSE = {
	contract_version: 2,
	as_of: 'now',
	target: { kind: 'workspace', id: 'ws', label: 'Workspace' },
	revision_token: '"r1"',
	items: [],
	attention: { awaiting_count: 0 },
};

function stubNetwork() {
	vi.stubGlobal(
		'fetch',
		vi.fn(async (url: string) => {
			const target = String(url);
			const payload = target.includes('/api/attention/queue')
				? body
				: target.includes('/api/host-access/leases')
					? { leases }
					: {};
			return {
				ok: true,
				status: 200,
				json: async () => payload,
				text: async () => JSON.stringify(payload),
				headers: { get: () => null },
			};
		}),
	);
}

async function settle() {
	for (let i = 0; i < 10; i += 1) {
		await Promise.resolve();
		await tick();
		flushSync();
	}
}

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

function docks(): NodeListOf<Element> {
	return host.querySelectorAll('[data-attention-dock]');
}

function dockBar(): HTMLButtonElement | null {
	return host.querySelector('[data-attention-dock] .dock-bar');
}



/** jsdom has no Web Animations API; HomeShell's `fade` transition needs it. */
function polyfillAnimate() {
	if (typeof Element.prototype.animate === 'function') return;
	Element.prototype.animate = function animate() {
		return {
			cancel() {}, finish() {}, play() {}, pause() {}, reverse() {},
			addEventListener() {}, removeEventListener() {},
			currentTime: 0, playState: 'finished', finished: Promise.resolve(),
			effect: { getComputedTiming: () => ({ duration: 0 }) },
		} as unknown as Animation;
	} as typeof Element.prototype.animate;
}

beforeEach(() => {
	polyfillAnimate();
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('test-bearer');
	body = queueBody(decisionItem(), 3);
	leases = [];
	__reset();
	vi.unstubAllGlobals();
	stubNetwork();
	threads.set([{ id: 'thread-1', title: 'Parent work', project_id: 'project-1' } as never]);
	activeThreadId.set('thread-1');
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	__reset();
	vi.restoreAllMocks();
});

describe('Host contract — one dock at the bottom of every work canvas', () => {
	it('reaches the user in the real chat view without leaving chat', async () => {
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();
		expect(docks().length).toBe(1);
		expect(host.textContent).toContain('Where should decision state live?');
		expect(host.textContent).toContain('Deep subagent');
	});


	it('never renders two docks in any single view', async () => {
		for (const view of ['chat', 'documents', 'settings']) {
			if (component) unmount(component);
			host.innerHTML = '';
			component = mount(HomeShell, { target: host, props: shellProps(view) });
			await settle();
			expect(docks().length, `${view}`).toBeLessThanOrEqual(1);
		}
	});

	it('deliberately has no dock in documents or settings', async () => {
		for (const view of ['documents', 'settings']) {
			if (component) unmount(component);
			host.innerHTML = '';
			component = mount(HomeShell, { target: host, props: shellProps(view) });
			await settle();
			expect(docks().length, `${view}`).toBe(0);
		}
	});

	it('renders nothing at all when the queue is empty', async () => {
		body = queueBody(null, 0);
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();
		expect(docks().length).toBe(0);
	});
});

describe('Host contract — one owner across a real route switch', () => {
	it('keeps exactly one dock through chat → documents → settings → chat', async () => {
		const props = $state(shellProps('chat'));
		component = mount(HomeShell, { target: host, props });
		await settle();
		expect(docks().length).toBe(1);

		for (const [view, expected] of [
			['documents', 0], ['settings', 0], ['chat', 1],
		] as const) {
			props.activeView = view;
			await settle();
			expect(docks().length, view).toBe(expected);
		}
	});




	it('survives an active-session change without duplicating or losing the dock', async () => {
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();
		activeThreadId.set('thread-2');
		await settle();
		expect(docks().length).toBe(1);
		expect(host.textContent).toContain('Where should decision state live?');
	});

	it('still shows the dock with no active session', async () => {
		threads.set([]);
		activeThreadId.set(null);
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();
		expect(docks().length).toBe(1);
	});

});

describe('Host contract — lease precedence in the shared slot', () => {
	it('a lease awaiting the user takes the slot; the dock defers, still named', async () => {
		leases = [awaitingUserLease()];
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();

		const bar = dockBar();
		expect(bar).toBeTruthy();
		expect(bar!.getAttribute('aria-disabled')).toBe('true');
		expect(host.querySelectorAll('.decision-chip').length).toBe(0);
	});

	it('a suppressed click is inert and never springs open once the lease clears', async () => {
		leases = [awaitingUserLease()];
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();
		dockBar()!.click();
		await settle();
		expect(host.querySelectorAll('.decision-chip').length).toBe(0);

		// The lease resolves at its own source; the control clears its own flag.
		leases = [];
		activeThreadId.set('thread-2');
		await settle();

		expect(host.querySelectorAll('.decision-chip').length).toBe(0);
		expect(dockBar()!.getAttribute('aria-disabled')).toBe('false');
	});

	it('an already-open chip collapses rather than stacking under a lease', async () => {
		// The user had opened the chip; a lease then arrives and takes the slot.
		// Suppression must close the chip, not stack a second large control —
		// and not merely hide it, or lifting the lease would spring it open.
		expandedId.set('decision-1');
		leases = [awaitingUserLease()];
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();

		expect(host.querySelectorAll('.decision-chip').length).toBe(0);
		expect(dockBar()).toBeTruthy();
		expect(dockBar()!.getAttribute('aria-disabled')).toBe('true');

		let open: string | null = 'x';
		expandedId.subscribe((value) => { open = value; })();
		expect(open).toBeNull();
	});

});

describe('Host contract — the external review lane hands off, never resolves', () => {
	it('actually reaches the Documents pane from chat', async () => {
		body = queueBody(externalReviewItem(), 1);
		for (const view of ['chat'] as const) {
			if (component) unmount(component);
			host.innerHTML = '';
			// The expanded item is module state that survives unmount by design,
			// so each pass must start from the collapsed bar.
			__reset();
			const opened: string[] = [];
			const props = shellProps(view) as unknown as Record<string, unknown>;
			props.onDocuments = (lens?: string) => opened.push(lens ?? 'all');
			component = mount(HomeShell, { target: host, props: props as never });
			await settle();
			dockBar()!.click();
			await settle();

			const open = Array.from(host.querySelectorAll('button')).find((button) =>
				button.textContent?.includes('Open in documents'),
			) as HTMLButtonElement;
			expect(open, view).toBeTruthy();
			open.click();
			await settle();
			// A hand-off that goes nowhere is a dead end, not a hand-off.
			expect(opened, view).toEqual(['all']);
		}
	});

	it('routes to Documents and offers no clear or dismiss', async () => {
		body = queueBody(externalReviewItem(), 1);
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();
		dockBar()!.click();
		await settle();

		expect(host.textContent).toContain('Reviewer findings on PR 9');
		expect(host.textContent).toContain('Open in documents');
		const labels = Array.from(host.querySelectorAll('button')).map(
			(button) => (button.textContent ?? '').toLowerCase(),
		);
		expect(labels.some((label) => label.includes('clear'))).toBe(false);
		expect(labels.some((label) => label.includes('dismiss'))).toBe(false);
		expect(labels.some((label) => label.includes('resolve'))).toBe(false);
	});
});

describe('Composed first viewport — one coherent needs-you hierarchy', () => {
	it('a decision, a lease and generic attention do not compete', async () => {
		leases = [awaitingUserLease()];
		body = queueBody(decisionItem(), 2);
		stubNetwork();

		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();

		// Exactly one dock, and it is deferred — not stacked under the lease.
		expect(docks().length).toBe(1);
		expect(dockBar()!.getAttribute('aria-disabled')).toBe('true');
		expect(host.querySelectorAll('.decision-chip').length).toBe(0);
		// The decision is still named while deferred: visible, not hidden.
		expect(dockBar()!.textContent).toContain('Where should decision state live?');
		// The generic pile never renders a second copy of it.
		expect(host.querySelectorAll('.al-list').length).toBe(0);

		// Once the lease resolves, the dock is available — but did not spring open.
		leases = [];
		activeThreadId.set('thread-2');
		await settle();
		expect(dockBar()!.getAttribute('aria-disabled')).toBe('false');
		expect(host.querySelectorAll('.decision-chip').length).toBe(0);
	});

});

describe('Opening a decision from Activity', () => {
	const open = (id: string) => () => activeThreadId.set(id);

	it('opens the owning thread and expands the head decision', async () => {
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();
		expect(await openDecisionFromActivity(open('child'))).toBe('opened');
		await settle();
		expect(get(expandedId)).toBe(body.current?.external_id);
		expect(get(expandedId)).not.toBeNull();
		expect(get(focusTarget)).toBeNull();
	});

	it('expands once for a dock that mounts after the open, never again', async () => {
		const props = $state(shellProps('documents'));
		component = mount(HomeShell, { target: host, props });
		await settle();
		expect(await openDecisionFromActivity(open('child'))).toBe('opened');
		props.activeView = 'chat';
		await settle();
		expect(get(expandedId)).not.toBeNull();
		collapse();
		props.activeView = 'documents';
		await settle();
		props.activeView = 'chat';
		await settle();
		expect(get(expandedId)).toBeNull();
	});

	it('opens a row\'s own thread without expanding another thread\'s decision', async () => {
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();
		expect(await openDecisionFromActivity(open('thread-b'), 'thread-b')).toBe('opened');
		await settle();
		expect(get(activeThreadId)).toBe('thread-b');
		expect(get(focusTarget)).toBeNull();
		expect(get(expandedId)).toBeNull();
	});

	it('requests nothing when the thread did not open', async () => {
		component = mount(HomeShell, { target: host, props: shellProps('chat') });
		await settle();
		expect(await openDecisionFromActivity(() => {})).toBe('failed');
		await settle();
		expect(get(focusTarget)).toBeNull();
		expect(get(expandedId)).toBeNull();
	});

	it('reports an empty queue instead of opening anything', async () => {
		body = queueBody(null, 0);
		const opened = vi.fn();
		expect(await openDecisionFromActivity(opened)).toBe('empty');
		expect(opened).not.toHaveBeenCalled();
	});
});
