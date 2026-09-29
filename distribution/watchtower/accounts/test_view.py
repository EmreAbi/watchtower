"""Headless Accounts UI tests: no real auth, network, provider or agent process."""

from __future__ import annotations

import copy
import asyncio
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from textual.widgets import Button, DataTable, Input, Select, Static

from view import AccountCenter, AddProfile, BrowserLoginModal, CodexTools, NewAgent, SwitchAccount, percentage, profile_details, quota_summary


def profile(name="work", provider="codex"):
    return {
        "id": name, "label": "Work" if name == "work" else "Personal", "provider": provider,
        "source": "managed", "home": "should-not-appear", "identity_verified": False,
        "status": {"state": "unknown", "email_masked": None, "plan": None,
                   "quota_windows": [], "observed_at": None, "error": None},
        "bindings": [],
    }


class FakeService:
    def __init__(self, profiles=None):
        self.profiles = profiles if profiles is not None else [profile(), profile("personal", "opencode")]
        self.default = None
        self.calls = []

    def list(self):
        self.calls.append(("list",))
        return copy.deepcopy({"version": 1, "profiles": self.profiles,
                              "defaults": {"default_profile": self.default}})

    def add(self, name, provider, label=None):
        self.calls.append(("add", name, provider, label))
        added = profile(name, provider)
        added["label"] = label
        self.profiles.append(added)
        return added

    def refresh(self, name):
        self.calls.append(("refresh", name))
        target = next(item for item in self.profiles if item["id"] == name)
        target["status"]["state"] = "connected"
        return target

    def set_default(self, name):
        self.calls.append(("set_default", name))
        self.default = name

    def models(self, name):
        self.calls.append(("models", name))
        return {"models": [{"id": "opencode/test-model", "label": "Zen Test Model"}]
                if name == "personal" else [{"id": "test-codex", "label": "Test Codex"}], "notice": ""}


def app_for(service):
    return AccountCenter(
        service, workspace_loader=Mock(return_value=[{"id": "w9", "name": "Sandbox", "cwd": "unused"}]),
        launcher=Mock(return_value={"pane_id": "w9:p7"}), connector=Mock(return_value=0),
    )


class FakeBrowserLogin:
    URL = "https://auth.openai.com/oauth/authorize?client_id=test&state=encoded%2Bstate&redirect_uri=http%3A%2F%2Flocalhost%3A1455"

    def __init__(self, *, block_start=False, start_error=None):
        self.start_calls = self.cancel_calls = self.close_calls = 0
        self.release = threading.Event()
        self.started = threading.Event()
        self.start_done = threading.Event()
        self.block_start = block_start
        self.start_error = start_error
        self.result = None

    def start(self):
        self.start_calls += 1
        self.started.set()
        if self.block_start:
            self.release.wait(3)
        self.start_done.set()
        if self.start_error:
            raise self.start_error
        return {"url": self.URL}

    def poll(self):
        return self.result

    def cancel(self):
        self.cancel_calls += 1
        self.release.set()

    def close(self):
        self.close_calls += 1


class FakeToolService:
    def __init__(self, *, block_update=False, block_check=False):
        self.calls = []
        self.block_update, self.block_check = block_update, block_check
        self.started = threading.Event()
        self.release = threading.Event()

    @staticmethod
    def result(state, *, can_update=False, installed="0.156.1", latest=None):
        return {"tool": "codex", "installed_version": installed, "latest_version": latest,
                "state": state, "message": "Shared Codex installation.", "can_update": can_update}

    def status(self, check_latest=False):
        self.calls.append(("status", check_latest))
        return self.result("unchecked")

    def check(self):
        self.calls.append(("check",))
        if self.block_check:
            self.started.set()
            self.release.wait(3)
        return self.result("update_available", can_update=True, latest="0.157.0")

    def update(self):
        self.calls.append(("update",))
        self.started.set()
        if self.block_update:
            self.release.wait(3)
        return self.result("updated", installed="0.157.0", latest="0.157.0")


class FakeSwitchService:
    def __init__(self):
        self.calls=[]
        self.candidates_error=self.prepare_error=self.apply_error=None
        self.block_apply=False
        self.started=threading.Event()
        self.release=threading.Event()
        self.agents=[dict(pane_id="w1:p1",handle="Lead",workspace="Sandbox",status="idle",eligible=True,reason="Ready to resume."),
            dict(pane_id="w1:p2",handle="Worker",workspace="Sandbox",status="working",eligible=False,reason="Agent is working; wait until it becomes idle."),
            dict(pane_id="w1:p3",handle="Unknown",workspace="Sandbox",status="unknown",eligible=False,reason="Live state could not be verified."),
            dict(pane_id="w2:p1",handle="Review",workspace="Research",status="done",eligible=True,reason="Ready to resume.")]

    def switch_candidates(self,source_id):
        self.calls.append(("candidates",source_id))
        if self.candidates_error: raise self.candidates_error
        return dict(source=profile(),targets=[profile(),profile("personal","codex"),profile("zen","opencode")],
            agents=copy.deepcopy(self.agents),notice="Idle Codex sessions can resume with another Codex account.")

    def prepare_switch(self,source,target,panes):
        self.calls.append(("prepare",source,target,list(panes)))
        if self.prepare_error: raise self.prepare_error
        return dict(token="single-use-plan",source=profile(source),target=profile(target),
            agents=[copy.deepcopy(a) for a in self.agents if a["pane_id"] in panes],
            notice="Selected sessions will restart under the target account. Eligibility is rechecked before switching.")

    def switch_accounts(self,token,progress_callback=None):
        self.calls.append(("switch",token))
        self.started.set()
        if progress_callback:
            progress_callback(dict(handle="Lead",pane_id="w1:p1",state="switching",message="Resuming session…"))
        if self.block_apply: self.release.wait(5)
        if self.apply_error: raise self.apply_error
        return dict(state="partial",message="1 switched;1 skipped after its state changed.",
            results=[dict(pane_id="w1:p1",handle="Lead",state="switched",message="Session resumed under Personal."),
                dict(pane_id="w2:p1",handle="Review",state="skipped",message="Agent became busy. Its account was not changed.")])


def login_app(login):
    app = app_for(FakeService())
    app.login_factory = Mock(return_value=login)
    app.browser_opener = Mock(return_value=True)
    app.clipboard_writer = Mock(return_value=True)
    app.execute_connect = Mock(return_value=0)
    return app


class FormattingTests(unittest.TestCase):
    def test_unknown_invalid_quota_is_not_zero(self):
        for value in (None, "0", True, -1, 101, float("nan"), float("inf")):
            self.assertEqual(percentage(value), "—")
        self.assertEqual(percentage(0), "0%")
        self.assertEqual(percentage(97.5), "97.5%")

    def test_details_do_not_claim_live_identity_or_leak_diagnostics(self):
        item = profile()
        item["label"] = "[red]Work[/red]\x1b\n"
        item["status"].update(state="credential_present", error="secret-provider-output")
        item["bindings"] = [{"handle": "worker", "workspace": "w9", "pane": "p2", "session_id": "private-session"}]
        text = profile_details(item, "work")
        self.assertIn("[red]Work[/red]", text.plain)
        self.assertNotIn("\x1b", text.plain)
        self.assertIn("not verified", text.plain)
        self.assertIn("validity has not been verified", text.plain)
        for secret in ("should-not-appear", "private-session", "secret-provider-output"):
            self.assertNotIn(secret, text.plain)

    def test_api_key_configuration_is_not_reported_as_verified_account(self):
        item = profile()
        item["status"].update(state="connected", auth_type="apiKey")
        text = profile_details(item, None).plain
        self.assertIn("API key configured", text)
        self.assertNotIn("Connected", text)
        self.assertIn("No quota data available", text)

    def test_new_provider_credentials_do_not_imply_verified_login_or_quota(self):
        for provider in ("gemini", "claude"):
            with self.subTest(provider=provider):
                item = profile(provider, provider)
                item["status"].update(state="credential_present")
                if provider == "gemini":
                    item["status"]["error"] = "provider_status_not_verified"
                text = profile_details(item, None).plain
                self.assertIn("Credentials present", text)
                self.assertIn("validity has not been verified", text)
                self.assertIn("usage limits are not reported", text)
                self.assertEqual(quota_summary(item["status"]), "—")
                self.assertNotIn("Connected", text)
                self.assertNotIn("0%", text)
                self.assertNotIn("100%", text)
                if provider == "gemini":
                    self.assertIn("Sign in with Google", text)
                    self.assertIn("/quit", text)
                    self.assertNotIn("Try Refresh or Connect", text)
                else:
                    self.assertIn("subscription sign-in", text)


class AccountCenterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Textual's layout tasks are intentionally CPU-heavy in headless mode.
        asyncio.get_running_loop().set_debug(False)

    async def test_open_and_cached_reload_do_not_query_provider(self):
        service = FakeService()
        app = app_for(service)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one("#profiles", DataTable).row_count, 2)
            await app.reload_snapshot().wait()
            self.assertTrue(all(call == ("list",) for call in service.calls))
            app.connector.assert_not_called()
            app.launcher.assert_not_called()

    async def test_empty_state_add_enabled_other_actions_disabled(self):
        app = app_for(FakeService([]))
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            self.assertFalse(app.query_one("#add", Button).disabled)
            self.assertFalse(app.query_one("#tools", Button).disabled)
            for name in ("connect", "refresh", "default", "new-agent"):
                self.assertTrue(app.query_one("#" + name, Button).disabled)
            self.assertIn("Keep your accounts", profile_details(app.selected_profile(), None).plain)

    async def test_refresh_selected_only_and_preserve_selection(self):
        service = FakeService()
        app = app_for(service)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(app.selected_id, "personal")
            await app.action_refresh_status().wait()
            await pilot.pause()
            self.assertIn(("refresh", "personal"), service.calls)
            self.assertNotIn(("refresh", "work"), service.calls)
            self.assertEqual(app.selected_id, "personal")

    async def test_add_profile_uses_selected_provider(self):
        service = FakeService()
        app = app_for(service)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#add")
            await pilot.pause()
            self.assertIsInstance(app.screen, AddProfile)
            app.screen.query_one("#profile-name", Input).value = "zen-work"
            app.screen.query_one("#profile-label", Input).value = "Zen Work"
            app.screen.query_one("#provider", Select).value = "opencode"
            await pilot.click("#save")
            await pilot.pause()
            await app.workers.wait_for_complete()
            self.assertIn(("add", "zen-work", "opencode", "Zen Work"), service.calls)
            self.assertEqual(app.selected_id, "zen-work")
            app.connector.assert_not_called()

    async def test_add_gemini_and_claude_with_native_login_guidance_on_narrow_screen(self):
        for provider, label, guidance in (("gemini", "Gemini", "Sign in with Google"),
                                           ("claude", "Claude", "subscription sign-in")):
            with self.subTest(provider=provider):
                service = FakeService([])
                app = app_for(service)
                async with app.run_test(size=(70, 36)) as pilot:
                    await pilot.pause()
                    await pilot.click("#add")
                    await pilot.pause()
                    app.screen.query_one("#provider", Select).value = provider
                    app.screen.query_one("#profile-name", Input).value = provider + "-work"
                    app.screen.query_one("#profile-label", Input).value = label + " Work"
                    await pilot.pause()
                    self.assertIn(guidance, str(app.screen.query_one("#provider-guide", Static).render()))
                    for widget_id in ("provider", "profile-name", "profile-label", "cancel", "save"):
                        region = app.screen.query_one("#" + widget_id).region
                        self.assertGreater(region.width, 0)
                        self.assertGreaterEqual(region.x, 0)
                        self.assertLessEqual(region.right, 70)
                        self.assertLessEqual(region.bottom, 36)
                    await pilot.click("#save")
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    self.assertIn(("add", provider + "-work", provider, label + " Work"), service.calls)
                    row = app.query_one("#profiles", DataTable).get_row(provider + "-work")
                    self.assertEqual(row[1].plain, label)
                    self.assertEqual(row[3].plain, "—")
                    self.assertTrue(app.query_one("#billing", Button).disabled)
                    app.connector.assert_not_called()

    async def test_cancel_add_does_not_mutate(self):
        service = FakeService()
        app = app_for(service)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#add")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertTrue(all(call == ("list",) for call in service.calls))
            self.assertFalse(app.busy)

    async def test_default_changes_preference_only(self):
        service = FakeService()
        app = app_for(service)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            await app.action_set_default().wait()
            self.assertEqual(service.default, "work")
            self.assertTrue(app.query_one("#default", Button).disabled)
            app.connector.assert_not_called()
            app.launcher.assert_not_called()

    async def test_connect_is_explicit_and_checks_result(self):
        service = FakeService()
        app = app_for(service)
        app.execute_connect = Mock(return_value=0)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            app.execute_connect.assert_not_called()
            await app.action_connect().wait()
            app.execute_connect.assert_called_once_with("personal")
            self.assertIn(("refresh", "personal"), service.calls)

    async def test_gemini_and_claude_connect_use_native_flow_and_refresh_selected_only(self):
        for provider in ("gemini", "claude"):
            with self.subTest(provider=provider):
                service = FakeService([profile(), profile(provider + "-work", provider)])
                app = app_for(service)
                app.execute_connect = Mock(return_value=0)
                app.login_factory = Mock(side_effect=AssertionError("Codex sign-in must not run"))
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    await pilot.press("down")
                    await pilot.pause()
                    app.execute_connect.assert_not_called()
                    await app.action_connect().wait()
                    app.execute_connect.assert_called_once_with(provider + "-work")
                    app.login_factory.assert_not_called()
                    self.assertIn(("refresh", provider + "-work"), service.calls)
                    self.assertNotIn(("refresh", "work"), service.calls)
                    self.assertEqual(app.query_one("#profiles", DataTable).get_row(provider + "-work")[3].plain, "—")
                    self.assertTrue(app.query_one("#billing", Button).disabled)

    async def test_cancelled_native_sign_in_does_not_refresh_or_claim_success(self):
        app = app_for(FakeService([profile("gemini-work", "gemini")]))
        app.execute_connect = Mock(return_value=1)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await app.action_connect().wait()
            self.assertNotIn(("refresh", "gemini-work"), app.service.calls)
            self.assertIn("Sign-in did not complete", str(app.query_one("#notice", Static).render()))
            self.assertFalse(app.busy)

    async def test_new_agent_passes_exact_account_handle_workspace(self):
        service = FakeService()
        app = app_for(service)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#new-agent")
            await pilot.pause()
            self.assertIsInstance(app.screen, NewAgent)
            app.screen.query_one("#agent-handle", Input).value = "demo-review"
            app.screen.query_one("#launch-profile", Select).value = "personal"
            await pilot.click("#launch")
            await pilot.pause()
        app.launcher.assert_called_once_with(service, "personal", "demo-review", "w9")
        app.connector.assert_not_called()

    async def test_new_agent_launches_selected_gemini_or_claude_profile(self):
        for provider in ("gemini", "claude"):
            with self.subTest(provider=provider):
                service = FakeService([profile(), profile(provider + "-work", provider)])
                app = app_for(service)
                async with app.run_test(size=(70, 36)) as pilot:
                    await pilot.pause()
                    await pilot.click("#new-agent")
                    await pilot.pause()
                    app.screen.query_one("#agent-handle", Input).value = "sandbox-" + provider
                    app.screen.query_one("#launch-profile", Select).value = provider + "-work"
                    await pilot.pause()
                    self.assertLessEqual(app.screen.query_one("#launch", Button).region.right, 70)
                    await pilot.click("#launch")
                    await pilot.pause()
                app.launcher.assert_called_once_with(service, provider + "-work", "sandbox-" + provider, "w9")
                app.connector.assert_not_called()

    async def test_new_agent_passes_explicit_opencode_model_and_resets_on_account_change(self):
        service = FakeService()
        app = app_for(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#new-agent")
            await pilot.pause()
            screen = app.screen
            screen.query_one("#agent-handle", Input).value = "model-demo"
            screen.query_one("#launch-model", Select).value = "test-codex"
            screen.query_one("#launch-profile", Select).value = "personal"
            await pilot.pause()
            self.assertEqual(screen.query_one("#launch-model", Select).value, "")
            screen.query_one("#launch-model", Select).value = "opencode/test-model"
            await pilot.click("#launch")
            await pilot.pause()
        app.launcher.assert_called_once_with(service, "personal", "model-demo", "w9", model="opencode/test-model")
        app.connector.assert_not_called()

    async def test_model_discovery_discards_stale_account_results(self):
        service = FakeService()
        release = threading.Event()
        started = threading.Event()
        original = service.models
        def catalog(profile_id):
            if profile_id == "work":
                started.set()
                release.wait(3)
            return original(profile_id)
        service.models = catalog
        app = app_for(service)
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.click("#new-agent")
                await pilot.pause()
                self.assertTrue(started.is_set())
                screen = app.screen
                screen.query_one("#launch-profile", Select).value = "personal"
                await pilot.pause()
                screen.query_one("#launch-model", Select).value = "opencode/test-model"
                release.set()
                await pilot.pause()
                self.assertEqual(screen.query_one("#launch-model", Select).value, "opencode/test-model")
                await pilot.press("escape")
            app.launcher.assert_not_called()
        finally:
            release.set()

    async def test_model_discovery_errors_are_sanitized_and_default_remains_available(self):
        service = FakeService()
        service.models = Mock(side_effect=RuntimeError("secret-provider-log"))
        app = app_for(service)
        async with app.run_test(size=(81, 25)) as pilot:
            await pilot.pause()
            await pilot.click("#new-agent")
            await pilot.pause()
            screen = app.screen
            notice = str(screen.query_one("#model-notice", Static).render())
            self.assertIn("Provider default", notice)
            self.assertNotIn("secret-provider-log", notice)
            self.assertEqual(screen.query_one("#launch-model", Select).value, "")
            await pilot.press("escape")
        app.launcher.assert_not_called()

    async def test_untrusted_exception_is_not_displayed(self):
        service = FakeService()
        service.refresh = Mock(side_effect=RuntimeError("private-key=never-print-this"))
        app = app_for(service)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            await app.action_refresh_status().wait()
            notice = str(app.query_one("#notice", Static).render())
            self.assertNotIn("never-print-this", notice)
            self.assertFalse(app.busy)

    async def test_billing_opens_only_fixed_provider_url(self):
        app = app_for(FakeService())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            with patch("view.webbrowser.open", return_value=True) as browser:
                app.action_billing()
                browser.assert_not_called()
                await pilot.press("down")
                await pilot.pause()
                app.action_billing()
                browser.assert_called_once_with("https://opencode.ai/auth")

    async def test_busy_actions_cannot_close_popup(self):
        app = app_for(FakeService())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            with patch.object(app, "exit") as exit_app:
                app.set_busy(True)
                app.action_quit()
                exit_app.assert_not_called()
                app.set_busy(False)
                app.action_quit()
                exit_app.assert_called_once()

    async def test_focused_workspace_is_selected_in_launch_form(self):
        app = app_for(FakeService())
        app.workspace_loader = Mock(return_value=[
            {"id": "w8", "name": "Account Stats"},
            {"id": "w9", "name": "Sandbox", "focused": True},
        ])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#new-agent")
            await pilot.pause()
            self.assertEqual(app.screen.query_one("#launch-workspace", Select).value, "w9")
            await pilot.press("escape")
            await pilot.pause()
            app.launcher.assert_not_called()

    async def test_narrow_screen_wraps_actions_without_hiding_them(self):
        app = app_for(FakeService())
        async with app.run_test(size=(70, 36)) as pilot:
            await pilot.pause()
            self.assertTrue(app.screen.has_class("compact"))
            for name in ("add", "connect", "refresh", "default", "new-agent", "billing"):
                button = app.query_one("#" + name, Button)
                self.assertGreater(button.region.width, 0)
                self.assertGreaterEqual(button.region.x, 0)
                self.assertLessEqual(button.region.right, 70)
                self.assertLessEqual(button.region.bottom, 36)
            self.assertLess(app.query_one("#profiles", DataTable).region.bottom,
                            app.query_one("#buttons").region.y)

    async def test_large_profile_list_remains_selectable_without_status_queries(self):
        service = FakeService([profile(f"profile-{index}") for index in range(150)])
        app = app_for(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#profiles", DataTable)
            self.assertEqual(table.row_count, 150)
            table.move_cursor(row=149, animate=False)
            await pilot.pause()
            self.assertEqual(app.selected_id, "profile-149")
            await app.reload_snapshot().wait()
            await pilot.pause()
            self.assertEqual(app.selected_id, "profile-149")
            self.assertTrue(all(call == ("list",) for call in service.calls))


class BrowserLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def test_browser_and_copy_are_explicit_and_preserve_complete_url(self):
        login = FakeBrowserLogin()
        app = login_app(login)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_connect()
            await pilot.pause()
            self.assertIsInstance(app.screen, BrowserLoginModal)
            await asyncio.shield(app.screen._start_task)
            await pilot.pause()
            self.assertEqual(login.start_calls, 1)
            app.browser_opener.assert_not_called()
            app.clipboard_writer.assert_not_called()
            app.execute_connect.assert_not_called()
            displayed = " ".join(str(node.render()) for node in app.screen.query(Static))
            self.assertNotIn("localhost", displayed)
            self.assertNotIn("encoded%2Bstate", displayed)
            await app.screen.external_action(False).wait()
            app.browser_opener.assert_called_once_with(FakeBrowserLogin.URL)
            await app.screen.external_action(True).wait()
            app.clipboard_writer.assert_called_once_with(FakeBrowserLogin.URL)
            await pilot.press("escape")
            await pilot.pause()
        self.assertEqual(login.cancel_calls, 1)
        self.assertEqual(login.close_calls, 1)

    async def test_success_refreshes_selected_account_after_close(self):
        login = FakeBrowserLogin()
        app = login_app(login)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_connect()
            await pilot.pause()
            modal = app.screen
            login.result = {"state": "success", "message": "Signed in."}
            await modal.poll_login().wait()
            self.assertEqual(str(modal.query_one("#login-cancel", Button).label), "Close")
            self.assertTrue(modal.query_one("#login-open", Button).disabled)
            await pilot.click("#login-cancel")
            await pilot.pause()
            await app.workers.wait_for_complete()
            self.assertIn(("refresh", "work"), app.service.calls)
        self.assertEqual(login.cancel_calls, 0)
        self.assertEqual(login.close_calls, 1)

    async def test_start_failure_does_not_display_exception_or_url(self):
        login = FakeBrowserLogin(start_error=RuntimeError("token=private " + FakeBrowserLogin.URL))
        app = login_app(login)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_connect()
            await pilot.pause()
            modal = app.screen
            await pilot.pause()
            displayed = " ".join(str(node.render()) for node in modal.query(Static))
            self.assertNotIn("token=private", displayed)
            self.assertNotIn("auth.openai.com", displayed)
            self.assertTrue(modal.query_one("#login-open", Button).disabled)
            await pilot.press("escape")
            await pilot.pause()

    async def test_safe_start_error_preserves_recovery_guidance(self):
        from browser_login import BrowserLoginError
        login = FakeBrowserLogin(start_error=BrowserLoginError("startup_failed"))
        app = login_app(login)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_connect()
            await pilot.pause()
            status = str(app.screen.query_one("#login-status", Static).render())
            self.assertIn("Close any other Codex sign-in attempt", status)
            await pilot.press("escape")
            await pilot.pause()

    async def test_cancel_during_start_disables_actions_and_closes_late_process(self):
        login = FakeBrowserLogin(block_start=True)
        app = login_app(login)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_connect()
            await pilot.pause()
            self.assertTrue(login.started.is_set())
            modal = app.screen
            modal.action_cancel()
            for action in ("login-open", "login-copy", "login-cancel"):
                self.assertTrue(modal.query_one("#" + action, Button).disabled)
            await pilot.pause()
            await app.workers.wait_for_complete()
            self.assertTrue(login.start_done.is_set())
            self.assertEqual(login.cancel_calls, 1)
            self.assertEqual(login.close_calls, 1)
            app.browser_opener.assert_not_called()
            self.assertNotIn(("refresh", "work"), app.service.calls)

    async def test_unmount_during_start_cleans_up_owned_login(self):
        login = FakeBrowserLogin(block_start=True)
        app = login_app(login)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_connect()
            await pilot.pause()
            self.assertTrue(login.started.is_set())
            await app.pop_screen()
            await pilot.pause()
            self.assertTrue(login.start_done.is_set())
            self.assertEqual(login.cancel_calls, 1)
            self.assertEqual(login.close_calls, 1)

    async def test_failed_browser_open_offers_copy_without_revealing_link(self):
        login = FakeBrowserLogin()
        app = login_app(login)
        app.browser_opener = Mock(return_value=False)
        async with app.run_test(size=(70, 36)) as pilot:
            await pilot.pause()
            app.action_connect()
            await pilot.pause()
            modal = app.screen
            await modal.external_action(False).wait()
            status = str(modal.query_one("#login-status", Static).render())
            self.assertIn("Copy link", status)
            self.assertNotIn("auth.openai.com", status)
            for action in ("login-open", "login-copy", "login-cancel"):
                self.assertLessEqual(modal.query_one("#" + action, Button).region.right, 70)
            await pilot.press("escape")
            await pilot.pause()


class CodexToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    def make_app(self, service):
        app = app_for(FakeService([]))
        app.tool_service_factory = Mock(return_value=service)
        return app

    async def test_tools_without_account_only_reads_installed_version(self):
        service = FakeToolService()
        app = self.make_app(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.tool_service_factory.assert_not_called()
            await pilot.click("#tools")
            await pilot.pause()
            self.assertIsInstance(app.screen, CodexTools)
            self.assertEqual(service.calls, [("status", False)])
            self.assertTrue(app.screen.query_one("#tool-update", Button).disabled)
            self.assertIn("0.156.1", str(app.screen.query_one("#tool-versions", Static).render()))
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.busy)

    async def test_check_does_not_install_and_explicit_update_keeps_dialog_open(self):
        service = FakeToolService()
        app = self.make_app(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#tools")
            await pilot.pause()
            await pilot.click("#tool-check")
            await pilot.pause()
            self.assertEqual(service.calls, [("status", False), ("check",)])
            self.assertFalse(app.screen.query_one("#tool-update", Button).disabled)
            await pilot.click("#tool-update")
            await pilot.pause()
            self.assertEqual(service.calls.count(("update",)), 1)
            self.assertIsInstance(app.screen, CodexTools)
            self.assertEqual(app.screen.tool_status["state"], "updated")
            self.assertTrue(app.screen.query_one("#tool-update", Button).disabled)
            self.assertFalse(app.screen.query_one("#tool-close", Button).disabled)
            app.launcher.assert_not_called()
            app.connector.assert_not_called()
            await pilot.press("escape")
            await pilot.pause()

    async def test_install_disables_duplicate_actions_and_modal_close(self):
        service = FakeToolService(block_update=True)
        app = self.make_app(service)
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.click("#tools")
                await pilot.pause()
                await pilot.click("#tool-check")
                await pilot.pause()
                modal = app.screen
                modal.start_action("update")
                modal.start_action("update")
                for button in ("tool-check", "tool-update", "tool-close"):
                    self.assertTrue(modal.query_one("#" + button, Button).disabled)
                await pilot.pause()
                self.assertTrue(service.started.is_set())
                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(app.screen, modal)
                self.assertEqual(service.calls.count(("update",)), 1)
                service.release.set()
                await pilot.pause()
                await pilot.click("#tool-close")
                await pilot.pause()
                self.assertFalse(app.busy)
        finally:
            service.release.set()

    async def test_check_can_be_closed_without_starting_install(self):
        service = FakeToolService(block_check=True)
        app = self.make_app(service)
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.click("#tools")
                await pilot.pause()
                await pilot.click("#tool-check")
                await pilot.pause()
                self.assertTrue(service.started.is_set())
                self.assertFalse(app.screen.query_one("#tool-close", Button).disabled)
                await pilot.press("escape")
                service.release.set()
                await pilot.pause()
                self.assertNotIsInstance(app.screen, CodexTools)
                self.assertNotIn(("update",), service.calls)
        finally:
            service.release.set()

    async def test_tools_controls_fit_narrow_screen(self):
        service = FakeToolService()
        app = self.make_app(service)
        async with app.run_test(size=(70, 36)) as pilot:
            await pilot.pause()
            self.assertLessEqual(app.query_one("#tools", Button).region.right, 70)
            await pilot.click("#tools")
            await pilot.pause()
            for button in ("tool-check", "tool-update", "tool-close"):
                self.assertLessEqual(app.screen.query_one("#" + button, Button).region.right, 70)
            await pilot.press("escape")
            await pilot.pause()

    async def test_unexpected_tool_error_is_sanitized(self):
        service = FakeToolService()
        service.check = Mock(side_effect=RuntimeError("private-output-token"))
        app = self.make_app(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#tools")
            await pilot.pause()
            await pilot.click("#tool-check")
            await pilot.pause()
            self.assertNotIn("private-output-token", str(app.screen.query_one("#tool-status", Static).render()))
            self.assertFalse(app.screen.query_one("#tool-close", Button).disabled)
            await pilot.press("escape")
            await pilot.pause()


class SwitchAccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch("switch_service.switch_supported", return_value=True))
        asyncio.get_running_loop().set_debug(False)

    def app(self,switch):
        app=app_for(FakeService([profile(),profile("personal","codex"),profile("zen","opencode")]))
        app.switch_service_factory=Mock(return_value=switch)
        return app

    async def open(self,app,pilot):
        await pilot.pause()
        await pilot.click("#switch-account")
        await pilot.pause()
        modal=app.screen
        self.assertIsInstance(modal,SwitchAccount)
        for worker in list(app.workers):
            if worker.group=="switch-candidates":
                await worker.wait()
        await pilot.pause()
        return modal

    async def test_macos_disables_existing_agent_switch_and_keeps_new_agent_available(self):
        service = FakeSwitchService()
        app = self.app(service)
        with patch("switch_service.switch_supported", return_value=False):
            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.pause()
                button = app.query_one("#switch-account", Button)
                self.assertTrue(button.disabled)
                self.assertIn("New agent", str(button.tooltip))
                self.assertFalse(app.query_one("#new-agent", Button).disabled)
                await pilot.click("#switch-account")
                await pilot.pause()
                app.switch_service_factory.assert_not_called()

    async def test_open_and_cancel_are_read_only_and_targets_stay_same_provider(self):
        service=FakeSwitchService();app=self.app(service)
        async with app.run_test(size=(81,25)) as pilot:
            modal=await self.open(app,pilot)
            self.assertEqual([p["id"] for p in modal.targets],["personal"])
            self.assertEqual(modal.selected,set())
            self.assertTrue(modal.query_one("#switch-review",Button).disabled)
            self.assertEqual(modal.query_one("#switch-agents",DataTable).row_count,4)
            self.assertIn("Agent is working",str(modal.query_one("#switch-agents",DataTable).get_row("w1:p2")[3]))
            await pilot.press("escape");await pilot.pause()
            self.assertIs(app.screen,app.screen_stack[0])
            self.assertEqual(service.calls,[("candidates","work")])
            self.assertFalse(any(call[0] in {"refresh","models","add","set_default"} for call in app.service.calls))
            app.launcher.assert_not_called();app.connector.assert_not_called()

    async def test_selected_agents_and_all_eligible_exclude_busy_unknown(self):
        service=FakeSwitchService();app=self.app(service)
        async with app.run_test(size=(81,25)) as pilot:
            modal=await self.open(app,pilot)
            modal.query_one("#switch-target",Select).value="personal";await pilot.pause()
            await pilot.click("#switch-all");await pilot.pause()
            self.assertEqual(modal.selected,{"w1:p1","w2:p1"})
            await pilot.click("#switch-clear");await pilot.pause()
            table=modal.query_one("#switch-agents",DataTable)
            table.move_cursor(row=1);table.focus();await pilot.press("enter");await pilot.pause()
            self.assertEqual(modal.selected,set())
            self.assertIn("working",str(modal.query_one("#switch-status",Static).render()))
            table.move_cursor(row=0);await pilot.press("enter");await pilot.pause()
            self.assertEqual(modal.selected,{"w1:p1"})
            await modal.prepare_switch().wait();await pilot.pause()
            self.assertEqual(service.calls[-1],("prepare","work","personal",["w1:p1"]))
            self.assertEqual(modal.stage,"review")
            self.assertIn("Lead",str(modal.query_one("#switch-summary",Static).render()))
            self.assertFalse(any(c[0]=="switch" for c in service.calls))
            await pilot.press("escape");await pilot.pause()

    async def test_review_is_explicit_apply_is_single_use_and_close_waits_for_result(self):
        service=FakeSwitchService();service.block_apply=True;app=self.app(service)
        async with app.run_test(size=(120,40)) as pilot:
            modal=await self.open(app,pilot)
            modal.query_one("#switch-target",Select).value="personal";await pilot.pause()
            await pilot.click("#switch-all");await pilot.pause()
            await modal.prepare_switch().wait();await pilot.pause()
            self.assertEqual(service.calls[-1],("prepare","work","personal",["w1:p1","w2:p1"]))
            worker=modal.apply_switch();await pilot.pause()
            self.assertTrue(service.started.is_set())
            self.assertEqual(modal.stage,"applying")
            await modal.apply_switch().wait()
            self.assertTrue(modal.query_one("#switch-close",Button).disabled)
            modal.action_close();await pilot.pause()
            self.assertIs(app.screen,modal)
            self.assertEqual([c for c in service.calls if c[0]=="switch"],[("switch","single-use-plan")])
            service.release.set();await worker.wait();await pilot.pause()
            self.assertEqual(modal.stage,"finished")
            summary=str(modal.query_one("#switch-summary",Static).render())
            self.assertIn("switched",summary);self.assertIn("skipped",summary)
            self.assertIn("became busy",summary)
            self.assertFalse(modal.query_one("#switch-close",Button).disabled)
            await pilot.click("#switch-close");await pilot.pause()
            self.assertFalse(app.busy)
            self.assertEqual(service.calls.count(("switch","single-use-plan")),1)

    async def test_untrusted_errors_are_hidden_and_failed_apply_does_not_retry(self):
        service=FakeSwitchService();service.prepare_error=RuntimeError("secret command environment");app=self.app(service)
        async with app.run_test(size=(81,25)) as pilot:
            modal=await self.open(app,pilot)
            modal.query_one("#switch-target",Select).value="personal";await pilot.pause()
            await pilot.click("#switch-all");await pilot.pause()
            await modal.prepare_switch().wait();await pilot.pause()
            self.assertEqual(modal.stage,"select")
            self.assertNotIn("secret",str(modal.query_one("#switch-status",Static).render()))
            self.assertFalse(any(c[0]=="switch" for c in service.calls))
            service.prepare_error=None;service.apply_error=RuntimeError("secret provider output")
            await modal.prepare_switch().wait();await modal.apply_switch().wait();await pilot.pause()
            self.assertEqual(modal.stage,"finished")
            self.assertIn("unconfirmed",str(modal.query_one("#switch-status",Static).render()))
            self.assertNotIn("secret",str(modal.query_one("#switch-summary",Static).render()))
            self.assertFalse(modal.query_one("#switch-apply").display)
            self.assertEqual(service.calls.count(("switch","single-use-plan")),1)
            await pilot.press("escape");await pilot.pause()

    async def test_switch_controls_remain_visible_on_narrow_screen(self):
        service=FakeSwitchService();app=self.app(service)
        async with app.run_test(size=(81,25)) as pilot:
            modal=await self.open(app,pilot)
            for name in ("switch-all","switch-clear","switch-review","switch-close"):
                button=modal.query_one("#"+name,Button)
                self.assertGreater(button.region.width,0)
                self.assertGreaterEqual(button.region.x,0)
                self.assertLessEqual(button.region.right,81)
                self.assertLessEqual(button.region.bottom,25)
            self.assertGreaterEqual(modal.query_one("#switch-agents").region.height,3)
            modal.query_one("#switch-target",Select).value="personal";await pilot.pause()
            await pilot.click("#switch-all");await pilot.pause()
            await modal.prepare_switch().wait();await pilot.pause()
            for name in ("switch-back","switch-apply","switch-close"):
                self.assertLessEqual(modal.query_one("#"+name).region.bottom,25)
            await pilot.press("escape");await pilot.pause()

    async def test_no_eligible_agents_show_reason_and_cannot_be_submitted(self):
        service=FakeSwitchService()
        for agent in service.agents:
            agent.update(eligible=False,reason="Session-preserving switching is not supported for this provider yet.")
        app=self.app(service)
        async with app.run_test(size=(81,25)) as pilot:
            modal=await self.open(app,pilot)
            self.assertTrue(modal.query_one("#switch-all",Button).disabled)
            self.assertTrue(modal.query_one("#switch-review",Button).disabled)
            self.assertIn("not supported",str(modal.query_one("#switch-agent-reason",Static).render()))
            await modal.prepare_switch().wait()
            self.assertEqual(service.calls,[("candidates","work")])
            await pilot.press("escape");await pilot.pause()


if __name__ == "__main__":
    unittest.main()
