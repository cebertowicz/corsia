/** Geometry for MeasurementAlignmentChart: each period's answered decisions as one 100% stacked bar of its three shares. */
import type { MeasurementRow } from '$lib/stores/measurement';
import { startParts, type BarPoint, type ChartGeometry, type ChartItem } from './measurementBarChart';

/** The three shares of answered decisions, bottom to top, as the stack draws them. */
export const ALIGNMENT_PARTS = [
	{ series: 'took_recommendation_pct', label: 'Took recommendation' },
	{ series: 'answered_other_pct', label: 'Answered Other' },
	{ series: 'answered_neither_pct', label: 'Neither' },
] as const;

export interface AlignmentPeriod {
	/** ISO date (YYYY-MM-DD) of the first day of the period. */
	start: string;
	/** Decisions answered in the period. */
	n: number;
	/** The three shares, 0 to 1, in ALIGNMENT_PARTS order; null when the period does not form one whole partition. */
	shares: [number, number, number] | null;
	/** Why the period draws no stack; null when it draws one. */
	issue: string | null;
	/** The measure had not started: the period holds its place on the shared axis and draws nothing, not even an n. */
	empty?: boolean;
	/** The running day: drawn outlined and labelled today so far, as on the page's bar charts. */
	partial?: boolean;
}

/** The one issue the chart labels too few under the bar; every other issue is told only in the table. */
export const TOO_FEW = 'Too few decisions answered to show the shares.';
export const INCONSISTENT_ANSWERS = 'This period’s answers were recorded inconsistently, so they aren’t shown. This is a CORSIA bug; please report it.';

/** Each drawn period's three shares, checked the way the stack needs them: all three served, measured and summing to one. */
export function alignmentPeriods(rows: MeasurementRow[], points: BarPoint[]): AlignmentPeriod[] {
	return points.map(({ start, empty, partial }) => {
		if (empty) return { start, n: 0, shares: null, issue: null, empty: true };
		const parts = ALIGNMENT_PARTS.map(({ series }) => rows.find((row) => row.period === start && row.series === series && row.dimension === null));
		const n = parts[0]?.n ?? 0;
		const issue = alignmentIssue(parts, n);
		return { start, n, shares: issue ? null : (parts.map((row) => row!.value as number) as [number, number, number]), issue, partial: Boolean(partial) };
	});
}

function alignmentIssue(parts: (MeasurementRow | undefined)[], n: number): string | null {
	if (n <= 0) return 'No decisions answered.';
	if (parts.some((row) => !row)) return INCONSISTENT_ANSWERS;
	if (parts.some((row) => row!.value === null || row!.thin)) return TOO_FEW;
	if (parts.some((row) => !row!.comparable)) return 'Not comparable with the other periods.';
	if (parts.some((row) => row!.n !== n)) return INCONSISTENT_ANSWERS;
	if (parts.some((row) => !Number.isFinite(row!.value) || row!.value! < 0 || row!.value! > 1)) return INCONSISTENT_ANSWERS;
	return Math.abs(parts.reduce((sum, row) => sum + row!.value!, 0) - 1) < 1e-9 ? null : INCONSISTENT_ANSWERS;
}

const f1 = (v: number) => v.toFixed(1);
export const sharePercent = (share: number) => `${Math.round(share * 100)}%`;

export function alignmentGeometry(periods: AlignmentPeriod[], axisTitle: string, W: number, ML: number, MR: number, MT: number, PH: number, fs: number, rot: boolean): ChartGeometry {
	const N = Math.max(periods.length, 1);
	const y = (share: number) => MT + PH - PH * share;
	const slot = (W - ML - MR) / N;
	const bw = Math.min(64, slot * 0.62);
	const y0 = MT + PH;
	const items: ChartItem[] = [{ kind: 'text', cls: 'at', x: 0, y: Number(f1(fs + 1)), anchor: 'start', text: axisTitle }];
	for (const share of [0, 0.5, 1]) {
		items.push({ kind: 'line', cls: share === 0 ? 'zero' : 'grid', x1: ML, x2: W - MR, y: Number(f1(y(share))) });
		items.push({ kind: 'text', cls: 'tk', x: ML - 4, y: Number(f1(y(share) + 4)), anchor: 'end', text: sharePercent(share) });
	}
	items.push({ kind: 'text', cls: 'nn', x: ML - 4, y: Number(f1(y0 + 3.7 * fs)), anchor: 'end', text: 'n' });
	const labels: ChartItem[] = [];
	let prevMonth: number | null = null;
	let stateLen = 0;
	periods.forEach((p, i) => {
		const cx = ML + slot * (i + 0.5);
		if (p.empty) return;
		if (p.shares) {
			let below = 0;
			p.shares.forEach((share, part) => {
				const top = y(below + share), bottom = y(below);
				items.push({ kind: 'rect', cls: `stack s${part}${p.partial ? ' part' : ''}`, x: Number(f1(cx - bw / 2)), y: Number(f1(top)), width: Number(f1(bw)), height: Number(f1(bottom - top)) });
				// A share is written on its own segment only where the segment has room for it, as the original chart had it.
				if (bw >= 32 && bottom - top >= (part === 2 ? 24 : 18)) labels.push({ kind: 'text', cls: `sl s${part}`, x: Number(f1(cx)), y: Number(f1((top + bottom) / 2 + 4)), anchor: 'middle', text: sharePercent(share) });
				below += share;
			});
		}
		const { day, month, monthName } = startParts(p.start);
		items.push({ kind: 'text', cls: 'xl', x: Number(f1(cx)), y: Number(f1(y0 + 1.5 * fs)), anchor: 'middle', text: String(day) });
		if (month !== prevMonth) {
			items.push({ kind: 'text', cls: 'xm', x: Number(f1(cx)), y: Number(f1(y0 + 2.6 * fs)), anchor: 'middle', text: monthName });
			prevMonth = month;
		}
		items.push({ kind: 'text', cls: 'nn', x: Number(f1(cx)), y: Number(f1(y0 + 3.7 * fs)), anchor: 'middle', text: Math.trunc(p.n).toLocaleString('en-US') });
		// The running day says so; a period withheld for too few answers says too few; every other issue is told in the table only.
		const state = p.partial ? 'today so far' : p.issue === TOO_FEW && p.n > 0 ? 'too few' : '';
		if (state) {
			if (rot) {
				const lx = cx - 0.35 * 10, ly = y0 + 4.5 * fs;
				stateLen = Math.max(stateLen, state.length * 0.56 * 10 + 4);
				items.push({ kind: 'text', cls: 'lo', x: Number(f1(lx)), y: Number(f1(ly)), anchor: 'start', text: state, rotate: { x: Number(f1(lx)), y: Number(f1(ly)), deg: 90 } });
			} else items.push({ kind: 'text', cls: 'lo', x: Number(f1(cx)), y: Number(f1(y0 + 4.8 * fs)), anchor: 'middle', text: state });
		}
	});
	items.push(...labels);
	const height = y0 + (rot ? Math.max(7.6 * fs, 4.5 * fs + stateLen) : 6 * fs) + 8;
	return { width: W, height: Number(f1(height)), items };
}

/** The same two viewBoxes as the page's bar charts: a 335 px phone chart and a desktop chart at the card's width. */
export const alignmentPhone = (periods: AlignmentPeriod[], axisTitle: string) => alignmentGeometry(periods, axisTitle, 335, 40, 4, 52, 200, 11, true);
export const alignmentDesktop = (periods: AlignmentPeriod[], axisTitle: string, width = 760) => alignmentGeometry(periods, axisTitle, width, 52, 8, 28, 150, 10.5, false);
