#!/usr/bin/env python3
"""Model Lab UI. Only a reviewed, explicit Start may run provider requests."""
from __future__ import annotations

import asyncio
import json
import re
import threading

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.command import CommandPalette
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Label, Select, Static, Tab, Tabs

try:
    from .appearance import ThemePaletteProvider, theme_colors
except ImportError:
    from appearance import ThemePaletteProvider, theme_colors


def semantic_colors(colors=None):
    # Standalone render helpers remain usable without constructing an App.
    return colors or dict(primary="cyan", success="green", warning="yellow", error="red", muted="dim")


def result_style(item, colors):
    if item.get("status") == "ok":
        return colors["success"] if item.get("passed") else colors["warning"]
    return colors["error"] if item.get("status") in {"error", "timeout", "failed"} else colors["muted"]


def plain(value, limit=1000):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", str(value if value is not None else ""))[:limit]


def safe_error(error, fallback):
    try:
        from service import LabError
        if isinstance(error, LabError):
            return plain(getattr(error, "message", None) or str(error))
    except ImportError:
        pass
    return fallback


def metric(value, suffix=""):
    if value is None:
        return "—"
    return (f"{value:.1f}" if isinstance(value, float) else plain(value, 60)) + suffix


def score_label(value):
    return "Not scored" if value is None else f"{value:.1f}".rstrip("0").rstrip(".") + "/100"


def response_seconds(value):
    return "—" if value is None else f"{value / 1000:.2f}".rstrip("0").rstrip(".") + "s"


def benchmark_value(report, row):
    # An incomplete attempt, or a tool failure with no answer, is not a model score.
    if report.get("status") != "completed" or not row.get("answered"):
        return None
    return row.get("benchmark_score")


def collection_label(pack):
    return {"published-subset": "Published subset", "private": "Private tests"}.get(pack.get("collection"), "Watchtower")


def pack_option_label(pack):
    return collection_label(pack) + " | " + plain(pack.get("name") or pack.get("id"))


def selected_case_count(pack, mode):
    value = pack.get("quick_count" if mode == "quick" else "case_count")
    return value if type(value) is int and value > 0 else None


def pack_metadata(pack, mode="quick", detailed=False):
    """Present scope from the frozen catalog; never imply an official score."""
    if not pack:
        return ""
    category = plain(pack.get("category") or pack.get("capability")).replace("-", " ")
    header = [collection_label(pack)]
    if category:
        header.append(category)
    if pack.get("version") is not None:
        header.append("v" + plain(pack["version"]))
    for name, key in (("Quick", "quick_count"), ("Full", "case_count")):
        if pack.get(key) is not None:
            header.append(name + " " + plain(pack[key]))
    lines = [" | ".join(header)]
    if pack.get("source_name"):
        source = "Source: " + plain(pack["source_name"])
        if pack.get("source_count") is not None:
            source += " | " + plain(pack["source_count"]) + " source cases"
        if pack.get("revision"):
            source += " | rev " + plain(pack["revision"], 12 if not detailed else 150)
        lines.append(source)
    if pack.get("collection") == "published-subset":
        lines.append("Fixed subset, adapted protocol; not an official full-benchmark score.")
    elif pack.get("collection") == "private":
        lines.append("Private reference agreement; not independently verified ground truth.")
    if detailed:
        for key, label in (("url", "Source URL"), ("license", "License"), ("selection", "Selection"), ("limitations", "Limits")):
            if pack.get(key):
                lines.append(label + ": " + plain(pack[key], 1500))
    return "\n".join(lines)


def is_accuracy(report):
    return (report.get("pack_info", {}).get("scoring_kind") == "accuracy"
            or report.get("scoring", {}).get("kind") == "accuracy")


def is_reference_agreement(report):
    return (report.get("pack_info", {}).get("scoring_kind") == "reference-agreement"
            or report.get("scoring", {}).get("kind") == "reference-agreement")


def score_name(report):
    return "Reference agreement" if is_reference_agreement(report) else "Accuracy" if is_accuracy(report) else "Benchmark"


def score_summary(report, compact=False, colors=None):
    colors = semantic_colors(colors)
    text = Text()
    pack = report.get("pack_info", {})
    accuracy = is_accuracy(report)
    reference = is_reference_agreement(report)
    if not compact and pack:
        text.append(pack_metadata(pack, report.get("mode")) + "\n\n", style=colors["muted"])
    for index, row in enumerate(report.get("rows", [])):
        if index:
            text.append("\n")
        value = benchmark_value(report, row)
        quality = row.get("quality_score") if row.get("answered") else None
        label = row.get("model") or row.get("target") or "Model"
        account = row.get("profile_label") or ""
        text.append(plain(label) + (" · " + plain(account) if account else "") + "\n", style="bold " + colors["primary"])
        text.append(score_name(report) + " " + score_label(value), style="bold " + colors["primary"] if value is not None else colors["muted"])
        text.append((" · Agreement among responses " if reference else " · Answer quality ") + score_label(quality) + "\n")
        text.append(("Matched " if reference else "Correct " if accuracy else "Passed ") + metric(row.get("passed")) + "/" + metric(row.get("total")), style=colors["success"])
        text.append(" · Answered " + metric(row.get("answered")) + " · ")
        text.append(("Not matched " if reference else "Not passed ") + metric(row.get("wrong")), style=colors["warning"] if row.get("wrong") else colors["muted"])
        text.append(" · ")
        text.append("Errors " + metric(row.get("execution_errors")) + "\n", style=colors["error"] if row.get("execution_errors") else colors["muted"])
        text.append("Not run " + metric(row.get("unrun")) + " · Cancelled " + metric(row.get("cancelled"))
            + " · Coverage " + metric(row.get("coverage"), "%")
            + " · Median answer " + response_seconds(row.get("median_response_ms")) + "\n")
        if not compact:
            text.append("p95 answer " + response_seconds(row.get("p95_response_ms"))
                + " · Tokens in/out " + metric(row.get("input_tokens")) + "/" + metric(row.get("output_tokens"))
                + " · Cost USD " + metric(row.get("cost_usd")) + "\n", style=colors["muted"])
    if not report.get("rows"):
        text.append("Not scored · No model results are available.\n", style=colors["muted"])
    if not compact:
        text.append("\nHow scoring works\n", style="bold")
        rule = report.get("scoring", {}).get("rule") or ("Reference agreement = 100 x matched references / planned case attempts." if reference else "Accuracy = 100 x correct answers / planned case attempts." if accuracy
            else "Benchmark score = 100 × total case credit ÷ planned case attempts.")
        text.append(plain(rule, 1000) + "\n")
        explanation = report.get("scoring", {}).get("explanation")
        if explanation:
            text.append(plain(explanation, 2000) + "\n")
        else:
            text.append(("Agreement measures matches to the frozen private references, not independently verified ground truth. " if reference else "Each answer is correct or incorrect. " if accuracy else "Cases have equal weight and allow partial credit. ")
                + "Errors earn zero credit only in completed runs; incomplete/no-answer runs are Not scored.\n")
            text.append("Quality averages answered-case credit; coverage is answered/planned. Speed (including startup) and cost are separate.\n")
        text.append("— means unavailable, never zero.\n")
        if report.get("mode") == "quick":
            count = selected_case_count(pack, "quick")
            if count is None:
                cases = report.get("conditions", {}).get("case_ids")
                count = len(cases) if isinstance(cases, list) and cases else 5
            text.append("Quick is a " + str(count) + "-case screening, not a general model ranking.\n", style=colors["warning"])
    return text


def readable(value, depth=0):
    """Human-readable evidence; structured details are not a JSON obstacle."""
    if depth > 5:
        return plain(value, 1000)
    if isinstance(value, dict):
        return "\n".join(plain(key).replace("_", " ").capitalize() + ": " + readable(item, depth + 1)
                         for key, item in list(value.items())[:40])
    if isinstance(value, list):
        return "\n".join("• " + readable(item, depth + 1) for item in value[:30]) or "None"
    return plain(value, 16000)


def case_text(report, item, colors=None):
    colors = semantic_colors(colors)
    text = Text()
    text.append(plain(item.get("model") or item.get("target_id")) + " / " + plain(item.get("case_id"))
                + " · repeat " + plain(item.get("repeat")) + "\n", style="bold " + colors["primary"])
    ok = item.get("status") == "ok"
    accuracy = is_accuracy(report)
    reference = is_reference_agreement(report)
    grade = ("Matched" if reference else "Correct" if accuracy else "Passed") if ok and item.get("passed") else ("Not matched" if reference else "Incorrect" if accuracy else "Wrong / partial") if ok else "No scored answer"
    credit = item.get("score")
    text.append(grade + (" · Credit " + score_label(credit * 100) if ok and type(credit) in (int, float) else "")
                + " · " + plain(item.get("status")) + "\n", style=result_style(item, colors))
    case = next((c for c in report.get("cases", []) if c.get("id") == item.get("case_id")), {})
    for key, label in (("source_id", "Source case"), ("topic", "Topic")):
        if case.get(key) is not None:
            text.append(label + ": " + plain(case[key]) + "\n", style="dim")
    response = item.get("text") or "No response was returned."
    if isinstance(response, str) and len(response) <= 16000:
        try:
            parsed = json.loads(response)
            if isinstance(parsed, (dict, list)):
                response = parsed
        except (ValueError, RecursionError):
            pass  # Malformed replies remain readable as the original response.
    fields = [("Prompt", case.get("prompt") or item.get("prompt") or "Prompt was not recorded in this report."),
              ("Actual response", response),
              ("Expected answer / reference", case.get("expected") or item.get("expected") or "Reference was not recorded."),
              ("Why this grade", item.get("error") or item.get("details") or "No grading explanation was recorded.")]
    for label, value in fields:
        text.append("\n" + label + "\n", style="bold")
        rendered = readable(value)
        text.append(rendered[:16000] + "\n")
        if len(rendered) > 16000:
            text.append("[Truncated here; Details retains the recorded evidence.]\n", style="dim")
    return text


def review_text(plan, colors=None):
    colors = semantic_colors(colors)
    pack = plan.get("pack", {})
    result = Text()
    result.append(plain(pack.get("name") or pack.get("id")) + "\n", style="bold " + colors["primary"])
    result.append("Requests: " + plain(plan.get("request_count")) + "\n", style="bold")
    if plan.get("request_limit") is not None:
        result.append("Request limit: " + plain(plan["request_limit"]) + "\n")
    result.append(pack_metadata(pack, plan.get("conditions", {}).get("mode"), detailed=True) + "\n")
    for target in plan.get("targets", []):
        result.append(plain(target.get("profile_label") or target.get("profile_id")) + " · "
                      + plain(target.get("provider")) + " / " + plain(target.get("model") or "Provider default") + "\n")
    result.append("\nConditions\n", style="bold")
    for key, value in plan.get("conditions", {}).items():
        result.append(plain(key).replace("_", " ") + ": " + plain(value, 1500) + "\n")
    result.append("\n" + plain(plan.get("notice"), 2000) + "\n")
    result.append("Start sends these test requests through the selected accounts and may use paid credits.", style=colors["warning"])
    return result


def report_text(report, colors=None):
    colors = semantic_colors(colors)
    text = Text()
    text.append("Run " + plain(report.get("run_id")) + "\n", style="bold " + colors["primary"])
    text.append(plain(report.get("pack_name") or report.get("pack_id")) + " · "
                + plain(report.get("mode")) + " · " + plain(report.get("status")) + "\n")
    if report.get("pack_info"):
        text.append(pack_metadata(report["pack_info"], report.get("mode"), detailed=True) + "\n")
    if report.get("error"):
        code = plain(report.get("error_code"), 80)
        text.append("\n" + ((code + ": ") if code else "")
                    + plain(report["error"], 1500) + "\n", style=colors["error"])
    if report.get("conditions"):
        text.append("\nRecorded conditions\n", style="bold")
        text.append(plain(json.dumps(report["conditions"], ensure_ascii=False, indent=2), 12000) + "\n")
    for row in report.get("rows", []):
        text.append("\n" + plain(row.get("target")) + "\n", style="bold")
        text.append(score_name(report) + " " + score_label(benchmark_value(report, row)) + (" · Matched " if is_reference_agreement(report) else " · Passed ") + metric(row.get("passed"))
                    + "/" + metric(row.get("total")) + " · Errors " + metric(row.get("errors"))
                    + " · Median " + metric(row.get("median_ms"), " ms")
                    + " · p95 " + metric(row.get("p95_ms"), " ms") + "\n")
        text.append("Tokens in/out: " + metric(row.get("input_tokens")) + " / " + metric(row.get("output_tokens"))
                    + " · Cost USD: " + metric(row.get("cost_usd")) + "\n")
    text.append("\nCase evidence\n", style="bold")
    for item in report.get("results", [])[:360]:
        text.append("\n" + plain(item.get("target_id")) + " / " + plain(item.get("case_id"))
                    + " · repeat " + plain(item.get("repeat")) + " · " + plain(item.get("status")) + "\n", style="bold")
        text.append(plain(item.get("provider")) + " / " + plain(item.get("model"))
                    + " · " + metric(item.get("elapsed_ms"), " ms") + "\n")
        text.append("Adapter: " + plain(item.get("adapter")) + " · Provider version: "
                    + plain(item.get("provider_version")) + "\n")
        score = item.get("score")
        ok = item.get("status") == "ok"
        text.append("Grade: " + (("Matched" if is_reference_agreement(report) else "PASS") if ok and item.get("passed") is True else ("Not matched" if is_reference_agreement(report) else "Incorrect" if is_accuracy(report) else "Wrong / partial") if ok else "No scored answer") + " · "
                    + score_label(score * 100 if ok and type(score) in (int, float) else None) + "\n", style=result_style(item, colors))
        for key in ("execution_conditions", "notices", "details", "error", "text"):
            value = item.get(key)
            if value:
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False, indent=2)
                text.append(key.title() + ": " + plain(value, 6000) + "\n")
                if len(str(value)) > 6000:
                    text.append("[Truncated in this view; the saved run retains the full response.]\n", style="dim")
    if not report.get("results"):
        text.append("No case outputs recorded yet.\n")
    return text


class RunReport(ModalScreen):
    BINDINGS = [("escape", "close", "Close results")]

    def __init__(self, report):
        super().__init__()
        self.report = report
        self.case_index = None

    def compose(self):
        colors = theme_colors(self.app)
        with Vertical(id="report-dialog"):
            yield Static(plain(self.report.get("pack_name") or self.report.get("pack_id")) + " · "
                + plain(self.report.get("mode")) + " · " + plain(self.report.get("status")), id="report-title", markup=False)
            yield Tabs(Tab("Summary", id="result-summary"), Tab("Cases", id="result-cases"),
                       Tab("Details", id="result-details"), id="report-tabs")
            with VerticalScroll(id="report-scroll"):
                if self.report.get("error"):
                    yield Static(plain(self.report["error"], 1500), classes="report-error", markup=False)
                yield Static(score_summary(self.report, colors=colors), id="scorecards", markup=False)
            with Vertical(id="case-pane"):
                yield DataTable(id="case-table", cursor_type="row", zebra_stripes=True, show_row_labels=False)
                with VerticalScroll(id="case-scroll"):
                    yield Static("No recorded cases yet.", id="case-evidence", markup=False)
            with VerticalScroll(id="report-details"):
                yield Static(report_text(self.report, colors=colors), id="report-evidence", markup=False)
            yield Button("Close results", id="close-report")

    def on_mount(self):
        self.app.theme_changed_signal.subscribe(self, self.refresh_theme)
        table = self.query_one("#case-table", DataTable)
        for label, width in (("Model", 16), ("Case", 20), ("Result", 12), ("Credit", 9)):
            table.add_column(label, width=width)
        for index, item in enumerate(self.report.get("results", [])):
            table.add_row(*self.case_cells(item), key=str(index))
        self.show_tab("result-summary")

    def case_cells(self, item):
        colors = theme_colors(self.app)
        ok = item.get("status") == "ok"
        result = ("Matched" if is_reference_agreement(self.report) else "Pass") if ok and item.get("passed") else ("Not matched" if is_reference_agreement(self.report) else "Incorrect" if is_accuracy(self.report) else "Wrong/partial") if ok else plain(item.get("status"))
        score = item.get("score")
        return (Text(plain(item.get("model") or item.get("target_id"))), Text(plain(item.get("case_id"))),
                Text(result, style=result_style(item, colors)),
                Text(score_label(score * 100) if ok and type(score) in (int, float) else "—", style=colors["primary"] if ok else colors["muted"]))

    def refresh_theme(self, _theme=None):
        if not self.is_mounted:
            return
        colors = theme_colors(self.app)
        self.query_one("#scorecards", Static).update(score_summary(self.report, colors=colors))
        self.query_one("#report-evidence", Static).update(report_text(self.report, colors=colors))
        table = self.query_one("#case-table", DataTable)
        for row, item in enumerate(self.report.get("results", [])):
            for column, value in enumerate(self.case_cells(item)):
                table.update_cell_at(Coordinate(row, column), value)
        if self.case_index is not None:
            self.query_one("#case-evidence", Static).update(case_text(self.report, self.report["results"][self.case_index], colors))

    def show_tab(self, tab):
        self.query_one("#report-scroll").display = tab == "result-summary"
        self.query_one("#case-pane").display = tab == "result-cases"
        self.query_one("#report-details").display = tab == "result-details"

    def on_tabs_tab_activated(self, event):
        if event.tabs.id == "report-tabs":
            event.stop()
            self.show_tab(event.tab.id)

    def on_data_table_row_highlighted(self, event):
        if event.data_table.id != "case-table":
            return
        event.stop()
        if event.row_key not in event.data_table.rows:
            return
        self.case_index = int(event.row_key.value)
        item = self.report["results"][self.case_index]
        self.query_one("#case-evidence", Static).update(case_text(self.report, item, theme_colors(self.app)))
        self.query_one("#case-scroll", VerticalScroll).scroll_home(animate=False)

    def action_close(self):
        self.dismiss()

    def on_button_pressed(self, event):
        if event.button.id == "close-report":
            self.dismiss()


def test_case_text(pack, case, section="prompt", colors=None):
    """Inspect local test definitions without preparing a provider request."""
    colors = semantic_colors(colors)
    text = Text()
    text.append(plain(case.get("id")) + "\n", style="bold " + colors["primary"])
    text.append("Not scored · Browse only" if case.get("_scored") is False else "Quick + Full" if case.get("id") in pack.get("quick_case_ids", []) else "Full only", style=colors["muted"])
    if case.get("topic"):
        text.append(" · " + plain(case["topic"]), style=colors["muted"])
    text.append("\n")
    if section == "prompt":
        fields = [("Prompt sent to the model", case.get("prompt", "No prompt recorded.")),
                  ("Response schema", case.get("output_schema", pack.get("output_schema"))),
                  ("Instructions", case.get("instructions", pack.get("instructions")))]
    elif section == "reference":
        text.append("Reference is shown for your inspection; it is not added to the model prompt.\n", style=colors["warning"])
        fields = [("Expected answer / reference", case.get("expected")),
                  ("Reference label", case.get("reference_label")),
                  ("Reference quality", case.get("reference_quality")),
                  ("Reference evidence", case.get("evidence")),
                  ("Exclusion reason", case.get("exclusion_reason")),
                  ("Source case", case.get("source_id")),
                  ("Stage / question", " / ".join(plain(case[key]) for key in ("stage", "question_id") if case.get(key)))]
    else:
        if case.get("_scored") is False:
            text.append("Excluded from scored case counts. This reference is not sent during a run.\n", style=colors["warning"])
        fields = [("Scoring", pack.get("scoring")), ("Case scoring", case.get("scoring")),
                  ("Pack and scope", pack_metadata(pack, detailed=True)),
                  ("Frozen pack hash", pack.get("hash")),
                  ("Privacy", "Browsing reads local definitions only. Starting a run sends the selected scored prompts to the chosen provider; references and browse-only cases are excluded."),
                  ("Description", pack.get("description")), ("Not-ready scenarios", pack.get("not_ready_scenarios"))]
    for label, value in fields:
        if value is None or value == "":
            continue
        text.append("\n" + label + "\n", style="bold " + colors["primary"])
        rendered = json.dumps(value, ensure_ascii=False, indent=2) if label == "Response schema" and isinstance(value, (dict, list)) else readable(value)
        text.append(plain(rendered, 32000) + "\n", style="")
    return text


class TestCase(ModalScreen):
    """A local test definition, never a run or a generated model answer."""
    BINDINGS = [("escape", "close", "Close test")]

    def __init__(self, pack, case):
        super().__init__()
        self.pack, self.case = pack, case

    def compose(self):
        with Vertical(id="test-dialog"):
            yield Static(plain(self.pack.get("name") or self.pack.get("id")) + " · Test definition", id="test-title", markup=False)
            yield Tabs(Tab("Prompt", id="test-prompt"), Tab("Reference", id="test-reference"),
                       Tab("Scoring", id="test-scoring"), id="test-tabs")
            with VerticalScroll(id="test-scroll"):
                yield Static(test_case_text(self.pack, self.case, colors=theme_colors(self.app)), id="test-evidence", markup=False)
            yield Button("Close test", id="close-test")

    def on_mount(self):
        self.app.theme_changed_signal.subscribe(self, self.refresh_theme)

    def refresh_theme(self, _theme=None):
        section = self.query_one("#test-tabs", Tabs).active.removeprefix("test-")
        self.query_one("#test-evidence", Static).update(test_case_text(self.pack, self.case, section, theme_colors(self.app)))

    def on_tabs_tab_activated(self, event):
        if event.tabs.id == "test-tabs":
            event.stop()
            self.refresh_theme()
            self.query_one("#test-scroll", VerticalScroll).scroll_home(animate=False)

    def action_close(self):
        self.dismiss()

    def on_button_pressed(self, event):
        if event.button.id == "close-test":
            event.stop()
            self.dismiss()


class ModelLab(App):
    TITLE = "Watchtower · Model Lab"
    BINDINGS = [("escape", "close", "Close"), ("f6", "themes", "Themes")]
    CSS = """
    Screen { background: $background; color: $foreground; }
    #heading { height: 2; padding: 0 2; color: $text-primary; text-style: bold; }
    #tabs { height: 2; }
    #content { height: 1fr; padding: 0 2; }
    #run-pane, #tests-pane, #history-pane, #compare-pane { height: 1fr; }
    #run-form, #review, #monitor { height: auto; }
    .pair { height: 4; grid-size: 2; grid-columns: 1fr; grid-gutter: 0 1; }
    .field { height: 4; }
    .field Label { height: 1; color: $text; }
    .field Select, .field Input { height: 3; margin: 0; }
    #targets { height: 10; grid-size: 2; grid-columns: 1fr; grid-gutter: 0 1; }
    .target { height: 10; }
    .model-notice { height: 2; color: $text-muted; }
    #limits { height: 4; grid-size: 3; grid-columns: 1fr; grid-gutter: 0 1; }
    #pack-details, #request-budget, #run-status, #run-progress, #compare-reason { height: auto; color: $text; }
    #request-budget { text-style: bold; }
    #request-budget.error { color: $text-error; }
    #review, #run-result { height: auto; padding: 1 0; }
    #run-error, .report-error { height: auto; color: $text-error; }
    #history-table, #comparison-table { height: 1fr; }
    #history-help { height: 2; color: $text-muted; }
    #history-score-scroll { height: auto; max-height: 9; }
    #history-scores { height: auto; }
    #compare-summary { height: auto; }
    #tests-summary { height: 2; color: $text; }
    #tests-table { height: 1fr; }
    #tests-selected { height: 2; color: $text-muted; }
    #notice { height: 2; padding: 0 2; color: $text; }
    #notice.error { color: $text-error; }
    #actions { height: 3; padding: 0 2; align-horizontal: right; }
    #actions Button { margin-left: 1; min-width: 9; }
    Footer { background: $panel; }
    RunReport, TestCase { align: center middle; background: $background 70%; }
    #report-dialog, #test-dialog { width: 94%; height: 94%; padding: 1 2; border: round $primary; background: $surface; }
    #report-title, #test-title { height: 1; text-style: bold; color: $text-primary; }
    #report-tabs, #test-tabs { height: 2; }
    #report-scroll, #report-details, #case-pane, #test-scroll { height: 1fr; }
    #case-table { height: 6; }
    #case-scroll { height: 1fr; }
    #close-report, #close-test { height: 3; align-horizontal: right; }
    """

    def __init__(self, service=None):
        super().__init__()
        self.initial_error = None
        if service is None:
            try:
                from service import LabService
                service = LabService()
            except Exception as exc:
                self.initial_error = safe_error(exc, "Model Lab is unavailable. Close and reopen it.")
        self.service = service
        self.profiles, self.packs, self.runs = [], [], []
        self.page, self.stage = "run", "form"
        self.busy, self.running = True, False
        self.prepared, self.last_run, self.history_id = None, None, None
        self.cancel_event = threading.Event()
        self.close_after_run = False
        self._run_task = None
        self._model_generation = [0, 0]
        self._model_loading = set()
        self._model_profiles = [None, None]
        self._history_generation = 0
        self._loading_form = False
        self.run_error_text = ""
        self.run_had_request_errors = False
        self.comparison_rows = []
        self._last_summary = None
        self._comparison_verdict = ""
        self._base_screen = None
        self._inspect_generation = 0
        self._inspecting = False
        self.inspected_pack = None
        self.test_case_id = None

    def compose(self) -> ComposeResult:
        yield Static("Model Lab · Test a model on fixed tasks", id="heading", markup=False)
        yield Tabs(Tab("Run", id="run"), Tab("Tests", id="tests"), Tab("History", id="history"), Tab("Compare", id="compare"), id="tabs")
        with Vertical(id="content"):
            with VerticalScroll(id="run-pane"):
                with Vertical(id="run-form"):
                    with Vertical(classes="field"):
                        yield Label("Run type")
                        yield Select([("Single model test", "single"), ("Compare two models", "comparison")],
                                     value="single", allow_blank=False, id="run-type")
                    with Grid(classes="pair"):
                        with Vertical(classes="field"):
                            yield Label("Benchmark pack")
                            yield Select([], id="pack", prompt="Choose a pack", disabled=True)
                        with Vertical(classes="field"):
                            yield Label("Mode")
                            yield Select([("Quick", "quick"), ("Full", "full")], value="quick", allow_blank=False, id="mode")
                    yield Static("", id="request-budget", markup=False)
                    yield Static("", id="pack-details", markup=False)
                    with Grid(id="targets"):
                        for index in range(2):
                            with Vertical(classes="field target", id=f"target-{index}"):
                                yield Label(f"Target {index + 1} · Account")
                                yield Select([], prompt="Choose account", id=f"profile-{index}", disabled=True)
                                yield Label("Model · type to search")
                                yield Select([], prompt="Choose explicit model", id=f"model-{index}")
                                yield Static("", id=f"model-notice-{index}", classes="model-notice", markup=False)
                    with Grid(id="limits"):
                        for identifier, label, value in (("repeats", "Repeats · 1–3", "1"), ("timeout", "Timeout · 30–180s", "60"),
                                                         ("request-limit", "Request limit · 1–120", "10")):
                            with Vertical(classes="field"):
                                yield Label(label)
                                yield Input(value=value, type="integer", id=identifier)
                yield Static("", id="review", markup=False)
                with Vertical(id="monitor"):
                    yield Static("", id="run-status", markup=False)
                    yield Static("", id="run-progress", markup=False)
                    yield Static("", id="run-error", markup=False)
                    yield Static("", id="run-result", markup=False)
            with Vertical(id="tests-pane"):
                with Vertical(classes="field"):
                    yield Label("Browse test pack · no account required")
                    yield Select([], id="inspect-pack", prompt="Choose a test pack", disabled=True)
                yield Static("Choose a pack to inspect its local test definitions.", id="tests-summary", markup=False)
                yield DataTable(id="tests-table", cursor_type="row", zebra_stripes=True, show_row_labels=False)
                yield Static("Select a case, then View test to read its prompt and reference.", id="tests-selected", markup=False)
            with Vertical(id="history-pane"):
                yield Static("Saved local runs. Select a row to inspect per-case outputs.", id="history-help", markup=False)
                yield DataTable(id="history-table", cursor_type="row", zebra_stripes=True, show_row_labels=False)
                with VerticalScroll(id="history-score-scroll"):
                    yield Static("Select a saved run to see model scores.", id="history-scores", markup=False)
            with Vertical(id="compare-pane"):
                with Grid(classes="pair"):
                    with Vertical(classes="field"):
                        yield Label("First run")
                        yield Select([], id="compare-a", prompt="Choose a run")
                    with Vertical(classes="field"):
                        yield Label("Second run · optional")
                        yield Select([("Same run's models", None)], value=None, allow_blank=False, id="compare-b")
                yield Static("Compare models within one run, or match identical conditions across runs.", id="compare-reason", markup=False)
                yield Static("", id="compare-verdict", markup=False)
                yield Static("", id="compare-summary", markup=False)
                yield DataTable(id="comparison-table", cursor_type="row", zebra_stripes=True, show_row_labels=False)
        yield Static("Loading saved accounts, packs and history…", id="notice", markup=False)
        with Horizontal(id="actions"):
            for identifier, label in (("refresh", "Refresh"), ("back", "Back"), ("review-setup", "Review setup"),
                                      ("start", "Start run"), ("cancel-run", "Cancel run"), ("new-run", "New run"),
                                      ("report", "View results"), ("view-test", "View test"), ("compare-runs", "Compare"), ("themes", "Themes"), ("close", "Close")):
                yield Button(label, id=identifier, variant="primary" if identifier in {"review-setup", "start", "compare-runs", "view-test"} else "default")
        yield Footer()

    def on_mount(self):
        self._base_screen = self.screen
        self.theme_changed_signal.subscribe(self, self.refresh_theme)
        self.query_one("#history-table", DataTable).add_columns("Run", "Pack / mode", "Status", "Done")
        tests = self.query_one("#tests-table", DataTable)
        for label, width in (("Case", 26), ("Topic", 18), ("Quick", 6), ("Scoring", 10)):
            tests.add_column(label, width=width)
        comparison = self.query_one("#comparison-table", DataTable)
        comparison.add_column("Model", width=20, key="target")
        for label, width in (("Score", 10), ("Quality", 10), ("Cover", 7), ("Err", 4), ("Median", 8)):
            comparison.add_column(label, width=width)
        self.controls()
        if self.initial_error:
            self.busy = False
            self.notice(self.initial_error, True)
            self.controls()
        else:
            self.load_initial()

    def search_themes(self):
        self.push_screen(CommandPalette(providers=[ThemePaletteProvider], placeholder="Search themes…"))

    def action_themes(self):
        self.search_themes()

    def refresh_theme(self, _theme=None):
        """Recolor already-loaded evidence; never refetch or re-run a test."""
        base = self._base_screen
        if not self.is_mounted or base is None:
            return
        colors = theme_colors(self)
        if self.prepared:
            base.query_one("#review", Static).update(review_text(self.prepared, colors))
        if self._last_summary:
            base.query_one("#run-result", Static).update(self.run_result_text(self._last_summary))
        run = next((item for item in self.runs if item["run_id"] == self.history_id), None)
        base.query_one("#history-scores", Static).update(score_summary(run, compact=True, colors=colors) if run else "No model results yet.")
        if self._comparison_verdict:
            base.query_one("#compare-verdict", Static).update(Text(self._comparison_verdict, style="bold " + colors["primary"]))
        table = base.query_one("#comparison-table", DataTable)
        for row_index, row in enumerate(self.comparison_rows):
            for column, value in enumerate(self.comparison_cells(row)):
                table.update_cell_at(Coordinate(row_index, column), value)

    def run_result_text(self, summary):
        result = Text("Recorded " + plain(summary.get("completed")) + " / "
            + plain(summary.get("request_count")) + " requests.\n\n")
        result.append_text(score_summary(summary, compact=True, colors=theme_colors(self)))
        result.append("\nView results for scoring rules and individual answers.", style=theme_colors(self)["muted"])
        return result

    def comparison_cells(self, row):
        colors = theme_colors(self)
        return (Text(plain(row.get("model") or row.get("target"))),
                Text(score_label(row.get("benchmark_score")), style="bold " + colors["primary"] if row.get("benchmark_score") is not None else colors["muted"]),
                Text(score_label(row.get("quality_score"))), Text(metric(row.get("coverage"), "%")),
                Text(metric(row.get("execution_errors")), style=colors["error"] if row.get("execution_errors") else colors["muted"]),
                Text(response_seconds(row.get("median_response_ms"))))

    def notice(self, message, error=False):
        self.query_one("#notice").set_class(error, "error")
        self.query_one("#notice", Static).update(Text(plain(message, 1000)))

    def controls(self):
        if not self.is_mounted or not self.query("#run-pane"):
            return
        for page in ("run", "tests", "history", "compare"):
            self.query_one(f"#{page}-pane").display = self.page == page
        self.query_one("#run-form").display = self.stage == "form"
        self.query_one("#review").display = self.stage == "review"
        self.query_one("#monitor").display = self.stage in {"running", "finished"}
        self.query_one("#target-1").display = self.target_count() == 2
        self.query_one("#targets").styles.grid_size_columns = self.target_count()
        for control in self.query("#run-form Select, #run-form Input"):
            control.disabled = self.busy or self.running or not (self.profiles and self.packs)
        for index in self._model_loading:
            self.query_one(f"#model-{index}").disabled = True
        for control in self.query("#compare-pane Select"):
            control.disabled = self.busy or self.running
        self.query_one("#inspect-pack").disabled = self.busy or self.running or not self.packs
        visible = {"close", "themes"}
        if self.running:
            visible.add("cancel-run")
        elif self.page == "run":
            visible |= {"form": {"review-setup"}, "review": {"back", "start"}, "finished": {"new-run", "report"}}.get(self.stage, set())
        elif self.page == "history":
            visible |= {"refresh", "report"}
        elif self.page == "tests":
            visible.add("view-test")
        else:
            visible |= {"refresh", "compare-runs"}
        for button in self.query("#actions Button"):
            button.display = button.id in visible
            button.disabled = self.busy and button.id not in {"close", "themes"}
        self.query_one("#review-setup").disabled = self.busy or self.active_models_loading() or not (self.profiles and self.packs)
        self.query_one("#cancel-run").disabled = self.cancel_event.is_set()
        self.query_one("#report").disabled = self.busy or not (self.last_run if self.page == "run" else self.history_id)
        self.query_one("#start").disabled = self.busy or self.running or not self.prepared
        self.query_one("#compare-runs").disabled = self.busy or not self.runs
        self.query_one("#view-test").disabled = self.busy or self.running or self._inspecting or self.test_case_id is None

    @work(group="initial")
    async def load_initial(self):
        try:
            profiles, packs, history = await asyncio.gather(asyncio.to_thread(self.service.profiles),
                asyncio.to_thread(self.service.packs), asyncio.to_thread(self.service.history), return_exceptions=True)
            problems = []
            if isinstance(profiles, Exception):
                problems.append(safe_error(profiles, "Accounts are unavailable. Local tests remain browseable."))
                profiles = {}
            if isinstance(packs, Exception):
                problems.append(safe_error(packs, "Local test packs could not be loaded."))
                packs = []
            if isinstance(history, Exception):
                problems.append(safe_error(history, "Saved history could not be loaded."))
                history = []
            self.profiles = [p for p in profiles.get("profiles", []) if p.get("id") and p.get("provider") in {"codex", "opencode"}]
            self.packs = packs
            self._loading_form = True
            try:
                pack = self.query_one("#pack", Select)
                with pack.prevent(Select.Changed):
                    pack.set_options([(Text(pack_option_label(p)), p["id"]) for p in packs])
                    pack.value = packs[0]["id"] if packs else Select.NULL
                inspector = self.query_one("#inspect-pack", Select)
                with inspector.prevent(Select.Changed):
                    inspector.set_options([(Text(pack_option_label(p)), p["id"]) for p in packs])
                    inspector.value = pack.value
                ids = [p["id"] for p in self.profiles]
                default = profiles.get("default_profile")
                default = default if default in ids else (ids[0] if ids else None)
                options = [(Text(plain(p.get("label") or p["id"]) + " · " + plain(p.get("provider"))), p["id"]) for p in self.profiles]
                for index in range(2):
                    picker = self.query_one(f"#profile-{index}", Select)
                    with picker.prevent(Select.Changed):
                        picker.set_options(options)
                        picker.value = default if default else Select.NULL
                    if index < self.target_count():
                        self.load_profile_models(index)
                self.update_pack_details()
                self.set_history(history)
            finally:
                self._loading_form = False
            self.notice(" ".join(problems) if problems else
                        "Choose a model and test pack. Two-model comparison is optional. Nothing runs until Start."
                        if self.profiles and self.packs else
                        "Browse local scenarios in Tests. Add a Codex or OpenCode account in Accounts when you want to run them."
                        if self.packs else "No benchmark packs are installed.", bool(problems) or not self.packs)
        except Exception as error:
            self.notice(safe_error(error, "Model Lab could not load its catalog. No test requests were sent."), True)
        finally:
            self.busy = False
            self.controls()
            if self.page == "tests":
                self.load_test_pack()

    def update_pack_details(self):
        value = self.query_one("#pack", Select).value
        pack = next((p for p in self.packs if p["id"] == value), {})
        mode = self.query_one("#mode", Select).value
        details = pack_metadata(pack, mode)
        if not pack.get("collection") == "published-subset" and pack.get("description"):
            details += "\n" + plain(pack["description"], 300)
        widget = self.query_one("#pack-details", Static)
        widget.update(Text(details))
        widget.tooltip = pack_metadata(pack, mode, detailed=True)
        self.update_request_budget()

    def test_cases(self):
        pack = self.inspected_pack or {}
        return [{**case, "_scored": True} for case in pack.get("cases", [])] + [
            {**case, "_scored": False} for case in pack.get("unscored_cases", [])]

    def load_test_pack(self):
        selected = self.query_one("#inspect-pack", Select).value
        self._inspect_generation += 1
        self.inspected_pack, self.test_case_id = None, None
        self.query_one("#tests-table", DataTable).clear()
        self.query_one("#tests-selected", Static).update("")
        self._inspecting = selected is not Select.NULL
        self.query_one("#tests-summary", Static).update("Reading local test definitions…" if self._inspecting else "No test packs are installed.")
        self.controls()
        if self._inspecting:
            self.fetch_test_pack(str(selected), self._inspect_generation)

    @work(group="inspect")
    async def fetch_test_pack(self, pack_id, generation):
        try:
            pack = await asyncio.to_thread(self.service.inspect_pack, pack_id)
            if not isinstance(pack, dict) or pack.get("id") != pack_id or not isinstance(pack.get("cases"), list):
                raise ValueError("Invalid test definition")
            error = None
        except Exception as exc:
            pack, error = None, safe_error(exc, "This test pack could not be read. No model requests were sent.")
        if (not self.is_mounted or generation != self._inspect_generation
                or self.query_one("#inspect-pack", Select).value != pack_id):
            return
        self._inspecting = False
        self.inspected_pack = pack
        if error:
            self.query_one("#tests-summary", Static).update(error)
            if self.page == "tests":
                self.notice(error, True)
        else:
            summary = f"{collection_label(pack)} | Quick {len(pack.get('quick_case_ids', []))} | Full {len(pack['cases'])}"
            if pack.get("unscored_cases"):
                summary += f" | Not scored {len(pack['unscored_cases'])}"
            if pack.get("not_ready_scenarios"):
                summary += f" | Not-ready groups {len(pack['not_ready_scenarios'])}"
            self.query_one("#tests-summary", Static).update(Text(summary))
            self.query_one("#tests-summary").tooltip = pack_metadata(pack, detailed=True) + "\nFrozen hash: " + plain(pack.get("hash"))
            table = self.query_one("#tests-table", DataTable)
            for index, case in enumerate(self.test_cases()):
                table.add_row(Text(plain(case.get("id"))), Text(plain(case.get("topic") or pack.get("category"))),
                    "Yes" if case.get("_scored") and case.get("id") in pack.get("quick_case_ids", []) else "—",
                    "Scored" if case.get("_scored") else "Not scored", key=str(index))
            if table.row_count:
                self.select_test_case("0")
            else:
                self.query_one("#tests-selected", Static).update("No case definitions are available in this pack.")
            if self.page == "tests":
                self.notice("Select a case, then View test. Browsing sends no model requests.")
        self.controls()

    def select_test_case(self, key):
        cases = self.test_cases()
        try:
            case = cases[int(key)]
        except (ValueError, TypeError, IndexError):
            return
        self.test_case_id = str(key)
        title = plain(case.get("id")) + (" · Not scored" if case.get("_scored") is False else "")
        self.query_one("#tests-selected", Static).update(Text(title + "\n" + plain(case.get("prompt"), 200)))
        self.controls()

    @work(group="test-modal")
    async def open_test(self):
        if self.busy or self.running or self._inspecting or self.test_case_id is None or not self.inspected_pack:
            return
        case = self.test_cases()[int(self.test_case_id)]
        self.busy = True
        self.controls()
        try:
            await self.push_screen_wait(TestCase(self.inspected_pack, case))
        finally:
            self.busy = False
            if self.is_mounted:
                self.controls()

    def required_requests(self):
        pack = next((p for p in self.packs if p["id"] == self.query_one("#pack", Select).value), {})
        count = selected_case_count(pack, self.query_one("#mode", Select).value)
        try:
            repeats = int(self.query_one("#repeats", Input).value)
        except ValueError:
            return None
        return count * repeats * self.target_count() if count and 1 <= repeats <= 3 else None

    def target_count(self):
        return 2 if self.query_one("#run-type", Select).value == "comparison" else 1

    def active_models_loading(self):
        return bool(self._model_loading.intersection(range(self.target_count())))

    def update_request_budget(self):
        if not self.is_mounted or not self.query("#request-budget"):
            return
        required = self.required_requests()
        try:
            limit = int(self.query_one("#request-limit", Input).value)
        except ValueError:
            limit = None
        error = required is not None and limit is not None and (required > limit or required > 120)
        if required is None or limit is None:
            message = "Required requests: enter valid repeats and request limit."
        else:
            count = self.target_count()
            message = f"Required requests: {required} | Your limit: {limit} | {count} target" + ("s" if count != 1 else "")
            if required > 120:
                message += "\nExceeds the 120-request cap. Reduce repeats or choose Quick."
            elif required > limit:
                message += "\nExceeds your limit. Adjust it explicitly before starting."
        widget = self.query_one("#request-budget", Static)
        widget.set_class(error, "error")
        widget.update(Text(message))

    def on_input_changed(self, event):
        if event.input.id in {"repeats", "request-limit", "timeout"} and self.is_mounted and not self.running:
            self.prepared = None
            self.update_request_budget()

    def load_profile_models(self, index):
        self._model_generation[index] += 1
        generation = self._model_generation[index]
        self._model_profiles[index] = None
        profile = self.query_one(f"#profile-{index}", Select).value
        picker = self.query_one(f"#model-{index}", Select)
        with picker.prevent(Select.Changed):
            picker.set_options([])
            picker.value = Select.NULL
        self._model_loading.discard(index)
        if profile is not Select.NULL:
            self._model_loading.add(index)
            self.query_one(f"#model-notice-{index}", Static).update("Loading models…")
            self.fetch_models(index, str(profile), generation)

    @work(group="models")
    async def fetch_models(self, index, profile, generation):
        try:
            result = await asyncio.to_thread(self.service.models, profile)
            choices = {m["id"]: plain(m.get("label") or m["id"], 160) for m in result.get("models", [])
                       if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]}
            message = plain(result.get("notice")) or ("Type to search models." if choices else "No models available. Check this account in Accounts.")
        except Exception:
            choices, message = {}, "Models unavailable. Choose another connected account or check Accounts."
        if (not self.is_mounted or generation != self._model_generation[index]
                or self.query_one(f"#profile-{index}", Select).value != profile):
            return
        picker = self.query_one(f"#model-{index}", Select)
        with picker.prevent(Select.Changed):
            picker.set_options([(Text(label if label == identity else label + " · " + identity), identity)
                                for identity, label in choices.items()])
            picker.value = Select.NULL
        item = self.query_one(f"#model-notice-{index}", Static)
        item.update(Text(message))
        item.tooltip = message
        self._model_loading.discard(index)
        self._model_profiles[index] = profile
        self.controls()

    def on_select_changed(self, event):
        if self._loading_form or self.running or self.busy:
            return
        identifier = event.select.id or ""
        if identifier == "inspect-pack":
            self.load_test_pack()
        if identifier.startswith("profile-"):
            index = int(identifier[-1])
            if index < self.target_count():
                self.load_profile_models(index)
        if identifier == "run-type":
            profile = self.query_one("#profile-1", Select).value
            if (self.target_count() == 2 and self._model_profiles[1] != profile
                    and 1 not in self._model_loading):
                self.load_profile_models(1)
            self.update_request_budget()
        if identifier in {"pack", "mode"}:
            self.update_pack_details()
        if identifier in {"compare-a", "compare-b"}:
            self.comparison_rows = []
            self._comparison_verdict = ""
            self.query_one("#comparison-table", DataTable).clear()
            self.query_one("#compare-summary", Static).update("")
            self.query_one("#compare-verdict", Static).update("")
            self.query_one("#compare-reason", Static).update("Press Compare to verify matching conditions.")
        if identifier.startswith(("profile-", "model-")) or identifier in {"pack", "mode", "run-type"}:
            self.prepared = None
        self.controls()

    def on_tabs_tab_activated(self, event):
        if event.tabs.id != "tabs":
            return
        self.page = event.tab.id
        if self.is_mounted and not self.busy and not self.running:
            if self.page == "history":
                self.notice("Select a saved run to inspect its actual outputs. Refresh only reads local history.")
            elif self.page == "compare":
                self.notice("Compare checks completed runs for identical conditions. It sends no model requests.")
            elif self.page == "tests":
                self.notice("Browse local prompts, response schemas, references and scoring. No model requests are sent.")
                selected = self.query_one("#inspect-pack", Select).value
                if not self._inspecting and (not self.inspected_pack or self.inspected_pack.get("id") != selected):
                    self.load_test_pack()
            elif self.stage == "review":
                self.notice("Review request count and account/model choices. Start explicitly sends the test requests.")
            elif self.stage == "form":
                self.notice("Choose models and limits, then review the exact request count. Nothing runs until Start.")
        self.controls()

    def on_button_pressed(self, event):
        actions = {"review-setup": self.prepare_run, "start": self.start_run, "back": self.back,
                   "cancel-run": self.cancel_run, "new-run": self.new_run, "close": self.action_close,
                   "refresh": self.refresh_history, "report": self.open_report, "compare-runs": self.compare_runs}
        actions["themes"] = self.action_themes
        actions["view-test"] = self.open_test
        if event.button.id in actions:
            event.stop()
            actions[event.button.id]()

    @work(group="prepare")
    async def prepare_run(self):
        if self.busy or self.running or self.stage != "form" or self.active_models_loading():
            return
        self.busy = True
        self.controls()
        try:
            values = [int(self.query_one("#" + key, Input).value) for key in ("repeats", "timeout", "request-limit")]
            if not (1 <= values[0] <= 3 and 30 <= values[1] <= 180 and 1 <= values[2] <= 120):
                self.notice("Use 1–3 repeats, a 30–180 second timeout and a request limit of 1–120.", True)
                return
            required = self.required_requests()
            if required is not None and required > values[2]:
                self.notice(f"Request count {required} exceeds your limit of {values[2]}. Adjust the limit or reduce this run.", True)
                return
            targets = [{"profile_id": self.query_one(f"#profile-{i}", Select).value,
                        "model": self.query_one(f"#model-{i}", Select).value} for i in range(self.target_count())]
            if any(t["profile_id"] is Select.NULL or t["model"] is Select.NULL for t in targets):
                self.notice("Choose an account and an explicit model for each selected target.", True)
                return
            self.prepared = await asyncio.to_thread(self.service.prepare,
                str(self.query_one("#pack", Select).value), str(self.query_one("#mode", Select).value), targets, *values)
            if not isinstance(self.prepared, dict) or not self.prepared.get("token"):
                raise ValueError("Invalid prepared run")
            self.query_one("#review", Static).update(review_text(self.prepared, theme_colors(self)))
            self.stage = "review"
            self.query_one("#run-pane", VerticalScroll).scroll_home(animate=False)
            self.notice("Review request count and account/model choices. Start explicitly sends the test requests.")
        except ValueError as error:
            self.notice(safe_error(error, "Check the numeric limits and model selections. Nothing was run."), True)
        except Exception as error:
            self.notice(safe_error(error, "This run could not be prepared. Nothing was run."), True)
        finally:
            self.busy = False
            self.controls()

    def back(self):
        if not self.running and not self.busy:
            self.prepared = None
            self.stage = "form"
            self.controls()

    def new_run(self):
        if not self.running and not self.busy:
            self.prepared = None
            self.stage = "form"
            self.notice("Adjust the targets or conditions, then review a new run.")
            self.controls()

    def progress(self, payload):
        try:
            self.call_from_thread(self.accept_progress, payload)
        except RuntimeError:
            pass

    def accept_progress(self, payload):
        if not self.is_mounted or not self.running or not isinstance(payload, dict) or not self.query("#run-progress"):
            return
        self.query_one("#run-progress", Static).update(Text(plain(payload.get("completed")) + " / "
            + plain(payload.get("total")) + " requests · " + plain(payload.get("target")) + " / "
            + plain(payload.get("case_id")) + " · " + plain(payload.get("status"))))
        if payload.get("status") == "ok":
            if self.run_error_text:
                self.run_error_text = ""
                self.query_one("#run-error", Static).update("")
                if not self.cancel_event.is_set():
                    self.notice("Latest request succeeded. Earlier request errors remain in the run report.")
        elif payload.get("status") != "cancelled" and self.show_run_error(payload, replace=True):
            self.run_had_request_errors = True
            self.notice("A request failed. The reason is shown above and retained in the run report.", True)

    def show_run_error(self, payload, replace=False):
        """Show service-owned safe diagnostics, never an arbitrary exception."""
        message = payload.get("error")
        code = payload.get("error_code")
        message = plain(message, 1500) if isinstance(message, str) else ""
        code = plain(code, 80) if isinstance(code, str) else ""
        if not (message or code) or (self.run_error_text and not replace):
            return False
        context = " / ".join(plain(payload.get(key), 200) for key in ("target", "case_id") if payload.get(key))
        self.run_error_text = ((context + "\n") if context else "") + ((code + ": ") if code else "") + (message or "Request failed. Inspect the run report before retrying.")
        self.query_one("#run-error", Static).update(Text(self.run_error_text))
        return True

    @work(group="run")
    async def start_run(self):
        if self.running or self.busy or self.stage != "review" or not self.prepared:
            return
        token = self.prepared["token"]
        self.prepared = None
        self.last_run = None
        self._last_summary = None
        self.running = True
        self.stage = "running"
        self.cancel_event = threading.Event()
        self.run_error_text = ""
        self.run_had_request_errors = False
        self.query_one("#run-status", Static).update("Running the reviewed benchmark…")
        self.query_one("#run-progress", Static).update("")
        self.query_one("#run-error", Static).update("")
        self.query_one("#run-result", Static).update("")
        self.notice("Cancel stops this run and waits for its owned processes to close.")
        self.controls()
        try:
            self._run_task = asyncio.create_task(asyncio.to_thread(self.service.run, token, self.cancel_event, self.progress))
            try:
                summary = await asyncio.shield(self._run_task)
            except asyncio.CancelledError:
                self.cancel_event.set()
                summary = await asyncio.shield(self._run_task)
            if self.is_mounted:
                self.last_run = summary.get("run_id")
                self._last_summary = summary
                status = summary.get("status")
                self.query_one("#run-status", Static).update(Text("Run " + plain(status) + " · " + plain(self.last_run)))
                if status != "cancelled":
                    self.show_run_error(summary, replace=True)
                self.query_one("#run-result", Static).update(self.run_result_text(summary))
                if status == "failed":
                    if not self.run_error_text:
                        self.show_run_error({"error": "The run failed. Inspect the saved report before starting another run."})
                    self.notice("Run failed and stopped. Saved evidence is available in View results. No automatic retry.", True)
                elif status == "cancelled":
                    self.notice("Run cancelled. Partial results are saved in View results; remaining requests were not run.")
                elif self.run_error_text or self.run_had_request_errors or any(row.get("errors", 0) for row in summary.get("rows", [])):
                    self.notice("Run saved with request errors. View results contains their details.", True)
                else:
                    self.notice("Run saved locally. Inspect its actual outputs and comparison metrics.")
        except Exception as error:
            if self.is_mounted:
                message = safe_error(error, "The run stopped unexpectedly. Inspect History before starting another run.")
                self.show_run_error({"error": message}, replace=True)
                self.notice(message, True)
                self.query_one("#run-status", Static).update("Run failed. No automatic retry.")
        finally:
            self.running = False
            self.stage = "finished"
            if self.is_mounted:
                self.controls()
                if self.close_after_run:
                    self.exit()
                else:
                    self.refresh_history()

    def cancel_run(self):
        if self.running:
            self.cancel_event.set()
            self.query_one("#run-status", Static).update("Cancelling… waiting for owned processes to finish cleanup.")
            self.notice("Cancellation requested. Partial results will remain in History.")
            self.controls()

    def action_close(self):
        if self.running:
            self.close_after_run = True
            self.cancel_run()
        else:
            self.exit()

    action_quit = action_close

    def set_history(self, runs):
        self.runs = [r for r in runs if isinstance(r, dict) and r.get("run_id")]
        table = self.query_one("#history-table", DataTable)
        table.clear()
        ids = [r["run_id"] for r in self.runs]
        self.history_id = self.history_id if self.history_id in ids else (ids[0] if ids else None)
        for run in self.runs:
            table.add_row(Text(plain(run.get("created_at") or run["run_id"], 32)),
                Text(plain(run.get("pack_name") or run.get("pack_id")) + " / " + plain(run.get("mode"))),
                Text(plain(run.get("status"))), f"{metric(run.get('completed'))}/{metric(run.get('request_count'))}", key=run["run_id"])
        self.query_one("#history-help", Static).update("Saved local runs. Select a row for per-case outputs."
            if ids else "No saved runs yet. Configure Run and explicitly press Start to create one.")
        if self.history_id:
            table.move_cursor(row=ids.index(self.history_id))
        self.update_history_scores()
        options = [(Text(plain(r.get("created_at") or r["run_id"], 32) + " · " + plain(r.get("pack_name") or r.get("pack_id"))
                         + " / " + plain(r.get("mode"))), r["run_id"]) for r in self.runs]
        for identifier in ("compare-a", "compare-b"):
            picker = self.query_one("#" + identifier, Select)
            previous = picker.value
            with picker.prevent(Select.Changed):
                picker.set_options(options if identifier == "compare-a" else [("Same run's models", None), *options])
                picker.value = previous if previous in ids else (ids[0] if ids and identifier == "compare-a" else None if identifier == "compare-b" else Select.NULL)

    def update_history_scores(self):
        run = next((item for item in self.runs if item["run_id"] == self.history_id), None)
        self.query_one("#history-scores", Static).update(score_summary(run, compact=True, colors=theme_colors(self)) if run else "No model results yet.")

    @work(group="history")
    async def refresh_history(self):
        self._history_generation += 1
        request = self._history_generation
        try:
            history = await asyncio.to_thread(self.service.history)
            if self.is_mounted and request == self._history_generation:
                self.set_history(history)
                self.controls()
        except Exception as error:
            if self.is_mounted:
                self.notice(safe_error(error, "Saved runs could not be loaded. Existing records are unchanged."), True)

    def on_data_table_row_highlighted(self, event):
        if not self.query("#run-pane"):
            return
        if event.data_table.id == "tests-table":
            if event.row_key in event.data_table.rows:
                self.select_test_case(event.row_key.value)
        elif event.data_table.id == "history-table":
            self.history_id = str(event.row_key.value)
            self.update_history_scores()
            self.controls()
        elif event.data_table.id == "comparison-table":
            if event.row_key not in event.data_table.rows:
                return
            index = int(event.row_key.value)
            if index >= len(self.comparison_rows):
                return
            row = self.comparison_rows[index]
            self.query_one("#compare-summary", Static).update(Text("Selected: " + plain(row.get("target"))
                + "\nUsage and cost are separate; available in History → View results."))

    def on_data_table_row_selected(self, event):
        if event.data_table.id == "tests-table" and event.row_key in event.data_table.rows:
            event.stop()
            self.select_test_case(event.row_key.value)
            self.open_test()

    @work(group="report")
    async def open_report(self):
        run_id = self.last_run if self.page == "run" else self.history_id
        if not run_id or self.busy or self.running:
            return
        self.busy = True
        self.controls()
        try:
            report = await asyncio.to_thread(self.service.report, run_id)
            await self.push_screen_wait(RunReport(report))
        except Exception as error:
            self.notice(safe_error(error, "This report could not be opened. Saved results are unchanged."), True)
        finally:
            self.busy = False
            self.controls()

    @work(group="compare")
    async def compare_runs(self):
        if self.busy or self.running:
            return
        first, second = (self.query_one("#" + key, Select).value for key in ("compare-a", "compare-b"))
        if first is Select.NULL:
            self.notice("Choose a saved run first.", True)
            return
        ids = [str(first)]
        if second is not None and second is not Select.NULL and second != first:
            ids.append(str(second))
        self.busy = True
        self.controls()
        table = self.query_one("#comparison-table", DataTable)
        table.clear()
        self.comparison_rows = []
        self._comparison_verdict = ""
        self.query_one("#compare-verdict", Static).update("")
        try:
            result = await asyncio.to_thread(self.service.compare, ids)
            self.query_one("#compare-reason", Static).update(Text(plain(result.get("reason"), 2000)))
            self.notice("Quality = answered-case credit. Cover = answered requests. Median = successful response time.")
            if result.get("comparable") is True:
                self.comparison_rows = result.get("rows", [])
                verdict = result.get("verdict")
                if isinstance(verdict, dict):
                    self._comparison_verdict = plain(verdict.get("text"), 1500)
                    self.query_one("#compare-verdict", Static).update(Text(self._comparison_verdict, style="bold " + theme_colors(self)["primary"]))
                for index, row in enumerate(self.comparison_rows):
                    table.add_row(*self.comparison_cells(row), key=str(index))
                self.query_one("#compare-summary", Static).update("Select a model. Usage and cost are in History → View results.")
            else:
                self.query_one("#compare-summary", Static).update("Comparison blocked. Choose completed runs with identical test conditions.")
        except Exception as error:
            self.notice(safe_error(error, "These runs could not be compared. No benchmark was started."), True)
        finally:
            self.busy = False
            self.controls()


def main():
    ModelLab().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
