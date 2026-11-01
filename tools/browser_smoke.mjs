/*
 * Superseded: the browser smoke test lives in tools/browser_smoke.py.
 *
 * This file was a Node/CDP version written first, then replaced when it turned
 * out node was not reachable from a Python subprocess on the target machine.
 * tools/cdp.py is a small pure-Python DevTools client, so the test needs no
 * Node, no npm and no Playwright: `python tools/browser_smoke.py`.
 *
 * Kept as a pointer rather than deleted so nobody re-implements it here.
 */
export {};
