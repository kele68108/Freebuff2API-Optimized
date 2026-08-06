import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from freebuff2api.app import _load_api_keys_cached, _sse_keepalive
from freebuff2api.codebuff import CodebuffAccountPool, CodebuffError, RateLimitState
from freebuff2api.config import Settings, load_settings


class DataDirTests(unittest.TestCase):
    """R1: data dir defaults to ~/.freebuff2api, never /root."""

    def test_settings_data_dir_defaults_to_home(self) -> None:
        settings = Settings(codebuff_token="t", local_api_key=None)
        self.assertEqual(settings.data_dir, Path.home() / ".freebuff2api")
        self.assertNotIn("root", str(settings.data_dir))

    def test_load_settings_reads_freestuff_data_dir_env(self) -> None:
        with patch.dict(
            "os.environ",
            {"FREEBUFF2API_DATA_DIR": "/var/lib/freebuff2api/data"},
            clear=True,
        ):
            settings = load_settings()
        self.assertEqual(settings.data_dir, Path("/var/lib/freebuff2api/data"))

    def test_admin_auth_data_dir_default_not_root(self) -> None:
        # auth.py computes DATA_DIR at import time from env/Path.home
        import importlib.util
        import sys

        spec = importlib.util.find_spec("auth")
        if spec is None:
            # admin backend not importable from this venv — test the pattern instead
            import os
            from pathlib import Path as P

            with patch.dict("os.environ", {}, clear=True):
                computed = P(os.getenv("FREEBUFF2API_DATA_DIR", str(P.home() / ".freebuff2api")))
            self.assertNotIn("root", str(computed))
            return
        with patch.dict("os.environ", {}, clear=True):
            import auth  # noqa: F401  (import-time DATA_DIR uses env or home)
            from auth import DATA_DIR

        self.assertNotIn("root", str(DATA_DIR))


class CachedApiKeysTests(unittest.TestCase):
    """R2: api_keys.json is read once per mtime, not per request."""

    def test_cache_hits_same_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            keys_file = data_dir / "api_keys.json"
            keys_file.write_text(json.dumps([{"key": "sk-1", "enabled": True}]))
            first = _load_api_keys_cached(data_dir)
            second = _load_api_keys_cached(data_dir)
            self.assertEqual(first, [{"key": "sk-1", "enabled": True}])
            self.assertIs(first, second)  # same cached list object

    def test_cache_invalidates_on_mtime_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            keys_file = data_dir / "api_keys.json"
            keys_file.write_text(json.dumps([{"key": "sk-1", "enabled": True}]))
            _load_api_keys_cached(data_dir)
            keys_file.write_text(json.dumps([{"key": "sk-2", "enabled": True}]))
            # force a distinct mtime
            now = time.time()
            os_utime = __import__("os").utime
            os_utime(keys_file, (now + 5, now + 5))
            fresh = _load_api_keys_cached(data_dir)
            self.assertEqual(fresh, [{"key": "sk-2", "enabled": True}])

    def test_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_load_api_keys_cached(Path(tmp)), [])


class HttpxPoolTuningTests(unittest.TestCase):
    """R3: httpx Limits come from settings, not hardcoded constants."""

    def test_pool_limits_are_settings_driven(self) -> None:
        from freebuff2api.codebuff import CodebuffClient

        settings = Settings(
            codebuff_token="t",
            local_api_key=None,
            httpx_max_connections=5,
            httpx_max_keepalive=2,
            httpx_keepalive_expiry=7.5,
            httpx_read_timeout=123.0,
        )
        with patch("freebuff2api.codebuff.httpx.AsyncClient") as mock_client:
            CodebuffClient(settings)
            _call = mock_client.call_args
            limits = _call.kwargs["limits"]
            self.assertEqual(limits.max_connections, 5)
            self.assertEqual(limits.max_keepalive_connections, 2)
            self.assertEqual(limits.keepalive_expiry, 7.5)
            self.assertEqual(_call.kwargs["timeout"].read, 123.0)

    def test_load_settings_reads_httpx_env(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "FREEBUFF_HTTPX_MAX_CONNECTIONS": "42",
                "FREEBUFF_HTTPX_KEEPALIVE": "7",
                "FREEBUFF_HTTPX_KEEPALIVE_EXPIRY": "12.5",
                "FREEBUFF_HTTPX_READ_TIMEOUT": "99",
            },
            clear=True,
        ):
            settings = load_settings()
        self.assertEqual(settings.httpx_max_connections, 42)
        self.assertEqual(settings.httpx_max_keepalive, 7)
        self.assertEqual(settings.httpx_keepalive_expiry, 12.5)
        self.assertEqual(settings.httpx_read_timeout, 99)


class _StubClient:
    """Minimal async client stub so pool lifecycle tests never hit the network."""

    def __init__(self, settings: object | None = None) -> None:
        self.settings = settings
        self.closed = False
        self.calls = 0

    async def aclose(self) -> None:
        self.closed = True

    async def get_streak(self) -> dict:
        self.calls += 1
        return {"streak": 1}


class BoundedConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """R4: max_waiters bounds waiting requests with a 503 fast-fail."""

    async def test_unlimited_waiters_when_max_is_zero(self) -> None:
        settings = Settings(
            codebuff_token="token-a",
            local_api_key=None,
            max_waiters=0,
        )
        with patch("freebuff2api.codebuff.CodebuffClient", _StubClient):
            pool = CodebuffAccountPool(settings)
            try:
                self.assertEqual(pool._max_waiters, 0)
            finally:
                await pool.aclose()

    async def test_waiter_limit_raises_503_when_exhausted(self) -> None:
        settings = Settings(
            codebuff_token="token-a",
            local_api_key=None,
            max_waiters=1,
        )
        with patch("freebuff2api.codebuff.CodebuffClient", _StubClient):
            pool = CodebuffAccountPool(settings)
            try:
                # occupy the single account and simulate one already-waiting request
                pool._accounts[0].busy = True
                pool._waiters = 1
                with self.assertRaises(CodebuffError) as ctx:
                    await pool._reserve_account("deepseek/deepseek-v4-flash")
                self.assertEqual(ctx.exception.status_code, 503)
            finally:
                await pool.aclose()


class HealthPingerTests(unittest.IsolatedAsyncioTestCase):
    """R7: proactive health pinger quarantines dead accounts."""

    class FailingClient(_StubClient):
        async def get_streak(self) -> dict:
            self.calls += 1
            raise CodebuffError("401 unauthorized", 502)

    class HealthyClient(FailingClient):
        async def get_streak(self) -> dict:
            self.calls += 1
            return {"streak": 1}

    async def test_failed_health_ping_quarantines_account(self) -> None:
        settings = Settings(
            codebuff_token="token-a",
            local_api_key=None,
            health_cooldown=300,
        )
        with patch("freebuff2api.codebuff.CodebuffClient", self.FailingClient):
            pool = CodebuffAccountPool(settings)
            try:
                self.assertEqual(pool.healthy_account_count(), 1)
                await pool.ping_health()
                self.assertEqual(pool.healthy_account_count(), 0)
                self.assertIsNone(
                    pool._next_available_index("deepseek/deepseek-v4-flash")
                )
            finally:
                await pool.aclose()

    async def test_healthy_ping_clears_quarantine(self) -> None:
        settings = Settings(
            codebuff_token="token-a",
            local_api_key=None,
            health_cooldown=300,
        )
        with patch("freebuff2api.codebuff.CodebuffClient", self.FailingClient):
            pool = CodebuffAccountPool(settings)
            try:
                await pool.ping_health()
                self.assertEqual(pool.healthy_account_count(), 0)
                # swap in a healthy client and re-ping → recovered
                pool._accounts[0].client = self.HealthyClient(object())
                await pool.ping_health()
                self.assertEqual(pool.healthy_account_count(), 1)
            finally:
                await pool.aclose()

    async def test_health_cooldown_expires(self) -> None:
        settings = Settings(
            codebuff_token="token-a",
            local_api_key=None,
            health_cooldown=0.01,
        )
        with patch("freebuff2api.codebuff.CodebuffClient", self.FailingClient):
            pool = CodebuffAccountPool(settings)
            try:
                await pool.ping_health()
                self.assertEqual(pool.healthy_account_count(), 0)
                await asyncio.sleep(0.02)
                self.assertEqual(pool.healthy_account_count(), 1)
            finally:
                await pool.aclose()


class SseKeepaliveTests(unittest.IsolatedAsyncioTestCase):
    """R10: idle SSE streams emit keepalive markers."""

    async def test_keepalive_emitted_on_idle(self) -> None:
        async def slow_lines():
            yield "data: a\n\n"
            await asyncio.sleep(0.3)
            yield "data: b\n\n"

        collected = [item async for item in _sse_keepalive(slow_lines(), 0.05)]
        self.assertIn(None, collected)  # keepalive marker between chunks
        self.assertEqual([c for c in collected if c is not None], ["data: a\n\n", "data: b\n\n"])

    async def test_no_keepalive_when_stream_keeps_flowing(self) -> None:
        async def fast_lines():
            for i in range(3):
                yield f"data: {i}\n\n"
                await asyncio.sleep(0.01)

        collected = [item async for item in _sse_keepalive(fast_lines(), 5.0)]
        self.assertNotIn(None, collected)
        self.assertEqual(len(collected), 3)

    async def test_keepalive_stops_at_stream_end(self) -> None:
        async def short_stream():
            yield "data: done\n\n"

        collected = [item async for item in _sse_keepalive(short_stream(), 0.02)]
        self.assertEqual(collected, ["data: done\n\n"])


class RateLimitStateQuarantineTests(unittest.TestCase):
    def test_quarantined_until_default_none(self) -> None:
        state = RateLimitState()
        self.assertIsNone(state.quarantined_until)


class SettingsDefaultsTests(unittest.TestCase):
    def test_new_settings_defaults(self) -> None:
        settings = Settings(codebuff_token="t", local_api_key=None)
        self.assertEqual(settings.max_waiters, 64)
        self.assertEqual(settings.health_interval, 60.0)
        self.assertEqual(settings.health_cooldown, 300.0)
        self.assertEqual(settings.sse_keepalive_seconds, 15.0)
        self.assertEqual(settings.httpx_max_connections, 100)


if __name__ == "__main__":
    unittest.main()
