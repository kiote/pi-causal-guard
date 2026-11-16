"""Fake bin/causal_store.py for the causal-guard extension tests. Returns canned JSON.

Each call is appended as one JSON line to the file in env FAKE_CAUSAL_STORE_LOG (if set),
so tests can see which subcommands ran. Known check ids start with "fake-model@r1#".
A prompt that contains FAIL_LOOKUP makes lookup exit 1; a prompt that contains NO_SEARCH_COMMAND gives
session_search_command null; a model with "invalid": true makes save exit 2.
"""
import json
import os
import sys

KNOWN_PREFIX = "fake-model@r1#"
SESSION_SEARCH_COMMAND = "python tools/session_search.py --recent"


def main(argv):
    cmd, args = argv[0], argv[1:]
    opts = {args[i]: args[i + 1] for i in range(0, len(args) - 1, 2) if args[i].startswith("--")}
    stdin = sys.stdin.read()
    payload = json.loads(stdin) if stdin.strip() else None
    log = os.environ.get("FAKE_CAUSAL_STORE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"cmd": cmd, "opts": opts, "stdin": payload}) + "\n")

    if cmd == "lookup":
        with open(opts["--prompt-file"], encoding="utf-8") as f:
            prompt = f.read()
        if "FAIL_LOOKUP" in prompt:
            print("fake lookup failure", file=sys.stderr)
            return 1
        out = {
            "domain": "checkout",
            "domain_source": "keyword",
            "domains": ["checkout", "general"],
            "models": [{"id": "fake-model", "path": "/store/models/fake-model.yaml", "revision": 1,
                        "last_gathered": "2026-11-10", "stale": False}],
            "repo_hits": ["docs/checkout/notes.md"],
            "session_search_command": None if "NO_SEARCH_COMMAND" in prompt else SESSION_SEARCH_COMMAND,
        }
    elif cmd == "check":
        model = opts["--model"]
        out = {"model": model, "revision": 1, "results": [
            {"check_id": f"{model}@r1#{c['id']}-abcdef", "claim_id": c["id"], "verdict": "follows",
             "problems": [], "notes": [], "risky_edges": []}
            for c in payload["claims"]]}
    elif cmd == "has-check":
        ids = [i for i in opts["--ids"].split(",") if i]
        out = {"missing": [i for i in ids if not i.startswith(KNOWN_PREFIX)]}
    elif cmd == "save":
        if payload.get("invalid"):
            print("validation: edge e1 is dangling", file=sys.stderr)
            return 2
        out = {"id": opts["--model"], "revision": 1, "path": f"/store/models/{opts['--model']}.yaml", "created": True}
    elif cmd == "update":
        out = {"id": opts["--model"], "revision": 2, "diff": ["+ fact: " + f["text"] for f in payload.get("facts", [])]}
    else:
        print(f"unknown subcommand {cmd}", file=sys.stderr)
        return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
