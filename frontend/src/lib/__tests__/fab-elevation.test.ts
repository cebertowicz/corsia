import { readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

/**
 * Issue #1322 — the shared attention container clipped the shared elevation.
 *
 * `.chat-attention-stack` clips with `overflow: hidden` deliberately, and the
 * clip edge is the element's padding edge — so with no padding, an occupant that
 * fills a track sits flush with the edge and loses its drop shadow.
 *
 * These guards read the authored sources the way `design-tokens.test.ts` does,
 * because the defect is a relationship between two authored files that no jsdom
 * test can see: how much paint room the slot grants, and how much of the band's
 * own geometry that room comes out of. Rendered proof lives in the PR.
 */

/**
 * Which source tree these guards read. Defaults to the working tree; pointing it
 * at a pre-fix copy re-runs the identical assertions, which is how "this test
 * actually fails the original defect" stays provable instead of remembered.
 */
const SRC = process.env.FAB_ELEVATION_SRC
	? resolve(process.env.FAB_ELEVATION_SRC)
	: resolve(__dirname, '../..');

/**
 * Comments explain a declaration; they must never satisfy a guard. Every
 * assertion below runs against declarations only, so a rule can neither pass by
 * being described in prose nor fail because a comment quotes the very value the
 * rule forbids.
 */
const declarationsOnly = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, '');

const read = (...parts: string[]) => declarationsOnly(readFileSync(join(SRC, ...parts), 'utf8'));

/** The declaration block of exactly one authored rule, up to its own `}`. */
function declarations(css: string, selector: string): string {
	const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
	const match = new RegExp(`${escaped}\\s*\\{([^}]*)\\}`).exec(css);
	expect(match, `no rule found for ${selector}`).not.toBeNull();
	return match![1];
}

/** Every rule that declares `overflow`, with the selector that declares it. */
function overflowRules(css: string): Array<{ selector: string; value: string }> {
	const rules: Array<{ selector: string; value: string }> = [];
	for (const match of css.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
		const selector = match[1].trim().replace(/\s+/g, ' ');
		for (const decl of match[2].matchAll(/overflow:\s*([^;]+);/g)) {
			rules.push({ selector, value: decl[1].trim() });
		}
	}
	return rules;
}

function customProperty(css: string, name: string): string {
	const match = new RegExp(`${name}:\\s*([^;]+);`).exec(css);
	expect(match, `${name} is not declared in the authored CSS`).not.toBeNull();
	return match![1].trim();
}

const STACK = 'lib/components/attention/ChatAttentionStack.svelte';
const STACK_CSS = read(STACK);
const NEW_CHAT = read('lib/components/NewChatFab.svelte');

describe('the attention slot grants its occupants room to paint', () => {
	/**
	 * Pre-fix this fails outright: no allowance exists, so the clip edge is the
	 * content edge and every occupant shadow is cut.
	 */
	it('widens the clip box, not the layout, by its own side inset', () => {
		const gutter = customProperty(STACK_CSS, '--chat-attention-shadow-gutter');
		expect(gutter, 'the gutter must be a px length').toMatch(/^(\d+(?:\.\d+)?)px$/);

		// The room has to come from somewhere the band already gives up: it is
		// inset by `calc(100% - 32px)`, i.e. 16px per side. Anything larger puts
		// the clip box outside the viewport below 792px wide and breaks
		// `margin: 0 auto` centring. Coupling the two is the point — widening the
		// band's inset without widening this number must fail here.
		const inset = /width:\s*min\(760px,\s*calc\(100% - (\d+)px\)\)/.exec(STACK_CSS);
		expect(inset, 'the band width must stay authored as min(760px, calc(100% - Npx))').not.toBeNull();
		expect(parseFloat(gutter) * 2).toBe(parseFloat(inset![1]));

		expect(STACK_CSS, 'the gutter must be applied as padding, which is the clip edge').toMatch(
			/padding:\s*var\(--chat-attention-shadow-gutter\)\s*;/,
		);
	});

	/**
	 * On the app's default `border-box`, padding eats the content band instead of
	 * growing the clip, so the allowance only preserves geometry by putting this
	 * one box back to `content-box`.
	 */
	it('keeps the content band byte-identical while the clip box grows', () => {
		expect(STACK_CSS, 'width and max-height must describe the content again').toMatch(
			/box-sizing:\s*content-box\s*;/,
		);
		expect(STACK_CSS, 'the height budget must stay on the content band').toMatch(
			/max-height:\s*var\(--chat-attention-available-height\)\s*;/,
		);
		expect(STACK_CSS, 'the band must stay pinned to the composer').toMatch(/bottom:\s*calc\(/);
		const bottom = /bottom:\s*calc\(([\s\S]*?)\);/.exec(STACK_CSS);
		expect(bottom![1], 'the band must be pulled up by exactly one gutter').toMatch(
			/-\s*var\(--chat-attention-shadow-gutter\)/,
		);
	});

	/**
	 * The slot publishes `offsetHeight` — now the padded clip box — into
	 * `--chat-attention-occupied-height`, and the transcript reserves exactly that
	 * as padding. Without the correction the gutter would silently land on every
	 * transcript.
	 */
	it('publishes the band it shows, not the padded box it measures', () => {
		expect(STACK_CSS, 'the published height must stop being the raw offsetHeight').not.toMatch(
			/const height = element\.firstElementChild \? element\.offsetHeight : 0;/,
		);
		expect(STACK_CSS, 'the gutter must be subtracted from what is published').toMatch(
			/offsetHeight\s*-\s*gutter/,
		);
	});

	/**
	 * `overflow: hidden` is a measured regression guard, not decoration, and the
	 * one place the band may release it is a focused drawer, which deliberately
	 * leaves the chat band and owns the viewport. Both halves are asserted from
	 * the selectors that carry them, so the exception stays narrow instead of
	 * becoming a second global escape.
	 */
	it('keeps the height-budget guard clipping, and releases it only for a drawer', () => {
		const rule = declarations(STACK_CSS, '.chat-attention-stack');
		expect(rule).toMatch(/overflow:\s*(hidden|clip)\s*;/);

		const released = overflowRules(STACK_CSS).filter((entry) => entry.value === 'visible');
		for (const entry of released) {
			expect(
				entry.selector,
				`only a focused drawer may release the clip, not "${entry.selector}"`,
			).toContain('[data-adaptive-drawer]');
		}
	});
});

describe('every floating control carries one elevation', () => {
	/** Pre-fix this fails: `.rail-newchat` hand-rolls an accent-tinted shadow. */
	it('routes the New Chat FAB through the shared elevation token', () => {
		const rule = declarations(NEW_CHAT, '.rail-newchat');
		expect(rule).toMatch(/box-shadow:\s*var\(--glass-elevation-1\)\s*,\s*var\(--glass-rim\)\s*;/);
	});

	/**
	 * The shared token is the whole treatment — not the token plus a bespoke
	 * second shadow kept alongside it, which is how a second elevation system
	 * starts.
	 */
	it('keeps no per-component shadow recipe beside the token', () => {
		for (const selector of ['.rail-newchat', '.rail-newchat:hover', '.rail-newchat.armed']) {
			const rule = declarations(NEW_CHAT, selector);
			for (const line of rule.split('\n')) {
				if (!/box-shadow/.test(line)) continue;
				expect(line, `${selector} still hand-rolls a shadow: ${line.trim()}`).not.toMatch(
					/\b\d+px\s+\d+px\s+\d+px\b/,
				);
				expect(line, `${selector} still tints its own shadow: ${line.trim()}`).not.toMatch(/color-mix/);
			}
		}
	});

	/**
	 * One elevation level for one kind of control, borrowed rather than invented.
	 * The rail peers this FAB must match are ChatScrollControl and PlanDrawerControl.
	 */
	it('matches the elevation its rail peers already use', () => {
		const shared = declarations(NEW_CHAT, '.rail-newchat').match(/box-shadow:\s*([^;]+);/)![1].trim();
		expect(shared).toBe('var(--glass-elevation-1), var(--glass-rim)');
		for (const peer of ['lib/components/ChatScrollControl.svelte', 'lib/components/plans/PlanDrawerControl.svelte']) {
			expect(read(peer), `${peer} no longer carries the shared pair`).toMatch(
				/box-shadow:\s*var\(--glass-elevation-1\)\s*,\s*var\(--glass-rim\)/,
			);
		}
	});
});
