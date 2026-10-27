import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "indexed_agent_deploy", Path(__file__).resolve().parents[1] / "scripts" / "indexed_agent_deploy.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class AgentDeploymentTests(unittest.TestCase):
    def test_resume_requires_exact_connection_only_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "previous.json"
            previous = {
                "status": "failed-no-automatic-retry", "created": [deploy.CONNECTION],
                "agentName": deploy.AGENT, "project": deploy.PROJECT,
                "failure": "connection.properties.isSharedToAll: mismatch",
            }
            path.write_text(json.dumps(previous), encoding="utf-8")
            self.assertEqual(deploy.connection_resume(path), previous)
            previous["created"].append(deploy.AGENT)
            path.write_text(json.dumps(previous), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "connection-only"):
                deploy.connection_resume(path)

    def test_foundry_uses_ai_audience_not_graph_or_search(self):
        with patch.object(deploy, "rest", return_value={}) as rest:
            deploy.foundry("GET", "agents")
            self.assertEqual(rest.call_args.kwargs["resource"], "https://ai.azure.com")
            self.assertTrue(rest.call_args.args[1].endswith("/agents?api-version=v1"))

    def test_agent_collision_prevents_connection_and_bot_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "phase1.json"
            output = Path(directory) / "phase2.json"
            source.write_text("{}", encoding="utf-8")
            account = {"tenantId": deploy.TENANT, "user": {"name": deploy.LOGIN}}
            roles = [{"principalId": user, "roleDefinitionId": deploy.CONSUMER}
                     for user in deploy.USERS]
            with patch("sys.argv", ["deploy", str(source), str(output)]), \
                 patch.object(deploy, "verify", return_value={}), \
                 patch.object(deploy, "cli", side_effect=[account, roles]), \
                 patch.object(deploy, "foundry", return_value={
                     "has_more": False, "data": [{"name": deploy.AGENT}]
                 }) as foundry, patch.object(deploy, "rest") as rest:
                with self.assertRaisesRegex(ValueError, "Agent name collision"):
                    deploy.main()
                foundry.assert_called_once_with("GET", "agents")
                rest.assert_not_called()
                self.assertFalse(output.exists())

    def test_publication_generator_preserves_shared_scope(self):
        result = deploy.generated("buildIndexedPublication(v.botId)", {"botId": deploy.BOT_ID})
        self.assertEqual(result["publishScope"], "Shared")
        self.assertFalse(result["publishAsAutopilot"])
        self.assertEqual(result["botServiceArmId"], deploy.BOT_ID)


if __name__ == "__main__":
    unittest.main()
