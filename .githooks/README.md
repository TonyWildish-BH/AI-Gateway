# Git hooks

Version-controlled hooks for this repository. Git does not enable them
automatically — each clone must opt in once:

```bash
git config core.hooksPath .githooks
```

Check it took:

```bash
git config --get core.hooksPath   # -> .githooks
```

## pre-commit

Rejects any staged `.ipynb` that carries stored cell outputs.

Most labs print the current tenant ID, subscription ID and user principal name
near the top (the `az account show` cell), and `az` commands echo full ARM
resource IDs. Those land in the notebook's stored outputs and are published on
commit. On 2026-09-11 that happened here: 8 notebooks shipped 50 occurrences of
an Entra tenant ID, a subscription ID, an app registration client ID and a
corporate UPN.

Remediation after the fact is expensive. Commits in a public fork stay fetchable
by SHA through the shared fork-network object store even after a force-push, so
clearing them needs a GitHub Support request. The commit boundary is the last
point where this is free.

The hook inspects the **staged** blob, not the working tree — stripping a
notebook on disk without re-staging does not get past it.

It shares its implementation with CI (`.github/workflows/nbchecks.py`), so local
and CI verdicts cannot drift. To run the same check by hand:

```bash
python .github/workflows/nbchecks.py --staged   # staged notebooks only
python .github/workflows/nbchecks.py            # every notebook in the tree
```

To strip outputs from a notebook:

```bash
jupyter nbconvert --clear-output --inplace path/to/notebook.ipynb
```

`git commit --no-verify` bypasses the hook. CI (`notebook-checks`) still runs,
and remains the backstop.
