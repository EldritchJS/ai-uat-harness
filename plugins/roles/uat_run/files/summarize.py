#!/usr/bin/env python3
# Assisted by Claude Opus
"""Merge a run's step records into steps.json, timesheet.csv and summary.txt.

Exits 1 when any step failed, timed out or errored, or when the JUnit reports
contain failures; 0 otherwise.
"""

import csv
import glob
import json
import os
import sys
from datetime import UTC, datetime

BAD = {"failed", "timeout", "error"}
COLUMNS = [
    "run",
    "test_id",
    "test",
    "mode",
    "sequence",
    "nodes",
    "step",
    "sweep",
    "kind",
    "namespace",
    "name",
    "status",
    "started",
    "finished",
    "duration_s",
    "results_dir",
    "logs",
]


def load(pattern):
    items = []
    for path in sorted(glob.glob(pattern)):
        with open(path) as f:
            items.append(json.load(f))
    return items


def main():
    run_dir = sys.argv[1]
    meta_path = os.path.join(run_dir, "meta", "run.json")
    meta = {}
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)

    steps = load(os.path.join(run_dir, "steps", "*.json"))
    steps.sort(key=lambda s: (s.get("started", ""), s.get("dir", "")))
    tests = load(os.path.join(run_dir, "tests", "*.json"))
    reports = {}
    for path in sorted(
        glob.glob(os.path.join(run_dir, "results", "*", "report", "summary.json"))
    ):
        role = path.split(os.sep)[-3]
        with open(path) as f:
            reports[role] = json.load(f)

    with open(os.path.join(run_dir, "steps.json"), "w") as f:
        json.dump(steps, f, indent=2)

    with open(os.path.join(run_dir, "timesheet.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        for s in steps:
            row = dict(s)
            row["nodes"] = " ".join(s.get("nodes", []))
            writer.writerow([row.get(c, "") for c in COLUMNS])

    bad_steps = [s for s in steps if s.get("status") in BAD]
    junit_bad = any(r.get("status") == "failed" for r in reports.values())
    test_steps = [
        s for s in steps if s.get("kind") not in ("Build", "Collect", "Teardown")
    ]
    if bad_steps or junit_bad:
        overall = "failed"
    elif test_steps and all(s.get("status") == "rendered" for s in test_steps):
        overall = "rendered"
    else:
        overall = "passed"

    lines = [
        f"run      {meta.get('run_id', os.path.basename(run_dir))}",
        f"cluster  {meta.get('cluster', '')}",
        f"status   {overall.upper()}",
        "",
        f"{'test':28} {'mode':8} {'sequences':>9} {'steps':>6} {'passed':>7} {'failed':>7}  status",
    ]
    for t in sorted(tests, key=lambda t: t.get("test_id", 0)):
        own = [
            s
            for s in steps
            if s.get("tid") == t.get("tid") and s.get("kind") not in ("Teardown",)
        ]
        failed = [s for s in own if s.get("status") in BAD]
        rendered = [s for s in own if s.get("status") == "rendered"]
        status = t.get("status", "")
        if status != "skipped":
            if failed:
                status = "failed"
            elif own and len(rendered) == len(own):
                status = "rendered"
            else:
                status = "passed" if own else "no steps"
        name = f"{t.get('tid', '')} {t.get('test', '')}"
        lines.append(
            f"{name:28} {t.get('mode', ''):8} {len(t.get('sequences', [])):>9} "
            f"{len(own):>6} {len(own) - len(failed) - len(rendered):>7} {len(failed):>7}  {status}"
        )
    if bad_steps:
        lines += ["", "Failed steps:"]
        for s in bad_steps:
            lines.append(
                f"  {s.get('dir', '')}  {s.get('status')}  {s.get('reason', '')}  logs: {s.get('logs', '')}"
            )
    for role, report in reports.items():
        totals = report.get("totals", {})
        lines += [
            "",
            (
                f"JUnit ({role}): {report.get('status', '')}  tests={totals.get('tests', 0)} "
                f"failures={totals.get('failures', 0)} errors={totals.get('errors', 0)} "
                f"skipped={totals.get('skipped', 0)}"
            ),
        ]
        for e in report.get("entries", []):
            if e.get("status") != "passed":
                lines.append(
                    f"  {e['name']}  failures={e['failures']} errors={e['errors']}"
                )
    text = "\n".join(lines) + "\n"
    with open(os.path.join(run_dir, "summary.txt"), "w") as f:
        f.write(text)

    meta["finished"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta["status"] = overall
    meta["steps"] = len(steps)
    meta["failed_steps"] = len(bad_steps)
    if os.path.isdir(os.path.dirname(meta_path)):
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)

    print(text, end="")
    sys.exit(1 if overall == "failed" else 0)


if __name__ == "__main__":
    main()
