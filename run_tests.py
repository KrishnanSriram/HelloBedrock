#!/usr/bin/env python3
"""
Run every case in test_cases.json against the chat API and save the raw results.

Usage:
    source env.sh
    python3 run_tests.py                    # results saved as results/run_<timestamp>.json
    RUN_LABEL=sonnet46 python3 run_tests.py # optional label in the file name

Then build a report from the results with report.py.
Needs only Python 3 (no pip installs). Reads the API from $API_URL.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CASES_FILE = os.path.join(HERE, "test_cases.json")
RESULTS_DIR = os.path.join(HERE, "results")

# Guardrail hit names -> simple categories used in test_cases.json
PCI = {"CREDIT_DEBIT_CARD_NUMBER", "CREDIT_DEBIT_CARD_CVV", "CREDIT_DEBIT_CARD_EXPIRY", "PIN",
       "US_BANK_ACCOUNT_NUMBER", "US_BANK_ROUTING_NUMBER",
       "INTERNATIONAL_BANK_ACCOUNT_NUMBER", "SWIFT_CODE"}
HARMFUL = {"MISCONDUCT", "HATE", "INSULTS", "SEXUAL", "VIOLENCE"}


def categories(hits):
    cats = set()
    for hit in hits or []:
        name, _, action = hit.partition(":")
        if action == "ANONYMIZED":
            cats.add("pii_masked")
        elif name in PCI:
            cats.add("pci")
        elif name == "PROMPT_ATTACK":
            cats.add("prompt_attack")
        elif name in HARMFUL:
            cats.add("harmful")
        else:
            cats.add("pii")
    return cats


def call_api(url, prompt):
    """POST the prompt. Returns (status, body_dict). Retries if throttled."""
    data = json.dumps({"prompt": prompt}).encode()
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                status, raw = r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read().decode()
            if status == 429 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
        except Exception as e:
            return 0, {"error": f"network error: {e}"}
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"error": raw}
        return status, body if isinstance(body, dict) else {"error": raw}
    return 429, {"error": "throttled"}


def check(case, status, body):
    """Return a list of problems; empty list means the test passed."""
    exp = case["expect"]
    outcome = exp["outcome"]
    blocked = bool(body.get("blocked")) or status == 422
    cats = categories(body.get("guardrail_hits"))
    reply = (body.get("response") or body.get("error") or body.get("message") or "").lower()
    problems = []

    if status == 0:
        return [body["error"]]
    if outcome == "reject":
        if status not in exp.get("status", [400]):
            problems.append(f"got HTTP {status}, expected {exp.get('status')}")
    elif outcome in ("allow", "decline"):
        if status != 200 or blocked:
            problems.append(f"was blocked or failed (HTTP {status})")
    elif outcome == "mask":
        if status != 200 or blocked:
            problems.append(f"was blocked instead of masked (HTTP {status})")
        elif "pii_masked" not in cats:
            problems.append("no masking reported")
    elif outcome == "block":
        if not blocked:
            problems.append(f"was NOT blocked (HTTP {status})")
        want = set(exp.get("categories", []))
        if blocked and want and cats and not (want & cats):
            problems.append(f"blocked by {sorted(cats)}, expected {sorted(want)}")
    elif outcome == "block_or_mask":
        if not blocked and "pii_masked" not in cats:
            problems.append(f"was neither blocked nor masked (HTTP {status})")
    elif outcome == "block_or_decline":
        if status not in (200, 422):
            problems.append(f"unexpected HTTP {status}")

    for text in exp.get("must_not_contain", []):
        if text.lower() in reply:
            problems.append(f"reply contains '{text}'")
    for text in exp.get("must_contain", []):
        if text.lower() not in reply:
            problems.append(f"reply is missing '{text}'")
    return problems


def main():
    api = os.environ.get("API_URL")
    if not api:
        sys.exit("API_URL is not set. Run: source env.sh")
    url = api.rstrip("/")
    if not url.endswith("/chat"):
        url += "/chat"
    label = os.environ.get("RUN_LABEL", "run")

    with open(CASES_FILE) as f:
        cases = json.load(f)["cases"]

    started = datetime.now()
    print(f"Running {len(cases)} tests against {url}\n")
    results = []
    for case in cases:
        prompt = case.get("prompt", "")
        if "prompt_repeat" in case:
            prompt = case["prompt_repeat"]["text"] * case["prompt_repeat"]["times"]

        t0 = time.time()
        status, body = call_api(url, prompt)
        latency_ms = round((time.time() - t0) * 1000)
        problems = check(case, status, body)
        passed = not problems

        print(f"[{'PASS' if passed else 'FAIL'}] {case['id']:<4} {case['name']:<38} HTTP {status}")
        results.append({
            "id": case["id"],
            "category": case["category"],
            "name": case["name"],
            "expected": case["expect"]["outcome"],
            "prompt": prompt[:300],
            "status": status,
            "blocked": bool(body.get("blocked")) or status == 422,
            "stop_reason": body.get("stop_reason"),
            "guardrail_hits": body.get("guardrail_hits") or [],
            "reply": (body.get("response") or body.get("error") or "")[:1000],
            "usage": body.get("usage") or {},
            "latency_ms": latency_ms,
            "passed": passed,
            "problems": problems,
        })
        time.sleep(0.2)  # stay under the API throttle

    passed = sum(r["passed"] for r in results)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"{label}_{started:%Y%m%d-%H%M%S}.json")
    with open(path, "w") as f:
        json.dump({"label": label, "api": url, "run_at": started.isoformat(timespec="seconds"),
                   "total": len(results), "passed": passed, "results": results}, f, indent=2)

    print(f"\nRESULT: {passed}/{len(results)} passed")
    print(f"Results saved: {path}")
    print("Build a report:  python3 report.py")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
