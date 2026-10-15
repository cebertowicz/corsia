export type LocalDownloadStart = 'started' | 'consent_required' | 'replace_consent_required' | 'failed';

type LocalDownloadPayload = {
	code?: 'consent_required' | 'replace_consent_required';
	ok?: boolean;
	stage?: string;
};

export type LocalDownloadResult = {
	outcome: LocalDownloadStart;
	stage: string | null;
};

export async function requestLocalDownload(
	localWanted: boolean,
	consentExisting = false,
	consentReplace = false,
): Promise<LocalDownloadResult> {
	if (!localWanted && !consentExisting && !consentReplace) return { outcome: 'started', stage: null };
	const body = {
		consent_existing: consentExisting,
		...(consentReplace ? { consent_replace: true } : {}),
	};
	const response = await fetch('/api/setup/local-download', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body),
	});
	const payload: LocalDownloadPayload | null = await response.json().catch(() => null);
	if (response.status === 409 && (payload?.code === 'consent_required' || payload?.code === 'replace_consent_required')) {
		return { outcome: payload.code, stage: null };
	}
	if (response.ok && payload?.ok !== false) {
		return { outcome: 'started', stage: typeof payload?.stage === 'string' ? payload.stage : null };
	}
	return { outcome: 'failed', stage: null };
}
