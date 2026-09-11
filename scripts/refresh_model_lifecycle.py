#!/usr/bin/env python3
"""Refresh the advisory 'lifecycle' metadata in shared/models.json from Azure.

The authoritative check is utils.validate_model_definitions(), which runs live at
deployment time. The 'lifecycle' field in the catalogue is a cached snapshot so a
deprecation is noticed while writing a lab rather than while deploying it. Run
this periodically (or whenever a model is added) to keep the snapshot honest.

To run locally, from the root folder of the repository:

    python scripts/refresh_model_lifecycle.py                      # dry run, report only
    python scripts/refresh_model_lifecycle.py --write              # update shared/models.json
    python scripts/refresh_model_lifecycle.py --subscription "My Subscription"

Only the 'foundry' section is checkable this way: 'external' models (AWS Bedrock,
Google Gemini, Ollama, self-hosted) are not in the Azure Cognitive Services
catalogue and are skipped.

Requires a logged-in Azure CLI (`az login`). The subscription ID is always looked
up at runtime - never hardcode one.

Exit codes: 0 = every catalogue model was found, 1 = at least one model is missing
from every queried region (the catalogue entry is wrong), 2 = could not run.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = REPO_ROOT / 'shared' / 'models.json'
MODELS_API_VERSION = '2024-10-01'

# When a model reports different statuses in different regions, the catalogue
# records the most available one: labs pin a region that offers the model, and
# the per-region detail is printed in the report anyway.
LIFECYCLE_PRECEDENCE = (
    'GenerallyAvailable',
    'Preview',
    'Legacy',
    'Deprecating',
    'Deprecated',
)


def lifecycle_rank(status: str | None) -> int:
    """Lower is more available. Unknown statuses sort last."""
    try:
        return LIFECYCLE_PRECEDENCE.index(status)  # type: ignore[arg-type]
    except ValueError:
        return len(LIFECYCLE_PRECEDENCE)


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(2)


# --------------------------------------------------------------------------
# Azure CLI
# --------------------------------------------------------------------------

def run_az(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(['az', *args], capture_output=True, text=True)
    except FileNotFoundError:
        fail("The Azure CLI ('az') is not on PATH. Install it and run 'az login', then retry.")


def resolve_subscription_id(subscription_name: str | None) -> tuple[str, str]:
    """Look the subscription ID up at runtime. Never hardcode a subscription ID."""
    if shutil.which('az') is None:
        fail("The Azure CLI ('az') is not on PATH. Install it and run 'az login', then retry.")

    if subscription_name:
        result = run_az(['account', 'list', '--query',
                         f"[?name=='{subscription_name}'].{{id:id,name:name}}", '--output', 'json'])
        if result.returncode != 0:
            fail("'az account list' failed. Are you logged in? Run 'az login' and retry.\n"
                 f"{result.stderr.strip()}")
        matches = json.loads(result.stdout or '[]')
        if not matches:
            fail(f"No subscription named '{subscription_name}' is visible to this account. "
                 "Run 'az account list --output table' to see the available names.")
        return matches[0]['id'], matches[0]['name']

    result = run_az(['account', 'show', '--output', 'json'])
    if result.returncode != 0:
        fail("No active Azure CLI session. Run 'az login' (and 'az account set --subscription "
             "\"<name>\"' if you have several), then retry.\n" + result.stderr.strip())
    account = json.loads(result.stdout or '{}')
    if not account.get('id'):
        fail("'az account show' returned no subscription ID. Run 'az login' and retry.")
    return account['id'], account.get('name', '(unnamed)')


def fetch_region_models(subscription_id: str, location: str) -> list[dict] | None:
    """Return the regional Cognitive Services model catalogue, or None on failure."""
    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/providers/Microsoft.CognitiveServices/locations/{location}/models"
        f"?api-version={MODELS_API_VERSION}"
    )
    result = run_az(['rest', '--method', 'get', '--url', url, '--output', 'json'])
    if result.returncode != 0:
        print(f"  {location}: query failed - {result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'unknown error'}",
              file=sys.stderr)
        return None
    try:
        payload = json.loads(result.stdout or '{}')
    except json.JSONDecodeError:
        print(f"  {location}: response was not valid JSON", file=sys.stderr)
        return None
    return payload.get('value', [])


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

class Finding:
    def __init__(self, role: str, name: str, version: str, publisher: str, catalogue_lifecycle: str | None):
        self.role = role
        self.name = name
        self.version = version
        self.publisher = publisher
        self.catalogue_lifecycle = catalogue_lifecycle
        self.per_region: dict[str, str] = {}       # location -> live lifecycleStatus
        self.other_versions: set[str] = set()      # same publisher + name, different version
        self.other_publishers: set[str] = set()    # same name, different format

    @property
    def live_lifecycle(self) -> str | None:
        seen = set(self.per_region.values())
        for status in LIFECYCLE_PRECEDENCE:
            if status in seen:
                return status
        return next(iter(seen), None)

    @property
    def missing_everywhere(self) -> bool:
        return not self.per_region

    @property
    def changed(self) -> bool:
        return self.live_lifecycle is not None and self.live_lifecycle != self.catalogue_lifecycle


def compare(catalog: dict, regional: dict[str, list[dict]]) -> list[Finding]:
    findings: list[Finding] = []

    for role, definition in catalog.get('foundry', {}).items():
        if role.startswith('$') or not isinstance(definition, dict):
            continue

        finding = Finding(
            role=role,
            name=definition.get('name', ''),
            version=str(definition.get('version', '')),
            publisher=definition.get('publisher', ''),
            catalogue_lifecycle=definition.get('lifecycle'),
        )

        for location, entries in regional.items():
            for entry in entries:
                model = entry.get('model', {})
                if model.get('name') != finding.name:
                    continue
                if model.get('format') != finding.publisher:
                    finding.other_publishers.add(str(model.get('format')))
                    continue
                if str(model.get('version')) != finding.version:
                    finding.other_versions.add(str(model.get('version')))
                    continue
                status = model.get('lifecycleStatus', 'unknown')
                # Keep the most available status if a region lists the model twice.
                current = finding.per_region.get(location)
                if current is None or lifecycle_rank(status) < lifecycle_rank(current):
                    finding.per_region[location] = status

        findings.append(finding)

    return findings


def print_report(findings: list[Finding], locations: list[str]) -> None:
    role_width = max([len(f.role) for f in findings] + [4])
    name_width = max([len(f.name) for f in findings] + [5])

    header = (f"{'role'.ljust(role_width)}  {'model'.ljust(name_width)}  "
              f"{'version'.ljust(11)}  {'catalogue'.ljust(18)}  {'live'.ljust(18)}  verdict")
    print(header)
    print('-' * len(header))

    for finding in findings:
        if finding.missing_everywhere:
            verdict = 'NOT FOUND'
        elif finding.changed:
            verdict = 'CHANGED'
        else:
            verdict = 'ok'
        print(f"{finding.role.ljust(role_width)}  {finding.name.ljust(name_width)}  "
              f"{finding.version.ljust(11)}  {(finding.catalogue_lifecycle or '-').ljust(18)}  "
              f"{(finding.live_lifecycle or '-').ljust(18)}  {verdict}")

    print()
    print('Per-region detail (- means the model is not offered in that region):')
    for finding in findings:
        detail = ', '.join(f"{loc}={finding.per_region.get(loc, '-')}" for loc in locations)
        print(f"  {finding.role.ljust(role_width)}  {detail}")


def print_missing(findings: list[Finding], locations: list[str]) -> None:
    missing = [f for f in findings if f.missing_everywhere]
    if not missing:
        return

    print()
    print('=' * 78)
    print(f"{len(missing)} CATALOGUE ENTRY/ENTRIES ARE WRONG - DEPLOYMENT WILL FAIL")
    print('=' * 78)
    print(f"These models were not found in ANY of the queried regions ({', '.join(locations)}).")
    print()
    for finding in missing:
        print(f"  {finding.role}: publisher='{finding.publisher}' name='{finding.name}' "
              f"version='{finding.version}'")
        if finding.other_versions:
            print(f"      the publisher offers this model at version(s): "
                  f"{', '.join(sorted(finding.other_versions))} - the catalogue version is wrong")
        if finding.other_publishers:
            print(f"      this model name is published by: {', '.join(sorted(finding.other_publishers))} "
                  f"- the catalogue publisher is wrong")
        if not finding.other_versions and not finding.other_publishers:
            print("      the name is not in the regional catalogue at all - it may have been retired, "
                  "renamed, or is only offered in a region that was not queried "
                  "(extend 'lifecycleCheckedIn' in shared/models.json)")


# --------------------------------------------------------------------------
# Writing (surgical, so key order, formatting and $comment blocks survive)
# --------------------------------------------------------------------------

ROLE_OPEN_RE = re.compile(r'^(\s{4})"([^"$][^"]*)":\s*\{\s*$')
ROLE_CLOSE_RE = re.compile(r'^\s{4}\},?\s*$')
LIFECYCLE_RE = re.compile(r'^(\s*)"lifecycle":\s*"[^"]*"(,?)\s*$')
CHECKED_ON_RE = re.compile(r'^(\s*)"lifecycleCheckedOn":\s*"[^"]*"(,?)\s*$')
SECTION_RE = re.compile(r'^\s{2}"(\w+)":\s*\{\s*$')


def write_updates(catalog_path: Path, findings: list[Finding], checked_on: str) -> list[str]:
    """Rewrite 'lifecycle' values and 'lifecycleCheckedOn' in place. Returns a change log."""
    updates = {f.role: f.live_lifecycle for f in findings if f.changed}
    changelog: list[str] = []

    lines = catalog_path.read_text(encoding='utf-8').splitlines(keepends=True)
    output: list[str] = []
    section: str | None = None
    role: str | None = None
    role_body_start = 0
    role_handled = True

    for line in lines:
        section_match = SECTION_RE.match(line)
        if section_match:
            section = section_match.group(1)
            role = None

        if section == 'foundry':
            open_match = ROLE_OPEN_RE.match(line)
            if open_match:
                role = open_match.group(2)
                role_handled = role not in updates
                role_body_start = len(output) + 1
                output.append(line)
                continue

            if role is not None and ROLE_CLOSE_RE.match(line):
                if not role_handled:
                    # No 'lifecycle' key in this role block - insert one at the end,
                    # keeping every existing key exactly where it was.
                    previous = output[-1].rstrip('\n')
                    if not previous.rstrip().endswith(','):
                        output[-1] = previous + ',\n'
                    indent = ' ' * 6
                    output.append(f'{indent}"lifecycle": "{updates[role]}"\n')
                    changelog.append(f"{role}: (absent) -> {updates[role]} (key added)")
                    role_handled = True
                role = None
                output.append(line)
                continue

            if role is not None and not role_handled and len(output) >= role_body_start:
                lifecycle_match = LIFECYCLE_RE.match(line)
                if lifecycle_match:
                    indent, comma = lifecycle_match.group(1), lifecycle_match.group(2)
                    old = line.split('"lifecycle":')[1].strip().rstrip(',').strip('"')
                    output.append(f'{indent}"lifecycle": "{updates[role]}"{comma}\n')
                    changelog.append(f"{role}: {old} -> {updates[role]}")
                    role_handled = True
                    continue

        if section is None:
            checked_match = CHECKED_ON_RE.match(line)
            if checked_match:
                indent, comma = checked_match.group(1), checked_match.group(2)
                output.append(f'{indent}"lifecycleCheckedOn": "{checked_on}"{comma}\n')
                changelog.append(f"lifecycleCheckedOn -> {checked_on}")
                continue

        output.append(line)

    catalog_path.write_text(''.join(output), encoding='utf-8')

    # A parse of the result is cheap insurance against a botched surgical edit.
    try:
        json.loads(catalog_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:
        fail(f"The rewritten catalogue is not valid JSON ({error}). Revert with 'git checkout "
             f"-- {catalog_path.relative_to(REPO_ROOT)}' and fix the script.")

    return changelog


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description='Refresh the advisory lifecycle metadata in shared/models.json from Azure.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--subscription', help='Subscription NAME (the ID is looked up at runtime)')
    parser.add_argument('--write', action='store_true',
                        help='Update shared/models.json. Default is dry-run/report-only.')
    parser.add_argument('--location', action='append', dest='locations',
                        help="Region to query; repeatable. Defaults to the catalogue's lifecycleCheckedIn.")
    parser.add_argument('--catalog', default=str(CATALOG_PATH), help='Path to models.json')
    args = parser.parse_args()

    catalog_path = Path(args.catalog)
    try:
        catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        fail(f"Model catalogue not found at '{catalog_path}'.")
    except json.JSONDecodeError as error:
        fail(f"Model catalogue '{catalog_path}' is not valid JSON: {error}")

    locations = args.locations or catalog.get('lifecycleCheckedIn') or []
    if not locations:
        fail("No regions to query. Add 'lifecycleCheckedIn' to the catalogue or pass --location.")

    subscription_id, subscription_name = resolve_subscription_id(args.subscription)
    print(f"Subscription: {subscription_name} ({subscription_id})")
    print(f"Regions:      {', '.join(locations)}")

    external_roles = [r for r in catalog.get('external', {}) if not r.startswith('$')]
    if external_roles:
        print(f"Skipping {len(external_roles)} 'external' role(s) - not deployed through Azure "
              f"Cognitive Services, so there is no lifecycle to check: {', '.join(external_roles)}")
    print()

    regional: dict[str, list[dict]] = {}
    for location in locations:
        print(f"Querying {location} ...", flush=True)
        entries = fetch_region_models(subscription_id, location)
        if entries is None:
            fail(f"Could not read the model catalogue for '{location}'. "
                 "Check that the subscription is registered for Microsoft.CognitiveServices "
                 "and that your account can read it.")
        regional[location] = entries
    print()

    findings = compare(catalog, regional)
    print_report(findings, locations)
    print_missing(findings, locations)

    changed = [f for f in findings if f.changed]
    missing = [f for f in findings if f.missing_everywhere]

    print()
    if args.write:
        checked_on = datetime.date.today().isoformat()
        changelog = write_updates(catalog_path, findings, checked_on)
        if changelog:
            print(f"Wrote {catalog_path}:")
            for entry in changelog:
                print(f"  {entry}")
        else:
            print(f"No changes written to {catalog_path}.")
    else:
        if changed:
            print(f"{len(changed)} lifecycle value(s) differ from the catalogue. "
                  "Re-run with --write to update shared/models.json.")
        else:
            print("Catalogue lifecycle values match the live Azure catalogue. "
                  "Re-run with --write to refresh 'lifecycleCheckedOn'.")

    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main())
