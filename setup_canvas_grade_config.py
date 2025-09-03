#!/usr/bin/env python3
"""
Interactive setup for Canvas Grade Calculator.
Creates a YAML config file with:
  - Canvas base URL and API token
  - Excluded course IDs and name substrings
  - Default weights and per-course weights
  - Default final policy and per-course policy overrides

Usage:
  python setup_canvas_grade_config.py --out config.yaml

After this runs, it prints example commands to use the calculator.
"""
from __future__ import annotations

import argparse
import getpass
import os
from typing import Dict, Any

try:
    import yaml  # type: ignore
except Exception:
    yaml = None

FINAL_POLICIES = [
    "ignore_all",
    "missing_zero_upcoming_ignore",
    "all_zero",
]

def parse_args():
    p = argparse.ArgumentParser(description="Initialize Canvas Grade Calculator config")
    p.add_argument("--out", default="config.yaml", help="Path to write the config file (YAML)")
    return p.parse_args()

def prompt(msg: str, default: str | None = None) -> str:
    d = f" [{default}]" if default else ""
    val = input(f"{msg}{d}: ").strip()
    return val or (default or "")

def yesno(msg: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    val = input(f"{msg} ({d}): ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes")

def collect_weights(kind: str) -> Dict[str, float]:
    print(f"\nEnter {kind} weights (blank name to stop). You can enter raw numbers or percents; they will be normalized to 100%.")
    weights: Dict[str, float] = {}
    while True:
        name = input("  Category name: ").strip()
        if not name:
            break
        val = input("  Weight (number or %): ").strip().replace('%', '')
        try:
            w = float(val)
        except Exception:
            print("    Invalid number; try again.")
            continue
        weights[name] = w
    return weights

def collect_by_course_weights() -> Dict[str, Dict[str, float]]:
    print("\nAdd per-course weight overrides (blank Course ID to stop).")
    by_id: Dict[str, Dict[str, float]] = {}
    while True:
        cid = input("  Course ID: ").strip()
        if not cid:
            break
        try:
            int(cid)
        except Exception:
            print("    Course ID must be an integer; try again.")
            continue
        print(f"  Enter weights for course {cid} ...")
        by_id[cid] = collect_weights("course-specific")
    return by_id

def collect_by_course_policy() -> Dict[str, str]:
    print("\nAdd per-course final policy overrides (blank Course ID to stop). Options: " + ", ".join(FINAL_POLICIES))
    by_id: Dict[str, str] = {}
    while True:
        cid = input("  Course ID: ").strip()
        if not cid:
            break
        try:
            int(cid)
        except Exception:
            print("    Course ID must be an integer; try again.")
            continue
        pol = input("  Final policy: ").strip()
        if pol not in FINAL_POLICIES:
            print("    Invalid policy; choose one of: " + ", ".join(FINAL_POLICIES))
            continue
        by_id[cid] = pol
    return by_id

def main():
    args = parse_args()
    if yaml is None:
        print("This tool requires PyYAML. Install with: pip install pyyaml")
        return

    print("\n=== Canvas connection ===")
    base_url = prompt("Canvas base URL", os.getenv("CANVAS_BASE_URL") or "https://school.instructure.com")
    token = getpass.getpass("Canvas API token (input hidden): ") or os.getenv("CANVAS_TOKEN") or ""

    print("\n=== Exclusions ===")
    ids_csv = prompt("Exclude course IDs (comma-separated)", "")
    excl_ids = []
    for x in ids_csv.split(','):
        x = x.strip()
        if not x:
            continue
        try:
            excl_ids.append(int(x))
        except Exception:
            print(f"  Skipping non-integer: {x}")
    name_subs = []
    while True:
        s = prompt("Exclude courses whose NAME contains (blank to stop)", "")
        if not s:
            break
        name_subs.append(s)

    print("\n=== Weights ===")
    default_weights = collect_weights("DEFAULT")
    by_course_weights = collect_by_course_weights()

    print("\n=== Final policy ===")
    print("Options:", ", ".join(FINAL_POLICIES))
    default_policy = prompt("Default final policy", "missing_zero_upcoming_ignore")
    by_course_policy = collect_by_course_policy()

    cfg: Dict[str, Any] = {
        "canvas": {
            "base_url": base_url,
            "token": token,
        },
        "exclusions": {
            "ids": excl_ids,
            "name_contains": name_subs,
        },
        "weights": {
            "default": default_weights or None,
            "by_course_id": by_course_weights,
        },
        "final_policy": {
            "default": default_policy,
            "by_course_id": by_course_policy,
        },
    }

    with open(args.out, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)

    print(f"\n✅ Wrote {args.out}")
    print("\nNext steps:")
    print("  1) (Optional) export env vars if you prefer not to keep token in file:")
    print(f"     export CANVAS_BASE_URL=\"{base_url}\"")
    print(f"     export CANVAS_TOKEN=\"<your token>\"")
    print("  2) Run the grade calculator. Examples:\n")
    print("     # All active courses using config")
    print(f"     python canvas_grade_calculator.py --all-courses --config {args.out}\n")
    print("     # Include completed")
    print(f"     python canvas_grade_calculator.py --all-courses --include-completed --config {args.out}\n")
    print("     # Single course with per-course weights from config (if present)")
    print(f"     python canvas_grade_calculator.py --course-id 12345 --config {args.out}\n")
    print("     # CLI still works and overrides config where provided")
    print(f"     python canvas_grade_calculator.py --course-id 210272 --weights '{{\"Homework\":20, \"Exam 1\":25, \"Exam 2\":25, \"Exam 3\":30}}' --final-policy all_zero --config {args.out}\n")
    print("🎉 Setup complete.")

if __name__ == "__main__":
    main()

