import asyncio
import errno
import io
import json
import logging
import os
import struct
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from infra import agent_pool, agent_pool_child


class FramingTests(unittest.TestCase):
    def test_frame_rejects_malformed_and_oversize(self):
        for data in (b'\x00\x00', struct.pack('!I', agent_pool_child.MAX_FRAME + 1), struct.pack('!I', 1) + b'{'):
            with self.assertRaises((ValueError, EOFError, json.JSONDecodeError)):
                agent_pool_child.read_frame(io.BytesIO(data))
        with self.assertRaises(ValueError):
            agent_pool._encode({'data': 'x' * agent_pool_child.MAX_FRAME})

    def test_ceiling_and_scrub(self):
        for cpus, expected in ((None, 1), (1, 1), (8, 2), (100, 6)):
            with patch.object(os, 'cpu_count', return_value=cpus):
                self.assertEqual(agent_pool.core_ceiling(), expected)
        with patch.dict(os.environ, {'SECRET_BEARER_TOKEN': 'secret'}):
            self.assertNotIn('SECRET_BEARER_TOKEN', agent_pool._environment())

    def test_unix_priority_success(self):
        if os.name == 'nt':
            self.skipTest('Unix priority path')
        with patch.object(os, 'nice') as nice:
            agent_pool_child.lower_priority()
            nice.assert_called_once_with(10)

    @unittest.skipUnless(os.name == 'nt', 'Windows priority path')
    def test_windows_priority_success(self):
        import ctypes
        with patch.object(ctypes, 'windll') as windll:
            windll.kernel32.GetCurrentProcess.return_value = 123
            windll.kernel32.SetPriorityClass.return_value = 1
            agent_pool_child.lower_priority()
            windll.kernel32.SetPriorityClass.assert_called_once_with(123, 0x00004000)

    def test_priority_failure_logged(self):
        if os.name == 'nt':
            self.skipTest('Unix priority path')
        with patch.object(os, 'nice', side_effect=OSError(errno.EPERM, 'denied')), self.assertLogs('infra.agent_pool_child', level='WARNING') as logs:
            self.assertEqual(agent_pool_child.lower_priority(), 'permission_denied')
        self.assertEqual(len(logs.records), 1)
        self.assertIn('errno=1', logs.output[0])

    def test_child_serves_call_when_priority_denied(self):
        if os.name == 'nt':
            self.skipTest('Unix priority path')
        identity = {'call_id': 'call', 'agent_id': 'agent', 'thread_id': 'thread'}
        incoming, outgoing = io.BytesIO(), io.BytesIO()
        agent_pool_child.write_frame(incoming, {'version': 1, 'type': 'call', 'identity': identity,
                                                 'operation': 'fixture.echo', 'payload': 'ok'})
        agent_pool_child.write_frame(incoming, {'version': 1, 'type': 'cancel'})
        incoming.seek(0)
        with patch.object(os, 'nice', side_effect=PermissionError(errno.EPERM, 'denied')), \
             patch.object(sys, 'stdin', SimpleNamespace(buffer=incoming)), \
             patch.object(sys, 'stdout', SimpleNamespace(buffer=outgoing)), \
             self.assertLogs('infra.agent_pool_child', level='WARNING') as logs:
            agent_pool_child.main()
        outgoing.seek(0)
        self.assertEqual(agent_pool_child.read_frame(outgoing)['priority'], 'permission_denied')
        self.assertEqual(agent_pool_child.read_frame(outgoing)['payload'], 'ok')
        self.assertEqual(len(logs.records), 1)
        self.assertIn('errno=1', logs.output[0])


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = agent_pool.AgentPool(limit=1, idle_seconds=.3, cancel_grace=.05)
        self.metrics = []
        self.assigned = asyncio.Event()
        def record(seconds, outcome):
            self.metrics.append(outcome)
            if outcome == 'assigned':
                self.assigned.set()
        self.metric_patch = patch('infra.worker_wait_timing.record_worker_wait', side_effect=record)
        self.metric_patch.start()

    async def asyncTearDown(self):
        await self.pool.close()
        self.metric_patch.stop()

    async def call(self, payload, **kw):
        return await self.pool.call('fixture.echo', payload, agent_id='agent', thread_id='thread', queue_timeout=kw.get('queue_timeout', 2), execution_timeout=kw.get('execution_timeout', 2))

    async def test_echo_idle_and_replacement(self):
        self.assertEqual(await self.call({'a': 1}), {'a': 1})
        first = next(iter(self.pool._workers))
        await asyncio.wait_for(first.wait(), 2)
        self.assertEqual(await self.call('again'), 'again')
        self.assertIsNot(first, next(iter(self.pool._workers)))
        self.assertEqual(self.metrics, ['assigned', 'assigned'])

    async def test_unexpected_idle_exit_is_reaped_and_replaced(self):
        await self.call('first')
        old = next(iter(self.pool._workers))
        old.kill()
        await old.wait()
        self.assertEqual(await self.call('next'), 'next')
        self.assertNotIn(old, self.pool._workers)

    async def test_cancel_when_child_stops_reading_stdin(self):
        import sys
        real_spawn = asyncio.create_subprocess_exec
        code = ("import json,struct,sys,time; "
                "b=json.dumps({'version':1,'type':'ready','priority':'lowered'}).encode(); "
                "sys.stdout.buffer.write(struct.pack('!I',len(b))+b); "
                "sys.stdout.buffer.flush(); time.sleep(30)")
        async def spawn_stalled(*args, **kwargs):
            return await real_spawn(sys.executable, '-c', code, **kwargs)
        with patch.object(agent_pool.asyncio, 'create_subprocess_exec', side_effect=spawn_stalled):
            call = asyncio.create_task(self.call('x' * (agent_pool.MAX_FRAME - 500), execution_timeout=20))
            await asyncio.wait_for(self.assigned.wait(), 3)
            proc = next(iter(self.pool._workers))
            async with asyncio.timeout(3):
                while proc.stdin.transport.get_write_buffer_size() < 65536:
                    await asyncio.sleep(.01)
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(call, 3)
            await asyncio.wait_for(self.pool.close(), 3)
            self.assertIsNotNone(proc.returncode)

    async def test_cancel_when_child_stdout_is_backpressured(self):
        real_read = agent_pool._read_frame
        response_wait = asyncio.Event()
        async def stall_response(stream):
            if self.assigned.is_set():
                response_wait.set()
                await asyncio.Event().wait()
            return await real_read(stream)
        with patch.object(agent_pool, '_read_frame', side_effect=stall_response):
            call = asyncio.create_task(self.call('x' * (agent_pool.MAX_FRAME - 500), execution_timeout=20))
            await asyncio.wait_for(response_wait.wait(), 3)
            proc = next(iter(self.pool._workers))
            async with asyncio.timeout(3):
                while not proc.stdout._paused:
                    await asyncio.sleep(.01)
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(call, 3)
            await asyncio.wait_for(self.pool.close(), 3)
            self.assertIsNotNone(proc.returncode)

    async def test_crash_fails_and_next_replaces(self):
        with self.assertRaises(agent_pool.WorkerFailed):
            await self.call({'_test_crash': True})
        self.assertEqual(await self.call('ok'), 'ok')

    async def test_deadline_saturation_and_cancel(self):
        running = asyncio.create_task(self.call({'_test_delay': 10}, execution_timeout=20))
        await asyncio.wait_for(self.assigned.wait(), 2)
        with self.assertRaises(asyncio.TimeoutError):
            await self.call('queued', queue_timeout=.05)
        queued = asyncio.create_task(self.call('cancelled'))
        await asyncio.sleep(.02)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(sorted(self.metrics), ['assigned', 'cancelled', 'timed_out'])
        self.assertEqual(await self.call('recovered'), 'recovered')

    async def test_execution_deadline_and_close_in_flight(self):
        with self.assertRaises(asyncio.TimeoutError):
            await self.call({'_test_delay': 1}, execution_timeout=.05)
        running = asyncio.create_task(self.call({'_test_delay': 1}))
        while len(self.metrics) < 2:
            await asyncio.sleep(.01)
        await self.pool.close()
        with self.assertRaises((agent_pool.WorkerFailed, BrokenPipeError, ConnectionError)):
            await running
        self.assertFalse(self.pool._workers)
        with self.assertRaises(agent_pool.PoolClosed):
            await self.call('closed')

    async def test_idle_expiry_and_assignment_are_serialized(self):
        await self.call('first')
        old = next(iter(self.pool._workers))
        async with self.pool._condition:
            await asyncio.sleep(.35)
            pending = asyncio.create_task(self.call('next'))
            await asyncio.sleep(.02)
        self.assertEqual(await pending, 'next')
        self.assertLessEqual(len(self.pool._workers), 1)
        if old not in self.pool._workers:
            self.assertIsNotNone(old.returncode)

    async def test_simultaneous_close_and_call_reaps_all(self):
        task = asyncio.create_task(self.call({'_test_delay': 1}))
        closing = asyncio.create_task(self.pool.close())
        results = await asyncio.gather(task, closing, return_exceptions=True)
        self.assertIsInstance(results[0], (agent_pool.PoolClosed, agent_pool.WorkerFailed))
        self.assertIsNone(results[1])
        self.assertFalse(self.pool._workers)
        self.assertEqual(self.pool._reserved, 0)

    async def test_real_child_priority(self):
        if os.name == 'nt' or not hasattr(os, 'getpriority'):
            self.skipTest('Unix getpriority required')
        probe = subprocess.run(
            [sys.executable, '-c', 'import os; os.nice(10)'], capture_output=True, text=True,
        )
        if probe.returncode and 'PermissionError: [Errno 1]' in probe.stderr:
            self.skipTest('priority probe: PermissionError: [Errno 1] Operation not permitted')
        self.assertEqual(probe.returncode, 0, probe.stderr)
        await self.call('priority')
        proc = next(iter(self.pool._workers))
        self.assertGreater(os.getpriority(os.PRIO_PROCESS, proc.pid), os.getpriority(os.PRIO_PROCESS, 0))

    async def test_computed_ceiling_saturation(self):
        await self.pool.close()
        with patch.object(os, 'cpu_count', return_value=8):
            self.pool = agent_pool.AgentPool(idle_seconds=.3, cancel_grace=.05)
        self.assertEqual(self.pool.limit, 2)
        first = asyncio.create_task(self.call({'_test_delay': 1}))
        second = asyncio.create_task(self.call({'_test_delay': 1}))
        while self.metrics.count('assigned') < 2:
            await asyncio.sleep(.01)
        with self.assertRaises(asyncio.TimeoutError):
            await self.call('third', queue_timeout=.05)
        self.assertEqual(len(self.pool._workers), 2)
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)

    async def test_stalled_spawn_bounds_close_and_late_reap(self):
        self.pool.shutdown_timeout = .05
        gate = asyncio.Event()
        real_spawn = asyncio.create_subprocess_exec
        spawned = []
        async def stalled(*args, **kwargs):
            await gate.wait()
            proc = await real_spawn(*args, **kwargs)
            spawned.append(proc)
            return proc
        with patch.object(agent_pool.asyncio, 'create_subprocess_exec', side_effect=stalled):
            with self.assertRaises(asyncio.TimeoutError):
                await self.call('x', queue_timeout=.05)
            with self.assertRaisesRegex(agent_pool.PoolError, 'shutdown timed out'):
                await asyncio.wait_for(self.pool.close(), .5)
            self.assertTrue(self.pool._pending_cleanup)
            gate.set()
            self.pool.shutdown_timeout = 3
            await self.pool.close()
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].returncode)
        self.assertFalse(self.pool._pending_cleanup)

    async def test_spawn_stall_deadline_and_late_reap(self):
        gate = asyncio.Event()
        real_spawn = asyncio.create_subprocess_exec
        spawned = []
        async def stalled(*args, **kwargs):
            await gate.wait()
            proc = await real_spawn(*args, **kwargs)
            spawned.append(proc)
            return proc
        with patch.object(agent_pool.asyncio, 'create_subprocess_exec', side_effect=stalled):
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(self.call('x', queue_timeout=.05), .5)
            self.assertEqual(self.pool._reserved, 1)
            gate.set()
            await asyncio.wait_for(self.pool.close(), 3)
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].returncode)
        self.assertFalse(self.pool._pending_cleanup)

    async def test_cancel_during_readiness_reaps_and_joins_timers(self):
        entered = asyncio.Event()
        async def stall_ready(stream):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(agent_pool, '_read_frame', side_effect=stall_ready):
            task = asyncio.create_task(self.call('x'))
            await asyncio.wait_for(entered.wait(), 2)
            proc = next(iter(self.pool._workers))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await asyncio.wait_for(self.pool.close(), 3)
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(self.pool._workers)

    async def test_cancel_while_returning_worker_reaps_it(self):
        result_ready = asyncio.Event()
        original = agent_pool._read_frame
        async def observe(stream):
            value = await original(stream)
            if value.get('type') == 'result':
                result_ready.set()
            return value
        with patch.object(agent_pool, '_read_frame', side_effect=observe):
            task = asyncio.create_task(self.call('x'))
            await asyncio.wait_for(self.assigned.wait(), 2)
            proc = next(iter(self.pool._workers))
            async with self.pool._condition:
                await asyncio.wait_for(result_ready.wait(), 2)
                await asyncio.sleep(.01)
                task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await self.pool.close()
        self.assertIsNotNone(proc.returncode)

    async def test_invalid_ready_frame_fails_assignment(self):
        with patch.object(agent_pool, '_read_frame', return_value={'version': 2, 'type': 'ready'}):
            with self.assertRaises(agent_pool.WorkerFailed):
                await self.call('x')
        await self.pool.close()
        self.assertFalse(self.pool._workers)
        self.assertEqual(self.metrics, ['failed'])

    async def test_child_protocol_failure_reaches_caller(self):
        original = agent_pool._encode
        def invalid_call(value):
            if value.get('type') == 'call':
                value = {**value, 'operation': 'invalid'}
            return original(value)
        with patch.object(agent_pool, '_encode', side_effect=invalid_call):
            with self.assertRaisesRegex(agent_pool.WorkerFailed, 'ValueError: unknown operation'):
                await self.call('x')
        await self.pool.close()
        self.assertFalse(self.pool._workers)

    async def test_stderr_is_drained_with_bounded_capture(self):
        reader = asyncio.StreamReader()
        worker = agent_pool.Worker(SimpleNamespace(stderr=reader))
        task = asyncio.create_task(agent_pool.capture_stderr(worker))
        reader.feed_data(b'x' * 200000 + b'\nworker error: bad frame\n')
        await asyncio.wait_for(worker.error_reported.wait(), 1)
        self.assertEqual(worker.error_detail, 'worker error: bad frame')
        reader.feed_eof()
        await task

    async def test_cause_arrives_while_stderr_remains_open(self):
        real_spawn = asyncio.create_subprocess_exec
        code = ("import json,struct,sys,time; "
                "b=json.dumps({'version':1,'type':'ready','priority':'lowered'}).encode(); "
                "sys.stdout.buffer.write(struct.pack('!I',len(b))+b); sys.stdout.buffer.flush(); "
                "n=struct.unpack('!I',sys.stdin.buffer.read(4))[0]; sys.stdin.buffer.read(n); "
                "sys.stderr.write('worker error: ValueError: kept open\\n'); sys.stderr.flush(); "
                "sys.stdout.buffer.write(struct.pack('!I',1)+b'{'); sys.stdout.buffer.flush(); "
                "time.sleep(30)")
        async def spawn(*args, **kwargs):
            return await real_spawn(sys.executable, '-c', code, **kwargs)
        with patch.object(agent_pool.asyncio, 'create_subprocess_exec', side_effect=spawn):
            with self.assertRaisesRegex(agent_pool.WorkerFailed, 'ValueError: kept open'):
                await asyncio.wait_for(self.call('x'), 1)
            proc = next(iter(self.pool._workers))
            self.assertIsNone(proc.returncode)
        await self.pool.close()
        self.assertIsNotNone(proc.returncode)

    async def test_priority_denial_warns_once_in_parent(self):
        if os.name == 'nt':
            self.skipTest('Unix priority path')
        real_spawn = asyncio.create_subprocess_exec
        code = ("import os; "
                "os.nice=lambda n: (_ for _ in ()).throw(PermissionError(1, 'denied')); "
                "from infra.agent_pool_child import main; main()")
        async def spawn(*args, **kwargs):
            return await real_spawn(sys.executable, '-c', code, **kwargs)
        with patch.object(agent_pool.asyncio, 'create_subprocess_exec', side_effect=spawn), \
             self.assertLogs('infra.agent_pool', level=logging.WARNING) as logs:
            self.assertEqual(await self.call('ok'), 'ok')
            await self.pool.close()
        self.assertEqual(len(logs.records), 1)
        self.assertIn('errno=1', logs.output[0])
        self.assertIn('denied', logs.output[0])

    async def test_parent_rejects_bad_frames(self):
        for raw in (struct.pack('!I', agent_pool.MAX_FRAME + 1), struct.pack('!I', 1) + b'{'):
            reader = asyncio.StreamReader()
            reader.feed_data(raw)
            reader.feed_eof()
            with self.assertRaises(agent_pool.WorkerFailed):
                await agent_pool._read_frame(reader)
