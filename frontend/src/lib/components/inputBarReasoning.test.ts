import { describe, it, expect } from 'vitest';
import {
	reasoningSegments,
	resolveReasoningEffort,
	reasoningEffortLabel,
} from './inputBarReasoning';
import type { ReasoningConfig } from '$lib/stores/models';

// External harnesses own an auto mode, which the backend marks with
// supports_auto so the selector keeps offering it.
const CODEX_CONFIG: ReasoningConfig = {
	type: 'effort_levels',
	values: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'],
	default: 'high',
	supports_auto: true,
};

const CC_CONFIG: ReasoningConfig = {
	type: 'effort_levels',
	values: ['low', 'medium', 'high', 'xhigh', 'max'],
	default: 'high',
	supports_auto: true,
};

// A local model discovered from an oMLX server: its levels come from the chat
// template and `default` is the level that server is configured to use.
const LOCAL_CONFIG: ReasoningConfig = {
	type: 'effort_levels',
	values: ['xhigh', 'medium', 'low'],
	default: 'xhigh',
};

const BOOL_CONFIG: ReasoningConfig = {
	type: 'boolean',
	default: true,
};

describe('reasoningSegments', () => {
	it('codex: no duplicate keys — none in values must not produce a second Off segment', () => {
		const segs = reasoningSegments(CODEX_CONFIG);
		const keys = segs.map(s => s.value ?? 'auto');
		const dupes = keys.filter((k, i) => keys.indexOf(k) !== i);
		expect(dupes).toEqual([]);
	});

	it('codex: always starts with Auto (null) and Off (none)', () => {
		const segs = reasoningSegments(CODEX_CONFIG);
		expect(segs[0]).toEqual({ value: null, label: 'Auto' });
		expect(segs[1]).toEqual({ value: 'none', label: 'Off' });
	});

	it('codex: remaining tiers follow in config order without none', () => {
		const segs = reasoningSegments(CODEX_CONFIG);
		const values = segs.map(s => s.value);
		expect(values).toEqual([null, 'none', 'minimal', 'low', 'medium', 'high', 'xhigh']);
	});

	it('cc: no duplicate keys (none not in cc values)', () => {
		const segs = reasoningSegments(CC_CONFIG);
		const keys = segs.map(s => s.value ?? 'auto');
		const dupes = keys.filter((k, i) => keys.indexOf(k) !== i);
		expect(dupes).toEqual([]);
	});

	it('cc: starts with Auto, Off, then cc tiers', () => {
		const segs = reasoningSegments(CC_CONFIG);
		const values = segs.map(s => s.value);
		expect(values).toEqual([null, 'none', 'low', 'medium', 'high', 'xhigh', 'max']);
	});

	it('local: offers no Auto — starts at Off then the template levels', () => {
		const segs = reasoningSegments(LOCAL_CONFIG);
		expect(segs.map(s => s.value)).toEqual(['none', 'xhigh', 'medium', 'low']);
	});

	it('boolean: returns Off / On pair', () => {
		const segs = reasoningSegments(BOOL_CONFIG);
		expect(segs).toEqual([
			{ value: 'none', label: 'Off' },
			{ value: 'medium', label: 'On' },
		]);
	});

	it('null config: returns empty array', () => {
		expect(reasoningSegments(null)).toEqual([]);
	});
});

describe('resolveReasoningEffort — effort_levels', () => {
	it('null raw resolves to null (auto)', () => {
		expect(resolveReasoningEffort(null, CODEX_CONFIG)).toBeNull();
		expect(resolveReasoningEffort(null, CC_CONFIG)).toBeNull();
	});

	it('none raw resolves to none for both runtimes', () => {
		expect(resolveReasoningEffort('none', CODEX_CONFIG)).toBe('none');
		expect(resolveReasoningEffort('none', CC_CONFIG)).toBe('none');
	});

	it('valid codex value resolves correctly', () => {
		expect(resolveReasoningEffort('medium', CODEX_CONFIG)).toBe('medium');
		expect(resolveReasoningEffort('xhigh', CODEX_CONFIG)).toBe('xhigh');
	});

	it('valid cc value resolves correctly', () => {
		expect(resolveReasoningEffort('max', CC_CONFIG)).toBe('max');
		expect(resolveReasoningEffort('high', CC_CONFIG)).toBe('high');
	});

	it('unknown raw falls back to null', () => {
		expect(resolveReasoningEffort('max', CODEX_CONFIG)).toBeNull();
		expect(resolveReasoningEffort('minimal', CC_CONFIG)).toBeNull();
	});
});

describe('reasoningEffortLabel', () => {
	it('null effort → AUTO chip label for effort_levels', () => {
		expect(reasoningEffortLabel(CODEX_CONFIG, null)).toBe('AUTO');
		expect(reasoningEffortLabel(CC_CONFIG, null)).toBe('AUTO');
	});

	it('none effort → OFF chip label', () => {
		expect(reasoningEffortLabel(CODEX_CONFIG, 'none')).toBe('OFF');
	});

	it('named effort → uppercased short label', () => {
		expect(reasoningEffortLabel(CODEX_CONFIG, 'medium')).toBe('MED');
		expect(reasoningEffortLabel(CC_CONFIG, 'xhigh')).toBe('XHI');
	});

	it('boolean config uses ON/OFF', () => {
		expect(reasoningEffortLabel(BOOL_CONFIG, 'medium')).toBe('ON');
		expect(reasoningEffortLabel(BOOL_CONFIG, 'none')).toBe('OFF');
	});

	it('null config returns empty string', () => {
		expect(reasoningEffortLabel(null, null)).toBe('');
	});

	it('local: untouched picker shows the server level, never AUTO', () => {
		expect(reasoningEffortLabel(LOCAL_CONFIG, null)).toBe('XHI');
	});
});

describe('local models (no provider auto mode)', () => {
	it('untouched selector resolves to the server-configured level', () => {
		expect(resolveReasoningEffort(null, LOCAL_CONFIG)).toBe('xhigh');
		expect(resolveReasoningEffort(undefined, LOCAL_CONFIG)).toBe('xhigh');
	});

	it('an explicit choice still wins', () => {
		expect(resolveReasoningEffort('low', LOCAL_CONFIG)).toBe('low');
	});

	it('thinking can still be turned off', () => {
		expect(resolveReasoningEffort('none', LOCAL_CONFIG)).toBe('none');
	});

	it('a level this model does not support falls back to its server level', () => {
		expect(resolveReasoningEffort('minimal', LOCAL_CONFIG)).toBe('xhigh');
	});
});

// An External model's levels come from its provider's published list: no known
// default, "Auto" sends nothing, and "Off" only when the list has it.
const EXTERNAL_CONFIG: ReasoningConfig = {
	type: 'effort_levels',
	values: ['low', 'high', 'max'],
	default: null,
	supports_auto: true,
	supports_off: false,
};

describe('External models (published levels)', () => {
	it('offers Auto and exactly the published levels, with no Off', () => {
		expect(reasoningSegments(EXTERNAL_CONFIG)).toEqual([
			{ value: null, label: 'Auto' },
			{ value: 'low', label: 'Low' },
			{ value: 'high', label: 'High' },
			{ value: 'max', label: 'Max' },
		]);
	});

	it('an untouched picker, or a level it does not list, shows Auto', () => {
		expect(resolveReasoningEffort(null, EXTERNAL_CONFIG)).toBeNull();
		expect(resolveReasoningEffort('medium', EXTERNAL_CONFIG)).toBeNull();
		expect(resolveReasoningEffort('none', EXTERNAL_CONFIG)).toBeNull();
		expect(reasoningEffortLabel(EXTERNAL_CONFIG, null)).toBe('AUTO');
	});

	it('a published level is kept', () => {
		expect(resolveReasoningEffort('max', EXTERNAL_CONFIG)).toBe('max');
	});
});
