"""Leakage-free trajectory window rendering for Gemini Decision 1."""

from __future__ import annotations

from typing import Sequence

from agent_loops.canonicalize import NormalizedStep

from agent_loop_decisions.config import (
    CALL_MAX_CHARS,
    MAIN_WINDOW_STEPS,
    OBS_HEAD_CHARS,
    OBS_TAIL_CHARS,
    TASK_MAX_CHARS,
    THOUGHT_MAX_CHARS,
)


def _truncate_text(text: str, max_chars: int) -> str:
    clean = " ".join((text or "").split())
    if not clean:
        return "(none)"
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 1] + "…"


def _format_obs(obs_text: str) -> str:
    obs = (obs_text or "").strip()
    if not obs:
        return "(empty)"
    limit = OBS_HEAD_CHARS + OBS_TAIL_CHARS
    if len(obs) <= limit:
        return obs
    omitted = len(obs) - limit
    return f"{obs[:OBS_HEAD_CHARS]} …[truncated {omitted} chars]… {obs[-OBS_TAIL_CHARS:]}"


def _format_task(task_text: str) -> str:
    txt = (task_text or "").strip()
    if not txt:
        return "(no task description available)"
    if len(txt) <= TASK_MAX_CHARS:
        return txt
    return txt[:TASK_MAX_CHARS]


def slice_prefix_steps(steps: Sequence[NormalizedStep], step_idx: int) -> list[NormalizedStep]:
    """Return strictly causal prefix containing only steps with s.step_idx <= step_idx."""
    if step_idx < 0:
        raise ValueError(f"step_idx must be non-negative, got {step_idx}")
    prefix: list[NormalizedStep] = []
    for s in steps:
        if s.step_idx <= step_idx:
            prefix.append(s)
    return prefix


def render_decision_window(
    task_text: str,
    prefix_steps: Sequence[NormalizedStep],
    *,
    step_idx: int,
    window_steps: int = MAIN_WINDOW_STEPS,
) -> str:
    """Render a strictly causal decision-point prompt containing only steps <= step_idx.

    Never accepts run length, outcome, model name, harness, loop labels, or rule counters.
    """
    if step_idx < 0:
        raise ValueError(f"step_idx must be non-negative, got {step_idx}")
    if not prefix_steps:
        raise ValueError("prefix_steps must be non-empty")

    for s in prefix_steps:
        if s.step_idx > step_idx:
            raise ValueError(
                f"Causal leakage detected: step {s.step_idx} > decision step {step_idx}"
            )

    recent = list(prefix_steps[-window_steps:])
    omitted = len(prefix_steps) > len(recent)
    omitted_note = "earlier steps omitted" if omitted else "from start of run"
    n_shown = len(recent)

    lines: list[str] = [
        f"TASK (first {TASK_MAX_CHARS:,} chars):",
        _format_task(task_text),
        "",
        f"AGENT HISTORY (the {n_shown} most recent steps of a run still in progress; {omitted_note}):",
    ]

    for s in recent:
        lines.append(f"[step {s.step_idx}]")
        lines.append(f"thought: {_truncate_text(s.assistant_text, THOUGHT_MAX_CHARS)}")
        if s.has_tool:
            call_repr = _truncate_text(s.canonical_call, CALL_MAX_CHARS)
            lines.append(f"call: {s.tool_name}: {call_repr}")
        else:
            lines.append("call: (no tool call)")
        err_flag = "yes" if s.is_error else "no"
        lines.append(f"output: {_format_obs(s.obs_text)}   (error: {err_flag})")

    return "\n".join(lines)
