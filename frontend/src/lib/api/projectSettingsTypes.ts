export type ProjectDetail = {
	id: string;
	name: string;
	description: string;
	status: string;
	accent_color: string | null;
	path: string | null;
	projects_root: string;
};

export type AgentRow = {
	thread_id: string;
	display_name: string;
	project_id?: string | null;
	responsibility: string;
	responsibility_paths?: string[];
	responsibility_domains?: string[];
	coordination_notes?: string | null;
	active: boolean;
};
