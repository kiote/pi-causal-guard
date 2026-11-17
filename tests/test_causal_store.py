"""AC1 tests for bin/causal_store.py (SPEC.md C-1)."""
import datetime as dt
import json
import re
import subprocess
import sys

import pytest
import yaml

import causal_store
from conftest import make_claim, make_model


def commit_count(store):
    out = subprocess.run(["git", "-C", str(store), "rev-list", "--count", "HEAD"], capture_output=True, text=True,
                         check=True)
    return int(out.stdout.strip())


def save(cli, model):
    return cli("save", "--model", model["id"], stdin=model)


def verdict(model, claim):
    return causal_store.check_claims(model, [claim])[0]


# ---------------------------------------------------------------- schema validation

def test_validate_accepts_valid_model():
    assert causal_store.validate_model(make_model([("X", "Y")])) == []


def test_validate_rejects_cycle():
    errors = causal_store.validate_model(make_model([("A", "B"), ("B", "C"), ("C", "A")]))
    assert any(e.startswith("directed cycle:") for e in errors), errors


def test_validate_rejects_dangling_edge():
    model = make_model([("X", "Y")], variables=["X"])
    errors = causal_store.validate_model(model)
    assert any("dangling edge X_Y" in e for e in errors), errors


def test_validate_rejects_duplicates():
    model = make_model([("X", "Y")])
    model["variables"].append({"id": "X", "label": "X2", "definition": "again"})
    model["edges"].append(dict(model["edges"][0]))
    errors = causal_store.validate_model(model)
    assert "duplicate variable id X" in errors
    assert "duplicate edge id X_Y" in errors
    assert any(e.startswith("duplicate edge X->Y") for e in errors), errors


def test_validate_rejects_missing_source_and_unsourced_known_edge():
    model = make_model([("X", "Y"), ("Y", "Z")], statuses={("Y", "Z"): "known"})
    model["edges"][0]["sources"] = ["nope"]
    model["edges"][1]["sources"] = []
    errors = causal_store.validate_model(model)
    assert "edge X_Y cites missing source nope" in errors
    assert "known edge Y_Z needs at least 1 source" in errors


def test_validate_rejects_bad_required_fields():
    model = make_model([("X", "Y")], schema_version=1, status="draft", last_gathered="13.11.2026", revision=0)
    model["edges"][0]["status"] = "design"
    errors = causal_store.validate_model(model)
    assert "schema_version must be 2" in errors
    assert any(e.startswith("status must be one of") for e in errors)
    assert "last_gathered must be a date YYYY-MM-DD" in errors
    assert "revision must be a positive integer" in errors
    assert any(e.startswith("edge X_Y status must be one of") for e in errors)


def test_validate_keeps_unknown_extra_keys(cli, store):
    model = make_model([("X", "Y")], purpose="kept")
    model["edges"][0]["metrics"] = ["all"]
    assert save(cli, model)[0] == 0
    saved = yaml.safe_load((store / "models" / "m.yaml").read_text(encoding="utf-8"))
    assert saved["purpose"] == "kept" and saved["edges"][0]["metrics"] == ["all"]


def test_cli_validate(cli, store):
    assert save(cli, make_model([("X", "Y")]))[0] == 0
    assert cli("validate", "--model", "m") == (0, {"ok": True, "errors": []})
    path = store / "models" / "m.yaml"
    broken = yaml.safe_load(path.read_text(encoding="utf-8"))
    broken["edges"].append({"id": "y_x", "from": "Y", "to": "X", "status": "assumed", "reason": "r", "sources": []})
    path.write_text(yaml.safe_dump(broken), encoding="utf-8")
    code, out = cli("validate", "--model", "m")
    assert code == 2 and out["ok"] is False and any("directed cycle" in e for e in out["errors"])
    code, out = cli("validate", "--model", "missing")
    assert code == 2 and out == {"ok": False, "errors": ["model not found: missing"]}


# ---------------------------------------------------------------- verdict rules (textbook graphs)

CONFOUNDER = make_model([("Z", "X"), ("Z", "Y"), ("X", "Y")])


def test_confounder_observational_does_not_follow():
    result = verdict(CONFOUNDER, make_claim("X", "Y", [["X", "Y"]]))
    assert result["verdict"] == "does_not_follow"
    assert result["problems"] == ["open backdoor path given nothing: X <- Z -> Y"]


def test_confounder_conditioned_follows():
    result = verdict(CONFOUNDER, make_claim("X", "Y", [["X", "Y"]], conditioned_on=["Z"]))
    assert result["verdict"] == "follows" and result["problems"] == [] and result["notes"] == []


def test_confounder_randomized_follows():
    result = verdict(CONFOUNDER, make_claim("X", "Y", [["X", "Y"]], design="randomized"))
    assert result["verdict"] == "follows"


def test_mediator_unclaimed_partly_and_ruled_out_follows():
    model = make_model([("X", "M"), ("M", "Y"), ("X", "Y")])
    result = verdict(model, make_claim("X", "Y", [["X", "Y"]]))
    assert result["verdict"] == "partly"
    assert result["notes"] == ["alternative path not claimed or ruled out: X->M->Y"]
    ruled = verdict(model, make_claim("X", "Y", [["X", "Y"]], ruled_out=[["X", "M", "Y"]]))
    assert ruled["verdict"] == "follows"
    both = verdict(model, make_claim("X", "Y", [["X", "Y"], ["X", "M", "Y"]]))
    assert both["verdict"] == "follows"


COLLIDER = make_model([("A", "X"), ("A", "C"), ("B", "C"), ("B", "Y"), ("X", "Y")])


def test_collider_conditioned_does_not_follow():
    result = verdict(COLLIDER, make_claim("X", "Y", [["X", "Y"]], conditioned_on=["C"]))
    assert result["verdict"] == "does_not_follow"
    assert result["problems"] == ["open backdoor path given C: X <- A -> C <- B -> Y"]


def test_collider_not_conditioned_follows():
    assert verdict(COLLIDER, make_claim("X", "Y", [["X", "Y"]]))["verdict"] == "follows"


def test_conditioning_on_mediator_does_not_follow_for_all_designs():
    # X->M->Y, U->M, U->Y; conditioning on M blocks the claim and opens X->M<-U->Y.
    model = make_model([("X", "M"), ("M", "Y"), ("U", "M"), ("U", "Y")])
    for design in ("observational", "randomized"):
        result = verdict(model, make_claim("X", "Y", [["X", "M", "Y"]], conditioned_on=["M"], design=design))
        assert result["verdict"] == "does_not_follow", design
        assert "conditioned_on contains descendants of X: M (overcontrol or collider bias)" in result["problems"]


def test_alternatives_are_truncated():
    # A ladder graph with many directed paths X -> Y.
    edges, prev = [], ["X"]
    for i in range(8):
        layer = [f"a{i}", f"b{i}"]
        edges += [(p, n) for p in prev for n in layer]
        prev = layer
    edges += [(p, "Y") for p in prev] + [("X", "Y")]
    result = verdict(make_model(edges), make_claim("X", "Y", [["X", "Y"]], design="randomized"))
    assert result["verdict"] == "partly"
    assert result["notes"][-1] == "... and more (truncated)"
    assert len(result["notes"]) == causal_store.MAX_ALTERNATIVES + 1


def test_claim_id_with_comma_is_rejected():
    errors = causal_store.claim_format_errors(make_claim("X", "Y", [["X", "Y"]]) | {"id": "c1,c2"})
    assert any("id must match" in e for e in errors)


def test_claimed_path_not_in_model_does_not_follow():
    model = make_model([("X", "M"), ("M", "Y")])
    result = verdict(model, make_claim("X", "Y", [["X", "Y"]], design="randomized"))
    assert result["verdict"] == "does_not_follow"
    assert result["problems"] == ["claimed path X->Y is not a directed path from X to Y in the model"]
    wrong_end = verdict(model, make_claim("X", "Y", [["X", "M"]], design="randomized"))
    assert wrong_end["verdict"] == "does_not_follow"
    unknown = verdict(model, make_claim("X", "Y", [["X", "Q", "Y"]], design="randomized"))
    assert unknown["verdict"] == "does_not_follow" and "unknown variables: Q" in unknown["problems"]


def test_risky_edges_listed_and_unknown_edge_caps_at_partly():
    model = make_model([("X", "M"), ("M", "Y")], statuses={("X", "M"): "known", ("M", "Y"): "assumed"})
    result = verdict(model, make_claim("X", "Y", [["X", "M", "Y"]], design="randomized"))
    assert result["verdict"] == "follows"
    assert result["risky_edges"] == [{"id": "M_Y", "from": "M", "to": "Y", "status": "assumed"}]
    model = make_model([("X", "M"), ("M", "Y")], statuses={("M", "Y"): "unknown"})
    result = verdict(model, make_claim("X", "Y", [["X", "M", "Y"]], design="randomized"))
    assert result["verdict"] == "partly"
    assert {"id": "M_Y", "from": "M", "to": "Y", "status": "unknown"} in result["risky_edges"]


def test_bad_claim_format_is_validation_error(cli):
    assert save(cli, make_model([("X", "Y")]))[0] == 0
    bad = make_claim("X", "Y", [["X", "Y"]], design="mechanism")
    code, out = cli("check", "--model", "m", stdin={"claims": [bad]})
    assert code == 2 and any("design must be one of" in e for e in out["errors"])


# ---------------------------------------------------------------- check_id and checks.jsonl

def test_check_id_is_stable():
    claim = make_claim("X", "Y", [["X", "Y"]], conditioned_on=["Z"])
    first = verdict(CONFOUNDER, claim)["check_id"]
    reordered = dict(reversed(list(claim.items())))
    assert verdict(CONFOUNDER, reordered)["check_id"] == first
    assert re.fullmatch(r"m@r1#c1-[0-9a-f]{6}", first)
    changed = dict(claim, text="another wording")
    assert verdict(CONFOUNDER, changed)["check_id"] != first
    assert verdict(dict(CONFOUNDER, revision=2), claim)["check_id"].startswith("m@r2#c1-")


def test_check_appends_results_and_has_check(cli, store):
    assert save(cli, CONFOUNDER)[0] == 0
    claims = [make_claim("X", "Y", [["X", "Y"]]), make_claim("X", "Y", [["X", "Y"]], conditioned_on=["Z"], claim_id="c2")]
    code, out = cli("check", "--model", "m", stdin={"claims": claims})
    assert code == 0 and out["model"] == "m" and out["revision"] == 1
    assert [r["verdict"] for r in out["results"]] == ["does_not_follow", "follows"]
    assert set(out["results"][0]) == {"check_id", "claim_id", "verdict", "problems", "notes", "risky_edges"}
    lines = [json.loads(line) for line in (store / "checks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [line["check_id"] for line in lines] == [r["check_id"] for r in out["results"]]
    ids = [r["check_id"] for r in out["results"]]
    assert cli("has-check", "--ids", ",".join(ids)) == (0, {"missing": []})
    assert cli("has-check", "--ids", f"{ids[0]},m@r1#zz-000000") == (0, {"missing": ["m@r1#zz-000000"]})


# ---------------------------------------------------------------- save and update

def test_save_bumps_revision_commits_and_registers(cli, store):
    model = make_model([("X", "Y")], model_id="checkout-flow", domain="checkout")
    code, out = cli("save", "--model", "checkout-flow", stdin={**model, "keywords": ["checkout", "cart"]})
    assert code == 0 and out["revision"] == 1 and out["created"] is True
    commits = commit_count(store)
    code, out = cli("save", "--model", "checkout-flow", stdin=model)
    assert code == 0 and out["revision"] == 2 and out["created"] is False
    assert commit_count(store) == commits + 1
    saved = yaml.safe_load((store / "models" / "checkout-flow.yaml").read_text(encoding="utf-8"))
    assert saved["revision"] == 2 and "keywords" not in saved
    registry = yaml.safe_load((store / "registry.yaml").read_text(encoding="utf-8"))
    assert registry["domains"]["checkout"] == {"keywords": ["checkout", "cart"], "models": ["checkout-flow"]}
    status = subprocess.run(["git", "-C", str(store), "status", "--porcelain", "--", "models", "registry.yaml"],
                            capture_output=True, text=True, check=True)
    assert status.stdout == ""


def test_save_rejects_invalid_model_without_writing(cli, store):
    code, out = save(cli, make_model([("A", "B"), ("B", "A")]))
    assert code == 2 and any("directed cycle" in e for e in out["errors"])
    assert not (store / "models" / "m.yaml").exists()


def test_update_diff_last_gathered_log_and_commit(cli, store):
    assert save(cli, make_model([("X", "Y")]))[0] == 0
    commits = commit_count(store)
    request = {
        "facts": [{"text": "Z drives Y", "source": "s2"}],
        "add_variables": [{"id": "Z", "label": "Z", "definition": "variable Z"}],
        "add_sources": {"s2": {"ref": "https://example.test/s2", "use": "new evidence"}},
        "add_edges": [{"id": "Z_Y", "from": "Z", "to": "Y", "status": "known", "reason": "seen", "sources": ["s2"]}],
        "change_edges": [{"id": "X_Y", "status": "unknown"}],
    }
    code, out = cli("update", "--model", "m", stdin=request)
    assert code == 0 and out["id"] == "m" and out["revision"] == 2
    today = dt.date.today().isoformat()
    for line in ["~ revision: 1 -> 2", "+ source s2", "+ variable Z", "+ edge Z_Y Z->Y (known)",
                 '~ edge X_Y status: "assumed" -> "unknown"', "+ fact: Z drives Y [s2]",
                 f"~ last_gathered: 2026-11-01 -> {today}"]:
        assert line in out["diff"], (line, out["diff"])
    saved = yaml.safe_load((store / "models" / "m.yaml").read_text(encoding="utf-8"))
    assert str(saved["last_gathered"]) == today and saved["revision"] == 2
    assert commit_count(store) == commits + 1
    log = [json.loads(line) for line in (store / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log[-1]["op"] == "update" and log[-1]["revision"] == 2 and log[-1]["diff"] == out["diff"]

    code, out = cli("update", "--model", "m", stdin={"remove_edges": ["Z_Y"]})
    assert code == 0 and out["revision"] == 3 and out["diff"] == ["~ revision: 2 -> 3", "- edge Z_Y Z->Y"]


def test_update_without_facts_keeps_last_gathered(cli, store):
    assert save(cli, make_model([("X", "Y")]))[0] == 0
    code, out = cli("update", "--model", "m", stdin={"change_edges": [{"id": "X_Y", "reason": "better reason"}]})
    assert code == 0
    saved = yaml.safe_load((store / "models" / "m.yaml").read_text(encoding="utf-8"))
    assert str(saved["last_gathered"]) == "2026-11-01"


def test_update_that_breaks_the_model_is_rejected(cli, store):
    assert save(cli, make_model([("X", "Y")]))[0] == 0
    before = (store / "models" / "m.yaml").read_text(encoding="utf-8")
    code, out = cli("update", "--model", "m", stdin={"add_edges": [
        {"id": "Y_X", "from": "Y", "to": "X", "status": "assumed", "reason": "loop", "sources": []}]})
    assert code == 2 and any("directed cycle" in e for e in out["errors"])
    code, out = cli("update", "--model", "m", stdin={"remove_edges": ["nope"]})
    assert code == 2 and out["errors"] == ["remove_edges: unknown edge nope"]
    code, out = cli("update", "--model", "m", stdin={})
    assert code == 2 and out["errors"] == ["update is empty"]
    assert (store / "models" / "m.yaml").read_text(encoding="utf-8") == before
    assert not (store / "log.jsonl").exists()


# ---------------------------------------------------------------- lookup

def write_config(store, **config):
    store.mkdir(parents=True, exist_ok=True)
    (store / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def setup_lookup_store(cli, store, search_repo, last_gathered="2026-11-01", **config):
    write_config(store, search_repo=str(search_repo), search_paths=["docs", "notes"], stale_days=7, **config)
    checkout = make_model([("X", "Y")], model_id="checkout-flow", domain="checkout", last_gathered=last_gathered)
    assert cli("save", "--model", "checkout-flow", stdin={**checkout, "keywords": ["widget", "checkout"]})[0] == 0
    other = make_model([("X", "Y")], model_id="telemetry-model", domain="analytics")
    assert cli("save", "--model", "telemetry-model", stdin={**other, "keywords": ["telemetry", "dashboard"]})[0] == 0


def lookup(cli, tmp_path, prompt):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")
    return cli("lookup", "--prompt-file", str(prompt_file))


def test_lookup_matches_domain_and_repo_hits(cli, store, search_repo, tmp_path):
    setup_lookup_store(cli, store, search_repo)
    code, out = lookup(cli, tmp_path, "Why does the checkout widget show lower conversion? Widget question.")
    assert code == 0, out
    assert out["domain"] == "checkout" and out["domain_source"] == "keyword"
    assert out["domains"] == ["general", "checkout", "analytics"]
    assert [m["id"] for m in out["models"]] == ["checkout-flow"]
    model = out["models"][0]
    assert model["revision"] == 1 and model["last_gathered"] == "2026-11-01"
    assert model["stale"] == ((dt.date.today() - dt.date(2026, 11, 1)).days > 7)
    assert model["path"].endswith("checkout-flow.yaml")
    assert sorted(out["repo_hits"]) == ["docs/checkout/conversion-notes.md", "notes/scratch.md"]
    assert "repo_error" not in out
    assert out["session_search_command"] is None


def test_lookup_returns_session_search_command(cli, store, search_repo, tmp_path):
    setup_lookup_store(cli, store, search_repo, session_search_command="python tools/search.py --recent")
    out = lookup(cli, tmp_path, "widget conversion question here")[1]
    assert out["session_search_command"] == "python tools/search.py --recent"


def test_lookup_uses_search_ref(cli, store, search_repo, tmp_path):
    setup_lookup_store(cli, store, search_repo, search_ref="HEAD")
    out = lookup(cli, tmp_path, "widget conversion question here")[1]
    assert sorted(out["repo_hits"]) == ["docs/checkout/conversion-notes.md", "notes/scratch.md"]


def test_lookup_without_search_repo_skips_repo_search(cli, store, tmp_path):
    code, out = lookup(cli, tmp_path, "Why did checkout conversion drop?")
    assert code == 0, out
    assert out["repo_hits"] == [] and "repo_error" not in out
    assert out["domain"] is None and out["domains"] == ["general"]
    assert out["session_search_command"] is None


def test_lookup_stale_flag_follows_stale_days(cli, store, search_repo, tmp_path):
    setup_lookup_store(cli, store, search_repo, last_gathered=dt.date.today().isoformat())
    assert lookup(cli, tmp_path, "widget check please")[1]["models"][0]["stale"] is False
    old = (dt.date.today() - dt.timedelta(days=8)).isoformat()
    assert cli("save", "--model", "checkout-flow", stdin=make_model([("X", "Y")], model_id="checkout-flow",
                                                                  domain="checkout", last_gathered=old))[0] == 0
    assert lookup(cli, tmp_path, "widget check please")[1]["models"][0]["stale"] is True


def test_lookup_tie_and_no_hit_give_none(cli, store, search_repo, tmp_path):
    setup_lookup_store(cli, store, search_repo)
    code, out = lookup(cli, tmp_path, "Compare the widget with telemetry data")
    assert code == 0 and out["domain"] is None and out["domain_source"] == "none" and out["models"] == []
    code, out = lookup(cli, tmp_path, "Something unrelated entirely")
    assert out["domain"] is None and out["domain_source"] == "none"
    # Whole-word match only: 'widgets' does not count for keyword 'widget'.
    assert lookup(cli, tmp_path, "many widgets here")[1]["domain"] is None


def test_lookup_uses_domain_search_paths(cli, store, search_repo, tmp_path):
    setup_lookup_store(cli, store, search_repo)
    registry = yaml.safe_load((store / "registry.yaml").read_text(encoding="utf-8"))
    registry["domains"]["checkout"]["search_paths"] = ["other"]
    (store / "registry.yaml").write_text(yaml.safe_dump(registry), encoding="utf-8")
    assert lookup(cli, tmp_path, "widget conversion rows")[1]["repo_hits"] == ["other/ignored.md"]


def test_lookup_reports_repo_error(cli, store, tmp_path):
    write_config(store, search_repo=str(tmp_path / "no-repo"))
    code, out = lookup(cli, tmp_path, "conversion rows question")
    assert code == 0 and out["repo_hits"] == [] and out["repo_error"].startswith("repository grep failed")


def test_search_terms():
    terms = causal_store.search_terms("What about the Widget conversion rows and conversion masking in SDK? a b c d e f g h")
    assert terms == ["widget", "conversion", "rows", "masking"]
    many = causal_store.search_terms("alpha bravo charlie delta echoes foxtrot golfing hotel")
    assert len(many) == 6


def test_store_is_created_with_git_and_only_the_general_domain(cli, store):
    code, out = cli("has-check", "--ids", "x")
    assert code == 0 and out == {"missing": ["x"]}
    assert (store / ".git").exists() and (store / "config.yaml").exists() and (store / "registry.yaml").exists()
    config = yaml.safe_load((store / "config.yaml").read_text(encoding="utf-8"))
    assert config == causal_store.DEFAULT_CONFIG
    assert config["search_repo"] is None and config["session_search_command"] is None
    registry = yaml.safe_load((store / "registry.yaml").read_text(encoding="utf-8"))
    assert registry == {"domains": {"general": {"keywords": [], "models": []}}}


def test_usage_error_is_one_json_object(cli):
    code, out = cli("save")
    assert code == 2 and out["error"].startswith("usage:")


# ---------------------------------------------------------------- v0.2.0: variable roles (AC5)

SPEC_ROLES = ("exposure", "outcome", "mediator", "context", "selection", "measurement", "latent", "other")


def test_each_listed_role_and_missing_role_pass():
    for role in SPEC_ROLES:
        model = make_model([("X", "Y")])
        model["variables"][0]["role"] = role
        assert causal_store.validate_model(model) == [], role
    assert "role" not in make_model([("X", "Y")])["variables"][0]
    assert causal_store.validate_model(make_model([("X", "Y")])) == []


def test_unlisted_role_fails():
    model = make_model([("X", "Y")])
    model["variables"][0]["role"] = "confounder"
    errors = causal_store.validate_model(model)
    assert errors == ["variable X role must be one of " + ", ".join(SPEC_ROLES)]


def test_update_with_unlisted_role_is_rejected_and_changes_nothing(cli, store):
    assert save(cli, make_model([("X", "Y")]))[0] == 0
    before = (store / "models" / "m.yaml").read_text(encoding="utf-8")
    commits = commit_count(store)
    code, out = cli("update", "--model", "m", stdin={
        "facts": [{"text": "Z exists", "source": "s1"}],
        "add_variables": [{"id": "Z", "label": "Z", "definition": "variable Z", "role": "instrument"}]})
    assert code == 2 and any(e.startswith("variable Z role must be one of") for e in out["errors"]), out
    assert (store / "models" / "m.yaml").read_text(encoding="utf-8") == before
    assert commit_count(store) == commits
    assert not (store / "log.jsonl").exists()


# ---------------------------------------------------------------- v0.2.0: edge types (AC6)

def bidirected(a, b, edge_id=None, status="assumed"):
    return {"id": edge_id or f"{a}_bi_{b}", "from": a, "to": b, "type": "bidirected", "status": status,
            "reason": f"unmeasured common cause of {a} and {b}", "sources": ["s1"]}


def test_missing_edge_type_is_directed():
    model = make_model([("X", "Y")])
    assert "type" not in model["edges"][0]
    model["edges"].append(dict(model["edges"][0], id="X_Y_typed", type="directed"))
    errors = causal_store.validate_model(model)
    assert errors == ["duplicate edge X->Y (X_Y_typed)"]
    loop = make_model([("X", "Y")])
    loop["edges"].append(dict(loop["edges"][0], id="Y_X", **{"from": "Y", "to": "X"}))
    assert any(e.startswith("directed cycle:") for e in causal_store.validate_model(loop))


def test_unknown_edge_type_fails():
    model = make_model([("X", "Y")])
    model["edges"][0]["type"] = "undirected"
    assert causal_store.validate_model(model) == ["edge X_Y type must be one of directed, bidirected"]


def test_bidirected_self_edge_fails():
    model = make_model([("X", "Y")])
    model["edges"].append(bidirected("X", "X"))
    assert causal_store.validate_model(model) == ["edge X_bi_X goes from X to itself"]


def test_two_bidirected_edges_on_one_pair_fail_in_either_order():
    for second in (bidirected("X", "Y", "b2"), bidirected("Y", "X", "b2")):
        model = make_model([("X", "Y")])
        model["edges"] += [bidirected("X", "Y", "b1"), second]
        errors = causal_store.validate_model(model)
        assert errors == [f"duplicate bidirected edge {second['from']}<->{second['to']} (b2)"], errors


def test_directed_and_bidirected_edge_on_one_pair_pass():
    model = make_model([("X", "Y")])
    model["edges"].append(bidirected("X", "Y"))
    assert causal_store.validate_model(model) == []


def test_bidirected_edge_is_not_part_of_the_cycle_test():
    model = make_model([("B", "A")])
    model["edges"].append(bidirected("A", "B"))
    assert causal_store.validate_model(model) == []
    cyclic = make_model([("A", "B"), ("B", "C"), ("C", "A")])
    cyclic["edges"].append(bidirected("A", "C"))
    assert any(e.startswith("directed cycle:") for e in causal_store.validate_model(cyclic))


# ---------------------------------------------------------------- v0.2.0: checks with bidirected edges (AC7)

def with_bidirected(edges, bidirected_pairs, **kwargs):
    names = sorted({n for pair in [*edges, *bidirected_pairs] for n in pair})
    model = make_model(edges, variables=names, **kwargs)
    model["edges"] += [bidirected(a, b) for a, b in bidirected_pairs]
    assert causal_store.validate_model(model) == []
    return model


def test_bidirected_confounding_observational_does_not_follow():
    model = with_bidirected([("X", "Y")], [("X", "Y")])
    result = verdict(model, make_claim("X", "Y", [["X", "Y"]]))
    assert result["verdict"] == "does_not_follow"
    assert result["problems"] == ["open backdoor path given nothing: X <-> Y"]
    assert result["notes"] == [] and result["risky_edges"] == [{"id": "X_Y", "from": "X", "to": "Y", "status": "assumed"}]


def test_bidirected_confounding_randomized_follows():
    model = with_bidirected([("X", "Y")], [("X", "Y")])
    result = verdict(model, make_claim("X", "Y", [["X", "Y"]], design="randomized"))
    assert result["verdict"] == "follows" and result["problems"] == [] and result["notes"] == []


def test_front_door_claim_with_bidirected_confounding_does_not_follow():
    model = with_bidirected([("X", "M"), ("M", "Y")], [("X", "Y")])
    result = verdict(model, make_claim("X", "Y", [["X", "M", "Y"]]))
    assert result["verdict"] == "does_not_follow"
    assert result["problems"] == ["open backdoor path given nothing: X <-> Y"]


def test_bidirected_backdoor_blocked_by_conditioning_follows():
    model = with_bidirected([("X", "Y"), ("Z", "Y")], [("X", "Z")])
    result = verdict(model, make_claim("X", "Y", [["X", "Y"]], conditioned_on=["Z"]))
    assert result["verdict"] == "follows" and result["problems"] == [] and result["notes"] == []
    open_result = verdict(model, make_claim("X", "Y", [["X", "Y"]]))
    assert open_result["problems"] == ["open backdoor path given nothing: X <-> Z -> Y"]


def test_claimed_path_over_bidirected_edge_does_not_follow():
    model = with_bidirected([("M", "Y")], [("X", "M")])
    result = verdict(model, make_claim("X", "Y", [["X", "M", "Y"]], design="randomized"))
    assert result["verdict"] == "does_not_follow"
    assert result["problems"] == ["claimed path X->M->Y is not a directed path from X to Y in the model"]
    assert result["risky_edges"] == [{"id": "M_Y", "from": "M", "to": "Y", "status": "assumed"}]
    named = verdict(model, make_claim("X", "Y", [["X", "M", "Y"]], conditioned_on=["X_bi_M"], design="randomized"))
    assert "unknown variables: X_bi_M" in named["problems"]


def test_conditioning_on_latent_variable_does_not_follow():
    model = make_model([("U", "X"), ("U", "Y"), ("X", "Y")])
    model["variables"][0]["role"] = "latent"
    assert model["variables"][0]["id"] == "U"
    for design in ("observational", "randomized"):
        result = verdict(model, make_claim("X", "Y", [["X", "Y"]], conditioned_on=["U"], design=design))
        assert result["verdict"] == "does_not_follow", design
        assert result["problems"] == [
            "conditioned_on contains latent variables: U (an unmeasured variable cannot be conditioned on)"]


# ---------------------------------------------------------------- v0.2.0: export (AC8)

def export_model():
    quoted = 'q"x'
    model = make_model([("X", "Y"), ("store size", "X"), ("X", quoted), (quoted, "Y"), ("U", "Y")],
                       variables=["X", "Y", "U", "store size", quoted],
                       statuses={("X", "Y"): "known", ("store size", "X"): "unknown", ("U", "Y"): "known"})
    roles = {"X": "exposure", "Y": "outcome", "U": "latent", "store size": "context", quoted: "mediator"}
    labels = {"X": "Ad spend", "Y": 'Sales "net"', "U": "Unmeasured demand", "store size": "Store size",
              quoted: "Path\\step"}
    for var in model["variables"]:
        var["role"] = roles[var["id"]]
        var["label"] = labels[var["id"]]
    model["edges"].append(bidirected("X", "Y", "X_bi_Y"))
    return model


EXPECTED_DAGITTY = "\n".join([
    "dag {",
    "X [exposure]",
    "Y [outcome]",
    "U [latent]",
    '"store size"',
    r'"q\"x"',
    "X -> Y",
    '"store size" -> X',
    r'X -> "q\"x"',
    r'"q\"x" -> Y',
    "U -> Y",
    "X <-> Y",
    "}",
])

EXPECTED_DOT = "\n".join([
    'digraph "m" {',
    '"X" [label="Ad spend"]',
    r'"Y" [label="Sales \"net\""]',
    '"U" [label="Unmeasured demand"]',
    '"store size" [label="Store size"]',
    r'"q\"x" [label="Path\\step"]',
    '"X" -> "Y" [style=solid]',
    '"store size" -> "X" [style=dotted]',
    r'"X" -> "q\"x" [style=dashed]',
    r'"q\"x" -> "Y" [style=dashed]',
    '"U" -> "Y" [style=solid]',
    '"X" -> "Y" [style=dashed, dir=both]',
    "}",
])


def git_state(store):
    head = subprocess.run(["git", "-C", str(store), "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
    status = subprocess.run(["git", "-C", str(store), "status", "--porcelain", "--untracked-files=all"],
                            capture_output=True, text=True, check=True)
    return head.stdout, status.stdout


def test_export_dagitty_and_dot_text_and_store_unchanged(cli, store):
    assert save(cli, export_model())[0] == 0
    before = git_state(store)
    model_text = (store / "models" / "m.yaml").read_text(encoding="utf-8")
    code, out = cli("export", "--model", "m", "--format", "dagitty")
    assert code == 0 and out == {"model": "m", "revision": 1, "format": "dagitty", "text": EXPECTED_DAGITTY}
    code, out = cli("export", "--model", "m", "--format", "dot")
    assert code == 0 and out == {"model": "m", "revision": 1, "format": "dot", "text": EXPECTED_DOT}
    assert git_state(store) == before and before[1] == ""
    assert (store / "models" / "m.yaml").read_text(encoding="utf-8") == model_text


def test_export_unknown_format_exits_1_and_invalid_model_exits_2(cli, store):
    assert save(cli, make_model([("X", "Y")]))[0] == 0
    code, out = cli("export", "--model", "m", "--format", "graphml")
    assert code == 1 and out["error"].startswith("usage: unknown format graphml")
    path = store / "models" / "m.yaml"
    broken = yaml.safe_load(path.read_text(encoding="utf-8"))
    broken["variables"][0]["role"] = "confounder"
    path.write_text(yaml.safe_dump(broken), encoding="utf-8")
    code, out = cli("export", "--model", "m", "--format", "dot")
    assert code == 2 and any("role must be one of" in e for e in out["errors"])
