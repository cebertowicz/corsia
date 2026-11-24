import { beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { canvasHeader } from '$lib/stores/canvasHeader';
import { syncCanvasHeader } from '$lib/homeCanvasHeader';
import { activityPhoneItem } from '$lib/stores/activityPhoneItem';

type Input = Parameters<typeof syncCanvasHeader>[0];

function makeInput(overrides: Partial<Input> = {}): Input {
	return {
		threadSettingsOpen: false,
		compactArtifact: null,
		contextAttachTarget: null,
		activeView: 'chat',
		uiScopeName: null,
		activeThreadId: null,
		conductorThreadId: null,
		threads: [],
		closeThreadSettings: () => {},
		closeSettings: () => {},
		closeDocuments: () => {},

		...overrides,
	};
}

beforeEach(() => {
	canvasHeader.set(null);
	activityPhoneItem.set(null);
});

function compact(overrides: Record<string, unknown> = {}) {
	return {
		title: 'Design.md',
		tabCount: 2,
		anyChanged: false,
		onBack: () => {},
		onTabs: () => {},
		...overrides,
	};
}

describe('syncCanvasHeader — compact artifact', () => {
	it('takes over with Back to chat and the tab title, which opens the open-files list', () => {
		const onBack = vi.fn();
		const onTabs = vi.fn();
		syncCanvasHeader(makeInput({
			compactArtifact: compact({ onBack, onTabs }),
			activeView: 'chat',
			activeThreadId: 's1',
		}));
		const h = get(canvasHeader);
		expect(h?.title).toBe('Design.md');
		expect(h?.backLabel).toBe('Chat');
		expect(h?.actions).toBeUndefined();
		expect(h?.titleAction).toMatchObject({ label: 'Open files, 2', title: 'Show open files', changed: false });
		h?.onBack?.();
		expect(onBack).toHaveBeenCalledOnce();
		h?.titleAction?.onClick();
		expect(onTabs).toHaveBeenCalledOnce();
	});

	it('marks the title action when any tab has changed', () => {
		syncCanvasHeader(makeInput({
			compactArtifact: compact({ anyChanged: true }),
			activeView: 'chat',
		}));
		expect(get(canvasHeader)?.titleAction).toMatchObject({ changed: true });
	});

	it('yields to true modal takeovers', () => {
		syncCanvasHeader(makeInput({ compactArtifact: compact(), threadSettingsOpen: true }));
		expect(get(canvasHeader)?.title).toBe('Chat settings');
	});

	it('is absent while chat is the active surface', () => {
		syncCanvasHeader(makeInput({ compactArtifact: null, activeView: 'chat' }));
		expect(get(canvasHeader)).toBeNull();
	});
});

describe('syncCanvasHeader — no doc', () => {
	it('returns null header in plain chat view (title shown by Header.svelte directly)', () => {
		syncCanvasHeader(makeInput({ activeView: 'chat' }));
		expect(get(canvasHeader)).toBeNull();
	});

	it('returns null header even when a regular session is active', () => {
		const threads = [
			{ id: 'chat-1', title: 'My Thread', user_id: 'u', profile: 'personal',
			  model: null, created_at: '2026-01-01', updated_at: '2026-01-01' },
		];
		syncCanvasHeader(makeInput({ activeView: 'chat', activeThreadId: 'chat-1', threads }));
		expect(get(canvasHeader)).toBeNull();
	});

	it('names Activity in a non-navigating header, whatever the scope', () => {
		syncCanvasHeader(makeInput({ activeView: 'activity', uiScopeName: 'CORSIA' }));
		const h = get(canvasHeader);
		expect(h?.title).toBe('Activity');
		expect(h?.onBack).toBeUndefined();
		expect(h?.actions).toBeUndefined();
	});

	it('gives a phone Activity item its way back to the list and Next with its position', () => {
		const onBack = vi.fn();
		const onNext = vi.fn();
		activityPhoneItem.set({ position: 2, count: 7, nextDisabled: false, onBack, onNext });
		syncCanvasHeader(makeInput({ activeView: 'activity' }));
		const h = get(canvasHeader);
		expect(h?.backLabel).toBe('Activity');
		expect(h?.links).toBe(true);
		h?.onBack?.();
		expect(onBack).toHaveBeenCalledOnce();
		const next = h?.actions?.[0];
		expect(next?.label).toBe('2 of 7 · Next ›');
		expect(next?.disabled).toBe(false);
		next?.onClick();
		expect(onNext).toHaveBeenCalledOnce();
	});

	it('greys the phone header Next on the last Activity item', () => {
		activityPhoneItem.set({ position: 7, count: 7, nextDisabled: true, onBack: vi.fn(), onNext: vi.fn() });
		syncCanvasHeader(makeInput({ activeView: 'activity' }));
		const next = get(canvasHeader)?.actions?.[0];
		expect(next?.label).toBe('7 of 7 · Next ›');
		expect(next?.disabled).toBe(true);
	});

	it('returns Documents header in documents view', () => {
		const closeDocuments = vi.fn();
		syncCanvasHeader(makeInput({ activeView: 'documents', closeDocuments }));
		const h = get(canvasHeader);
		expect(h?.title).toBe('Documents');
		h?.onBack?.();
		expect(closeDocuments).toHaveBeenCalledOnce();
	});

	it('labels Documents back route by the context attach origin', () => {
		const closeDocuments = vi.fn();
		syncCanvasHeader(makeInput({
			activeView: 'documents',
			contextAttachTarget: {
				scope: 'thread',
				threadId: 'thread-1',
				returnTo: 'thread-settings',
			},
			closeDocuments,
		}));
		const h = get(canvasHeader);
		expect(h?.title).toBe('Documents');
		expect(h?.backLabel).toBe('Chat settings');
		h?.onBack?.();
		expect(closeDocuments).toHaveBeenCalledOnce();
	});

	it('returns settings header in settings view', () => {
		const closeSettings = vi.fn();
		syncCanvasHeader(makeInput({ activeView: 'settings', closeSettings }));
		const h = get(canvasHeader);
		expect(h?.title).toBe('Settings');
		h?.onBack?.();
		expect(closeSettings).toHaveBeenCalledOnce();
	});

	it('names a drilled Settings page and goes up one level from it', () => {
		const onBack = vi.fn();
		const closeSettings = vi.fn();
		syncCanvasHeader(makeInput({
			activeView: 'settings',
			settingsDrill: { title: 'Alpha', backLabel: 'Projects', onBack },
			closeSettings,
		}));
		const h = get(canvasHeader);
		expect(h).toMatchObject({ title: 'Alpha', backLabel: 'Projects' });
		h?.onBack?.();
		expect(onBack).toHaveBeenCalledOnce();
		expect(closeSettings).not.toHaveBeenCalled();
	});

	it('labels Documents back as Settings when a context pick started there', () => {
		syncCanvasHeader(makeInput({
			activeView: 'documents',
			contextAttachTarget: { scope: 'workspace', returnTo: 'settings', returnSection: 'context' },
		}));
		expect(get(canvasHeader)?.backLabel).toBe('Settings');
	});

	it('names the workspace scope\'s designated hidden thread as CORSIA settings', () => {
		syncCanvasHeader(makeInput({
			threadSettingsOpen: true,
			uiScopeName: null,
			activeThreadId: 'agent-1',
			conductorThreadId: 'agent-1',
		}));
		expect(get(canvasHeader)?.title).toBe('CORSIA settings');
		expect(get(canvasHeader)?.backLabel).toBe('CORSIA');
	});

	it('keeps Conductor for a project scope\'s designated hidden thread', () => {
		syncCanvasHeader(makeInput({
			threadSettingsOpen: true,
			uiScopeName: 'Alpha',
			activeThreadId: 'conductor-1',
			conductorThreadId: 'conductor-1',
		}));
		expect(get(canvasHeader)?.title).toBe('Conductor settings');
		expect(get(canvasHeader)?.backLabel).toBe('Conductor');
	});

	it('returns thread settings header with back to the active thread', () => {
		const closeThreadSettings = vi.fn();
		const threads = [
			{ id: 'thread-1', title: 'CTX stability review', user_id: 'u', profile: 'personal',
			  model: null, created_at: '2026-01-01', updated_at: '2026-01-01' },
		];
		syncCanvasHeader(makeInput({
			threadSettingsOpen: true,
			activeView: 'chat',
			activeThreadId: 'thread-1',
			threads,
			closeThreadSettings,
		}));
		const h = get(canvasHeader);
		expect(h?.title).toBe('Chat settings');
		expect(h?.backLabel).toBe('CTX stability review');
		h?.onBack?.();
		expect(closeThreadSettings).toHaveBeenCalledOnce();
	});
});


describe('syncCanvasHeader — long titles (mobile truncation contract)', () => {
	it('passes long document names through unmodified — CSS truncates via text-overflow', () => {
		const longName = 'A'.repeat(200);
		syncCanvasHeader(makeInput({
			compactArtifact: compact({ title: longName }),
			activeView: 'chat',
		}));
		expect(get(canvasHeader)?.title).toBe(longName);
	});

	it('passes long session titles through unmodified — Header.svelte truncates via .h-scope', () => {
		const threads = [
			{ id: 's1', title: 'B'.repeat(200), user_id: 'u', profile: 'personal',
			  model: null, created_at: '2026-01-01', updated_at: '2026-01-01' },
		];
		// A regular chat session does NOT set canvasHeader (Header shows activeThread.title directly).
		syncCanvasHeader(makeInput({ activeView: 'chat', activeThreadId: 's1', threads }));
		// canvasHeader must be null — title rendering stays with Header's activeThread binding.
		expect(get(canvasHeader)).toBeNull();
	});
});
