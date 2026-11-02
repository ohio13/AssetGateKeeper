"""Enterprise suite: config, PostgreSQL adapter, analytics, RBAC, auth, VCS, DCC.

Nothing here needs a network, a database server, an identity provider, a
Perforce server or a DCC install: every external system is injected, faked or
absent on purpose, and the *absent* cases assert the fail-closed behaviour that
matters in production.

    python tests/test_enterprise.py
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import json
import os
import shutil
import socketserver
import sys
import threading
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent.parent
for extra in (str(ROOT), str(ROOT / "server"), str(ROOT / "engine")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import re                                          # noqa: E402
import enterprise.analytics as analytics          # noqa: E402
import enterprise.auth as auth                    # noqa: E402
import enterprise.config as econfig               # noqa: E402
import enterprise.rbac as rbac                    # noqa: E402
import enterprise.remote as remote                # noqa: E402
import enterprise.store as store                  # noqa: E402
import enterprise.vcs as vcs                      # noqa: E402
import plugins.preflight_core as preflight        # noqa: E402
import asset_gatekeeper as agk                    # noqa: E402

WORK = Path("scratch") / "enterprise_tests"
if WORK.exists():
    shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)

failures = []


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def expect(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (f"  [{detail}]" if detail else ""))
    if not condition:
        failures.append(label)


def expect_raises(label, exc_type, call, contains=""):
    try:
        call()
    except exc_type as exc:
        ok = contains.lower() in str(exc).lower() if contains else True
        expect(label, ok, str(exc)[:100])
        return exc
    except Exception as exc:                       # wrong type is still a failure
        expect(label, False, f"raised {type(exc).__name__}: {exc}")
        return None
    expect(label, False, "no exception raised")
    return None


# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
print("1. configuration")

defaults = econfig.load_config()
expect("no config file means defaults", defaults.source == "defaults")
expect("defaults use SQLite, exactly as 1.0 did",
       defaults.database.is_postgres is False and not defaults.auth_enabled)

cfg_file = WORK / "enterprise.toml"
cfg_file.write_text(
    "[enterprise]\n"
    "[enterprise.database]\n"
    'dsn = "postgresql://agk@db:5432/assets"\n'
    "pool_max = 20\n"
    "[enterprise.oidc]\n"
    'issuer = "https://studio.okta.com"\n'
    'client_id = "agk-client"\n'
    'allowed_domains = ["studio.example"]\n'
    "[enterprise.rbac]\n"
    "[[enterprise.rbac.grant]]\n"
    'subject = "group:environment-leads"\n'
    'path = "/environments/**"\n'
    'permissions = ["rules.override", "rules.edit"]\n'
    "[[enterprise.rbac.grant]]\n"
    'subject = "group:contractors"\n'
    'path = "/characters/**"\n'
    'permissions = ["rules.override"]\n'
    "deny = true\n"
    "[enterprise.session]\n"
    "ttl_seconds = 3600\n",
    encoding="utf-8")

config = econfig.load_config(cfg_file)
expect("a config file is parsed", config.database.is_postgres)
expect("pool size is honoured", config.database.pool_max == 20)
expect("OIDC is detected as enabled", config.oidc.enabled and config.oidc.issuer.startswith("https://"))
expect("domain allow-list survives parsing", config.oidc.allowed_domains == ("studio.example",))
expect("two RBAC grants are loaded", len(config.rbac.grants) == 2, str(config.rbac.grants))
expect("the deny grant keeps its flag", config.rbac.grants[1].deny is True)
expect("warnings explain the missing client secret",
       any("AGK_OIDC_CLIENT_SECRET" in w for w in config.warnings), str(config.warnings))

expect_raises("an unknown key is refused, not ignored", econfig.ConfigError,
              lambda: econfig.load_config(_write(WORK / "bad1.toml",
                                                "[enterprise.database]\ndn = 'x'\n")),
              "unknown key")

expect_raises("a client_secret in a file is refused", econfig.ConfigError,
              lambda: econfig.load_config(_write(WORK / "bad2.toml",
                                                "[enterprise.oidc]\nclient_secret = 'hunter2'\n")),
              "refusing 'client_secret'")

expect_raises("a short session secret is refused", econfig.ConfigError,
              lambda: econfig.load_config(_write(WORK / "bad3.toml",
                                                "[enterprise]\nsession_secret = 'short'\n")),
              "32 characters")

os.environ["AGK_DATABASE_DSN"] = "postgresql://env/db"
env_config = econfig.load_config(cfg_file)
expect("the environment overrides the file for the DSN",
       env_config.database.dsn == "postgresql://env/db", env_config.database.dsn)
del os.environ["AGK_DATABASE_DSN"]

summary = econfig.describe(config)
expect("the summary never prints a secret", not any("hunter" in line for line in summary))
expect("the summary names the backend", any("PostgreSQL" in line for line in summary))


# --------------------------------------------------------------------------- #
# 2. Path-based RBAC
# --------------------------------------------------------------------------- #
print("2. path-based RBAC")

expect("a department subtree matches", rbac.path_matches("/environments/**", "/environments/rock/crag.obj"))
expect("a subtree pattern does not match a sibling", not rbac.path_matches("/environments/**", "/characters/rock.obj"))
expect("single-star matches exactly one level", rbac.path_matches("/characters/*", "/characters/hero.obj"))
expect("single-star refuses two levels", not rbac.path_matches("/characters/*", "/characters/hero/head.obj"))
expect("a plain folder covers its children", rbac.path_matches("/environments", "/environments/rock.obj"))
expect("matching is case-insensitive", rbac.path_matches("/Environments/**", "/environments/ROCK.obj"))
expect("backslashes are normalised", rbac.normalise_path(r"\\environments\\rock.obj") == "/environments/rock.obj")
expect_raises("traversal is refused, not resolved", rbac.RBACError,
              lambda: rbac.normalise_path("/environments/../secrets/keys.obj"), "..")

engine = rbac.AuthorizationEngine.from_specs(config.rbac.grants, default_deny=True)
env_lead = rbac.Principal(subject="u1", username="ana", groups=("environment-leads",))
contractor = rbac.Principal(subject="u2", username="bob", groups=("contractors",))
artist = rbac.Principal(subject="u3", username="cy", groups=("artists",))

decision = engine.decide(env_lead, "rules.override", "/environments/rock/crag.obj")
expect("the environment lead may override in /environments/", decision.allowed, decision.reason)
expect("the decision names the rule that allowed it",
       decision.matched is not None and decision.matched.path == "/environments/**")
expect("the same lead may not override in /characters/",
       not engine.decide(env_lead, "rules.override", "/characters/hero.obj").allowed)
expect("an unrelated artist is denied by default",
       not engine.decide(artist, "rules.override", "/environments/rock.obj").allowed,
       "default_deny")
explicit_deny = engine.decide(contractor, "rules.override", "/characters/hero.obj")
expect("the contractor's own grant allows /characters/",
       not explicit_deny.allowed, explicit_deny.reason)

deny_engine = rbac.AuthorizationEngine([
    rbac.Grant("group:artists", "/", frozenset({"rules.override"})),
    rbac.Grant("group:artists", "/environments/rock", frozenset({"rules.override"}), deny=True),
])
rock_artist = rbac.Principal(subject="u4", username="dee", groups=("artists",))
expect("broader allow still works elsewhere",
       deny_engine.decide(rock_artist, "rules.override", "/characters/a.obj").allowed)
expect("a more specific deny wins over the broad allow",
       not deny_engine.decide(rock_artist, "rules.override", "/environments/rock/b.obj").allowed)

director = rbac.Principal(subject="u5", username="eve", roles=("director",))
expect("a director is not locked out of a policy they must be able to change",
       engine.decide(director, "rules.edit", "/characters/hero.obj").allowed)
expect("a director does not silently inherit per-path grants in the report",
       engine.decide(director, "rules.edit", "/characters/hero.obj").matched is None)

expect("permissions_for lists what a UI may enable",
       "rules.override" in engine.permissions_for(env_lead, "/environments/x.obj")
       and "admin" not in engine.permissions_for(env_lead, "/environments/x.obj"))
expect("explain returns the rules touching a path",
       any(row["path"] == "/environments/**" for row in engine.explain("/environments/rock.obj")))
expect_raises("an unknown permission is refused", rbac.RBACError,
              lambda: engine.decide(env_lead, "rules.destroy", "/"), "unknown permission")
expect_raises("a typo in a grant permission is refused at construction", rbac.RBACError,
              lambda: rbac.AuthorizationEngine([rbac.Grant("user:x", "/", frozenset({"rules.overide"}))]),
              "unknown permission")
expect("the policy audit flags a global admin",
       any("admin on the whole tree" in w for w in rbac.AuthorizationEngine(
           rbac.default_grants()).audit_policy()))

# --------------------------------------------------------------------------- #
# 3. Authentication
# --------------------------------------------------------------------------- #
print("3. authentication")

secret = "k" * 40


def make_token(claims, alg="HS256", key=secret, kid="test"):
    header = base64.urlsafe_b64encode(json.dumps({"alg": alg, "typ": "JWT",
                                                  "kid": kid}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    signing_input = header + b"." + payload
    signature = hmac.new(key.encode(), signing_input, hashlib.sha256).digest()
    body = base64.urlsafe_b64encode(signature).rstrip(b"=")
    return b".".join([header, payload, body]).decode()


now = time.time()
good = make_token({"sub": "u1", "email": "ana@studio.example", "groups": ["environment-leads"],
                   "iss": "https://idp", "aud": "agk", "exp": now + 600, "iat": now,
                   "nonce": "n1"})
claims = auth.decode_jwt(good, hmac_secret=secret, issuer="https://idp", audience="agk",
                         nonce="n1")
expect("a well-formed HS256 token verifies", claims["sub"] == "u1")
expect_raises("a tampered payload fails", auth.AuthError,
              lambda: auth.decode_jwt(good[:-4] + "AAAA", hmac_secret=secret))
expect_raises("an expired token fails", auth.AuthError,
              lambda: auth.decode_jwt(make_token({"sub": "u", "exp": now - 3600}),
                                      hmac_secret=secret), "expired")
expect_raises("a token without exp is refused", auth.AuthError,
              lambda: auth.decode_jwt(make_token({"sub": "u"}), hmac_secret=secret), "expiry")
expect_raises("a wrong audience fails", auth.AuthError,
              lambda: auth.decode_jwt(good, hmac_secret=secret, audience="other"), "audience")
expect_raises("a wrong issuer fails", auth.AuthError,
              lambda: auth.decode_jwt(good, hmac_secret=secret, issuer="https://evil"), "issuer")
expect_raises("a replayed nonce fails", auth.AuthError,
              lambda: auth.decode_jwt(good, hmac_secret=secret, nonce="n2"), "nonce")
alg_none = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode() + "." + \
    base64.urlsafe_b64encode(b'{"sub":"u","exp":9999999999}').rstrip(b"=").decode() + "."
expect_raises("alg=none is refused before any signature check", auth.AuthError,
              lambda: auth.decode_jwt(alg_none, hmac_secret=secret), "unsigned")
expect_raises("an HMAC token cannot masquerade as RSA (algorithm confusion)",
              auth.AuthError,
              lambda: auth.decode_jwt(make_token({"sub": "u", "exp": now + 60}, alg="RS256"),
                                      hmac_secret=secret), "no public key")
# Hide the package whether or not this machine has it: the check is about the
# message a studio sees without it, and used to fail wherever it was installed.
_hidden = {name: None for name in ("cryptography", "cryptography.exceptions",
                                   "cryptography.hazmat", "cryptography.hazmat.primitives",
                                   "cryptography.hazmat.primitives.asymmetric")}
_saved = {name: sys.modules.get(name) for name in _hidden}
sys.modules.update(_hidden)
try:
    expect_raises("RS256 without the cryptography package fails closed with a fix",
                  auth.AuthError,
                  lambda: auth.decode_jwt(make_token({"sub": "u", "exp": now + 60}, alg="RS256"),
                                          public_key_pem=b"-----BEGIN PUBLIC KEY-----\n-----END PUBLIC KEY-----\n"),
                  "cryptography")
finally:
    for _name, _module in _saved.items():
        if _module is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _module

oidc_config = econfig.OIDCConfig(issuer="https://idp", client_id="agk",
                                 allowed_domains=("studio.example",))
principal = auth.principal_from_claims(claims, oidc_config)
expect("claims become a principal", principal.username == "ana@studio.example"
       and principal.groups == ("environment-leads",))
expect_raises("an off-domain email is refused", auth.AuthError,
              lambda: auth.principal_from_claims({**claims, "email": "x@gmail.com"},
                                                 oidc_config), "allow-list")

discovery = {"issuer": "https://idp", "authorization_endpoint": "https://idp/auth",
             "token_endpoint": "https://idp/token", "jwks_uri": "https://idp/jwks"}
client = auth.OIDCClient(oidc_config, opener=lambda url, timeout: discovery,
                         poster=lambda url, payload, timeout: {"id_token": good})
expect("discovery is cached and used", client.discovery()["token_endpoint"] == "https://idp/token")
url = client.authorization_url("state1", "n1", "verifier-verifier-verifier")
expect("the authorization URL carries PKCE S256", "code_challenge_method=S256" in url)
expect("the authorization URL carries state and nonce",
       "state=state1" in url and "nonce=n1" in url)
expect_raises("an ID token for another issuer is refused", auth.AuthError,
              lambda: auth.OIDCClient(
                  econfig.OIDCConfig(issuer="https://other", client_id="agk"),
                  opener=lambda url, timeout: discovery,
                  poster=lambda url, payload, timeout: {"id_token": good}).complete_login("c", "v", "n1"),
              "issuer")

pem = auth._rsa_jwk_to_pem({"kty": "RSA", "kid": "k1",
                            "n": base64.urlsafe_b64encode(b"\x00\xc1" * 64).rstrip(b"=").decode(),
                            "e": "AQAB"})
expect("an RSA JWK converts to a PEM public key",
       pem.startswith(b"-----BEGIN PUBLIC KEY-----") and b"END PUBLIC KEY" in pem)

session_config = econfig.SessionConfig(secret=secret, ttl_seconds=60)
codec = auth.SessionCodec(session_config)
cookie = codec.issue(principal, now=1000)
restored = codec.verify(cookie, now=1010)
expect("a session cookie round-trips the principal", restored.username == principal.username)
expect("the session keeps the groups for RBAC", restored.groups == principal.groups)
expect_raises("an expired session is refused", auth.AuthError,
              lambda: codec.verify(cookie, now=2000), "expired")
expect_raises("a forged session is refused", auth.AuthError,
              lambda: codec.verify(cookie[:-2] + "xy", now=1010), "signature")
expect_raises("a session needs a real secret", auth.AuthError,
              lambda: auth.SessionCodec(econfig.SessionConfig(secret="short")))

state, nonce, verifier = auth.new_login_state()
expect("login state is unpredictable and long",
       len({state, nonce, verifier}) == 3 and len(verifier) > 40)

saml = auth.SamlHandler(econfig.SAMLConfig(entity_id="https://agk/sp",
                                           sso_url="https://idp/sso",
                                           audience="https://agk/sp"))
saml_url, request_id = saml.build_authn_request("relay")
expect("an AuthnRequest is built with a unique id",
       saml_url.startswith("https://idp/sso?") and request_id.startswith("_"))

unsigned = base64.b64encode(
    b'<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">'
    b'<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">'
    b'<saml:Subject><saml:NameID>ana</saml:NameID></saml:Subject>'
    b"</saml:Assertion></samlp:Response>").decode()
expect_raises("an unsigned assertion is refused", auth.AuthError,
              lambda: saml.parse_response(unsigned), "unsigned")

signed_marker = base64.b64encode(b"<Response><Signature>Zm9v</Signature></Response>").decode()
expect_raises("a signed assertion without signxml fails closed with a fix", auth.AuthError,
              lambda: auth.SamlHandler(econfig.SAMLConfig(
                  entity_id="e", sso_url="s", x509_cert="CERT")).parse_response(signed_marker),
              "signxml")

lab = auth.SamlHandler(econfig.SAMLConfig(entity_id="e", sso_url="s",
                                          require_signature=False, allow_unsigned=True))
lab_principal = lab.parse_response(unsigned)
expect("the explicit lab opt-out still parses the identity",
       lab_principal.username == "ana" and lab_principal.auth_method == "saml")

# --------------------------------------------------------------------------- #
# 4. Storage: SQLite today, PostgreSQL when a DSN is supplied
# --------------------------------------------------------------------------- #
print("4. storage")

translator = store.SqlTranslator()
expect("placeholders are rewritten", translator.translate("SELECT * FROM t WHERE a = ? AND b = ?")
       == "SELECT * FROM t WHERE a = %s AND b = %s")
expect("PRAGMA becomes a no-op", translator.translate("PRAGMA journal_mode = WAL") == "")
expect("GROUP_CONCAT with a newline separator becomes string_agg",
       "string_agg(p.message, chr(10))" in translator.translate(
           "SELECT GROUP_CONCAT(p.message, char(10)) FROM problems p"))
upsert = translator.translate("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)")
expect("INSERT OR REPLACE becomes an exact upsert",
       "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value" in upsert, upsert)
expect("the upsert keeps the placeholder translation",
       "%s" in upsert and "?" not in upsert, upsert)
expect_raises("an unknown table in an upsert is refused rather than guessed",
              store.StoreError,
              lambda: translator.translate("INSERT OR REPLACE INTO mystery (a) VALUES (1)"),
              "no known conflict key")

schema = """
CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    filename TEXT NOT NULL UNIQUE COLLATE NOCASE
);
"""
statements = translator.schema_statements(schema)
expect("AUTOINCREMENT becomes an identity column",
       all("AUTOINCREMENT" not in s.upper() for s in statements)
       and any("GENERATED BY DEFAULT AS IDENTITY" in s for s in statements))
expect("COLLATE NOCASE is dropped for PostgreSQL",
       all("COLLATE" not in s.upper() for s in statements))
expect("foreign keys are moved into a second pass",
       all("REFERENCES" not in s for s in statements if "CREATE TABLE" in s.upper())
       and any("ALTER TABLE verdicts ADD CONSTRAINT" in s for s in statements), str(statements))
expect("the foreign key keeps its ON DELETE rule",
       any("ON DELETE SET NULL" in s for s in statements))

expect("DSN detection distinguishes the engines",
       store.dialect_for("postgresql://u@h/db") is store.POSTGRES
       and store.dialect_for("metrics.db") is store.SQLITE)
expect_raises("PostgreSQL without psycopg fails with an install hint", store.StoreError,
              lambda: store.open_store("postgresql://u@127.0.0.1:1/db"), "psycopg")

db_path = WORK / "metrics.db"
metrics = store.open_store(db_path)
import server.asset_health_server as health  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
metrics.executescript(health.SCHEMA)
expect("the real AssetHealth schema runs on SQLite through the store", True)
metrics.execute("INSERT INTO projects (name, api_token, created_at) VALUES (?, ?, ?)",
                ("demo", "t0k", "2024-01-01T00:00:00"))
metrics.execute("INSERT INTO verdicts (timestamp, received_at, filename, passed, status, metric,"
                " project_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("2024-01-02T10:00:00", "2024-01-02T10:00:01", "rock.obj", 0, "FAILED",
                 "72,500 tris / 4 mat", 1))
metrics.execute("INSERT INTO verdicts (timestamp, received_at, filename, passed, status, metric)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("2024-02-02T10:00:00", "2024-02-02T10:00:01", "rock.obj", 1,
                 "PASSED", "12,000 tris / 2 mat"))
metrics.execute("INSERT INTO verdicts (timestamp, received_at, filename, passed, status, metric)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("2024-03-02T10:00:00", "2024-03-02T10:00:01", "hero.obj", 1,
                 "HEALED", "30,000 tris / 3 mat"))
for verdict_id, message in ((1, "72,500 triangles exceeds the 50,000 budget (+22,500)"),
                            (1, "4 material slots exceeds the 3 slot budget (draw calls)"),
                            (1, "72,500 triangles exceeds the 50,000 budget (+22,500)")):
    metrics.execute("INSERT INTO problems (verdict_id, message) VALUES (?, ?)",
                    (verdict_id, message))
rows = metrics.query("SELECT COUNT(*) AS n FROM verdicts")
expect("rows are readable through the store", rows[0]["n"] == 3)

# --------------------------------------------------------------------------- #
# 5. Analytics
# --------------------------------------------------------------------------- #
print("5. analytics")

expect("relative windows are understood", analytics.parse_since("90d") is not None)
expect_raises("a nonsense window is refused", analytics.AnalyticsError,
              lambda: analytics.parse_since("soon"), "window")
expect("bucket expressions differ per engine",
       "strftime" in analytics.bucket_expression("sqlite", "timestamp", "month")
       and "to_char" in analytics.bucket_expression("postgres", "timestamp", "month"))

trend = analytics.compliance_trend(metrics, bucket="month")
expect("one point per month", [p.bucket for p in trend.points] ==
       ["2024-01", "2024-02", "2024-03"], str([p.bucket for p in trend.points]))
expect("the first month is a failure", trend.points[0].compliance == 0.0)
expect("a healed asset counts as acceptable", trend.points[2].compliance == 1.0)
expect("the report carries its own window",
       trend.window["bucket"] == "month")
expect("the delta summarises the direction of travel", trend.delta > 0, str(trend.delta))
expect("the ascii renderer produces one line per bucket",
       len(analytics.render_ascii(trend).splitlines()) == 3)
summary = analytics.compliance_summary(metrics)
expect("the summary exposes its denominator",
       summary["total"] == 3 and summary["acceptable"] == 2, str(summary))
expect("a healed asset is not counted twice",
       summary["compliance"] < 1.0, str(summary))

growth = analytics.polygon_growth(metrics, bucket="month")
expect("polygon growth reads the metric text",
       growth.points[0].peak_triangles == 72500, str(growth.as_dict()))
expect("growth reports the average, not just the peak",
       growth.points[0].average_triangles == 72500)
expect("triangle parsing handles thousands separators",
       analytics.triangles_from_metric("12,345 tris / 3 mat") == 12345)
expect("an unreadable metric yields None, never zero",
       analytics.triangles_from_metric("unverified") is None)

clusters = analytics.error_frequency(metrics)
expect("recurring errors are clustered by signature", len(clusters) == 2, str(len(clusters)))
expect("the repeated problem is counted twice", clusters[0].count == 2)
expect("numbers are folded out of the signature",
       "#" in clusters[0].signature and "72" not in clusters[0].signature)
expect("clusters keep an example for the human", "exceeds" in clusters[0].example)
expect("clusters keep the first and last sighting",
       clusters[0].first_seen and clusters[0].last_seen)

expect_raises("growth over an empty window says why it is empty",
              analytics.AnalyticsError,
              lambda: analytics.polygon_growth(metrics, since="2099-01-01"), "no triangle counts")

# --------------------------------------------------------------------------- #
# 6. Version control: LFS pointers and Perforce
# --------------------------------------------------------------------------- #
print("6. version control")

pointer = (b"version https://git-lfs.github.com/spec/v1\n"
           b"oid sha256:" + b"a" * 64 + b"\nsize 41943040\n")
expect("a pointer is recognised", vcs.is_lfs_pointer(pointer))
parsed = vcs.parse_lfs_pointer(pointer)
expect("the pointer exposes the real size", parsed.size == 41943040)
expect("the pointer exposes the oid", parsed.oid == "a" * 64)
expect("the short oid is for humans", parsed.short_oid == "aaaaaaaaaaaa")
expect("a round-trip reproduces the pointer bytes", parsed.as_bytes() == pointer)
expect("a real file is not a pointer", not vcs.is_lfs_pointer(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40))
expect_raises("a truncated pointer is refused", vcs.VCSError,
              lambda: vcs.parse_lfs_pointer(b"version https://git-lfs.github.com/spec/v1\n"), "oid or size")
expect_raises("an unknown pointer version is refused", vcs.VCSError,
              lambda: vcs.parse_lfs_pointer(pointer.replace(b"spec/v1", b"spec/v9")))

pointer_file = WORK / "normal_Albedo.png"
pointer_file.write_bytes(pointer)
expect("a pointer on disk is detected", vcs.is_pointer_file(pointer_file))

tagged = ("... change 42\n... depotFile //depot/main/rock.obj\n... action edit\n... rev 7\n"
          "... depotFile //depot/main/rock.png\n... action add\n... rev 1\n")
records = vcs.split_records(tagged)
expect("ztag records split per file", len(records) == 2, str(records))
expect("ztag keys are parsed", records[0]["action"] == "edit" and records[0]["rev"] == "7")
expect("tagged key/value parsing works for p4 info",
       vcs.parse_tagged("... userName ana\n... clientName ws1\n")["clientName"] == "ws1")


class FakeP4:
    """A p4 that answers the four commands this client is allowed to use."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout=30.0, env=None):
        self.calls.append(list(argv))
        args = [a for a in argv if not a.startswith("-z")]
        if "describe" in args:
            return vcs.CommandResult(tuple(argv), 0, tagged, "")
        if "print" in args:
            destination = Path(args[args.index("-o") + 1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"# fake depot content\nv 0 0 0\nv 1 0 0\nv 1 1 0\nf 1 2 3\n")
            return vcs.CommandResult(tuple(argv), 0, "", "")
        if "info" in args:
            return vcs.CommandResult(tuple(argv), 0, "... userName ana\n... clientName ws1\n", "")
        return vcs.CommandResult(tuple(argv), 1, "", "unknown command")


fake = FakeP4()
client = vcs.PerforceClient(econfig.VCSConfig(p4_port="ssl:p4:1666", p4_user="ana"), executor=fake)
ok, reason = client.available()
expect("the client reports a reachable server", ok and reason == "")
described = client.describe("42")
expect("a changelist is described", len(described) == 2 and described[0].depot_file.endswith("rock.obj"))
expect("deleted files carry no content", vcs.DepotFile("//d/x", "delete").has_content is False)

fetched = client.materialise("42", WORK / "cl42", suffixes={".obj"})
expect("materialise fetches only the wanted suffixes", len(fetched) == 1, str(fetched))
expect("materialise writes real bytes to a temp tree", fetched[0].stat().st_size > 0)
expect("materialise used p4 print, never a sync",
       any("print" in " ".join(call) for call in fake.calls))
expect("the depot structure is preserved in the temp tree",
       "depot" in str(fetched[0]).replace("\\", "/"))
expect("p4 is never invoked through a shell",
       all(isinstance(part, str) for call in fake.calls for part in call))

# --------------------------------------------------------------------------- #
# 7. Remote validation and the runner farm
# --------------------------------------------------------------------------- #
print("7. remote validation and runners")

expect("a URL is classified", remote.parse_source("https://art/rock.obj").kind == "url")
depot_source = remote.parse_source("//depot/main/rock.obj@7")
expect("a depot path keeps its revision",
       depot_source.kind == "depot" and depot_source.revision == "7")
expect("a changelist is classified", remote.parse_source("12345").kind == "changelist")
expect_raises("an ambiguous source is refused", remote.RemoteError,
              lambda: remote.parse_source("rock.obj"), "cannot tell")

served = WORK / "served"
served.mkdir()
(served / "cube.obj").write_text(
    "v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3\nf 1 3 4\n", encoding="utf-8")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        return


handler = lambda *a, **kw: QuietHandler(*a, directory=str(served), **kw)  # noqa: E731
httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base_url = f"http://127.0.0.1:{httpd.server_address[1]}"

fetched_path = remote.Fetcher(timeout=5.0, max_bytes=1024 * 1024).fetch(
    base_url + "/cube.obj", WORK / "downloaded.obj")
expect("an asset is fetched over HTTP", fetched_path.stat().st_size > 0)
expect_raises("a 404 is reported clearly", remote.RemoteError,
              lambda: remote.Fetcher(timeout=5.0).fetch(base_url + "/missing.obj",
                                                        WORK / "missing.obj"), "HTTP 404")
expect_raises("the size cap is enforced",
              remote.RemoteError,
              lambda: remote.Fetcher(timeout=5.0, max_bytes=10).fetch(
                  base_url + "/cube.obj", WORK / "toolarge.obj"), "cap")

report = remote.validate_sources([remote.Source("url", base_url + "/cube.obj")],
                                 WORK / "remote_run")
expect("a remote asset is gated without a working copy", report.ok, str(report.as_dict())[:120])
expect("the report names the source it validated",
       report.results[0].source.endswith("cube.obj"))

gate_vcs = econfig.VCSConfig()
p4_client = vcs.PerforceClient(gate_vcs, executor=fake)
cl_report = remote.validate_sources([remote.Source("changelist", "42")],
                                    WORK / "remote_cl", perforce=p4_client)
expect("a changelist is validated straight from the depot",
       len(cl_report.results) >= 1 and cl_report.results[0].source.startswith("//") is False,
       str(cl_report.as_dict())[:160])

hub = remote.RunnerHub(token="tok").start()
try:
    job = hub.submit([base_url + "/cube.obj"], project="demo")
    worker = remote.RunnerClient(hub.url, token="tok", name="r1", timeout=5.0)
    claimed = worker.claim()
    expect("a runner claims the queued job", claimed is not None and claimed.id == job.id)
    expect("the claim carries the sources", claimed.sources[0].endswith("cube.obj"))
    expect("an idle hub answers 204, not an error",
           remote.RunnerClient(hub.url, token="tok", timeout=5.0).claim() is None)
    hub.submit([base_url + "/cube.obj"], project="demo")
    worked = worker.work_once(WORK / "runner_work")
    expect("the runner validates and posts a report",
           worked is not None and "ok" in worked, str(worked)[:120])
    expect("the hub stores the result", bool(hub.results))
    unauth = remote.RunnerClient(hub.url, token="wrong", timeout=5.0)
    expect_raises("a bad runner token is rejected", remote.RemoteError,
                  lambda: unauth.claim(), "401")
finally:
    hub.stop()
    httpd.shutdown()
    httpd.server_close()

# --------------------------------------------------------------------------- #
# 8. DCC pre-flight core
# --------------------------------------------------------------------------- #
print("8. DCC pre-flight")

rules_file = WORK / "rules.toml"
rules_file.write_text("[mesh]\nmax_triangles = 1500\nmax_material_slots = 1\n"
                      "[texture]\nmax_edge = 64\npower_of_two = true\n", encoding="utf-8")
budget = preflight.load_budget(rules_file)

good_report = preflight.assess(asset="crate.obj", triangles=1200, vertices=900,
                               materials=1, budget=budget)
expect("an in-budget asset is clean", good_report.ok and not good_report.findings)
expect("the report states the budget it used", "1,500" in good_report.budget_summary,
       good_report.budget_summary)

over_report = preflight.assess(asset="boss.obj", triangles=4000, materials=4,
                               degenerate=2,
                               textures=[("boss_Albedo.png", 256, 256)],
                               budget=budget)
expect("an over-budget asset is blocked", not over_report.ok)
codes = {finding.code for finding in over_report.errors}
expect("triangle, material, degenerate and texture errors are all raised",
       {"tri_budget", "mat_budget", "degenerate", "tex_size"} <= codes, str(codes))
expect("the message says how far over the budget it is",
       any("+2,500" in f.message for f in over_report.errors))
expect("non-power-of-two is a warning, not a block",
       any(f.code == "tex_pot" and f.severity == "warning"
           for f in preflight.assess(asset="x", textures=[("a.png", 100, 64)],
                                     budget=budget).findings))
expect("errors sort before warnings",
       over_report.sorted_findings()[0].severity == "error")
expect("the text report is readable in any DCC",
       "BLOCKED" in over_report.as_text() and "triangles" in over_report.as_text())

tag_budgets = {"character": agk.Budget(), "environment rock": agk.Budget()}
typo = preflight.assess(asset="hero.obj", triangles=100, folder="charcter",
                        tag_budgets=tag_budgets, tag_mode="strict", budget=budget)
expect("a misspelled department folder is caught inside the DCC", not typo.ok, str(typo.findings))
expect("the panel suggests the folder the artist meant",
       typo.tag_suggestion is not None and typo.tag_suggestion[1] == "character",
       str(typo.tag_suggestion))

lenient = preflight.assess(asset="hero.obj", triangles=100, folder="charcter",
                           tag_budgets=tag_budgets, tag_mode="suggest", budget=budget)
expect("the same typo is a warning outside strict mode", lenient.ok and lenient.warnings)

good_tag = preflight.assess(asset="rock.obj", triangles=100, folder="Environment Rock",
                            tag_budgets=tag_budgets, budget=budget)
expect("a correct folder resolves to its tag", good_tag.tag == "environment rock", str(good_tag.tag))
expect("a normalised folder name is not reported as a typo", good_tag.tag_suggestion is None)

# --------------------------------------------------------------------------- #
# 9. Resilience primitives
# --------------------------------------------------------------------------- #
print("9. circuit breakers, retries and the health loop")

import enterprise.resilience as rz                 # noqa: E402

clock_now = [0.0]
breaker = rz.CircuitBreaker("db", failure_threshold=2, reset_timeout=10.0,
                            clock=lambda: clock_now[0])
expect("a fresh breaker admits traffic", breaker.allow() and breaker.state is rz.State.CLOSED)
breaker.record_failure("boom")
expect("one failure is not enough to trip it", breaker.allow())
breaker.record_failure("boom again")
expect("it trips after the threshold", breaker.state is rz.State.OPEN and not breaker.allow())
expect("the breaker remembers why it opened", "boom again" in breaker.stats().last_error)
clock_now[0] = 10.5
expect("after the cooldown it half-opens", breaker.state is rz.State.HALF_OPEN)
expect("half-open admits exactly one probe", breaker.allow())
expect("the second probe is refused until the first answers", not breaker.allow())
breaker.record_success()
expect("a successful probe closes it again",
       breaker.state is rz.State.CLOSED and breaker.allow())

sleeps = []
def flaky():
    flaky.calls += 1
    if flaky.calls < 3:
        raise OSError("transient")
    return "ok"
flaky.calls = 0
expect("a retrying call succeeds after transient failures",
       rz.retry(flaky, attempts=3, base_delay=0.01, rng=__import__("random").Random(1),
                sleep=sleeps.append) == "ok")
expect("it backed off between attempts and not after success", len(sleeps) == 2, str(sleeps))
def always_fails():
    raise OSError("still down")
expect_raises("retries are bounded, not infinite", OSError,
              lambda: rz.retry(always_fails, attempts=2, base_delay=0.0, sleep=lambda _: None))

ping_ok = [False]
monitor = rz.HealthMonitor(lambda: (_ for _ in ()).throw(OSError("down")) if not ping_ok[0]
                           else True, name="db", interval=0.01, breaker=breaker)
sample = monitor.check_now()
monitor.check_now()
expect("a failed ping is recorded and trips the breaker",
       sample.ok is False and breaker.state is rz.State.OPEN, str(sample))
ping_ok[0] = True
sample = monitor.check_now()
expect("a good ping closes it again", sample.ok and breaker.state is rz.State.CLOSED)
expect("the monitor keeps a history", len(monitor.history) == 3)
expect("the monitor reports counters", monitor.as_dict()["checks"] == 3)

# --------------------------------------------------------------------------- #
# 10. Versioned migrations
# --------------------------------------------------------------------------- #
print("10. versioned migrations")

import shutil as _shutil                            # noqa: E402
from enterprise.migrations import (MigrationError, MigrationRunner,  # noqa: E402
                                   discover, effective_statements, lint)

repo_migrations = discover(ROOT / "migrations")
expect("the shipped migrations are found and ordered",
       [m.key for m in repo_migrations] == ["0001_baseline", "0002_verdict_metrics"],
       str([m.key for m in repo_migrations]))
expect("checksums are stable across reads",
       repo_migrations[0].checksum == discover(ROOT / "migrations")[0].checksum)
expect("the baseline covers every table the server creates",
       {"verdicts", "problems", "projects", "budgets", "alerts_state",
        "admin_users", "admin_sessions", "rule_audit", "meta",
        "modifications", "project_roots"} <=
       set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", repo_migrations[0].up)))
expect("the shipped migrations pass the zero-downtime linter on PostgreSQL",
       lint(repo_migrations[0], "postgres") == [] and lint(repo_migrations[1], "postgres") == [],
       str(lint(repo_migrations[0], "postgres")))
expect("and on SQLite", lint(repo_migrations[0], "sqlite") == [])

pg_statements = effective_statements(repo_migrations[1].up, "postgres")
expect("indexes become CONCURRENTLY on PostgreSQL",
       any("CREATE INDEX CONCURRENTLY" in s for s in pg_statements), str(pg_statements)[:160])
expect("foreign keys are added NOT VALID so the table is not scanned",
       any("NOT VALID" in s and "ADD CONSTRAINT" in s for s in pg_statements),
       str(pg_statements)[:160])
expect("identity columns replace AUTOINCREMENT",
       any("GENERATED BY DEFAULT AS IDENTITY" in s for s in
           effective_statements(repo_migrations[0].up, "postgres")))
expect("SQLite gets the plain statements", all("CONCURRENTLY" not in s for s in
       effective_statements(repo_migrations[1].up, "sqlite")))

blocking = WORK / "blocking_migrations"
if blocking.exists():
    _shutil.rmtree(blocking)
blocking.mkdir(parents=True)
(blocking / "0001_risky.up.sql").write_text(
    "ALTER TABLE verdicts DROP COLUMN metric;", encoding="utf-8")
(blocking / "0001_risky.down.sql").write_text("SELECT 1;", encoding="utf-8")
risky = discover(blocking)[0]
expect("a blocking migration is refused by the linter",
       any("DROP COLUMN" in problem for problem in lint(risky, "postgres")),
       str(lint(risky, "postgres")))
(blocking / "0001_risky.up.sql").write_text(
    "-- agk:allow-blocking\nALTER TABLE verdicts DROP COLUMN metric;", encoding="utf-8")
risky = discover(blocking)[0]
expect("the marker lets an operator override it on purpose",
       lint(risky, "postgres") == [], str(lint(risky, "postgres")))

(blocking / "0003_gap.up.sql").write_text("SELECT 1;", encoding="utf-8")
(blocking / "0003_gap.down.sql").write_text("SELECT 1;", encoding="utf-8")
expect_raises("a gap in the sequence is refused", MigrationError,
              lambda: discover(blocking), "contiguous")
if (WORK / "half").exists():
    _shutil.rmtree(WORK / "half")
(WORK / "half").mkdir(parents=True)
_write(WORK / "half" / "0001_x.up.sql", "SELECT 1;")
expect_raises("a missing down script is refused", MigrationError,
              lambda: discover(WORK / "half"), "both")

mig_db = WORK / "migrated.db"
mig_store = store.open_store(mig_db)
runner = MigrationRunner(mig_store, ROOT / "migrations")
expect("the plan lists every pending migration",
       [m.key for m in runner.plan()] == ["0001_baseline", "0002_verdict_metrics"])
dry = runner.apply(dry_run=True)
expect("a dry run reports the steps without writing",
       len(dry) == 2 and runner.applied() == {}, str(dry))
applied = runner.apply()
expect("apply runs both migrations", [step["key"] for step in applied] ==
       ["0001_baseline", "0002_verdict_metrics"])
expect("the history is recorded with checksums",
       runner.applied()[1]["checksum"] == repo_migrations[0].checksum)
expect("verify is clean after a rollout", runner.verify() == [], str(runner.verify()))
expect("nothing is left to plan", runner.plan() == [])
expect("the tables the code needs actually exist",
       runner.check_tables(["verdicts", "projects", "verdict_metrics"]) == [])

held = WORK / "drifted_migrations"
if held.exists():
    _shutil.rmtree(held)
_shutil.copytree(ROOT / "migrations", held)
(held / "0002_verdict_metrics.up.sql").write_text(
    (held / "0002_verdict_metrics.up.sql").read_text(encoding="utf-8") + "\n-- edited\n",
    encoding="utf-8")
drift = MigrationRunner(mig_store, held).verify()
expect("editing an applied migration is detected",
       any("checksum changed" in problem for problem in drift), str(drift))

rolled = runner.rollback(to=0)
expect("rollback runs the down scripts newest-first",
       [step["key"] for step in rolled] == ["0002_verdict_metrics", "0001_baseline"])
expect("rollback empties the history", runner.applied() == {})
expect("rollback removed the tables",
       len(runner.check_tables(["verdicts", "verdict_metrics"])) == 2)
mig_store.close()

# --------------------------------------------------------------------------- #
# 11. Audit and SIEM forwarding
# --------------------------------------------------------------------------- #
print("11. audit and SIEM forwarding")

import enterprise.audit as audit                   # noqa: E402

event = audit.AuditEvent(event="rbac.denied", severity="notice", actor="ana",
                         action="rules.override", path="/environments/rock.obj",
                         outcome="denied", detail={"token": "s3cret", "why": "no grant"})
payload = json.loads(event.as_json())
expect("events carry a versioned schema and a timestamp",
       payload["schema"] == audit.SCHEMA and payload["ts"].endswith("Z"))
expect("secrets are redacted before serialisation",
       payload["detail"]["token"] == audit.REDACTED and payload["detail"]["why"] == "no grant")
expect("redaction is recursive", audit.redact({"a": {"password": "x"}})["a"]["password"]
       == audit.REDACTED)
expect("long values are truncated", len(audit.redact("x" * 5000)) <= 400)
expect("an unknown severity is refused", True)
expect_raises("an unknown severity is rejected", audit.AuditError,
              lambda: audit.AuditEvent(event="x", severity="shout"))

audit_file = WORK / "audit.jsonl"
file_emitter = audit.JsonLineEmitter(audit_file)
sink = []
class Grabber:
    def emit(self, item):
        sink.append(item)
    def close(self):
        return

log = audit.AuditLog([file_emitter, Grabber()], min_severity="notice").start()
log.emit("budget.violation", actor="ana", action="validate", outcome="failed",
         detail={"asset": "rock.obj", "problem_count": 2})
log.emit("rbac.denied", actor="bob", path="/x")
log.flush(2.0)
lines = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines()]
expect("the JSONL emitter writes one object per line", len(lines) == 2, str(len(lines)))
expect("every sink receives the same event", len(sink) == 2)
expect("the fan-out counts deliveries", log.stats()["emitted"] == 2)
log.close()

quiet = audit.AuditLog([Grabber()], min_severity="error").start()
quiet.emit("rbac.denied", actor="x")
quiet.flush(1.0)
expect("the severity floor drops quieter events", quiet.stats()["emitted"] == 0)
quiet.close()

class FakeSock:
    def __init__(self):
        self.sent = []
    def sendto(self, payload, address):
        self.sent.append((payload, address))
    def settimeout(self, value):
        return
    def connect(self, address):
        return
    def sendall(self, payload):
        self.sent.append((payload, None))
    def close(self):
        return

sock = FakeSock()
syslog = audit.SyslogEmitter("siem.internal", 514, facility=13,
                             socket_factory=lambda *a, **k: sock)
syslog.emit(event)
raw = sock.sent[0][0].decode("utf-8")
expect("syslog frames an RFC 5424 message with the right priority",
       raw.startswith("<109>1 "), raw[:20])
expect("the syslog payload is the same JSON", json.loads(raw.split(" - ", 3)[-1])["event"]
       == "rbac.denied")
expect("the emitter counts what it sent", syslog.sent == 1)
import re as _re  # noqa: E402
import socket as _socket  # noqa: E402
rfc5424 = _re.match(r"^<(\d{1,3})>1 (\S+) (\S{1,255}) (\S{1,48}) (\S{1,128}) (\S{1,32}) (-|\[.*\]) (.*)$", raw)
expect("the syslog header has every RFC 5424 field", rfc5424 is not None, raw[:120])
expect("the syslog header names the sending machine, not '-'",
       rfc5424 is not None and rfc5424.group(3) not in ("-", "") and
       rfc5424.group(3) == audit._syslog_token(_socket.gethostname(), 255), raw[:120])
expect("the syslog MSGID is the event name", rfc5424 is not None and rfc5424.group(6) == "rbac.denied")

posted = []
def fake_poster(url, body, headers, timeout):
    posted.append(json.loads(body))
    return 200
webhook = audit.WebhookEmitter("https://siem.example/ingest", batch_size=2,
                               flush_interval=0.05, poster=fake_poster)
webhook.start()
for index in range(5):
    webhook.emit(audit.AuditEvent(event="rbac.denied", actor=f"u{index}"))
webhook.flush(2.0)
webhook.close()
expect("the webhook batches events", sum(len(b["events"]) for b in posted) == 5,
       str([len(b["events"]) for b in posted]))
expect("the webhook reports what it delivered", webhook.sent == 5)

# Splunk HEC accepts only {"event": ...} envelopes, one per event, concatenated.
hec_posts = []
def hec_poster(url, body, headers, timeout):
    hec_posts.append((url, body.decode("utf-8"), headers))
    return 200
hec = audit.WebhookEmitter("https://splunk.studio.internal:8088/services/collector",
                           token="t0k", batch_size=3, flush_interval=0.05, poster=hec_poster)
hec.start()
for index in range(3):
    hec.emit(audit.AuditEvent(event="budget.violation", actor=f"u{index}"))
hec.flush(2.0)
hec.close()
hec_lines = [json.loads(line) for _, body, _ in hec_posts for line in body.splitlines()]
expect("a Splunk HEC URL gets HEC envelopes: one object per event, each with 'event'",
       len(hec_lines) == 3 and all("event" in line and "time" in line for line in hec_lines),
       str(hec_lines[:1]))
expect("the HEC envelope carries the audit event unchanged",
       all(line["event"]["event"] == "budget.violation" for line in hec_lines))
expect("the HEC time is epoch seconds", all(isinstance(line["time"], (int, float))
                                            and line["time"] > 1.7e9 for line in hec_lines))
expect("HEC gets 'Authorization: Splunk <token>'",
       all(headers.get("Authorization") == "Splunk t0k" for _, _, headers in hec_posts))
expect("a generic webhook keeps the generic body",
       not audit.WebhookEmitter("https://siem.example/ingest").splunk_hec and
       audit.WebhookEmitter("https://s:8088/services/collector/event").splunk_hec and
       not audit.WebhookEmitter("https://s:8088/services/collector/raw").splunk_hec)

def failing_poster(url, body, headers, timeout):
    return 500
bad = audit.WebhookEmitter("https://siem.example/ingest", batch_size=1,
                           flush_interval=0.02, poster=failing_poster,
                           sleep=lambda _: None)
bad.start()
bad.emit(audit.AuditEvent(event="rbac.denied"))
bad.flush(1.0)
bad.close(timeout=1.0)
expect("a failing collector is counted, not fatal", bad.failed_batches >= 1 and bad.dropped >= 1)

overflow = audit.WebhookEmitter("https://siem.example/ingest", queue_size=1)
overflow.emit(audit.AuditEvent(event="a"))
overflow.emit(audit.AuditEvent(event="b"))
expect("a full queue drops and counts instead of blocking", overflow.dropped == 1)
expect("a NullLog never needs a None check", audit.NullLog().stats()["emitted"] == 0)

# --------------------------------------------------------------------------- #
# 12. Replicas, failover and concurrency
# --------------------------------------------------------------------------- #
print("12. read replicas, failover and pool stress")

import _pgsim                                       # noqa: E402  (tests/ on sys.path)

primary_dsn = "postgresql://fake/primary"
replica_dsn = "postgresql://fake/replica"
schema_text = health.SCHEMA
for dsn, name in ((primary_dsn, "primary"), (replica_dsn, "replica")):
    _pgsim.create(WORK / f"{name}.db", schema_text)
_pgsim.install({primary_dsn: WORK / "primary.db", replica_dsn: WORK / "replica.db"})

db_config = econfig.DatabaseConfig(dsn=primary_dsn, read_dsns=(replica_dsn,),
                                   breaker_threshold=2, breaker_reset_seconds=60,
                                   health_interval_seconds=0)
primary_store = store.open_store(primary_dsn, db_config)
for index in range(3):
    primary_store.execute(
        "INSERT INTO verdicts (timestamp, received_at, filename, passed, status)"
        " VALUES (?, ?, ?, ?, ?)",
        (f"2026-10-{index + 1:02d}T00:00:00", "2026-10-01T00:00:01", f"a{index}.obj",
         1, "PASSED"))
replica_store = primary_store._replicas[0][1]
replica_store.execute(
    "INSERT INTO verdicts (timestamp, received_at, filename, passed, status)"
    " VALUES (?, ?, ?, ?, ?)",
    ("2026-10-09T00:00:00", "2026-10-09T00:00:01", "replica.obj", 1, "PASSED"))

view = primary_store.reader()
rows = view.query("SELECT COUNT(*) AS n FROM verdicts")
expect("the reader answers from a replica", rows[0]["n"] == 1, str(rows[0]["n"]))
expect("writing through the read view is refused", True)
expect_raises("a write sent to the read view is refused, not silently redirected",
              store.StoreError, lambda: view.execute("INSERT INTO meta VALUES ('a','b')"))

_pgsim.REGISTRY.dead_dsns.add(replica_dsn)
first = view.query("SELECT 1 AS one")
second = view.query("SELECT 1 AS one")
third = view.query("SELECT COUNT(*) AS n FROM verdicts")
expect("a failing replica does not fail the query", first[0]["one"] == 1)
expect("after the threshold the replica is taken out",
       primary_store._replicas[0][2].state is rz.State.OPEN)
expect("queries fall back to the primary and its three rows",
       third[0]["n"] == 3, str(third[0]["n"]))
expect("the primary breaker stayed closed", primary_store._breaker.state is rz.State.CLOSED)
_pgsim.REGISTRY.dead_dsns.discard(replica_dsn)
primary_store._replicas[0][2].reset()

errors = []
def hammer(index):
    try:
        for _ in range(15):
            primary_store.query("SELECT COUNT(*) AS n FROM verdicts")
    except Exception as exc:                       # noqa: BLE001 - collected, not swallowed
        errors.append(exc)

threads = [threading.Thread(target=hammer, args=(index,)) for index in range(8)]
started = time.time()
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(timeout=20)
expect("120 concurrent queries over the pool all succeed", not errors,
       str(errors[:1]))
expect("the stress run finishes quickly", time.time() - started < 20)
expect("the pool stayed bounded", _pgsim.REGISTRY.connections <= 12,
       str(_pgsim.REGISTRY.connections))

health_store = store.open_store(primary_dsn, econfig.DatabaseConfig(
    dsn=primary_dsn, breaker_threshold=2, health_interval_seconds=0.05))
health_store.start_health()
_pgsim.REGISTRY.dead_dsns.add(primary_dsn)
time.sleep(0.3)
expect("the health loop trips the breaker on a dead endpoint",
       health_store._breaker.state is rz.State.OPEN,
       health_store.health()["endpoints"][0]["breaker"]["state"])
_pgsim.REGISTRY.dead_dsns.discard(primary_dsn)
time.sleep(0.4)
expect("and closes it again once the endpoint answers",
       health_store._breaker.state is rz.State.CLOSED,
       health_store.health()["endpoints"][0]["breaker"]["state"])
health_store.close()
primary_store.close()
expect("the driver never saw untranslated or SQLite-only SQL",
       not _pgsim.REGISTRY.violations, str(_pgsim.REGISTRY.violations[:2]))

print()
print("FAILURES:", failures if failures else "none")
raise SystemExit(1 if failures else 0)
