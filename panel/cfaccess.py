"""Verify the Cloudflare Access JWT on every request, so the origin does not rely on the tunnel alone."""
import jwt
from flask import abort, current_app, g, request

HEADER = "Cf-Access-Jwt-Assertion"


class AccessVerifier:
    def __init__(self, team, aud, key_resolver=None):
        self.issuer = f"https://{team}.cloudflareaccess.com"
        self.aud = aud
        # key_resolver(token) -> public key. The default fetches and caches the team's signing keys.
        self.key_resolver = key_resolver or self._jwks_resolver(f"{self.issuer}/cdn-cgi/access/certs")

    @staticmethod
    def _jwks_resolver(url):
        client = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=10)
        return lambda token: client.get_signing_key_from_jwt(token).key

    def verify(self, token):
        """Return the claims or raise jwt.PyJWTError. RS256 only, so a token cannot pick a weaker algorithm."""
        return jwt.decode(token, self.key_resolver(token), algorithms=["RS256"], audience=self.aud,
                          issuer=self.issuer, options={"require": ["exp", "iat", "iss", "aud"]})


def init_app(app):
    team, aud = app.config["CF_ACCESS_TEAM"], app.config["CF_ACCESS_AUD"]
    if bool(team) != bool(aud):
        raise RuntimeError("Set both PANEL_CF_ACCESS_TEAM and PANEL_CF_ACCESS_AUD, or neither.")
    if team:
        app.extensions["cf_access"] = AccessVerifier(team, aud)
        app.before_request(gate)


def gate():
    """Fail closed: no valid Access identity, no response. Only the local health check is exempt."""
    if request.endpoint == "main.healthz":
        return
    token = request.headers.get(HEADER, "")
    try:
        claims = current_app.extensions["cf_access"].verify(token) if token else None
    except Exception:  # bad signature, wrong audience, expired, or the key fetch failed
        claims = None
    if not claims:
        abort(403, "Cloudflare Access identity required.")
    g.access_email = str(claims.get("email", ""))[:120]
