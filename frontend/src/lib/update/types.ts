import type { UpdateChanges, UpdateOperation } from './model';

export interface UpdateSchedule {
	automatic: boolean;
	time: string;
	tonight_at: string | null;
}

export interface UpdateStatus {
	branch: string | null;
	current_sha: string | null;
	remote_sha: string | null;
	commits_behind: number | null;
	available: boolean | null;
	checked_at: number | null;
	error: string | null;
	current_version?: string | null;
	target_version?: string | null;
	changes?: UpdateChanges | null;
	drain_cap_seconds?: number | null;
	usual_seconds?: number | null;
	agents_working?: number | null;
	server_time?: string | null;
	operation?: UpdateOperation | null;
	schedule?: UpdateSchedule | null;
}
