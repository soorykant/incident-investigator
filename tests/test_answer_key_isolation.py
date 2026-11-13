"""Answer keys must never reach agent code (PRD R6, steps 1 and 11)."""

import ast
import builtins
import re
from pathlib import Path

import pytest

import incident_investigator
from incident_investigator import dataset
from incident_investigator.dataset import list_incident_ids, load_answer_key
from incident_investigator.investigate import MODES, investigate
from incident_investigator.llm.mock import MockLLM

SRC = Path(incident_investigator.__file__).parent
LOADER = SRC / "dataset.py"
# Evaluation code (ticket 09) is the one other place that may load answer keys. Agent,
# graph, tool and LLM code may not, and none of them may import the evaluation package.
EVALUATION = SRC / "evaluation"
ALLOWED = {LOADER, *EVALUATION.rglob("*.py")}
MENTION = re.compile(r"answer_key|AnswerKey", re.IGNORECASE)
# Building a path from the data folder is how a module would find the keys without naming them.
DATA_PATH = re.compile(r"\bDATA_DIR\b|[\"']data[\"']\s*\)?\s*/|/\s*[\"']data[\"']")
# What the rest of the code may take from the loader: scenarios, never keys.
SAFE_DATASET_NAMES = {"load_scenario", "list_incident_ids", "UnknownIncidentError"}


def _scanned() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if p not in ALLOWED]


def test_only_the_dataset_loader_mentions_answer_keys():
    offenders = [str(p.relative_to(SRC)) for p in _scanned() if MENTION.search(p.read_text())]
    assert offenders == []


def test_no_other_module_builds_a_path_into_the_data_folder():
    # Evaluation code too: it reads keys only through the loader.
    offenders = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if p != LOADER and DATA_PATH.search(p.read_text())]
    assert offenders == []


def test_other_modules_import_only_scenario_loading_from_the_dataset_module():
    offenders: list[str] = []
    for path in _scanned():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module == "incident_investigator.dataset":
                offenders += [
                    f"{path.relative_to(SRC)}: {a.name}"
                    for a in node.names
                    if a.name not in SAFE_DATASET_NAMES
                ]
            elif isinstance(node, ast.ImportFrom) and node.module == "incident_investigator":
                offenders += [
                    f"{path.relative_to(SRC)}: dataset module" for a in node.names if a.name == "dataset"
                ]
            elif isinstance(node, ast.Import):
                offenders += [
                    f"{path.relative_to(SRC)}: {a.name}"
                    for a in node.names
                    if a.name == "incident_investigator.dataset"
                ]
    assert offenders == []


def test_the_evaluation_allowlist_is_only_the_evaluation_package():
    assert ALLOWED - {LOADER}, "the evaluation package should exist"
    assert all(p.is_relative_to(EVALUATION) for p in ALLOWED - {LOADER})


def test_nothing_outside_the_evaluation_package_imports_it():
    offenders: list[str] = []
    for path in _scanned():
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            if isinstance(node, ast.ImportFrom) and node.module == "incident_investigator":
                names += [f"incident_investigator.{a.name}" for a in node.names]
            offenders += [
                f"{path.relative_to(SRC)}: {n}" for n in names if n.startswith("incident_investigator.evaluation")
            ]
    assert offenders == []


def test_the_scan_covers_the_agent_and_tool_packages():
    scanned = {p.parent.name for p in SRC.rglob("*.py")}
    assert {"agents", "tools", "llm"} <= scanned


def test_a_full_investigation_never_opens_an_answer_key(monkeypatch: pytest.MonkeyPatch):
    """Runtime proof: trip a wire on every way the keys could be read, then run."""
    touched: list[str] = []
    real_open, real_path_open, real_read_text = builtins.open, Path.open, Path.read_text

    def guarded_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "answer_keys" in str(file):
            touched.append(str(file))
        return real_open(file, *args, **kwargs)

    def guarded_path_open(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "answer_keys" in str(self):
            touched.append(str(self))
        return real_path_open(self, *args, **kwargs)

    def guarded_read_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "answer_keys" in str(self):
            touched.append(str(self))
        return real_read_text(self, *args, **kwargs)

    def forbidden(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        touched.append("load_answer_key")
        raise AssertionError("an investigation called load_answer_key")

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(dataset, "load_answer_key", forbidden)

    for incident_id in list_incident_ids():
        for mode in MODES:
            investigate(incident_id, MockLLM(delay_s=0.0), mode=mode, tool_backoff_s=0)

    assert touched == []


# --- PRD step 11: no hardcoding -----------------------------------------------

# Narrow, reviewed exceptions: user-facing examples, not logic.
EXAMPLES = {SRC / "cli.py": ["e.g. inc01"], SRC / "api.py": ["e.g. inc01"]}


def _source_without_examples(path: Path) -> str:
    text = path.read_text()
    for example in EXAMPLES.get(path, []):
        text = text.replace(example, "")
    return text


def _all_source_files() -> list[Path]:
    return [p for p in SRC.rglob("*") if p.is_file() and p.suffix in {".py", ".md", ".json", ".txt", ".yaml", ".toml"}]


def test_no_source_outside_the_loader_names_an_incident_id():
    ids = list_incident_ids()
    assert len(ids) == 10
    literal = re.compile(r"\b(" + "|".join(ids) + r")\b")
    offenders = {
        str(p.relative_to(SRC)): sorted(set(literal.findall(_source_without_examples(p))))
        for p in _all_source_files()
        if p != LOADER
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_no_source_outside_the_loader_names_an_answer_key_root_cause_service():
    services = {s for i in list_incident_ids() if (s := load_answer_key(i).service)}
    assert services
    offenders = {
        str(p.relative_to(SRC)): sorted(s for s in services if s in _source_without_examples(p))
        for p in _all_source_files()
        if p != LOADER
    }
    assert {k: v for k, v in offenders.items() if v} == {}
