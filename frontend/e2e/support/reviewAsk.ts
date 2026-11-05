import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');

/**
 * Raises an agent's review ask on an artifact through the plan service as the owning agent thread (an agent's
 * `block_node` with `ask_artifact_id`), in the isolated E2E mock state. It writes the database directly, so an open
 * document's review bar shows the ask on its next refresh (any artifact or plan change).
 */
export function raiseAsk(threadId: string, artifactId: string, reason: string): void {
	const state = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !state) throw new Error('The ask fixture requires isolated E2E mock state');
	execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), ['-c', [
		'import sys, uuid',
		'from infra.database import get_db',
		'from plans import service',
		'from plans.boundary import PlanActor, mutate',
		'thread, artifact, reason = sys.argv[1:]',
		'db = get_db()',
		'user = db.execute("SELECT user_id FROM threads WHERE id=?", (thread,)).fetchone()["user_id"]',
		'plan = service.ensure_plan("thread", thread, db=db)',
		'node = service.add_node(plan.id, kind="task", title="Review the proposal", acceptance_criteria="Reviewed", db=db)',
		'db.commit()',
		'mutate(plan.id, "block_node", {"node_id": node.id, "reason_key": "review", "reason": reason, "owner_kind": "user", "owner_id": None,',
		'  "ask_artifact_id": artifact, "expected_revision": service._node_row(db, node.id)["revision"]},',
		'  actor=PlanActor(thread, user, None), provenance="e2e.review_bar", mutation_id=uuid.uuid4().hex, db=db)',
		'db.commit()',
	].join('\n'), threadId, artifactId, reason], {
		cwd: path.join(repoRoot, 'backend'), encoding: 'utf8',
		env: { ...process.env, CORSIA_TEST_MODE: '1', CORSIA_STATE_DIR: state, CORSIA_WORKSPACE: process.env.CORSIA_E2E_WORKSPACE ?? '' },
	});
}
