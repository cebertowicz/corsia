import { readable } from 'svelte/store';

/** A server timestamp as a Date; see `parseWsTimestamp` for how a zone is read. */
export function parseUTC(iso: string): Date {
	return new Date(parseWsTimestamp(iso));
}

/**
 * Epoch milliseconds for a server timestamp, or NaN when it is not one.
 *
 * The server writes UTC, often without a zone suffix; read bare, `new Date()`
 * takes such a string as local time. A string with an explicit zone keeps it,
 * and a fraction longer than milliseconds is cut for engines that reject it.
 */
export function parseWsTimestamp(raw: unknown): number {
	if (typeof raw !== 'string' || !raw) return Number.NaN;
	const hasExplicitZone = /(Z|[+-]\d{2}:\d{2})$/.test(raw);
	const value = hasExplicitZone ? raw : `${raw}Z`;
	const direct = new Date(value).getTime();
	if (Number.isFinite(direct)) return direct;
	const millisPrecision = value.replace(
		/\.(\d{3})\d+(Z|[+-]\d{2}:\d{2})$/,
		'.$1$2',
	);
	return new Date(millisPrecision).getTime();
}

export function formatTime(iso: string): string {
	try {
		return parseUTC(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
	} catch { return ''; }
}

export const TIME_BUCKETS = ['Today', 'Yesterday', 'This week', 'This month', 'Older'] as const;
export type TimeBucket = (typeof TIME_BUCKETS)[number];

export function bucketOf(iso: string, now: Date = new Date()): TimeBucket {
	if (!iso) return 'Older';
	const t = parseUTC(iso).getTime();
	if (Number.isNaN(t)) return 'Older';
	const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
	if (t >= startToday) return 'Today';
	if (t >= startToday - 86400000) return 'Yesterday';
	if (t >= startToday - 7 * 86400000) return 'This week';
	if (t >= startToday - 30 * 86400000) return 'This month';
	return 'Older';
}

/**
 * A moment as a person says it, in local time: "today at 3:40 PM",
 * "yesterday at 9:05 AM", "tomorrow at 9:00 AM", "Friday at 9:00 AM" within
 * the coming week, otherwise the short date ("Sep 22"). Days turn at local
 * midnight. `zone` names the time zone after the clock ("9:00 AM PDT"), and
 * a far date keeps its clock too ("Oct 9 at 9:00 AM PDT"), because a zone is
 * asked for only where the exact moment matters. `clock` keeps the clock on a
 * far date with no zone; `plain` joins day and clock with a space ("Monday
 * 9:00 AM") for a line that supplies its own "at". `compact` drops "today" and leaves the clock
 * alone. Empty for an invalid date.
 */
export function formatMoment(
	at: Date,
	now: Date = new Date(),
	{ zone = false, clock = false, plain = false, compact = false, locales = [] }: {
		zone?: boolean; clock?: boolean; plain?: boolean; compact?: boolean; locales?: Intl.LocalesArgument;
	} = {},
): string {
	if (Number.isNaN(at.getTime())) return '';
	const day = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
	const days = Math.round((day(at) - day(now)) / 86400000);
	const join = plain ? ' ' : ' at ';
	const time = at.toLocaleTimeString(locales, {
		hour: 'numeric', minute: '2-digit', ...(zone ? { timeZoneName: 'short' as const } : {}),
	});
	if (compact && days === 0) return time;
	if (days === 0) return `today${join}${time}`;
	if (days === -1) return `yesterday${join}${time}`;
	if (days === 1) return `tomorrow${join}${time}`;
	if (days > 1 && days < 7) return `${at.toLocaleDateString(locales, { weekday: 'long' })}${join}${time}`;
	const year = at.getFullYear() === now.getFullYear() ? {} : { year: 'numeric' as const };
	const date = at.toLocaleDateString(locales, { month: 'short', day: 'numeric', ...year });
	return zone || clock ? `${date}${join}${time}` : date;
}

/** Milliseconds from `now` until the next local midnight. */
export function msUntilNextLocalDay(now: Date): number {
	const next = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
	return next.getTime() - now.getTime();
}

/**
 * One shared clock for day-relative labels ("today at", "yesterday at"): it
 * ticks once at each local midnight, so a page left open overnight relabels
 * without a per-row timer.
 */
export const localDayClock = readable(new Date(), (set) => {
	let timer: ReturnType<typeof setTimeout>;
	const schedule = () => {
		const now = new Date();
		set(now);
		timer = setTimeout(schedule, msUntilNextLocalDay(now) + 1_000);
	};
	schedule();
	return () => clearTimeout(timer);
});

/** A clock that ticks every minute, for countdowns ("ends in 12 min"); it also relabels days at midnight. */
export const minuteClock = readable(new Date(), (set) => {
	set(new Date());
	const timer = setInterval(() => set(new Date()), 60_000);
	return () => clearInterval(timer);
});

export function formatDateTime(iso: string): string {
	try {
		return parseUTC(iso).toLocaleString();
	} catch { return ''; }
}

export function relLabel(iso: string): string {
	const d = parseUTC(iso);
	const t = d.getTime();
	if (Number.isNaN(t)) return '';
	const m = Math.max(0, Math.round((Date.now() - t) / 60000));
	if (m < 1) return 'just now';
	if (m < 60) return `${m}m ago`;
	const h = Math.round(m / 60);
	if (h < 24) return `${h}h ago`;
	const days = Math.round(h / 24);
	return days < 30 ? `${days}d ago` : `${Math.round(days / 30)}mo ago`;
}

/** The time as a touch screen's meta row shows it: "now", "5m", "3h", then the date ("Oct 9"). Without an ISO time it shortens the long label. */
export function compactRelLabel(iso: string | null | undefined, label = ''): string {
	const t = iso ? parseUTC(iso).getTime() : Number.NaN;
	if (Number.isNaN(t)) return label === 'just now' ? 'now' : label.replace(/ ago$/, '');
	const m = Math.max(0, Math.round((Date.now() - t) / 60000));
	if (m < 1) return 'now';
	if (m < 60) return `${m}m`;
	if (m < 60 * 24) return `${Math.round(m / 60)}h`;
	const d = new Date(t);
	const sameYear = d.getFullYear() === new Date().getFullYear();
	return d.toLocaleDateString(undefined, sameYear ? { month: 'short', day: 'numeric' } : { month: 'short', day: 'numeric', year: 'numeric' });
}
