# to run locally, use the following command in the root folder of the repository: python .github/workflows/nbchecks.py
# to check only what is staged for commit (used by .githooks/pre-commit): python .github/workflows/nbchecks.py --staged

import json, sys, os, subprocess
from pathlib import Path

def cells_with_outputs(notebook_content):
    return [index for index, cell in enumerate(notebook_content.get('cells', []))
            if cell.get('cell_type') == 'code' and cell.get('outputs')]

def has_outputs_stored(file):
    with open(file, 'r', encoding='utf-8') as f:
        notebook_content = json.load(f)

    if cells_with_outputs(notebook_content):
        print(f"The notebook {file} has outputs stored.", file=sys.stderr)
        return True
    return False

def staged_notebooks():
    # --diff-filter=ACM skips deletions; -z keeps paths with spaces intact
    completed = subprocess.run(
        ['git', 'diff', '--cached', '--name-only', '--diff-filter=ACM', '-z', '--', '*.ipynb'],
        capture_output=True, text=True, check=True)
    return [path for path in completed.stdout.split('\0') if path]

def staged_has_outputs_stored(path):
    # Read the staged blob, not the working tree. A notebook can be staged with
    # outputs and stripped on disk afterwards; the commit would still carry them.
    completed = subprocess.run(['git', 'show', f':{path}'],
                               capture_output=True, text=True, check=True)
    try:
        notebook_content = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        print(f"The notebook {path} could not be parsed: {error}", file=sys.stderr)
        return True

    offending = cells_with_outputs(notebook_content)
    if offending:
        cells = ', '.join(str(index) for index in offending)
        print(f"The notebook {path} has outputs stored (code cells: {cells}).", file=sys.stderr)
        return True
    return False

def check_all():
    exit_code = 0
    for file in Path(".").glob('**/*.ipynb'):
        filename = os.fsdecode(file)
        if has_outputs_stored(filename):
            exit_code = 1
    if exit_code == 0:
        print("All good. No stored output found.")
    return exit_code

def check_staged():
    offenders = [path for path in staged_notebooks() if staged_has_outputs_stored(path)]
    if not offenders:
        print("All good. No stored output found in staged notebooks.")
        return 0

    print("", file=sys.stderr)
    print("Commit blocked: stored cell outputs can embed tenant IDs, subscription IDs,", file=sys.stderr)
    print("resource IDs and user names. Strip them, then re-stage:", file=sys.stderr)
    print("", file=sys.stderr)
    for path in offenders:
        print(f"  jupyter nbconvert --clear-output --inplace {path}", file=sys.stderr)
    print(f"  git add {' '.join(offenders)}", file=sys.stderr)
    print("", file=sys.stderr)
    print("To bypass this check: git commit --no-verify", file=sys.stderr)
    return 1

def main(argv):
    if '--staged' in argv:
        return check_staged()
    return check_all()

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
