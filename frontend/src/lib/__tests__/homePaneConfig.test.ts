/**
 * Tests for buildPaneConfig — ensures the conductor-pane wiring cannot regress.
 *
 * Key invariants after unified-conductor-pane-ia:
 *   1. Active conductor session → NO attention-conductor section; artifacts is FIRST.
 *   2. Active non-conductor session → no attention-conductor section (unchanged).
 *   3. No active session and no scope → no pane.
 *   4. Null-session regression: activeThreadId matching conductor still returns CHAT_SECTIONS (no attention-conductor).
 */

import { describe, it, expect } from 'vitest';
import { buildPaneConfig } from '$lib/homePaneConfig';
import type { Thread } from '$lib/stores/threads';

// ── Helpers ───────────────────────────────────────────────────────────────────

function makeThread(id: string): Thread {
	return {
		id,
		user_id: 'u1',
		profile: 'personal',
		title: 'Test',
		model: null,
		project_id: undefined,
		created_at: new Date().toISOString(),
		updated_at: new Date().toISOString(),
	};
}

/** Chat views always have a pane; assert it so tests read their sections directly. */
function pane(...args: Parameters<typeof buildPaneConfig>) {
	const config = buildPaneConfig(...args);
	if (!config) throw new Error('expected a pane');
	return config;
}

const SCOPE = { id: 'proj-1', name: 'CORSIA' };
const CONDUCTOR_ID = 'conductor-session-abc';
const OTHER_ID = 'regular-session-xyz';

function kinds(config: NonNullable<ReturnType<typeof buildPaneConfig>>) {
	return config.sections.map((s) => s.kind ?? 'plain');
}

function titles(config: NonNullable<ReturnType<typeof buildPaneConfig>>) {
	return config.sections.map((s) => s.title);
}

// ── Active conductor session ──────────────────────────────────────────────────

describe('buildPaneConfig — active conductor session', () => {
	const thread = makeThread(CONDUCTOR_ID);

	it('does NOT include attention-conductor (unified queue handled by ArtifactPane)', () => {
		const config = pane('chat', thread, SCOPE, CONDUCTOR_ID);
		expect(config.sections.some((s) => (s.kind as string) === 'attention-conductor')).toBe(false);
	});

	it('puts the current thread plan first and keeps artifacts below it', () => {
		const config = pane('chat', thread, SCOPE, CONDUCTOR_ID);
		expect(kinds(config).slice(0, 2)).toEqual(['plan', 'artifacts']);
	});

	it('labels the pane with the project when the conductor session is project-scoped', () => {
		const config = pane('chat', thread, SCOPE, CONDUCTOR_ID);
		expect(config.label).toBe('CORSIA');
		expect(config).not.toHaveProperty('gear');
	});

	it('uses the approved thread hierarchy and removes the draft-plan stub', () => {
		const config = pane('chat', thread, SCOPE, CONDUCTOR_ID);
		const titleList = titles(config);
		expect(titleList.slice(0, 2)).toEqual(['Plan', 'Artifacts']);
		expect(titleList).not.toContain('Draft a plan');
		expect(titleList).not.toContain('Thread context');
	});

	it('conductor and non-conductor chat threads produce same section structure', () => {
		const condConfig = pane('chat', thread, SCOPE, CONDUCTOR_ID);
		const otherThread = makeThread(OTHER_ID);
		const otherConfig = pane('chat', otherThread, SCOPE, CONDUCTOR_ID);
		expect(kinds(condConfig)).toEqual(kinds(otherConfig));
	});
});

// ── Active non-conductor session ──────────────────────────────────────────────

describe('buildPaneConfig — active non-conductor session', () => {
	const thread = makeThread(OTHER_ID);

	it('does NOT include attention-conductor', () => {
		const config = pane('chat', thread, SCOPE, CONDUCTOR_ID);
		expect(config.sections.some((s) => (s.kind as string) === 'attention-conductor')).toBe(false);
	});

	it('still includes artifacts', () => {
		const config = pane('chat', thread, SCOPE, CONDUCTOR_ID);
		expect(config.sections.some((s) => s.kind === 'artifacts')).toBe(true);
	});

	it('exposes project settings when a chat session is project-scoped', () => {
		const config = pane('chat', thread, SCOPE, CONDUCTOR_ID);
		expect(config.label).toBe('CORSIA');
		expect(config).not.toHaveProperty('gear');
	});

	it('does not show project settings for workspace-only chat threads', () => {
		const config = pane('chat', thread, null, CONDUCTOR_ID);
		expect(config.label).toBe('Workspace');
		expect(config).not.toHaveProperty('gear');
	});

	it('keeps thread context out of the right pane; thread settings owns it', () => {
		const threadWithContext = { ...thread, bootloader_files: JSON.stringify(['/workspace/thread.md']) };
		const config = pane('chat', threadWithContext, SCOPE, CONDUCTOR_ID);
		expect(config.sections.some((s) => s.title === 'Thread context')).toBe(false);
		expect(config.sections.some((s) => s.kind === 'artifacts')).toBe(true);
	});

});

// ── No active session ─────────────────────────────────────────────────────────

describe('buildPaneConfig — no active session', () => {
	it('has no pane without a thread or scope, so the Projects list never shows there', () => {
		expect(buildPaneConfig('chat', null, null, CONDUCTOR_ID)).toBeNull();
	});

	it('scope landing uses scope sections (no attention-conductor)', () => {
		const config = pane('chat', null, SCOPE, CONDUCTOR_ID);
		expect(config.sections.some((s) => (s.kind as string) === 'attention-conductor')).toBe(false);
		expect(config.label).toBe('CORSIA');
	});

});

// ── Runtime blocker regression: activeThread null, activeThreadId matches conductor ──
//
// Reproduces the dev incident: conductor session absent from $threads so
// $activeThread stays null, but $activeThreadId is already set after goConductor().
// buildPaneConfig must use the fallback ID path and return CONDUCTOR_SECTIONS.

describe('buildPaneConfig — activeThread null but activeThreadId matches conductor (runtime blocker)', () => {
	it('returns chat/artifact sections (no attention-conductor) when conductor session not yet in $threads', () => {
		const config = pane('chat', null, SCOPE, CONDUCTOR_ID, CONDUCTOR_ID);
		expect(config.sections.some((s) => (s.kind as string) === 'attention-conductor')).toBe(false);
		expect(kinds(config).slice(0, 2)).toEqual(['plan', 'artifacts']);
	});

	it('returns correct label and label when conductor match is project-scoped (no session obj)', () => {
		const config = pane('chat', null, SCOPE, CONDUCTOR_ID, CONDUCTOR_ID);
		expect(config.label).toBe('CORSIA');
		expect(config).not.toHaveProperty('gear');
	});

	it('keeps the project label when activeThreadId does not match conductorThreadId', () => {
		const config = pane('chat', null, SCOPE, CONDUCTOR_ID, OTHER_ID);
		expect(config.sections.some((s) => (s.kind as string) === 'attention-conductor')).toBe(false);
		expect(config.label).toBe('CORSIA');
		expect(config).not.toHaveProperty('gear');
	});

	it('gives an open thread missing from the store the thread pane, with or without a scope', () => {
		expect(kinds(pane('chat', null, null, CONDUCTOR_ID, OTHER_ID))).toEqual(['plan', 'artifacts']);
		expect(kinds(pane('chat', null, SCOPE, CONDUCTOR_ID, OTHER_ID))).toEqual(['plan', 'artifacts']);
	});

	it('activeThread present takes priority over activeThreadId — plan first', () => {
		const thread = makeThread(CONDUCTOR_ID);
		const config = pane('chat', thread, SCOPE, CONDUCTOR_ID, CONDUCTOR_ID);
		expect(kinds(config).slice(0, 2)).toEqual(['plan', 'artifacts']);
	});
});

// ── No conductorThreadId provided (pre-fetch / unknown) ─────────────────────

describe('buildPaneConfig — conductorThreadId unknown', () => {
	it('active session with no conductorThreadId never shows attention-conductor', () => {
		const thread = makeThread(CONDUCTOR_ID);
		const config = pane('chat', thread, SCOPE, null);
		expect(config.sections.some((s) => (s.kind as string) === 'attention-conductor')).toBe(false);
	});

	it('active session with undefined conductorThreadId never shows attention-conductor', () => {
		const thread = makeThread(CONDUCTOR_ID);
		const config = pane('chat', thread, SCOPE, undefined);
		expect(config.sections.some((s) => (s.kind as string) === 'attention-conductor')).toBe(false);
	});
});

// ── Views other than chat ─────────────────────────────────────────────────────

describe('buildPaneConfig — pane only on workspace Activity', () => {
	it('workspace Activity lists Projects', () => {
		const config = buildPaneConfig('activity', null, null, CONDUCTOR_ID);
		expect(config).not.toBeNull();
		expect(config!.label).toBe('Workspace');
		expect(kinds(config!)).toEqual(['projects']);
	});

	it('project Activity has no pane', () => {
		expect(buildPaneConfig('activity', null, SCOPE, CONDUCTOR_ID)).toBeNull();
	});

	// Workspace and project settings live in Settings, which has no pane.
	it.each(['measurement', 'settings', 'documents'] as const)('%s has no pane in either scope', (view) => {
		expect(buildPaneConfig(view, null, null, CONDUCTOR_ID)).toBeNull();
		expect(buildPaneConfig(view, null, SCOPE, CONDUCTOR_ID)).toBeNull();
	});

	it('a thread keeps its Plan and Artifacts sections while thread settings are open', () => {
		const config = pane('chat', makeThread(OTHER_ID), SCOPE, CONDUCTOR_ID, null);
		expect(kinds(config)).toEqual(['plan', 'artifacts']);
	});
});

describe('buildPaneConfig — the pane never carries a settings gear', () => {
	it.each([
		['conductor thread', makeThread(CONDUCTOR_ID), SCOPE],
		['project thread', makeThread(OTHER_ID), SCOPE],
		['workspace thread', makeThread(OTHER_ID), null],
		['scope landing', null, SCOPE],
	] as const)('%s', (_name, thread, scope) => {
		expect(pane('chat', thread, scope, CONDUCTOR_ID)).not.toHaveProperty('gear');
	});
});
