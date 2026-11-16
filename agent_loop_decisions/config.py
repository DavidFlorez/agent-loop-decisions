"""Pinned constants, question definitions, and phase defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Pinned external references
AGENT_LOOPS_COMMIT = "5e29d428f4e80403f982b7bbf4671fa9b696880e"
GEMINI_CLI_COMMIT = "4fa64e71b9b4e1bb65a86cc2863574f561b82340"
SWE_DATASET_REPO = "ibragim-bad/swe_rebench_07_2026_trajectories"
SWE_DATASET_SHA = "cdae27cdd16673f0c682871ad55d325f24cc7020"
TB_DATASET_REPO = "yoonholee/terminalbench-trajectories"
TB_DATASET_SHA = "04e8940f5b6736a7ce8d22224fe2f2af74163ed2"

# Decisions API configuration
DECISION_MODEL = "gemini-decision-1"
API_VERSION = "v1beta"
PRICE_PER_M_INPUT_TOKENS_USD = 0.10
ESTIMATE_CHARS_PER_TOKEN = 3.0

# Safety-filter HTTP 400 error.code values from Decisions API docs
SAFETY_BLOCK_CODES: frozenset[str] = frozenset(
    {
        "safety",
        "prohibited_content",
        "blocklist",
        "image_safety",
        "content_blocked",
    }
)

# Default spend caps (USD)
DEFAULT_PHASE_CAPS_USD: dict[str, float] = {
    "pilot": 0.25,
    "swe": 7.00,
    "tb": 3.00,
}
DEFAULT_GLOBAL_CAP_USD = 10.00

# Conservative default rate limits (Free tier: 200 RPM, 50,000 TPM, 10,000 RPD)
DEFAULT_MAX_RPM = 180
DEFAULT_MAX_TPM = 45_000
MAX_RETRIES = 5

# Sampling & decision grid parameters
RANDOM_SEED = 42
SWE_SAMPLE_A_PER_MODEL = 23
TB_SAMPLE_A_PER_MODEL = 10
SWE_DEFAULT_ONLY_ENRICHMENT_N = 60
TB_STRICT_ENRICHMENT_N = 120
TB_DEFAULT_ONLY_ENRICHMENT_N = 60

GRID_STRIDE = 4
GRID_MAX_STEP = 200  # evaluate at t = 4, 8, 12, ... < min(n_steps, 200)
MAIN_WINDOW_STEPS = 12
TASK_MAX_CHARS = 1500
THOUGHT_MAX_CHARS = 300
CALL_MAX_CHARS = 500
OBS_HEAD_CHARS = 400
OBS_TAIL_CHARS = 400

# Threshold sweep (must stay in [0.01, 0.99])
THRESHOLDS: tuple[float, ...] = (0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.98)
TARGET_FSR_LEVELS: tuple[float, ...] = (0.01, 0.02, 0.05)

# Gemini CLI LLM loop check parameters (pinned to GEMINI_CLI_COMMIT)
GEMINI_CLI_LLM_CHECK_AFTER_TURNS = 30
GEMINI_CLI_DEFAULT_INTERVAL = 10
GEMINI_CLI_MIN_INTERVAL = 5
GEMINI_CLI_MAX_INTERVAL = 15
GEMINI_CLI_HISTORY_COUNT = 20
GEMINI_CLI_CONFIDENCE_THRESHOLD = 0.9

# Main 3-question payload
MAIN_QUESTIONS: dict[str, dict[str, Any]] = {
    "stuck": {
        "type": "noul",
        "instructions": (
            "Is the agent stuck in an unproductive loop, repeating the same actions or "
            "the same failing approach in its recent steps without making progress toward the task?"
        ),
        "criteria": {
            "true_description": (
                "Recent steps repeat actions or a failing approach and produce no new "
                "information or progress."
            ),
            "false_description": (
                "Recent steps gather new information, change code, or try a materially "
                "different approach, even if they revisit the same files."
            ),
        },
    },
    "progress": {
        "type": "noul",
        "instructions": (
            "Did the agent's most recent steps produce new information or changes that "
            "move the task forward?"
        ),
    },
    "doomed": {
        "type": "noul",
        "instructions": (
            "If the agent keeps working the way its recent steps show, is it likely to "
            "finish without solving the task?"
        ),
    },
}

# Gemini CLI stand-in question (condensed from LOOP_DETECTION_SYSTEM_PROMPT)
GEMINI_CLI_QUESTIONS: dict[str, dict[str, Any]] = {
    "unproductive_loop": {
        "type": "noul",
        "instructions": (
            "Is the assistant stuck in a repetitive, unproductive loop over at least 5 "
            "consecutive actions with no net change or forward progress toward the original task?"
        ),
        "criteria": {
            "true_description": (
                "Exhibits an alternating cycle with identical outcomes, semantic repetition "
                "of the same tool and equivalent arguments with identical outcomes, or stuck "
                "reasoning restating the same plan without new action or progress."
            ),
            "false_description": (
                "Makes forward progress, including cross-file batch operations on different "
                "file paths, incremental edits to different parts of a file, sequential "
                "searches on different targets, or retries with modified arguments or approach."
            ),
        },
    },
}

# Standard repository paths
ROOT_DIR = Path(".")
DATA_DIR = ROOT_DIR / "data"
CACHE_DIR = ROOT_DIR / "cache"
RESULTS_DIR = ROOT_DIR / "results"
ASSETS_DIR = ROOT_DIR / "assets"
AUDIT_DIR = ROOT_DIR / "audit"
