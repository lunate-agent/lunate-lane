"""The identity check runs against the real Apps. It prints no token."""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from scripts import identity  # noqa: E402


class IdentityTests(unittest.TestCase):
    @unittest.skipUnless(identity.CONFIG.exists(), "live test: no App config on this machine")
    def test_both_identities_mint_and_read(self) -> None:
        for name in ("worker", "reviewer"):
            with self.subTest(name=name):
                result = identity.check(name)
                self.assertTrue(result.startswith(f"{name}: OK"), result)
                self.assertNotIn("ghs_", result)

    def test_bad_key_path_is_reported_not_raised_raw(self) -> None:
        with self.assertRaises(identity.IdentityError):
            identity.app_jwt(1, pathlib.Path("/nonexistent/key.pem"))


if __name__ == "__main__":
    unittest.main()
