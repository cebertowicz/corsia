import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { LiveFaultSupervisor, preflight, readExpectedArm } from './live-fault-supervisor.mjs';

test('preflight requires explicit test gates and records a fresh run identity below state', () => {
	const root = fs.mkdtempSync(path.join(os.tmpdir(), 'corsia-live-fault-'));
	const workspaceRaw = path.join(root, 'workspace');
	const stateRaw = path.join(root, 'state');
	fs.mkdirSync(workspaceRaw);
	fs.mkdirSync(stateRaw);
	const workspace = fs.realpathSync(workspaceRaw);
	const state = fs.realpathSync(stateRaw);
	assert.throws(() => preflight({ CORSIA_E2E_LIVE_FAULTS: '1' }), /CORSIA_TEST_MODE=1/);
	const configuration = preflight({
		CORSIA_E2E_LIVE_FAULTS: '1', CORSIA_TEST_MODE: '1', CORSIA_E2E_MOCK: '1', CORSIA_E2E_FRESH_ROOTS: '1',
		CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state, CORSIA_E2E_BACKEND_PORT: '3110', CORSIA_E2E_BACKEND_PYTHON: '/python',
	});
	assert.equal(path.dirname(configuration.runDir), state);
	assert.deepEqual(JSON.parse(fs.readFileSync(path.join(configuration.runDir, 'identity.json'), 'utf8')), { run_id: configuration.runId, workspace, state_dir: state });
	assert.deepEqual(JSON.parse(fs.readFileSync(path.join(configuration.runDir, 'restart.json'), 'utf8')), { case_id: null, restart_count: 0 });
	fs.rmSync(root, { recursive: true, force: true });
});

test('marker requires current case, child and reachable stage', () => {
	const root = fs.mkdtempSync(path.join(os.tmpdir(), 'corsia-arm-contract-'));
	const marker = path.join(root, 'armed-cut.json');
	const arm = { run_id: 'run', case_id: 'case', pid: 101, generation: 1, artifact_id: 'doc', stage: 'choice_apply' };
	try {
		fs.writeFileSync(marker, JSON.stringify(arm));
		fs.writeFileSync(path.join(root, 'restart.json'), JSON.stringify({ case_id: 'case', restart_count: 0 }));
		assert.deepEqual(readExpectedArm(marker, 'run', 101), arm);
		assert.equal(readExpectedArm(marker, 'other', 101), null);
		assert.equal(readExpectedArm(marker, 'run', 202), null);
		fs.writeFileSync(marker, JSON.stringify({ ...arm, stage: 'invented' }));
		assert.equal(readExpectedArm(marker, 'run', 101), null);
		fs.writeFileSync(marker, JSON.stringify(arm));
		fs.writeFileSync(path.join(root, 'restart.json'), JSON.stringify({ case_id: 'old-case', restart_count: 0 }));
		assert.equal(readExpectedArm(marker, 'run', 101), null);
		fs.writeFileSync(path.join(root, 'restart.json'), JSON.stringify({ case_id: 'case', restart_count: 1 }));
		assert.equal(readExpectedArm(marker, 'run', 101), null);
	} finally {
		fs.rmSync(root, { recursive: true, force: true });
	}
});

class FakeChild extends EventEmitter {
	constructor(pid) {
		super();
		this.pid = pid;
		this.exitCode = null;
		this.signalCode = null;
	}

	kill(signal) {
		this.signalCode = signal;
	}
}

test('supervisor restarts once only for a marker bound to the SIGKILLed child', () => {
	const children = [new FakeChild(101), new FakeChild(202)];
	const restarts = [];
	const failures = [];
	let arm = { pid: 101, case_id: 'case-1', artifact_id: 'artifact-1', stage: 'choice_rename', generation: 1 };
	const supervisor = new LiveFaultSupervisor({
		spawnChild: () => children.shift(),
		inspectArm: pid => arm?.pid === pid ? arm : null,
		clearArm: () => { arm = null; },
		recordRestart: record => restarts.push(record),
		onFatal: error => failures.push(error.message),
	});
	const first = supervisor.start();
	first.emit('exit', null, 'SIGKILL');
	assert.deepEqual([...supervisor.restartedCases], ['case-1']);
	assert.deepEqual(restarts, [{ case_id: 'case-1', restart_count: 1, prior_pid: 101, stage: 'choice_rename' }]);
	assert.equal(failures.length, 0);

	arm = { pid: 202, case_id: 'case-1', artifact_id: 'artifact-1', stage: 'choice_apply', generation: 1 };
	supervisor.child.emit('exit', null, 'SIGKILL');
	assert.match(failures[0], /expected one armed SIGKILL only/);
});

test('a new case may restart once without accepting a stale child or repeated case', () => {
	const children = [new FakeChild(1), new FakeChild(2), new FakeChild(3)];
	let arm = { pid: 1, case_id: 'case-1', stage: 'choice_record' };
	const records = [];
	const failures = [];
	const supervisor = new LiveFaultSupervisor({
		spawnChild: () => children.shift(), inspectArm: pid => arm?.pid === pid ? arm : null,
		clearArm: () => { arm = null; }, recordRestart: record => records.push(record),
		onFatal: error => failures.push(error.message),
	});
	const first = supervisor.start();
	first.emit('exit', null, 'SIGKILL');
	arm = { pid: 2, case_id: 'case-2', stage: 'choice_apply' };
	supervisor.child.emit('exit', null, 'SIGKILL');
	assert.deepEqual(records.map(record => [record.case_id, record.restart_count]), [['case-1', 1], ['case-2', 1]]);
	assert.equal(supervisor.child.pid, 3);
	assert.deepEqual(failures, []);
	supervisor.onChildExit(first, null, 'SIGKILL');
	assert.match(failures[0], /owned current child/);
});

test('supervisor fails loudly for an unarmed exit and shuts down only its child', () => {
	const child = new FakeChild(303);
	const failures = [];
	const supervisor = new LiveFaultSupervisor({
		spawnChild: () => child,
		inspectArm: () => null,
		clearArm: () => assert.fail('unexpected arm clear'),
		onFatal: error => failures.push(error.message),
	});
	supervisor.start();
	child.emit('exit', 1, null);
	assert.match(failures[0], /pid=303 exited code=1 signal=null/);

	const running = new FakeChild(404);
	const shutdown = new LiveFaultSupervisor({
		spawnChild: () => running,
		inspectArm: () => null,
		clearArm: () => assert.fail('unexpected arm clear'),
		onFatal: error => assert.fail(error.message),
	});
	shutdown.start();
	shutdown.shutdown();
	assert.equal(running.signalCode, 'SIGTERM');
});
