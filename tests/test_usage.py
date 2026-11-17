import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import urllib.error


SCRIPT = Path(__file__).resolve().parents[1] / "ai-usage"


class UsageTests(unittest.TestCase):
    def setUp(self):
        loader = importlib.machinery.SourceFileLoader("usage_cli", str(SCRIPT))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.cli = importlib.util.module_from_spec(spec)
        loader.exec_module(self.cli)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_text(json.dumps({"tokens": {
            "access_token": "test-codex-token", "account_id": "test-account"}}))
        self.enterContext(patch.dict(os.environ, {"HOME": str(home), "NO_COLOR": "1"}))
        self.cli.CACHE_DIR = str(home / "cache")
        self.cli.COLOR = False
        self.now = datetime.now(timezone.utc)
        self.expiry = (self.now + timedelta(days=4)).isoformat()
        self.payloads = {
            "https://api.anthropic.com/api/oauth/usage": {"seven_day": {
                "utilization": 30, "resets_at": (self.now + timedelta(days=3.5)).isoformat()}},
            "https://chatgpt.com/backend-api/wham/usage": {"rate_limit": {
                "primary_window": {"used_percent": 80, "limit_window_seconds": 604800,
                                   "reset_at": (self.now + timedelta(days=3.5)).timestamp()}}},
            "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits": {
                "available_count": 1, "credits": [self.credit()]},
        }
        self.requests = []
        self.enterContext(patch.object(self.cli.urllib.request, "urlopen", side_effect=self.urlopen))
        self.enterContext(patch.object(self.cli.subprocess, "run", return_value=
            subprocess.CompletedProcess([], 0, json.dumps({"claudeAiOauth": {
                "accessToken": "test-claude-token"}}))))

    def credit(self, **changes):
        return {"status": "available", "is_supported_by_plan": True,
                "reset_type": "codex_rate_limits", "expires_at": self.expiry, **changes}

    @property
    def reset_payload(self):
        return self.payloads["https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"]

    def urlopen(self, request, timeout):
        self.requests.append(request)
        payload = self.payloads[request.full_url]
        if isinstance(payload, Exception):
            raise payload
        return io.StringIO(json.dumps(payload))

    def run_cli(self, *args):
        output = io.StringIO()
        with patch.object(sys, "argv", [str(SCRIPT), *args]), contextlib.redirect_stdout(output):
            code = self.cli.main()
        return code, output.getvalue()

    def test_default_shows_count_and_expiry_with_read_only_authenticated_requests(self):
        code, output = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("resets 1 (next expires in ", output)
        self.assertIn("selection pace -70.0 pts", output)
        self.assertNotIn("test-codex-token", output)
        self.assertEqual(len(self.requests), 3)
        for request in self.requests:
            self.assertEqual(request.get_method(), "GET")
            if request.host == "chatgpt.com":
                self.assertEqual(request.get_header("Authorization"), "Bearer test-codex-token")
                self.assertEqual(request.get_header("Chatgpt-account-id"), "test-account")

    def test_pick_accounts_for_reset_reserve(self):
        self.assertEqual(self.run_cli("--pick"), (0, "codex\n"))

    def test_pick_without_resets_preserves_weekly_pace_rule(self):
        self.reset_payload["credits"] = []
        self.assertEqual(self.run_cli("--pick"), (0, "claude-code\n"))

    def test_agent_keeps_actual_usage_and_exposes_selection_pace(self):
        _, output = self.run_cli("--agent")
        result = json.loads(output)
        codex = result["harnesses"]["codex"]
        self.assertEqual(result["recommended_harness"], "codex")
        self.assertEqual(codex["weekly"]["used_pct"], 80)
        self.assertEqual(codex["weekly"]["pace_delta_pct"], 30)
        self.assertEqual(codex["selection_pace_pct"], -70)
        self.assertEqual(codex["resets"], {"available_count": 1, "expires_at": [self.expiry]})
        self.assertNotIn("_reset", output)

    def test_only_unexpired_supported_available_full_resets_count(self):
        earlier = (self.now + timedelta(days=1)).isoformat()
        self.reset_payload["available_count"] = 99
        self.reset_payload["credits"] += [
            self.credit(expires_at=earlier),
            self.credit(expires_at=(self.now - timedelta(seconds=1)).isoformat()),
            self.credit(status="redeemed"), self.credit(status="expired"),
            self.credit(is_supported_by_plan=False), self.credit(reset_type="other"),
        ]
        _, output = self.run_cli("--agent")
        resets = json.loads(output)["harnesses"]["codex"]["resets"]
        self.assertEqual(resets, {"available_count": 2, "expires_at": [earlier, self.expiry]})

    def test_reset_failure_keeps_usage_and_falls_back_to_existing_pick(self):
        self.payloads["https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"] = (
            urllib.error.HTTPError("unused", 503, "Unavailable", {}, None))
        self.assertEqual(self.run_cli("--pick"), (0, "claude-code\n"))
        _, output = self.run_cli()
        self.assertIn("resets unknown", output)
        _, output = self.run_cli("--agent")
        codex = json.loads(output)["harnesses"]["codex"]
        self.assertEqual(codex["weekly"]["used_pct"], 80)
        self.assertIsNone(codex["resets"]["available_count"])
        self.assertIn("503", codex["resets"]["error"])

    def test_missing_reset_schema_is_unknown(self):
        self.reset_payload.clear()
        _, output = self.run_cli("--agent")
        resets = json.loads(output)["harnesses"]["codex"]["resets"]
        self.assertIsNone(resets["available_count"])

    def test_exhausted_weekly_quota_still_skipped_with_banked_resets(self):
        self.payloads["https://chatgpt.com/backend-api/wham/usage"]["rate_limit"]["primary_window"]["used_percent"] = 100
        self.assertEqual(self.run_cli("--pick"), (0, "claude-code\n"))
        _, output = self.run_cli("--agent")
        self.assertIn("redeem one to restore quota", json.loads(output)["reason"])

    def test_pick_none_when_all_exhausted_even_with_resets(self):
        self.payloads["https://api.anthropic.com/api/oauth/usage"]["seven_day"]["utilization"] = 100
        self.payloads["https://chatgpt.com/backend-api/wham/usage"]["rate_limit"]["primary_window"]["used_percent"] = 100
        self.assertEqual(self.run_cli("--pick"), (1, "none\n"))

    def test_five_hour_gate_and_wait_use_reset_adjusted_pace(self):
        limits = self.payloads["https://chatgpt.com/backend-api/wham/usage"]["rate_limit"]
        limits["secondary_window"] = {"used_percent": 95, "limit_window_seconds": 18000,
                                      "reset_at": (self.now + timedelta(minutes=30)).timestamp()}
        self.assertEqual(self.run_cli("--pick"), (0, "claude-code\n"))
        _, output = self.run_cli("--agent")
        wait = json.loads(output)["wait_option"]
        self.assertEqual(wait["harness"], "codex")
        self.assertEqual(wait["pace_delta_pct"], 30)
        self.assertEqual(wait["selection_pace_pct"], -70)

    def test_cached_responses_do_not_count_expired_resets(self):
        cache = Path(self.cli.CACHE_DIR)
        cache.mkdir()
        (cache / "codex-resets.json").write_text(json.dumps({"available_count": 1, "credits": [
            self.credit(expires_at=(self.now - timedelta(seconds=1)).isoformat())]}))
        _, output = self.run_cli("--agent")
        self.assertEqual(json.loads(output)["harnesses"]["codex"]["resets"]["available_count"], 0)
        self.assertEqual(len(self.requests), 2)

    def test_all_three_endpoints_are_cached(self):
        self.run_cli()
        self.run_cli("--pick")
        self.assertEqual(len(self.requests), 3)

    CLAUDE_URL = "https://api.anthropic.com/api/oauth/usage"

    def claude_requests(self):
        return [r for r in self.requests if r.full_url == self.CLAUDE_URL]

    def write_snapshot(self, written_ago=10, week_reset_in=timedelta(days=2), used=40):
        cache = Path(self.cli.CACHE_DIR)
        cache.mkdir(exist_ok=True)
        (cache / "claude-statusline.json").write_text(json.dumps({
            "written_at": self.now.timestamp() - written_ago,
            "rate_limits": {
                "seven_day": {"used_percentage": used,
                              "resets_at": (self.now + week_reset_in).timestamp()},
                "five_hour": {"used_percentage": 20,
                              "resets_at": (self.now + timedelta(hours=2)).timestamp()}}}))

    def rate_limit_claude(self, retry_after="600"):
        self.payloads[self.CLAUDE_URL] = urllib.error.HTTPError(
            self.CLAUDE_URL, 429, "Too Many Requests", {"Retry-After": retry_after}, None)

    def test_recent_statusline_snapshot_replaces_claude_request(self):
        self.write_snapshot()
        _, output = self.run_cli("--agent")
        claude = json.loads(output)["harnesses"]["claude-code"]
        self.assertEqual(claude["source"], "statusline")
        self.assertEqual(claude["weekly"]["used_pct"], 40)
        self.assertEqual(claude["five_hour"]["used_pct"], 20)
        self.assertEqual(self.claude_requests(), [])

    def test_old_or_rolled_over_snapshot_falls_back_to_api(self):
        for kwargs in ({"written_ago": self.cli.STATUSLINE_MAX_AGE + 60},
                       {"week_reset_in": timedelta(seconds=-1)}):
            self.write_snapshot(**kwargs)
            for f in Path(self.cli.CACHE_DIR).glob("claude-code.*"):
                f.unlink()
            self.requests.clear()
            _, output = self.run_cli("--agent")
            claude = json.loads(output)["harnesses"]["claude-code"]
            self.assertEqual(claude["source"], "api")
            self.assertEqual(claude["weekly"]["used_pct"], 30)
            self.assertEqual(len(self.claude_requests()), 1)

    def test_claude_response_cached_longer_than_codex(self):
        self.run_cli()
        old = time.time() - 120
        for name in ("claude-code", "codex", "codex-resets"):
            os.utime(Path(self.cli.CACHE_DIR) / f"{name}.json", (old, old))
        self.requests.clear()
        self.run_cli()
        self.assertEqual(self.claude_requests(), [])
        self.assertEqual(len(self.requests), 2)

    def test_429_shows_stale_cache_and_waits_out_retry_after(self):
        self.run_cli()
        old = time.time() - 900
        os.utime(Path(self.cli.CACHE_DIR) / "claude-code.json", (old, old))
        self.rate_limit_claude()
        _, output = self.run_cli("--agent")
        result = json.loads(output)
        self.assertEqual(result["harnesses"]["claude-code"]["stale_minutes"], 15)
        self.assertEqual(result["harnesses"]["claude-code"]["weekly"]["used_pct"], 30)
        _, output = self.run_cli()
        self.assertIn("(stale 15m, rate limited)", output)
        self.assertEqual(len(self.claude_requests()), 2)

    def test_429_without_cache_errors_and_does_not_retry_early(self):
        self.rate_limit_claude(retry_after="0")
        _, output = self.run_cli("--agent")
        claude = json.loads(output)["harnesses"]["claude-code"]
        self.assertIn("rate limited (HTTP 429), retry in 4m", claude["error"])
        self.assertEqual(self.run_cli("--pick"), (0, "codex\n"))
        self.assertEqual(len(self.claude_requests()), 1)

    def test_success_after_retry_after_clears_block(self):
        self.rate_limit_claude()
        self.run_cli()
        blocked = Path(self.cli.CACHE_DIR) / "claude-code.blocked"
        blocked.write_text(str(time.time() - 1))
        self.payloads[self.CLAUDE_URL] = {"seven_day": {
            "utilization": 30, "resets_at": (self.now + timedelta(days=3.5)).isoformat()}}
        _, output = self.run_cli("--agent")
        self.assertNotIn("stale_minutes", json.loads(output)["harnesses"]["claude-code"])
        self.assertFalse(blocked.exists())

    def test_help_does_not_fetch_usage(self):
        code, output = self.run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("never redeemed", output)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
