import { expect, test, type APIRequestContext, type Page, type TestInfo } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadPastFirstRunLanding, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { agentsBody, createAgent, createProject, kpiBody, mockAveragePeriods, mockPeriod, mockWindows, selectProjectScope, sizeCoverage } from '../support/measurement-fixture';
import { waitForMainSlideToSettle } from '../support/sidebar';

/**
 * No page scrolls sideways on a phone.
 *
 * Why this flow exists: a page that ships with one sideways-scrolling region
 * reads as broken on a phone, and each earlier fix was a one-page spec that only
 * covered the page it was written for. This flow covers every page by
 * construction: it discovers the routes and the in-app views from the source and
 * fails when one has no way to be opened here, so a new page is either measured
 * or named as unregistered.
 *
 * What it measures: every element a reader can drag sideways, which is the
 * document, the body, and any element whose computed `overflow-x` is `auto` or
 * `scroll`. The app shell scrolls inside its own pane, so the document alone can
 * read clean while the pane scrolls sideways.
 */

const PHONE = { width: 393, height: 851 };
const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

/**
 * Regions that scroll sideways on purpose, each named by the exact region a
 * reader reaches. Add one only with a reason the content stays usable: the
 * Agents table keeps its own focusable, labelled region, so the page around it
 * does not move. A selector must name that one region (role and label), never a
 * shared class, so an unrelated wrapper does not inherit the exemption.
 */
const ALLOWED_SCROLL_REGIONS: { selector: string; why: string }[] = [
	{
		selector: '.table-wrap[role="region"][aria-label^="Agents table"]',
		why: 'the Agents table is a labelled, focusable region that scrolls on its own',
	},
];

/** Routes that no reader reaches, each with the reason it is not measured. */
const EXCLUDED_ROUTES: Record<string, string> = {
	'/_design': 'internal design showcase, not a reader surface',
	'/_design/artifacts': 'internal design showcase for the artifact pane, not a reader surface',
	'/_design/context-chip-probe': 'internal probe for the context chip, not a reader surface',
	'/_design/focus-outcomes': 'internal design showcase for Focus outcomes, not a reader surface',
};

// ---- Discovery from source ------------------------------------------------

function read(relative: string): string {
	return fs.readFileSync(path.join(FRONTEND, relative), 'utf8');
}

/** Every `+page.svelte` under `src/routes`, as the URL it serves. */
function discoverRoutes(): string[] {
	const root = path.join(FRONTEND, 'src/routes');
	const found: string[] = [];
	const walk = (dir: string) => {
		for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
			if (entry.isDirectory()) walk(path.join(dir, entry.name));
			else if (entry.name === '+page.svelte') {
				const segments = path.relative(root, dir).split(path.sep).filter((s) => s && !/^\(.*\)$/.test(s));
				found.push(`/${segments.join('/')}`);
			}
		}
	};
	walk(root);
	return found.sort();
}

/** The views the shell switches between, from the `View` union. */
function discoverViews(): string[] {
	const match = read('src/lib/homePaneConfig.ts').match(/export type View\s*=\s*([^;]+);/);
	if (!match) throw new Error('could not find `export type View` in src/lib/homePaneConfig.ts');
	return [...match[1].matchAll(/'([^']+)'/g)].map((m) => m[1]);
}

/**
 * The rows a reader can actually reach on a phone: SettingsView's root list
 * (source `SettingsView.svelte` 54-67, 158-168). Matching id/label pairs out of
 * source text was the wrong contract — a section written differently or derived
 * from a list fell out of coverage while the nonempty assertion stayed green. The
 * rendered list is the component's output at the width the guard measures, so a
 * new, renamed or derived row shows up here whether or not the source still
 * matches one regex.
 */
const MOBILE_SETTINGS_ROOT = '.settings-view .mobile-root';
const MOBILE_SETTINGS_ROW = `${MOBILE_SETTINGS_ROOT} [data-settings-section] button.face`;

/**
 * The contract the labels must satisfy before anything is measured: there is
 * something to click, each one is unambiguous to target, and each one is
 * registered with its own readiness proof. Membership is own-property only, so a
 * label like `constructor` or `toString` cannot borrow its way past the registry
 * and be measured against the previous tab's content. An unregistered tab
 * therefore refuses measurement instead of quietly going unmeasured, which is
 * what coverage means.
 */
function requireMeasuredTabLabels(labels: string[], registered: Record<string, string>, where: string): string[] {
	const trimmed = labels.map((label) => label.trim());
	if (!trimmed.length) throw new Error(`${where}: no settings tab was discovered, so nothing would be measured`);
	const blank = trimmed.filter((label) => !label).length;
	if (blank) throw new Error(`${where}: ${blank} tab(s) render no text, so they cannot be clicked or read`);
	const duplicates = [...new Set(trimmed.filter((label, i) => trimmed.indexOf(label) !== i))];
	if (duplicates.length) throw new Error(`${where}: tab labels ${duplicates.join(', ')} repeat, so targeting is ambiguous`);
	const unregistered = trimmed.filter((label) => !Object.hasOwn(registered, label));
	if (unregistered.length) throw new Error(`${where}: tab labels ${unregistered.join(', ')} have no readiness proof: add them to SETTINGS_TAB_CONTENT or the tab is unmeasured`);
	return trimmed;
}

/** Discovered from the live root list, scoped so hidden desktop navigation is never counted. */
async function discoverSettingTabs(page: Page): Promise<string[]> {
	const root = page.locator(MOBILE_SETTINGS_ROOT);
	await expect(root, 'the settings navigation shows its phone root list').toBeVisible({ timeout: 20_000 });
	const rows = page.locator(MOBILE_SETTINGS_ROW);
	await expect(rows, 'the phone root list is populated before its rows are measured').not.toHaveCount(0, { timeout: 20_000 });
	return requireMeasuredTabLabels(await rows.allInnerTexts(), SETTINGS_TAB_CONTENT, 'the rendered phone settings root list');
}

// ---- Measuring ------------------------------------------------------------

type Offender = { element: string; scrollWidth: number; clientWidth: number; overruns: Overrun[] };

/**
 * Failure-only evidence for the element that actually reaches past the failing
 * pane's right edge. `box` is the element's own rect; `text` is the widest
 * raw fragment geometry of a direct text node, which matters because a stretched flex
 * item (`.card-value`) keeps its box inside its cell while its text spills past
 * it, so a box-only reading can find nothing. Carries geometry and CSS only --
 * never page text.
 */
type Overrun = {
	kind: 'box' | 'text';
	element: string;
	rightEdge: number;
	width: number;
	minWidth: string;
	whiteSpace: string;
	position: string;
	clip: string;
	clipPath: string;
};

async function sidewaysScrollers(page: Page): Promise<Offender[]> {
	const allowed = ALLOWED_SCROLL_REGIONS.map((region) => region.selector);
	return page.evaluate((allowedSelectors) => {
		const name = (el: Element) => `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ''}${typeof el.className === 'string' && el.className ? `.${el.className.trim().split(/\s+/).slice(0, 3).join('.')}` : ''}`;
		const styleOf = (el: Element) => {
			const style = getComputedStyle(el);
			return { minWidth: style.minWidth, whiteSpace: style.whiteSpace, position: style.position, clip: style.clip, clipPath: style.clipPath };
		};
		/** Potential overrun from raw direct-text fragment geometry; painting is unproved. */
		const textOverrun = (el: Element, paneRight: number): Overrun | null => {
			// Range geometry ignores clipping. Exclude explicit self-clipping only
			// from text ranking; keep box records and offender detection unchanged.
			const own = getComputedStyle(el);
			if (own.clip !== 'auto' || own.clipPath !== 'none') return null;
			let widest: Overrun | null = null;
			let widestRight = -Infinity;
			for (const node of Array.from(el.childNodes)) {
				if (!(node instanceof Text) || !node.nodeValue?.trim()) continue;
				const range = document.createRange();
				range.selectNodeContents(node);
				for (const rect of Array.from(range.getClientRects())) {
					if (rect.right > widestRight) {
						widestRight = rect.right;
						widest = { kind: 'text', element: name(el), rightEdge: Math.round(rect.right - paneRight), width: Math.round(rect.width), ...styleOf(el) };
					}
				}
			}
			return widest;
		};
		const candidates = [document.documentElement, document.body, ...Array.from(document.querySelectorAll<HTMLElement>('*'))];
		return candidates
			.filter((el) => el === document.documentElement || el === document.body || /auto|scroll/.test(getComputedStyle(el).overflowX))
			.filter((el) => el.scrollWidth > el.clientWidth + 1)
			.filter((el) => !allowedSelectors.some((selector) => el.matches(selector)))
			.map((el) => {
				const paneRight = el.getBoundingClientRect().right;
				const overruns: Overrun[] = [];
				const record = (target: Element) => {
					const box = target.getBoundingClientRect();
					overruns.push({ kind: 'box', element: name(target), rightEdge: Math.round(box.right - paneRight), width: Math.round(box.width), ...styleOf(target) });
					const text = textOverrun(target, paneRight);
					if (text) overruns.push(text);
				};
				record(el);
				for (const child of Array.from(el.querySelectorAll<HTMLElement>('*'))) {
					// A descendant inside a deliberately scrolling region (the Agents
					// table) stays inside its own box, so it is excluded here rather
					// than allowed to report as the widest overrun and mask the cause.
					if (allowedSelectors.some((selector) => child.closest(selector) !== null)) continue;
					record(child);
				}
				return {
					element: name(el),
					scrollWidth: el.scrollWidth,
					clientWidth: el.clientWidth,
					overruns: overruns.filter((entry) => entry.rightEdge > 1).sort((a, b) => b.rightEdge - a.rightEdge).slice(0, 5),
				};
			});
	}, allowed);
}

/** The unchanged offender verdict, plus potential box and text overruns. */
function overrunDetail(offender: Offender): string {
	if (!offender.overruns.length) return ' [no box or text overrun found; cause unobserved]';
	return ` [widest overruns: ${offender.overruns
		.slice(0, 5)
		.map((entry) => `${entry.kind === 'text' ? 'text (potential; unclippedness unproved)' : 'box'} ${entry.element} right+${entry.rightEdge}px w=${entry.width} min-width=${entry.minWidth} white-space=${entry.whiteSpace} position=${entry.position} clip=${entry.clip} clip-path=${entry.clipPath}`)
		.join(' | ')}]`;
}

// ---- Populated disposable data -------------------------------------------

type Seed = { projectId: string; projectName: string; agentId: string; agentTitle: string };

/** One unbroken token wider than a phone: what a pasted URL or hash does to a row. */
const LONG_TOKEN = `example.test/${'z'.repeat(90)}`;
/** An unbroken 60-character identifier, as a path-like or underscored thread title is, in an automatic finding. */
const FINDING_AGENT = `lane_${'n'.repeat(60)}`;
const FINDING_ACTION = `Review ${'a'.repeat(60)}`;
/** A local model selector as the catalog serves it, about 50 characters with no space to wrap at. */
const LOCAL_MODEL = 'omlx@ns-f378151a51/Qwen3.8-Flash-Next-MLX-oQ6-MTP';
const DEBUG_VERSION_HASH = '3e8bac2779e44891e427005349d4f498978e2afb';
/**
 * The metadata a CI merge build actually shows: `3e8bac2` is the synthetic merge commit,
 * so its subject carries two unbreakable 40-character hashes. That is what a phone has to
 * stay readable with — a short hand-written subject would let the guard pass on data the
 * build never produces. Label format follows `scripts/generate-build-info.mjs`.
 */
const DEBUG_MERGE_SUBJECT = 'Merge f5cf3fabb180f7a281f1e0c098e6ce1ff7a136dd into c8f1b2c3f2fe45c856d53bdb365057aa002b3659';
const DEBUG_VERSION = {
	frontend: { label: '2026.10.04.1734', short_hash: '3e8bac2', full_hash: DEBUG_VERSION_HASH, commit_iso: '2026-10-04T17:34:00-07:00', commit_subject: DEBUG_MERGE_SUBJECT, dirty: false, build_iso: '2026-10-04T17:34:00-07:00' },
	backend: { label: '2026.10.04.1734', short_hash: '3e8bac2', full_hash: DEBUG_VERSION_HASH, commit_iso: '2026-10-04T17:34:00-07:00', commit_subject: DEBUG_MERGE_SUBJECT, dirty: false, build_iso: '2026-10-04T17:34:00-07:00', start_iso: '2026-10-04T17:34:00-07:00', current_head: { short_hash: '3e8bac2', full_hash: DEBUG_VERSION_HASH, commit_iso: '2026-10-04T17:34:00-07:00', commit_subject: DEBUG_MERGE_SUBJECT, dirty: false } },
};

/**
 * Create the project and agent on the disposable backend and answer the four
 * reads whose real data the disposable instance cannot populate: measurement,
 * the Activity plan index, the agent's transcript, and Debug version data.
 * `hold` and `versionHold` let negative controls withhold a required read
 * until they choose to release it.
 */
async function seed(
	page: Page,
	request: APIRequestContext,
	testInfo: TestInfo,
	modelsHold: Promise<void> = Promise.resolve(),
	versionHold: Promise<void> = Promise.resolve(),
	costHold: Promise<void> = Promise.resolve(),
): Promise<Seed> {
	const suffix = `${testInfo.project.name}-${Date.now()}`;
	// Long, unbroken tokens are what overflow on a phone; keep them in the data.
	const project = await createProject(request, `Phone guard project ${suffix} with-a-long-unbroken-name-${'x'.repeat(28)}`);
	const agentTitle = `Phone guard agent ${suffix}-${'y'.repeat(28)}`;
	const agentId = await createAgent(request, agentTitle, project.id);
	await page.route('**/api/measurement/**', async (route) => {
		const url = new URL(route.request().url());
		if (url.pathname.endsWith('/agents')) {
			// One automatic finding whose agent name and action are unbroken.
			const finding = {
				agent: { id: agentId, name: FINDING_AGENT, project: { id: project.id, name: project.name } },
				metric: 'fresh_tokens_per_outcome', current: 140, mean: 100, sd: 12, n: 20, standardized_size: 3.3,
				zero_variance_baseline: false, relative_adverse_change: 0.4, minimum_n: 20,
				window: { from: '2026-08-03', to: '2026-09-28' }, action: FINDING_ACTION,
			};
			return route.fulfill({ json: { ...agentsBody(project, agentId, agentTitle, url), findings_state: 'flagged', findings: [finding] } });
		}
		if (url.pathname.endsWith('/key-figures')) return route.fulfill({ json: keyFiguresBody(project.id) });
		if (url.pathname.endsWith('/where-to-look')) {
			// One row whose agent name and action are unbroken: the Biggest changes card must wrap them.
			const row = (subject: { id: string; name: string; kind: 'agent' | 'scope' }, latest: number, previous: number, change: number) => ({
				subject, measure: 'fresh_tokens_per_outcome', label: 'Fresh tokens per outcome', format: 'tokens',
				previous, latest, change, n: 40, overall: 100_000, action: FINDING_ACTION,
			});
			return route.fulfill({ json: {
				period: mockPeriod(url.searchParams.get('grain')), minimum_n: 20,
				worse: [row({ id: agentId, name: FINDING_AGENT, kind: 'agent' }, 140_000, 100_000, 0.4)],
				improved: [row({ id: project.id, name: project.name, kind: 'scope' }, 80_000, 100_000, -0.2)],
			} });
		}
		if (url.pathname.endsWith('/models')) {
			await modelsHold;
			return route.fulfill({ json: {
				rows: [
					{ model: 'claude-code/claude-opus-5-5', label: 'Claude Opus 5.5', tasks: 20, failed_turns_per_100: 1.5, spend_usd: 51, recorded_cost_per_task: 2.55, cost_class: 'priced', rate_reason: 'none', cost_change: -0.1, change_reason: 'none', previous_tasks: 18, not_offered_now: false, other: false },
					{ model: 'codex/gpt-6.1-sol', label: 'gpt-6.1-sol', tasks: 14, failed_turns_per_100: null, spend_usd: 18.3, recorded_cost_per_task: 1.31, cost_class: 'priced', rate_reason: 'none', cost_change: null, change_reason: 'new', previous_tasks: 0, not_offered_now: false, other: false },
					// An unresolved local selector is labelled by the selector itself: one unbroken name the table must wrap.
					{ model: LOCAL_MODEL, label: LOCAL_MODEL, tasks: 14, failed_turns_per_100: 12.5, spend_usd: null, recorded_cost_per_task: null, cost_class: 'local', rate_reason: 'local', cost_change: null, change_reason: 'local', previous_tasks: 3, not_offered_now: false, other: false },
				],
				token_shares: [
					{ model: 'claude-code/claude-opus-5-5', label: 'Claude Opus 5.5', share_of_tokens: 0.35, tokens: 3500000000 }, { model: 'codex/gpt-6.1-sol', label: 'gpt-6.1-sol', share_of_tokens: 0.2, tokens: 2000000000 },
					{ model: 'codex/gpt-5.6-sol', label: 'gpt-5.6-sol', share_of_tokens: 0.12, tokens: 1200000000 }, { model: 'claude-code/claude-sonnet-5-5', label: 'Claude Sonnet 5.5', share_of_tokens: 0.1, tokens: 1000000000 },
					{ model: 'codex/gpt-5.6-terra', label: 'gpt-5.6-terra', share_of_tokens: 0.08, tokens: 800000000 }, { model: 'codex/gpt-5.6-luna', label: 'gpt-5.6-luna', share_of_tokens: 0.06, tokens: 600000000 },
					{ model: 'qwen3.8-flash', label: 'qwen3.8-flash', share_of_tokens: 0.05, tokens: 500000000 }, { model: 'deepseek-v4.1-flash', label: 'deepseek-v4.1-flash', share_of_tokens: 0.04, tokens: 400000000 },
				],
				min_tasks: 14, unmatched_failed_turns: 0, unresolved_token_share: 0.02, total_spend_usd: 69.3,
			} });
		}
		const ids = url.searchParams.get('ids')?.split(',').filter(Boolean) ?? [];
		return route.fulfill({ json: {
			period: mockPeriod(url.searchParams.get('grain')),
			average_periods: mockAveragePeriods(url.searchParams.get('grain')),
			// The top row reads each series' served windows: the pooled figure and the n behind it, for both windows.
			windows: mockWindows(ids),
			rows: ids.flatMap((series) => series !== 'estimated_human_hours_per_active_hour'
				? ['2026-08-03', '2026-08-10'].map((period) => ({
					period, series, value: series === 'outcomes' ? 20 : 1, average: series === 'outcomes' ? 20 : 1, n: 20, proxy: false,
					partial: false, comparable: true, break: null, dimension: null, thin: false,
				}))
				// The size headline reads two ways: the project scope serves a comparable six-week run, so it draws its
				// trend chart with a value on every week; the agent scope serves a non-comparable latest week, so it
				// reads its comparability line on a phone.
				: url.searchParams.get('agent') === agentId
					? [['2026-09-21', 70.4], ['2026-09-28', sizeCoverage.estimated_hours / sizeCoverage.active_hours]].map(([period, value]) => ({
						period, series, value, average: null, n: sizeCoverage.sized, proxy: true, partial: true, comparable: false, break: null, dimension: null, thin: false, size: sizeCoverage,
					}))
					: [['2026-08-24', 48.2], ['2026-08-31', 61.75], ['2026-09-07', 55.1], ['2026-09-14', 83.31], ['2026-09-21', 70.4], ['2026-09-28', 79.06]].map(([period, value]) => ({
						period, series, value, average: value, n: sizeCoverage.sized, proxy: true, partial: true, comparable: true, break: null, dimension: null, thin: false, size: sizeCoverage,
					}))),
			definitions: [],
		} });
	});
	await page.route('**/api/measurement/cost**', async (route) => {
		await costHold;
		const period = (day: string, dollars: number, outcomes: number, perOutcome: number) => ({
			period: day, dollars, priced_records: 800, usage_records: 1_000, outcomes, per_outcome: perOutcome,
			sized: 120, hours: 2_400, per_hour: dollars / 2_400, average: dollars / 2_400,
		});
		const previous = period('2026-09-21', 5_300_000_000.5, 1425, 3_719_298.25);
		const latest = period('2026-09-28', 10_600_000_000.5, 3746, 2_829_683.5);
		const pooled = ({ period: from, average: _average, ...figures }: ReturnType<typeof period>) => ({ from, to: from, ...figures });
		return route.fulfill({ json: {
			definition_version: 1, period: mockPeriod(new URL(route.request().url()).searchParams.get('grain')), series: [previous, latest], latest: pooled(latest), previous: pooled(previous),
			coverage: { priced_records: 1_600, usage_records: 2_000 }, not_api_priced_records: 3,
			excluded: [{ kind: 'contract_unproven', text: 'usage records whose token counting format is not proven', records: 400 }],
			actual: { subscription_records: 1_600, unknown_charge_records: 0, external_unset_records: 0, direct_api_dollars: 4.2, complete: true, workspace_subscription: { subscription_dollars: null, overage_dollars: null, subscription_facts: 0, overage_facts: 0, subscription_coverage: 'none', overage_coverage: 'none', overlapping_overage: [], allocation: 'unavailable' } },
		} });
	});
	await page.route('**/api/version', async (route) => {
		await versionHold;
		return route.fulfill({ json: DEBUG_VERSION });
	});
	// One active plan for the seeded agent, so Activity renders a project group and a row.
	await page.route('**/api/plans/activity/scopes/workspace', async (route) => {
		const at = '2026-10-01T12:00:00Z';
		return route.fulfill({ json: {
			contract_version: 1, representation: 'activity_scope_index', as_of: at, revision_token: 'phone-guard',
			target: { kind: 'workspace_scope', id: 'workspace', label: 'Workspace' },
			attention: { awaiting_count: 0 },
			plans: [{
				plan: { id: 'phone-guard-plan', owner_kind: 'thread', owner_id: agentId, state: 'active', revision: 1, updated_at: at },
				owner: { label: agentTitle, project_id: project.id, thread_id: agentId },
				progress: { open: 2, liveness: 'advancing' }, has_nodes: true,
				display: { label_key: 'working', tone_key: 'active' }, priority_at: at,
			}],
		} });
	});
	await page.route(`**/api/threads/${agentId}/messages**`, async (route) => {
		const message = (id: string, role: 'user' | 'assistant', content: string, n: number) => ({
			id, thread_id: agentId, role, content, created_at: `2026-10-01T12:00:00.00000${n}`, metadata: null,
		});
		return route.fulfill({ json: {
			messages: [
				message('phone-guard-1', 'user', `Please check ${LONG_TOKEN} for me`, 1),
				message('phone-guard-2', 'assistant', `Checked ${LONG_TOKEN}: it resolves.`, 2),
			],
			has_more: false, oldest_id: null,
		} });
	});
	return { projectId: project.id, projectName: project.name, agentId, agentTitle };
}

// ---- Proving the surface before measuring ----------------------------------

/**
 * A surface is measured only after these hold, each a web-first assertion that
 * waits for the page instead of a fixed delay: the reader is signed in (the
 * seeded agent, which only an authenticated read returns, is listed),
 * and nothing on the page is still loading or has failed.
 */
async function signedIn(page: Page, data: Seed, timeout = 20_000) {
	await expect(page.getByRole('button', { name: 'Toggle sidebar', exact: true })).toBeVisible({ timeout });
	await expect(page.getByText(data.agentTitle).first(), 'the seeded agent is listed, so the read was authenticated').toBeAttached({ timeout });
}

async function loaded(page: Page, scope: string, timeout = 20_000) {
	const root = page.locator(scope);
	await expect(root, `${scope} is on screen`).toBeVisible({ timeout });
	await expect(root.locator('.skeleton, [aria-busy="true"], [aria-label^="Loading"]'), `${scope} has stopped loading`).toHaveCount(0, { timeout });
	await expect(root.locator('[role="alert"]'), `${scope} shows no error`).toHaveCount(0, { timeout });
}

// ---- Openers and readiness: how a reader reaches each view, and what proves it is there ----

async function openSidebar(page: Page) {
	// On a phone the sidebar is off-canvas until toggled; wait for its actual open state before using its rail.
	const sidebar = page.locator('aside.sidebar');
	if (!(await sidebar.evaluate((element) => element.classList.contains('open')))) {
		await page.getByRole('button', { name: 'Toggle sidebar', exact: true }).click();
		await expect(sidebar, 'the phone sidebar opened before rail navigation').toHaveClass(/\bopen\b/);
		// The sidebar opens before the main surface finishes sliding across the rail.
		// Wait on the transform transition itself. A `transitionend` listener armed
		// next to the click can attach after the transition ended, and then never fires.
		await waitForMainSlideToSettle(page);
		return;
	}
	await expect(sidebar, 'the phone sidebar opened before rail navigation').toHaveClass(/\bopen\b/);
}

async function clickRail(page: Page, title: string) {
	await openSidebar(page);
	await page.locator(`.rail-nav :is(button, a)[title="${title}"]`).click();
}

type Target = {
	open: (page: Page, data: Seed) => Promise<void>;
	/** Proves the expected surface is showing, with its populated content, and nothing is loading. */
	ready: (page: Page, data: Seed, timeout?: number) => Promise<void>;
};

/** The endpoint's exact shape, populated for the layout: the widest key figures the row can show. */
function keyFiguresBody(projectId: string) {
	return { definition_version: 1, scope: { project: projectId, agent: null }, timezone: 'America/Los_Angeles', today: '2026-10-08', kpi: kpiBody };
}

// Reading order of a drilled Measurement scope: key figures, size headline, Cost with Models inside it, the kept charts, Biggest changes, then the Agents table inside it.
const MEASUREMENT_ORDER = ['section.kpis', 'section.sizing-headline', 'section[aria-label="Cost"]', 'section[aria-label="Models comparison"]', '.kept-panels', '#measurement-where-to-look', 'section.agents'];

const MEASUREMENT_READY: Target['ready'] = async (page, data, timeout = 20_000) => {
	// The page's title is its scope's name: the project's, or the agent's when the scope narrows to one.
	const escaped = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
	await expect(page.locator('.shell-header .h-title'), 'the Measurement view is open, titled by its scope in the app header').toHaveText(new RegExp(`^(${escaped(data.projectName)}|${escaped(data.agentTitle)})$`), { timeout });
	await expect(page.locator('section.sizing-headline'), 'the drilled-in scope shows its size headline').toBeVisible({ timeout });
	await loaded(page, '.measurement-page', timeout);
	await expect(
		page.getByRole('region', { name: /^Agents table/ }).getByText(data.agentTitle),
		'the Agents table shows the seeded agent',
	).toBeVisible({ timeout });
	await expect(page.getByRole('region', { name: 'Biggest changes' }), 'Biggest changes is shown above the Agents table').toBeVisible({ timeout });
	// The key figures: the widest seeded figures, populated and error-free.
	const figures = page.locator('section.kpis');
	await expect(figures.getByRole('alert'), 'the key figures loaded without an error').toHaveCount(0, { timeout });
	await expect(figures.locator('[data-kpi="tokens"]'), 'the key figures show the seeded token total').toContainText('123B+', { timeout });
	const sizing = page.locator('.sizing-headline');
	await expect(sizing.locator('.stat'), 'the size headline answers with its seeded rate').toContainText('h of work per hour you spend', { timeout });
	await expect(sizing.locator('.stat b'), 'the size headline shows its seeded rate').not.toHaveText('–', { timeout });
	await expect(sizing.locator('.why'), 'the size headline shows why it moved').toContainText('Why it moved', { timeout });
	await expect(sizing.locator('.nf').first(), 'the size notes say what is left out of the hours').toContainText('have no usable size rating', { timeout });
	await expect.poll(
		() => page.evaluate((selectors) => {
			const found = selectors.map((selector) => document.querySelector(selector));
			return found.every((element, index) => element !== null
				&& (index === 0 || Boolean(found[index - 1]!.compareDocumentPosition(element) & Node.DOCUMENT_POSITION_FOLLOWING)));
		}, MEASUREMENT_ORDER),
		{ message: 'the Measurement sections read key figures, size headline, Cost, Models, kept charts, Biggest changes, then Agents', timeout },
	).toBe(true);
	const apiCost = page.getByRole('region', { name: 'Cost', exact: true });
	await expect(apiCost.getByRole('heading', { name: 'Cost per person-hour of work delivered', exact: true }), 'the API-equivalent Cost section is showing').toBeVisible({ timeout });
	await expect(apiCost.locator('.chart'), 'the API-equivalent Cost chart is populated').toHaveCount(1, { timeout });
	await expect(page.locator('section.models'), 'the Models section shows the seeded resolved model').toContainText('Claude Opus 5.5', { timeout });
	await expect(page.locator('section.models th[scope="row"]').filter({ hasText: LOCAL_MODEL }), 'the Models table shows the local model selector').toHaveCount(1, { timeout });
};

function measurementScopeUrl(page: Page, data: Seed): string {
	const url = new URL('/', page.url());
	url.searchParams.set('view', 'measurement');
	url.searchParams.set('project', data.projectId);
	url.searchParams.set('agent', data.agentId);
	url.searchParams.set('grain', 'days');
	return url.toString();
}

async function measurementScopedRouteReady(page: Page, data: Seed, timeout = 20_000): Promise<void> {
	await MEASUREMENT_READY(page, data, timeout);
	// The agent scope serves a non-comparable latest week: its headline says so and draws no trend.
	await expect(page.locator('.sizing-headline .comparability'), 'the size headline shows its comparability line').toBeVisible({ timeout });
	await expect(page.locator('.sizing-headline .chart'), 'a non-comparable latest week draws no chart').toHaveCount(0, { timeout });
	const url = new URL(page.url());
	expect(url.searchParams.get('project'), 'the direct Measurement route keeps its project selection').toBe(data.projectId);
	expect(url.searchParams.get('agent'), 'the direct Measurement route keeps its agent selection').toBe(data.agentId);
	expect(url.searchParams.get('grain'), 'the direct Measurement route keeps its grain selection').toBe('days');
	const agentChip = page.locator('.agent-chip');
	await expect(agentChip, 'the selected agent is visible in the Measurement controls').toContainText(`Agent: ${data.agentTitle}`, { timeout });
	await expect(agentChip.getByRole('button', { name: 'Remove agent filter', exact: true }), 'the selected agent has a named Remove agent filter control').toBeVisible({ timeout });
}

/** Settings tabs, each with what proves its own content (not the previous tab's) is showing. */
const SETTINGS_TAB_CONTENT: Record<string, string> = {
	Providers: 'Accounts and rollover',
	'Connected apps': 'Connected apps',
	Skills: 'All projects',
	Context: 'All projects',
	Projects: 'Active',
	Keys: 'Keys',
	System: 'System',
	Debug: 'Debug',
};

async function settingsTabReady(page: Page, tab: string, timeout = 20_000) {
	const heading = SETTINGS_TAB_CONTENT[tab];
	if (!heading) throw new Error(`Settings tab "${tab}" has no readiness proof: add it to SETTINGS_TAB_CONTENT`);
	await expect(page.getByRole('heading', { name: heading, exact: true }), `the ${tab} tab shows its content`).toBeVisible({ timeout });
	await expect(page.locator('.settings-content .loading'), `the ${tab} tab has stopped loading`).toHaveCount(0, { timeout });
	if (tab === 'Debug') {
		const versionCards = page.locator('.settings-content .settings-card').filter({ has: page.locator('dl.kv') });
		await expect(versionCards, 'all four Debug version cards are populated').toHaveCount(4, { timeout });
		await expect(versionCards.locator('h3'), 'the four canonical Debug version cards are shown in order').toHaveText([
			'Frontend — this browser bundle',
			"Frontend — server's deployed bundle",
			'Backend — running process',
			'Backend — current HEAD on disk',
		], { timeout });
		// The three cards fed by /api/version bind the deterministic payload. The
		// browser-bundle card is deliberately excluded: it shows the bundle this run
		// actually loaded, so it carries a full hash but not the server's values.
		await expect(versionCards.locator(`code.mono-wrap:text-is("${DEBUG_VERSION_HASH}")`), 'the three server-backed cards bind the deterministic full hash').toHaveCount(3, { timeout });
		await expect(versionCards.locator(`code:text-is("${DEBUG_VERSION.frontend.label}")`), 'the two server version cards bind the deterministic build label').toHaveCount(2, { timeout });
		// The merge subject carries two unbreakable 40-character hashes: the very content
		// that must stay inside the card, so readiness waits for it, and the overflow scan
		// that follows is the regression check on it.
		await expect(versionCards.locator(`dd:text-is("${DEBUG_MERGE_SUBJECT}")`), 'the three server-backed cards show the populated commit subject').toHaveCount(3, { timeout });
	}
}

/** One entry per member of the `View` union. A new view must add its target here. */
const VIEW_TARGETS: Record<string, Target> = {
	activity: {
		open: async (page) => {
			await clickRail(page, 'Activity');
		},
		ready: async (page, data, timeout = 20_000) => {
			await expect(page.locator('.rail-nav button[title="Activity"][aria-current="page"]'), 'the Activity view is open').toHaveCount(1, { timeout });
			await expect(page.getByRole('heading', { name: 'Activity', level: 1 })).toBeVisible({ timeout });
			await expect(page.locator('.activity-group').getByText(data.agentTitle).first(), 'Activity lists the seeded plan').toBeVisible({ timeout });
			await loaded(page, '.activity-work', timeout);
		},
	},
	chat: {
		open: async (page, data) => {
			// A reader picks the thread back up from Activity's Continue list.
			await clickRail(page, 'Activity');
			await expect(page.locator('.rail-nav button[title="Activity"][aria-current="page"]'), 'the Activity view is open').toHaveCount(1);
			await expect(page.getByRole('heading', { name: 'Activity', level: 1 })).toBeVisible();
			await loaded(page, '.activity-work');
			const resume = page.locator('.resume-row').filter({ hasText: data.agentTitle });
			await expect(resume, 'Activity Continue lists the seeded plan').toBeVisible();
			await resume.click();
			// The assistant's reply sits under the "Worked" disclosure; a reader opens it to read the answer.
			await page.getByRole('button', { name: 'Expand Worked' }).click();
		},
		ready: async (page, _data, timeout = 20_000) => {
			await expect(page.getByRole('textbox', { name: 'Message CORSIA' }), 'the thread is open').toBeVisible({ timeout });
			await expect(page.locator('.message').filter({ hasText: LONG_TOKEN }), 'the transcript shows both seeded messages').toHaveCount(2, { timeout });
		},
	},
	settings: {
		open: async (page) => {
			await clickRail(page, 'Settings');
		},
		ready: async (page, _data, timeout = 20_000) => {
			await expect(page.locator('.settings-view'), 'the Settings view is open').toBeVisible({ timeout });
			if ((page.viewportSize()?.width ?? 0) <= 768) {
				await expect(page.locator(MOBILE_SETTINGS_ROOT), 'the Settings phone root list is open').toBeVisible({ timeout });
				return;
			}
			await settingsTabReady(page, 'Providers', timeout);
		},
	},
	measurement: {
		open: async (page, data) => {
			await clickRail(page, 'Measurement');
			await expect(page.locator('.shell-header .h-title')).toHaveText(new RegExp(`^(All work|${data.projectName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})$`), { timeout: 20_000 });
			await expect(page.getByRole('button', { name: /Measurement scope:/ }), 'the scope picker is showing').toBeVisible({ timeout: 20_000 });
			// The seeded project's scope serves the comparable run the ready check reads.
			if (await page.getByRole('button', { name: `Measurement scope: ${data.projectName}`, exact: true }).isVisible()) return;
			// Earlier runs leave projects whose names share the first 20 characters, so the whole name picks this run's.
			await selectProjectScope(page, data.projectName);
		},
		// The project scope serves a comparable run, so its headline draws the trend chart.
		ready: async (page, data, timeout = 20_000) => {
			await MEASUREMENT_READY(page, data, timeout);
			await expect(page.locator('.sizing-headline .mob svg .vl'), 'the size headline chart prints a value on every week').toHaveCount(6, { timeout });
		},
	},
	documents: {
		open: async (page, data) => {
			// Documents has no rail link; the reader arrives from a thread's pane.
			await VIEW_TARGETS.chat.open(page, data);
			await VIEW_TARGETS.chat.ready(page, data);
			const browse = page.getByRole('button', { name: 'Browse documents…' });
			if (!(await browse.isVisible())) await page.getByRole('button', { name: 'Toggle panel' }).click();
			await browse.evaluate((button: HTMLElement) => button.click());
		},
		ready: async (page, _data, timeout = 20_000) => {
			await expect(page.getByPlaceholder('Search docs…'), 'the Documents view is open').toBeVisible({ timeout });
			await expect(page.locator('.row-skel'), 'the document library has loaded').toHaveCount(0, { timeout });
			await expect(page.locator('.lib-error'), 'the document library did not fail').toHaveCount(0, { timeout });
			await expect(page.locator('.body .row').first(), 'the document library lists documents').toBeVisible({ timeout });
		},
	},
};

/**
 * A routed page other than the shell, by the pattern `discoverRoutes` finds.
 * It names a concrete URL (a dynamic segment needs a real value) and what
 * proves that page, not a redirect, is showing. None exists yet.
 */
const ROUTE_TARGETS: Record<string, { url: (data: Seed) => string; ready: Target['ready'] }> = {};

// ---- Measuring: signed in, on the expected surface, populated, then scrolled ----

async function measure(page: Page, ready: () => Promise<void>): Promise<Offender[]> {
	await ready();
	// Web fonts change line breaks; wait for them to load rather than for a delay.
	await page.evaluate(() => document.fonts.ready.then(() => true));
	return sidewaysScrollers(page);
}

/** The readiness proof for a shell view: signed in, then the view's own content. */
const viewReady = (page: Page, data: Seed, view: string) => async () => {
	await signedIn(page, data);
	await VIEW_TARGETS[view].ready(page, data);
};

// ---- The flow --------------------------------------------------------------

test.describe('@flow:no-sideways-scroll @surface:shell no page scrolls sideways on a phone', () => {
	test.use({ viewport: PHONE });
	// One phone project is enough; the desktop project would run the same 393px page twice.
	test.beforeEach(({}, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-mobile', 'the phone guard runs once, on the phone project');
	});

	test('every route and view has a way to be opened here', () => {
		const views = discoverViews();
		expect(views.length, 'the View union was found').toBeGreaterThan(0);
		expect(
			views.filter((view) => !(view in VIEW_TARGETS)),
			'a view with no target is unmeasured: add one to VIEW_TARGETS',
		).toEqual([]);
		expect(
			Object.keys(VIEW_TARGETS).filter((view) => !views.includes(view)),
			'a target for a view that no longer exists: remove it from VIEW_TARGETS',
		).toEqual([]);

		const routes = discoverRoutes();
		expect(routes, 'the shell route was found').toContain('/');
		const excluded = Object.keys(EXCLUDED_ROUTES);
		expect(excluded.filter((route) => !routes.includes(route)), 'an excluded route that no longer exists').toEqual([]);
		expect(
			Object.keys(ROUTE_TARGETS).filter((route) => !routes.includes(route)),
			'a route target for a route that no longer exists',
		).toEqual([]);
		expect(
			routes.filter((route) => route !== '/' && !excluded.includes(route) && !(route in ROUTE_TARGETS)),
			'a routed page with no target is unmeasured: add one to ROUTE_TARGETS, or exclude it in EXCLUDED_ROUTES with a reason',
		).toEqual([]);
		const sample: Seed = { projectId: 'p', projectName: 'p', agentId: 'a', agentTitle: 't' };
		for (const [route, target] of Object.entries(ROUTE_TARGETS)) {
			expect(target.url(sample), `${route} needs a concrete URL for its dynamic segments`).not.toMatch(/\[/);
		}
	});

	for (const scheme of ['light', 'dark'] as const) {
		test(`nothing scrolls sideways at 393px in ${scheme}`, async ({ page, request }, testInfo) => {
			test.setTimeout(120_000);
			await page.emulateMedia({ colorScheme: scheme });
			const data = await seed(page, request, testInfo);
			const failures: string[] = [];

			const check = async (label: string, ready: () => Promise<void>) => {
				const offenders = await measure(page, ready);
				if (offenders.length) {
					failures.push(`${label}: ${offenders.map((o) => `${o.element} ${o.scrollWidth}>${o.clientWidth}${overrunDetail(o)}`).join('; ')}`);
					await page.screenshot({ path: testInfo.outputPath(`${scheme}-${label.replace(/\W+/g, '-')}.png`) });
				}
			};

			// Routes: each routed page, loaded cold, signed in, at its concrete URL.
			for (const [route, target] of Object.entries(ROUTE_TARGETS)) {
				await loginByUrl(page, { waitForComposer: false });
				const url = target.url(data);
				await page.goto(url);
				await check(`route ${route}`, async () => {
					expect(new URL(page.url()).pathname, `${route} was not redirected`).toBe(new URL(url, page.url()).pathname);
					await signedIn(page, data);
					await target.ready(page, data);
				});
			}

			// Views: the shell's own pane scrolls, so each is opened as a reader would.
			await loadPastFirstRunLanding(page, () => loginByUrl(page, { waitForComposer: false }), { lands: true });
			for (const view of discoverViews()) {
				await VIEW_TARGETS[view].open(page, data);
				await check(`view ${view}`, viewReady(page, data, view));
				if (view === 'settings') {
					for (const tab of await discoverSettingTabs(page)) {
						await page.locator(MOBILE_SETTINGS_ROOT).getByRole('button', { name: tab, exact: true }).click();
						await check(`settings ${tab}`, () => settingsTabReady(page, tab));
						await page.locator('.shell-header .h-back').click();
						await expect(page.locator(MOBILE_SETTINGS_ROOT), `the ${tab} section returns to the Settings root list`).toBeVisible();
					}
				}
			}

			// Keep the project-drill coverage above, and separately prove that a reader's
			// direct project-and-agent Measurement URL survives entering and refreshing it.
			await page.goto(measurementScopeUrl(page, data));
			await check('measurement direct project-agent route', () => measurementScopedRouteReady(page, data));
			await page.reload();
			await check('measurement direct project-agent route after refresh', () => measurementScopedRouteReady(page, data));

			expect(failures, 'pages with a sideways-scrolling region (add an allowlist entry only for the Agents table)').toEqual([]);
		});
	}

	// ---- Negative controls: the guard must fail for the intended reason ----
	// A short timeout is safe only when the first readiness step is the one expected to fail.
	// Where earlier steps must pass first, keep the default: under runner load they can take
	// longer than a short budget and the control then fails for the wrong reason.

	test.describe('the guard refuses to measure what it has not proven', () => {
		test.beforeEach(() => test.setTimeout(60_000));

		test('a signed-out page is not measured as the app', async ({ page, request }, testInfo) => {
			const data = await seed(page, request, testInfo);
			await page.goto('/');
			await expect(signedIn(page, data, 3_000)).rejects.toThrow(/seeded agent is listed|Toggle sidebar/);
		});

		test('the wrong view is not measured as the expected one', async ({ page, request }, testInfo) => {
			const data = await seed(page, request, testInfo);
			await loginByUrl(page, { waitForComposer: false });
			await VIEW_TARGETS.activity.open(page, data);
			await viewReady(page, data, 'activity')();
			await expect(VIEW_TARGETS.measurement.ready(page, data, 3_000)).rejects.toThrow(/the Measurement view is open/);
		});

		test('content that arrives late is waited for, not measured around', async ({ page, request }, testInfo) => {
			let release!: () => void;
			const hold = new Promise<void>((resolve) => { release = resolve; });
			const data = await seed(page, request, testInfo, hold);
			await loginByUrl(page, { waitForComposer: false });
			await VIEW_TARGETS.measurement.open(page, data);
			let released = false;
			const timer = setTimeout(() => { released = true; release(); }, 2_000);
			try {
				await measure(page, viewReady(page, data, 'measurement'));
				expect(released, 'the measurement waited for the withheld Models list').toBe(true);
			} finally {
				clearTimeout(timer);
				release();
			}
		});

		test('content that never arrives fails as not ready, not as a clean page', async ({ page, request }, testInfo) => {
			const data = await seed(page, request, testInfo, new Promise<void>(() => undefined));
			await loginByUrl(page, { waitForComposer: false });
			await VIEW_TARGETS.measurement.open(page, data);
			await expect(measure(page, () => MEASUREMENT_READY(page, data))).rejects.toThrow(/Models list shows the seeded model|has stopped loading/);
		});

		test('withheld Cost data fails as not ready, not as a clean page', async ({ page, request }, testInfo) => {
			const data = await seed(page, request, testInfo, Promise.resolve(), Promise.resolve(), new Promise<void>(() => undefined));
			await loginByUrl(page, { waitForComposer: false });
			await VIEW_TARGETS.measurement.open(page, data);
			await expect(measure(page, () => MEASUREMENT_READY(page, data))).rejects.toThrow(/Cost request has settled|has stopped loading/);
		});

		test('withheld Debug version data blocks readiness until it arrives', async ({ page, request }, testInfo) => {
			let release!: () => void;
			const versionHold = new Promise<void>((resolve) => { release = resolve; });
			const data = await seed(page, request, testInfo, Promise.resolve(), versionHold);
			await loginByUrl(page, { waitForComposer: false });
			await VIEW_TARGETS.settings.open(page, data);
			await page.getByRole('button', { name: 'Debug', exact: true }).click();
			try {
				await expect(settingsTabReady(page, 'Debug')).rejects.toThrow(/all four Debug version cards are populated/);
			} finally {
				release();
			}
			await settingsTabReady(page, 'Debug');
		});

		test('a tab the navigation renders but the guard has not registered refuses measurement', () => {
			// The exact shape `allInnerTexts()` yields for a real mobile tab: the svg
			// contributes whitespace around the label, and the label is the section's own.
			const rendered = ['\n\t\t\t\t\t\tProviders\n', ' System ', '\n\t\t\t\t\t\tDebug\n'];
			expect(requireMeasuredTabLabels(rendered, SETTINGS_TAB_CONTENT, 'a phone navigation'), 'a rendered tab set is measured as written').toEqual(['Providers', 'System', 'Debug']);
			// An extra section shows up in the navigation on its own, so it cannot be
			// skipped: unregistered stops the guard instead of reporting a pass.
			const extra = [...rendered, '\n\t\t\t\t\t\tRoles\n'];
			expect(() => requireMeasuredTabLabels(extra, SETTINGS_TAB_CONTENT, 'a phone navigation'), 'a rendered tab with no readiness proof refuses measurement').toThrow(/Roles.*no readiness proof/);
			// A blank or duplicated label cannot be targeted, so it refuses too.
			expect(() => requireMeasuredTabLabels(['Providers', '  '], SETTINGS_TAB_CONTENT, 'a phone navigation'), 'a blank tab label refuses measurement').toThrow(/render no text/);
			expect(() => requireMeasuredTabLabels(['Providers', 'Providers'], SETTINGS_TAB_CONTENT, 'a phone navigation'), 'a duplicated tab label refuses measurement').toThrow(/ambiguous/);
			// A tab named after something on Object.prototype is not registered, even
			// though `label in SETTINGS_TAB_CONTENT` would say it is.
			expect(() => requireMeasuredTabLabels(['constructor'], SETTINGS_TAB_CONTENT, 'a phone navigation'), 'an inherited prototype name is not a registered tab').toThrow(/constructor.*no readiness proof/);
			expect(() => requireMeasuredTabLabels(['toString'], SETTINGS_TAB_CONTENT, 'a phone navigation'), 'an inherited method name is not a registered tab').toThrow(/toString.*no readiness proof/);
			expect(() => requireMeasuredTabLabels([], SETTINGS_TAB_CONTENT, 'a phone navigation'), 'an empty navigation refuses measurement').toThrow(/no settings tab was discovered/);
		});

		test('an overflowing element is found, and the exemption covers only the Agents table', async ({ page, request }, testInfo) => {
			const data = await seed(page, request, testInfo);
			await loginByUrl(page, { waitForComposer: false });
			await VIEW_TARGETS.measurement.open(page, data);
			await measure(page, viewReady(page, data, 'measurement'));
			await page.evaluate(() => {
				const wide = '<div style="width:900px;height:8px"></div>';
				const host = document.querySelector('.measurement-page')!;
				host.insertAdjacentHTML('beforeend', `<div id="probe-plain" class="table-wrap" style="overflow-x:auto">${wide}</div>`);
				host.insertAdjacentHTML('beforeend', `<div id="probe-labelled" class="table-wrap" role="region" aria-label="Agents table, probe" style="overflow-x:auto">${wide}</div>`);
			});
			const found = (await sidewaysScrollers(page)).map((o) => o.element);
			expect(found.some((element) => element.includes('probe-plain')), 'an unrelated .table-wrap is not exempt').toBe(true);
			expect(found.some((element) => element.includes('probe-labelled')), 'the labelled Agents table region is exempt').toBe(false);
		});
	});
});
