"""Eval harness for logb: measure end-to-end agent behavior so prompt and
code changes can be diffed rather than guessed-at.

A test case is a JSON file under `logb/eval/corpus/` with the shape:

    {
      "id": "innovus-rca",
      "log_path": "logs/sample_innovus.log",
      "question": "why did the run crash?",
      "mode": "eda",
      "expected_facts": [
        "IMPSDC-3071",
        "core_clk",
        "top.sdc",
        {"any_of": ["place stage", "place_design", "during place"]}
      ],
      "forbidden_facts": ["impex_4022", "techlib_1366"],
      "max_steps": 8
    }

Run via:  logb eval [--filter PATTERN] [--max-cases N]
"""

from .runner import EvalCase, EvalResult, run_corpus, score_one

__all__ = ["EvalCase", "EvalResult", "run_corpus", "score_one"]
