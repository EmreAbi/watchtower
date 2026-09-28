"""Versioned team templates: portable data only, with no account or runtime I/O."""
from __future__ import annotations

from copy import deepcopy


CATALOG_VERSION = 1

_TEMPLATES = (
    {
        "id": "quick-task", "version": 1, "name": "Quick Task",
        "description": "One assistant for a standalone answer, image, translation or small output.",
        "lead": "assistant", "independent_review": False,
        "roles": [
            {"id": "assistant", "name": "Assistant", "kind": "controller", "icon": "👤",
             "tab": "Assistant", "cwd_target": "project",
             "scope": "Complete the user's routine standalone request directly. Keep preparation and reporting brief. "
                      "Create the requested output without automatic delegation or mandatory independent review."},
        ],
        "acceptance": ["The requested answer or output is delivered.",
                       "Check the output proportionally and disclose material limitations."],
    },
    {
        "id": "research", "version": 1, "name": "Research",
        "description": "A researcher and independent reviewer for evidence-based findings.",
        "lead": "researcher", "independent_review": True,
        "roles": [
            {"id": "researcher", "name": "Researcher", "kind": "controller", "icon": "👤",
             "tab": "Research", "cwd_target": "work",
             "scope": "Own the research question, source selection and concise findings. Distinguish source facts, "
                      "inferences and unresolved questions. Produce a source-linked report and supporting evidence "
                      "in this team's output directories. Do not edit project files."},
            {"id": "reviewer", "name": "Reviewer", "kind": "reviewer", "icon": "🔎",
             "tab": "Review", "cwd_target": "metadata",
             "scope": "Independently check the assigned report's claims, sources, coverage and limitations. "
                      "Verify the exact submitted evidence. Do not rewrite the researcher's report or edit project "
                      "files; record findings separately in this team's review/report directories."},
        ],
        "acceptance": ["Claims have source links and distinguish evidence from inference.",
                       "The report answers the agreed question and identifies material uncertainty.",
                       "An independent reviewer checks the submitted report and evidence."],
    },
    {
        "id": "development", "version": 1, "name": "Development",
        "description": "A controller, implementation worker and independent reviewer for code changes.",
        "lead": "lead", "independent_review": True,
        "roles": [
            {"id": "lead", "name": "Controller", "kind": "controller", "icon": "👤",
             "tab": "Coordination", "cwd_target": "metadata",
             "scope": "Own the user task, acceptance criteria, scope and completion evidence. Assign implementation "
                      "to the worker and coordinate independent review. Keep a single owner for each changed file, "
                      "build and running service. Do not edit project files yourself."},
            {"id": "worker", "name": "Worker", "kind": "worker", "icon": "⚙",
             "tab": "Implementation", "cwd_target": "project",
             "scope": "Implement the specifically assigned code change in the selected project. Preserve unrelated "
                      "work, run relevant checks and submit a stable change with evidence. You are the only template "
                      "role assigned to write project implementation files; do not expand the user task."},
            {"id": "reviewer", "name": "Reviewer", "kind": "reviewer", "icon": "🔎",
             "tab": "Review", "cwd_target": "metadata",
             "scope": "Independently inspect the exact submitted change, acceptance criteria and relevant test "
                      "evidence. Do not edit implementation files or approve your own contribution. Write findings "
                      "only in this team's review/report directories and request targeted fixes when needed."},
        ],
        "acceptance": ["The change satisfies the user's acceptance criteria within the agreed scope.",
                       "Relevant checks pass, or their concrete limits are reported.",
                       "An independent reviewer checks the exact submitted change and evidence."],
    },
)


def list_templates():
    """Return detached copies; UI annotations cannot mutate built-in policy."""
    return deepcopy(list(_TEMPLATES))


def get_template(template_id):
    for template in _TEMPLATES:
        if template["id"] == template_id:
            return deepcopy(template)
    raise ValueError("Choose an available team template.")
