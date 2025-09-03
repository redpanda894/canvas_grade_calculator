#!/usr/bin/env python3
"""
Canvas LMS Grade Calculator (multi-course + exclusions + config overrides)

Fetches assignments from Canvas, groups them by assignment group ("category"),
applies user-provided category weights or Canvas group weights, and computes:
  - Running grade (only graded work)
  - Final estimate (policy for ungraded items is configurable)

Features:
  - `--all-courses` to calculate grades for every active (and optionally completed) course
  - `--csv` supports single- and multi-course export (one file)
  - `--exclude-course-ids` and `--exclude-name-contains` to skip specific courses
  - `--config` YAML/JSON file for exclusions, per-course weights, per-course final policies, and Canvas auth
  - CLI flags still work; precedence is: CLI > config > env > Canvas

Requirements:
  - Python 3.9+
  - requests, pydantic, pyyaml (pip install requests pydantic pyyaml)

Auth sources (precedence: CLI > config > ENV):
  - CLI: --base-url, --token
  - Config: canvas.base_url, canvas.token
  - ENV: CANVAS_BASE_URL, CANVAS_TOKEN
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timedelta, timezone

import requests
from pydantic import BaseModel

try:
    import yaml  # type: ignore
except Exception:
    yaml = None

# ----------------------------- Models ----------------------------- #

class Submission(BaseModel):
    score: Optional[float] = None
    workflow_state: Optional[str] = None
    missing: Optional[bool] = None
    excused: Optional[bool] = None

class Assignment(BaseModel):
    id: int
    name: str
    points_possible: Optional[float] = None
    assignment_group_id: Optional[int] = None
    muted: Optional[bool] = None
    published: Optional[bool] = True
    due_at: Optional[str] = None
    submission: Optional[Submission] = None

class AssignmentGroup(BaseModel):
    id: int
    name: str
    group_weight: Optional[float] = None

class CategoryResult(BaseModel):
    group_id: int
    group_name: str
    weight_pct: float
    running_earned: float
    running_possible: float
    running_pct: Optional[float]
    final_earned: float
    final_possible: float
    final_pct: Optional[float]

class CourseRollup(BaseModel):
    course_id: int
    course_name: Optional[str] = None
    running_total_pct: Optional[float]
    final_total_pct: Optional[float]
    policy: str
    categories: List[CategoryResult]

# ----------------------------- API Client ----------------------------- #

class CanvasClient:
    def __init__(self, base_url: str, token: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        self.timeout = timeout

    def _get(self, path: str, params: Optional[dict] = None):
        url = f"{self.base_url}{path}"
        items = []
        while url:
            resp = self.session.get(url, params=params, timeout=self.timeout)
            if not resp.ok:
                raise RuntimeError(f"GET {url} failed: {resp.status_code} {resp.text}")
            data = resp.json()
            if isinstance(data, list):
                items.extend(data)
            else:
                return data
            next_url = None
            links = requests.utils.parse_header_links(resp.headers.get("Link", ""))
            for link in links:
                if link.get("rel") == "next":
                    next_url = link.get("url")
            url = next_url
            params = None
        return items

    def get_course(self, course_id: int):
        return self._get(f"/api/v1/courses/{course_id}")

    def list_my_courses(self, state: str = "active") -> List[dict]:
        params = {"enrollment_state": state, "per_page": 100}
        return self._get("/api/v1/courses", params=params)

    def get_assignment_groups(self, course_id: int) -> Tuple[List[AssignmentGroup], Dict[int, AssignmentGroup]]:
        raw = self._get(
            f"/api/v1/courses/{course_id}/assignment_groups",
            params={"include[]": ["assignments"]},
        )
        groups = []
        by_id: Dict[int, AssignmentGroup] = {}
        for g in raw:
            ag = AssignmentGroup(id=g["id"], name=g["name"], group_weight=g.get("group_weight"))
            groups.append(ag)
            by_id[ag.id] = ag
        return groups, by_id

    def get_assignments_with_submissions(self, course_id: int) -> List[Assignment]:
        items = self._get(
            f"/api/v1/courses/{course_id}/assignments",
            params={"include[]": ["submission"], "per_page": 100},
        )
        result: List[Assignment] = []
        for a in items:
            sub = a.get("submission") or {}
            assignment = Assignment(
                id=a["id"],
                name=a["name"],
                points_possible=a.get("points_possible"),
                assignment_group_id=a.get("assignment_group_id"),
                muted=a.get("muted"),
                published=a.get("published", True),
                due_at=a.get("due_at"),
                submission=Submission(
                    score=sub.get("score"),
                    workflow_state=sub.get("workflow_state"),
                    missing=sub.get("missing"),
                    excused=sub.get("excused"),
                ) if sub else None,
            )
            result.append(assignment)
        return result

# ----------------------------- Weight Handling ----------------------------- #

def normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("Sum of weights must be > 0")
    return {k: (v / total) * 100.0 for k, v in weights.items()}

@dataclass
class WeightPlan:
    by_group_name: Dict[str, float]  # name -> weight %

    @classmethod
    def from_user(cls, user_weights: Optional[Dict[str, float]], canvas_groups: List[AssignmentGroup]):
        if user_weights:
            return cls(by_group_name=normalize_weights(user_weights))
        canvas_defined: Dict[str, float] = {}
        for g in canvas_groups:
            if g.group_weight is not None:
                canvas_defined[g.name] = g.group_weight
        if not canvas_defined:
            raise ValueError("No weights provided and course does not have weighted assignment groups in Canvas.")
        total = sum(canvas_defined.values())
        if 0 < total <= 1.001:
            canvas_defined = {k: v * 100.0 for k, v in canvas_defined.items()}
        return cls(by_group_name=normalize_weights(canvas_defined))

# ----------------------------- Calculation ----------------------------- #

class FinalPolicy:
    IGNORE_ALL = "ignore_all"
    MISSING_ZERO_UPCOMING_IGNORE = "missing_zero_upcoming_ignore"  # default
    ALL_ZERO = "all_zero"

@dataclass
class Tally:
    earned: float = 0.0
    possible: float = 0.0


def categorize_assignments(assignments: List[Assignment], groups_by_id: Dict[int, AssignmentGroup]):
    by_group: Dict[int, List[Assignment]] = {}
    for a in assignments:
        gid = a.assignment_group_id or -1
        by_group.setdefault(gid, []).append(a)
    if -1 not in groups_by_id:
        groups_by_id[-1] = AssignmentGroup(id=-1, name="(Uncategorized)", group_weight=None)
    return by_group


def compute_category_results(
    by_group: Dict[int, List[Assignment]],
    groups_by_id: Dict[int, AssignmentGroup],
    weight_plan: WeightPlan,
    final_policy: str,
) -> List[CategoryResult]:
    results: List[CategoryResult] = []
    name_by_id = {gid: groups_by_id[gid].name for gid in by_group}

    for gid, assignments in sorted(by_group.items(), key=lambda kv: name_by_id[kv[0]].lower()):
        g = groups_by_id[gid]
        group_name = g.name
        weight_pct = weight_plan.by_group_name.get(group_name, 0.0)

        running = Tally()
        final = Tally()

        for a in assignments:
            if not a.published:
                continue
            pts = a.points_possible or 0.0
            if pts <= 0:
                continue
            sub = a.submission
            is_graded = sub is not None and sub.score is not None and (sub.excused is not True)
            is_missing = bool(sub and sub.missing)

            if is_graded:
                running.earned += max(0.0, float(sub.score))
                running.possible += pts

            if final_policy == FinalPolicy.IGNORE_ALL:
                if is_graded:
                    final.earned += max(0.0, float(sub.score))
                    final.possible += pts
            elif final_policy == FinalPolicy.ALL_ZERO:
                if is_graded:
                    final.earned += max(0.0, float(sub.score))
                final.possible += pts
            else:  # MISSING_ZERO_UPCOMING_IGNORE
                if is_graded:
                    final.earned += max(0.0, float(sub.score))
                    final.possible += pts
                else:
                    if is_missing:
                        final.possible += pts

        running_pct = (running.earned / running.possible * 100.0) if running.possible > 0 else None
        final_pct = (final.earned / final.possible * 100.0) if final.possible > 0 else None

        results.append(
            CategoryResult(
                group_id=gid,
                group_name=group_name,
                weight_pct=weight_pct,
                running_earned=running.earned,
                running_possible=running.possible,
                running_pct=running_pct,
                final_earned=final.earned,
                final_possible=final.possible,
                final_pct=final_pct,
            )
        )

    return results


def weighted_total(categories: List[CategoryResult]) -> Tuple[Optional[float], Optional[float]]:
    running_sum = 0.0
    final_sum = 0.0
    weight_total = 0.0
    for c in categories:
        w = c.weight_pct
        if w <= 0:
            continue
        weight_total += w
        if c.running_pct is not None:
            running_sum += (c.running_pct * w / 100.0)
        if c.final_pct is not None:
            final_sum += (c.final_pct * w / 100.0)
    if weight_total == 0:
        return None, None
    scale = 100.0 / weight_total
    return running_sum * scale, final_sum * scale

# ----------------------------- Config Loading/Merge ----------------------------- #

def load_config(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    text = open(path, "r", encoding="utf-8").read()
    if path.lower().endswith((".yml", ".yaml")):
        if yaml is None:
            raise RuntimeError("PyYAML is required for YAML configs: pip install pyyaml")
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text or "{}")
    # normalize shapes
    data.setdefault("canvas", {})  # holds base_url/token
    data.setdefault("weights", {})
    data["weights"].setdefault("default", None)
    data["weights"].setdefault("by_course_id", {})
    data.setdefault("final_policy", {})
    data["final_policy"].setdefault("default", None)
    data["final_policy"].setdefault("by_course_id", {})
    data.setdefault("exclusions", {})
    data["exclusions"].setdefault("ids", [])
    data["exclusions"].setdefault("name_contains", [])
    return data


def get_effective_weights(course_id: int, cli_weights: Optional[Dict[str, float]], cfg: Dict[str, Any]) -> Optional[Dict[str, float]]:
    # Precedence: CLI > config.by_course_id > config.default > None (fall back to Canvas)
    if cli_weights:
        return cli_weights
    by_id = (cfg.get("weights", {}) or {}).get("by_course_id", {})
    if str(course_id) in by_id:
        return by_id[str(course_id)]
    if course_id in by_id:
        return by_id[course_id]
    default_w = (cfg.get("weights", {}) or {}).get("default")
    return default_w


def get_effective_policy(course_id: int, cli_policy: Optional[str], cfg: Dict[str, Any]) -> str:
    # Precedence: CLI > config.by_course_id > config.default > script default
    if cli_policy:
        return cli_policy
    by_id = (cfg.get("final_policy", {}) or {}).get("by_course_id", {})
    if str(course_id) in by_id:
        return by_id[str(course_id)]
    if course_id in by_id:
        return by_id[course_id]
    default_p = (cfg.get("final_policy", {}) or {}).get("default")
    return default_p or FinalPolicy.MISSING_ZERO_UPCOMING_IGNORE


def build_exclusions(cli_ids: Optional[str], cli_names: Optional[List[str]], cfg: Dict[str, Any]) -> tuple[set[int], List[str]]:
    ids: set[int] = set()
    # From config
    for v in (cfg.get("exclusions", {}) or {}).get("ids", []):
        try:
            ids.add(int(v))
        except Exception:
            pass
    # From CLI
    if cli_ids:
        for x in cli_ids.split(','):
            x = x.strip()
            if not x:
                continue
            try:
                ids.add(int(x))
            except ValueError:
                print(f"[WARN] Skipping non-integer course id in --exclude-course-ids: {x}")
    # Names
    names = [(s or "").lower() for s in (cfg.get("exclusions", {}) or {}).get("name_contains", [])]
    if cli_names:
        names.extend([(s or "").lower() for s in cli_names])
    return ids, names

# ----------------------------- CSV Export ----------------------------- #

def export_csv_single(path: str, rollup: CourseRollup):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Course", rollup.course_name])
        w.writerow(["Course ID", rollup.course_id])
        w.writerow(["Final Policy", rollup.policy])
        w.writerow([])
        w.writerow(["Category", "Weight %", "Run Earned", "Run Possible", "Run %", "Final Earned", "Final Possible", "Final %"])
        for c in rollup.categories:
            w.writerow([
                c.group_name,
                f"{c.weight_pct:.2f}",
                f"{c.running_earned:.2f}",
                f"{c.running_possible:.2f}",
                f"{c.running_pct:.2f}" if c.running_pct is not None else "",
                f"{c.final_earned:.2f}",
                f"{c.final_possible:.2f}",
                f"{c.final_pct:.2f}" if c.final_pct is not None else "",
            ])
        w.writerow([])
        w.writerow(["Running Total %", f"{rollup.running_total_pct:.2f}" if rollup.running_total_pct is not None else ""])
        w.writerow(["Final Total %", f"{rollup.final_total_pct:.2f}" if rollup.final_total_pct is not None else ""])


def export_csv_multi(path: str, rollups: List[CourseRollup]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Course", "Course ID", "Category", "Weight %", "Run Earned", "Run Possible", "Run %", "Final Earned", "Final Possible", "Final %", "Running Total %", "Final Total %", "Policy"]) 
        for r in rollups:
            for c in r.categories:
                w.writerow([
                    r.course_name,
                    r.course_id,
                    c.group_name,
                    f"{c.weight_pct:.2f}",
                    f"{c.running_earned:.2f}",
                    f"{c.running_possible:.2f}",
                    f"{c.running_pct:.2f}" if c.running_pct is not None else "",
                    f"{c.final_earned:.2f}",
                    f"{c.final_possible:.2f}",
                    f"{c.final_pct:.2f}" if c.final_pct is not None else "",
                    f"{r.running_total_pct:.2f}" if r.running_total_pct is not None else "",
                    f"{r.final_total_pct:.2f}" if r.final_total_pct is not None else "",
                    r.policy,
                ])

# ----------------------------- CLI & Main ----------------------------- #

def parse_args():
    p = argparse.ArgumentParser(description="Canvas LMS Grade Calculator")
    p.add_argument("--base-url", help="Canvas base URL (e.g., https://school.instructure.com)")
    p.add_argument("--token", help="Canvas API token")
    p.add_argument("--course-id", type=int, help="Single course ID to process")
    p.add_argument("--course-name", help="Select a course by (case-insensitive) substring of its name; errors if multiple match")
    p.add_argument("--all-courses", action="store_true", help="Calculate grades for all active courses in your account")
    p.add_argument("--include-completed", action="store_true", help="Include completed courses when using --all-courses")
    p.add_argument("--week", action="store_true", help="List assignments due in the next 7 days across your courses (sorted by due date, then course)")
    p.add_argument(
        "--config",
        help="Path to YAML/JSON config file. If omitted, uses ./config.yaml when present.",
    )
    p.add_argument("--exclude-course-ids", help="Comma-separated list of course IDs to exclude (e.g. '101,202,303')")
    p.add_argument("--exclude-name-contains", action="append", help="Exclude courses whose name contains this text (case-insensitive); can be provided multiple times")
    p.add_argument("--weights", help='JSON mapping of assignment group name to weight (percent or raw). Example: \'{"Homework":40,"Exams":60}\'')
    p.add_argument("--weights-file", help="YAML or JSON file with weights mapping (global override)")
    p.add_argument("--final-policy", choices=[
        FinalPolicy.IGNORE_ALL,
        FinalPolicy.MISSING_ZERO_UPCOMING_IGNORE,
        FinalPolicy.ALL_ZERO,
    ], help="How to treat ungraded work when estimating the final grade (global override)")
    p.add_argument("--show-assignments", action="store_true", help="Print a table of all assignments and their status per course")
    p.add_argument("--csv", help="Path to export CSV results (single or multi-course)")
    return p.parse_args()

def load_weights_from_args(args) -> Optional[Dict[str, float]]:
    if args.weights:
        try:
            return {str(k): float(v) for k, v in json.loads(args.weights).items()}
        except Exception as e:
            raise ValueError(f"Failed to parse --weights JSON: {e}")
    if args.weights_file:
        path = args.weights_file
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        text = open(path, "r", encoding="utf-8").read()
        if path.lower().endswith((".yml", ".yaml")):
            if yaml is None:
                raise RuntimeError("PyYAML is required for YAML weights: pip install pyyaml")
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
        return {str(k): float(v) for k, v in (data or {}).items()}
    return None

def format_pct(x: Optional[float]) -> str:
    return f"{x:.2f}%" if x is not None else "—"

def main():
    args = parse_args()
    # Prefer explicit --config; otherwise auto-load ./config.yaml if present
    cfg_path = args.config if args.config else ("config.yaml" if os.path.exists("config.yaml") else None)
    cfg = load_config(cfg_path)

    # Resolve Canvas auth: CLI > config > ENV
    base_url = args.base_url or (cfg.get("canvas", {}) or {}).get("base_url") or os.getenv("CANVAS_BASE_URL")
    token = args.token or (cfg.get("canvas", {}) or {}).get("token") or os.getenv("CANVAS_TOKEN")

    if not base_url or not token:
        print("Error: Missing Canvas base_url or token. Provide via CLI, or in config under 'canvas', or set CANVAS_BASE_URL/CANVAS_TOKEN.", file=sys.stderr)
        sys.exit(2)

    # Build exclusion filters (config + CLI)
    exclude_ids, exclude_names = build_exclusions(args.exclude_course_ids, args.exclude_name_contains, cfg)

    def should_skip(name: str | None, cid: int) -> bool:
        if cid in exclude_ids:
            return True
        lname = (name or "").lower()
        return any(sub in lname for sub in exclude_names)

    client = CanvasClient(base_url, token)

    # CLI global weights (if provided) override config/Canvas
    cli_global_weights = load_weights_from_args(args)

    rollups: List[CourseRollup] = []

    # --------- Helper: parse Canvas due dates ---------
    def parse_due(dt: Optional[str]) -> Optional[datetime]:
        if not dt:
            return None
        s = dt.strip()
        # Handle trailing 'Z' and ensure fromisoformat compatibility
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        try:
            d = datetime.fromisoformat(s)
        except Exception:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)

    # --------- Special mode: --week (solitary behavior) ---------
    if args.week:
        states = ["active"] + (["completed"] if args.include_completed else [])
        now_utc = datetime.now(timezone.utc)
        end_utc = now_utc + timedelta(days=7)
        items: List[Tuple[datetime, str, int, str, float]] = []  # (due, course_name, course_id, assignment_name, points)
        seen_courses = set()
        for st in states:
            for c in client.list_my_courses(state=st):
                cid = c.get("id")
                cname = c.get("name") or ""
                if not cid or cid in seen_courses or should_skip(cname, cid):
                    continue
                seen_courses.add(cid)
                for a in client.get_assignments_with_submissions(cid):
                    if not a.published:
                        continue
                    due = parse_due(a.due_at)
                    if not due:
                        continue
                    if now_utc <= due <= end_utc:
                        items.append((due, cname, cid, a.name, float(a.points_possible or 0.0)))
        if not items:
            print("No assignments due in the next 7 days.")
            return
        items.sort(key=lambda t: (t[0], t[1].lower()))
        print("Due (UTC)              | Course                          | Assignment                          | Pts")
        print("-" * 90)
        for due, cname, cid, aname, pts in items:
            due_str = due.strftime("%Y-%m-%d %H:%M")
            print(f"{due_str:<21}| {cname[:30]:<30} | {aname[:32]:<32} | {pts:>4.0f}")
        return

    def handle_course(cid: int):
        policy = get_effective_policy(cid, args.final_policy, cfg)
        groups, groups_by_id = client.get_assignment_groups(cid)
        assignments = client.get_assignments_with_submissions(cid)
        by_group = categorize_assignments(assignments, groups_by_id)

        # Determine weights with precedence (CLI > config.by_course_id > config.default > Canvas)
        weights = get_effective_weights(cid, cli_global_weights, cfg)
        try:
            weight_plan = WeightPlan.from_user(weights, groups)
        except ValueError:
            # No usable weights from CLI/config/Canvas. Fallback to equal weights across observed groups.
            group_names = [groups_by_id[gid].name for gid in by_group.keys()] or ["(Uncategorized)"]
            auto_weights = {name: 1.0 for name in group_names}
            print(f"[INFO] No weights for course {cid}; using equal weights across {len(auto_weights)} group(s).", file=sys.stderr)
            weight_plan = WeightPlan.from_user(auto_weights, groups)

        categories = compute_category_results(by_group, groups_by_id, weight_plan, policy)
        running_total, final_total = weighted_total(categories)
        course = client.get_course(cid)
        r = CourseRollup(
            course_id=cid,
            course_name=course.get("name"),
            running_total_pct=running_total,
            final_total_pct=final_total,
            policy=policy,
            categories=categories,
        )
        rollups.append(r)

        # ---- Print course report ----
        print(f"\n=== {r.course_name} (ID {r.course_id}) ===")
        print("Weights:")
        for name, w in sorted(weight_plan.by_group_name.items(), key=lambda kv: kv[0].lower()):
            print(f"  - {name}: {w:.2f}%")
        print("Category                       Weight    Run Earn   Run Poss         Run %    Final %")
        print("-" * 86)
        for c in r.categories:
            run_pct = format_pct(c.running_pct)
            fin_pct = format_pct(c.final_pct)
            print(f"{c.group_name:<30} {c.weight_pct:6.2f}% {c.running_earned:10.2f} {c.running_possible:10.2f} {run_pct:>12} {fin_pct:>10}")
        print("-" * 86)
        print(f"Running total: {format_pct(r.running_total_pct)}")
        print(f"Final estimate: {format_pct(r.final_total_pct)}  (policy: {r.policy})")

        if args.show_assignments:
            print("\nAssignments:\n")
            print("{:<8} {:<36} {:<20} {:>7} {:>7} {:>8} {:>8}".format("GroupID", "Assignment", "Due", "Pts", "Score", "Missing", "Excused"))
            print("-" * 110)
            for a in sorted(assignments, key=lambda x: (x.assignment_group_id or -1, x.due_at or "9999")):
                sub = a.submission or Submission()
                pts = a.points_possible or 0.0
                score = "" if sub.score is None else f"{sub.score:.2f}"
                print(f"{(a.assignment_group_id or -1):<8} {a.name[:36]:<36} {str(a.due_at or '-'):<20} {pts:>7.2f} {score:>7} {str(bool(sub.missing))[:5]:>8} {str(bool(sub.excused))[:5]:>8}")

    # --------- Select courses ---------
    if args.course_id and args.course_name:
        print("Error: Provide only one of --course-id or --course-name", file=sys.stderr)
        sys.exit(2)
    if args.all_courses:
        states = ["active"] + (["completed"] if args.include_completed else [])
        seen = set()
        for st in states:
            for c in client.list_my_courses(state=st):
                cid = c.get("id")
                cname = c.get("name")
                if not cid or cid in seen or should_skip(cname, cid):
                    continue
                seen.add(cid)
                handle_course(cid)
    else:
        if args.course_name:
            query = args.course_name.lower()
            states = ["active"] + (["completed"] if args.include_completed else [])
            seen = set()
            matches: List[Tuple[int, str]] = []
            for st in states:
                for c in client.list_my_courses(state=st):
                    cid = c.get("id")
                    cname = c.get("name") or ""
                    if not cid or cid in seen:
                        continue
                    seen.add(cid)
                    if query in cname.lower():
                        if should_skip(cname, cid):
                            continue
                        matches.append((cid, cname))
            if len(matches) == 0:
                print(f"Error: No course name contains '{args.course_name}'. Use --include-completed to search completed courses or specify --course-id.", file=sys.stderr)
                sys.exit(2)
            if len(matches) > 1:
                print("Error: Multiple courses match; be more specific or use --course-id:", file=sys.stderr)
                for cid, cname in matches:
                    print(f"  - {cname} (ID {cid})", file=sys.stderr)
                sys.exit(2)
            handle_course(matches[0][0])
        else:
            if not args.course_id:
                print("Error: must provide --course-id, --course-name, or --all-courses", file=sys.stderr)
                sys.exit(2)
            if should_skip(None, args.course_id):
                print(f"Course {args.course_id} excluded")
                sys.exit(0)
            handle_course(args.course_id)

    # --------- CSV export ---------
    if args.csv:
        if len(rollups) == 1:
            export_csv_single(args.csv, rollups[0])
        else:
            export_csv_multi(args.csv, rollups)
        print(f"CSV results written to {args.csv}")

if __name__ == "__main__":
    main()
