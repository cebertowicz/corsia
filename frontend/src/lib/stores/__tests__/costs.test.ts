import { describe, it, expect } from 'vitest';
import {
	formatCost,
	formatCostWithClass,
	formatKnownCost,
} from '../costs';

describe('formatCost (pre-existing behavior, regression guard)', () => {
	it('keeps unknown dollars distinct from measured zero', () => {
		expect(formatCost(null)).toBe('—');
		expect(formatKnownCost(0.25, 2)).toBe('$0.2500 + 2 unpriced calls');
		expect(formatKnownCost(null, 1)).toBe('— (1 unpriced call)');
		expect(formatKnownCost(0, 0)).toBe('$0.00');
	});
	it('returns $0.00 for zero', () => {
		expect(formatCost(0)).toBe('$0.00');
	});

	it('uses tiered precision', () => {
		expect(formatCost(0.0001234)).toMatch(/^\$0\.000123/);
		expect(formatCost(1.234)).toBe('$1.234');
		expect(formatCost(15.678)).toBe('$15.68');
		expect(formatCost(150)).toBe('$150');
	});
});

describe('formatCostWithClass (issue #116)', () => {
	it('local models render as $0.00 (local), distinct from $0.00', () => {
		expect(formatCostWithClass(0, 'local')).toBe('$0.00 (local)');
		expect(formatCostWithClass(0, 'local')).not.toBe('$0.00');
	});

	it('local-model rendering ignores the numeric value', () => {
		expect(formatCostWithClass(42, 'local')).toBe('$0.00 (local)');
	});

	it('no_pricing models render as em-dash placeholder, not $0.00', () => {
		expect(formatCostWithClass(0, 'no_pricing')).toBe('—');
		expect(formatCostWithClass(0, 'no_pricing')).not.toBe('$0.00');
		expect(formatCostWithClass(0, 'no_pricing')).not.toBe('$0.00 (local)');
	});

	it('priced models fall through to standard formatCost', () => {
		expect(formatCostWithClass(0, 'priced')).toBe('$0.00');
		expect(formatCostWithClass(1.234, 'priced')).toBe('$1.234');
		expect(formatCostWithClass(15.5, 'priced')).toBe('$15.50');
	});

	it('undefined class falls through to standard formatCost (back-compat)', () => {
		expect(formatCostWithClass(0, undefined)).toBe('$0.00');
		expect(formatCostWithClass(1.234, undefined)).toBe('$1.234');
	});
});
