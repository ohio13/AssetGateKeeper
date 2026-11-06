# Contributing to AssetGatekeeper

Thank you for helping. This is an alpha, so the most useful thing you can send is
a file it gets wrong.

## Reporting a problem

Open an issue with the **Bug report** form. Please include:

- the exact line the tool printed (`FAILED: ...` or `ERROR: ...`), or the traceback;
- what you expected, and how you know (for example, the triangle count Unreal or
  Blender shows for the same file);
- your operating system, Python version (`python --version`) and
  `asset-gatekeeper --version`;
- the file itself, if you are allowed to share it. A file that reproduces the
  problem gets fixed far faster than a description of one.

## Making a change

1. Fork the repository and create a branch.
2. Install it in editable mode: `python -m pip install -e ".[full,dev]"`.
3. Make the change, and add a check that fails without it. Most rule changes need
   a line in `tests/test_gatekeeper.py` or `tests/test_realworld.py`.
4. Run the main suites before opening a pull request:

   ```bash
   python tests/gen_test_assets.py
   python tests/test_realworld.py
   python tests/test_gatekeeper.py
   python tests/test_formats.py
   python -m pyflakes asset_gatekeeper.py healer.py
   ```

5. Open a pull request saying what it fixes and how you checked it.

Adding a new rule touches three places: the `Budget` field, the check in
`Gatekeeper`, and the rules reference in `docs/05-RULES-REFERENCE.md`. The DCC
panels share `plugins/preflight_core.py`, so a rule added there reaches Maya,
3ds Max and Blender together.

## Style

- One line per asset, every problem on it, and the number that broke the rule.
- A message says what to do, not only what is wrong.
- No new runtime dependency without a very good reason: artists install this on
  their own machines.
