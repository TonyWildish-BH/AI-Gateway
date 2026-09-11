#!/usr/bin/env python3
"""Guard rail for the central model catalogue (shared/models.json).

Every lab must refer to models by ROLE, never by literal model name, so that a
deprecation is a one-line change in shared/models.json instead of a hunt across
a hundred notebooks. This script fails CI when a hardcoded model literal creeps
back into the labs or the shared Bicep modules.

To run locally, from the root folder of the repository:

    python scripts/check_model_catalog.py                 # report + exit code
    python scripts/check_model_catalog.py --fix-suggestions   # + how to fix each hit
    python scripts/check_model_catalog.py labs/model-routing  # scan a subset

Exit codes: 0 = clean, 1 = violations found, 2 = the script could not run.

The script never edits files. It only reports.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = REPO_ROOT / 'shared' / 'models.json'

# Which file types are meaningful per scanned root. Notebook *markdown* cells are
# never scanned - prose is allowed to name models.
SCAN_ROOTS: dict[str, set[str]] = {
    'labs': {'.ipynb', '.bicep', '.tf', '.tfvars', '.py', '.xml'},
    'modules': {'.bicep', '.json', '.xml'},
}

# --- Allowlist -------------------------------------------------------------
# Every entry here must carry a justification. Do NOT add paths just to make the
# check green; a hit in a deployable lab is a real finding.
EXCLUDED_PATH_PARTS: tuple[tuple[str, str], ...] = (
    (
        'labs/_deprecated/',
        'Archived labs. AGENTS.md explicitly says to disregard this folder; '
        'they are not deployed and are kept only for reference.',
    ),
    (
        'modules/apim/v1/specs/',
        'Vendored OpenAI/Azure OpenAPI specifications, imported verbatim into APIM. '
        'Model names there are upstream documentation examples inside "description" '
        'and "example" fields, not deployment configuration.',
    ),
    (
        'modules/apim/v2/specs/',
        'Vendored OpenAI/Azure OpenAPI specifications - see modules/apim/v1/specs/.',
    ),
    (
        'modules/apim/v3/specs/',
        'Vendored OpenAI/Azure OpenAPI specifications - see modules/apim/v1/specs/.',
    ),
    (
        'modules/apim/v4/specs/',
        'Vendored OpenAI/Azure OpenAPI specifications - see modules/apim/v1/specs/.',
    ),
    (
        'shared/models.json',
        'The catalogue itself - it is the one place a literal model name belongs.',
    ),
)

# Per-line escape hatch for the rare genuine exception. It MUST carry a reason:
#     ... # model-catalog-allow: <why this literal cannot resolve through a role>
# Bare pragmas without a reason are ignored (and therefore still fail).
ALLOW_PRAGMA = re.compile(r'model-catalog-allow:\s*(?P<reason>\S.*)')

# --- Model-shaped patterns -------------------------------------------------
# Families the repo actually uses. Anything matching these outside the catalogue
# is a hardcoded model literal, whether or not the catalogue knows the name.
MODEL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ('openai-gpt', re.compile(r'(?<![\w.\-/])gpt-[A-Za-z0-9][\w.\-]*')),
    # o3 / o4 reasoning models. Bounded on both sides so that hex blobs and
    # identifiers such as "foo_o3x" do not match.
    ('openai-o-series', re.compile(r'(?<![\w.\-/])o[34](?:-[A-Za-z][\w.\-]*)?(?![\w.])')),
    ('openai-embedding', re.compile(r'(?<![\w.\-/])text-embedding-[A-Za-z0-9][\w.\-]*')),
    ('openai-router', re.compile(r'(?<![\w.\-/])model-router(?![\w.])')),
    ('deepseek', re.compile(r'(?<![\w.\-/])DeepSeek-[A-Za-z0-9][\w.\-]*', re.IGNORECASE)),
    ('phi', re.compile(r'(?<![\w.\-/])Phi-[0-9][\w.\-]*', re.IGNORECASE)),
    ('kimi', re.compile(r'(?<![\w.\-/])Kimi-[A-Za-z0-9][\w.\-]*', re.IGNORECASE)),
    ('flux', re.compile(r'(?<![\w.\-/])FLUX[.\-][A-Za-z0-9][\w.\-]*')),
    ('gemini', re.compile(r'(?<![\w.\-/])gemini-[0-9][\w.\-]*', re.IGNORECASE)),
    # Bedrock ids only. Narrowed to a hyphenated suffix so the `anthropic` Python
    # SDK (anthropic.Anthropic(), anthropic.messages...) is not flagged.
    ('bedrock-anthropic', re.compile(r'(?<![\w.\-/])anthropic\.[a-z0-9]+(?:-[a-z0-9.]+)+(?::[0-9]+)?')),
    ('bedrock-amazon', re.compile(r'(?<![\w.\-/])us\.amazon\.[a-z0-9][\w.\-]*(?::[0-9]+)?')),
    ('huggingface-llama', re.compile(r'(?<![\w.\-/])meta-llama/[\w.\-]+')),
    ('ollama-embedding', re.compile(r'(?<![\w.\-/])mxbai-[\w.\-]+')),
)

# Substrings that mean "this is not a model reference": doc links, screenshots,
# architecture diagrams. Stripped before matching.
URL_RE = re.compile(r'\b[a-z][a-z0-9+.\-]*://[^\s"\'<>)\]]+')
IMAGE_FILE_RE = re.compile(r'[\w./\-]+\.(?:png|gif|jpe?g|svg|webp|mmd|drawio)\b', re.IGNORECASE)

# Bicep has no docstring syntax - @description() IS the documentation mechanism,
# so it is treated like a code comment. It is metadata only and never affects
# what gets deployed; the parameter it documents is still scanned.
BICEP_DOC_DECORATOR_RE = re.compile(
    r"@(?:sys\.)?description\(\s*(?:'''.*?'''|'(?:[^'\\\n]|\\.)*')\s*\)",
    re.DOTALL,
)

# Comment syntax per file type. Notebook code cells are treated as Python.
LINE_COMMENT_TOKENS: dict[str, tuple[str, ...]] = {
    '.py': ('#',),
    '.ipynb': ('#',),
    '.tf': ('#', '//'),
    '.tfvars': ('#', '//'),
    '.bicep': ('//',),
    '.xml': (),
    '.json': (),
}
BLOCK_COMMENTS: dict[str, tuple[str, str]] = {
    '.bicep': ('/*', '*/'),
    '.tf': ('/*', '*/'),
    '.tfvars': ('/*', '*/'),
    '.xml': ('<!--', '-->'),
}


class Violation:
    def __init__(self, path: str, line: int, literal: str, role: str | None, family: str, snippet: str):
        self.path = path
        self.line = line
        self.literal = literal
        self.role = role
        self.family = family
        self.snippet = snippet


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------

def load_catalog(catalog_path: Path) -> dict:
    try:
        with catalog_path.open(encoding='utf-8') as handle:
            return json.load(handle)
    except FileNotFoundError:
        sys.exit(f"Model catalogue not found at '{catalog_path}'. It should live at shared/models.json.")
    except json.JSONDecodeError as error:
        sys.exit(f"Model catalogue '{catalog_path}' is not valid JSON: {error}")


def build_literal_index(catalog: dict) -> dict[str, str]:
    """Literal model name -> catalogue role, from foundry, external and aliases."""
    index: dict[str, str] = {}

    for section in ('foundry', 'external'):
        for role, definition in catalog.get(section, {}).items():
            if role.startswith('$') or not isinstance(definition, dict):
                continue
            name = definition.get('name')
            if name:
                index.setdefault(name, role)

    for literal, role in catalog.get('aliases', {}).items():
        if literal.startswith('$'):
            continue
        index[literal] = role

    return index


def build_role_names(catalog: dict) -> set[str]:
    """Every role key in the catalogue - these are what labs SHOULD contain."""
    return {
        role
        for section in ('foundry', 'external')
        for role in catalog.get(section, {})
        if not role.startswith('$')
    }


# --------------------------------------------------------------------------
# Comment / noise stripping
# --------------------------------------------------------------------------

def blank_out(text: str, start: int, end: int) -> str:
    """Replace text[start:end] with spaces, preserving newlines and offsets."""
    chunk = ''.join('\n' if ch == '\n' else ' ' for ch in text[start:end])
    return text[:start] + chunk + text[end:]


def strip_comments(text: str, ext: str) -> str:
    """Blank out comments while preserving line numbers and string contents.

    A small state machine, rather than a regex, so that a '#' or '//' inside a
    string literal (an URL, a prompt) is not mistaken for a comment.
    """
    line_tokens = LINE_COMMENT_TOKENS.get(ext, ())
    block = BLOCK_COMMENTS.get(ext)
    quote_chars = '"\'' if ext != '.xml' else ''

    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]

        # Triple-quoted Python strings (docstrings) - skip wholesale.
        if quote_chars and ext in ('.py', '.ipynb') and text[i:i + 3] in ('"""', "'''"):
            delim = text[i:i + 3]
            end = text.find(delim, i + 3)
            i = n if end == -1 else end + 3
            continue

        if ch in quote_chars:
            delim = ch
            i += 1
            while i < n:
                if text[i] == '\\':
                    i += 2
                    continue
                if text[i] == delim or text[i] == '\n':
                    i += 1
                    break
                i += 1
            continue

        if block and text.startswith(block[0], i):
            end = text.find(block[1], i + len(block[0]))
            end = n if end == -1 else end + len(block[1])
            for j in range(i, end):
                if out[j] != '\n':
                    out[j] = ' '
            i = end
            continue

        matched_token = next((tok for tok in line_tokens if text.startswith(tok, i)), None)
        if matched_token:
            end = text.find('\n', i)
            end = n if end == -1 else end
            for j in range(i, end):
                out[j] = ' '
            i = end
            continue

        i += 1

    stripped = ''.join(out)

    if ext == '.bicep':
        for match in BICEP_DOC_DECORATOR_RE.finditer(stripped):
            stripped = blank_out(stripped, match.start(), match.end())

    return stripped


def strip_noise(line: str) -> str:
    """Blank out URLs and image/diagram filenames - never model references."""
    line = URL_RE.sub(lambda m: ' ' * len(m.group(0)), line)
    line = IMAGE_FILE_RE.sub(lambda m: ' ' * len(m.group(0)), line)
    return line


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def find_literals(line: str, role_names: set[str]) -> list[tuple[str, str]]:
    """Return [(literal, family)] for a single already-cleaned line.

    Catalogue ROLE names are skipped: they are exactly what the labs are supposed
    to use, and some of them (deepseek-chat, deepseek-reasoning) look like model
    names to the family patterns.
    """
    found: list[tuple[str, str]] = []
    claimed: list[tuple[int, int]] = []

    for family, pattern in MODEL_PATTERNS:
        for match in pattern.finditer(line):
            span = match.span()
            if any(span[0] < c_end and c_start < span[1] for c_start, c_end in claimed):
                continue
            claimed.append(span)
            literal = match.group(0).rstrip('.-')
            if literal in role_names:
                continue
            found.append((literal, family))

    return found


def scan_lines(rel_path: str, numbered_lines: list[tuple[int, str, str]], literal_index: dict[str, str],
               role_names: set[str], allowed: list[tuple[str, int, str]]) -> list[Violation]:
    """numbered_lines is [(line number, original line, comment-stripped line)]."""
    violations: list[Violation] = []

    for line_no, original, stripped in numbered_lines:
        # The pragma lives in a comment, so it is matched against the original line.
        pragma = ALLOW_PRAGMA.search(original)
        cleaned = strip_noise(stripped)
        for literal, family in find_literals(cleaned, role_names):
            if pragma:
                allowed.append((rel_path, line_no, pragma.group('reason').strip()))
                continue
            violations.append(
                Violation(rel_path, line_no, literal, literal_index.get(literal), family, original.strip())
            )

    return violations


# --------------------------------------------------------------------------
# File readers
# --------------------------------------------------------------------------

def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding='utf-8')
    except (UnicodeDecodeError, OSError):
        return None


def plain_file_lines(path: Path, ext: str) -> list[tuple[int, str, str]]:
    text = read_text(path)
    if text is None:
        return []
    originals = text.splitlines()
    stripped = strip_comments(text, ext).splitlines()
    return [(i, originals[i - 1], stripped[i - 1]) for i in range(1, len(originals) + 1)]


def notebook_lines(path: Path) -> list[tuple[int, str, str]]:
    """Code-cell source lines, mapped back to their raw line number in the .ipynb.

    Markdown cells and stored outputs are ignored entirely. The raw line number
    is recovered by walking the file's JSON string literals in document order,
    which gives contributors a location they can click on; if the notebook is not
    formatted one-source-line-per-JSON-line the mapping degrades gracefully to
    the nearest preceding raw line.
    """
    text = read_text(path)
    if text is None:
        return []

    try:
        notebook = json.loads(text)
    except json.JSONDecodeError:
        print(f"{path}: not valid JSON, skipped", file=sys.stderr)
        return []

    # Candidate raw lines: those that are a single JSON string (optionally with a
    # trailing comma), which is how Jupyter serialises each element of "source".
    raw_candidates: list[tuple[int, str]] = []
    for raw_no, raw_line in enumerate(text.splitlines(), start=1):
        candidate = raw_line.strip().rstrip(',').strip()
        if len(candidate) >= 2 and candidate.startswith('"') and candidate.endswith('"'):
            try:
                raw_candidates.append((raw_no, json.loads(candidate)))
            except json.JSONDecodeError:
                continue

    cursor = 0
    last_raw_no = 1
    result: list[tuple[int, str, str]] = []

    for cell in notebook.get('cells', []):
        if cell.get('cell_type') != 'code':
            continue
        source = cell.get('source', [])
        if isinstance(source, str):
            source = source.splitlines(keepends=True)

        cell_text = strip_comments(''.join(source), '.ipynb')
        cleaned_lines = cell_text.split('\n')

        for original, cleaned in zip(source, cleaned_lines):
            probe = cursor
            raw_no = None
            while probe < len(raw_candidates):
                if raw_candidates[probe][1] == original:
                    raw_no = raw_candidates[probe][0]
                    cursor = probe + 1
                    break
                probe += 1
            if raw_no is None:
                raw_no = last_raw_no
            last_raw_no = raw_no
            result.append((raw_no, original.rstrip('\n'), cleaned))

    return result


# --------------------------------------------------------------------------
# Walking
# --------------------------------------------------------------------------

def is_excluded(rel_path: str) -> bool:
    return any(rel_path.startswith(part) or f'/{part}' in f'/{rel_path}'
               for part, _ in EXCLUDED_PATH_PARTS)


def iter_files(targets: list[Path]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        if target.is_file():
            files.append(target)
            continue
        for path in sorted(target.rglob('*')):
            if path.is_file():
                files.append(path)
    return files


def extensions_for(rel_path: str) -> set[str]:
    for root, exts in SCAN_ROOTS.items():
        if rel_path == root or rel_path.startswith(f'{root}/'):
            return exts
    # A path outside the known roots (an explicit argument) - scan everything we
    # know how to read.
    return set().union(*SCAN_ROOTS.values())


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

FIX_HINTS = {
    '.py': "utils.model_name('{role}')   # or read shared/models.json directly if utils is not importable here",
    '.ipynb': "models_config = utils.models_config(('{role}', {{'capacity': 20}}))   # name: utils.model_name('{role}')",
    '.bicep': "loadJsonContent('../../shared/models.json').foundry['{role}'].name",
    '.tf': "jsondecode(file(\"../../shared/models.json\")).foundry[\"{role}\"].name",
    '.tfvars': "jsondecode(file(\"../../shared/models.json\")).foundry[\"{role}\"].name",
    '.xml': (
        "APIM policies cannot read the catalogue, so route the name through a named value: "
        "declare `resource nv... namedValues` in the lab's Bicep with "
        "`value: loadJsonContent('../../shared/models.json', '$.foundry.{role}.name')` "
        "and reference it here as {{{{that-named-value}}}}. "
        "See labs/apim-purview-dlp for a worked example."
    ),
}


def print_report(violations: list[Violation], show_fixes: bool) -> None:
    for violation in violations:
        if violation.role:
            print(f"{violation.path}:{violation.line}: {violation.literal} "
                  f"-> use catalogue role '{violation.role}'")
        else:
            print(f"{violation.path}:{violation.line}: {violation.literal} "
                  f"-> no catalogue role for this literal; add one to shared/models.json "
                  f"(family: {violation.family})")

        if show_fixes:
            ext = Path(violation.path).suffix.lower()
            print(f"    found in: {violation.snippet[:160]}")
            if violation.role and ext in FIX_HINTS:
                print(f"    replace with: {FIX_HINTS[ext].format(role=violation.role)}")
            elif violation.role:
                print(f"    resolve '{violation.role}' through shared/models.json rather than hardcoding the name")
            else:
                print("    add a role for this model to shared/models.json, then resolve through it")


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Fail when a hardcoded model literal appears outside shared/models.json.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('paths', nargs='*', help='Files or directories to scan (default: labs/ and modules/)')
    parser.add_argument('--fix-suggestions', action='store_true',
                        help='Print the offending source line and the catalogue lookup that replaces it')
    parser.add_argument('--catalog', default=str(CATALOG_PATH), help='Path to models.json')
    args = parser.parse_args()

    catalog = load_catalog(Path(args.catalog))
    literal_index = build_literal_index(catalog)
    role_names = build_role_names(catalog)

    if args.paths:
        targets = [Path(p).resolve() for p in args.paths]
        missing = [p for p in targets if not p.exists()]
        if missing:
            print('No such path: ' + ', '.join(str(p) for p in missing), file=sys.stderr)
            return 2
    else:
        targets = [REPO_ROOT / root for root in SCAN_ROOTS]
        targets = [t for t in targets if t.exists()]

    violations: list[Violation] = []
    allowed: list[tuple[str, int, str]] = []
    scanned = 0

    for path in iter_files(targets):
        try:
            rel_path = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel_path = path.as_posix()

        if is_excluded(rel_path):
            continue

        ext = path.suffix.lower()
        if ext not in extensions_for(rel_path):
            continue

        numbered = notebook_lines(path) if ext == '.ipynb' else plain_file_lines(path, ext)
        if not numbered:
            continue

        scanned += 1
        violations.extend(scan_lines(rel_path, numbered, literal_index, role_names, allowed))

    violations.sort(key=lambda v: (v.path, v.line))

    if allowed:
        print(f"{len(allowed)} literal(s) explicitly allowed by a 'model-catalog-allow:' pragma:")
        for rel_path, line_no, reason in allowed:
            print(f"  {rel_path}:{line_no}: {reason}")
        print()

    if not violations:
        print(f"All good. No hardcoded model literals found in {scanned} scanned file(s); "
              f"every model resolves through shared/models.json.")
        return 0

    print_report(violations, args.fix_suggestions)

    files_affected = len({v.path for v in violations})
    print()
    print(f"{len(violations)} hardcoded model literal(s) in {files_affected} file(s) "
          f"(scanned {scanned}).")
    print("Models must be referenced by ROLE so a deprecation is a one-line change in "
          "shared/models.json. See the 'Model catalogue' section of AGENTS.md.")
    if not args.fix_suggestions:
        print("Re-run with --fix-suggestions to see the replacement for each hit.")
    return 1


if __name__ == '__main__':
    sys.exit(main())
