#!/usr/bin/env python3
"""
Run every case in test_cases.json against the chat API and print a report.

Usage:
    source env.sh
    python3 run_tests.py

Needs only Python 3 (no pip installs). Reads the API from $API_URL.
Writes a report file: test_report_<timestamp>.md
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

    with open(CASES_FILE) as f:
        cases = json.load(f)["cases"]

    print(f"Running {len(cases)} tests against {url}\n")
    results = []
    for case in cases:
        prompt = case.get("prompt", "")
        if "prompt_repeat" in case:
            prompt = case["prompt_repeat"]["text"] * case["prompt_repeat"]["times"]

        status, body = call_api(url, prompt)
        problems = check(case, status, body)
        passed = not problems
        hits = ", ".join(body.get("guardrail_hits") or []) or "-"
        reply = (body.get("response") or body.get("error") or "").replace("\n", " ")

        print(f"[{'PASS' if passed else 'FAIL'}] {case['id']:<4} {case['name']:<38} "
              f"HTTP {status}  hits: {hits}")
        if not passed:
            print(f"       problem: {'; '.join(problems)}")
            print(f"       reply:   {reply[:160]}")

        results.append({"case": case, "status": status, "hits": hits,
                        "reply": reply, "passed": passed, "problems": problems})
        time.sleep(0.2)  # stay under the API throttle

    write_report(url, results)


def write_report(url, results):
    total = len(results)
    passed = sum(r["passed"] for r in results)
    failed = [r for r in results if not r["passed"]]

    # Summary by category
    groups = {}
    for r in results:
        g = groups.setdefault(r["case"]["category"], [0, 0])
        g[0] += r["passed"]
        g[1] += 1

    print("\n" + "=" * 60)
    print(f"RESULT: {passed}/{total} passed, {len(failed)} failed")
    print("=" * 60)
    for name, (p, t) in groups.items():
        print(f"  {name:<18} {p}/{t}")
    if failed:
        print("\nFailed tests:")
        for r in failed:
            print(f"  {r['case']['id']}  {r['case']['name']}: {'; '.join(r['problems'])}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(HERE, f"test_report_{stamp}.md")
    with open(path, "w") as f:
        f.write(f"# Guardrail test report\n\n")
        f.write(f"- Run at: {datetime.now():%Y-%m-%d %H:%M:%S}\n- API: {url}\n")
        f.write(f"- Result: **{passed}/{total} passed, {len(failed)} failed**\n\n")
        f.write("## By category\n\n| Category | Passed |\n|---|---|\n")
        for name, (p, t) in groups.items():
            f.write(f"| {name} | {p}/{t} |\n")
        if failed:
            f.write("\n## Failures\n\n| ID | Test | Problem | Reply |\n|---|---|---|---|\n")
            for r in failed:
                f.write(f"| {r['case']['id']} | {r['case']['name']} | {'; '.join(r['problems'])} "
                        f"| {r['reply'][:120].replace('|', '/')} |\n")
        f.write("\n## All tests\n\n| Result | ID | Test | Expected | HTTP | Guardrail hits |\n"
                "|---|---|---|---|---|---|\n")
        for r in results:
            f.write(f"| {'PASS' if r['passed'] else 'FAIL'} | {r['case']['id']} | {r['case']['name']} "
                    f"| {r['case']['expect']['outcome']} | {r['status']} | {r['hits']} |\n")
    print(f"\nReport saved: {path}")
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()