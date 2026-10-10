/** Context occupancy for one thread, as carried on receipts, frames and the next-turn preview. */
export interface ContextUsage {
	/** The thread a stored row belongs to; the store's writers attach it. */
	thread_id?: string;
	/** The model of the stored message a receipt was read back from on entry. */
	receipt_model?: string | null;
	model_context_window: number;
	/** Provider capacity; larger than model_context_window iff a CORSIA budget caps it (#1753). */
	discovered_context_window?: number | null;
	system_tokens: number;
	tool_def_tokens: number;
	history_tokens: number;
	user_tokens: number;
	context_source_tokens?: number;
	total_used: number | null;
	utilization: number | null;
	provider_reported_total_used?: number | null;
	provider_reported_utilization?: number | null;
	provider_reported_input_tokens?: number | null;
	provider_reported_cached_input_tokens?: number | null;
	provider_reported_cache_read_input_tokens?: number | null;
	provider_reported_cache_creation_input_tokens?: number | null;
	/** The preview's preflight ledger: only the parts the sheet adds up. */
	preflight?: { checkpoint_tokens?: number } | null;
	attribution_delta_tokens?: number | null;
	attribution_delta_reason?: string | null;
	token_usage_available?: boolean;
	token_source?: 'measured' | 'estimated';
	was_trimmed: boolean;
	history_messages: number;
	continuity_mode?: string;
	/** The installed checkpoint the measured turn was dispatched on, when it was on one. */
	checkpoint_generation_id?: string | null;
	source_artifacts?: Array<{
		kind?: string;
		harness?: string;
		method?: string;
		snapshot_id?: string;
		content_hash?: string;
		payload?: Record<string, unknown>;
		params?: Record<string, unknown>;
	}>;
	preview?: boolean;
	/** Kept after a later turn that could not measure: shown, but not a usable receipt. */
	stale?: boolean;
	/** Same admission rule the checkpoint-start endpoint applies (#1952): the
	 *  sheet shows Checkpoint Now only when a click would actually succeed. */
	checkpoint_eligible?: boolean | null;
	checkpoint_ineligible_reason?: string | null;
	snapshot_id?: string;
	/** View-only descriptor costs within Skills for the selected snapshot. */
	lesson_index_estimates?: {
		snapshot_id: string | null;
		model: string | null;
		sources: Record<string, Array<{
			id: string;
			behavioral_sha256: string;
			prompt_tokens: number | null;
		}> | null>;
	};
	context_snapshot?: {
		snapshot_id?: string;
		content_hash?: string;
		delivery_leaves?: Array<{
			harness?: string;
			destination_mode?: string;
			snapshot_id?: string;
			content_hash?: string;
		}>;
		source_inventory?: Array<{
			source_id?: string;
			origin_section?: string;
			label?: string;
			stable_reference?: string;
			freshness?: string;
			inclusion?: string;
			reason?: string;
			owner_surface?: string;
			prompt_tokens?: number | null;
			/** The file on disk differs from the copy installed in this thread; false or
			 *  absent when equal or unknown. Preview rows only. */
			edited_since_install?: boolean;
			metadata?: Record<string, unknown>;
		}>;
		agent_facts?: Record<string, unknown>;
		scope_facts?: Record<string, unknown>;
		harness_model_facts?: Record<string, unknown>;
	} | null;
}
