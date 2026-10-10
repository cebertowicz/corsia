export interface DiffLine {
	type: 'ctx' | 'add' | 'del';
	text: string;
}

const MAX_LINES = 400;

export function lineDiff(oldText: string, newText: string): DiffLine[] | null {
	const a = oldText === '' ? [] : oldText.split('\n');
	const b = newText === '' ? [] : newText.split('\n');
	if (a.length > MAX_LINES || b.length > MAX_LINES) return null;

	const n = a.length, m = b.length;
	const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
	for (let i = n - 1; i >= 0; i--) {
		for (let j = m - 1; j >= 0; j--) {
			dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
		}
	}

	const out: DiffLine[] = [];
	let i = 0, j = 0;
	while (i < n && j < m) {
		if (a[i] === b[j]) {
			out.push({ type: 'ctx', text: a[i] });
			i++; j++;
		} else if (dp[i + 1][j] >= dp[i][j + 1]) {
			out.push({ type: 'del', text: a[i] });
			i++;
		} else {
			out.push({ type: 'add', text: b[j] });
			j++;
		}
	}
	while (i < n) out.push({ type: 'del', text: a[i++] });
	while (j < m) out.push({ type: 'add', text: b[j++] });
	return out;
}

export function diffStats(lines: DiffLine[]): [number, number] {
	let add = 0, del = 0;
	for (const l of lines) {
		if (l.type === 'add') add++;
		else if (l.type === 'del') del++;
	}
	return [add, del];
}
