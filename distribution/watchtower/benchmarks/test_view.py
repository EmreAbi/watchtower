"""Hermetic Model Lab UI checks; no provider requests or live accounts."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Button, DataTable, Input, Select, Static, Tabs
from textual.command import CommandPalette
from view import ModelLab, RunReport, TestCase, report_text, score_summary, case_text, pack_option_label, review_text, test_case_text
from appearance import ThemePaletteProvider, theme_colors


class LabError(ValueError):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


PACK = dict(id="core", name="Core tasks", version="1", description="Fixed synthetic tasks.",
            case_count=10, quick_count=5, capability="text", hash="pack-hash")
PUBLISHED_PACK = dict(id="published-fixture",name="Published fixture subset",version=1,description="Synthetic test metadata.",
    case_count=40,quick_count=5,capability="reasoning",category="math-reasoning",hash="synthetic-published-hash",
    collection="published-subset",source_name="Published fixture",url="https://example.invalid/benchmark",
    revision="fixture-revision-1234567890",license="Synthetic test only",selection="Fixed source IDs from the test split.",
    source_count=1319,limitations="This fixture is not a live benchmark.",scoring_kind="accuracy")
PRIVATE_PACK = dict(id="private-fixture",name="Synthetic references",version=1,description="Private synthetic frozen examples.",
    case_count=4,quick_count=4,capability="typed-choice",category="classification",hash="synthetic-private-hash",
    collection="private",scoring_kind="reference-agreement",source_name="Synthetic private fixture")


def inspect_fixture(pack):
    return dict(deepcopy(pack),
        cases=[dict(id="case-one",prompt="Classify the synthetic pen. Return a JSON object.",
                    expected={"answer":{"category":"office"}},source_id="source-one",topic="classification",
                    reference_label="office",reference_quality="Human-reviewed fixture",evidence="Synthetic pen is stationery.")],
        unscored_cases=[dict(id="case-unscored",prompt="Classify the ambiguous synthetic record.",expected=None,
                    topic="classification",reference_quality="Unresolved",exclusion_reason="No agreed reference.")],
        quick_case_ids=["case-one"],output_schema={"answer":{"category":"string"}},
        instructions="Return JSON only. This is a synthetic local fixture.",
        scoring={"kind":pack.get("scoring_kind","case-credit"),"rule":"Equal credit per scored case.",
                 "explanation":"Compare responses with the frozen reference."},
        not_ready_scenarios=[{"name":"Taxonomy", "reason":"Reference review is not complete."}])


def saved_run(identifier="run-one", status="completed"):
    return dict(run_id=identifier, created_at="2026-01-01T12:00:00Z", pack_id="core", pack_name="Core tasks",
        pack_version="1", mode="quick", repeats=1, status=status, request_count=10, completed=10,
        targets=[dict(id="t1", profile_id="work", profile_label="Work", provider="codex", model="small")],
        rows=[dict(run_id=identifier, target="Work / small", target_id="t1", score=80., passed=4, total=5,
                   benchmark_score=80. if status=="completed" else None, quality_score=100.,
                   answered=4, wrong=0, execution_errors=1, cancelled=0, unrun=0, coverage=80.,
                   median_response_ms=125., p95_response_ms=250., provider="codex",model="small",profile_label="Work",
                   completed=5, errors=1, median_ms=125., p95_ms=250., input_tokens=None, output_tokens=40, cost_usd=None)],
        conditions={"mode":"quick", "repeats":1, "timeout_seconds":60, "adapter":"synthetic-adapter"},
        cases=[dict(id="case-one",prompt="Return the word synthetic.",expected={"answer":"synthetic"})],
        results=[dict(target_id="t1", case_id="case-one", repeat=1, status="ok", score=1., passed=True,
                      details="Exact match", text="Synthetic answer", elapsed_ms=100, provider="codex", model="small",
                      provider_version="test-version", adapter="synthetic-adapter")])


class FakeService:
    def __init__(self):
        self.calls = []
        self.pack_catalog = [deepcopy(PACK)]
        self.saved = []
        self.prepare_error = self.run_error = self.catalog_error = None
        self.profiles_error = self.inspect_error = None
        self.empty_profiles = False
        self.progress_payload = self.summary_override = None
        self.started = threading.Event()
        self.release = threading.Event()
        self.cleanup_entered = threading.Event()
        self.cleanup_release = threading.Event()
        self.block_run = self.wait_cleanup = False
        self.compare_result = dict(comparable=True, reason="Matching benchmark conditions.", rows=saved_run()["rows"],
            verdict={"kind":"single","text":"One target scored; add a matching run to compare models."})

    def profiles(self):
        self.calls.append(("profiles",))
        if self.profiles_error:
            raise self.profiles_error
        if self.empty_profiles:
            return dict(profiles=[],default_profile=None)
        return dict(profiles=[dict(id="work", label="Work", provider="codex"),
                              dict(id="open", label="Open", provider="opencode")], default_profile="work")

    def packs(self):
        self.calls.append(("packs",))
        return deepcopy(self.pack_catalog)

    def inspect_pack(self, pack_id):
        self.calls.append(("inspect",pack_id))
        if self.inspect_error:
            raise self.inspect_error
        return inspect_fixture(next(pack for pack in self.pack_catalog if pack["id"]==pack_id))

    def models(self, profile):
        self.calls.append(("models", profile))
        if self.catalog_error:
            raise self.catalog_error
        ids = ("small", "large") if profile == "work" else ("opencode/one", "opencode/two")
        return dict(models=[dict(id=m, label=m) for m in ids], notice="Synthetic model catalog.")

    def history(self):
        self.calls.append(("history",))
        return deepcopy(self.saved)

    def prepare(self, pack_id, mode, assignments, repeats, timeout_seconds, request_limit):
        self.calls.append(("prepare", pack_id, mode, deepcopy(assignments), repeats, timeout_seconds, request_limit))
        if self.prepare_error:
            raise self.prepare_error
        pack = next(p for p in self.pack_catalog if p["id"] == pack_id)
        count = pack["quick_count" if mode == "quick" else "case_count"] * repeats * len(assignments)
        if count > request_limit:
            raise LabError("Request count exceeds your configured limit.")
        return dict(token="opaque-plan-token", pack=deepcopy(pack), request_count=count,
            conditions=dict(mode=mode, repeats=repeats, timeout_seconds=timeout_seconds, request_limit=request_limit),
            targets=[dict(a, id=f"t{i+1}", profile_label=a["profile_id"],
                          provider="codex" if a["profile_id"] == "work" else "opencode") for i,a in enumerate(assignments)],
            notice="No requests have been sent yet.")

    def run(self, token, cancel_event, progress_callback):
        self.calls.append(("run", token))
        self.started.set()
        progress_callback(self.progress_payload or dict(completed=1, total=10, case_id="case-one", target="t1", status="completed"))
        if self.block_run:
            while not cancel_event.is_set() and not self.release.wait(.01):
                pass
        if self.wait_cleanup:
            self.cleanup_entered.set()
            self.cleanup_release.wait(3)
        if self.run_error:
            raise self.run_error
        summary = saved_run(status="cancelled" if cancel_event.is_set() else "completed")
        summary.update(self.summary_override or {})
        self.saved.insert(0, summary)
        return deepcopy(summary)

    def report(self, run_id):
        self.calls.append(("report", run_id))
        return deepcopy(next(r for r in self.saved if r["run_id"] == run_id))

    def compare(self, ids):
        self.calls.append(("compare", list(ids)))
        return deepcopy(self.compare_result)


class ModelLabTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def ready(self, app, pilot):
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def configured(self, app, pilot):
        await self.ready(app, pilot)
        app.query_one("#run-type", Select).value = "comparison"
        await self.ready(app, pilot)
        app.query_one("#model-0", Select).value = "small"
        app.query_one("#model-1", Select).value = "large"
        await pilot.pause()

    async def test_tests_browser_without_accounts_exposes_prompt_schema_and_reference(self):
        service=FakeService();service.empty_profiles=True;service.pack_catalog=[deepcopy(PRIVATE_PACK)]
        app=ModelLab(service)
        async with app.run_test(size=(81,25)) as pilot:
            await self.ready(app,pilot)
            app.query_one("#tabs",Tabs).active="tests";await self.ready(app,pilot)
            table=app.query_one("#tests-table",DataTable)
            self.assertEqual(table.row_count,2)
            self.assertEqual(str(table.get_row_at(1)[3]),"Not scored")
            self.assertEqual(str(table.get_row_at(1)[2]),"—")
            self.assertIn("Private tests",str(app.query_one("#tests-summary",Static).render()))
            self.assertFalse(app.query_one("#view-test",Button).disabled)
            self.assertLessEqual(app.query_one("#view-test").region.bottom,25)
            self.assertTrue(app.query_one("#review-setup",Button).disabled)
            await pilot.click("#view-test");await pilot.pause()
            self.assertIsInstance(app.screen,TestCase)
            prompt=str(app.screen.query_one("#test-evidence",Static).render())
            self.assertIn("Classify the synthetic pen",prompt)
            self.assertIn('"category": "string"',prompt)
            self.assertNotIn("Synthetic pen is stationery",prompt)
            app.screen.query_one("#test-tabs",Tabs).active="test-reference";await pilot.pause()
            reference=str(app.screen.query_one("#test-evidence",Static).render())
            self.assertIn("Human-reviewed fixture",reference)
            self.assertIn("Synthetic pen is stationery",reference)
            self.assertIn("not added to the model prompt",reference)
            app.screen.query_one("#test-tabs",Tabs).active="test-scoring";await pilot.pause()
            scoring=str(app.screen.query_one("#test-evidence",Static).render())
            self.assertIn("synthetic-private-hash",scoring)
            self.assertIn("Starting a run sends",scoring)
            self.assertIn("Taxonomy",scoring)
            await pilot.press("escape");await self.ready(app,pilot)
            self.assertEqual(app.page,"tests")
            self.assertEqual(app.test_case_id,"0")
            self.assertFalse(any(c[0] in {"models","prepare","run"} for c in service.calls))

    async def test_failed_account_lookup_does_not_hide_local_test_definitions(self):
        service=FakeService();service.profiles_error=RuntimeError("secret credential detail")
        app=ModelLab(service)
        async with app.run_test(size=(81,25)) as pilot:
            await self.ready(app,pilot)
            notice=str(app.query_one("#notice",Static).render())
            self.assertIn("Accounts are unavailable",notice)
            self.assertNotIn("secret",notice)
            app.query_one("#tabs",Tabs).active="tests";await self.ready(app,pilot)
            self.assertEqual(app.query_one("#tests-table",DataTable).row_count,2)
            self.assertEqual(app.inspected_pack["id"],"core")
            self.assertFalse(any(c[0] in {"models","prepare","run"} for c in service.calls))

    async def test_test_browser_discards_stale_pack_response(self):
        service=FakeService();service.empty_profiles=True
        service.pack_catalog=[deepcopy(PACK),deepcopy(PRIVATE_PACK)]
        entered=threading.Event();release=threading.Event()
        original=service.inspect_pack
        def delayed(pack_id):
            if pack_id=="core":
                entered.set();release.wait(3)
            return original(pack_id)
        service.inspect_pack=delayed;app=ModelLab(service)
        async with app.run_test(size=(81,25)) as pilot:
            await self.ready(app,pilot)
            app.query_one("#tabs",Tabs).active="tests";await pilot.pause()
            self.assertTrue(entered.is_set())
            app.query_one("#inspect-pack",Select).value=PRIVATE_PACK["id"]
            await pilot.pause();release.set();await self.ready(app,pilot)
            self.assertEqual(app.inspected_pack["id"],PRIVATE_PACK["id"])
            self.assertIn("Private tests",str(app.query_one("#tests-summary",Static).render()))
            self.assertFalse(any(c[0] in {"models","prepare","run"} for c in service.calls))

    async def test_test_browser_read_errors_are_safe_and_pack_can_be_reselected(self):
        service=FakeService();service.empty_profiles=True
        service.pack_catalog=[deepcopy(PACK),deepcopy(PRIVATE_PACK)]
        service.inspect_error=RuntimeError("private path and secret")
        app=ModelLab(service)
        async with app.run_test(size=(81,25)) as pilot:
            await self.ready(app,pilot)
            app.query_one("#tabs",Tabs).active="tests";await self.ready(app,pilot)
            self.assertIn("could not be read",str(app.query_one("#notice",Static).render()))
            self.assertNotIn("secret",str(app.query_one("#notice",Static).render()))
            self.assertTrue(app.query_one("#view-test",Button).disabled)
            service.inspect_error=None
            app.query_one("#inspect-pack",Select).value=PRIVATE_PACK["id"];await self.ready(app,pilot)
            self.assertEqual(app.query_one("#tests-table",DataTable).row_count,2)
            self.assertFalse(app.query_one("#view-test",Button).disabled)

    async def test_unscored_test_reference_and_theme_changes_are_local(self):
        service=FakeService();service.empty_profiles=True;service.pack_catalog=[deepcopy(PRIVATE_PACK)]
        app=ModelLab(service)
        async with app.run_test(size=(120,40)) as pilot:
            await self.ready(app,pilot)
            app.query_one("#tabs",Tabs).active="tests";await self.ready(app,pilot)
            app.select_test_case("1");app.open_test();await pilot.pause()
            self.assertIsInstance(app.screen,TestCase)
            calls=list(service.calls)
            app.screen.query_one("#test-tabs",Tabs).active="test-scoring";await pilot.pause()
            evidence=app.screen.query_one("#test-evidence",Static).render()
            self.assertIn("Not scored · Browse only",str(evidence))
            self.assertIn("Excluded from scored case counts",str(evidence))
            app.theme="textual-light";await pilot.pause()
            recolored=app.screen.query_one("#test-evidence",Static).render()
            self.assertNotEqual(recolored.spans,evidence.spans)
            self.assertEqual(str(recolored),str(evidence))
            app.screen.query_one("#test-tabs",Tabs).active="test-reference";await pilot.pause()
            self.assertIn("No agreed reference",str(app.screen.query_one("#test-evidence",Static).render()))
            self.assertEqual(service.calls,calls)
            await pilot.click("#close-test");await self.ready(app,pilot)
            self.assertEqual(app.test_case_id,"1")

    def test_private_pack_scores_use_reference_agreement_and_dynamic_quick_count(self):
        report=saved_run();report["pack_info"]=deepcopy(PRIVATE_PACK)
        report["scoring"]={"kind":"reference-agreement","rule":"Reference agreement = matches / attempts.",
                           "explanation":"References are not independently verified ground truth."}
        shown=score_summary(report).plain
        for label in ("Reference agreement 80/100","Agreement among responses","Matched 4/5","Not matched 0","4-case screening"):
            self.assertIn(label,shown)
        self.assertNotIn("Accuracy",shown)
        self.assertIn("Private tests",pack_option_label(PRIVATE_PACK))
        self.assertIn("Matched",case_text(report,report["results"][0]).plain)
        self.assertIn("Reference agreement",report_text(report).plain)
        fixture=inspect_fixture(PRIVATE_PACK)
        self.assertIn("Frozen pack hash",test_case_text(fixture,fixture["cases"][0],"scoring").plain)

    async def test_default_single_model_needs_only_one_selection_and_five_requests(self):
        service=FakeService();app=ModelLab(service)
        async with app.run_test(size=(81,25)) as pilot:
            await self.ready(app,pilot)
            self.assertEqual(app.query_one("#run-type",Select).value,"single")
            self.assertFalse(app.query_one("#target-1").display)
            self.assertEqual(app.query_one("#targets").styles.grid_size_columns,1)
            self.assertEqual([call for call in service.calls if call[0]=="models"],[("models","work")])
            self.assertIs(app.query_one("#model-1",Select).value,Select.NULL)
            app.query_one("#model-0",Select).value="small";await pilot.pause()
            budget=str(app.query_one("#request-budget",Static).render())
            self.assertIn("Required requests: 5",budget)
            self.assertIn("1 target",budget)
            await app.prepare_run().wait()
            self.assertEqual(service.calls[-1],("prepare","core","quick",
                [{"profile_id":"work","model":"small"}],1,60,10))
            self.assertIn("Requests: 5",str(app.query_one("#review",Static).render()))
            self.assertFalse(any(call[0]=="run" for call in service.calls))

    async def test_explicit_comparison_toggle_updates_budget_and_excludes_hidden_target(self):
        service=FakeService();service.pack_catalog=[deepcopy(PUBLISHED_PACK)];app=ModelLab(service)
        async with app.run_test(size=(81,25)) as pilot:
            await self.configured(app,pilot)
            self.assertTrue(app.query_one("#target-1").display)
            self.assertEqual(app.required_requests(),10)
            app.query_one("#mode",Select).value="full";await pilot.pause()
            self.assertEqual(app.required_requests(),80)
            app.query_one("#run-type",Select).value="single";await pilot.pause()
            self.assertFalse(app.query_one("#target-1").display)
            self.assertEqual(app.required_requests(),40)
            self.assertEqual(app.query_one("#request-limit",Input).value,"10")
            app.query_one("#request-limit",Input).value="40";await pilot.pause()
            await app.prepare_run().wait()
            self.assertEqual(service.calls[-1],("prepare",PUBLISHED_PACK["id"],"full",
                [{"profile_id":"work","model":"small"}],1,60,40))
            self.assertEqual(len(app.prepared["targets"]),1)
            self.assertFalse(any(call[0]=="run" for call in service.calls))

    async def test_open_and_changes_only_read_catalogs_and_history(self):
        service = FakeService()
        app = ModelLab(service)
        async with app.run_test(size=(81,25)) as pilot:
            await self.configured(app, pilot)
            app.query_one("#profile-1", Select).value = "open"
            await self.ready(app,pilot)
            self.assertEqual(app.query_one("#repeats", Input).value,"1")
            self.assertEqual(app.query_one("#timeout", Input).value,"60")
            self.assertEqual(app.query_one("#request-limit", Input).value,"10")
            self.assertTrue(all(call[0] in {"profiles","packs","history","models"} for call in service.calls))
            self.assertIs(app.query_one("#model-1", Select).value, Select.NULL)
            self.assertIn("No saved runs", str(app.query_one("#history-help",Static).render()))
            self.assertFalse(app.query_one("#start").display)

    async def test_review_records_exact_choices_before_separate_start(self):
        service = FakeService()
        app = ModelLab(service)
        async with app.run_test(size=(81,25)) as pilot:
            await self.configured(app,pilot)
            app.query_one("#profile-1",Select).value="open"
            await self.ready(app,pilot)
            app.query_one("#model-1",Select).value="opencode/two"
            await pilot.pause()
            await app.prepare_run().wait()
            self.assertEqual(service.calls[-1],("prepare","core","quick",
                [{"profile_id":"work","model":"small"},{"profile_id":"open","model":"opencode/two"}],1,60,10))
            self.assertFalse(any(call[0]=="run" for call in service.calls))
            self.assertEqual(app.stage,"review")
            shown=str(app.query_one("#review",Static).render())
            self.assertIn("Requests: 10",shown)
            self.assertIn("opencode/two",shown)
            self.assertNotIn("opaque-plan-token",shown)
            self.assertIn("paid credits",shown)
            await app.start_run().wait()
            await self.ready(app,pilot)
            self.assertEqual(app.stage,"finished")
            self.assertEqual(app.last_run,"run-one")
            self.assertEqual(sum(c[0]=="run" for c in service.calls),1)
            self.assertEqual(app.query_one("#history-table",DataTable).row_count,1)

    async def test_back_invalidates_prepared_plan(self):
        service=FakeService();app=ModelLab(service)
        async with app.run_test(size=(81,25)) as pilot:
            await self.configured(app,pilot)
            await app.prepare_run().wait()
            app.back()
            await app.start_run().wait()
            self.assertIsNone(app.prepared)
            self.assertEqual(app.stage,"form")
            self.assertFalse(any(c[0]=="run" for c in service.calls))

    async def test_invalid_limits_and_request_cap_never_run(self):
        service=FakeService();app=ModelLab(service)
        with patch.dict(sys.modules,{"service":SimpleNamespace(LabError=LabError)}):
            async with app.run_test(size=(81,25)) as pilot:
                await self.configured(app,pilot)
                for field,value in (("repeats","4"),("timeout","29"),("request-limit","121")):
                    control=app.query_one("#"+field,Input);before=control.value;control.value=value
                    await app.prepare_run().wait()
                    self.assertEqual(app.stage,"form")
                    control.value=before
                app.query_one("#request-limit",Input).value="9"
                await app.prepare_run().wait()
                self.assertIn("exceeds",str(app.query_one("#notice",Static).render()))
                self.assertFalse(any(c[0]=="run" for c in service.calls))

    async def test_cancel_and_close_wait_for_owned_cleanup_and_no_second_run(self):
        service=FakeService();service.block_run=service.wait_cleanup=True
        app=ModelLab(service)
        try:
            async with app.run_test(size=(81,25)) as pilot:
                await self.configured(app,pilot)
                await app.prepare_run().wait()
                worker=app.start_run()
                await pilot.pause()
                self.assertTrue(service.started.is_set())
                self.assertIn("case-one",str(app.query_one("#run-progress",Static).render()))
                await app.start_run().wait()
                with patch.object(app,"exit") as exit_app:
                    app.action_close()
                    self.assertTrue(app.cancel_event.is_set())
                    self.assertTrue(await asyncio.to_thread(service.cleanup_entered.wait,1))
                    self.assertTrue(app.running)
                    exit_app.assert_not_called()
                    service.cleanup_release.set()
                    await worker.wait()
                    exit_app.assert_called_once()
                self.assertEqual(sum(c[0]=="run" for c in service.calls),1)
                self.assertEqual(service.saved[0]["status"],"cancelled")
        finally:
            service.release.set();service.cleanup_release.set()

    async def test_cancel_without_close_keeps_partial_report_available(self):
        service=FakeService();service.block_run=True;app=ModelLab(service)
        try:
            async with app.run_test(size=(81,25)) as pilot:
                await self.configured(app,pilot);await app.prepare_run().wait()
                worker=app.start_run();await pilot.pause()
                app.cancel_run();await worker.wait();await self.ready(app,pilot)
                self.assertEqual(app.last_run,"run-one")
                self.assertIn("cancelled",str(app.query_one("#run-status",Static).render()))
                self.assertFalse(app.query_one("#report",Button).disabled)
        finally: service.release.set()

    async def test_failure_is_sanitized_and_previous_run_is_not_reused(self):
        service=FakeService();service.run_error=RuntimeError("private-launch-secret")
        app=ModelLab(service)
        async with app.run_test(size=(81,25)) as pilot:
            await self.configured(app,pilot);app.last_run="old-run"
            await app.prepare_run().wait();await app.start_run().wait();await self.ready(app,pilot)
            self.assertIsNone(app.last_run)
            self.assertTrue(app.query_one("#report",Button).disabled)
            self.assertNotIn("private-launch-secret",str(app.query_one("#notice",Static).render()))
            self.assertNotIn("private-launch-secret",str(app.query_one("#run-error",Static).render()))
            self.assertIn("No automatic retry",str(app.query_one("#run-status",Static).render()))

    async def test_progress_error_clears_after_success_but_final_failure_remains_reported(self):
        service=FakeService();service.block_run=True
        service.progress_payload=dict(completed=1,total=10,case_id="case-one",target="small",status="error",
            error_code="unsupported_protocol",error="This CLI does not support the benchmark protocol. Update the provider tool.")
        service.summary_override=dict(status="failed",completed=1,error_code="unsupported_protocol",
            error="The provider protocol is unsupported. Update the provider tool before retrying.")
        app=ModelLab(service)
        try:
            async with app.run_test(size=(81,25)) as pilot:
                await self.configured(app,pilot);await app.prepare_run().wait()
                worker=app.start_run();await pilot.pause()
                self.assertTrue(app.running)
                shown=str(app.query_one("#run-error",Static).render())
                self.assertIn("unsupported_protocol",shown)
                self.assertIn("Update the provider tool",shown)
                self.assertIn("small / case-one",shown)
                self.assertLessEqual(app.query_one("#run-error").region.bottom,app.query_one("#run-pane").region.bottom)
                app.accept_progress(dict(completed=2,total=10,target="large",case_id="case-two",status="ok"))
                self.assertEqual(str(app.query_one("#run-error",Static).render()),"")
                self.assertEqual(app.run_error_text,"")
                self.assertFalse(app.query_one("#notice").has_class("error"))
                self.assertIn("Latest request succeeded",str(app.query_one("#notice",Static).render()))
                service.release.set();await worker.wait();await self.ready(app,pilot)
                self.assertIn("Run failed",str(app.query_one("#run-status",Static).render()))
                self.assertIn("before retrying",str(app.query_one("#run-error",Static).render()))
                self.assertIn("Recorded 1 / 10",str(app.query_one("#run-result",Static).render()))
                self.assertFalse(app.query_one("#report",Button).disabled)
                self.assertIn("unsupported_protocol",report_text(service.saved[0]).plain)
        finally: service.release.set()

    async def test_latest_failure_replaces_previous_target_and_success_keeps_saved_errors(self):
        service=FakeService();service.block_run=True
        service.progress_payload=dict(completed=1,total=10,case_id="case-one",target="small",status="error",
            error_code="provider_error",error="First request failed.")
        failed=dict(target_id="t1",case_id="case-one",repeat=1,status="error",score=0,passed=False,
            model="small",text="",error="First request failed.",error_code="provider_error")
        service.summary_override=dict(results=[deepcopy(failed)])
        app=ModelLab(service)
        try:
            async with app.run_test(size=(81,25)) as pilot:
                await self.configured(app,pilot);await app.prepare_run().wait()
                worker=app.start_run();await pilot.pause()
                app.accept_progress(dict(completed=2,total=10,case_id="case-two",target="large",status="timeout",
                    error_code="timeout",error="Second request exceeded its timeout."))
                shown=str(app.query_one("#run-error",Static).render())
                self.assertIn("large / case-two",shown)
                self.assertIn("Second request",shown)
                self.assertNotIn("First request",shown)
                app.accept_progress(dict(completed=3,total=10,case_id="case-three",target="large",status="ok"))
                self.assertEqual(str(app.query_one("#run-error",Static).render()),"")
                self.assertIn("case-three · ok",str(app.query_one("#run-progress",Static).render()))
                service.release.set();await worker.wait();await self.ready(app,pilot)
                self.assertEqual(str(app.query_one("#run-error",Static).render()),"")
                self.assertIn("saved with request errors",str(app.query_one("#notice",Static).render()))
                self.assertEqual(service.saved[0]["results"],[failed])
                self.assertFalse(app.query_one("#report",Button).disabled)
        finally: service.release.set()

    async def test_cancelled_run_remains_cancelled_after_request_errors(self):
        service=FakeService();service.block_run=True
        service.progress_payload=dict(completed=1,total=10,case_id="case-one",target="small",status="error",
            error_code="provider_error",error="The selected provider could not answer this request.")
        app=ModelLab(service)
        try:
            async with app.run_test(size=(81,25)) as pilot:
                await self.configured(app,pilot);await app.prepare_run().wait()
                worker=app.start_run();await pilot.pause();app.cancel_run()
                await worker.wait();await self.ready(app,pilot)
                self.assertIn("Run cancelled",str(app.query_one("#run-status",Static).render()))
                self.assertIn("Run cancelled",str(app.query_one("#notice",Static).render()))
                self.assertIn("provider_error",str(app.query_one("#run-error",Static).render()))
                self.assertFalse(app.query_one("#report",Button).disabled)
        finally: service.release.set()

    async def test_summary_only_failure_reason_is_visible_and_new_run_clears_it(self):
        service=FakeService()
        service.summary_override=dict(status="failed",completed=0,error="Model identity changed. Check the selected model.")
        app=ModelLab(service)
        async with app.run_test(size=(81,25)) as pilot:
            await self.configured(app,pilot);await app.prepare_run().wait()
            await app.start_run().wait();await self.ready(app,pilot)
            self.assertIn("Model identity changed",str(app.query_one("#run-error",Static).render()))
            self.assertFalse(app.query_one("#report",Button).disabled)
            app.new_run();service.summary_override=None
            await app.prepare_run().wait();await app.start_run().wait();await self.ready(app,pilot)
            self.assertEqual(app.run_error_text,"")
            self.assertNotIn("identity changed",str(app.query_one("#run-error",Static).render()))

    async def test_history_report_shows_real_case_evidence_and_closes(self):
        service=FakeService();service.saved=[saved_run()];app=ModelLab(service)
        async with app.run_test(size=(81,25)) as pilot:
            await self.ready(app,pilot)
            app.query_one("#tabs",Tabs).active="history";await pilot.pause()
            worker=app.open_report();await pilot.pause()
            self.assertIsInstance(app.screen,RunReport)
            self.assertTrue(app.screen.query_one("#report-scroll").display)
            self.assertFalse(app.screen.query_one("#report-details").display)
            summary=str(app.screen.query_one("#scorecards",Static).render())
            for value in ("Benchmark 80/100","Answer quality 100/100","Passed 4/5","Not passed 0","Errors 1","5-case screening"):
                self.assertIn(value,summary)
            self.assertNotIn('"timeout_seconds"',summary)
            self.assertIn("Cost USD —",summary)
            self.assertIn("Tokens in/out —/40",summary)
            text=report_text(app.screen.report).plain
            for value in ("Synthetic answer","case-one","synthetic-adapter","test-version","Recorded conditions"):
                self.assertIn(value,text)
            await pilot.press("escape");await worker.wait()
            self.assertNotIsInstance(app.screen,RunReport)
            self.assertFalse(app.busy)

    async def test_compare_guard_blocks_mismatched_adapters_and_keeps_report_data(self):
        service=FakeService();service.saved=[saved_run(),saved_run("run-two")];app=ModelLab(service)
        async with app.run_test(size=(81,25)) as pilot:
            await self.ready(app,pilot)
            app.query_one("#tabs",Tabs).active="compare";await pilot.pause()
            await app.compare_runs().wait()
            self.assertEqual(service.calls[-1],("compare",["run-one"]))
            table=app.query_one("#comparison-table",DataTable)
            self.assertEqual(table.row_count,1)
            self.assertEqual(str(table.get_row_at(0)[1]),"80/100")
            self.assertEqual(str(table.get_row_at(0)[2]),"100/100")
            self.assertEqual(len(table.columns),6)
            self.assertIn("One target scored",str(app.query_one("#compare-verdict",Static).render()))
            app.query_one("#compare-b",Select).value="run-two";await pilot.pause()
            service.compare_result=dict(comparable=False,reason="Execution adapters differ; compare same protocol.",rows=saved_run()["rows"],
                verdict={"kind":"winner","text":"Never display this invalid verdict"})
            await app.compare_runs().wait()
            self.assertEqual(service.calls[-1],("compare",["run-one","run-two"]))
            self.assertEqual(table.row_count,0)
            self.assertIn("adapters differ",str(app.query_one("#compare-reason",Static).render()))
            self.assertNotIn("Never display",str(app.query_one("#compare-verdict",Static).render()))
            self.assertEqual(len(service.saved[0]["results"]),1)

    async def test_model_account_race_cannot_restore_old_selection(self):
        service=FakeService();original=service.models
        started,release=threading.Event(),threading.Event()
        def catalog(profile):
            if profile=="work": started.set();release.wait(3)
            return original(profile)
        service.models=catalog;app=ModelLab(service)
        try:
            async with app.run_test(size=(81,25)) as pilot:
                await pilot.pause()
                self.assertTrue(started.is_set())
                app.query_one("#profile-0",Select).value="open";await pilot.pause()
                app.query_one("#model-0",Select).value="opencode/two";await pilot.pause()
                release.set();await self.ready(app,pilot)
                self.assertEqual(app.query_one("#model-0",Select).value,"opencode/two")
                self.assertTrue(all(c[0] in {"profiles","packs","history","models"} for c in service.calls))
        finally: release.set()

    async def test_fixed_actions_and_scrolling_form_fit_81_by_25(self):
        app=ModelLab(FakeService())
        async with app.run_test(size=(81,25)) as pilot:
            await self.configured(app,pilot)
            for identifier in ("review-setup","close"):
                button=app.query_one("#"+identifier)
                self.assertTrue(button.display)
                self.assertLessEqual(button.region.right,81)
                self.assertLessEqual(button.region.bottom,25)
            limit=app.query_one("#request-limit",Input)
            limit.scroll_visible(animate=False);await pilot.pause()
            self.assertGreaterEqual(limit.region.y,4)
            self.assertLessEqual(limit.region.bottom,app.query_one("#run-pane").region.bottom)
            await app.prepare_run().wait();await pilot.pause()
            for identifier in ("back","start","close"):
                self.assertLessEqual(app.query_one("#"+identifier).region.right,81)
                self.assertLessEqual(app.query_one("#"+identifier).region.bottom,25)

    async def test_narrow_comparison_preserves_target_and_report_wraps(self):
        service=FakeService();service.saved=[saved_run()]
        target="Synthetic account / provider/long-model-version-name"
        service.compare_result["rows"][0]["target"]=target
        app=ModelLab(service)
        async with app.run_test(size=(81,25)) as pilot:
            await self.configured(app,pilot);await app.prepare_run().wait()
            app.query_one("#tabs",Tabs).active="compare";await pilot.pause()
            self.assertNotIn("Start explicitly",str(app.query_one("#notice",Static).render()))
            await app.compare_runs().wait();await pilot.pause()
            table=app.query_one("#comparison-table",DataTable)
            self.assertEqual(table.columns["target"].width,20)
            self.assertEqual(str(table.get_row_at(0)[0]),"small")
            self.assertFalse(str(table.get_row_at(0)[0]).startswith("Synthetic account"))
            self.assertLessEqual(sum(column.width+2 for column in table.columns.values()),table.size.width)
            self.assertIn(target,str(app.query_one("#compare-summary",Static).render()))
            for identifier in ("refresh","compare-runs","close"):
                self.assertLessEqual(app.query_one("#"+identifier).region.right,81)
                self.assertLessEqual(app.query_one("#"+identifier).region.bottom,25)
            app.query_one("#tabs",Tabs).active="history";await pilot.pause()
            report=app.open_report();await pilot.pause()
            scroll=app.screen.query_one("#report-scroll")
            self.assertLessEqual(scroll.virtual_size.width,scroll.size.width)
            self.assertLessEqual(app.screen.query_one("#close-report").region.bottom,25)
            await pilot.press("escape");await report.wait()

    async def test_cases_show_prompt_answer_and_readable_expected_without_switching_app_tab(self):
        service=FakeService();run=saved_run()
        run["results"].append(dict(target_id="t1",case_id="case-two",repeat=1,status="error",score=0.,passed=False,
            model="small",error="Provider tool rejected the protocol.",text=""))
        service.saved=[run];app=ModelLab(service)
        async with app.run_test(size=(81,25)) as pilot:
            await self.ready(app,pilot)
            app.query_one("#tabs",Tabs).active="history";await pilot.pause()
            worker=app.open_report();await pilot.pause()
            app.screen.query_one("#report-tabs",Tabs).active="result-cases";await pilot.pause()
            self.assertEqual(app.page,"history")
            self.assertTrue(app.screen.query_one("#case-pane").display)
            shown=str(app.screen.query_one("#case-evidence",Static).render())
            for expected in ("Return the word synthetic.","Synthetic answer","Answer: synthetic","Exact match"):
                self.assertIn(expected,shown)
            table=app.screen.query_one("#case-table",DataTable);table.move_cursor(row=1);await pilot.pause()
            shown=str(app.screen.query_one("#case-evidence",Static).render())
            self.assertIn("No scored answer",shown)
            self.assertNotIn("Credit 0",shown)
            self.assertIn("No response was returned",shown)
            self.assertIn("Provider tool rejected",shown)
            app.screen.query_one("#report-tabs",Tabs).active="result-details";await pilot.pause()
            self.assertTrue(app.screen.query_one("#report-details").display)
            self.assertFalse(app.screen.query_one("#report-scroll").display)
            await pilot.press("escape");await worker.wait()

    async def test_history_selection_shows_scores_and_unanswered_cancelled_run_is_not_scored(self):
        service=FakeService();cancelled=saved_run("cancelled-run",status="cancelled")
        row=cancelled["rows"][0]
        row.update(benchmark_score=None,quality_score=None,answered=0,wrong=0,passed=0,execution_errors=1,cancelled=1,unrun=3,
            median_response_ms=None,p95_response_ms=None,coverage=0.)
        cancelled["results"][0].update(status="error",passed=False,score=0,text="")
        service.saved=[saved_run(),cancelled];app=ModelLab(service)
        async with app.run_test(size=(81,25)) as pilot:
            await self.ready(app,pilot)
            app.query_one("#tabs",Tabs).active="history";await pilot.pause()
            self.assertIn("Benchmark 80/100",str(app.query_one("#history-scores",Static).render()))
            app.query_one("#history-table",DataTable).move_cursor(row=1);await pilot.pause()
            shown=str(app.query_one("#history-scores",Static).render())
            self.assertIn("Benchmark Not scored",shown)
            self.assertIn("Answer quality Not scored",shown)
            self.assertIn("Not run 3",shown)
            self.assertNotIn("0/100",shown)
            self.assertIn("Benchmark Not scored",report_text(cancelled).plain)
            self.assertIn("No scored answer",report_text(cancelled).plain)
            self.assertNotIn("Grade: FAIL",report_text(cancelled).plain)
            for identifier in ("report","close"):
                self.assertLessEqual(app.query_one("#"+identifier).region.bottom,25)

    async def test_finished_run_shows_scores_immediately_without_opening_report(self):
        app=ModelLab(FakeService())
        async with app.run_test(size=(81,25)) as pilot:
            await self.configured(app,pilot);await app.prepare_run().wait();await app.start_run().wait();await self.ready(app,pilot)
            shown=str(app.query_one("#run-result",Static).render())
            self.assertIn("Benchmark 80/100",shown)
            self.assertIn("Answer quality 100/100",shown)
            self.assertIn("Errors 1",shown)
            self.assertIn("View results",str(app.query_one("#report",Button).label))

    def test_wrong_answer_zero_is_a_score_but_no_answers_never_is(self):
        report=saved_run();row=report["rows"][0]
        row.update(benchmark_score=0.,quality_score=0.,answered=5,wrong=5,passed=0,execution_errors=0)
        self.assertIn("Benchmark 0/100",score_summary(report).plain)
        row.update(benchmark_score=0.,answered=0)
        self.assertIn("Benchmark Not scored",score_summary(report).plain)

    def test_score_colors_are_semantic_without_changing_numbers_or_score_thresholds(self):
        colors=dict(primary="#0077cc",success="#118833",warning="#b87700",error="#cc1122",muted="#777777")
        report=saved_run();report["rows"][0]["wrong"]=2
        rendered=score_summary(report, colors=colors)
        def span_style(text, fragment):
            offset=text.plain.index(fragment)
            return next(str(span.style) for span in text.spans if span.start <= offset < span.end)
        self.assertEqual(span_style(rendered,"Passed 4/5"), colors["success"])
        self.assertEqual(span_style(rendered,"Not passed 2"), colors["warning"])
        self.assertEqual(span_style(rendered,"Errors 1"), colors["error"])
        for score in (0,50,100):
            report["rows"][0]["benchmark_score"]=score
            text=score_summary(report, colors=colors)
            self.assertEqual(span_style(text,f"Benchmark {score}/100"),"bold "+colors["primary"])
        self.assertEqual(score_summary(report,colors=colors).plain,score_summary(report).plain)

    async def test_theme_switch_recolors_review_finished_and_open_report_without_service_calls(self):
        service=FakeService();app=ModelLab(service)
        async with app.run_test(size=(81,25)) as pilot:
            await self.configured(app,pilot);await app.prepare_run().wait()
            original=app.query_one("#review",Static).render()
            calls=list(service.calls)
            dark_background=app.screen.styles.background
            app.theme="textual-light";await pilot.pause()
            light=app.query_one("#review",Static).render()
            self.assertEqual(light.plain,original.plain)
            self.assertNotEqual(light.spans,original.spans)
            self.assertNotEqual(app.screen.styles.background,dark_background)
            self.assertEqual(service.calls,calls)
            await app.start_run().wait();await self.ready(app,pilot)
            base=app.screen
            finished=base.query_one("#run-result",Static).render()
            report_worker=app.open_report();await pilot.pause()
            self.assertIsInstance(app.screen,RunReport)
            modal=app.screen
            modal.query_one("#report-tabs",Tabs).active="result-cases";await pilot.pause()
            evidence=modal.query_one("#case-evidence",Static).render()
            case_result=modal.query_one("#case-table",DataTable).get_row_at(0)[2]
            calls=list(service.calls)
            app.theme="textual-dark";await pilot.pause()
            updated=base.query_one("#run-result",Static).render()
            self.assertEqual(updated.plain,finished.plain)
            self.assertNotEqual(updated.spans,finished.spans)
            changed=modal.query_one("#case-evidence",Static).render()
            self.assertEqual(changed.plain,evidence.plain)
            self.assertNotEqual(changed.spans,evidence.spans)
            self.assertNotEqual(modal.query_one("#case-table",DataTable).get_row_at(0)[2].style,case_result.style)
            self.assertEqual(modal.query_one("#report-tabs",Tabs).active,"result-cases")
            self.assertEqual(service.calls,calls)
            self.assertEqual(modal.query_one("#scorecards",Static).render().plain,score_summary(service.saved[0]).plain)
            modal.action_close();await report_worker.wait()

    async def test_theme_switch_recolors_history_and_comparison_and_palette_is_passive(self):
        service=FakeService();service.saved=[saved_run()];app=ModelLab(service)
        async with app.run_test(size=(120,40)) as pilot:
            await self.ready(app,pilot)
            app.query_one("#tabs",Tabs).active="compare";await pilot.pause()
            await app.compare_runs().wait()
            table=app.query_one("#comparison-table",DataTable)
            before=table.get_row_at(0)[1]
            history=app.query_one("#history-scores",Static).render()
            calls=list(service.calls)
            app.theme="textual-light";await pilot.pause()
            after=table.get_row_at(0)[1]
            self.assertEqual(str(before),str(after))
            self.assertNotEqual(before.style,after.style)
            self.assertEqual(app.query_one("#history-scores",Static).render().plain,history.plain)
            self.assertNotEqual(app.query_one("#history-scores",Static).render().spans,history.spans)
            self.assertEqual(service.calls,calls)
            await pilot.click("#themes");await pilot.pause()
            self.assertIsInstance(app.screen,CommandPalette)
            self.assertIn(ThemePaletteProvider,app.screen._supplied_providers)
            self.assertEqual(service.calls,calls)
            await pilot.press("escape")
    def test_case_json_code_is_readable_and_malformed_response_stays_verbatim(self):
        report=saved_run();item=report["results"][0]
        code="def solve(value):\n    return value + 1\n"
        item["text"]=json.dumps({"code":code})
        shown=case_text(report,item).plain
        self.assertIn("Code: def solve(value):\n    return value + 1",shown)
        self.assertNotIn('\\n    return',shown)
        self.assertIn(item["text"],report_text(report).plain)
        item["text"]='{"code": invalid-json'
        self.assertIn(item["text"],case_text(report,item).plain)

    async def test_published_pack_budget_changes_live_without_raising_user_limit(self):
        service=FakeService();service.pack_catalog.append(deepcopy(PUBLISHED_PACK));app=ModelLab(service)
        async with app.run_test(size=(81,25)) as pilot:
            await self.configured(app,pilot)
            app.query_one("#pack",Select).value=PUBLISHED_PACK["id"]
            app.query_one("#mode",Select).value="full";await pilot.pause()
            details=str(app.query_one("#pack-details",Static).render())
            for value in ("Published subset","math reasoning","Full 40","Published fixture","1319","not an official"):
                self.assertIn(value,details)
            self.assertIn("Required requests: 80",str(app.query_one("#request-budget",Static).render()))
            self.assertEqual(app.query_one("#request-limit",Input).value,"10")
            await app.prepare_run().wait()
            self.assertFalse(any(call[0]=="prepare" for call in service.calls))
            self.assertIn("exceeds",str(app.query_one("#notice",Static).render()))
            app.query_one("#request-limit",Input).value="100";await pilot.pause()
            await app.prepare_run().wait();await pilot.pause()
            self.assertEqual(app.prepared["request_count"],80)
            self.assertIn("Source URL: https://example.invalid/benchmark",str(app.query_one("#review",Static).render()))
            self.assertFalse(any(call[0]=="run" for call in service.calls))
            app.back();app.query_one("#repeats",Input).value="2";await pilot.pause()
            self.assertIn("Required requests: 160",str(app.query_one("#request-budget",Static).render()))
            self.assertIn("120-request cap",str(app.query_one("#request-budget",Static).render()))
            self.assertEqual(app.query_one("#request-limit",Input).value,"100")
            app.query_one("#mode",Select).value="quick";await pilot.pause()
            self.assertIn("Required requests: 20",str(app.query_one("#request-budget",Static).render()))
            self.assertFalse(app.query_one("#request-budget").has_class("error"))
            for identifier in ("review-setup","close"):
                self.assertLessEqual(app.query_one("#"+identifier).region.bottom,25)

    def test_published_accuracy_summary_scope_and_source_case_are_readable(self):
        report=saved_run();report["pack_info"]=deepcopy(PUBLISHED_PACK);report["pack_info"]["quick_count"]=7
        report["scoring"]={"kind":"accuracy","rule":"Accuracy = 100 x correct / planned.",
            "explanation":"Answers are correct or incorrect. Incomplete runs are not ranked."}
        report["cases"][0].update(source_id="original-case-42",topic="arithmetic")
        shown=score_summary(report).plain
        for value in ("Published subset","Published fixture","Accuracy 80/100","Correct 4/5","7-case screening", "not an official"):
            self.assertIn(value,shown)
        self.assertIn(report["scoring"]["explanation"],shown)
        self.assertNotIn("allow partial credit",shown)
        item=report["results"][0];item.update(passed=False,score=0)
        evidence=case_text(report,item).plain
        self.assertIn("Incorrect",evidence)
        self.assertNotIn("Wrong / partial",evidence)
        self.assertIn("Source case: original-case-42",evidence)
        self.assertIn("Topic: arithmetic",evidence)
        details=report_text(report).plain
        self.assertIn(PUBLISHED_PACK["revision"],details)
        self.assertIn(PUBLISHED_PACK["selection"],details)
        self.assertIn(PUBLISHED_PACK["license"],details)
        self.assertTrue(pack_option_label(PUBLISHED_PACK).startswith("Published subset |"))
        self.assertTrue(pack_option_label(PACK).startswith("Watchtower |"))

    def test_legacy_scoring_and_quick_count_remain_compatible(self):
        report=saved_run()
        self.assertIn("Benchmark 80/100",score_summary(report).plain)
        self.assertIn("allow partial credit",score_summary(report).plain)
        self.assertIn("5-case screening",score_summary(report).plain)
        report["conditions"]["case_ids"]=["a","b","c"]
        self.assertIn("3-case screening",score_summary(report).plain)

    async def test_real_service_contract_persists_only_synthetic_runner_evidence(self):
        from service import LabService
        class SyntheticRunner:
            def __init__(self): self.requests=[]
            def profiles(self): return {"profiles":[dict(id="test",label="Synthetic test account",provider="codex")],"default_profile":"test"}
            def models(self,profile): return {"models":[dict(id="small",label="Small"),dict(id="large",label="Large")]}
            def run(self,profile,model,prompt,timeout,cancel):
                self.requests.append((profile,model))
                return dict(status="ok",text="Synthetic test response",adapter="test-only",provider_version="fixture",
                            conditions={"execution_kind":"synthetic"},elapsed_ms=1,usage={})
        with tempfile.TemporaryDirectory() as directory:
            runner=SyntheticRunner();service=LabService(runner=runner,home=directory);app=ModelLab(service)
            async with app.run_test(size=(81,25)) as pilot:
                await self.ready(app,pilot)
                app.query_one("#model-0",Select).value="small";await pilot.pause()
                await app.prepare_run().wait()
                self.assertEqual(runner.requests,[])
                await app.start_run().wait();await self.ready(app,pilot)
                self.assertEqual(runner.requests,[("test","small")]*5)
                self.assertEqual(app.stage,"finished")
                self.assertEqual(app.query_one("#history-table",DataTable).row_count,1)
                saved=service.report(app.last_run)
                self.assertIn("Synthetic test response",report_text(saved).plain)
                self.assertTrue(service.compare([app.last_run])["comparable"])


class LauncherTest(unittest.TestCase):
    def test_private_runtime_fresh_child_and_clean_python_environment(self):
        directory=Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as root:
            python=Path(root)/"python.exe";python.write_bytes(b"synthetic marker")
            spec=importlib.util.spec_from_file_location("model_lab_launcher_test",directory/"launcher.py")
            launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
            runtime=SimpleNamespace(runtime_candidates=lambda env:[python],supports_textual=lambda path,env:True)
            fake_spec=SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module:None))
            with patch.object(launcher.importlib.util,"spec_from_file_location",return_value=fake_spec), \
                    patch.object(launcher.importlib.util,"module_from_spec",return_value=runtime), \
                    patch.dict(os.environ,{"PYTHONHOME":"unsafe","PYTHONPATH":"unsafe","VIRTUAL_ENV":"unsafe",
                        "NO_COLOR":"1","TEXTUAL_COLOR_SYSTEM":"standard","TERM":"dumb"}), \
                    patch.object(launcher.subprocess,"run",return_value=SimpleNamespace(returncode=0)) as run:
                self.assertEqual(launcher.main(),0)
                self.assertEqual(os.environ["NO_COLOR"],"1")
                self.assertEqual(os.environ["TERM"],"dumb")
            self.assertEqual(run.call_args.args[0],[str(python),"-B",str(directory/"view.py")])
            self.assertEqual(run.call_args.kwargs["cwd"],directory)
            for key in ("PYTHONHOME","PYTHONPATH","VIRTUAL_ENV","NO_COLOR"):
                self.assertNotIn(key,run.call_args.kwargs["env"])
            self.assertEqual(run.call_args.kwargs["env"]["TERM"],"xterm-256color")
            self.assertEqual(run.call_args.kwargs["env"]["COLORTERM"],"truecolor")
            self.assertEqual(run.call_args.kwargs["env"]["TEXTUAL_COLOR_SYSTEM"],"truecolor")
            with patch.dict(os.environ,run.call_args.kwargs["env"],clear=True):
                self.assertFalse(ModelLab(FakeService()).no_color)


if __name__=="__main__":
    unittest.main()
