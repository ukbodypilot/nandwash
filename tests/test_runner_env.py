import os
import unittest
from unittest import mock

from nandwash import cli


class RunnerEnv(unittest.TestCase):
    def test_background_runner_inherits_the_run_settings(self):
        env = {"PYTHONPATH": "/var/tmp/nw04", "PYTHONDONTWRITEBYTECODE": "1", "NANDWASH_MAINT_DIR": "/m",
               "SECRET_TOKEN": "x"}
        with mock.patch.dict(os.environ, env, clear=True):
            got = cli._runner_env()
        self.assertEqual(got, {k: v for k, v in env.items() if k != "SECRET_TOKEN"})


if __name__ == "__main__":
    unittest.main()
