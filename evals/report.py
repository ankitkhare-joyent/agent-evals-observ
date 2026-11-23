"""Turn per-run rows into numbers you can act on: pass rates with confidence intervals, reliability
(pass@k vs pass^k), cost and latency, and a CI gate.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from xml.sax.saxutils import escape


def wilson(passed: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval for a pass rate. With 20 runs, 80% observed means "somewhere in 58-92%"."""
    if n == 0:
        return 0.0, 0.0
    p = passed / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def summarize(rows: list[dict]) -> dict:
    """rows: one per (variant, case, rep) that produced an answer. Errors are reported separately."""
    out: dict = {"variants": {}}
    for variant in sorted({r["variant"] for r in rows}):
        vr = [r for r in rows if r["variant"] == variant]
        metrics: dict[str, list[bool]] = defaultdict(list)
        for r in vr:
            for g in r["grades"]:
                metrics[g["name"]].append(g["passed"])
            metrics["overall"].append(r["passed"])
        by_case: dict[str, list[bool]] = defaultdict(list)
        for r in vr:
            by_case[r["case_id"]].append(r["passed"])
        by_cat: dict[str, list[bool]] = defaultdict(list)
        for r in vr:
            by_cat[r["category"]].append(r["passed"])
        lat = [r["trajectory"]["latency_s"] for r in vr]
        cost = [r["trajectory"]["cost_usd"] for r in vr]
        out["variants"][variant] = {
            "runs": len(vr),
            "metrics": {k: {"passed": sum(v), "n": len(v), "rate": sum(v) / len(v), "ci95": wilson(sum(v), len(v))}
                        for k, v in sorted(metrics.items())},
            "by_category": {k: {"passed": sum(v), "n": len(v)} for k, v in sorted(by_cat.items())},
            # pass@k: the case succeeded at least once in k tries (capability).
            # pass^k: it succeeded every time (reliability - what a user experiences on a re-ask).
            "pass_at_k": statistics.mean(any(v) for v in by_case.values()) if by_case else 0,
            "pass_pow_k": statistics.mean(all(v) for v in by_case.values()) if by_case else 0,
            "k": max((len(v) for v in by_case.values()), default=0),
            "latency_p50_s": statistics.median(lat) if lat else 0,
            "latency_p95_s": sorted(lat)[max(0, math.ceil(0.95 * len(lat)) - 1)] if lat else 0,
            "cost_per_run_usd": statistics.mean(cost) if cost else 0,
            "cost_total_usd": sum(cost),
            "tokens_in": sum(r["trajectory"]["input_tokens"] for r in vr),
            "tokens_out": sum(r["trajectory"]["output_tokens"] for r in vr),
            "judge_cost_usd": sum(r.get("judge_cost_usd", 0) for r in vr),
        }
    return out


def check_gates(summary: dict, gates: dict[str, float]) -> list[str]:
    failures = []
    for variant, s in summary["variants"].items():
        for metric, threshold in gates.items():
            m = s["metrics"].get(metric)
            if m and m["rate"] < threshold:
                failures.append(f"{variant}: {metric} {pct(m['rate'])} < gate {pct(threshold)}")
    return failures


def markdown(run_id: str, summary: dict, rows: list[dict], errors: list[dict], gate_failures: list[str]) -> str:
    lines = [f"# Eval run `{run_id}`", ""]
    lines.append("**Gate: FAILED**" if gate_failures else "**Gate: passed**")
    lines += [f"- {f}" for f in gate_failures]
    if errors:
        lines += ["", f"**{len(errors)} run(s) hit infrastructure errors and were not scored** (see errors.jsonl)."]
    for variant, s in summary["variants"].items():
        lines += ["", f"## Variant `{variant}` ({s['runs']} scored runs)", "",
                  "| metric | pass rate | 95% CI | passed / n |", "|---|---|---|---|"]
        for name, m in s["metrics"].items():
            lo, hi = m["ci95"]
            lines.append(f"| {name} | {pct(m['rate'])} | {pct(lo)}-{pct(hi)} | {m['passed']} / {m['n']} |")
        lines += ["", f"- Reliability over k={s['k']} reps: pass@k {pct(s['pass_at_k'])}, pass^k {pct(s['pass_pow_k'])}",
                  f"- Latency p50 {s['latency_p50_s']:.1f}s, p95 {s['latency_p95_s']:.1f}s",
                  (f"- Agent cost ${s['cost_total_usd']:.3f} total, ${s['cost_per_run_usd']:.4f} per run "
                   f"({s['tokens_in']:,} tokens in, {s['tokens_out']:,} out); judge ${s['judge_cost_usd']:.3f}"),
                  "", "| category | passed / n |", "|---|---|"]
        lines += [f"| {k} | {v['passed']} / {v['n']} |" for k, v in s["by_category"].items()]
    failed = [r for r in rows if not r["passed"]]
    if failed:
        lines += ["", "## Failed runs", ""]
        for r in failed:
            bad = [g for g in r["grades"] if not g["passed"]]
            lines.append(f"### {r['variant']} / {r['case_id']} (rep {r['rep']})")
            lines.append(f"> {r['question']}")
            lines.append("")
            lines += [f"- **{g['name']}**: {g['detail']}" for g in bad]
            lines.append(f"- trace: {r.get('trace_url', '')}")
            lines.append("")
    return "\n".join(lines) + "\n"


def junit(rows: list[dict], errors: list[dict]) -> str:
    """JUnit XML so CI systems (GitHub Actions, GitLab, Jenkins) show each case as a test."""
    cases = []
    for r in rows:
        name = escape(f"{r['variant']}.{r['case_id']}.rep{r['rep']}")
        if r["passed"]:
            cases.append(f'<testcase classname="evals" name="{name}" time="{r["trajectory"]["latency_s"]}"/>')
        else:
            msg = escape("; ".join(f"{g['name']}: {g['detail'][:200]}" for g in r["grades"] if not g["passed"]))
            cases.append(f'<testcase classname="evals" name="{name}"><failure message="{msg}"/></testcase>')
    for e in errors:
        name = escape(f"{e['variant']}.{e['case_id']}.rep{e['rep']}")
        cases.append(f'<testcase classname="evals" name="{name}"><error message="{escape(e["error"][:300])}"/></testcase>')
    failures = sum(not r["passed"] for r in rows)
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<testsuite name="agent-evals" tests="{len(cases)}" '
            f'failures="{failures}" errors="{len(errors)}">\n' + "\n".join(cases) + "\n</testsuite>\n")
