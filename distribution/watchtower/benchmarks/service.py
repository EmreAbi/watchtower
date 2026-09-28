"""Versioned benchmark plans, bounded explicit runs, and comparable evidence."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import statistics
import threading
import time

try:
    from . import packs, grading, answer_grading, private_packs, choice_grading
    from .providers import ERROR_MESSAGES, NOTICE_MESSAGES, _accounts_module
except ImportError:
    import packs, grading, answer_grading, private_packs, choice_grading
    from providers import ERROR_MESSAGES, NOTICE_MESSAGES, _accounts_module

PROTOCOL = "watchtower-model-lab/1"
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_CHARS = 32000
_ID = re.compile(r"[a-f0-9]{32}\Z")
FATAL_PROVIDER_ERRORS = frozenset({"authorization", "rate_limit", "model_unavailable", "unsupported_protocol", "opencode_free_tier",
                                   "configuration_error", "ownership_failed", "invalid_input", "model_mismatch"})
SCORING = {
    "version": "case-credit-v1",
    "rule": "Benchmark score = 100 × total case credit ÷ planned case attempts.",
    "explanation": (
        "Each case attempt has equal weight and can earn partial credit. A benchmark score requires a completed run, "
        "all planned attempts, and at least one scored response. Technical errors earn zero benchmark credit. "
        "Quality is the average credit of scored responses only; it is not a pass rate. Coverage is scored responses "
        "divided by planned attempts. Response times include adapter startup and use scored responses only. "
        "Incomplete runs and targets with no scored responses are not ranked. Speed and cost do not affect scores."
    ),
}


def _grader_for(pack):
    if pack.get("capability") == "typed-choice":
        return choice_grading
    return answer_grading if pack.get("capability") in ("numeric-answer", "exact-answer", "normalized-answer") else grading


def _pack_info(pack):
    return private_packs.pack_info(pack) if pack.get("capability") == "typed-choice" else packs.pack_info(pack)


def _scoring(info):
    if info.get("scoring_kind") == "reference-agreement":
        return dict(version="reference-agreement-v1", kind="reference-agreement",
                    rule="Reference agreement = 100 × matched answers ÷ planned eligible questions.",
                    explanation="This measures agreement with the declared frozen reference labels; inspect their quality before interpreting the score. "
                    "Each eligible question earns full credit or zero. Excluded source records are available for inspection only and are never "
                    "silently assigned a reference class. Errors earn zero in completed runs. Agreement among responses uses received responses; "
                    "coverage shows the proportion received. Incomplete runs are not ranked. This is a single-question JSON adaptation; "
                    "historical runs using other execution protocols are not directly comparable.")
    if info.get("scoring_kind") != "accuracy":
        return deepcopy(SCORING)
    return dict(version="binary-accuracy-v1", kind="accuracy",
                rule="Subset accuracy = 100 × correct answers ÷ planned case attempts.",
                explanation="Each question earns either full credit or zero; no partial credit. Technical errors earn zero credit in completed runs. "
                "Answer quality is accuracy among received responses; coverage shows how many were received. "
                "Incomplete runs or targets with no answers are not scored. This fixed subset uses an adapted zero-shot JSON protocol, "
                "not an official full-benchmark leaderboard score. Speed and cost are separate.")


class LabError(ValueError):
    def __init__(self, message):
        self.message = message
        super().__init__(message)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _number(value):
    return value if type(value) in (float, int) and math.isfinite(value) and value >= 0 else None


def _plain(value, limit=300):
    return " ".join(str(value or "").split())[:limit]


def _write(path, data, *, preserve_order=False):
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(4))
    try:
        with temporary.open("xb") as stream:
            stream.write(json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
                         if preserve_order else _canonical(data))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _p95(values):
    return sorted(values)[max(0, math.ceil(len(values) * .95) - 1)] if values else None


class LabService:
    def __init__(self, runner=None, home=None):
        if runner is None:
            if __package__:
                from .providers import ProviderRunner
            else:
                from providers import ProviderRunner
            runner = ProviderRunner()
        self.runner = runner
        base = home or _accounts_module().default_home()
        if not Path(base).is_absolute():
            raise LabError("Model Lab requires an absolute Watchtower home.")
        self.root = Path(base).resolve() / "state" / "benchmarks" / "runs"
        self.pack_root = self.root.parent / "packs"
        self._plans = {}
        self._lock = threading.Lock()
        self._active = set()

    def packs(self):
        try:
            local = private_packs.list_packs(self.pack_root)
            if any(item["id"] in packs.MANIFEST for item in local):
                raise ValueError("Private pack identity collides with a bundled pack.")
            return deepcopy(packs.list_packs() + local)
        except (OSError, ValueError):
            raise LabError("A local test pack is missing, changed or invalid. Restore its versioned files.") from None

    def _load_pack(self, pack_id):
        if isinstance(pack_id, str) and pack_id in packs.MANIFEST:
            return packs.load_pack(pack_id)
        return private_packs.load_pack(self.pack_root, pack_id)

    def inspect_pack(self, pack_id):
        """Read a test library without looking up accounts or preparing a paid run."""
        try:
            pack = self._load_pack(pack_id)
        except (OSError, ValueError, KeyError, TypeError):
            raise LabError("The test pack is missing, changed or invalid. Restore its versioned files.") from None
        capability = pack["capability"]
        schemas = {
            "structured-decisions": {"action": "approve | reject | review", "priority": "integer: 0 | 1 | 2",
                                     "reason": "routine | insufficient_data | unverified | risk_limit | manual_check"},
            "coding": {"code": "Python source defining solve(...)"},
            "debugging": {"code": "Corrected Python source defining solve(...)"},
            "code-review": {"findings": [{"line": "positive integer", "category": "boundary | null-handling | mutation | security | resource | arithmetic | logic"}]},
        }
        return deepcopy({**_pack_info(pack), "hash": pack["hash"], "cases": pack["cases"],
                         "unscored_cases": pack.get("unscored_cases", []),
                         "quick_case_ids": pack["quick_case_ids"],
                         "output_schema": pack.get("output_schema", schemas.get(capability, {"answer": "string"})),
                         "instructions": pack.get("instructions", "Return only the JSON object requested in the prompt. Reference answers are shown here for inspection; they are not sent to the model."),
                         "scoring": _scoring(_pack_info(pack)),
                         "not_ready_scenarios": pack.get("not_ready_scenarios", [])})

    def profiles(self):
        try:
            raw = self.runner.profiles()
            items = raw.get("profiles", []) if isinstance(raw, dict) else raw
            profiles = [{"id": p["id"], "label": p.get("label") or p["id"], "provider": p["provider"]}
                        for p in items if p.get("provider") in ("codex", "opencode") and not p.get("configuration_error")]
            return {"profiles": profiles, "default_profile": raw.get("default_profile") if isinstance(raw, dict) else None}
        except Exception:
            raise LabError("Accounts could not be read. Open Accounts and check the selected profiles.") from None

    def models(self, profile_id):
        try:
            catalog = self.runner.models(profile_id)
            return {"models": [{"id": m["id"], "label": m.get("label") or m["id"]} for m in catalog["models"]],
                    "notice": catalog.get("notice", ""), "default_model": catalog.get("default_model")}
        except Exception:
            return {"models": [], "notice": "Models could not be loaded. Select a connected account and try again.", "default_model": None}

    def prepare(self, pack_id, mode, assignments, repeats=1, timeout_seconds=60, request_limit=10):
        if mode not in ("quick", "full"):
            raise LabError("Choose Quick or Full.")
        if type(repeats) is not int or not 1 <= repeats <= 3:
            raise LabError("Choose one to three repeats.")
        if type(timeout_seconds) is not int or not 30 <= timeout_seconds <= 180:
            raise LabError("Choose a per-request timeout from 30 to 180 seconds.")
        if type(request_limit) is not int or not 1 <= request_limit <= 120:
            raise LabError("Choose a request limit from 1 to 120.")
        if not isinstance(assignments, list) or not 1 <= len(assignments) <= 2:
            raise LabError("Choose one or two account/model targets.")
        try:
            pack = self._load_pack(pack_id)
            selected = private_packs.select_cases(pack, mode) if pack.get("capability") == "typed-choice" else packs.select_cases(pack, mode)
        except (ValueError, OSError, KeyError):
            raise LabError("The benchmark pack is invalid or changed. Restore its versioned files.") from None
        request_count = len(selected) * repeats * len(assignments)
        if request_count > request_limit:
            raise LabError(f"This run needs {request_count} requests; the chosen limit is {request_limit}.")
        profiles = {p["id"]: p for p in self.profiles()["profiles"]}
        catalogs, targets, seen = {}, [], set()
        for index, item in enumerate(assignments):
            if not isinstance(item, dict) or set(item) != {"profile_id", "model"}:
                raise LabError("Choose an explicit account and model for each target.")
            profile_id, model = item["profile_id"], item["model"]
            if not isinstance(profile_id, str) or profile_id not in profiles or not isinstance(model, str) or not model:
                raise LabError("Choose a connected account and an explicit model; provider defaults are not reproducible.")
            if (profile_id, model) in seen:
                raise LabError("Choose distinct account/model targets; use Repeats to repeat a target.")
            seen.add((profile_id, model))
            if profile_id not in catalogs:
                catalogs[profile_id] = {m["id"] for m in self.models(profile_id)["models"]}
            if model not in catalogs[profile_id]:
                raise LabError("A selected model is no longer in this account's catalog. Refresh the model selection.")
            profile = profiles[profile_id]
            targets.append(dict(id=f"t{index + 1}", profile_id=profile_id, profile_label=profile["label"],
                                provider=profile["provider"], model=model))
        conditions = dict(protocol=PROTOCOL, pack_id=pack["id"], pack_version=pack["version"], pack_hash=pack["hash"],
                          grading_version=pack.get("grading_version", "1"),
                          grader_hash=hashlib.sha256(Path(_grader_for(pack).__file__).read_bytes()).hexdigest(),
                          provider_adapter_hash=hashlib.sha256(Path(__file__).with_name("providers.py").read_bytes()).hexdigest(),
                          case_ids=[c["id"] for c in selected], mode=mode, repeats=repeats,
                          timeout_seconds=timeout_seconds, context="fresh-per-case", tool_policy="answer-only-tools-forbidden",
                          response_limit_chars=MAX_RESPONSE_CHARS, schedule="rotating-case-order-v1",
                          failure_policy="stop-on-infrastructure-or-three-consecutive-errors-per-target-v2")
        if pack.get("capability") == "typed-choice":
            conditions["test_input_protocol"] = pack["protocol"]
            conditions["reference_quality"] = pack["reference_quality"]
        prepared = dict(pack=deepcopy(pack), cases=deepcopy(selected), targets=targets, conditions=conditions,
                        request_count=request_count, request_limit=request_limit, prepared_at=time.monotonic())
        token = secrets.token_hex(24)
        with self._lock:
            self._plans = {k: v for k, v in self._plans.items() if time.monotonic() - v["prepared_at"] < 900}
            self._plans[token] = prepared
        public_pack = {**_pack_info(pack), "hash": pack["hash"]}
        notice = "Start sends these fixed benchmark cases to the selected accounts. Logical request count and timeout are enforced; a dollar estimate is unavailable. No application-level retries."
        if len({target["provider"] for target in targets}) > 1:
            notice += " These targets use different protocols (Codex CLI and OpenCode stateless). Individual results will be saved, but strict comparison will be unavailable."
        if pack.get("capability") == "typed-choice":
            notice += " This private pack sends the selected test inputs to that provider. Scores measure reference agreement; inspect the declared reference quality."
        return dict(token=token, pack=public_pack, targets=deepcopy(targets), conditions=deepcopy(conditions),
                    request_count=request_count, request_limit=request_limit,
                    notice=notice)

    def _persist(self, directory, report):
        payload = deepcopy(report)
        payload["record_hash"] = _digest(report)
        _write(directory / "report.json", payload)

    def run(self, token, cancel_event=None, progress_callback=None):
        cancel_event = cancel_event or threading.Event()
        with self._lock:
            prepared = self._plans.pop(token, None)
        if prepared is None or time.monotonic() - prepared["prepared_at"] > 900:
            raise LabError("This run plan expired or was already started. Prepare a new run.")
        if hashlib.sha256(Path(_grader_for(prepared["pack"]).__file__).read_bytes()).hexdigest() != prepared["conditions"]["grader_hash"]:
            raise LabError("The grader changed after setup. Prepare a new run.")
        if hashlib.sha256(Path(__file__).with_name("providers.py").read_bytes()).hexdigest() != prepared["conditions"]["provider_adapter_hash"]:
            raise LabError("The provider adapter changed after setup. Prepare a new run.")
        profiles = {p["id"]: p for p in self.profiles()["profiles"]}
        if any(t["profile_id"] not in profiles or profiles[t["profile_id"]]["provider"] != t["provider"] for t in prepared["targets"]):
            raise LabError("A selected account was removed or changed after setup. Prepare a new run.")
        run_id = secrets.token_hex(16)
        directory = self.root / run_id
        directory.mkdir(parents=True, exist_ok=False)
        pack = prepared["pack"]
        report = dict(run_id=run_id, created_at=_now(), pack_id=pack["id"], pack_name=pack["name"],
                      pack_info=_pack_info(pack),
                      pack_version=pack["version"], pack_hash=pack["hash"], pack_snapshot_hash=_digest(pack), mode=prepared["conditions"]["mode"],
                      repeats=prepared["conditions"]["repeats"], conditions=prepared["conditions"],
                      targets=prepared["targets"], request_count=prepared["request_count"],
                      request_limit=prepared["request_limit"], completed=0, status="running", results=[])
        self._active.add(run_id)
        consecutive_errors = {target["id"]: 0 for target in report["targets"]}
        try:
            _write(directory / "pack.json", pack, preserve_order=pack.get("capability") == "typed-choice")
            self._persist(directory, report)
            for repeat in range(report["repeats"]):
                for case_index, case in enumerate(prepared["cases"]):
                    # Alternate first/second target deterministically to reduce order bias.
                    targets = report["targets"]
                    offset = (case_index + repeat) % len(targets)
                    for target in targets[offset:] + targets[:offset]:
                        if cancel_event.is_set():
                            break
                        started = time.monotonic()
                        try:
                            raw = self.runner.run(target["profile_id"], target["model"], packs.public_request(case),
                                                  report["conditions"]["timeout_seconds"], cancel_event)
                        except Exception:
                            raw = dict(status="error", error="The provider request failed.")
                        status = raw.get("status", "error") if isinstance(raw, dict) else "error"
                        status = status if status in ("ok", "error", "cancelled", "timeout") else "error"
                        identity_mismatch = isinstance(raw, dict) and any(
                            raw.get(key) is not None and raw.get(key) != expected
                            for key, expected in (("provider", target["provider"]), ("model", target["model"]), ("observed_model", target["model"])))
                        if identity_mismatch:
                            status = "error"
                        response = raw.get("text", "") if isinstance(raw, dict) else ""
                        if not isinstance(response, str) or len(response) > MAX_RESPONSE_CHARS:
                            status, response = "error", ""
                        grade = dict(score=0, passed=False, details="No valid response.", metrics={})
                        if status == "ok":
                            try:
                                grade = _grader_for(pack).grade(case, response)
                            except Exception:
                                status, grade = "error", dict(score=0, passed=False, details="The response could not be graded.", metrics={})
                        usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
                        usage = usage if isinstance(usage, dict) else {}
                        result = dict(target_id=target["id"], case_id=case["id"], repeat=repeat + 1, status=status,
                                      score=max(0, min(1, _number(grade.get("score")) or 0)), passed=grade.get("passed") is True and status == "ok",
                                      details=_plain(grade.get("details"), 1000), metrics=grade.get("metrics", {}), text=response,
                                      elapsed_ms=_number(raw.get("elapsed_ms")) if isinstance(raw, dict) else None,
                                      usage={key: _number(usage.get(key)) for key in ("input_tokens", "output_tokens", "cost_usd")},
                                      provider=target["provider"], model=target["model"],
                                      observed_model=_plain(raw.get("observed_model"), 200) or None if isinstance(raw, dict) else None,
                                      provider_version=_plain(raw.get("provider_version"), 120) if isinstance(raw, dict) else "",
                                      adapter=_plain(raw.get("adapter"), 120) if isinstance(raw, dict) else "")
                        effective = raw.get("conditions", {}) if isinstance(raw, dict) else {}
                        result["execution_conditions"] = {str(k)[:80]: v for k, v in effective.items()
                            if isinstance(effective, dict) and type(v) in (str, bool, int, float, type(None))
                            and (type(v) is not float or math.isfinite(v))
                            and (not isinstance(v, str) or len(v) <= 500)} if isinstance(effective, dict) else {}
                        codes = raw.get("notice_codes", []) if isinstance(raw, dict) else []
                        result["notices"] = [dict(code=code, message=NOTICE_MESSAGES[code])
                            for code in dict.fromkeys(code for code in codes[:16] if isinstance(code, str) and code in NOTICE_MESSAGES)] if isinstance(codes, list) else []
                        if result["elapsed_ms"] is None:
                            result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
                        if status != "ok":
                            # Raw exceptions/output never become UI diagnostics.
                            code = raw.get("error_code") if isinstance(raw, dict) else None
                            code = code if isinstance(code, str) and code in ERROR_MESSAGES else status if status in ("timeout", "cancelled") else "provider_error"
                            result["error_code"] = code
                            result["error"] = ERROR_MESSAGES[code]
                        if identity_mismatch:
                            result["error_code"] = "model_mismatch"
                            result["error"] = "Provider or model identity changed; the run was stopped."
                        report["results"].append(result)
                        report["completed"] += 1
                        self._persist(directory, report)
                        if progress_callback:
                            try:
                                progress_callback(dict(completed=report["completed"], total=report["request_count"],
                                                       case_id=case["id"], target=target["model"], status=status,
                                                       error=result.get("error"), error_code=result.get("error_code")))
                            except Exception:
                                pass  # Rendering cannot retry or abort a completed paid request.
                        if status == "cancelled":
                            cancel_event.set()
                        # Other targets' successes must not keep a broken model running.
                        target_id = target["id"]
                        if status in ("error", "timeout"):
                            consecutive_errors[target_id] += 1
                        elif status == "ok":
                            consecutive_errors[target_id] = 0
                        if not cancel_event.is_set() and (identity_mismatch
                                or result.get("error_code") in FATAL_PROVIDER_ERRORS or consecutive_errors[target_id] >= 3):
                            report["error_code"] = result.get("error_code", "provider_error")
                            raise LabError("Run stopped: " + result["error"] + " No later cases were sent.")
                    if cancel_event.is_set():
                        break
                if cancel_event.is_set():
                    break
            report["status"] = "cancelled" if cancel_event.is_set() else "completed"
        except LabError as error:
            report["status"] = "failed"
            report["error"] = error.message
        except Exception:
            report["status"] = "failed"
            report["error"] = "Run stopped because its evidence could not be saved. It will not retry automatically."
        finally:
            report["finished_at"] = _now()
            self._active.discard(run_id)
            try:
                self._persist(directory, report)
            except OSError:
                raise LabError("Run evidence could not be saved. Inspect Model Lab history before starting another run.") from None
        return self._summary(report)

    def _rows(self, report):
        rows = []
        total = len(report["conditions"]["case_ids"]) * report["repeats"]
        for target in report["targets"]:
            items = [r for r in report["results"] if r["target_id"] == target["id"]]
            elapsed = [r["elapsed_ms"] for r in items if _number(r.get("elapsed_ms")) is not None]
            answered = [r for r in items if r["status"] == "ok"]
            response_elapsed = [r["elapsed_ms"] for r in answered if _number(r.get("elapsed_ms")) is not None]
            score = round(100 * sum(r["score"] for r in items) / total, 2) if total else 0
            scored = report["status"] == "completed" and len(items) == total and bool(answered)
            def usage(key):
                values = [r.get("usage", {}).get(key) for r in items]
                return sum(values) if values and all(_number(v) is not None for v in values) else None
            rows.append(dict(run_id=report["run_id"], target=target["profile_label"] + " / " + target["model"], target_id=target["id"],
                             provider=target["provider"], model=target["model"], profile_label=target["profile_label"],
                             score=score, benchmark_score=score if scored else None,
                             quality_score=round(100 * math.fsum(r["score"] for r in answered) / len(answered), 2) if answered else None,
                             answered=len(answered), wrong=sum(not r["passed"] for r in answered),
                             execution_errors=sum(r["status"] in ("error", "timeout") for r in items),
                             cancelled=sum(r["status"] == "cancelled" for r in items), unrun=max(total - len(items), 0),
                             coverage=round(100 * len(answered) / total, 2) if total else 0,
                             median_response_ms=statistics.median(response_elapsed) if response_elapsed else None,
                             p95_response_ms=_p95(response_elapsed),
                             passed=sum(r["passed"] for r in items), total=total, completed=len(items),
                             errors=sum(r["status"] != "ok" for r in items),
                             median_ms=statistics.median(elapsed) if elapsed else None, p95_ms=_p95(elapsed),
                             input_tokens=usage("input_tokens"), output_tokens=usage("output_tokens"), cost_usd=usage("cost_usd")))
        return rows

    def _summary(self, report):
        return {**{k: deepcopy(v) for k, v in report.items() if k not in ("results", "record_hash", "cases", "rows", "scoring")},
                "rows": self._rows(report), "scoring": _scoring(report.get("pack_info", {}))}

    def report(self, run_id):
        if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
            raise LabError("Invalid run ID.")
        path = self.root / run_id / "report.json"
        if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise LabError("Invalid run path.")
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_REPORT_BYTES + 1)
            if len(raw) > MAX_REPORT_BYTES:
                raise ValueError()
            report = json.loads(raw)
            actual_hash = report.pop("record_hash")
            if report.get("run_id") != run_id or actual_hash != _digest(report):
                raise ValueError()
            snapshot = path.with_name("pack.json")
            if snapshot.is_symlink() or not snapshot.resolve().is_relative_to(self.root.resolve()):
                raise ValueError()
            with snapshot.open("rb") as stream:
                frozen = stream.read(MAX_REPORT_BYTES + 1)
            if len(frozen) > MAX_REPORT_BYTES:
                raise ValueError()
            pack = json.loads(frozen)
            if (_digest(pack) != report.get("pack_snapshot_hash")
                    or any(pack.get(k) != report.get(field) for k, field in (("id", "pack_id"), ("version", "pack_version"), ("hash", "pack_hash")))):
                raise ValueError()
            if report["status"] == "running" and run_id not in self._active:
                report["status"] = "interrupted"
            info = _pack_info(pack)
            return {**report, "rows": self._rows(report), "pack_info": info, "scoring": _scoring(info),
                    "cases": [{key: deepcopy(case[key]) for key in ("id", "prompt", "expected", "source_id", "topic", "stage", "question_id", "reference_quality", "exclusion_reason") if key in case}
                              for case in pack["cases"]]}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise LabError("Run evidence is missing, changed or invalid; it cannot be compared.") from None

    def history(self):
        if not self.root.exists():
            return []
        summaries = []
        for directory in self.root.iterdir():
            if _ID.fullmatch(directory.name) and directory.is_dir():
                try:
                    summaries.append(self._summary(self.report(directory.name)))
                except LabError:
                    # Keep invalid evidence visible, never silently rank it.
                    summaries.append(dict(run_id=directory.name, created_at="", pack_name="Invalid evidence", status="invalid", rows=[], targets=[]))
        return sorted(summaries, key=lambda r: r.get("created_at", ""), reverse=True)[:50]

    def compare(self, run_ids):
        if not isinstance(run_ids, list) or not 1 <= len(run_ids) <= 8 or len(set(run_ids)) != len(run_ids):
            raise LabError("Choose one to eight distinct runs.")
        reports = [self.report(identifier) for identifier in run_ids]
        if any(r["status"] != "completed" or r["completed"] != r["request_count"] for r in reports):
            return dict(comparable=False, reason="Only fully completed runs can be compared. Missing cases are never dropped.", rows=[])
        expected = _digest(reports[0]["conditions"])
        if any(_digest(r["conditions"]) != expected for r in reports[1:]):
            return dict(comparable=False, reason="Pack version/hash, cases, mode, repeats and execution conditions must match.", rows=[])
        executions = set()
        for report in reports:
            for result in report["results"]:
                if not result.get("adapter") or not result.get("execution_conditions"):
                    return dict(comparable=False, reason="Execution protocol could not be verified for every response. Individual results remain available.", rows=[])
                executions.add(_digest(dict(adapter=result["adapter"], conditions=result["execution_conditions"],
                                            provider_version=result.get("provider_version"))))
        if len(executions) != 1:
            return dict(comparable=False, reason="Execution adapters or versions differ. Compare models using the same protocol; individual results remain available.", rows=[])
        rows = [row for report in reports for row in report["rows"]]
        if any(row["benchmark_score"] is None for row in rows):
            return dict(comparable=False, reason="No scored response is available for one or more targets. These targets cannot be ranked.", rows=[])
        # Rank full-precision credit rather than the rounded score shown in the
        # table. Times and cost deliberately cannot break a benchmark-score tie.
        ranks = {}
        for report in reports:
            total = len(report["conditions"]["case_ids"]) * report["repeats"]
            for target in report["targets"]:
                ranks[(report["run_id"], target["id"])] = math.fsum(
                    item["score"] for item in report["results"] if item["target_id"] == target["id"]) / total
        highest = max(ranks.values())
        winners = [row for row in rows if ranks[(row["run_id"], row["target_id"])] == highest]
        scope = "this run" if len(reports) == 1 else "this comparison"
        names = "; ".join(row["target"] for row in winners)
        tied = len(winners) > 1
        text = f"Highest score in {scope}: {names}" + (" (tie)." if tied else ".")
        if not tied and any(row["benchmark_score"] == winners[0]["benchmark_score"] and row not in winners for row in rows):
            text += " Displayed scores round to the same value; unrounded case credit separates them."
        verdict = dict(kind="tie" if tied else "winner", text=text,
                       targets=[{key: row[key] for key in ("run_id", "target_id", "profile_label", "model", "benchmark_score")} for row in winners])
        if len(rows) == 1:
            verdict.update(kind="single", text="One target scored; add a matching run to compare models.")
        return dict(comparable=True, reason="Same frozen pack and conditions. CLI adapter/version details remain in each run; latency includes adapter startup.",
                    rows=rows, conditions=reports[0]["conditions"], verdict=verdict, scoring=_scoring(reports[0].get("pack_info", {})))
