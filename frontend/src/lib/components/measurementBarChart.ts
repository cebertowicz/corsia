/** Geometry for MeasurementBarChart: bars with a zero baseline, value labels, an n row and a moving average. */

/** A period whose value is withheld although something stands behind it: no bar, and it says why instead of "no data". */
export const isWithheld = (p: BarPoint) => p.value === null && p.n > 0;

export interface BarPoint {
	/** ISO date (YYYY-MM-DD) of the first day of the period. */
	start: string;
	/** Unknown is null, never zero. */
	value: number | null;
	/** What n counts for this period. */
	n: number;
	/** The served moving average at this period, on the value's own axis; null before the series has a whole window, and then no point is drawn. */
	ma: number | null;
	/** The period is still running (Days): drawn outlined and labelled, never the latest highlight, never low n. */
	partial?: boolean;
	/** A week on the shared Weeks axis the series has nothing for, as before its measure started: only its date is drawn. */
	empty?: boolean;
}

export type ChartItem =
	| { kind: 'text'; cls: string; x: number; y: number; anchor: 'start' | 'middle' | 'end'; text: string; rotate?: { x: number; y: number; deg?: number }; size?: number }
	| { kind: 'line'; cls: string; x1: number; x2: number; y: number }
	| { kind: 'rect'; cls: string; x: number; y: number; width: number; height: number }
	| { kind: 'path'; cls: string; d: string }
	| { kind: 'polyline'; cls: string; points: string }
	| { kind: 'circle'; cls: string; cx: number; cy: number; r: number };

export interface ChartGeometry { width: number; height: number; items: ChartItem[] }

export interface ChartSpec {
	points: BarPoint[];
	axisTitle: string;
	tickFormat: (value: number, decimals: number) => string;
	valueFormat: (value: number) => string;
	minN: number;
	/** Axis 0 to 100 in steps of 25. */
	percent100?: boolean;
	showN?: boolean;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const f1 = (v: number) => v.toFixed(1);

/** Day of month and month name of a period start, read as the calendar date it is. */
export function startParts(iso: string): { day: number; month: number; monthName: string } {
	const month = Number(iso.slice(5, 7)) - 1;
	return { day: Number(iso.slice(8, 10)), month, monthName: MONTHS[month] };
}

/** An axis step and tick count that gives two to four intervals. */
export function niceAxis(top: number): { step: number; count: number } {
	const t = top && top > 0 ? top : 1;
	const e = Math.floor(Math.log10(t));
	const steps = new Set<number>();
	for (const k of [e - 2, e - 1, e, e + 1]) for (const m of [1, 2, 2.5, 5]) steps.add(m * 10 ** k);
	for (const step of [...steps].sort((a, b) => a - b)) {
		const count = Math.ceil(t / step - 1e-9);
		if (count >= 2 && count <= 4) return { step, count };
	}
	return { step: t, count: 1 };
}

export const isLow = (p: BarPoint, minN: number) => p.value !== null && !p.partial && p.n < minN;

/** True when a low-n bar is taller than the axis, so it is cut with a break mark. */
export function hasClippedBar(points: BarPoint[], minN: number): boolean {
	const ok = points.filter((p) => p.value !== null && !isLow(p, minN)).map((p) => p.value as number);
	return ok.length > 0 && points.some((p) => p.value !== null && isLow(p, minN) && p.value > Math.max(...ok) * 1.25);
}

export function chartGeometry(spec: ChartSpec, W: number, ML: number, MR: number, MT: number, PH: number, fs: number, rot: boolean): ChartGeometry {
	const { points: pts, minN } = spec;
	const nrow = spec.showN !== false;
	const N = pts.length;
	const ok = pts.filter((p) => p.value !== null && !isLow(p, minN)).map((p) => p.value as number);
	const known = pts.filter((p) => p.value !== null).map((p) => p.value as number);
	const base = ok.length ? Math.max(...ok) : known.length ? Math.max(...known) : 1;
	const vals = [...ok, ...pts.filter((p) => p.ma !== null && p.ma <= 1.25 * base).map((p) => p.ma as number)];
	const { step, count: nt } = spec.percent100 ? { step: 25, count: 4 } : niceAxis(vals.length ? Math.max(...vals) : base);
	const ymax = step * nt;
	const y = (v: number) => MT + PH - (PH * Math.min(v, ymax)) / ymax;
	const slot = (W - ML - MR) / N;
	const bw = slot * 0.62;
	const y0 = MT + PH;
	const items: ChartItem[] = [{ kind: 'text', cls: 'at', x: 0, y: Number(f1(fs + 1)), anchor: 'start', text: spec.axisTitle }];
	const decimals = Math.abs(step - Math.round(step)) < 1e-9 ? 0 : Math.abs(step * 10 - Math.round(step * 10)) < 1e-6 ? 1 : 2;
	for (let i = 0; i <= nt; i++) {
		const v = i * step;
		items.push({ kind: 'line', cls: i === 0 ? 'zero' : 'grid', x1: ML, x2: W - MR, y: Number(f1(y(v))) });
		items.push({ kind: 'text', cls: 'tk', x: ML - 4, y: Number(f1(y(v) + 4)), anchor: 'end', text: spec.tickFormat(v, decimals) });
	}
	if (nrow) items.push({ kind: 'text', cls: 'nn', x: ML - 4, y: Number(f1(y0 + 3.7 * fs)), anchor: 'end', text: 'n' });
	let prevMonth: number | null = null;
	// The moving average as runs of consecutive points: a period with none ends the run, so the line never bridges a gap.
	const runs: [number, number][][] = [[]];
	const labels: ChartItem[] = [];
	const labFs = rot ? 11.5 : fs;
	// One line of n, each directly under its own bar: the text shrinks, then abbreviates, to fit its slot.
	const full = (n: number) => Math.trunc(n).toLocaleString('en-US');
	const compact = (n: number) => (n < 10000 ? `${(n / 1000).toFixed(1)}k` : `${(n / 1000).toFixed(0)}k`);
	const needsCompact = rot && pts.length > 0 && Math.max(...pts.map((p) => full(p.n).length)) * 0.6 * 8.5 > slot - 3;
	const nText = (n: number) => (n >= 10000 || (needsCompact && n >= 1000) ? compact(n) : full(n));
	const nSize = nrow && pts.length ? Math.max(8.5, Math.min(fs, (slot - 3) / (Math.max(...pts.map((p) => nText(p.n).length)) * 0.6))) : fs;
	// The state of a period is spelled under its own n; a phone writes it downward so it can never sit under a neighbour.
	const stateOf = (p: BarPoint) => (p.partial ? 'today so far' : p.value === null ? (isWithheld(p) ? 'too few' : '') : isLow(p, minN) ? 'low n' : '');
	let stateLen = 0;
	// The latest highlight is the last completed period; a running one is outlined instead.
	let latest = N - 1;
	while (latest > 0 && pts[latest].partial) latest--;
	pts.forEach((p, i) => {
		const cx = ML + slot * (i + 0.5);
		const cur = i === latest ? ' cur' : '';
		const low = isLow(p, minN);
		if (p.value !== null) {
			const top = y(p.value);
			const clipped = p.value > ymax + 1e-9;
			items.push({ kind: 'rect', cls: `bar${low ? ' low' : ''}${p.partial ? ' part' : ''}${cur}`, x: Number(f1(cx - bw / 2)), y: Number(f1(top)), width: Number(f1(bw)), height: Number(f1(Math.max(0.8, y0 - top))) });
			if (clipped) {
				const x = cx - bw / 2 - 1;
				items.push({ kind: 'path', cls: 'brk', d: `M${f1(x)} ${f1(top + 6)} l${f1(bw + 2)} -3 M${f1(x)} ${f1(top + 10)} l${f1(bw + 2)} -3` });
			}
			const txt = spec.valueFormat(p.value);
			const ln = txt.length * 0.62 * labFs;
			const may = p.ma !== null ? y(p.ma) : null;
			if (rot && txt.length > 1) {
				let yb = top - 4;
				if (may !== null && yb - ln - 2 <= may && may <= yb + 3 && may - 5 - ln >= 4) yb = may - 5;
				labels.push({ kind: 'text', cls: 'vl', x: Number(f1(cx + 4)), y: Number(f1(yb)), anchor: 'start', text: txt, rotate: { x: Number(f1(cx + 4)), y: Number(f1(yb)) } });
			} else {
				let yb = top - 3;
				if (may !== null && yb - labFs - 1 <= may && may <= yb + 3 && may - 5 - labFs >= 4) yb = may - 5;
				labels.push({ kind: 'text', cls: 'vl', x: Number(f1(cx)), y: Number(f1(yb)), anchor: 'middle', text: txt });
			}
		}
		if (p.ma !== null) runs[runs.length - 1].push([Number(f1(cx)), Number(f1(y(p.ma)))]);
		else if (runs[runs.length - 1].length) runs.push([]);
		const { day, month, monthName } = startParts(p.start);
		items.push({ kind: 'text', cls: `xl${cur}`, x: Number(f1(cx)), y: Number(f1(y0 + 1.5 * fs)), anchor: 'middle', text: String(day) });
		if (month !== prevMonth) {
			items.push({ kind: 'text', cls: 'xm', x: Number(f1(cx)), y: Number(f1(y0 + 2.6 * fs)), anchor: 'middle', text: monthName });
			prevMonth = month;
		}
		if (p.empty) return;
		if (nrow) {
			items.push({ kind: 'text', cls: 'nn', x: Number(f1(cx)), y: Number(f1(y0 + 3.7 * fs)), anchor: 'middle', text: nText(p.n), ...(nSize < fs ? { size: Number(nSize.toFixed(1)) } : {}) });
		}
		const state = stateOf(p);
		if (state && rot) {
			const lx = cx - 0.35 * 10, ly = y0 + 4.5 * fs;
			stateLen = Math.max(stateLen, state.length * 0.56 * 10 + 4);
			items.push({ kind: 'text', cls: 'lo', x: Number(f1(lx)), y: Number(f1(ly)), anchor: 'start', text: state, rotate: { x: Number(f1(lx)), y: Number(f1(ly)), deg: 90 } });
		} else if (state) {
			items.push({ kind: 'text', cls: 'lo', x: Number(f1(cx)), y: Number(f1(y0 + 4.8 * fs)), anchor: 'middle', text: state });
		}
	});
	const height = y0 + (rot ? Math.max(7.6 * fs, 4.5 * fs + stateLen) : 6 * fs) + 8;
	for (const run of runs) {
		if (run.length >= 2) items.push({ kind: 'polyline', cls: 'ma', points: run.map(([a, b]) => `${a},${b}`).join(' ') });
		else if (run.length === 1) items.push({ kind: 'circle', cls: 'ma', cx: run[0][0], cy: run[0][1], r: 2 });
	}
	items.push(...labels);
	return { width: W, height: Number(f1(height)), items };
}

/** The two viewBoxes the render ships: a 335 px phone chart with rotated labels and a 760 px desktop chart. */
export const phoneGeometry = (spec: ChartSpec) => chartGeometry(spec, 335, 40, 4, 52, 200, 11, true);
export const desktopGeometry = (spec: ChartSpec, width = 760) => chartGeometry(spec, width, 52, 8, 28, 150, 10.5, false);
