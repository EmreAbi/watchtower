# Model Lab

Open **menu → model lab** to test an explicit model on fixed, versioned cases.
**Single model test** is the default. Choose **Compare two models** only when
you want both to run together. Select a pack and account/model, review the request count,
then press **Start**. Opening the page or reviewing setup never runs inference.
Connected Codex and OpenCode accounts come from Accounts; no credentials are
copied into benchmark records.

Open **Tests** to browse a pack without running it or connecting an account.
Select a case and press **View test** to inspect its full prompt, response
format, reference answer, reference quality and scoring explanation. Quick
membership and inspection-only records are marked in the list. Browsing does
not prepare or send any model requests.

## Fixed packs

| Pack v1 | Full | Quick | What it measures |
| --- | ---: | ---: | --- |
| Structured Decisions | 20 | 5 | Rule following and exact structured output |
| Coding | 20 | 5 | Small pure Python functions against hidden examples |
| Code Review | 20 | 5 | Seeded defects and false alarms, including clean controls |
| Debugging | 20 | 5 | Repairs of small faulty Python functions |
| GSM8K subset | 40 | 5 | Published numerical word problems |
| BIG-Bench Hard subset | 40 | 5 | Published reasoning questions across five topics |
| BIG-Bench Extra Hard subset | 40 | 5 | More demanding reasoning across five topics |

The first four packs contain 80 original synthetic fixtures. The three published
subsets add 120 questions from pinned, attributed upstream datasets: 200 total.
They use an adapted zero-shot JSON answer protocol, not an official full-suite
leaderboard protocol. Their source, license, selection and limitations appear
in setup/results; see [published benchmarks](BENCHMARKS.md) for details.
Quick always uses the same five case IDs in each pack.
Pack content and grader hashes are recorded. Changing cases requires a new
pack version and manifest hash. Expected answers and hidden examples are never
included in provider requests.

## Private local tests

Import your own frozen typed-choice tests into
`WATCHTOWER_HOME/state/benchmarks/packs`. Pack identities, source declarations,
reference quality and case counts come from your local definitions; no company
fixtures, private paths or source hashes are bundled with Watchtower.

Each case preserves its state, question instructions and exact choice criteria.
The fixed single-question JSON envelope keeps distinct options such as `none`,
`unknown` and `uncertain` separate. **Reference agreement** measures agreement
with the declared frozen labels; inspect their quality and limitations before
interpreting a score. Unscored records remain inspectable with exclusion reasons
and are never silently assigned a scored reference. Historical results produced
with different protocols are not imported as new Model Lab runs.

Prepare canonical pack JSON files outside this repository, then create a local
import manifest. Paths are relative to that manifest and must remain within its
directory; each SHA-256 pins the exact corresponding pack file bytes:

```json
{
  "schema": "watchtower-private-import/1",
  "packs": [
    {"path": "private-example.json", "sha256": "<64-character SHA-256>"}
  ]
}
```

The canonical pack schema is enforced by `private_packs.py`; the synthetic
`test_private_packs.make_pack()` fixture demonstrates its fields without private
data. A pack has `version: 1`, `capability: "typed-choice"`, protocol
`private-typed-choice-json-v1`, declared source hashes and counts, scored and
inspection-only cases, and the first five scored case IDs as its Quick subset
(or all scored cases when fewer than five exist). Source paths inside pack
provenance are metadata only; the importer never opens or executes them. The
import manifest verifies the canonical pack files, not the original source
corpus or the correctness of its labels. Source review remains your responsibility.

From this checkout:

```powershell
python -B scripts/import_watchtower_private_tests.py --source <absolute-import.json> --check
python -B scripts/import_watchtower_private_tests.py --source <absolute-import.json> --home <absolute-WATCHTOWER_HOME>
```

Use unique private pack IDs, preferably beginning with `private-`. Existing
local v1 snapshots retain their original protocol and hash and remain readable.
An identical reimport is a no-op; conflicting v1 content is rejected. Importing
validates bounded files and writes an immutable local pack/manifest pair; it
never connects to a provider. Only the reviewed **Start** action sends selected
private inputs to the chosen provider. The 120-request cap remains unchanged.

Coding and Debugging use a bounded AST interpreter, not `eval`, `exec`, a shell,
or unrestricted execution of generated Python. The prompt specifies the
supported pure-function subset. This first release does not assess repository
edits, browser work, arbitrary Python, or multi-agent orchestration.

## Conditions and scoring

Each case starts fresh, with no resumed conversation. Set 1–3 repeats, a
30–180 second per-request timeout, and a 1–120 request limit. The request count
must fit the limit before Start. Requests run sequentially, alternating the
first target per case/repeat. There are no automatic application-level retries
or model fallbacks. Provider internals may retry transport requests.
Configuration, authorization, unavailable-model and protocol errors stop the
run immediately and show their reason. Three consecutive other provider errors
or timeouts for the same target also stop it; another model's successes do not
reset that target's error count. Incorrect model answers still receive their score and
do not stop later cases. Cancelled and failed runs remain visible in History.

The request limit is not a dollar spending cap. The UI cannot provide an exact
cost estimate. Unknown token usage or cost stays unavailable, never zero.
Time includes provider startup and inference. Errors remain in the planned
score denominator; partial/cancelled runs cannot enter strict comparison.

**View results** opens a readable summary, with individual cases and technical
details available separately. Scores are calculated from the saved evidence;
opening results never repeats a model request or changes an earlier record.

## Appearance

Open **Themes** (or press **F6**) to see color samples beside each theme name. Themes affect
the form, result dialogs and score details, including light backgrounds.
Passed/correct counts use the success color, incorrect answers use the warning
color and execution errors use the error color. Labels and numbers remain
visible, so results do not depend on color alone. Scores and grading are unchanged.

Model Lab enables truecolor only for its own native popup process. An inherited
`NO_COLOR` flag from the launching shell no longer makes every theme grayscale;
the parent shell and agent environments are not modified.

## Score reference

| Measure | Meaning |
| --- | --- |
| Score / 100 | For a completed run, 100 × earned case credit ÷ planned case attempts. Each case/repeat has equal weight; execution errors earn no credit. |
| Answer quality / 100 | Average credit for responses actually received. Use alongside coverage: one correct answer is not equivalent to completing the pack. |
| Passed | Cases receiving full credit; a partly correct answer can earn points without passing. |
| Coverage | Responses received ÷ planned case attempts. Wrong answers count as received; errors, timeouts, cancellation and unrun cases do not. |
| Response time | Median and p95 for received responses, including adapter startup. Error durations cannot make a model look faster. |
| Usage / cost | Provider-reported values only. Unknown values stay unavailable; neither cost nor speed changes the quality score. |

Incomplete runs and targets with no received responses display **Not scored**.
A received but completely wrong answer can legitimately score zero. Strict
comparison names the highest score or a tie only after checking the same frozen
cases and execution conditions; it does not claim a universally best model.

The four packs award case credit differently, using fixed deterministic graders:

| Pack | Case credit |
| --- | --- |
| Structured Decisions | Correct, exactly typed fields ÷ three required fields; invalid JSON or missing/extra fields earn zero. |
| Coding / Debugging | Hidden examples passed ÷ examples in that case, within the documented pure-Python subset. |
| Code Review | Precision/recall F1 against seeded line/category findings; duplicate and invented findings reduce precision. Clean cases earn full credit only with no findings. |

The three published packs instead show **Accuracy / 100**: correct answers
divided by planned attempts, with no partial credit. They require one JSON
string field, `answer`. GSM8K compares numeric values; BBH compares exact text
after outer-whitespace trimming; BBEH additionally ignores case and spacing
around commas. Errors earn zero in completed runs. These rules and wrapper
instructions are part of each frozen pack, and use a separate versioned grader.
The original four packs and their grader remain unchanged.

Full published subsets require 40 requests per model: 80 for two models and
one repeat. Increase the request limit explicitly before Start. Two models and
two repeats require 160 requests, beyond the current per-run cap of 120.

Single-model runs can be compared later in History/Compare when their frozen
pack and execution conditions match. There is no need to rerun the first model
just to test a second one. Quick uses up to five requests per target, bounded by the pack’s eligible case count.

For example, credits of 1, 1, 0.5 and 0 across four planned attempts produce
**62.5 / 100**, with two fully passed cases. Small Quick runs are a smoke test,
not statistical proof that one model is best. Scores apply to the selected pack;
different packs are not combined into a single global model ranking.

**OpenCode** uses its native stateless generation endpoint with the chosen model,
neutral configuration and selected account. **Codex** uses a fresh ephemeral CLI
agent with user configuration/rules ignored, environment tools disabled, and a
read-only sandbox; any tool event invalidates the response. These are different
execution protocols. Individual results can be viewed together, but strict
comparison requires matching adapters, CLI versions, pack/grader hashes and
conditions. It does not present cross-protocol results as a raw-model ranking.

Model IDs are explicitly requested and recorded; an actual model ID is only
reported when the provider exposes it. Availability in a catalog does not
guarantee account entitlement. If access is refused, the error is retained.
OpenCode's catalog and stateless generation must use the same neutral
configuration location. Some free models reject this stateless route even when
listed in OpenCode; Model Lab shows that restriction and stops immediately.
It does not substitute a model or spoof a normal OpenCode session.

## Evidence and cancellation

History is local under `WATCHTOWER_HOME/state/benchmarks/runs/<run-id>`. Each run
contains a frozen pack and a hash-checked report with per-case responses,
grades, status, timings and available usage. Editing or corrupting a report
makes it invalid for comparison. This hash detects changes; it is not a signed
audit trail. Run data is never bundled into the portable distribution.

Cancel stops the owned provider process and prevents later cases from starting.
Closing through the UI waits for that cleanup. A request already accepted by a
remote provider may still incur usage. Interrupted evidence remains visible;
it is never silently presented as a completed result.

## Local validation

Use the Accounts Python runtime (Textual is required for UI tests):

```powershell
python -B -m unittest discover -s distribution/watchtower/benchmarks -p "test_*.py"
python -B -m unittest scripts.test_watchtower_benchmarks
python -B -m unittest scripts.test_watchtower_private_import
```

Tests use synthetic accounts and fake inference. Real paid runs start only from
the user's reviewed Start action.
