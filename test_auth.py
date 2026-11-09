import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telethon import functions
from telethon.errors import FreshResetAuthorisationForbiddenError, SessionPasswordNeededError

import auth


OLD = SimpleNamespace(
    current=False, api_id=1, hash=11,
    device_model="PC 64bit", app_name="Old bot", date_created="2026-01-01",
)


class FakeClient:
    def __init__(self, me_id=42, old=(), session_value="test-secret"):
        self.me_id = me_id
        self.authorizations = list(old)
        self.session = SimpleNamespace(save=Mock(return_value=session_value))
        self.connected = False
        self.logged_out = False
        self.resets = []
        self.reset_error = None
        self.sign_in = AsyncMock()

    async def connect(self):
        self.connected = True

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return True

    async def log_out(self):
        self.logged_out = True

    async def get_me(self):
        return SimpleNamespace(id=self.me_id, bot=False)

    async def send_code_request(self, phone):
        return SimpleNamespace(phone_code_hash="test-code-hash")

    async def __call__(self, request):
        if isinstance(request, functions.account.GetAuthorizationsRequest):
            return SimpleNamespace(authorizations=list(self.authorizations))
        if isinstance(request, functions.account.ResetAuthorizationRequest):
            if self.reset_error:
                raise self.reset_error
            self.resets.append(request.hash)
            self.authorizations = [a for a in self.authorizations if a.hash != request.hash]
            return True
        raise AssertionError(f"Неожиданный запрос Telegram: {type(request)}")


class AuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.args = SimpleNamespace(project="project", service="worker", environment="production", expected_user_id=42)
        self.client = FakeClient(old=[OLD])
        self.factory = self._patch("auth.TelegramClient", return_value=self.client)
        self._patch("auth.StringSession", return_value=SimpleNamespace())
        self.railway = self._patch("auth.railway", side_effect=[
            '{"API_ID":"1","API_HASH":"test-api-hash"}', "",
            '{"USER_SESSION_STRING":"test-secret"}',
        ])
        self._patch("builtins.input", side_effect=["+79990000000", ""])
        self._patch("auth.getpass", return_value="12345")
        self.output = io.StringIO()
        self._patch("sys.stdout", new=self.output)

    def _patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    async def test_fresh_login_saves_then_revokes_without_printing_secrets(self):
        await auth.main(self.args)
        self.assertEqual(self.railway.call_args_list[1].args, (
            self.args, ["set", "USER_SESSION_STRING", "--stdin", "--skip-deploys"], "test-secret",
        ))
        self.assertEqual(self.client.resets, [11])
        self.assertEqual(self.client.authorizations, [])
        self.assertFalse(self.client.logged_out)
        self.assertFalse(self.client.connected)
        for secret in ("test-secret", "test-api-hash", "12345", "+79990000000"):
            self.assertNotIn(secret, self.output.getvalue())

    async def test_saved_session_is_reused_without_new_code(self):
        self.railway.side_effect = ['{"API_ID":"1","API_HASH":"test","USER_SESSION_STRING":"saved"}']
        with patch("builtins.input") as prompt:
            await auth.main(self.args)
        prompt.assert_not_called()
        self.assertEqual(self.railway.call_count, 1)
        self.assertEqual(self.client.resets, [11])
        self.assertFalse(self.client.logged_out)

    async def test_forbidden_reset_keeps_saved_session_and_explains_phone_step(self):
        self.client.reset_error = FreshResetAuthorisationForbiddenError(request=None)
        with self.assertRaisesRegex(RuntimeError, "не отозван"):
            await auth.main(self.args)
        self.assertEqual(self.railway.call_count, 3)
        self.assertIn("Настройки → Устройства", self.output.getvalue())
        self.assertFalse(self.client.logged_out)

    async def test_wrong_account_cannot_replace_scanner(self):
        self.client.me_id = 99
        with self.assertRaisesRegex(RuntimeError, "другой аккаунт"):
            await auth.main(self.args)
        self.assertEqual(self.railway.call_count, 1)
        self.assertTrue(self.client.logged_out)

    async def test_saved_session_of_another_account_is_rejected(self):
        self.railway.side_effect = ['{"API_ID":"1","API_HASH":"test","USER_SESSION_STRING":"saved"}']
        self.client.me_id = 99
        with self.assertRaisesRegex(RuntimeError, "другому аккаунту"):
            await auth.main(self.args)
        self.assertEqual(self.client.resets, [])

    async def test_uncertain_save_is_not_retried_or_revoked(self):
        self.railway.side_effect = ['{"API_ID":"1","API_HASH":"test"}', TimeoutError()]
        with self.assertRaises(TimeoutError):
            await auth.main(self.args)
        self.assertEqual(self.railway.call_count, 2)
        self.assertEqual(self.client.resets, [])
        self.assertFalse(self.client.logged_out)
        self.assertFalse(self.client.connected)

    async def test_two_factor_password_is_hidden(self):
        self.client.sign_in.side_effect = [SessionPasswordNeededError(request=None), None]
        with patch("auth.getpass", side_effect=["12345", "test-password"]):
            await auth.main(self.args)
        self.assertEqual(self.client.sign_in.call_args.kwargs, {"password": "test-password"})
        self.assertNotIn("test-password", self.output.getvalue())


class RailwayTests(unittest.TestCase):
    def test_secret_goes_to_stdin_not_arguments(self):
        args = SimpleNamespace(project="p", service="s", environment="production")
        with patch("auth.shutil.which", return_value="npx.cmd"), patch("auth.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            auth.railway(args, ["set", "USER_SESSION_STRING", "--stdin", "--skip-deploys"], "test-secret")
        self.assertNotIn("test-secret", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["input"], "test-secret")
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_failed_command_does_not_disclose_output(self):
        args = SimpleNamespace(project="p", service="s", environment="production")
        with patch("auth.shutil.which", return_value="npx.cmd"), patch("auth.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=1, stdout="secret-output", stderr="secret-error")
            with self.assertRaises(RuntimeError) as caught:
                auth.railway(args, ["list", "--json"])
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
