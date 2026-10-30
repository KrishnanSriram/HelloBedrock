#!/usr/bin/env python3
"""
Build reports from the results that run_tests.py saves in results/.

Usage:
    python3 report.py                          # report on the latest run
    python3 report.py results/run_X.json       # report on a specific run
    python3 report.py results/A.json results/B.json   # compare two or more runs

Writes to reports/:
    one run   -> report_<label>_<time>.md  and  report_<label>_<time>.csv (for spreadsheets)
    several   -> compare_<time>.md
Needs only Python 3 (no pip installs).
"""
import csv
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(HERE, "results")
REPORTS_DIR = os.path.join(HERE, "reports")


# ------------------------------------------------------------------ helpers
def load(path):
    with open(path) as f:
        run = json.load(f)
    run["file"] = os.path.basename(path)
    return run


def cell(text, limit=140):
    """Make text safe for one Markdown table cell."""
    text = str(text or "").replace("\n", " ").replace("|", "/")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def tokens(r):
    u = r.get("usage") or {}
    return u.get("inputTokens", 0) or 0, u.get("outputTokens", 0) or 0


def layer(r):
    """Which layer of the system decided the outcome of this request."""
    status, inp_out = r["status"], tokens(r)
    if status in (400, 413):
        return "Input validation (Lambda)"
    if r.get("stop_reason") == "precheck_blocked":
        return "Blocked before the model (precheck)"
    if r["blocked"] and inp_out == (0, 0):
        return "Blocked by guardrail on input"
    if r["blocked"]:
        return "Blocked by guardrail on output"
    if any(h.endswith(":ANONYMIZED") for h in r["guardrail_hits"]):
        return "Masked, then answered"
    if status == 200:
        return "Answered by the model"
    return f"Error (HTTP {status})"


def by_category(results):
    groups = defaultdict(list)
    for r in results:
        groups[r["category"]].append(r)
    return groups


# --------------------------------------------------------------- one run
def single_report(run):
    results = run["results"]
    total, passed = len(results), sum(r["passed"] for r in results)
    failed = [r for r in results if not r["passed"]]
    tin = sum(tokens(r)[0] for r in results)
    tout = sum(tokens(r)[1] for r in results)
    latencies = sorted(r["latency_ms"] for r in results)
    median = latencies[len(latencies) // 2] if latencies else 0

    lines = [
        f"# Guardrail test report: {run['label']}",
        "",
        f"**{passed}/{total} passed, {len(failed)} failed**",
        "",
        f"- Run at: {run['run_at']}",
        f"- API: {run['api']}",
        f"- Tokens: {tin:,} input, {tout:,} output",
        f"- Latency: median {median:,} ms, slowest {latencies[-1] if latencies else 0:,} ms",
        f"- Results file: {run['file']}",
        "",
        "## By category",
        "",
        "| Category | Passed | Failed | Median latency (ms) |",
        "|---|---|---|---|",
    ]
    for cat, rs in by_category(results).items():
        p = sum(r["passed"] for r in rs)
        lat = sorted(r["latency_ms"] for r in rs)
        lines.append(f"| {cat} | {p}/{len(rs)} | {len(rs) - p} | {lat[len(lat) // 2]:,} |")

    lines += ["", "## Which layer handled each request", "",
              "| Layer | Requests |", "|---|---|"]
    for name, n in Counter(layer(r) for r in results).most_common():
        lines.append(f"| {name} | {n} |")

    hit_counts = Counter(h for r in results for h in r["guardrail_hits"])
    if hit_counts:
        lines += ["", "## Guardrail policies that fired", "", "| Policy | Times |", "|---|---|"]
        for name, n in hit_counts.most_common():
            lines.append(f"| {name} | {n} |")

    lines += ["", "## Failures", ""]
    if not failed:
        lines.append("None.")
    else:
        lines += ["| ID | Test | Expected | HTTP | Problem | Reply |", "|---|---|---|---|---|---|"]
        for r in failed:
            lines.append(f"| {r['id']} | {cell(r['name'])} | {r['expected']} | {r['status']} | "
                         f"{cell('; '.join(r['problems']))} | {cell(r['reply'], 160)} |")

    lines += ["", "## All tests", "",
              "| Result | ID | Category | Test | Expected | HTTP | Handled by | Guardrail hits | Tokens in/out | ms |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        tin_r, tout_r = tokens(r)
        lines.append(f"| {'PASS' if r['passed'] else 'FAIL'} | {r['id']} | {r['category']} | {cell(r['name'])} | "
                     f"{r['expected']} | {r['status']} | {layer(r)} | {cell(', '.join(r['guardrail_hits']) or '-')} | "
                     f"{tin_r}/{tout_r} | {r['latency_ms']:,} |")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(REPORTS_DIR, f"report_{run['label']}_{stamp}")
    with open(base + ".md", "w") as f:
        f.write("\n".join(lines) + "\n")

    with open(base + ".csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["result", "id", "category", "test", "expected", "http", "handled_by",
                    "guardrail_hits", "input_tokens", "output_tokens", "latency_ms", "problems", "reply"])
        for r in results:
            tin_r, tout_r = tokens(r)
            w.writerow(["PASS" if r["passed"] else "FAIL", r["id"], r["category"], r["name"],
                        r["expected"], r["status"], layer(r), ", ".join(r["guardrail_hits"]),
                        tin_r, tout_r, r["latency_ms"], "; ".join(r["problems"]), r["reply"]])

    # Console summary
    print(f"{run['label']}: {passed}/{total} passed, {len(failed)} failed  ({run['run_at']})")
    for cat, rs in by_category(results).items():
        print(f"  {cat:<18} {sum(r['passed'] for r in rs)}/{len(rs)}")
    for r in failed:
        print(f"  FAIL {r['id']}  {r['name']}: {'; '.join(r['problems'])}")
    print(f"\nReport: {base}.md\nCSV:    {base}.csv")


# ---------------------------------------------------------- several runs
def compare_report(runs):
    labels = [f"{r['label']} ({r['run_at'][5:16].replace('T', ' ')})" for r in runs]
    index = [{x["id"]: x for x in run["results"]} for run in runs]
    ids = list(dict.fromkeys(x["id"] for run in runs for x in run["results"]))

    lines = ["# Guardrail test comparison", "", "## Summary", "",
             "| Run | Passed | Tokens in | Tokens out | Median latency (ms) |", "|---|---|---|---|---|"]
    for label, run in zip(labels, runs):
        rs = run["results"]
        lat = sorted(r["latency_ms"] for r in rs)
        lines.append(f"| {label} | {sum(r['passed'] for r in rs)}/{len(rs)} | "
                     f"{sum(tokens(r)[0] for r in rs):,} | {sum(tokens(r)[1] for r in rs):,} | "
                     f"{lat[len(lat) // 2] if lat else 0:,} |")

    def outcome(r):
        if r is None:
            return "—"
        return f"{'PASS' if r['passed'] else 'FAIL'} · {r['status']} · {layer(r)}"

    differ = [i for i in ids
              if len({(idx.get(i) or {}).get("passed") for idx in index}) > 1
              or len({layer(idx[i]) if i in idx else None for idx in index}) > 1]

    lines += ["", f"## Tests that differ between runs ({len(differ)})", ""]
    if not differ:
        lines.append("None: every test had the same result and was handled by the same layer.")
    else:
        lines += ["| ID | Test | " + " | ".join(labels) + " |",
                  "|---|---|" + "---|" * len(labels)]
        for i in differ:
            name = next(idx[i]["name"] for idx in index if i in idx)
            lines.append(f"| {i} | {cell(name)} | " + " | ".join(outcome(idx.get(i)) for idx in index) + " |")
        lines += ["", "### Replies for the tests that differ", ""]
        for i in differ:
            name = next(idx[i]["name"] for idx in index if i in idx)
            lines.append(f"**{i} {name}**")
            lines.append("")
            for label, idx in zip(labels, index):
                reply = idx[i]["reply"] if i in idx else "(not run)"
                lines.append(f"- *{label}:* {cell(reply, 300)}")
            lines.append("")

    lines += ["", "## All tests", "",
              "| ID | Category | Test | " + " | ".join(labels) + " |",
              "|---|---|---|" + "---|" * len(labels)]
    for i in ids:
        first = next(idx[i] for idx in index if i in idx)
        lines.append(f"| {i} | {first['category']} | {cell(first['name'])} | " +
                     " | ".join(outcome(idx.get(i)) for idx in index) + " |")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(REPORTS_DIR, f"compare_{stamp}.md")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")

    for label, run in zip(labels, runs):
        print(f"{label}: {run['passed']}/{run['total']} passed")
    print(f"{len(differ)} tests differ between runs")
    for i in differ:
        print(f"  {i}: " + "  vs  ".join(outcome(idx.get(i)) for idx in index))
    print(f"\nComparison: {path}")


# -------------------------------------------------------------------- main
def main():
    files = sys.argv[1:]
    if not files:
        found = sorted(glob.glob(os.path.join(RESULTS_DIR, "*.json")), key=os.path.getmtime)
        if not found:
            sys.exit("No results yet. Run: python3 run_tests.py")
        files = [found[-1]]
    os.makedirs(REPORTS_DIR, exist_ok=True)
    runs = [load(p) for p in files]
    if len(runs) == 1:
        single_report(runs[0])
    else:
        compare_report(runs)


if __name__ == "__main__":
    main()
