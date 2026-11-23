"""Calibrate the LLM judge against your own labels. A judge you haven't checked is a random number generator
with good grammar.

    mise run calibrate -- export reports/<run_id>      # writes evals/labels/<run_id>.yaml for you to fill in
    (edit the file: set human_correctness to pass or fail for each item, without looking at the judge)
    mise run calibrate -- compare evals/labels/<run_id>.yaml

`compare` reports raw agreement and Cohen's kappa (agreement corrected for chance) between you and the
judge, and lists every disagreement. Rules of thumb: below ~0.9 agreement on clear-cut items, fix the judge
prompt before trusting its numbers; kappa above ~0.8 is strong. Re-check whenever you change the judge
prompt or model.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CRITERION = "judge_correctness"


def export(report_dir: Path) -> None:
    rows = [json.loads(line) for line in (report_dir / "results.jsonl").read_text().splitlines()]
    cases = {c["id"]: c for c in json.loads((report_dir / "cases.json").read_text())}
    items = [{"case_id": r["case_id"], "rep": r["rep"], "question": r["question"],
              "reference": cases[r["case_id"]]["reference"], "answer": r["answer"],
              "human_correctness": None} for r in rows]
    out = ROOT / "evals" / "labels" / f"{report_dir.name}.yaml"
    out.parent.mkdir(exist_ok=True)
    out.write_text(yaml.safe_dump({"report": str(report_dir.relative_to(ROOT)), "items": items},
                                  sort_keys=False, allow_unicode=True, width=110))
    print(f"wrote {out.relative_to(ROOT)}: label {len(items)} items (pass/fail), then run `compare`")


def cohen_kappa(a: list[bool], b: list[bool]) -> float:
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def compare(labels_file: Path) -> None:
    doc = yaml.safe_load(labels_file.read_text())
    rows = {(r["case_id"], r["rep"]): r for r in
            (json.loads(line) for line in (ROOT / doc["report"] / "results.jsonl").read_text().splitlines())}
    human, model, disagree = [], [], []
    for item in doc["items"]:
        if item.get("human_correctness") not in ("pass", "fail"):
            continue
        grade = next((g for g in rows[(item["case_id"], item["rep"])]["grades"] if g["name"] == CRITERION), None)
        if grade is None:
            continue
        h, m = item["human_correctness"] == "pass", grade["passed"]
        human.append(h)
        model.append(m)
        if h != m:
            disagree.append((item["case_id"], item["rep"], h, m, grade["detail"]))
    if not human:
        sys.exit("no labelled items yet: set human_correctness to pass or fail")
    agree = sum(h == m for h, m in zip(human, model)) / len(human)
    print(f"{len(human)} labelled items: agreement {agree:.0%}, Cohen's kappa {cohen_kappa(human, model):.2f}")
    for case_id, rep, h, m, why in disagree:
        print(f"\n- {case_id} rep{rep}: you said {'pass' if h else 'fail'}, judge said {'pass' if m else 'fail'}\n"
              f"  judge's reasoning: {why[:400]}")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("export", "compare"):
        sys.exit(__doc__)
    (export if sys.argv[1] == "export" else compare)(Path(sys.argv[2]).resolve())
