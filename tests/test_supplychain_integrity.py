"""Offline dependency and workflow guardrails; no Home Assistant or device I/O."""

import importlib.metadata
import json
from pathlib import Path
import re
import unittest

import paho.mqtt.client as mqtt


ROOT = Path(__file__).resolve().parents[1]
# Update these together with the review record, not by blindly following moving tags.
REVIEWED_ACTIONS = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/setup-python": "ece7cb06caefa5fff74198d8649806c4678c61a1",
    "home-assistant/actions/hassfest": "86d4564712fa02543c808e3a693dd04f9d3f8361",
    "hacs/action": "1ebf01c408f29afcb6406bd431bc98fd8cbb15aa",
}


class SupplyChainIntegrityTests(unittest.TestCase):
    def workflow(self, name):
        return (ROOT / ".github" / "workflows" / name).read_text()

    def test_manifest_requires_reviewed_mqtt_api(self):
        manifest = json.loads((ROOT / "custom_components/qubo/manifest.json").read_text())
        self.assertEqual(manifest["requirements"], ["paho-mqtt>=2.1.0"])
        self.assertEqual(importlib.metadata.version("paho-mqtt"), "2.1.0")
        # Construct only; do not connect, start a loop, or access any device.
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self.assertEqual(client.callback_api_version, mqtt.CallbackAPIVersion.VERSION2)
        # The baseline hub explicitly selects VERSION1, also a Paho 2.x enum.
        with self.assertWarns(DeprecationWarning):
            legacy_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
        self.assertEqual(legacy_client.callback_api_version, mqtt.CallbackAPIVersion.VERSION1)

    def test_test_dependencies_are_exact_and_installed(self):
        requirements = (ROOT / "requirements-test.txt").read_text().splitlines()
        pins = [line for line in requirements if line and not line.startswith("#")]
        self.assertEqual(pins, ["aiohttp==3.14.5", "paho-mqtt==2.1.0", "voluptuous==0.16.0"])
        for pin in pins:
            name, version = pin.split("==")
            self.assertEqual(importlib.metadata.version(name), version)

    def test_all_action_references_are_reviewed_immutable_commits(self):
        seen = set()
        for path in (ROOT / ".github/workflows").glob("*.y*ml"):
            # This guards our simple uses: scalars, not a general YAML parser.
            refs = re.findall(r"^\s*(?:-\s*)?uses:\s*[\"']?([^\s\"']+)", path.read_text(), re.MULTILINE)
            self.assertTrue(refs, str(path))
            for ref in refs:
                action, sha = ref.rsplit("@", 1)
                self.assertRegex(sha, r"^[0-9a-f]{40}$")
                self.assertIn(action, REVIEWED_ACTIONS)
                self.assertEqual(sha, REVIEWED_ACTIONS[action])
                seen.add(action)
        self.assertEqual(seen, set(REVIEWED_ACTIONS))

    def test_checkout_does_not_persist_credentials(self):
        for name in ["hassfest.yaml", "security-regression.yaml"]:
            text = self.workflow(name)
            self.assertRegex(text, r"uses: actions/checkout@[^\n]+\n\s+with:\n\s+persist-credentials: false")
            self.assertIn("permissions:\n  contents: read\n", text)
            self.assertNotIn("pull_request_target", text)

    def test_hacs_keeps_empty_permissions_without_comment_writes(self):
        text = self.workflow("validate.yaml")
        self.assertIn("permissions: {}", text)
        self.assertIn('comment: "false"', text)

    def test_ci_runs_offline_unittest_with_real_dependencies(self):
        text = self.workflow("security-regression.yaml")
        self.assertIn("on:\n  push:\n  pull_request:\n", text)
        self.assertIn('python-version: "3.13"', text)
        self.assertIn("python -m pip install --index-url https://pypi.org/simple -r requirements-test.txt", text)
        self.assertIn("python -m unittest discover -s tests -v", text)


if __name__ == "__main__":
    unittest.main()
