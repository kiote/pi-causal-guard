"""Shared fixtures. Every test runs against a temporary store (PI_CAUSAL_STORE=tmp_path/store)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin"
sys.path.insert(0, str(BIN))

REAL_STORE = (Path.home() / ".pi" / "agent" / "causal-models").resolve()
GIT_IDENTITY = ["-c", "user.name=pi-causal-guard-test", "-c", "user.email=noreply@local", "-c", "commit.gpgsign=false"]


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Temporary store root; never the real ~/.pi/agent/causal-models."""
    root = tmp_path / "store"
    assert REAL_STORE not in [root.resolve(), *root.resolve().parents]
    monkeypatch.setenv("PI_CAUSAL_STORE", str(root))
    return root


@pytest.fixture
def cli(store):
    """Run bin/causal_store.py; assert stdout is exactly one JSON object; return (exit code, object)."""

    def run(*args, stdin=None):
        data = None if stdin is None else json.dumps(stdin).encode("utf-8")
        proc = subprocess.run([sys.executable, str(BIN / "causal_store.py"), *args], input=data,
                              capture_output=True, env={**os.environ, "PI_CAUSAL_STORE": str(store)})
        out = proc.stdout.decode("utf-8")
        lines = [line for line in out.splitlines() if line.strip()]
        assert len(lines) == 1, f"expected one JSON line, got: {out!r} stderr={proc.stderr!r}"
        obj = json.loads(lines[0])
        assert isinstance(obj, dict)
        return proc.returncode, obj

    return run


def make_model(edges, model_id="m", domain="d", statuses=None, variables=None, **extra):
    """Valid schema v2 model from a list of (from, to) pairs; edge id is '<from>_<to>'."""
    statuses = statuses or {}
    names = variables or sorted({n for pair in edges for n in pair})
    model = {
        "schema_version": 2,
        "id": model_id,
        "domain": domain,
        "title": f"Model {model_id}",
        "revision": 1,
        "status": "hypothesis",
        "last_gathered": "2026-11-01",
        "variables": [{"id": n, "label": n, "definition": f"variable {n}"} for n in names],
        "edges": [{"id": f"{a}_{b}", "from": a, "to": b, "status": statuses.get((a, b), "assumed"),
                   "reason": f"{a} affects {b}", "sources": ["s1"]} for a, b in edges],
        "sources": {"s1": {"ref": "https://example.test/s1", "use": "test source"}},
    }
    model.update(extra)
    return model


def make_claim(cause, effect, claimed_paths, design="observational", conditioned_on=(), ruled_out=(), claim_id="c1",
               text="test claim"):
    return {"id": claim_id, "text": text, "cause": cause, "effect": effect,
            "claimed_paths": [list(p) for p in claimed_paths], "conditioned_on": list(conditioned_on),
            "ruled_out": [list(p) for p in ruled_out], "design": design}


@pytest.fixture
def search_repo(tmp_path):
    """Tiny git repo with an origin/main ref, used as config.search_repo."""
    repo = tmp_path / "docs-repo"
    files = {
        "docs/checkout/conversion-notes.md": "Checkout conversion dropped after the widget change.\n",
        "docs/analytics/notes.md": "Telemetry notes.\n",
        "notes/scratch.md": "Conversion again.\n",
        "other/ignored.md": "conversion outside the searched paths\n",
    }
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *GIT_IDENTITY, *args], check=True, capture_output=True)

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    return repo
