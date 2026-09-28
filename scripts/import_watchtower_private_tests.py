"""Import declared canonical test packs into a private Watchtower home.

The source manifest pins explicit local pack files by SHA-256. No source code,
inference, authentication or historical provider results are executed/imported.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PureWindowsPath
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "distribution/watchtower/benchmarks" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


private_packs = _load_module("watchtower_private_packs", "private_packs.py")
bundled_packs = _load_module("watchtower_bundled_packs", "packs.py")
IMPORT_SCHEMA = "watchtower-private-import/1"
MAX_IMPORT_PACKS = 32


def _validate_import_pack(pack):
    if not isinstance(pack, dict) or "hash" in pack:
        raise ValueError("Private import requires a canonical pack definition.")
    private_packs.validate_snapshot({**pack, "hash": private_packs.canonical_hash(pack)})
    if pack["id"] in bundled_packs.MANIFEST:
        raise ValueError("Private pack ID is reserved by a bundled test pack; choose a unique private ID.")


def read_packs(source):
    """Read only explicitly listed, contained files; never follow provenance paths."""
    source = private_packs.checked_path(source)
    manifest = private_packs.parse_json(private_packs._read(source, private_packs.MAX_MANIFEST_BYTES, source.parent))
    if (not isinstance(manifest, dict) or set(manifest) != {"schema", "packs"}
            or manifest["schema"] != IMPORT_SCHEMA or not isinstance(manifest["packs"], list)
            or not 1 <= len(manifest["packs"]) <= MAX_IMPORT_PACKS):
        raise ValueError("Private import manifest is invalid.")
    result, identities = [], set()
    for entry in manifest["packs"]:
        if (not isinstance(entry, dict) or set(entry) != {"path", "sha256"}
                or not isinstance(entry["path"], str) or not entry["path"] or "\\" in entry["path"]
                or Path(entry["path"]).is_absolute() or PureWindowsPath(entry["path"]).drive
                or any(part in ("", ".", "..") for part in entry["path"].split("/"))
                or not isinstance(entry["sha256"], str) or not private_packs._SHA.fullmatch(entry["sha256"])):
            raise ValueError("Private import file declaration is invalid.")
        path = private_packs.checked_path(source.parent / entry["path"], root=source.parent)
        raw = private_packs._read(path, private_packs.MAX_PACK_BYTES, source.parent)
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError("Private import SHA-256 mismatch.")
        pack = private_packs.parse_json(raw)
        # Canonical source files omit the runtime snapshot hash.
        _validate_import_pack(pack)
        if pack["id"] in identities:
            raise ValueError("Duplicate private pack in import manifest.")
        identities.add(pack["id"])
        result.append(pack)
    return result


def pack_bytes_and_manifest(pack):
    _validate_import_pack(pack)
    raw = (json.dumps(pack, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if len(raw) > private_packs.MAX_PACK_BYTES:
        raise ValueError("Private pack exceeds the local library file limit.")
    manifest = {"schema": private_packs.SCHEMA, "id": pack["id"], "version": 1, "protocol": pack["protocol"],
                "pack_hash": private_packs.canonical_hash(pack), "files": {"pack.json": hashlib.sha256(raw).hexdigest()},
                "source_hashes": {item["key"]: item["sha256"] for item in pack["source"]["files"]}}
    return raw, (json.dumps(manifest, indent=2) + "\n").encode("utf-8")


def install_packs(home, packs):
    home = private_packs.checked_path(home)
    root = home / "state" / "benchmarks" / "packs"
    if root.resolve().is_relative_to(REPO_ROOT.resolve()):
        raise ValueError("Private test payloads must not be installed inside the Watchtower repository.")
    encoded = [(pack, *pack_bytes_and_manifest(pack)) for pack in packs]
    if len({pack["id"] for pack, _, _ in encoded}) != len(encoded):
        raise ValueError("Duplicate private pack installation request.")
    private_packs.checked_path(root)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".import.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write("Private pack import in progress\n")
    try:
        # Check every existing version before writing any pack.
        for pack, _raw, _manifest in encoded:
            directory = root / pack["id"] / "v1"
            private_packs.checked_path(directory, root=root)
            if directory.exists() and private_packs.load_pack(root, pack["id"])["hash"] != private_packs.canonical_hash(pack):
                raise ValueError("Existing private v1 differs; changed packs require a new reviewed version.")
        for pack, raw, manifest in encoded:
            parent = root / pack["id"]
            final = parent / "v1"
            if final.exists():
                continue
            parent.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".pending-", dir=parent) as staging:
                staged = private_packs.checked_path(staging, root=root)
                (staged / "pack.json").write_bytes(raw)
                (staged / "manifest.json").write_bytes(manifest)
                if final.exists():
                    raise ValueError("Private version appeared during import; nothing will be overwritten.")
                staged.rename(final)
        return [private_packs.pack_info(private_packs.load_pack(root, pack["id"])) for pack, _, _ in encoded]
    finally:
        lock.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Absolute path to the local import manifest")
    parser.add_argument("--home", type=Path)
    parser.add_argument("--check", action="store_true", help="Validate declared pack files; do not install")
    args = parser.parse_args(argv)
    if not args.check and args.home is None:
        parser.error("--home is required for installation")
    packs = read_packs(args.source)
    if not args.check:
        install_packs(args.home, packs)
    print(json.dumps({"installed": not args.check, "packs": [{"id": pack["id"], "scored": len(pack["cases"]),
                      "unscored": len(pack["unscored_cases"]), "hash": private_packs.canonical_hash(pack)} for pack in packs]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
