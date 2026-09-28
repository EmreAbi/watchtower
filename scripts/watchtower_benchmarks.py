"""Allowlist Model Lab code and fixed benchmark cases; never ship run/auth data."""
from pathlib import Path

RUNTIME_FILES = {"packs.py", "grading.py", "answer_grading.py", "private_packs.py", "choice_grading.py", "providers.py", "service.py", "view.py", "appearance.py", "launcher.py",
                 "herdr-plugin.toml", "bin/center.cmd", "README.md",
                 "packs/structured-decisions-v1.json", "packs/coding-v1.json",
                 "packs/code-review-v1.json", "packs/debugging-v1.json",
                 "packs/gsm8k-subset-v1.json", "packs/bbh-subset-v1.json", "packs/bbeh-subset-v1.json",
                 "licenses/gsm8k-MIT.txt", "licenses/bbh-MIT.txt", "licenses/bbeh-NOTICE.txt", "BENCHMARKS.md"}
DEVELOPMENT_FILES = {"test_packs.py", "test_grading.py", "test_answer_grading.py", "test_private_packs.py", "test_choice_grading.py", "test_providers.py", "test_service.py", "test_private_service.py", "test_published_service.py", "test_view.py", "test_appearance.py"}


def validated_benchmark_files(root):
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if not RUNTIME_FILES <= actual or actual - RUNTIME_FILES - DEVELOPMENT_FILES:
        raise ValueError("Unexpected or missing files in the Model Lab package")
    result = []
    for name in sorted(RUNTIME_FILES):
        path = root / name
        if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (path, *path.parents)):
            raise ValueError("Model Lab package cannot contain links")
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Model Lab file escaped package root")
        result.append(path)
    return result
