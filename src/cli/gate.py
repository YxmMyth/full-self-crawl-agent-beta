"""Gate — the human decision point between recon and harvest.

Recon writes a strategy_report.md when it finishes (see
src/planner/recon_planner.py:_emit_strategy_report). This module shows
that report to the operator and asks: continue to harvest, stop, or
edit the requirement first.

Future replacements (Feishu webhook, web dashboard) implement the same
contract: show report, get decision, optionally edit requirement.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

from src.config import Config
from src.runtime import status_hook
from src.utils.logging import get_logger

logger = get_logger(__name__)


_DECISION_CONTINUE = "continue"
_DECISION_STOP = "stop"
_DECISION_EDIT = "edit"


def ask_continue_to_harvest(domain: str) -> str:
    """Show the strategy report and ask the operator: y / N / edit.

    Returns one of 'continue' / 'stop' / 'edit'.
    """
    run_dir = Config.run_dir(domain)
    report_path = run_dir / "strategy_report.md"
    req_path = run_dir / "requirement.txt"

    print("\n" + "=" * 60)
    print("  RECON COMPLETE")
    print("=" * 60)
    print(f"\nDomain:       {domain}")
    if req_path.exists():
        print(f"Requirement:  {req_path.read_text(encoding='utf-8').strip()}")
    print(f"Run ID:       {Config.RUN_ID}")

    if report_path.exists():
        print(f"\nStrategy report ({report_path}):\n")
        print(report_path.read_text(encoding="utf-8"))
    else:
        print(f"\n[!] Strategy report missing at {report_path}")
        print("    Recon may have skipped report generation. Proceeding to ask anyway.")

    # Helmsman path: no stdin (the mission may be a docker-run subprocess with
    # no attached pipe). Raise gate_pending via status.json and poll for the
    # operator's decision file — the same cross-process signal-file pattern the
    # WebGateway uses for human-assist. The console writes gate_response.json
    # (supervisor.answer_gate) when the operator clicks continue / stop.
    if status_hook.enabled():
        return _await_gate_via_console(run_dir)

    print("\n" + "-" * 60)
    while True:
        try:
            ans = input("  Continue to harvest? [y/N/edit]: ").strip().lower()
        except EOFError:
            # Non-interactive — treat as stop
            print("  (no stdin — defaulting to stop)")
            return _DECISION_STOP
        if ans in ("y", "yes"):
            return _DECISION_CONTINUE
        if ans in ("", "n", "no"):
            return _DECISION_STOP
        if ans == "edit":
            return _DECISION_EDIT
        print("  Please answer y / N / edit.")


def _await_gate_via_console(run_dir) -> str:
    """Block until the Helmsman operator answers the gate (continue / stop).

    Mirrors WebGateway: set status.json gate_pending=True, then poll
    workspace/gate_response.json. Waits indefinitely — the gate is an explicit
    human checkpoint (use --no-gate for unattended runs). Returns 'continue' or
    'stop' ('edit' is not offered by the web console).
    """
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    resp_file = workspace / "gate_response.json"
    try:
        resp_file.unlink()
    except FileNotFoundError:
        pass

    status_hook.set_flag(gate_pending=True)
    logger.info("Gate raised to console; awaiting operator decision (continue/stop).")
    try:
        while True:
            if resp_file.exists():
                try:
                    data = json.loads(resp_file.read_text(encoding="utf-8"))
                    decision = str(data.get("decision", _DECISION_STOP))
                except Exception:
                    decision = _DECISION_STOP
                if decision not in (_DECISION_CONTINUE, _DECISION_STOP, _DECISION_EDIT):
                    decision = _DECISION_STOP
                logger.info(f"Gate decision from console: {decision}")
                return decision
            time.sleep(1.0)
    finally:
        status_hook.set_flag(gate_pending=False)
        try:
            resp_file.unlink()
        except FileNotFoundError:
            pass


def open_requirement_for_edit(domain: str) -> str:
    """Open requirement.txt in $EDITOR (or fallback to manual edit).

    Returns the requirement text after editing (stripped).
    """
    req_path = Config.run_dir(domain) / "requirement.txt"
    editor = os.environ.get("EDITOR", "")
    if editor:
        try:
            subprocess.run([editor, str(req_path)], check=False)
        except FileNotFoundError:
            print(f"\n[!] $EDITOR='{editor}' not found.")
            print(f"    Manually edit: {req_path}")
            try:
                input("    Press Enter when done.")
            except EOFError:
                pass
    else:
        print(f"\n[!] No $EDITOR set. Manually edit: {req_path}")
        try:
            input("    Press Enter when done.")
        except EOFError:
            pass
    return req_path.read_text(encoding="utf-8").strip()
