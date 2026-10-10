import { writable } from 'svelte/store';

export type ConnectionState = 'disconnected' | 'connecting' | 'connected' | 'error';

export const connectionState = writable<ConnectionState>('disconnected');
export const connectionError = writable<string | null>(null);

export type DrainTurnBlocker = {
	threadId: string;
	turnId: string;
	turnOrder: number;
	executionClass: string;
	title: string;
	startedAt: string;
};

export type DrainStatus = {
	active: boolean;
	reason: string;
	activeProcesses: number;
	turnAdmissions: number;
	activeTurns: number;
	hostAccessSupervisors: number;
	ownedProcessGroups: number;
	browserCommands: number;
	blockingTurns: DrainTurnBlocker[];
	countdown: number | null;
} | null;

export const drainStatus = writable<DrainStatus>(null);
let drainStatusGeneration = 0;
let latestDrainRefreshRequest = 0;

const drainCount = (value: unknown): number => (
	Number.isInteger(value) && (value as number) >= 0 ? value as number : 0
);

export function applyDrainStatus(data: any): void {
	drainStatusGeneration += 1;
	if (data?.active !== true && data?.draining !== true) {
		drainStatus.set(null);
		return;
	}
	const blockingTurns = Array.isArray(data.blockingTurns)
		? data.blockingTurns.flatMap((turn: any) => (
			typeof turn?.threadId === 'string'
			&& typeof turn?.turnId === 'string'
			&& Number.isInteger(turn?.turnOrder)
			&& typeof turn?.executionClass === 'string'
			&& typeof turn?.title === 'string'
			&& typeof turn?.startedAt === 'string'
				? [turn]
				: []
		))
		: [];
	drainStatus.set({
		active: true,
		reason: data.reason || 'restart',
		activeProcesses: drainCount(data.activeProcesses),
		turnAdmissions: drainCount(data.turnAdmissions),
		activeTurns: drainCount(data.activeTurns),
		hostAccessSupervisors: drainCount(data.hostAccessSupervisors),
		ownedProcessGroups: drainCount(data.ownedProcessGroups),
		browserCommands: drainCount(data.browserCommands),
		blockingTurns,
		countdown: data.countdown ?? null,
	});
}

export async function refreshDrainStatus(
	authToken: string,
	isCurrent: () => boolean = () => true,
): Promise<void> {
	if (!isCurrent()) return;
	const requestedGeneration = drainStatusGeneration;
	const requestId = ++latestDrainRefreshRequest;
	const response = await fetch(
		`/api/system/drain?token=${encodeURIComponent(authToken)}`,
		{ cache: 'no-store' },
	);
	if (!isCurrent()) return;
	if (!response.ok) throw new Error(`drain status request failed: ${response.status}`);
	const status = await response.json();
	if (
		!isCurrent()
		|| drainStatusGeneration !== requestedGeneration
		|| requestId !== latestDrainRefreshRequest
	) return;
	applyDrainStatus(status);
}

export type SecurityAlert = {
	vulns: number;
	errors: number;
	report: string;
	date: string;
} | null;

export const securityAlert = writable<SecurityAlert>(null);

export async function checkSecurityAlert(authToken: string | null = null): Promise<void> {
	if (!authToken) {
		securityAlert.set(null);
		return;
	}
	try {
		const res = await fetch('/api/security/alert', {
			headers: { Authorization: `Bearer ${authToken}` },
		});
		if (!res.ok) { securityAlert.set(null); return; }
		const data = await res.json();
		securityAlert.set(data?.vulns != null ? data : null);
	} catch {}
}

export async function dismissSecurityAlert(token: string): Promise<void> {
	try {
		await fetch(`/api/security/alert?token=${token}`, { method: 'DELETE' });
		securityAlert.set(null);
	} catch {}
}
