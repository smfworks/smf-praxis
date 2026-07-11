#!/usr/bin/env python3
"""H05 calibration harness — find the verifier threshold that separates
correct answers from wrong ones on real tasks.

Run with a live logprob-exposing backend:
    export OPENAI_BASE_URL=http://127.0.0.1:8088/v1
    export OPENAI_API_KEY=EMPTY
    python3 scripts/calibrate_h05_verifier.py

Output: per-task scores (correct vs wrong), separation, and the threshold
that best discriminates with zero false-rejects on correct answers.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from hybridagent.verifier_llm import LLMVerifierConfig, LLMVerifierCritic

# 10 calibration tasks — each a real task a Praxis agent could be asked,
# with a CORRECT answer and a WRONG answer. The verifier should score the
# correct answer higher. Tasks span reasoning, factual, and formatting.
TASKS = [
    {
        "task": "What is 15 multiplied by 12? Reply with just the number.",
        "correct": "180",
        "wrong": "The answer is 150.",
    },
    {
        "task": "Name the capital of Australia. Reply with just the city name.",
        "correct": "Canberra",
        "wrong": "Sydney",
    },
    {
        "task": "Does a square have 3 or 4 sides? Reply with just the number.",
        "correct": "4",
        "wrong": "3",
    },
    {
        "task": "What is the chemical symbol for gold? Reply with just the symbol.",
        "correct": "Au",
        "wrong": "Gd",
    },
    {
        "task": "Translate 'hello' to Spanish. Reply with just the word.",
        "correct": "hola",
        "wrong": "bonjour",
    },
    {
        "task": "Is the sky typically blue at noon on a clear day? Reply yes or no.",
        "correct": "yes",
        "wrong": "no, it is green",
    },
    {
        "task": "What is 7 minus 3? Reply with just the number.",
        "correct": "4",
        "wrong": "10",
    },
    {
        "task": "Name the planet closest to the sun. Reply with just the name.",
        "correct": "Mercury",
        "wrong": "Venus",
    },
    {
        "task": "How many days are in a standard week? Reply with just the number.",
        "correct": "7",
        "wrong": "five",
    },
    {
        "task": "Is water wet? Reply yes or no.",
        "correct": "yes",
        "wrong": "no, water is dry",
    },
]


def main() -> int:
    if not (os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("VERTEX_API_KEY")):
        print("ERROR: set OPENAI_BASE_URL (local server) or VERTEX_API_KEY",
              file=sys.stderr)
        return 1
    n_eval = int(os.environ.get("H05_N_EVAL", "4"))
    model = os.environ.get("H05_MODEL", "gemini-2.5-flash")
    config = LLMVerifierConfig(enabled=True, n_evaluations=n_eval,
                               threshold=0.5, model=model)
    critic = LLMVerifierCritic(config)

    results = []
    for i, t in enumerate(TASKS, 1):
        print(f"[{i}/{len(TASKS)}] {t['task']}", flush=True)
        # Score both answers via the internal _score to get continuous values.
        try:
            s_correct = critic._score(t["task"], t["correct"])
        except Exception as e:
            s_correct = -1.0
            print(f"  correct error: {e}", flush=True)
        try:
            s_wrong = critic._score(t["task"], t["wrong"])
        except Exception as e:
            s_wrong = -1.0
            print(f"  wrong error: {e}", flush=True)
        sep = s_correct - s_wrong
        verdict_c = "APPROVE" if s_correct >= 0.5 else "REVISE"
        verdict_w = "APPROVE" if s_wrong >= 0.5 else "REVISE"
        print(f"  correct={s_correct:.4f} ({verdict_c})  "
              f"wrong={s_wrong:.4f} ({verdict_w})  "
              f"sep={sep:+.4f}", flush=True)
        results.append({"task": t["task"], "correct": t["correct"],
                        "wrong": t["wrong"], "s_correct": s_correct,
                        "s_wrong": s_wrong, "sep": sep})

    # Summary
    correct_scores = [r["s_correct"] for r in results]
    wrong_scores = [r["s_wrong"] for r in results]
    n_correct_higher = sum(1 for r in results if r["sep"] > 0)
    n_tied = sum(1 for r in results if r["sep"] == 0)
    min_correct = min(correct_scores) if correct_scores else 0
    max_wrong = max(wrong_scores) if wrong_scores else 1

    print("\n=== SUMMARY ===")
    print(f"tasks: {len(results)}")
    print(f"correct higher than wrong: {n_correct_higher}/{len(results)}")
    print(f"tied: {n_tied}")
    print(f"correct score range: [{min(correct_scores):.4f}, {max(correct_scores):.4f}]")
    print(f"wrong score range:   [{min(wrong_scores):.4f}, {max(wrong_scores):.4f}]")
    print(f"min correct score: {min_correct:.4f}")
    print(f"max wrong score:   {max_wrong:.4f}")
    # Best threshold: halfway between min correct and max wrong, but only
    # if they separate (min correct > max wrong). Otherwise no threshold
    # discriminates cleanly.
    if min_correct > max_wrong:
        threshold = (min_correct + max_wrong) / 2
        print(f"\nCLEAN SEPARATION — recommended threshold: {threshold:.4f}")
        print(f"  (min correct {min_correct:.4f} > max wrong {max_wrong:.4f})")
    else:
        print("\nNO CLEAN SEPARATION — threshold 0.5 misclassifies.")
        print("  Review per-task scores and pick a threshold that minimizes")
        print("  false-rejects on correct answers.")

    # Save raw results
    out = {"model": model, "n_evaluations": n_eval, "results": results,
           "min_correct": min_correct, "max_wrong": max_wrong,
           "n_correct_higher": n_correct_higher}
    outpath = os.path.join(os.path.dirname(__file__), "..",
                           "docs", "harness", "h05-calibration.json")
    with open(outpath, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nraw results -> {outpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())