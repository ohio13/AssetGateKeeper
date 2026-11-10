# Contributing to AssetGatekeeper

Thank you for your interest. Please read this before opening an issue or a pull
request. It explains how the project is run, so nobody puts in time expecting
something it can't give.

## How the project is run

AssetGatekeeper is a one-person project. I develop it in my spare time for my own
projects, and it is shared openly so that anyone can use it or learn from it.
That keeps it manageable, and it means:

- **Development is ongoing.** I fix the bugs I come across and add the features
  my projects need. Every fix and feature reaches everyone who uses it.
- **I'm not taking feature requests.** It is not that ideas are unwelcome; I
  simply can't build features I don't need myself and keep the project going.
- **Bug reports are read, but replies can be slow,** and not every report will
  get one. A report is most likely to lead to a fix when it shows a problem I
  can reproduce and that would affect anyone using the tool.
- **Pull requests probably won't be reviewed or merged.** Reviewing someone
  else's change properly takes time the project doesn't have. If you have built
  something useful, please keep it in your own fork; the MIT licence lets you
  change, use and share it however you like.

Security problems are the exception: please report those privately, as
[SECURITY.md](SECURITY.md) describes.

## If you report a bug

Use the **Bug report** form and include:

- the exact line the tool printed (`FAILED: ...` or `ERROR: ...`), or the traceback;
- what you expected, and how you know (for example, the triangle count Unreal or
  Blender shows for the same file);
- your operating system, Python version (`python --version`) and
  `asset-gatekeeper --version`;
- the file itself, if you are allowed to share it. A file that shows the problem
  is far easier to fix than a description of one.

## Working on your own copy

1. Fork the repository.
2. Install it in editable mode: `python -m pip install -e ".[full,dev]"`.
3. Make your change, and add a check that fails without it. Most rule changes
   need a line in `tests/test_gatekeeper.py` or `tests/test_realworld.py`.
4. Run the main suites (or `python tools/certify.py` for all of them):

   ```bash
   python tests/gen_test_assets.py
   python tests/test_realworld.py
   python tests/test_gatekeeper.py
   python tests/test_formats.py
   python -m pyflakes asset_gatekeeper.py healer.py
   ```

Adding a new rule touches three places: the `Budget` field, the check in
`Gatekeeper`, and the rules reference in `docs/05-RULES-REFERENCE.md`. The DCC
panels share `plugins/preflight_core.py`, so a rule added there reaches Maya,
3ds Max and Blender together.

## Style the tool keeps to

- One line per asset, every problem on it, and the number that broke the rule.
- A message says what to do, not only what is wrong.
- No new runtime dependency without a very good reason: artists install this on
  their own machines.
