/** How the picker and the limit-held line read a usage reset time. */

function sameDay(a: Date, b: Date): boolean {
	return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/** A reset in the browser's zone: a time today, a weekday within six days, else a date. */
export function resetLabel(iso: string | null | undefined, withTime = false, now: Date = new Date()): string {
	if (!iso) return '';
	const at = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
	if (Number.isNaN(at.getTime())) return '';
	const time = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: at.getMinutes() ? '2-digit' : undefined }).format(at);
	if (sameDay(at, now)) return time;
	const days = (at.getTime() - now.getTime()) / 86_400_000;
	if (days < 6) {
		const weekday = new Intl.DateTimeFormat(undefined, { weekday: 'short' }).format(at);
		return withTime ? `${weekday} ${time}` : weekday;
	}
	return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' }).format(at);
}

/** The soonest of several resets that reads as a time, or undefined when none does. */
export function earliestReset(isos: (string | null | undefined)[]): string | undefined {
	return isos.filter((iso): iso is string => !!iso && resetLabel(iso) !== '')
		.sort((a, b) => new Date(a).getTime() - new Date(b).getTime())[0];
}
