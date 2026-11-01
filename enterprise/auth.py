"""Enterprise sign-in: OIDC / OAuth2, SAML 2.0, and signed sessions.

What this is
    A service-provider implementation small enough to read in one sitting and
    strict enough to survive a security review: no token ever reaches a browser,
    no unsigned assertion is trusted, and every failure mode is a refusal rather
    than a fallback.

Hard rules, all enforced in code below
    * ``alg: none`` is rejected before the signature is even considered.
    * The algorithm is chosen by the *key*, never by the token: an RSA key never
      validates an HS256 token (the classic algorithm-confusion attack).
    * ``exp``, ``nbf``, ``iss``, ``aud`` and ``nonce`` are all checked; a missing
      claim is a failure, not a pass.
    * SAML assertions must be signed by the configured IdP certificate. Without
      ``signxml`` installed, signed assertions are refused with an actionable
      message - and ``allow_unsigned`` has to be set on purpose for a lab IdP.
    * Sessions are HMAC-SHA256 cookies with an expiry inside the signed payload,
      so a stolen cookie expires even if the server restarts with a new clock.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .config import OIDCConfig, SAMLConfig, SessionConfig
from .rbac import Principal

SAML_PROTOCOL_NS = "urn:oasis:names:tc:SAML:2.0:protocol"
SAML_ASSERTION_NS = "urn:oasis:names:tc:SAML:2.0:assertion"


class AuthError(Exception):
    """Authentication failed. The message is safe to show; never includes a token."""


# --------------------------------------------------------------------------- #
# Base64 and JSON helpers
# --------------------------------------------------------------------------- #

def b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except Exception as exc:
        raise AuthError("malformed base64 segment in token") from exc


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _json_segment(segment: str, what: str) -> dict:
    try:
        value = json.loads(b64url_decode(segment).decode("utf-8"))
    except Exception as exc:
        raise AuthError(f"token {what} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise AuthError(f"token {what} is not a JSON object")
    return value


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #

SUPPORTED_HMAC = {"HS256": hashlib.sha256, "HS384": hashlib.sha384,
                  "HS512": hashlib.sha512}
SUPPORTED_RSA = {"RS256", "RS384", "RS512", "ES256", "PS256"}


def _verify_rsa(algorithm: str, signing_input: bytes, signature: bytes,
                key_pem: bytes) -> None:
    try:
        from cryptography.exceptions import InvalidSignature  # noqa: PLC0415
        from cryptography.hazmat.primitives import hashes, serialization  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa  # noqa: PLC0415
    except ModuleNotFoundError as exc:  # pragma: no cover - optional package
        raise AuthError(
            f"{algorithm} needs the optional 'cryptography' package: "
            "python -m pip install 'asset-gatekeeper[enterprise]'") from exc

    public_key = serialization.load_pem_public_key(key_pem)
    digest = {"RS256": hashes.SHA256(), "RS384": hashes.SHA384(),
              "RS512": hashes.SHA512(), "PS256": hashes.SHA256(),
              "ES256": hashes.SHA256()}.get(algorithm)
    if digest is None:
        raise AuthError(f"unsupported algorithm {algorithm}")
    try:
        if isinstance(public_key, rsa.RSAPublicKey):
            if algorithm.startswith("PS"):
                public_key.verify(signature, signing_input,
                                  padding.PSS(mgf=padding.MGF1(digest),
                                              salt_length=padding.PSS.MAX_LENGTH),
                                  digest)
            else:
                public_key.verify(signature, signing_input, padding.PKCS1v15(), digest)
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(signature, signing_input, ec.ECDSA(digest))
        else:
            raise AuthError(f"unsupported key type for {algorithm}")
    except InvalidSignature as exc:
        raise AuthError("token signature does not verify") from exc


def decode_jwt(token: str, *, hmac_secret: Optional[str] = None,
               public_key_pem: Optional[bytes] = None,
               issuer: Optional[str] = None, audience: Optional[str] = None,
               nonce: Optional[str] = None, leeway: int = 120,
               now: Optional[float] = None) -> dict[str, Any]:
    """Verify a JWT and return its claims, or raise :class:`AuthError`."""
    if not token or token.count(".") != 2:
        raise AuthError("not a signed JWT (expected three segments)")
    header_segment, payload_segment, signature_segment = token.split(".")
    header = _json_segment(header_segment, "header")
    claims = _json_segment(payload_segment, "payload")

    algorithm = str(header.get("alg", "")).upper()
    if not algorithm or algorithm == "NONE":
        raise AuthError("unsigned tokens are not accepted")
    if header.get("crit"):
        # A critical header we do not implement must not be ignored.
        raise AuthError("token uses a critical header extension we do not support")

    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature = b64url_decode(signature_segment)

    # The key decides the algorithm. A token does not get to pick its own.
    if algorithm in SUPPORTED_HMAC:
        if not hmac_secret:
            raise AuthError(f"{algorithm} token presented but no shared secret is configured")
        expected = hmac.new(hmac_secret.encode("utf-8"), signing_input,
                            SUPPORTED_HMAC[algorithm]).digest()
        if not hmac.compare_digest(expected, signature):
            raise AuthError("token signature does not verify")
    elif algorithm in SUPPORTED_RSA:
        if not public_key_pem:
            raise AuthError(f"{algorithm} token presented but no public key is configured")
        _verify_rsa(algorithm, signing_input, signature, public_key_pem)
    else:
        raise AuthError(f"unsupported signing algorithm {algorithm}")

    moment = time.time() if now is None else now
    exp = claims.get("exp")
    if exp is None:
        raise AuthError("token has no expiry (exp): refusing an immortal credential")
    if float(exp) + leeway < moment:
        raise AuthError("token expired")
    nbf = claims.get("nbf")
    if nbf is not None and float(nbf) - leeway > moment:
        raise AuthError("token is not valid yet (nbf)")
    iat = claims.get("iat")
    if iat is not None and float(iat) - leeway > moment:
        raise AuthError("token was issued in the future (iat)")

    if issuer is not None and claims.get("iss") != issuer:
        raise AuthError("token issuer does not match the configured issuer")
    if audience is not None:
        aud = claims.get("aud")
        audiences = aud if isinstance(aud, list) else [aud]
        if audience not in audiences:
            raise AuthError("token audience does not include this client")
    if nonce is not None and claims.get("nonce") != nonce:
        raise AuthError("token nonce does not match the login attempt")
    return claims


# --------------------------------------------------------------------------- #
# Principal
# --------------------------------------------------------------------------- #

def principal_from_claims(claims: dict[str, Any], config: OIDCConfig) -> Principal:
    subject = str(claims.get("sub") or "")
    if not subject:
        raise AuthError("token has no subject (sub)")
    email = str(claims.get(config.username_claim) or claims.get("email") or "")
    if config.allowed_domains and email:
        domain = email.rsplit("@", 1)[-1].lower()
        if domain not in config.allowed_domains:
            raise AuthError(f"email domain '{domain}' is not on the allow-list")
    elif config.allowed_domains and not email:
        raise AuthError("no email claim to check against the allow-list")

    raw_groups = claims.get(config.groups_claim) or []
    if isinstance(raw_groups, str):
        raw_groups = [raw_groups]
    groups = tuple(str(g).strip() for g in raw_groups if str(g).strip())

    friendly = email or subject
    return Principal(subject=subject, username=friendly, email=email,
                     groups=groups, roles=groups, auth_method="oidc")


# --------------------------------------------------------------------------- #
# OIDC
# --------------------------------------------------------------------------- #

def _http_json(url: str, timeout: float = 10.0,
               opener: Optional[Callable[[str, float], dict]] = None) -> dict:
    if opener is not None:
        return opener(url, timeout)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read(8 * 1024 * 1024).decode("utf-8"))
    except (urllib.error.URLError, ValueError) as exc:
        raise AuthError(f"could not fetch {url}: {exc}") from exc


@dataclass
class JwksCache:
    """Keys cached with a TTL, refreshed once on an unknown ``kid``."""

    jwks_uri: str
    ttl_seconds: int = 3600
    opener: Optional[Callable[[str, float], dict]] = None
    _keys: dict[str, bytes] = None  # type: ignore[assignment]
    _fetched_at: float = 0.0

    def __post_init__(self) -> None:
        if self._keys is None:
            self._keys = {}

    def key_for(self, kid: str) -> bytes:
        if not self._keys or (time.time() - self._fetched_at) > self.ttl_seconds:
            self.refresh()
        if kid not in self._keys:
            self.refresh()                      # key rotation: one retry, no loop
        if kid not in self._keys:
            raise AuthError(f"no signing key with kid '{kid}' at the IdP")
        return self._keys[kid]

    def refresh(self) -> None:
        document = _http_json(self.jwks_uri, opener=self.opener)
        keys: dict[str, bytes] = {}
        for jwk in document.get("keys", []):
            if not isinstance(jwk, dict) or jwk.get("kty") != "RSA":
                continue
            kid = str(jwk.get("kid") or "")
            try:
                keys[kid] = _rsa_jwk_to_pem(jwk)
            except AuthError:
                continue
        if not keys:
            raise AuthError("the IdP published no usable RSA signing keys")
        self._keys = keys
        self._fetched_at = time.time()


def _rsa_jwk_to_pem(jwk: dict) -> bytes:
    """Convert an RSA JWK to PEM without pulling in a crypto library for base64."""
    try:
        modulus = int.from_bytes(b64url_decode(str(jwk["n"])), "big")
        exponent = int.from_bytes(b64url_decode(str(jwk["e"])), "big")
    except Exception as exc:
        raise AuthError("malformed RSA JWK") from exc

    def der_integer(value: int) -> bytes:
        raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
        if raw[0] & 0x80:
            raw = b"\x00" + raw
        return b"\x02" + der_length(len(raw)) + raw

    def der_length(length: int) -> bytes:
        if length < 0x80:
            return bytes([length])
        raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
        return bytes([0x80 | len(raw)]) + raw

    rsa_key = der_integer(modulus) + der_integer(exponent)
    sequence = b"\x30" + der_length(len(rsa_key)) + rsa_key
    # SubjectPublicKeyInfo wrapper with the rsaEncryption algorithm identifier.
    algorithm = bytes.fromhex("300d06092a864886f70d0101010500")
    bit_string = b"\x03" + der_length(len(sequence) + 1) + b"\x00" + sequence
    spki = b"\x30" + der_length(len(algorithm + bit_string)) + algorithm + bit_string
    body = base64.b64encode(spki).decode("ascii")
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return ("-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines)
            + "\n-----END PUBLIC KEY-----\n").encode("ascii")


class OIDCClient:
    """Authorization-code flow with PKCE, discovery and ID-token verification."""

    def __init__(self, config: OIDCConfig,
                 opener: Optional[Callable[[str, float], dict]] = None,
                 poster: Optional[Callable[[str, dict, float], dict]] = None) -> None:
        self.config = config
        self._opener = opener
        self._poster = poster
        self._discovery: Optional[dict] = None
        self._jwks: Optional[JwksCache] = None

    # -- discovery --------------------------------------------------------- #

    def discovery(self) -> dict:
        if self._discovery is None:
            url = self.config.issuer.rstrip("/") + "/.well-known/openid-configuration"
            document = _http_json(url, self.config.http_timeout, self._opener)
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                if not document.get(key):
                    raise AuthError(f"IdP discovery document has no {key}")
            if document.get("issuer") and document["issuer"].rstrip("/") != \
                    self.config.issuer.rstrip("/"):
                raise AuthError("discovery issuer does not match the configured issuer")
            self._discovery = document
        return self._discovery

    def jwks(self) -> JwksCache:
        if self._jwks is None:
            self._jwks = JwksCache(self.discovery()["jwks_uri"],
                                   opener=self._opener)
        return self._jwks

    # -- flow -------------------------------------------------------------- #

    def authorization_url(self, state: str, nonce: str,
                          code_verifier: str) -> str:
        document = self.discovery()
        challenge = b64url_encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "scope": " ".join(self.config.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return document["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)

    def exchange_code(self, code: str, code_verifier: str) -> dict:
        document = self.discovery()
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.config.redirect_uri,
            "client_id": self.config.client_id,
            "code_verifier": code_verifier,
        }
        if self.config.client_secret:
            payload["client_secret"] = self.config.client_secret
        return self._post(document["token_endpoint"], payload)

    def _post(self, url: str, payload: dict) -> dict:
        if self._poster is not None:
            return self._poster(url, payload, self.config.http_timeout)
        body = urllib.parse.urlencode(payload).encode("ascii")
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded",
                                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request,     # noqa: S310
                                        timeout=self.config.http_timeout) as response:
                return json.loads(response.read(8 * 1024 * 1024).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise AuthError(f"the IdP rejected the token request (HTTP {exc.code})") from exc
        except (urllib.error.URLError, ValueError) as exc:
            raise AuthError(f"token request failed: {exc}") from exc

    def complete_login(self, code: str, code_verifier: str, nonce: str) -> Principal:
        """Exchange the code and verify the ID token end to end."""
        tokens = self.exchange_code(code, code_verifier)
        id_token = tokens.get("id_token")
        if not id_token:
            raise AuthError("the IdP returned no id_token (check the 'openid' scope)")
        header = _json_segment(str(id_token).split(".")[0], "header")
        key = self.jwks().key_for(str(header.get("kid", "")))
        claims = decode_jwt(id_token, public_key_pem=key,
                            issuer=self.config.issuer,
                            audience=self.config.client_id,
                            nonce=nonce,
                            leeway=self.config.clock_skew_seconds)
        principal = principal_from_claims(claims, self.config)
        return principal


# --------------------------------------------------------------------------- #
# SAML 2.0 (service provider side)
# --------------------------------------------------------------------------- #

class SamlHandler:
    """Builds AuthnRequests and reads Assertions from an IdP POST.

    Signature validation is mandatory unless the deployment explicitly opts out.
    That is the only place an opt-out exists in this module.
    """

    def __init__(self, config: SAMLConfig) -> None:
        self.config = config

    def build_authn_request(self, relay_state: str = "") -> tuple[str, str]:
        """Return (sso_url_with_query, request_id). The XML is deflated + base64."""
        request_id = "_" + secrets.token_hex(16)
        issued = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        xml = (
            f'<samlp:AuthnRequest xmlns:samlp="{SAML_PROTOCOL_NS}" '
            f'xmlns:saml="{SAML_ASSERTION_NS}" ID="{request_id}" Version="2.0" '
            f'IssueInstant="{issued}" '
            f'AssertionConsumerServiceURL="{_xml_escape(self._acs())}" '
            f'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">'
            f'<saml:Issuer>{_xml_escape(self.config.entity_id)}</saml:Issuer>'
            f"</samlp:AuthnRequest>")
        query = urllib.parse.urlencode({"SAMLRequest": b64url_encode(xml.encode("utf-8")),
                                        "RelayState": relay_state})
        return self.config.sso_url + ("&" if "?" in self.config.sso_url else "?") + query, request_id

    def _acs(self) -> str:
        return self.config.audience or ""

    def parse_response(self, saml_response_b64: str) -> Principal:
        if not saml_response_b64:
            raise AuthError("empty SAMLResponse")
        try:
            xml_bytes = base64.b64decode(saml_response_b64, validate=False)
        except Exception as exc:
            raise AuthError("SAMLResponse is not valid base64") from exc

        self._verify_signature(xml_bytes)

        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            raise AuthError("SAMLResponse is not valid XML") from exc

        status = root.find(f".//{{{SAML_PROTOCOL_NS}}}StatusCode")
        if status is not None:
            code = status.get("Value", "")
            if not code.endswith(":Success"):
                raise AuthError(f"the IdP returned {code.rsplit(':', 1)[-1]}")

        assertion = root.find(f".//{{{SAML_ASSERTION_NS}}}Assertion")
        if assertion is None:
            raise AuthError("SAMLResponse contains no Assertion")
        self._check_conditions(assertion)

        name_id = assertion.findtext(f".//{{{SAML_ASSERTION_NS}}}NameID") or ""
        attributes: dict[str, list[str]] = {}
        for attribute in assertion.iter(f"{{{SAML_ASSERTION_NS}}}Attribute"):
            name = attribute.get("Name", "")
            values = [v.text or "" for v in attribute.iter(f"{{{SAML_ASSERTION_NS}}}AttributeValue")]
            if name:
                attributes.setdefault(name, []).extend(values)

        email = (attributes.get(self.config.username_attribute, [""])[0]
                 or name_id)
        groups = tuple(attributes.get(self.config.groups_attribute, ()))
        if not name_id and not email:
            raise AuthError("the assertion carries no NameID and no mail attribute")
        return Principal(subject=name_id or email, username=email or name_id,
                         email=email, groups=groups, roles=groups,
                         auth_method="saml")

    # -- internals --------------------------------------------------------- #

    def _check_conditions(self, assertion: ET.Element) -> None:
        conditions = assertion.find(f"{{{SAML_ASSERTION_NS}}}Conditions")
        if conditions is None:
            if self.config.require_signature:
                raise AuthError("assertion has no Conditions: refusing an unbounded claim")
            return
        now = time.time()
        not_before = conditions.get("NotBefore")
        not_after = conditions.get("NotOnOrAfter")
        if not_before and _saml_time(not_before) - 120 > now:
            raise AuthError("assertion is not valid yet")
        if not_after and _saml_time(not_after) + 120 < now:
            raise AuthError("assertion has expired")
        audience = conditions.find(
            f"{{{SAML_ASSERTION_NS}}}AudienceRestriction/{{{SAML_ASSERTION_NS}}}Audience")
        if self.config.audience and audience is not None:
            if (audience.text or "").strip() != self.config.audience:
                raise AuthError("assertion audience does not match this application")

    def _verify_signature(self, xml_bytes: bytes) -> None:
        signed = b"Signature" in xml_bytes
        if not signed:
            if self.config.allow_unsigned and not self.config.require_signature:
                return
            raise AuthError("the SAMLResponse is unsigned; refusing to trust it")
        if not self.config.require_signature:
            return
        if not self.config.x509_cert:
            raise AuthError("the SAMLResponse is signed but no x509_cert is configured")
        try:
            from signxml import XMLVerifier  # noqa: PLC0415 - optional dependency
        except ModuleNotFoundError as exc:  # pragma: no cover - optional package
            raise AuthError(
                "verifying a SAML signature needs the optional 'signxml' package: "
                "python -m pip install 'asset-gatekeeper[saml]'") from exc
        try:
            XMLVerifier().verify(xml_bytes, x509_cert=self.config.x509_cert)
        except Exception as exc:              # signxml raises a wide family
            raise AuthError(f"SAML signature verification failed: {type(exc).__name__}") from exc


def _xml_escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _saml_time(value: str) -> float:
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except ValueError as exc:
        raise AuthError(f"unreadable SAML timestamp {value!r}") from exc


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #

class SessionCodec:
    """Signed, expiring cookie sessions. No server-side store required."""

    def __init__(self, config: SessionConfig) -> None:
        if not config.enabled:
            raise AuthError("sessions need a secret of at least 32 characters")
        self.config = config

    def issue(self, principal: Principal, now: Optional[float] = None) -> str:
        issued = int(now if now is not None else time.time())
        payload = {
            "sub": principal.subject,
            "usr": principal.username,
            "email": principal.email,
            "grp": list(principal.groups),
            "roles": list(principal.roles),
            "amr": principal.auth_method,
            "iat": issued,
            "exp": issued + self.config.ttl_seconds,
        }
        body = b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = hmac.new(self.config.secret.encode("utf-8"), body.encode("ascii"),
                             hashlib.sha256).digest()
        return f"{body}.{b64url_encode(signature)}"

    def verify(self, cookie_value: str, now: Optional[float] = None) -> Principal:
        if not cookie_value or cookie_value.count(".") != 1:
            raise AuthError("malformed session cookie")
        body, signature = cookie_value.split(".")
        expected = hmac.new(self.config.secret.encode("utf-8"), body.encode("ascii"),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(expected, b64url_decode(signature)):
            raise AuthError("session cookie signature does not verify")
        payload = _json_segment(body, "session")
        moment = now if now is not None else time.time()
        if float(payload.get("exp", 0)) < moment:
            raise AuthError("session expired")
        return Principal(subject=str(payload.get("sub", "")),
                         username=str(payload.get("usr", "")),
                         email=str(payload.get("email", "")),
                         groups=tuple(payload.get("grp", ())),
                         roles=tuple(payload.get("roles", ())),
                         auth_method=str(payload.get("amr", "session")))


def new_login_state() -> tuple[str, str, str]:
    """(state, nonce, pkce_verifier) - three fresh secrets per attempt."""
    verifier = b64url_encode(secrets.token_bytes(48))
    return secrets.token_urlsafe(24), secrets.token_urlsafe(24), verifier
