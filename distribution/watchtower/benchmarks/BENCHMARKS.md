# Published benchmarks and evaluation roadmap

Research and implementation reviewed on 2026-09-28. Model Lab now contains
200 fixed cases: 80 original Watchtower cases and 120 questions selected from
published datasets. The new packs are local, reproducible subsets, not official
full-benchmark runs or leaderboard submissions.

## Available published subsets

| Pack | Full / Quick | Coverage | Upstream source and license |
| --- | --- | --- | --- |
| GSM8K subset v1 | 40 / 5 | Multistep numerical word problems | [OpenAI GSM8K](https://github.com/openai/grade-school-math), MIT |
| BIG-Bench Hard subset v1 | 40 / 5 | Logical deduction, shuffled objects, arithmetic, Dyck languages, formal fallacies | [BBH](https://github.com/suzgunmirac/BIG-Bench-Hard), MIT |
| BIG-Bench Extra Hard subset v1 | 40 / 5 | Boolean expressions, arithmetic, object properties, temporal sequence, zebra puzzles | [Google DeepMind BBEH](https://github.com/google-deepmind/bbeh), dataset CC-BY-4.0 |

BBEH was introduced to address saturation of BBH; this makes it a useful harder
complement, not a guarantee that a small selection separates every model.
Older public benchmarks may already occur in training data. Quick's five
questions are a screening pass, not evidence of a universal model ranking.

### Frozen sources and sampling

| Dataset | Pinned upstream Git revision | Candidate population |
| --- | --- | ---: |
| GSM8K | `3101c7d5072418e28b9008a6636bde82a006892c` | 1,319 test questions |
| BBH | `9ee07bd481feebf959a6b59d61ea57bdcf30964d` | 1,250 questions across the five selected topics |
| BBEH | `80d12ca916b7158f22293fcf3144f4d3d854d4be` | 1,000 questions across the five selected topics |

Source indices are ranked by a fixed SHA-256 selection key. GSM8K takes 40;
BBH and BBEH take eight per topic, interleaved so Quick covers all five topics.
Selection does not use model performance. These are not the complete BBH/BBEH
suites and are not BBEH Mini. The pack records the exact selection rule,
original file/index, source revision and each downloaded file's SHA-256.

The repository importers `scripts/build_watchtower_published_packs.py` and
`scripts/build_watchtower_bbeh_pack.py` reproduce the fixtures from verified
source bytes. Downloading is explicit; normal benchmark runs use bundled data
and never fetch changing upstream questions. License texts and BBEH attribution
are included under `licenses/`. Original question text and answer semantics
are retained; the answer instructions and evaluation protocol are adapted.

### What the score means

Every request asks for exactly `{"answer":"..."}` with no examples or extra
fields. References are never sent. This differs from original few-shot,
chain-of-thought or answer-extraction protocols. A correct answer earns one
point; an incorrect or malformed answer earns zero:

* GSM8K: numeric equality, allowing valid thousands separators and equivalent
  decimals. Units, expressions and scientific notation are rejected.
* BBH: exact final-answer text after trimming outer whitespace; case and
  internal spacing matter. The prompt declares this rule.
* BBEH: whole-answer equality after trimming, case folding and comma-spacing
  normalization. The upstream evaluator's broader fuzzy matching is not used.

Accuracy is correct answers divided by all planned attempts in a completed run.
Execution errors count as zero; coverage and answer quality are shown separately.
Incomplete runs cannot be ranked. Timeout, adapter, model, pack and grader
identity remain part of strict comparison. Costs and speed do not change accuracy.
Each published pack has 40 Full questions; two models therefore require 80
requests. The user must explicitly raise the default request limit of 10.

## Next: faithful coding and agent evaluation

| Benchmark | What it adds | Integration requirement |
| --- | --- | --- |
| [HumanEval](https://github.com/openai/human-eval) / [EvalPlus](https://github.com/evalplus/evalplus) | Function correctness with executable hidden tests; EvalPlus strengthens the tests | Separate isolated Python execution and faithful whole-task pass@1 scoring; preserve underlying dataset licenses |
| [MBPP](https://github.com/google-research/google-research/blob/master/mbpp/README.md) | Another established Python programming task family | Same isolated runner, dataset attribution and documented prompt protocol |
| [SWE-bench](https://github.com/SWE-bench/SWE-bench) | Fixing real repository issues against tests | Per-task repository/environment images, agent tools and substantially larger runtime/storage budgets |
| [LiveCodeBench](https://github.com/LiveCodeBench/LiveCodeBench) | Versioned, date-aware programming evaluations | Pin a release window, resolve dataset redistribution terms and use an isolated execution runner |
| [Terminal-Bench](https://www.tbench.ai/news/terminal-bench-4-0) | Agents performing real terminal tasks | Harbor task environments, tool access, separate long-running job and scoring lifecycle |

These benchmarks were researched but are not selectable packs yet. The current
bounded AST coding grader does not reproduce them. The next implementation
step is an isolated execution runner with a fixed HumanEval+ selection, followed
by repository/terminal evaluations as a separate agent category. Docker's local
CLI is installed, but its engine was unavailable during this inspection; no
engine, container image or paid benchmark run was started.
