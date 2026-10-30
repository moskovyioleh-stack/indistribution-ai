import asyncio
import unittest
from unittest.mock import patch
import tilda_contracts as contracts


class PacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_internal_requests_are_spaced(self):
        clock = [100.0]
        starts = []
        async def sleep(delay):
            clock[0] += delay
        async def operation():
            starts.append(clock[0])
            return 'OK'
        with patch.dict('os.environ', {'TILDA_MCP_MIN_INTERVAL_SECONDS': '1'}), \
             patch.object(contracts, '_upstream_gate', asyncio.Lock()), \
             patch.object(contracts, '_next_upstream_call', 0), \
             patch.object(contracts.time, 'monotonic', lambda: clock[0]), \
             patch.object(contracts.asyncio, 'sleep', sleep):
            for _ in range(7):
                self.assertEqual(await contracts.paced_upstream(operation), 'OK')
        self.assertEqual(starts, [100, 101, 102, 103, 104, 105, 106])

    async def test_rate_error_defers_next_call_without_retrying_write(self):
        clock = [100.0]
        attempts = []
        async def sleep(delay):
            clock[0] += delay
        async def failed_write():
            attempts.append(clock[0])
            raise RuntimeError('Too many requests: private data')
        async def next_read():
            attempts.append(clock[0])
        with patch.dict('os.environ', {'TILDA_MCP_MIN_INTERVAL_SECONDS': '1'}), \
             patch.object(contracts, '_upstream_gate', asyncio.Lock()), \
             patch.object(contracts, '_next_upstream_call', 0), \
             patch.object(contracts.time, 'monotonic', lambda: clock[0]), \
             patch.object(contracts.asyncio, 'sleep', sleep):
            with self.assertRaises(RuntimeError):
                await contracts.paced_upstream(failed_write)
            self.assertEqual(len(attempts), 1)
            await contracts.paced_upstream(next_read)
        self.assertEqual(attempts, [100, 130])
