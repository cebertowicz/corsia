import {
	loadProviderPolicy,
	saveProviderPolicy,
	saveUseWorkspace,
	type ProviderEntry,
	type ProviderPolicy,
} from '$lib/api/providerPolicy';
import { saveFailureText } from '$lib/saveFailure';

/** One policy owner for both workspace account controls and project overrides. */
export function createProviderPolicyState(projectId?: string | (() => string | undefined)) {
	const currentProjectId = () => typeof projectId === 'function' ? projectId() : projectId;
	let policy = $state<ProviderPolicy | null>(null);
	let error = $state('');
	let loading = $state(true);
	let pendingTargets = $state<Set<string>>(new Set());
	let failures = $state<Record<string, string>>({});
	const providerQueues = new Map<string, Promise<void>>();
	// A patch function returns null when the change would not alter anything, so nothing is sent.
	type PolicyPatch = Partial<ProviderEntry> | ((entry: ProviderEntry) => Partial<ProviderEntry> | null);
	// A failed read names the read, never a route's wording; the host's Reload button is the next step.
	const loadFailureText = (result: { ok: boolean; reached?: boolean }) =>
		!result.ok && result.reached === false ? 'Couldn’t load providers. Check your connection.' : 'Couldn’t load providers.';

	async function load() {
		loading = true;
		error = '';
		failures = {};
		try {
			const result = await loadProviderPolicy(currentProjectId());
			if (result.ok && Array.isArray(result.value?.providers)) policy = result.value;
			else error = loadFailureText(result);
		} finally {
			loading = false;
		}
	}

	function setPending(target: string, pending: boolean) {
		const next = new Set(pendingTargets);
		if (pending) next.add(target);
		else next.delete(target);
		pendingTargets = next;
	}

	function setFailure(target: string, text: string | null) {
		const { [target]: _, ...rest } = failures;
		failures = text === null ? rest : { ...rest, [target]: text };
	}

	/** Re-read the policy without touching loading or failures, so the next save uses the current revision. When it cannot, `error` says so and the host offers a reload. */
	async function refresh(): Promise<ProviderPolicy | null> {
		const result = await loadProviderPolicy(currentProjectId());
		if (result.ok && Array.isArray(result.value?.providers)) {
			policy = result.value;
			return result.value;
		}
		error = loadFailureText(result);
		return null;
	}

	/** Name why a save failed by the server's stable `code`; the routes' own wording is developer text, so none is shown. A change elsewhere or a lost response re-reads, so the retry has the current revision. */
	async function failureLine(result: { reached?: boolean; code?: string }): Promise<string> {
		if (result.code === 'project_not_found') return 'Couldn’t save this change. This project doesn’t exist; choose another project.';
		if (result.reached !== false && result.code !== 'policy_changed') return saveFailureText(undefined, true);
		const latest = await refresh();
		if (!latest || result.reached === false) return saveFailureText(undefined, false);
		return saveFailureText('This shows the latest setting; try again to change it.', true);
	}

	function save(entry: ProviderEntry, patch: PolicyPatch, target = `provider:${entry.provider_key}`): Promise<void> {
		if (pendingTargets.has(target)) return Promise.resolve();
		setPending(target, true);
		setFailure(target, null);
		error = '';
		const previous = providerQueues.get(entry.provider_key) ?? Promise.resolve();
		let queued: Promise<void>;
		queued = previous.then(async () => {
			const current = policy?.providers.find((item) => item.provider_key === entry.provider_key) ?? entry;
			const resolvedPatch = typeof patch === 'function' ? patch(current) : patch;
			if (!resolvedPatch) return;
			const result = await saveProviderPolicy({ ...current, ...resolvedPatch }, currentProjectId());
			if (result.ok && policy) {
				policy = { ...policy, providers: policy.providers.map((item) => item.provider_key === entry.provider_key ? result.value : item) };
			} else if (!result.ok) {
				setFailure(target, await failureLine(result));
			}
		}).finally(() => {
			setPending(target, false);
			if (providerQueues.get(entry.provider_key) === queued) providerQueues.delete(entry.provider_key);
		});
		providerQueues.set(entry.provider_key, queued);
		return queued;
	}

	async function toggleScope(useWorkspace: boolean) {
		const current = currentProjectId();
		const target = 'scope';
		if (!current || !policy || pendingTargets.has(target)) return;
		setPending(target, true);
		setFailure(target, null);
		error = '';
		try {
			const result = await saveUseWorkspace(current, useWorkspace, policy.override_revision ?? 0);
			if (result.ok) policy = result.value;
			else setFailure(target, await failureLine(result));
		} finally {
			setPending(target, false);
		}
	}

	function setEnabled(provider: ProviderEntry, unitId: string, enabled: boolean) {
		void save(provider, (current) => current.units.some((unit) => unit.id === unitId)
			? { disabled: current.units.filter((unit) => unit.id === unitId ? !enabled : !unit.enabled).map((unit) => unit.id) }
			: null, `unit:${provider.provider_key}:${unitId}`);
	}

	function move(provider: ProviderEntry, unitId: string, direction: -1 | 1) {
		void save(provider, (current) => {
			const order = current.units.map((unit) => unit.id);
			const index = order.indexOf(unitId);
			const destination = index + direction;
			if (index < 0 || destination < 0 || destination >= order.length) return null;
			[order[index], order[destination]] = [order[destination], order[index]];
			return { order };
		}, `order:${provider.provider_key}:${unitId}`);
	}

	function isPending(target: string): boolean { return pendingTargets.has(target); }
	function hasFailure(target: string): boolean { return target in failures; }
	function failureTextFor(target: string): string | null { return failures[target] ?? null; }

	return {
		get policy() { return policy; },
		get error() { return error; },
		get loading() { return loading; },
		isPending,
		hasFailure,
		failureText: failureTextFor,
		load,
		save,
		toggleScope,
		setEnabled,
		move,
	};
}
