import { describe, it, expect } from 'vitest';
import { planKeyString, parseKeyString, keyMatchesChange, type PlanKey } from '$lib/stores/planKeys';

describe('planKeyString / parseKeyString', () => {
	it('round-trips full, compact Activity, thread, and child keys', () => {
		const keys: PlanKey[] = [
			{ kind: 'workspace' },
			{ kind: 'project', id: 'p1' },
			{ kind: 'activity-workspace' },
			{ kind: 'activity-project', id: 'p1' },
			{ kind: 'thread', id: 't1' },
			{ kind: 'child', id: 'n1' },
		];
		for (const key of keys) {
			expect(parseKeyString(planKeyString(key))).toEqual(key);
		}
	});

	it('is stable across rebuilt-but-equal key objects', () => {
		const a: PlanKey = { kind: 'thread', id: 't1' };
		const b: PlanKey = { kind: 'thread', id: 't1' };
		expect(planKeyString(a)).toBe(planKeyString(b));
	});

	it('rejects malformed strings', () => {
		expect(parseKeyString('bogus')).toBeNull();
		expect(parseKeyString('thread:')).toBeNull();
	});

	it('round-trips ids containing colons', () => {
		const key: PlanKey = { kind: 'thread', id: 'a:b:c' };
		expect(parseKeyString(planKeyString(key))).toEqual(key);
	});
});

describe('keyMatchesChange', () => {
	const scope = { workspace_id: 'ws', project_id: 'p1' };

	it('full and compact scopes match the same plan changes', () => {
		const change = {
			plan_id: 'x', revision: 1, owner: { kind: 'thread', id: 't1' }, scope,
		};
		expect(keyMatchesChange({ kind: 'workspace' }, null, change)).toBe(true);
		expect(keyMatchesChange({ kind: 'activity-workspace' }, null, change)).toBe(true);
		expect(keyMatchesChange({ kind: 'project', id: 'p1' }, null, change)).toBe(true);
		expect(keyMatchesChange({ kind: 'activity-project', id: 'p1' }, null, change)).toBe(true);
		expect(keyMatchesChange({ kind: 'project', id: 'p2' }, null, change)).toBe(false);
		expect(keyMatchesChange({ kind: 'activity-project', id: 'p2' }, null, change)).toBe(false);
	});

	it('thread matches only its own plan owner', () => {
		const key: PlanKey = { kind: 'thread', id: 't1' };
		expect(keyMatchesChange(key, null, {
			plan_id: 'x', revision: 1, owner: { kind: 'thread', id: 't1' }, scope,
		})).toBe(true);
		expect(keyMatchesChange(key, null, {
			plan_id: 'x', revision: 1, owner: { kind: 'thread', id: 't2' }, scope,
		})).toBe(false);
	});

	it('child matches its resolved owner thread, never the node id', () => {
		const key: PlanKey = { kind: 'child', id: 'node-1' };
		expect(keyMatchesChange(key, 'child-thread', {
			plan_id: 'x', revision: 1,
			owner: { kind: 'thread', id: 'child-thread' }, scope,
		})).toBe(true);
		expect(keyMatchesChange(key, 'child-thread', {
			plan_id: 'x', revision: 1,
			owner: { kind: 'thread', id: 'node-1' }, scope,
		})).toBe(false);
	});

	it('an unresolved child never matches', () => {
		const key: PlanKey = { kind: 'child', id: 'node-1' };
		expect(keyMatchesChange(key, null, {
			plan_id: 'x', revision: 1,
			owner: { kind: 'thread', id: 'node-1' }, scope,
		})).toBe(false);
	});
});
