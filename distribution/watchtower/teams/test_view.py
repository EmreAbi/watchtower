"""Headless team chooser tests; fake services never start processes or agents."""
from __future__ import annotations

import asyncio
import copy
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from textual.widgets import Button, Input, Select, Static
from view import TeamSetup


TEMPLATES = [
    dict(id="quick", name="Quick Task", summary="One assistant for a short, focused task.",
         completion_policy="The assistant reports its result directly.", roles=[dict(id="assistant", label="Assistant")]),
    dict(id="research", name="Research", summary="A researcher coordinates evidence and a reviewer checks it.",
         completion_policy="The researcher consolidates findings after local review.",
         roles=[dict(id="researcher", label="Researcher / controller"), dict(id="reviewer", label="Reviewer")]),
    dict(id="development", name="Development", summary="A lead, worker and reviewer share a project.",
         completion_policy="The lead reports completion after implementation and local review.",
         roles=[dict(id="lead", label="Lead / controller"), dict(id="worker", label="Worker"), dict(id="reviewer", label="Reviewer")]),
]


class TeamError(ValueError):
    pass


class FakeService:
    def __init__(self):
        self.calls = []
        self.accounts = [dict(id="work", label="Work", provider="codex"),
                         dict(id="personal", label="Personal", provider="codex"),
                         dict(id="open", label="Open account", provider="opencode"),
                         dict(id="other", label="Other", provider="claude")]
        self.model_error = None
        self.catalogs = {
            "work": dict(models=[dict(id="codex-small", label="Codex small"), dict(id="codex-large", label="Codex large")],
                         default_model="codex-small", notice=""),
            "personal": dict(models=[dict(id="codex-small", label="Codex small")], default_model="codex-small", notice=""),
            "open": dict(models=[dict(id="open-provider/model-a", label="Model A"),
                                  dict(id="open-provider/model-b", label="Model B")], default_model=None, notice="Saved connected models."),
        }
        self.prepare_error = None
        self.create_error = None
        self.result = dict(state="created", workspace_id="w-test", panes=[dict(role="assistant", pane_id="w-test:p1")],
                           message="Workspace created; the agent is ready for your first task.")
        self.block_create = False
        self.started, self.release = threading.Event(), threading.Event()

    def templates(self):
        self.calls.append(("templates",))
        return copy.deepcopy(TEMPLATES)

    def profiles(self):
        self.calls.append(("profiles",))
        return dict(profiles=copy.deepcopy(self.accounts), default_profile="personal")

    def models(self, profile_id):
        self.calls.append(("models", profile_id))
        if self.model_error:
            raise self.model_error
        return copy.deepcopy(self.catalogs[profile_id])

    def prepare(self, template_id, label, project_dir, assignments, models):
        self.calls.append(("prepare", template_id, label, project_dir, dict(assignments), dict(models)))
        if self.prepare_error:
            raise self.prepare_error
        template = next(t for t in TEMPLATES if t["id"] == template_id)
        return dict(token="private-opaque-plan-token", template=template["name"], label=label, project=project_dir,
                    summary=template["summary"], completion_policy=template["completion_policy"],
                    roles=[dict(role=r["id"], label=r["label"], profile_id=assignments[r["id"]],
                                profile_label=next(p["label"] for p in self.accounts if p["id"] == assignments[r["id"]]),
                                provider=next(p["provider"] for p in self.accounts if p["id"] == assignments[r["id"]]),
                                model=models[r["id"]]) for r in template["roles"]])

    def create(self, token):
        self.calls.append(("create", token))
        self.started.set()
        if self.block_create:
            self.release.wait(3)
        if self.create_error:
            raise self.create_error
        return copy.deepcopy(self.result)


class DelayedModels(FakeService):
    def __init__(self):
        super().__init__()
        self.model_started = threading.Event()
        self.model_release = threading.Event()

    def models(self, profile_id):
        result = super().models(profile_id)
        if profile_id == "work":
            self.model_started.set()
            self.model_release.wait(3)
        return result


class TeamSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def ready(self, app, pilot):
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def fill(self, app, pilot, template="quick"):
        app.query_one("#template", Select).value = template
        app.query_one("#workspace-label", Input).value = "Demo workspace"
        app.query_one("#project-dir", Input).value = "C:/Projects/demo"
        await self.ready(app, pilot)

    async def test_open_reads_only_templates_and_saved_profiles_and_never_infers_path(self):
        service = FakeService()
        app = TeamSetup(service)
        async with app.run_test(size=(81, 25)) as pilot:
            await self.ready(app, pilot)
            self.assertCountEqual(service.calls, [("templates",), ("profiles",), ("models", "personal")])
            self.assertEqual(app.current_template, "quick")
            self.assertEqual(app.query_one("#workspace-label", Input).value, "")
            self.assertEqual(app.query_one("#project-dir", Input).value, "")
            self.assertEqual(app.query_one("#role-account-0", Select).value, "personal")
            self.assertEqual([p["id"] for p in app.profiles], ["work", "personal", "open"])
            self.assertFalse(app.query_one("#create-workspace").display)
            self.assertIn("Codex and OpenCode", str(app.query_one("#provider-hint", Static).render()))
            self.assertIsNone(app.query_one("#role-model-0", Select).value)

    async def test_all_templates_and_role_assignments_change_without_starting_work(self):
        service = FakeService()
        app = TeamSetup(service)
        async with app.run_test(size=(81, 25)) as pilot:
            await self.ready(app, pilot)
            await self.fill(app, pilot, "research")
            self.assertEqual(app.current_template, "research")
            self.assertTrue(app.query_one("#role-field-1").display)
            self.assertFalse(app.query_one("#role-field-2").display)
            app.query_one("#role-account-1", Select).value = "work"
            await pilot.pause()
            app.query_one("#template", Select).value = "development"
            await pilot.pause()
            self.assertTrue(app.query_one("#role-field-2").display)
            self.assertEqual(app.query_one("#role-account-2", Select).value, "work")
            self.assertEqual(app.query_one("#role-account-0", Select).value, "personal")
            self.assertTrue(all(call[0] in {"templates", "profiles", "models"} for call in service.calls))

    async def test_review_prepares_exact_assignments_and_explicit_create_is_separate(self):
        service = FakeService()
        app = TeamSetup(service)
        async with app.run_test(size=(81, 25)) as pilot:
            await self.ready(app, pilot)
            await self.fill(app, pilot, "development")
            app.query_one("#role-account-1", Select).value = "work"
            await pilot.pause()
            await pilot.click("#review-setup")
            await self.ready(app, pilot)
            self.assertEqual(app.stage, "review")
            self.assertEqual(service.calls[-1], ("prepare", "development", "Demo workspace", "C:/Projects/demo",
                                                {"lead": "personal", "worker": "work", "reviewer": "personal"},
                                                {"lead": None, "worker": None, "reviewer": None}))
            self.assertFalse(any(call[0] == "create" for call in service.calls))
            shown = str(app.query_one("#prepared-plan", Static).render())
            self.assertIn("Demo workspace", shown)
            self.assertIn("Worker → Work", shown)
            self.assertIn("Completion policy", shown)
            self.assertIn("Model: Provider default", shown)
            self.assertNotIn("private-opaque-plan-token", shown)
            self.assertFalse(app.query_one("#setup-form").display)
            await pilot.click("#create-workspace")
            await self.ready(app, pilot)
            self.assertEqual(app.stage, "finished")
            self.assertEqual(service.calls[-1], ("create", "private-opaque-plan-token"))
            self.assertIn("w-test:p1", str(app.query_one("#outcome", Static).render()))
            self.assertFalse(app.query_one("#create-workspace").display)

    async def test_back_requires_new_plan_and_keeps_form_values(self):
        service = FakeService()
        app = TeamSetup(service)
        async with app.run_test(size=(81, 25)) as pilot:
            await self.ready(app, pilot)
            await self.fill(app, pilot)
            await app.prepare_setup().wait()
            await pilot.pause()
            await pilot.click("#back")
            await pilot.pause()
            self.assertEqual(app.stage, "form")
            self.assertIsNone(app.prepared)
            self.assertEqual(app.query_one("#workspace-label", Input).value, "Demo workspace")
            app.query_one("#workspace-label", Input).value = "Edited name"
            await app.prepare_setup().wait()
            self.assertEqual(service.calls[-1][2], "Edited name")
            self.assertFalse(any(call[0] == "create" for call in service.calls))

    async def test_partial_creation_shows_recorded_ids_without_retry_or_back(self):
        service = FakeService()
        service.result = dict(state="partial", workspace_id="w-partial", message="One agent could not be started.",
                              panes=[dict(role="lead", pane_id="w-partial:p1", state="started"),
                                     dict(role="worker", pane_id="w-partial:p2", state="failed")])
        app = TeamSetup(service)
        async with app.run_test(size=(81, 25)) as pilot:
            await self.ready(app, pilot)
            await self.fill(app, pilot)
            await app.prepare_setup().wait()
            await app.create_workspace().wait()
            await app.create_workspace().wait()
            app.back()
            self.assertEqual(app.stage, "finished")
            self.assertEqual(sum(call[0] == "create" for call in service.calls), 1)
            text = str(app.query_one("#outcome", Static).render())
            self.assertIn("w-partial:p1", text)
            self.assertIn("w-partial:p2", text)
            self.assertIn("Nothing is retried automatically", text)
            self.assertFalse(app.query_one("#back").display)
            self.assertTrue(app.query_one("#notice").has_class("error"))

    async def test_busy_creation_blocks_duplicate_start_back_and_close(self):
        service = FakeService()
        service.block_create = True
        app = TeamSetup(service)
        try:
            async with app.run_test(size=(81, 25)) as pilot:
                await self.ready(app, pilot)
                await self.fill(app, pilot)
                await app.prepare_setup().wait()
                first = app.create_workspace()
                self.assertTrue(await asyncio.to_thread(service.started.wait, 1))
                await app.create_workspace().wait()
                app.back()
                with patch.object(app, "exit") as exit_app:
                    app.action_close()
                    exit_app.assert_not_called()
                self.assertEqual(app.stage, "review")
                self.assertTrue(app.query_one("#close", Button).disabled)
                service.release.set()
                await first.wait()
                self.assertEqual(sum(call[0] == "create" for call in service.calls), 1)
        finally:
            service.release.set()

    async def test_safe_validation_error_and_generic_error_sanitization(self):
        service = FakeService()
        app = TeamSetup(service)
        async with app.run_test(size=(81, 25)) as pilot:
            await self.ready(app, pilot)
            await self.fill(app, pilot)
            with patch.dict(sys.modules, {"service": types.SimpleNamespace(TeamError=TeamError)}):
                service.prepare_error = TeamError("Choose an existing project folder.")
                await app.prepare_setup().wait()
                self.assertIn("existing project folder", str(app.query_one("#notice", Static).render()))
                service.prepare_error = RuntimeError("secret-diagnostic")
                await app.prepare_setup().wait()
                notice = str(app.query_one("#notice", Static).render())
                self.assertNotIn("secret-diagnostic", notice)
                self.assertIn("nothing was created", notice)
            self.assertEqual(app.stage, "form")
            self.assertIsNone(app.prepared)

    async def test_unconfirmed_create_is_final_and_generic_details_stay_hidden(self):
        service = FakeService()
        service.create_error = RuntimeError("credential-value")
        app = TeamSetup(service)
        async with app.run_test(size=(81, 25)) as pilot:
            await self.ready(app, pilot)
            await self.fill(app, pilot)
            await app.prepare_setup().wait()
            await app.create_workspace().wait()
            shown = str(app.query_one("#outcome", Static).render())
            self.assertIn("Creation status is unconfirmed", shown)
            self.assertNotIn("credential-value", shown)
            self.assertEqual(app.stage, "finished")
            await app.create_workspace().wait()
            self.assertEqual(sum(call[0] == "create" for call in service.calls), 1)

    async def test_no_supported_profiles_explains_accounts_and_never_offers_login(self):
        service = FakeService()
        service.accounts = [dict(id="other", label="Other", provider="claude")]
        app = TeamSetup(service)
        async with app.run_test(size=(81, 25)) as pilot:
            await self.ready(app, pilot)
            self.assertIn("No Codex or OpenCode accounts", str(app.query_one("#notice", Static).render()))
            self.assertTrue(app.query_one("#review-setup", Button).disabled)
            self.assertEqual(len(service.calls), 2)

    async def test_mixed_provider_models_are_explicit_in_plan_and_keep_full_opencode_id(self):
        service = FakeService()
        app = TeamSetup(service)
        async with app.run_test(size=(81, 25)) as pilot:
            await self.ready(app, pilot)
            await self.fill(app, pilot, "development")
            app.query_one("#role-account-1", Select).value = "open"
            await self.ready(app, pilot)
            app.query_one("#role-model-0", Select).value = "codex-small"
            app.query_one("#role-model-1", Select).value = "open-provider/model-b"
            await pilot.pause()
            await app.prepare_setup().wait()
            self.assertEqual(service.calls[-1][4], {"lead": "personal", "worker": "open", "reviewer": "personal"})
            self.assertEqual(service.calls[-1][5], {"lead": "codex-small", "worker": "open-provider/model-b", "reviewer": None})
            shown = str(app.query_one("#prepared-plan", Static).render())
            self.assertIn("Open account · opencode", shown)
            self.assertIn("Model: open-provider/model-b", shown)
            self.assertIn("Model: Provider default", shown)
            self.assertFalse(any(call[0] == "create" for call in service.calls))

    async def test_account_change_resets_model_and_ignores_late_previous_catalog(self):
        service = DelayedModels()
        app = TeamSetup(service)
        try:
            async with app.run_test(size=(81, 25)) as pilot:
                await self.ready(app, pilot)
                app.query_one("#role-model-0", Select).value = "codex-small"
                await pilot.pause()
                app.query_one("#role-account-0", Select).value = "work"
                await pilot.pause()
                self.assertTrue(await asyncio.to_thread(service.model_started.wait, 1))
                self.assertIsNone(app.models["assistant"])
                self.assertIsNone(app.query_one("#role-model-0", Select).value)
                self.assertTrue(app.query_one("#role-model-0", Select).disabled)
                self.assertTrue(app.query_one("#review-setup", Button).disabled)
                app.query_one("#role-account-0", Select).value = "open"
                await pilot.pause()
                for _ in range(50):
                    if not app._models_loading:
                        break
                    await asyncio.sleep(.01)
                self.assertFalse(app._models_loading)
                app.query_one("#role-model-0", Select).value = "open-provider/model-a"
                await pilot.pause()
                service.model_release.set()
                await self.ready(app, pilot)
                self.assertEqual(app.query_one("#role-model-0", Select).value, "open-provider/model-a")
                self.assertEqual(app.models["assistant"], "open-provider/model-a")
                self.assertFalse(app.query_one("#role-model-0", Select).disabled)
                self.assertIn("Saved connected models", str(app.query_one("#model-notice-0", Static).render()))
        finally:
            service.model_release.set()

    async def test_template_change_discards_late_models_for_previous_role(self):
        service = DelayedModels()
        app = TeamSetup(service)
        try:
            async with app.run_test(size=(81, 25)) as pilot:
                await self.ready(app, pilot)
                app.query_one("#role-account-0", Select).value = "work"
                await pilot.pause()
                self.assertTrue(await asyncio.to_thread(service.model_started.wait, 1))
                app.query_one("#template", Select).value = "research"
                await pilot.pause()
                service.model_release.set()
                await self.ready(app, pilot)
                self.assertEqual(app.current_template, "research")
                self.assertEqual(app.query_one("#role-account-0", Select).value, "personal")
                self.assertIsNone(app.query_one("#role-model-0", Select).value)
                with self.assertRaises(Exception):
                    app.query_one("#role-model-0", Select).value = "codex-large"
                self.assertFalse(app._models_loading)
        finally:
            service.model_release.set()

    async def test_template_change_keeps_shared_role_model_only_for_same_account(self):
        service = FakeService()
        app = TeamSetup(service)
        async with app.run_test(size=(81, 25)) as pilot:
            await self.ready(app, pilot)
            await self.fill(app, pilot, "research")
            app.query_one("#role-account-1", Select).value = "open"
            await self.ready(app, pilot)
            app.query_one("#role-model-1", Select).value = "open-provider/model-b"
            await pilot.pause()
            await self.fill(app, pilot, "development")
            self.assertEqual(app.query_one("#role-account-2", Select).value, "open")
            self.assertEqual(app.query_one("#role-model-2", Select).value, "open-provider/model-b")
            app.query_one("#role-account-2", Select).value = "personal"
            await self.ready(app, pilot)
            self.assertIsNone(app.query_one("#role-model-2", Select).value)

    async def test_model_errors_offer_provider_default_and_sanitize_untrusted_details(self):
        for error, message in ((TeamError("Model list unavailable; reconnect this account in Accounts."), "reconnect this account"),
                               (RuntimeError("private-provider-secret"), "Provider default or check Accounts")):
            with self.subTest(error=type(error).__name__):
                service = FakeService()
                service.model_error = error
                app = TeamSetup(service)
                with patch.dict(sys.modules, {"service": types.SimpleNamespace(TeamError=TeamError)}):
                    async with app.run_test(size=(81, 25)) as pilot:
                        await self.ready(app, pilot)
                        notice = app.query_one("#model-notice-0", Static)
                        self.assertIn(message, str(notice.render()))
                        self.assertNotIn("private-provider-secret", str(notice.render()))
                        self.assertTrue(notice.has_class("error"))
                        self.assertIsNone(app.query_one("#role-model-0", Select).value)
                        self.assertFalse(app.query_one("#role-model-0", Select).disabled)
                        await self.fill(app, pilot)
                        await app.prepare_setup().wait()
                        self.assertEqual(service.calls[-1][5], {"assistant": None})
                        self.assertFalse(any(call[0] == "create" for call in service.calls))

    async def test_empty_catalog_has_explanation_and_explicit_provider_default(self):
        service = FakeService()
        service.catalogs["personal"] = dict(models=[], default_model=None, notice="")
        app = TeamSetup(service)
        async with app.run_test(size=(81, 25)) as pilot:
            await self.ready(app, pilot)
            self.assertIn("No models listed", str(app.query_one("#model-notice-0", Static).render()))
            self.assertIsNone(app.query_one("#role-model-0", Select).value)
            self.assertFalse(app.query_one("#role-model-0", Select).disabled)
            self.assertFalse(app.query_one("#review-setup", Button).disabled)

    async def test_development_form_and_final_actions_fit_81_by_25(self):
        app = TeamSetup(FakeService())
        async with app.run_test(size=(81, 25)) as pilot:
            await self.ready(app, pilot)
            await self.fill(app, pilot, "development")
            for element in ("template", "workspace-label", "project-dir", "role-account-0", "role-account-1", "role-account-2",
                            "role-model-0", "role-model-1", "role-model-2", "review-setup", "close"):
                widget = app.query_one("#" + element)
                self.assertTrue(widget.display)
                self.assertGreater(widget.region.width, 0)
                self.assertLessEqual(widget.region.right, 81)
                self.assertLessEqual(widget.region.bottom, 25)
            await app.prepare_setup().wait()
            await pilot.pause()
            for element in ("create-workspace", "back", "close"):
                widget = app.query_one("#" + element)
                self.assertTrue(widget.display)
                self.assertLessEqual(widget.region.right, 81)
                self.assertLessEqual(widget.region.bottom, 25)


if __name__ == "__main__":
    unittest.main()
