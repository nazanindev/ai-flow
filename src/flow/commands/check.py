"""flow check — independent mid-loop checker for local diffs."""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any

import anthropic
from rich.console import Console

from flow.billing import metered_call
from flow.config import load_style, style_prompt
from flow.router import utility_model

console = Console()

CHECK_SYSTEM = """You are an independent evaluator for code changes.
Review the provided git diff and return ONLY valid JSON with this exact schema:
{
  "summary": "short human summary",
  "overall": "A|B|C|D",
  "dimensions": {
    "correctness": "A|B|C|D",
    "architecture": "A|B|C|D",
    "test_coverage": "A|B|C|D"
  },
  "findings": [
    {
      "severity": "blocker|warning|note",
      "file": "path or empty string",
      "line": 0,
      "title": "short title",
      "detail": "why this matters",
      "action": "specific fix recommendation"
    }
  ]
}

Rules:
- blocker = likely bug/regression, broken behavior, security risk, or missing required test for changed logic.
- warning = notable quality risk that should be fixed soon.
- note = optional improvement.
- If uncertain, prefer warning over blocker.
- Keep findings concise and actionable.
- Output JSON only, no markdown."""


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))


def _local_diff() -> str:
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git diff failed")
    return result.stdout


def _default_report() -> dict[str, Any]:
    return {
        "summary": "No significant issues found.",
        "overall": "A",
        "dimensions": {
            "correctness": "A",
            "architecture": "A",
            "test_coverage": "A",
        },
        "findings": [],
    }


def _normalize_report(raw: Any) -> dict[str, Any]:
    base = _default_report()
    if not isinstance(raw, dict):
        return base

    summary = str(raw.get("summary", base["summary"])).strip() or base["summary"]
    overall = str(raw.get("overall", base["overall"])).strip().upper()
    if overall not in {"A", "B", "C", "D"}:
        overall = "C"

    dims_raw = raw.get("dimensions", {}) if isinstance(raw.get("dimensions"), dict) else {}
    dimensions = {}
    for key in ("correctness", "architecture", "test_coverage"):
        value = str(dims_raw.get(key, base["dimensions"][key])).strip().upper()
        dimensions[key] = value if value in {"A", "B", "C", "D"} else "C"

    findings: list[dict[str, Any]] = []
    for row in raw.get("findings", []) if isinstance(raw.get("findings"), list) else []:
        if not isinstance(row, dict):
            continue
        sev = str(row.get("severity", "note")).strip().lower()
        if sev not in {"blocker", "warning", "note"}:
            sev = "note"
        findings.append(
            {
                "severity": sev,
                "file": str(row.get("file", "")).strip(),
                "line": int(row.get("line", 0) or 0),
                "title": str(row.get("title", "")).strip() or "Untitled finding",
                "detail": str(row.get("detail", "")).strip(),
                "action": str(row.get("action", "")).strip(),
            }
        )

    return {
        "summary": summary,
        "overall": overall,
        "dimensions": dimensions,
        "findings": findings,
    }


def run_check(diff_text: str | None = None, run_id: str = "check-local") -> dict[str, Any]:
    """Evaluate local diff and return normalized structured report."""
    style = load_style()
    check_style = style_prompt(style, ["ci_review"])
    system = (check_style + "\n\n" + CHECK_SYSTEM) if check_style else CHECK_SYSTEM

    diff = diff_text if diff_text is not None else _local_diff()
    if not diff.strip():
        report = _default_report()
        report["summary"] = "Empty diff — nothing to evaluate."
        return report

    resp = metered_call(
        _client(),
        utility_model("fast"),
        run_id=run_id,
        purpose="check",
        max_tokens=1400,
        system=system,
        messages=[{"role": "user", "content": f"Evaluate this diff:\n\n{diff[:18000]}"}],
    )
    raw = ""
    if getattr(resp, "content", None):
        raw = str(resp.content[0].text).strip()
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {}
    report = _normalize_report(parsed)
    report["blocker_count"] = sum(1 for f in report["findings"] if f["severity"] == "blocker")
    report["warning_count"] = sum(1 for f in report["findings"] if f["severity"] == "warning")
    report["note_count"] = sum(1 for f in report["findings"] if f["severity"] == "note")
    return report


def _print_human(report: dict[str, Any]) -> None:
    dims = report["dimensions"]
    console.print(
        "[bold]flow check[/bold] "
        f"(overall {report['overall']}) "
        f"[dim]correctness {dims['correctness']} · architecture {dims['architecture']} · "
        f"test_coverage {dims['test_coverage']}[/dim]"
    )
    console.print(report["summary"])
    findings = report.get("findings", [])
    if not findings:
        console.print("[green]No findings.[/green]")
        return
    for f in findings:
        location = f["file"] or "(unknown file)"
        if f["line"] > 0:
            location = f"{location}:{f['line']}"
        console.print(
            f"- [{f['severity']}] {f['title']} — {location}\n"
            f"  {f['detail']}\n"
            f"  Action: {f['action']}"
        )


def cmd_check(json_output: bool = False) -> None:
    """Run independent diff checker and print report."""
    try:
        report = run_check()
    except Exception as e:
        console.print(f"[red]flow check failed:[/red] {e}")
        raise SystemExit(1)

    try:
        from flow.tracker import init_db, load_active_run, save_event
        from flow.config import get_project_id
        init_db()
        run = load_active_run(get_project_id())
        if run:
            save_event(
                run_id=run.run_id,
                event_type="check_result",
                project=run.project,
                phase=run.phase.value,
                metadata={
                    "overall": report.get("overall", "?"),
                    "blocker_count": report.get("blocker_count", 0),
                    "warning_count": report.get("warning_count", 0),
                },
            )
    except Exception:
        pass

    if json_output:
        console.print(json.dumps(report, indent=2))
    else:
        _print_human(report)

    if int(report.get("blocker_count", 0)) > 0:
        raise SystemExit(1)
