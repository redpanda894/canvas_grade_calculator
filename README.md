# Canvas Grade Calculator

Calculate current and projected final grades from Canvas LMS across one or many courses. Pulls assignments and assignment groups via the Canvas API, applies weights (from Canvas, config, or CLI), and reports running grades as well as final estimates under configurable policies for ungraded work.

Features
- All courses mode: process all active (and optionally completed) courses
- Single course by ID or name substring
- Exclusions by course ID or name substring
- Weights from Canvas weighted groups, config, or CLI (with clear precedence)
- Final policy options for ungraded work (ignore, treat missing as zero, or all zero)
- CSV export for single or multi-course results
- Optional per-assignment table output
- “Week” view: list assignments due in the next 7 days

Requirements
- Python 3.9+
- Packages: requests, pydantic, pyyaml

Install
1) Create a virtual environment (optional but recommended)
   python -m venv .venv && source .venv/bin/activate
2) Install dependencies from requirements.txt
   pip install -r requirements.txt

Authentication
You need your Canvas base URL and an API token with read access:
- Base URL: e.g., https://school.instructure.com
- API token: generate in your Canvas account settings

Provide auth via one of the following (highest precedence first):
1) CLI flags: --base-url and --token
2) Config file: canvas.base_url and canvas.token
3) Environment variables: CANVAS_BASE_URL and CANVAS_TOKEN

Quick Start
- Single course by ID using env vars
  export CANVAS_BASE_URL="https://school.instructure.com"
  export CANVAS_TOKEN="<your token>"
  python canvas_grade_calculator.py --course-id 123456

- All active courses using a config file
  python canvas_grade_calculator.py --all-courses --config config.yaml

Interactive Config Setup (optional)
Use the helper to create a YAML config with auth, exclusions, weights, and final policy:
  python setup_canvas_grade_config.py --out config.yaml

Configuration File
See config_example.yaml for a sample. Key sections:
- canvas: base_url and token (or omit token and use env var)
- exclusions:
  - ids: list of course IDs to skip
  - name_contains: list of substrings to skip by course name (case-insensitive)
- weights:
  - default: global weight mapping (category name -> weight). Optional.
  - by_course_id: mapping of course_id -> weight mapping
- final_policy:
  - default: global policy
  - by_course_id: per-course policy

Weights and Precedence
Effective weights per course are determined in this order:
1) CLI: --weights or --weights-file
2) Config: weights.by_course_id[course_id]
3) Config: weights.default
4) Canvas course assignment group weights
If none are available, the tool falls back to equal weights across observed groups and logs an info message.

Weight values can be raw numbers or percents; they are normalized to 100%. Matching is by assignment group name.

Final Policy Options
- ignore_all: exclude all ungraded assignments (only graded work counts)
- missing_zero_upcoming_ignore: treat marked missing as zero, ignore not-yet-due or unsubmitted items (default)
- all_zero: treat all ungraded assignments as zero

CLI Usage
  python canvas_grade_calculator.py [options]

Key flags
- --base-url: Canvas base URL
- --token: Canvas API token
- --config: path to YAML/JSON config (auto-loads ./config.yaml if present)
- --course-id: process a single course by ID
- --course-name: select a single course by a name substring (errors if multiple match)
- --all-courses: process all active courses in your account
- --include-completed: include completed courses with --all-courses or name search
- --exclude-course-ids: comma-separated course IDs to skip
- --exclude-name-contains: may be provided multiple times to skip by name substring
- --weights: JSON mapping of group name -> weight (overrides config/Canvas)
- --weights-file: YAML or JSON file containing a weights mapping
- --final-policy: ignore_all | missing_zero_upcoming_ignore | all_zero
- --show-assignments: print a per-assignment table per course
- --csv: write CSV results to the given path
- --week: list assignments due in the next 7 days across your courses (no grade calc)

Examples
- All active courses, include completed, export CSV
  python canvas_grade_calculator.py --all-courses --include-completed --csv grades.csv --config config.yaml

- Single course by ID with CLI weights and final policy override
  python canvas_grade_calculator.py \
    --course-id 210272 \
    --weights '{"Homework":20, "Exam 1":25, "Exam 2":25, "Exam 3":30}' \
    --final-policy all_zero \
    --config config.yaml

- Single course by name substring
  python canvas_grade_calculator.py --course-name "Biology" --config config.yaml

- Show per-assignment table
  python canvas_grade_calculator.py --course-id 123456 --show-assignments --config config.yaml

- Next 7 days view
  python canvas_grade_calculator.py --week --include-completed --config config.yaml

CSV Export
- Single-course export writes a summary with per-category rows and totals.
- Multi-course export writes one row per course/category with totals and policy.
Specify the file with --csv path/to/file.csv

Notes
- Keep your API token secure; prefer environment variables or a local config not committed to source control. Do not share tokens in public repos.
- Category names must match assignment group names in Canvas when using custom weights.
- If Canvas assignment groups do not have weights and you don’t provide any, the tool uses equal weights across observed groups.
