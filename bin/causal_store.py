#!/usr/bin/env python3
"""Causal-model store for pi-causal-guard (SPEC.md, component C-1).

Every subcommand prints exactly one JSON object on stdout.
Exit codes: 0 success, 2 validation error (bad input or invalid model), 1 other failure.

Store root: env PI_CAUSAL_STORE, default ~/.pi/agent/causal-models (own git repo).
Layout: config.yaml, registry.yaml, models/<id>.yaml, checks.jsonl, log.jsonl.
A model id in registry model_paths has its model file at that path instead (an external file, not committed).

Subcommands: lookup, check, save, update, validate, export, has-check, link.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

# networkx is imported inside the graph functions: its import takes about 2 s and lookup does not need it.

DEFAULT_STORE = Path.home() / ".pi" / "agent" / "causal-models"
# search_repo: git repo searched by lookup (None: no repo search). search_paths: pathspecs in that repo.
# session_search_command: command the agent runs to search earlier sessions (None: no command, gate G2 off).
DEFAULT_CONFIG = {
    "search_repo": None,
    "search_paths": ["."],
    "search_ref": "origin/main",
    "session_search_command": None,
    "stale_days": 7,
}
DEFAULT_REGISTRY = {"domains": {"general": {"keywords": [], "models": []}}}
GIT_IDENTITY = ["-c", "user.name=pi-causal-guard", "-c", "user.email=noreply@local", "-c", "commit.gpgsign=false"]
SEARCH_TIMEOUT_S = 6
SEARCH_MAX_HITS = 10
SEARCH_MAX_TERMS = 6
SEARCH_MIN_LETTERS = 4

MODEL_STATUSES = ("hypothesis", "reviewed")
EDGE_STATUSES = ("known", "assumed", "unknown")
RISKY_EDGE_STATUSES = ("assumed", "unknown")
EDGE_TYPES = ("directed", "bidirected")
VARIABLE_ROLES = ("exposure", "outcome", "mediator", "context", "selection", "measurement", "latent", "other")
EXPORT_FORMATS = ("dagitty", "dot")
DAGITTY_ROLE_ATTRIBUTES = ("exposure", "outcome", "latent")
DAGITTY_PLAIN_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DOT_EDGE_STYLES = {"known": "solid", "assumed": "dashed", "unknown": "dotted"}
CLAIM_DESIGNS = ("randomized", "observational")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OPEN_PATH_SEARCH_LIMIT = 20000
MAX_ALTERNATIVES = 50
CLAIM_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# Prompt words that never become repository search terms (lower case, 4+ letters).
STOP_WORDS = frozenset("""
about above after again against also always among another anything around because been before being below
between both cannot could does doing done down during each either else enough even ever every from further
gave give given gives going have having here hers herself himself however into itself just know like look
made make makes many maybe more most much must myself need needs never next nothing once only other others
ours over please really same should show since some something still such sure take tell than that thats
their them themselves then there these they thing things think this those though through thus today too
under until upon very want wants were what whatever when where whether which while whom whose will with
within without would yeah your yours yourself
""".split())


class ValidationError(Exception):
    """Bad input or an invalid model; exit code 2."""

    def __init__(self, errors):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


# ---------------------------------------------------------------- store basics

def store_root():
    return Path(os.environ.get("PI_CAUSAL_STORE") or DEFAULT_STORE).expanduser()


def git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), *GIT_IDENTITY, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=check)


def git_commit(root, paths, message):
    git(root, "add", "--", *[str(p) for p in paths])
    git(root, "commit", "-q", "-m", message)


def dump_yaml(data):
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_yaml(data), encoding="utf-8")


def plain(value):
    """YAML dates become ISO strings so models stay JSON-serializable."""
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def read_yaml(path):
    try:
        return plain(yaml.safe_load(path.read_text(encoding="utf-8")))
    except yaml.YAMLError as exc:
        raise ValidationError(f"{path.name}: invalid YAML: {exc}") from exc


def ensure_store(root):
    """Create the store (git init, default config and registry) on first use."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "models").mkdir(exist_ok=True)
    created = []
    if not (root / ".git").exists():
        git(root, "init", "-q")
    if not (root / "config.yaml").exists():
        write_yaml(root / "config.yaml", DEFAULT_CONFIG)
        created.append("config.yaml")
    if not (root / "registry.yaml").exists():
        write_yaml(root / "registry.yaml", DEFAULT_REGISTRY)
        created.append("registry.yaml")
    if created:
        git_commit(root, created, "init store")


def load_config(root):
    config = dict(DEFAULT_CONFIG)
    path = root / "config.yaml"
    if path.exists():
        config.update(read_yaml(path) or {})
    return config


def load_registry(root):
    path = root / "registry.yaml"
    registry = (read_yaml(path) if path.exists() else None) or {}
    registry.setdefault("domains", {})
    if registry["domains"] is None:
        registry["domains"] = {}
    return registry


def save_registry(root, registry):
    write_yaml(root / "registry.yaml", registry)


def model_path(root, model_id):
    """Store file of a model: models/<id>.yaml."""
    return root / "models" / f"{model_id}.yaml"


def expand_external_path(model_id, value):
    """Path of an external model file; it must be absolute or start with ~ (expanded)."""
    error = f"model path of {model_id} must be absolute or start with ~: {value!r}"
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(error)
    try:
        path = Path(value).expanduser() if value.startswith("~") else Path(value)
    except RuntimeError as exc:
        raise ValidationError(f"{error} ({exc})") from exc
    if not path.is_absolute():
        raise ValidationError(error)
    return path


def model_file(root, model_id, registry=None):
    """Return (path, external): the registry model_paths entry of model_id, else models/<id>.yaml."""
    registry = load_registry(root) if registry is None else registry
    model_paths = registry.get("model_paths") or {}
    if not isinstance(model_paths, dict):
        raise ValidationError("registry model_paths must be a mapping {model id: path}")
    if model_id in model_paths:
        return expand_external_path(model_id, model_paths[model_id]), True
    return model_path(root, model_id), False


def require_slug(value, what):
    if not isinstance(value, str) or not SLUG_RE.match(value):
        raise ValidationError(f"{what} must be a slug (letters, digits, '.', '_', '-'): {value!r}")


def load_model(root, model_id):
    require_slug(model_id, "model id")
    path, external = model_file(root, model_id)
    if not path.exists():
        raise ValidationError(f"model not found: {model_id} ({path})" if external else f"model not found: {model_id}")
    model = read_yaml(path)
    if not isinstance(model, dict):
        raise ValidationError(f"model {model_id} is not a mapping")
    return model


def append_jsonl(path, record):
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def today_iso():
    return dt.date.today().isoformat()


# ---------------------------------------------------------------- model schema v2

def is_text(value):
    return isinstance(value, str) and value.strip() != ""


def is_date(value):
    if not isinstance(value, str) or not DATE_RE.match(value):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def edge_type(edge):
    """Edge type; a missing type is directed."""
    return edge.get("type", "directed")


def edge_arrow(edge):
    """`from->to`, or `from<->to` for a bidirected edge (used in update diffs)."""
    arrow = "<->" if edge_type(edge) == "bidirected" else "->"
    return f"{edge.get('from')}{arrow}{edge.get('to')}"


def validate_model(model):
    """Return the list of schema v2 errors (empty when valid)."""
    import networkx as nx
    if not isinstance(model, dict):
        return ["model must be a mapping"]
    errors = []
    if model.get("schema_version") != 2:
        errors.append("schema_version must be 2")
    for key in ("id", "domain"):
        if not isinstance(model.get(key), str) or not SLUG_RE.match(model[key]):
            errors.append(f"{key} must be a slug (letters, digits, '.', '_', '-')")
    if not is_text(model.get("title")):
        errors.append("title must be a non-empty string")
    revision = model.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        errors.append("revision must be a positive integer")
    if model.get("status") not in MODEL_STATUSES:
        errors.append(f"status must be one of {', '.join(MODEL_STATUSES)}")
    if not is_date(model.get("last_gathered")):
        errors.append("last_gathered must be a date YYYY-MM-DD")

    sources = model.get("sources")
    if not isinstance(sources, dict):
        errors.append("sources must be a mapping {key: {ref, use}}")
        sources = {}
    for key, source in sources.items():
        if not isinstance(source, dict) or not is_text(source.get("ref")) or not is_text(source.get("use")):
            errors.append(f"source {key} needs non-empty ref and use")

    variables = model.get("variables")
    if not isinstance(variables, list):
        errors.append("variables must be a list")
        variables = []
    var_ids = set()
    for i, var in enumerate(variables):
        if not isinstance(var, dict) or not is_text(var.get("id")):
            errors.append(f"variable #{i + 1} needs an id")
            continue
        if var["id"] in var_ids:
            errors.append(f"duplicate variable id {var['id']}")
        var_ids.add(var["id"])
        for key in ("label", "definition"):
            if not is_text(var.get(key)):
                errors.append(f"variable {var['id']} needs a non-empty {key}")
        if "role" in var and var["role"] not in VARIABLE_ROLES:
            errors.append(f"variable {var['id']} role must be one of {', '.join(VARIABLE_ROLES)}")

    edges = model.get("edges")
    if not isinstance(edges, list):
        errors.append("edges must be a list")
        edges = []
    edge_ids, pairs, bidirected_pairs = set(), set(), set()
    graph = nx.DiGraph()
    for i, edge in enumerate(edges):
        if not isinstance(edge, dict) or not is_text(edge.get("id")):
            errors.append(f"edge #{i + 1} needs an id")
            continue
        eid = edge["id"]
        if eid in edge_ids:
            errors.append(f"duplicate edge id {eid}")
        edge_ids.add(eid)
        src, dst = edge.get("from"), edge.get("to")
        etype = edge_type(edge)
        if etype not in EDGE_TYPES:
            errors.append(f"edge {eid} type must be one of {', '.join(EDGE_TYPES)}")
        elif etype == "directed":
            if (src, dst) in pairs:
                errors.append(f"duplicate edge {src}->{dst} ({eid})")
            pairs.add((src, dst))
        else:
            if frozenset((src, dst)) in bidirected_pairs:
                errors.append(f"duplicate bidirected edge {src}<->{dst} ({eid})")
            bidirected_pairs.add(frozenset((src, dst)))
        if src == dst and src in var_ids:
            errors.append(f"edge {eid} goes from {src} to itself")
        dangling = [n for n in (src, dst) if n not in var_ids]
        if dangling:
            errors.append(f"dangling edge {eid}: unknown variable {', '.join(map(str, dangling))}")
        elif etype == "directed" and src != dst:
            graph.add_edge(src, dst)
        if edge.get("status") not in EDGE_STATUSES:
            errors.append(f"edge {eid} status must be one of {', '.join(EDGE_STATUSES)}")
        if not is_text(edge.get("reason")):
            errors.append(f"edge {eid} needs a non-empty reason")
        edge_sources = edge.get("sources")
        if not isinstance(edge_sources, list):
            errors.append(f"edge {eid} sources must be a list of source keys")
            edge_sources = []
        for key in edge_sources:
            if key not in sources:
                errors.append(f"edge {eid} cites missing source {key}")
        if edge.get("status") == "known" and not edge_sources:
            errors.append(f"known edge {eid} needs at least 1 source")
    if not nx.is_directed_acyclic_graph(graph):
        cycle = [a for a, _ in nx.find_cycle(graph)]
        errors.append("directed cycle: " + " -> ".join(cycle + cycle[:1]))
    return errors


def model_graph(model):
    """Directed graph of the variables and the directed edges (bidirected edges are left out)."""
    import networkx as nx
    graph = nx.DiGraph()
    graph.add_nodes_from(v["id"] for v in model["variables"])
    for edge in model["edges"]:
        if edge_type(edge) == "directed":
            graph.add_edge(edge["from"], edge["to"], id=edge["id"], status=edge["status"])
    return graph


@dataclass(frozen=True)
class HiddenNode:
    """Unmeasured common cause of a bidirected edge. Not a str, so it never equals a variable id."""
    edge_id: str


def canonical_graph(model, graph):
    """graph plus, for each bidirected edge a <-> b, a hidden node with the edges hidden->a and hidden->b."""
    canonical = graph.copy()
    for edge in model["edges"]:
        if edge_type(edge) == "bidirected":
            hidden = HiddenNode(edge["id"])
            canonical.add_edge(hidden, edge["from"])
            canonical.add_edge(hidden, edge["to"])
    return canonical


# ---------------------------------------------------------------- claim checks

def claim_format_errors(claim):
    if not isinstance(claim, dict):
        return ["claim must be an object"]
    cid = claim.get("id")
    where = f"claim {cid}" if is_text(cid) else "claim"
    errors = []
    if not is_text(cid):
        errors.append("claim needs an id")
    elif not CLAIM_ID_RE.match(cid):
        errors.append(f"{where}: id must match {CLAIM_ID_RE.pattern} (letters, digits, _ . -)")
    if not isinstance(claim.get("text"), str):
        errors.append(f"{where} needs text")
    for key in ("cause", "effect"):
        if not is_text(claim.get(key)):
            errors.append(f"{where} needs {key}")
    if is_text(claim.get("cause")) and claim.get("cause") == claim.get("effect"):
        errors.append(f"{where}: cause and effect must differ")
    if claim.get("design") not in CLAIM_DESIGNS:
        errors.append(f"{where} design must be one of {', '.join(CLAIM_DESIGNS)}")

    def is_path(p):
        return isinstance(p, list) and len(p) >= 2 and all(is_text(n) for n in p)

    paths = claim.get("claimed_paths")
    if not isinstance(paths, list) or not paths or not all(is_path(p) for p in paths):
        errors.append(f"{where} claimed_paths must be a non-empty list of paths (lists of 2+ variable ids)")
    ruled_out = claim.get("ruled_out", [])
    if not isinstance(ruled_out, list) or not all(is_path(p) for p in ruled_out):
        errors.append(f"{where} ruled_out must be a list of paths (lists of 2+ variable ids)")
    conditioned = claim.get("conditioned_on", [])
    if not isinstance(conditioned, list) or not all(is_text(n) for n in conditioned):
        errors.append(f"{where} conditioned_on must be a list of variable ids")
    return errors


def check_id_for(model_id, revision, claim):
    canonical = json.dumps(claim, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:6]
    return f"{model_id}@r{revision}#{claim['id']}-{digest}"


def format_directed(path):
    return "->".join(path)


def format_trail(graph, path):
    """Trail as 'a -> b <- c'; a step over a hidden node a <- hidden -> b shows as 'a <-> b'."""
    out = [path[0]]
    for a, b in zip(path, path[1:]):
        if isinstance(b, HiddenNode):
            continue
        if isinstance(a, HiddenNode):
            out.append(f"<-> {b}")
        else:
            out.append(f"-> {b}" if graph.has_edge(a, b) else f"<- {b}")
    return " ".join(out)


def trail_is_open(graph, path, z):
    import networkx as nx
    for a, node, b in zip(path, path[1:], path[2:]):
        collider = graph.has_edge(a, node) and graph.has_edge(b, node)
        if collider:
            if node not in z and not (nx.descendants(graph, node) & z):
                return False
        elif node in z:
            return False
    return True


def open_backdoor_path(graph, cause, effect, conditioned):
    """Backdoor test: remove the outgoing edges of cause, then test d-separation given conditioned.

    Returns (is_open, open trail or None if not found in the search limit, backdoor graph).
    The trail is the shortest open one among the first OPEN_PATH_SEARCH_LIMIT simple trails."""
    import networkx as nx
    backdoor_graph = graph.copy()
    backdoor_graph.remove_edges_from(list(graph.out_edges(cause)))
    z = set(conditioned) - {cause, effect}
    if nx.is_d_separator(backdoor_graph, {cause}, {effect}, z):
        return False, None, backdoor_graph
    skeleton = backdoor_graph.to_undirected(as_view=True)
    best = None
    def length(path):
        return sum(not isinstance(n, HiddenNode) for n in path)

    for i, path in enumerate(nx.all_simple_paths(skeleton, cause, effect)):
        if i >= OPEN_PATH_SEARCH_LIMIT:
            break
        if (best is None or length(path) < length(best)) and trail_is_open(backdoor_graph, path, z):
            best = path
    return True, best, backdoor_graph


def check_claim(model, graph, canonical, claim):
    """Apply the verdict rules 1-6 of SPEC.md C-1 to one claim (already format-checked).

    graph: variables and directed edges (rules 1, 4, variable lookup). canonical: graph plus a hidden node per
    bidirected edge (rules 2, 3, 5)."""
    import networkx as nx
    problems, notes = [], []
    cause, effect = claim["cause"], claim["effect"]
    conditioned = list(claim.get("conditioned_on", []))
    claimed = [list(p) for p in claim["claimed_paths"]]
    ruled_out = [list(p) for p in claim.get("ruled_out", [])]

    mentioned = [cause, effect, *conditioned, *(n for p in claimed + ruled_out for n in p)]
    unknown = sorted({n for n in mentioned if n not in graph})
    if unknown:
        problems.append(f"unknown variables: {', '.join(unknown)}")

    # 1. Claimed paths are directed paths cause -> effect.
    for path in claimed:
        directed = all(graph.has_edge(a, b) for a, b in zip(path, path[1:]))
        if path[0] != cause or path[-1] != effect or not directed:
            problems.append(f"claimed path {format_directed(path)} is not a directed path from {cause} to {effect} in the model")

    endpoints_known = cause in graph and effect in graph
    # 2. Backdoor (observational only).
    if endpoints_known and claim["design"] == "observational" and all(n in graph for n in conditioned):
        is_open, open_path, backdoor_graph = open_backdoor_path(canonical, cause, effect, conditioned)
        if is_open:
            given = ", ".join(conditioned) if conditioned else "nothing"
            trail = format_trail(backdoor_graph, open_path) if open_path else "open backdoor (path not listed)"
            problems.append(f"open backdoor path given {given}: {trail}")

    # 2b. Conditioning on a descendant of cause (all designs): overcontrol or collider bias.
    if endpoints_known and all(n in graph for n in conditioned):
        bad = sorted(set(conditioned) & nx.descendants(canonical, cause))
        if bad:
            problems.append(f"conditioned_on contains descendants of {cause}: {', '.join(bad)} (overcontrol or collider bias)")

    # 6. Conditioning on a latent variable (all designs): it is not measured.
    latent = {v["id"] for v in model["variables"] if v.get("role") == "latent"}
    bad_latent = sorted(set(conditioned) & latent)
    if bad_latent:
        problems.append(f"conditioned_on contains latent variables: {', '.join(bad_latent)} (an unmeasured variable cannot be conditioned on)")

    # 3. Alternatives: other directed paths cause -> effect, neither claimed nor ruled out (listed up to a limit).
    alternatives, truncated = [], False
    if endpoints_known:
        for path in nx.all_simple_paths(canonical, cause, effect):
            if path not in claimed and path not in ruled_out:
                if len(alternatives) >= MAX_ALTERNATIVES:
                    truncated = True
                    break
                alternatives.append(path)
    for path in sorted(alternatives, key=lambda p: (len(p), p)):
        notes.append(f"alternative path not claimed or ruled out: {format_directed(path)}")
    if truncated:
        notes.append("... and more (truncated)")

    # 4. Risky edges on claimed paths.
    risky_edges, seen = [], set()
    for path in claimed:
        for a, b in zip(path, path[1:]):
            if not graph.has_edge(a, b) or (a, b) in seen:
                continue
            seen.add((a, b))
            data = graph.edges[a, b]
            if data["status"] in RISKY_EDGE_STATUSES:
                risky_edges.append({"id": data["id"], "from": a, "to": b, "status": data["status"]})
    unknown_edges = [e for e in risky_edges if e["status"] == "unknown"]
    for edge in unknown_edges:
        notes.append(f"unknown edge {edge['id']} ({edge['from']}->{edge['to']}) on a claimed path: verdict at most partly")

    if problems:
        verdict = "does_not_follow"
    elif alternatives or unknown_edges:
        verdict = "partly"
    else:
        verdict = "follows"
    return {
        "check_id": check_id_for(model["id"], model["revision"], claim),
        "claim_id": claim["id"],
        "verdict": verdict,
        "problems": problems,
        "notes": notes,
        "risky_edges": risky_edges,
    }


def check_claims(model, claims):
    if not isinstance(claims, list) or not claims:
        raise ValidationError("claims must be a non-empty list")
    errors = [e for c in claims for e in claim_format_errors(c)]
    ids = [c.get("id") for c in claims if isinstance(c, dict)]
    errors += [f"duplicate claim id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    if errors:
        raise ValidationError(errors)
    graph = model_graph(model)
    canonical = canonical_graph(model, graph)
    return [check_claim(model, graph, canonical, c) for c in claims]


# ---------------------------------------------------------------- lookup

def keyword_hits(prompt, keyword):
    pattern = r"(?<!\w)" + re.escape(keyword) + r"(?!\w)"
    return len(re.findall(pattern, prompt, flags=re.IGNORECASE))


def match_domain(prompt, registry):
    scores = {}
    for domain_id, domain in registry["domains"].items():
        keywords = (domain or {}).get("keywords") or []
        scores[domain_id] = sum(keyword_hits(prompt, k) for k in keywords if is_text(k))
    best = max(scores.values(), default=0)
    winners = [d for d, s in scores.items() if s == best]
    if best == 0 or len(winners) != 1:
        return None
    return winners[0]


def search_terms(prompt):
    terms, seen = [], set()
    for word in re.findall(r"\w+", prompt):
        lower = word.lower()
        letters = sum(ch.isalpha() for ch in lower)
        if letters < SEARCH_MIN_LETTERS or lower in STOP_WORDS or lower in seen:
            continue
        seen.add(lower)
        terms.append(re.escape(lower))
        if len(terms) == SEARCH_MAX_TERMS:
            break
    return terms


def repo_grep(repo, ref, terms, paths):
    """git grep -l on ref in repo; returns (hits, error)."""
    if not terms:
        return [], None
    cmd = ["git", "-C", str(repo), "grep", "-l", "-i", "-E", "|".join(terms), ref, "--", *paths]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=SEARCH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return [], f"repository grep timed out after {SEARCH_TIMEOUT_S} s"
    except OSError as exc:
        return [], f"repository grep failed: {exc}"
    if proc.returncode == 1 and not proc.stderr.strip():
        return [], None
    if proc.returncode != 0:
        return [], f"repository grep failed (exit {proc.returncode}): {proc.stderr.strip()[:300]}"
    prefix = ref + ":"
    hits = [line[len(prefix):] if line.startswith(prefix) else line for line in proc.stdout.splitlines() if line]
    return hits[:SEARCH_MAX_HITS], None


def model_summary(root, registry, model_id, stale_days):
    """Lookup entry of a model; None when its file is missing or its model_paths entry is invalid."""
    if not SLUG_RE.match(model_id):
        return None
    try:
        path, _ = model_file(root, model_id, registry)
    except ValidationError:
        return None
    if not path.exists():
        return None
    try:
        model = read_yaml(path) or {}
    except ValidationError:
        model = {}
    last = model.get("last_gathered")
    stale = True
    if is_date(last):
        stale = (dt.date.today() - dt.date.fromisoformat(last)).days > stale_days
    return {"id": model_id, "path": str(path), "revision": model.get("revision"), "last_gathered": last, "stale": stale}


def cmd_lookup(root, prompt_file):
    try:
        prompt = Path(prompt_file).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"cannot read prompt file: {exc}") from exc
    config = load_config(root)
    registry = load_registry(root)
    domain = match_domain(prompt, registry)
    models = []
    paths = list(config.get("search_paths") or DEFAULT_CONFIG["search_paths"])
    if domain:
        entry = registry["domains"][domain] or {}
        for model_id in entry.get("models") or []:
            summary = model_summary(root, registry, model_id, int(config.get("stale_days", 7)))
            if summary:
                models.append(summary)
        paths = list(entry.get("search_paths") or paths)
    hits, error = [], None
    if config.get("search_repo"):
        ref = config.get("search_ref") or DEFAULT_CONFIG["search_ref"]
        hits, error = repo_grep(config["search_repo"], ref, search_terms(prompt), paths)
    out = {
        "domain": domain,
        "domain_source": "keyword" if domain else "none",
        "domains": list(registry["domains"].keys()),
        "models": models,
        "repo_hits": hits,
        "session_search_command": config.get("session_search_command") or None,
    }
    if error:
        out["repo_error"] = error
    return out


# ---------------------------------------------------------------- write commands

def register_model(registry, model_id, domain, keywords):
    """Put model_id in exactly one domain; create the domain with keywords if new."""
    domains = registry["domains"]
    for other_id, other in domains.items():
        if other_id != domain and other and model_id in (other.get("models") or []):
            other["models"].remove(model_id)
    entry = domains.get(domain)
    if entry is None:
        entry = domains[domain] = {"keywords": list(keywords or []), "models": []}
    entry.setdefault("models", [])
    if entry["models"] is None:
        entry["models"] = []
    if model_id not in entry["models"]:
        entry["models"].append(model_id)


def cmd_save(root, model_id, data):
    require_slug(model_id, "model id")
    if not isinstance(data, dict):
        raise ValidationError("stdin must be a model object")
    model = dict(data)
    keywords = model.pop("keywords", None)
    if keywords is not None and (not isinstance(keywords, list) or not all(is_text(k) for k in keywords)):
        raise ValidationError("keywords must be a list of strings")
    if model.setdefault("id", model_id) != model_id:
        raise ValidationError(f"model id {model['id']!r} does not match --model {model_id}")
    registry = load_registry(root)
    path, external = model_file(root, model_id, registry)
    created = not path.exists()
    old_revision = 0 if created else load_model(root, model_id).get("revision", 0)
    model["revision"] = (old_revision if isinstance(old_revision, int) else 0) + 1
    errors = validate_model(model)
    if errors:
        raise ValidationError(errors)
    write_yaml(path, model)
    register_model(registry, model_id, model["domain"], keywords)
    registry_file = root / "registry.yaml"
    registry_changed = not registry_file.exists() or registry_file.read_text(encoding="utf-8") != dump_yaml(registry)
    if registry_changed:
        save_registry(root, registry)
    # An external model file belongs to another repository: commit only the store files that changed.
    paths = ([] if external else [path]) + ([root / "registry.yaml"] if registry_changed else [])
    if paths:
        git_commit(root, paths, f"{'create' if created else 'save'} {model_id} r{model['revision']}")
    return {"id": model_id, "revision": model["revision"], "path": str(path), "created": created}


UPDATE_KEYS = ("facts", "add_edges", "change_edges", "remove_edges", "add_variables", "add_sources")


def apply_update(model, data):
    """Apply an update request to model in place; return human-readable diff lines."""
    errors = [f"unknown update key {k}" for k in data if k not in UPDATE_KEYS]
    facts = data.get("facts") or []
    add_edges = data.get("add_edges") or []
    change_edges = data.get("change_edges") or []
    remove_edges = data.get("remove_edges") or []
    add_variables = data.get("add_variables") or []
    add_sources = data.get("add_sources") or {}
    for key, value, kind in (("facts", facts, list), ("add_edges", add_edges, list), ("change_edges", change_edges, list),
                             ("remove_edges", remove_edges, list), ("add_variables", add_variables, list),
                             ("add_sources", add_sources, dict)):
        if not isinstance(value, kind):
            errors.append(f"{key} must be a {kind.__name__}")
    if errors:
        raise ValidationError(errors)
    if not any((facts, add_edges, change_edges, remove_edges, add_variables, add_sources)):
        raise ValidationError("update is empty")
    for fact in facts:
        if not isinstance(fact, dict) or not is_text(fact.get("text")):
            errors.append("each fact needs text")
    if errors:
        raise ValidationError(errors)

    diff = []
    variables = model.setdefault("variables", [])
    edges = model.setdefault("edges", [])
    sources = model.setdefault("sources", {})
    for key, source in add_sources.items():
        diff.append(f"{'~' if key in sources else '+'} source {key}")
        sources[key] = source
    for var in add_variables:
        variables.append(var)
        diff.append(f"+ variable {var.get('id') if isinstance(var, dict) else var}")
    edges_by_id = {e.get("id"): e for e in edges if isinstance(e, dict)}
    for eid in remove_edges:
        if eid not in edges_by_id:
            errors.append(f"remove_edges: unknown edge {eid}")
            continue
        edge = edges_by_id.pop(eid)
        edges.remove(edge)
        diff.append(f"- edge {eid} {edge_arrow(edge)}")
    for change in change_edges:
        if not isinstance(change, dict) or change.get("id") not in edges_by_id:
            errors.append(f"change_edges: unknown edge {change.get('id') if isinstance(change, dict) else change}")
            continue
        edge = edges_by_id[change["id"]]
        for field, value in change.items():
            if field != "id" and edge.get(field) != value:
                diff.append(f"~ edge {change['id']} {field}: {json.dumps(edge.get(field), ensure_ascii=False)} -> {json.dumps(value, ensure_ascii=False)}")
                edge[field] = value
    for edge in add_edges:
        edges.append(edge)
        if isinstance(edge, dict):
            diff.append(f"+ edge {edge.get('id')} {edge_arrow(edge)} ({edge.get('status')})")
        else:
            diff.append(f"+ edge {edge}")
    if errors:
        raise ValidationError(errors)
    for fact in facts:
        diff.append(f"+ fact: {fact['text']}" + (f" [{fact['source']}]" if fact.get("source") else ""))
    if facts and model.get("last_gathered") != today_iso():
        diff.append(f"~ last_gathered: {model.get('last_gathered')} -> {today_iso()}")
        model["last_gathered"] = today_iso()
    return diff


def cmd_update(root, model_id, data):
    if not isinstance(data, dict):
        raise ValidationError("stdin must be an update object")
    model = load_model(root, model_id)
    old_revision = model.get("revision", 0)
    diff = apply_update(model, data)
    model["revision"] = (old_revision if isinstance(old_revision, int) else 0) + 1
    diff.insert(0, f"~ revision: {old_revision} -> {model['revision']}")
    errors = validate_model(model)
    if errors:
        raise ValidationError(errors)
    path, external = model_file(root, model_id)
    write_yaml(path, model)
    if not external:
        git_commit(root, [path], f"update {model_id} r{model['revision']}")
    append_jsonl(root / "log.jsonl", {"ts": now_iso(), "op": "update", "model": model_id,
                                      "revision": model["revision"], "request": data, "diff": diff})
    return {"id": model_id, "revision": model["revision"], "diff": diff}


def cmd_check(root, model_id, data):
    if not isinstance(data, dict):
        raise ValidationError("stdin must be an object {claims: [...]}")
    model = load_model(root, model_id)
    errors = validate_model(model)
    if errors:
        raise ValidationError([f"model {model_id} is invalid: {e}" for e in errors])
    claims = data.get("claims")
    results = check_claims(model, claims)
    ts = now_iso()
    for claim, result in zip(claims, results):
        append_jsonl(root / "checks.jsonl", {"ts": ts, "model": model_id, "revision": model["revision"],
                                             "claim": claim, **result})
    return {"model": model_id, "revision": model["revision"], "results": results}


def cmd_validate(root, model_id):
    try:
        model = load_model(root, model_id)
    except ValidationError as exc:
        return {"ok": False, "errors": exc.errors}
    errors = validate_model(model)
    return {"ok": not errors, "errors": errors}


def dagitty_id(value):
    if DAGITTY_PLAIN_ID_RE.fullmatch(value):
        return value
    return quote_text(value)


def quote_text(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def export_dagitty(model):
    lines = ["dag {"]
    for var in model["variables"]:
        role = var.get("role")
        lines.append(dagitty_id(var["id"]) + (f" [{role}]" if role in DAGITTY_ROLE_ATTRIBUTES else ""))
    for edge in model["edges"]:
        arrow = "<->" if edge_type(edge) == "bidirected" else "->"
        lines.append(f"{dagitty_id(edge['from'])} {arrow} {dagitty_id(edge['to'])}")
    lines.append("}")
    return "\n".join(lines)


def export_dot(model):
    lines = [f"digraph {quote_text(model['id'])} {{"]
    for var in model["variables"]:
        lines.append(f"{quote_text(var['id'])} [label={quote_text(var['label'])}]")
    for edge in model["edges"]:
        attributes = f"style={DOT_EDGE_STYLES[edge['status']]}"
        if edge_type(edge) == "bidirected":
            attributes += ", dir=both"
        lines.append(f"{quote_text(edge['from'])} -> {quote_text(edge['to'])} [{attributes}]")
    lines.append("}")
    return "\n".join(lines)


def cmd_export(root, model_id, export_format):
    """Read-only export of a valid model as DAGitty or DOT text."""
    if export_format not in EXPORT_FORMATS:
        raise UsageError(f"unknown format {export_format} (use {' or '.join(EXPORT_FORMATS)})")
    model = load_model(root, model_id)
    errors = validate_model(model)
    if errors:
        raise ValidationError([f"model {model_id} is invalid: {e}" for e in errors])
    text = export_dagitty(model) if export_format == "dagitty" else export_dot(model)
    return {"model": model_id, "revision": model["revision"], "format": export_format, "text": text}


def cmd_link(root, model_id, path_text):
    """Map model_id to an external model file in registry model_paths; remove the store copy."""
    require_slug(model_id, "model id")
    path = expand_external_path(model_id, path_text)
    if not path.exists():
        raise ValidationError(f"model not found: {model_id} ({path})")
    model = read_yaml(path)
    if not isinstance(model, dict):
        raise ValidationError(f"model {model_id} is not a mapping")
    if model.get("id") != model_id:
        raise ValidationError(f"model id {model.get('id')!r} does not match --model {model_id}")
    errors = validate_model(model)
    if errors:
        raise ValidationError([f"model {model_id} is invalid: {e}" for e in errors])
    registry = load_registry(root)
    model_paths = registry.get("model_paths") or {}
    if not isinstance(model_paths, dict):
        raise ValidationError("registry model_paths must be a mapping {model id: path}")
    model_paths[model_id] = path_text
    registry["model_paths"] = model_paths
    register_model(registry, model_id, model["domain"], None)
    save_registry(root, registry)
    git(root, "add", "--", "registry.yaml")
    store_copy = model_path(root, model_id)
    removed_store_copy = store_copy.exists()
    if removed_store_copy:
        git(root, "rm", "-q", "-f", "--ignore-unmatch", "--", str(store_copy))
        store_copy.unlink(missing_ok=True)
    # A repeated link with the same path changes no store file: no commit then.
    if git(root, "diff", "--cached", "--quiet", check=False).returncode != 0:
        git(root, "commit", "-q", "-m", f"link {model_id}")
    return {"id": model_id, "path": path_text, "revision": model["revision"], "removed_store_copy": removed_store_copy}


def cmd_has_check(root, ids):
    wanted = [i.strip() for i in ids.split(",") if i.strip()]
    known = set()
    path = root / "checks.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                known.add(json.loads(line).get("check_id"))
            except (json.JSONDecodeError, AttributeError):
                continue
    return {"missing": [i for i in wanted if i not in known]}


# ---------------------------------------------------------------- CLI

def read_stdin_json():
    text = sys.stdin.read()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"stdin is not valid JSON: {exc}") from exc


class UsageError(Exception):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    """Raise instead of printing usage text, so stdout stays one JSON object."""

    def error(self, message):
        raise UsageError(message)


def build_parser():
    parser = JsonArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("lookup").add_argument("--prompt-file", required=True)
    for name in ("check", "save", "update", "validate"):
        sub.add_parser(name).add_argument("--model", required=True)
    export = sub.add_parser("export")
    export.add_argument("--model", required=True)
    export.add_argument("--format", required=True)
    sub.add_parser("has-check").add_argument("--ids", required=True)
    link = sub.add_parser("link")
    link.add_argument("--model", required=True)
    link.add_argument("--path", required=True)
    return parser


def run(argv):
    """Return (output object, exit code)."""
    try:
        args = build_parser().parse_args(argv)
    except UsageError as exc:
        return {"error": f"usage: {exc}"}, 2
    root = store_root()
    try:
        ensure_store(root)
        if args.command == "lookup":
            return cmd_lookup(root, args.prompt_file), 0
        if args.command == "check":
            return cmd_check(root, args.model, read_stdin_json()), 0
        if args.command == "save":
            return cmd_save(root, args.model, read_stdin_json()), 0
        if args.command == "update":
            return cmd_update(root, args.model, read_stdin_json()), 0
        if args.command == "validate":
            out = cmd_validate(root, args.model)
            return out, 0 if out["ok"] else 2
        if args.command == "has-check":
            return cmd_has_check(root, args.ids), 0
        if args.command == "export":
            return cmd_export(root, args.model, args.format), 0
        if args.command == "link":
            return cmd_link(root, args.model, args.path), 0
    except UsageError as exc:
        return {"error": f"usage: {exc}"}, 1
    except ValidationError as exc:
        return {"error": "validation failed", "errors": exc.errors}, 2
    except subprocess.CalledProcessError as exc:
        return {"error": f"command failed: {' '.join(map(str, exc.cmd))}: {(exc.stderr or '').strip()[:500]}"}, 1
    except Exception as exc:  # one JSON object on stdout, even on unexpected failures
        return {"error": f"{type(exc).__name__}: {exc}"}, 1
    return {"error": f"unknown command {args.command}"}, 2


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    out, code = run(sys.argv[1:] if argv is None else argv)
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
