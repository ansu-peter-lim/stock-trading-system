"""Offline AST inventory for the codebase token-efficiency audit.

This tool reads repository source, test, and research-document files.  It does
not import project modules, call a network API, or alter production/research
behavior.  Its output is a descriptive input to a human refactoring decision.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SOURCE_ROOT = Path("src")
TEST_ROOT = Path("tests")
RESEARCH_DOC_ROOT = Path("docs/research")
DEFAULT_OUTPUT = Path(
    "data/processed/strategy_review/codebase_token_efficiency_audit_v0_1.json"
)
VERSION_RE = re.compile(r"(?:^|[_-])v(\d+(?:[_-]\d+)*)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FunctionRecord:
    path: str
    qualified_name: str
    name: str
    line: int
    end_line: int
    structural_hash: str


class _StructureNormalizer(ast.NodeTransformer):
    """Keep control structure while removing local identifier spelling."""

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        node = self.generic_visit(node)
        node.name = "FUNCTION"
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_arg(self, node: ast.arg) -> ast.AST:
        node.arg = "ARG"
        return node

    def visit_Name(self, node: ast.Name) -> ast.AST:
        node.id = "NAME"
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        node = self.generic_visit(node)
        node.attr = "ATTR"
        return node


def _module_name(path: Path) -> str:
    return ".".join(path.with_suffix("").parts)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _classification(path: Path) -> str:
    normalized = path.as_posix()
    name = path.name
    if "daily_ma_unified" in name and normalized.startswith("tests/"):
        return "ACTIVE_PRIMARY_TEST"
    if (
        "daily_ma_unified" in name
        or path.name == "algorithm_research_checkpoint_before_daily_unification.md"
    ):
        return "ACTIVE_PRIMARY"
    if normalized.startswith("src/backtest_engine/") or normalized in {
        "src/strategy_review/chart.py",
        "src/kiwoom_daily/models.py",
        "src/kiwoom_daily/parser.py",
        "src/kiwoom_daily/store.py",
        "src/kiwoom_daily/adapter.py",
        "src/kiwoom_daily/collector.py",
    }:
        return (
            "ACTIVE_SUPPORT"
            if normalized == "src/strategy_review/chart.py"
            else "PRODUCTION_FOUNDATION"
        )
    if normalized.startswith("docs/research/"):
        return "HISTORICAL_ARCHIVE"
    if any(
        token in name
        for token in (
            "market_bar",
            "market_time",
            "market_clock",
            "down_box",
            "upper_transition",
            "market_speed",
        )
    ):
        return "FROZEN_RESEARCH"
    if normalized.startswith(("src/strategy_review/", "src/kiwoom_minute/")):
        return "FROZEN_RESEARCH"
    if normalized.startswith("tests/"):
        return "FROZEN_RESEARCH"
    return "LEGACY_OR_UNUSED_CANDIDATE"


def _risk(classification: str) -> str:
    return {
        "ACTIVE_PRIMARY": "LOW",
        "ACTIVE_PRIMARY_TEST": "LOW",
        "ACTIVE_SUPPORT": "MEDIUM",
        "PRODUCTION_FOUNDATION": "MEDIUM",
        "FROZEN_RESEARCH": "HIGH",
        "HISTORICAL_ARCHIVE": "HIGH",
        "LEGACY_OR_UNUSED_CANDIDATE": "MEDIUM",
    }[classification]


def _imports(tree: ast.AST) -> tuple[str, ...]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level + (node.module or "")
            imports.add(prefix)
    return tuple(sorted(imports))


def _functions(path: Path, tree: ast.AST) -> list[FunctionRecord]:
    records: list[FunctionRecord] = []
    parent_stack: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            parent_stack.append(node.name)
            self.generic_visit(node)
            parent_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            clone = _StructureNormalizer().visit(
                ast.fix_missing_locations(ast.parse(ast.unparse(node)))
            )
            encoded = ast.dump(clone, annotate_fields=False, include_attributes=False)
            records.append(
                FunctionRecord(
                    path=path.as_posix(),
                    qualified_name=".".join([*parent_stack, node.name]),
                    name=node.name,
                    line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    structural_hash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                )
            )
            parent_stack.append(node.name)
            self.generic_visit(node)
            parent_stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

    Visitor().visit(tree)
    return records


def _constants(tree: ast.AST) -> dict[str, str]:
    output: dict[str, str] = {}
    for node in tree.body if isinstance(tree, ast.Module) else ():
        targets: list[ast.Name] = []
        if isinstance(node, ast.Assign):
            targets = [
                target for target in node.targets if isinstance(target, ast.Name)
            ]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets, value = [node.target], node.value
        else:
            continue
        if value is None:
            continue
        for target in targets:
            if target.id.isupper():
                output[target.id] = ast.dump(
                    value, annotate_fields=False, include_attributes=False
                )
    return output


def _inventory_file(
    path: Path, kind: str
) -> tuple[dict[str, Any], list[FunctionRecord], dict[str, str]]:
    text = _read(path)
    line_count = len(text.splitlines())
    record: dict[str, Any] = {
        "path": path.as_posix(),
        "kind": kind,
        "line_count": line_count,
        "approx_size_bytes": path.stat().st_size,
        "classification": _classification(path),
        "refactor_risk": _risk(_classification(path)),
        "research_version": (
            VERSION_RE.search(path.stem).group(1)
            if VERSION_RE.search(path.stem)
            else None
        ),
        "active_relevance": _classification(path)
        in {"ACTIVE_PRIMARY", "ACTIVE_PRIMARY_TEST", "ACTIVE_SUPPORT"},
        "direct_imports": [],
        "imported_by": [],
        "test_counterpart": [],
        "token_burden_estimate": "HIGH"
        if line_count >= 800
        else "MEDIUM"
        if line_count >= 300
        else "LOW",
    }
    if kind != "python":
        return record, [], {}
    tree = ast.parse(text, filename=path.as_posix())
    record["direct_imports"] = list(_imports(tree))
    return record, _functions(path, tree), _constants(tree)


def _counterpart(source_path: str, test_paths: set[str]) -> list[str]:
    stem = Path(source_path).stem
    return sorted(path for path in test_paths if stem in Path(path).stem)


def _duplicate_classification(
    records: list[FunctionRecord], classes: Mapping[str, str]
) -> str:
    values = {classes[record.path] for record in records}
    if values <= {"FROZEN_RESEARCH", "HISTORICAL_ARCHIVE"}:
        return "FROZEN_DUPLICATION_ACCEPTED"
    if "ACTIVE_PRIMARY" in values or "ACTIVE_SUPPORT" in values:
        return "SHARE_FOR_NEW_CODE_ONLY"
    if "PRODUCTION_FOUNDATION" in values:
        return "SAFE_TO_SHARE"
    return "DO_NOT_TOUCH"


def _active_v0_1_case(
    files: Sequence[Mapping[str, Any]], functions: Sequence[FunctionRecord]
) -> dict[str, Any]:
    """Describe the active V0.1 file without importing or modifying it."""

    active_path = "src/kiwoom_daily/daily_ma_unified_buy_visual_proof_v0_1.py"
    by_path = {str(record["path"]): record for record in files}
    module_by_path = {
        _module_name(Path(path)): path
        for path in by_path
        if path.startswith(("src/", "tests/"))
    }

    def local_imports(path: str) -> set[str]:
        record = by_path[path]
        base = _module_name(Path(path)).rsplit(".", 1)[0]
        resolved: set[str] = set()
        for imported in record["direct_imports"]:
            target = str(imported)
            if target.startswith("."):
                target = f"{base}.{target.lstrip('.')}"
            if target in module_by_path:
                resolved.add(module_by_path[target])
        return resolved

    reachable: set[str] = {active_path}
    frontier = [active_path]
    while frontier:
        current = frontier.pop()
        for target in local_imports(current):
            if target not in reachable:
                reachable.add(target)
                frontier.append(target)
    names = {
        "DATA_LOADING": {"generate_visual_proof"},
        "DAILY_FEATURE_CALCULATION": {
            "_slope",
            "slope_state",
            "confirmed_breakout",
            "confirmed_breakdown",
            "upward_inflection",
            "consecutive_below_runs",
            "calculate_daily_ma_points",
        },
        "BUY_SIGNAL_STATE_MACHINE": {
            "_last_inflection_within_run",
            "detect_buy_candidates",
            "_candidate",
        },
        "VISUAL_SAMPLE_SELECTION": {
            "select_signal_windows",
            "_candidate_label",
            "_events_for_window",
        },
        "METADATA_SERIALIZATION": {
            "_summary",
            "_json_default",
            "assert_no_future_outcome_fields",
        },
        "RUN_ORCHESTRATION": {"_in_research_period", "generate_visual_proof", "main"},
    }
    buckets: dict[str, list[FunctionRecord]] = defaultdict(list)
    for function in functions:
        if function.path == active_path:
            for label, function_names in names.items():
                if function.name in function_names:
                    buckets[label].append(function)
                    break
    return {
        "active_path": active_path,
        "read_set": [
            {
                "path": path,
                "line_count": by_path[path]["line_count"],
                "classification": by_path[path]["classification"],
            }
            for path in sorted(reachable)
        ],
        "read_set_file_count": len(reachable),
        "read_set_line_count": sum(
            int(by_path[path]["line_count"]) for path in reachable
        ),
        "responsibilities": {
            label: {
                "function_count": len(records),
                "approx_function_lines": sum(
                    record.end_line - record.line + 1 for record in records
                ),
                "functions": [record.name for record in records],
            }
            for label, records in sorted(buckets.items())
        },
    }


def scan_repository(root: Path) -> dict[str, Any]:
    root = root.resolve()
    source_paths = sorted(root.joinpath(SOURCE_ROOT).rglob("*.py"))
    test_paths = sorted(root.joinpath(TEST_ROOT).rglob("*.py"))
    doc_paths = sorted(root.joinpath(RESEARCH_DOC_ROOT).glob("*.md"))
    files: list[dict[str, Any]] = []
    functions: list[FunctionRecord] = []
    constants: dict[str, list[dict[str, str]]] = defaultdict(list)
    module_to_path: dict[str, str] = {}
    for path in [*source_paths, *test_paths]:
        module_to_path[_module_name(path.relative_to(root))] = path.relative_to(
            root
        ).as_posix()
    for path, kind in [
        *((path, "python") for path in source_paths),
        *((path, "python") for path in test_paths),
        *((path, "research_doc") for path in doc_paths),
    ]:
        relative = path.relative_to(root)
        record, file_functions, file_constants = _inventory_file(relative, kind)
        files.append(record)
        functions.extend(file_functions)
        for name, value in file_constants.items():
            constants[f"{name}:{value}"].append({"path": record["path"], "name": name})
    imported_by: dict[str, set[str]] = defaultdict(set)
    for record in files:
        for imported in record["direct_imports"]:
            target = imported.lstrip(".")
            if target in module_to_path:
                imported_by[module_to_path[target]].add(record["path"])
    test_path_set = {
        record["path"] for record in files if record["path"].startswith("tests/")
    }
    for record in files:
        record["imported_by"] = sorted(imported_by[record["path"]])
        if record["path"].startswith("src/"):
            record["test_counterpart"] = _counterpart(record["path"], test_path_set)
    classes = {record["path"]: record["classification"] for record in files}
    by_name: dict[str, list[FunctionRecord]] = defaultdict(list)
    by_structure: dict[str, list[FunctionRecord]] = defaultdict(list)
    for function in functions:
        by_name[function.name].append(function)
        by_structure[function.structural_hash].append(function)
    duplicate_functions: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for label, groups in (("same_name", by_name), ("structural", by_structure)):
        for group in groups.values():
            paths = tuple(sorted({item.path for item in group}))
            if len(group) < 2 or len(paths) < 2:
                continue
            identity = (label, paths)
            if identity in seen:
                continue
            seen.add(identity)
            duplicate_functions.append(
                {
                    "match_kind": label,
                    "function_name": group[0].name,
                    "occurrences": len(group),
                    "paths": list(paths),
                    "records": [
                        asdict(item)
                        for item in sorted(
                            group, key=lambda item: (item.path, item.line)
                        )
                    ],
                    "classification": _duplicate_classification(group, classes),
                }
            )
    duplicate_functions.sort(
        key=lambda item: (-item["occurrences"], item["function_name"], item["paths"])
    )
    repeated_constants = [
        {"constant_value_ast": key, "occurrences": len(value), "records": value}
        for key, value in constants.items()
        if len(value) >= 2
    ]
    repeated_constants.sort(
        key=lambda item: (-item["occurrences"], item["constant_value_ast"])
    )
    classification_counts = Counter(record["classification"] for record in files)
    large = {
        bucket: [
            {
                key: record[key]
                for key in (
                    "path",
                    "line_count",
                    "approx_size_bytes",
                    "classification",
                    "token_burden_estimate",
                )
            }
            for record in sorted(
                (
                    item
                    for item in files
                    if item["kind"] == kind and not item["path"].startswith("tests/")
                ),
                key=lambda item: (-item["line_count"], item["path"]),
            )[:20]
        ]
        for bucket, kind in (("source", "python"), ("research_docs", "research_doc"))
    }
    large["tests"] = [
        {
            key: record[key]
            for key in (
                "path",
                "line_count",
                "approx_size_bytes",
                "classification",
                "token_burden_estimate",
            )
        }
        for record in sorted(
            (item for item in files if item["path"].startswith("tests/")),
            key=lambda item: (-item["line_count"], item["path"]),
        )[:20]
    ]
    return {
        "audit_version": "CODEBASE_TOKEN_EFFICIENCY_AUDIT_V0_1",
        "network_calls": 0,
        "scan_scope": {
            "source": "src/**/*.py",
            "tests": "tests/**/*.py",
            "research_docs": "docs/research/*.md",
        },
        "counts": {
            "source_files": len(source_paths),
            "test_files": len(test_paths),
            "research_docs": len(doc_paths),
            "classification": dict(sorted(classification_counts.items())),
        },
        "files": sorted(files, key=lambda item: item["path"]),
        "large_files": large,
        "duplicate_function_candidates": duplicate_functions[:100],
        "repeated_constant_candidates": repeated_constants[:100],
        "active_v0_1_case": _active_v0_1_case(files, functions),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = scan_repository(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
