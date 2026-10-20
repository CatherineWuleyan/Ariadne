"""Tests for configuration precedence, storage, and secret handling."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ariadne.config import (
    Config,
    Runtime,
    mask_secret,
    parse_bool,
    resolve_runtime,
)


class ConfigFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_defaults_and_sparse_save(self) -> None:
        config = Config.load(self.path)
        self.assertFalse(config.exists)
        self.assertEqual(config.get("model.provider"), "deepseek")
        config.set("model.api_key", "sk-test-1234567890")
        config.set("model.model", "custom-model")
        config.save()
        self.assertTrue(self.path.is_file())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["model"]["api_key"], "sk-test-1234567890")
        self.assertEqual(raw["model"]["model"], "custom-model")
        self.assertNotIn("provider", raw["model"])  # default not pinned

        reloaded = Config.load(self.path)
        self.assertTrue(reloaded.exists)
        self.assertEqual(reloaded.get("model.api_key"), "sk-test-1234567890")
        self.assertIsNone(reloaded.get_explicit("model.provider"))
        self.assertEqual(reloaded.get_explicit("model.model"), "custom-model")

    def test_unknown_key_rejected(self) -> None:
        config = Config.load(self.path)
        with self.assertRaises(Exception):
            config.set("model.typo", "x")

    def test_bool_and_payment_validation(self) -> None:
        config = Config.load(self.path)
        self.assertTrue(parse_bool("yes"))
        self.assertFalse(parse_bool("off"))
        config.set("bohr.auto_approve_spend", "true")
        self.assertTrue(config.get("bohr.auto_approve_spend"))
        with self.assertRaises(Exception):
            config.set("bohr.payment_source", "credit-card")

    def test_retrieval_config_roundtrip_and_ranges(self) -> None:
        config = Config.load(self.path)
        config.set("retrieval.queries", "3")
        config.set("retrieval.use_query_planner", "false")
        config.set("retrieval.candidate_limit", "10")
        config.set("retrieval.min_relevance", "0.35")
        config.set("retrieval.use_lkm_extract", "false")
        config.set("retrieval.max_extractions", "2")
        config.save()
        reloaded = Config.load(self.path)
        self.assertEqual(reloaded.get("retrieval.queries"), 3)
        self.assertFalse(reloaded.get("retrieval.use_query_planner"))
        self.assertEqual(reloaded.get("retrieval.candidate_limit"), 10)
        self.assertAlmostEqual(reloaded.get("retrieval.min_relevance"), 0.35)
        self.assertFalse(reloaded.get("retrieval.use_lkm_extract"))
        self.assertEqual(reloaded.get("retrieval.max_extractions"), 2)
        with self.assertRaises(Exception):
            config.set("retrieval.candidate_limit", "1")
        with self.assertRaises(Exception):
            config.set("retrieval.queries", "many")

    def test_mask_never_reveals_secret(self) -> None:
        self.assertEqual(mask_secret(""), "")
        self.assertEqual(mask_secret("short"), "********")
        masked = mask_secret("sk-1234567890abcdef")
        self.assertIn("…", masked)
        self.assertNotIn("1234567890", masked)


class RuntimePrecedenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.config_path = base / "config.json"
        self.agent_env = base / "agent.env"
        self.agent_env.write_text(
            "DEEPSEEK_API_KEY=agent-key\nDEEPSEEK_MODEL=agent-model\n",
            encoding="utf-8",
        )
        config = Config.load(self.config_path)
        config.set("model.api_key", "config-key")
        config.set("model.model", "config-model")
        config.set("bohr.access_key", "config-bohr")
        config.set("bohr.profile", "config-profile")
        config.save()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_env_wins_over_config_and_agent_env(self) -> None:
        config = Config.load(self.config_path)
        with mock.patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "env-key",
            "DEEPSEEK_MODEL": "env-model",
        }, clear=False):
            runtime = resolve_runtime(config, agent_env_path=self.agent_env)
        self.assertEqual(runtime.model_api_key, "env-key")
        self.assertEqual(runtime.model, "env-model")
        self.assertEqual(runtime.sources["model_api_key"], "env:DEEPSEEK_API_KEY")

    def test_config_wins_over_agent_env(self) -> None:
        config = Config.load(self.config_path)
        with mock.patch.dict(os.environ, {}, clear=True):
            runtime = resolve_runtime(config, agent_env_path=self.agent_env)
        self.assertEqual(runtime.model_api_key, "config-key")
        self.assertEqual(runtime.model, "config-model")
        self.assertEqual(runtime.bohr_access_key, "config-bohr")
        self.assertEqual(runtime.bohr_profile, "config-profile")

    def test_agent_env_is_last_resort(self) -> None:
        config = Config.load(self.config_path)
        config.unset("model.api_key")
        config.unset("model.model")
        with mock.patch.dict(os.environ, {}, clear=True):
            runtime = resolve_runtime(config, agent_env_path=self.agent_env)
        self.assertEqual(runtime.model_api_key, "agent-key")
        self.assertEqual(runtime.model, "agent-model")
        self.assertEqual(runtime.sources["model_api_key"], "agent/.env:DEEPSEEK_API_KEY")

    def test_cli_override_wins(self) -> None:
        config = Config.load(self.config_path)
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "env-key"}, clear=False):
            runtime = resolve_runtime(
                config,
                overrides={"model_api_key": "cli-key", "model": "cli-model"},
                agent_env_path=self.agent_env,
            )
        self.assertEqual(runtime.model_api_key, "cli-key")
        self.assertEqual(runtime.model, "cli-model")
        self.assertEqual(runtime.sources["model_api_key"], "cli")

    def test_yes_and_apply_env(self) -> None:
        config = Config.load(self.config_path)
        with mock.patch.dict(os.environ, {}, clear=True):
            runtime = resolve_runtime(config, yes=True, agent_env_path=self.agent_env)
            runtime.apply_env()
            self.assertTrue(runtime.auto_approve_spend)
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "config-key")
            self.assertEqual(os.environ["BOHR_ACCESS_KEY"], "config-bohr")
            self.assertEqual(os.environ["ARIADNE_PYTHON"], runtime.python)
            self.assertEqual(os.environ["ARIADNE_AUTO_APPROVE_SPEND"], "1")

    def test_summary_has_no_secret(self) -> None:
        config = Config.load(self.config_path)
        with mock.patch.dict(os.environ, {}, clear=True):
            runtime = resolve_runtime(config, agent_env_path=self.agent_env)
        text = json.dumps(runtime.summary(), ensure_ascii=False)
        self.assertNotIn("config-key", text)
        self.assertNotIn("config-bohr", text)
        self.assertTrue(runtime.summary()["model"]["api_key_present"])

    def test_invalid_base_url_rejected(self) -> None:
        config = Config.load(self.config_path)
        with self.assertRaises(Exception):
            config.set("model.base_url", "ftp://example.com")


class ConfigCliTest(unittest.TestCase):
    def test_init_no_input_and_show_json(self) -> None:
        from ariadne import cli

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            code = cli.main([
                "config", "init", "--no-input",
                "--api-key", "sk-test-abcdefgh",
                "--model", "m1",
                "--bohr-access-key", "bohr-test-abcdefgh",
                "--bohr-profile", "p1",
                "--config", str(path),
            ])
            self.assertEqual(code, 0)
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            with mock.patch("sys.stdout") as stdout:
                code = cli.main(["config", "show", "--json", "--config", str(path)])
            self.assertEqual(code, 0)
            text = "".join(call.args[0] for call in stdout.write.call_args_list)
            self.assertNotIn("sk-test-abcdefgh", text)
            self.assertNotIn("bohr-test-abcdefgh", text)
            payload = json.loads(text)
            self.assertTrue(payload["runtime"]["model"]["api_key_present"])
            self.assertTrue(payload["runtime"]["bohr"]["access_key_present"])

    def test_doctor_json_does_not_leak_secret(self) -> None:
        from ariadne import cli

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cli.main([
                "config", "init", "--no-input",
                "--api-key", "sk-doctor-secret-1234",
                "--config", str(path),
            ])
            config = Config.load(path)
            runtime = resolve_runtime(config, agent_env_path=None)
            with mock.patch("ariadne.cli.bohr_status", return_value={
                "available": True, "logged_in": True, "profile": "p",
                "auth_method": "access_key", "credential_source": "config",
                "host": "https://open.bohrium.com", "ak_present": True, "error": "",
            }), mock.patch("ariadne.cli.bohr_version", return_value="2.7.14"), \
                    mock.patch("ariadne.cli.python_version", return_value="3.13.7"):
                checks = cli._doctor(runtime)
            text = json.dumps(checks, ensure_ascii=False)
            self.assertNotIn("sk-doctor-secret-1234", text)
            self.assertTrue(checks["readiness"]["demo"])


class ModelTestEndpointTest(unittest.TestCase):
    def test_model_test_does_not_echo_the_key(self) -> None:
        from ariadne import cli

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"OK"}}]}'

        with tempfile.TemporaryDirectory() as tmp:
            config = Config.load(Path(tmp) / "config.json")
            runtime = Runtime(
                config=config, model_api_key="sk-super-secret-123456",
                model_base_url="https://api.example.com/v1", model="m",
                merge_model="m", python=sys.executable, bohr_bin="",
            )
            with mock.patch("ariadne.cli.urllib.request.urlopen",
                            return_value=FakeResponse()):
                result = cli._test_model(runtime)
        self.assertTrue(result["ok"])
        self.assertNotIn("sk-super-secret-123456", json.dumps(result))


class AuthBohrTest(unittest.TestCase):
    def test_access_key_login_saves_profile(self) -> None:
        from ariadne import cli

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = Config.load(path)
            runtime = Runtime(
                config=config, model_api_key="k", model_base_url="https://x/v1",
                model="m", merge_model="m", bohr_profile="",
                bohr_access_key="", bohr_host="https://open.bohrium.com",
                python=sys.executable, bohr_bin="/usr/local/bin/bohr",
            )
            completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
            with mock.patch("ariadne.cli.run", return_value=completed) as runner, \
                    mock.patch("ariadne.cli.bohr_status", return_value={
                        "available": True, "logged_in": True, "profile": "p",
                        "error": "",
                    }):
                status = cli._auth_bohr(
                    runtime, profile="p", access_key="ak-test",
                    device=False, no_input=True,
                )
            command = runner.call_args.args[0]
            self.assertIn("--ak", command)
            self.assertIn("ak-test", command)
            self.assertIn("--ak-only", command)
            self.assertIn("--profile", command)
            self.assertTrue(status["logged_in"])
            reloaded = Config.load(path)
            self.assertEqual(reloaded.get("bohr.access_key"), "ak-test")
            self.assertEqual(reloaded.get("bohr.profile"), "p")


if __name__ == "__main__":
    unittest.main()
