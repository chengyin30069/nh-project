import tempfile
import unittest
from pathlib import Path

from server.config import config_environment, load_yaml_config


class ServerConfigTests(unittest.TestCase):
    def test_config_environment_flattens_values_and_resolves_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            env = config_environment(
                {
                    "auth": {"cookie": "a=b", "user_agent": "Browser"},
                    "server": {
                        "allowed_networks": ["127.0.0.1/32", "172.17.0.0/16"],
                        "trusted_proxies": ["127.0.0.1/32", "172.16.0.0/12"],
                    },
                    "paths": {"storage": "library", "download_script": "scripts/download.sh"},
                    "download": {"media_servers": [1, 3, 5], "parallel": 12},
                },
                config_path,
            )
            self.assertEqual(env["NH_COOKIE"], "a=b")
            self.assertEqual(env["NH_MEDIA_SERVER_LIST"], "1 3 5")
            self.assertEqual(env["NH_ALLOWED_NETWORKS"], "127.0.0.1/32,172.17.0.0/16")
            self.assertEqual(env["NH_TRUSTED_PROXIES"], "127.0.0.1/32,172.16.0.0/12")
            self.assertEqual(env["NH_FOLDER_PATH"], str(Path(tmp, "library").resolve()))

    def test_explicit_missing_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "not found"):
                load_yaml_config(Path(tmp) / "missing.yaml", required=True)


if __name__ == "__main__":
    unittest.main()
