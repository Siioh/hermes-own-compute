"""A small AWS client for EC2 hosts: Signature Version 4, the EC2/STS Query API and SSM.

Standard library only (``urllib``, ``hmac``, ``hashlib``, ``xml.etree``): no boto3, no
``aws`` command. Every request goes through one ``http`` callable, so tests replace the
network with an in-process fake (see ``tests/test_ec2_*``).

Credentials are never stored here: an :class:`AwsClient` gets a ``credentials`` callable that
reads them from the Hermes secret store by name at the moment of each call. Every error text
that leaves this module is redacted against the key values, and AWS error codes are turned into
plain sentences that say what to do (:func:`plain_error`).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Callable

__all__ = [
    "Credentials", "AwsError", "AwsClient", "sign_request", "signing_key", "plain_error",
    "flatten_params", "parse_xml", "REGION_RE", "EC2_VERSION", "STS_VERSION", "CHECKIP_URL",
    "urllib_http", "lookup_public_ip",
]

log = logging.getLogger(__name__)

EC2_VERSION = "2016-11-15"
STS_VERSION = "2011-06-15"
#: The one outside lookup an EC2 host may make besides AWS itself: this computer's public
#: address, for the SSH rule of the security group. The user can type the address instead.
CHECKIP_URL = "https://checkip.amazonaws.com"
REGION_RE = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-\d{1,2}$")
_TIMEOUT_S = 30.0
_RETRY_CODES = frozenset({"Throttling", "ThrottlingException", "RequestLimitExceeded",
                          "ServiceUnavailable", "InternalError", "Unavailable"})
_MAX_BODY = 8 * 1024 * 1024


@dataclass(frozen=True)
class Credentials:
    """An AWS key as read from the secret store. The repr never shows the values."""

    access_key: str
    secret_key: str
    token: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "Credentials([redacted])"

    __str__ = __repr__

    def values(self) -> list[str]:
        return [v for v in (self.access_key, self.secret_key, self.token) if v]


class AwsError(Exception):
    """An AWS call failed. ``code`` is the AWS error code (or a local one such as
    ``no_credentials`` / ``network``); ``plain`` is the sentence shown to the user."""

    def __init__(self, code: str, plain: str, *, action: str = "", status: int | None = None,
                 detail: str = ""):
        super().__init__(plain)
        self.code = code
        self.plain = plain
        self.action = action
        self.status = status
        self.detail = detail

    def __str__(self) -> str:
        return self.plain


# --------------------------------------------------------------------------- SigV4


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    """The SigV4 signing key for ``date`` (YYYYMMDD), ``region`` and ``service``."""
    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")


def _uri_encode(text: str, *, keep_slash: bool = False) -> str:
    return urllib.parse.quote(text, safe="-_.~/" if keep_slash else "-_.~")


def _canonical_query(query: str) -> str:
    if not query:
        return ""
    pairs = []
    for part in query.split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        pairs.append((_uri_encode(urllib.parse.unquote_plus(key)),
                      _uri_encode(urllib.parse.unquote_plus(value))))
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


def sign_request(method: str, url: str, headers: dict[str, str], body: bytes, *,
                 credentials: Credentials, region: str, service: str,
                 amz_date: str) -> dict[str, str]:
    """Return ``headers`` plus ``X-Amz-Date``, the session token and ``Authorization``.

    ``amz_date`` is ``YYYYMMDDTHHMMSSZ``. ``headers`` must include ``Host``; every header given
    is signed. Implements the published Signature Version 4 algorithm.
    """
    parts = urllib.parse.urlsplit(url)
    out = dict(headers)
    out["X-Amz-Date"] = amz_date
    if credentials.token:
        out["X-Amz-Security-Token"] = credentials.token
    canon: dict[str, str] = {}
    for name, value in out.items():
        canon[name.strip().lower()] = " ".join(str(value).strip().split())
    signed = ";".join(sorted(canon))
    canonical_headers = "".join(f"{k}:{canon[k]}\n" for k in sorted(canon))
    path = parts.path or "/"
    canonical_request = "\n".join([
        method.upper(), _uri_encode(urllib.parse.unquote(path), keep_slash=True),
        _canonical_query(parts.query), canonical_headers, signed, _sha256_hex(body or b"")])
    date = amz_date[:8]
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                                _sha256_hex(canonical_request.encode("utf-8"))])
    signature = hmac.new(signing_key(credentials.secret_key, date, region, service),
                         string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={credentials.access_key}/{scope}, "
                            f"SignedHeaders={signed}, Signature={signature}")
    return out


# --------------------------------------------------------------------------- params + XML


def flatten_params(value: Any, prefix: str = "") -> dict[str, str]:
    """EC2 Query parameters from nested data: lists become ``Name.1``, ``Name.2`` and dicts
    become ``Name.Key``. Booleans are ``true`` / ``false``."""
    out: dict[str, str] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            out.update(flatten_params(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value, 1):
            out.update(flatten_params(item, f"{prefix}.{i}"))
    elif isinstance(value, bool):
        out[prefix] = "true" if value else "false"
    elif value is not None:
        out[prefix] = str(value)
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _element(el: ET.Element) -> Any:
    children = list(el)
    if not children:
        return (el.text or "").strip()
    if all(_local(c.tag) in ("item", "member") for c in children):
        return [_element(c) for c in children]
    out: dict[str, Any] = {}
    repeated: set[str] = set()
    for c in children:
        key = _local(c.tag)
        value = _element(c)
        if key in out:
            if key not in repeated:
                out[key] = [out[key]]
                repeated.add(key)
            out[key].append(value)
        else:
            out[key] = value
    return out


def parse_xml(body: bytes) -> dict:
    """An AWS XML response as ``{root tag: content}``; raises ``ValueError`` on bad XML."""
    if b"<!DOCTYPE" in body[:2048] or b"<!ENTITY" in body:
        raise ValueError("unexpected document type in the AWS response")
    root = ET.fromstring(body)
    return {_local(root.tag): _element(root)}


def _xml_error(body: bytes) -> tuple[str, str]:
    try:
        root = ET.fromstring(body) if b"<!ENTITY" not in body else None
    except ET.ParseError:
        root = None
    if root is None:
        return "", ""
    code = next((el.text or "" for el in root.iter() if _local(el.tag) == "Code"), "")
    msg = next((el.text or "" for el in root.iter() if _local(el.tag) == "Message"), "")
    return code.strip(), msg.strip()


# --------------------------------------------------------------------------- plain errors

_KEY_HINT = ("Check that AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in the Hermes secrets belong "
             "to the same, active key.")


def plain_error(code: str, message: str, *, action: str = "", region: str = "",
                instance_type: str = "") -> str:
    """One sentence for an AWS error code, saying what happened and what to do."""
    where = f" in {region}" if region else ""
    kind = instance_type or "this instance type"
    if code in ("AuthFailure", "InvalidClientTokenId", "SignatureDoesNotMatch",
                "UnrecognizedClientException", "IncompleteSignature", "InvalidAccessKeyId"):
        return f"AWS did not accept the access key. {_KEY_HINT}"
    if code in ("ExpiredToken", "ExpiredTokenException", "TokenRefreshRequired"):
        return ("The AWS session token has expired. Store a fresh AWS_SESSION_TOKEN with its key "
                "pair, or remove AWS_SESSION_TOKEN when using a long-term key.")
    if code == "RequestExpired":
        return ("AWS rejected the request time. Check that this computer's clock and time zone "
                "are correct, then try again.")
    if code in ("UnauthorizedOperation", "AccessDenied", "AccessDeniedException",
                "UnauthorizedAccess"):
        what = f"{action} " if action else "this action "
        return (f"The AWS key is not allowed to call {what.strip()}{where}. Add the permission to "
                "the key's IAM policy (docs/ec2-iam-policy.json in the Own Compute plugin folder is a policy that allows it), then try again.")
    if code in ("OptInRequired", "SubscriptionRequired"):
        return (f"Region {region or '(unknown)'} is not enabled for this AWS account. Enable it in "
                "the AWS console, or choose another region.")
    if code in ("InstanceLimitExceeded", "VcpuLimitExceeded", "MaxSpotInstanceCountExceeded",
                "InsufficientFreeAddressesInSubnet", "ResourceLimitExceeded"):
        return (f"The AWS account's quota for running {kind} instances{where} is used up. Stop or "
                "terminate another instance, or ask for a higher quota in the AWS Service Quotas "
                "console.")
    if code in ("InsufficientInstanceCapacity", "InsufficientCapacity",
                "InsufficientHostCapacity", "InsufficientReservedInstanceCapacity"):
        return (f"AWS has no free {kind} capacity{where} right now. Try again in a few minutes, "
                "or pick another instance type or subnet.")
    if code.startswith("InvalidAMIID"):
        return (f"The machine image (AMI) was not found{where}. Pick the automatic image or an "
                "AMI id from this region.")
    if code in ("Unsupported", "InvalidParameterCombination") and "instance type" in \
            message.lower():
        return f"{kind} is not offered{where} or not with this image. Pick another instance type."
    if code == "InvalidInstanceType" or (code == "InvalidParameterValue"
                                         and "instancetype" in message.lower().replace(" ", "")):
        return f"{kind} is not a valid instance type{where}."
    if code.startswith("InvalidKeyPair"):
        return ("The SSH key pair of this host is missing in AWS. Run Test on the host to create "
                "it again.")
    if code.startswith("InvalidGroup") or code.startswith("InvalidSecurityGroup"):
        return ("The security group of this host was not found. Check the group id, or leave it "
                "empty so one is created.")
    if code.startswith("InvalidSubnet"):
        return "The subnet was not found in this region. Check the subnet id or leave it empty."
    if code == "VPCIdNotSpecified" or code == "MissingInput" and "vpc" in message.lower():
        return ("This region has no default network (VPC). Give a subnet id and a security group "
                "id on the host, or create a default VPC in the AWS console.")
    if code.startswith("InvalidInstanceID"):
        return "The instance no longer exists in AWS."
    if code == "IncorrectInstanceState":
        return "The instance is in a state that does not allow this right now. Try again shortly."
    if code in _RETRY_CODES:
        return "AWS is busy or rate-limiting requests. Try again in a minute."
    text = message.strip().rstrip(".")
    return f"AWS refused {action or 'the request'}: {code or 'error'}" + (f": {text}." if text
                                                                         else ".")


# --------------------------------------------------------------------------- http


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: a signed request (its Authorization and session token headers)
    is never sent to another address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_http(method: str, url: str, headers: dict[str, str], body: bytes | None,
                timeout: float) -> tuple[int, bytes]:
    """The real network call: ``(status, body)``. HTTPS only; redirects are not followed."""
    if not url.startswith("https://"):
        raise ValueError("AWS endpoints are only called over HTTPS")
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 - https only
            return resp.status, resp.read(_MAX_BODY)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(_MAX_BODY) if exc.fp is not None else b""


HttpFn = Callable[[str, str, dict, "bytes | None", float], "tuple[int, bytes]"]


_NOT_PUBLIC = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"))


def _usable_public_address(addr: "ipaddress.IPv4Address") -> bool:
    """One routable IPv4 address: not loopback, link-local, multicast, reserved, unspecified or
    in a private range (documentation ranges are accepted)."""
    if (addr.is_unspecified or addr.is_loopback or addr.is_link_local or addr.is_multicast
            or addr.is_reserved):
        return False
    return not any(addr in net for net in _NOT_PUBLIC)


def lookup_public_ip(http: HttpFn | None = None, timeout: float = 10.0) -> str:
    """This computer's public IPv4 address, from :data:`CHECKIP_URL`. Raises ``AwsError``."""
    fn = http or urllib_http
    try:
        status, body = fn("GET", CHECKIP_URL, {"User-Agent": "cloud-compute"}, None, timeout)
    except Exception as exc:  # noqa: BLE001
        raise AwsError("network", "This computer's public address could not be looked up at "
                       f"{CHECKIP_URL} ({type(exc).__name__}). Type the address or range that "
                       "may use SSH instead.") from None
    text = (body or b"").decode("ascii", "replace").strip()
    try:
        addr = ipaddress.IPv4Address(text)
    except ValueError:
        addr = None
    if status != 200 or addr is None or not _usable_public_address(addr):
        raise AwsError("network", f"{CHECKIP_URL} did not return a public IPv4 address. Type "
                       "the address or range that may use SSH instead.")
    return str(addr)


# --------------------------------------------------------------------------- client


class AwsClient:
    """Signed calls to EC2, STS and SSM in one region.

    ``credentials`` is called for every request and returns :class:`Credentials` or None
    (no key stored). ``http`` replaces the network (tests). ``clock`` gives the UTC time.
    """

    def __init__(self, region: str, credentials: Callable[[], Credentials | None], *,
                 http: HttpFn | None = None, clock: Callable[[], _dt.datetime] | None = None,
                 sleep: Callable[[float], None] | None = None, retries: int = 2):
        if not isinstance(region, str) or not REGION_RE.fullmatch(region):
            raise AwsError("invalid_region", f"{region!r} is not an AWS region name (for example "
                           "us-east-1).")
        self.region = region
        self._credentials = credentials
        self._http = http or urllib_http
        self._clock = clock or (lambda: _dt.datetime.now(_dt.timezone.utc))
        self._sleep = sleep or time.sleep
        self._retries = retries

    # -- plumbing ------------------------------------------------------------

    def _creds(self) -> Credentials:
        creds = self._credentials()
        if creds is None or not creds.access_key or not creds.secret_key:
            raise AwsError("no_credentials",
                           "No AWS key is stored. Add AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY "
                           "to the Hermes secrets of this profile (never in chat), then test "
                           "again.")
        return creds

    def _redact(self, text: str, creds: Credentials | None) -> str:
        out = text or ""
        for value in sorted((creds.values() if creds else []), key=len, reverse=True):
            out = out.replace(value, "[redacted]")
        from .redact import redact_text
        return redact_text(out)

    def _send(self, service: str, method: str, url: str, headers: dict[str, str], body: bytes,
              action: str, *, retry: bool = True) -> tuple[int, bytes, Credentials]:
        creds = self._creds()
        attempt = 0
        while True:
            now = self._clock().astimezone(_dt.timezone.utc)
            amz_date = now.strftime("%Y%m%dT%H%M%SZ")
            signed = sign_request(method, url, headers, body, credentials=creds,
                                  region=self.region, service=service, amz_date=amz_date)
            try:
                status, data = self._http(method, url, signed, body, _TIMEOUT_S)
            except AwsError:
                raise
            except Exception as exc:  # noqa: BLE001 - DNS, TLS, timeout, refused
                detail = self._redact(f"{type(exc).__name__}: {exc}", creds)[:200]
                raise AwsError("network", f"AWS ({service}.{self.region}.amazonaws.com) could not "
                               f"be reached: {detail}. Check the region name and the network.",
                               action=action) from None
            if status >= 500 or status == 429 or (status == 400 and _xml_or_json_code(data)
                                                  in _RETRY_CODES):
                if retry and attempt < self._retries:
                    attempt += 1
                    self._sleep(min(8.0, 1.5 ** attempt))
                    continue
            return status, data, creds

    def _raise(self, status: int, data: bytes, creds: Credentials, action: str,
               extra: dict | None = None) -> None:
        code, message = _xml_error(data)
        if not code:
            try:
                parsed = json.loads(data.decode("utf-8")) if data else {}
                code = str(parsed.get("__type", "")).rsplit("#", 1)[-1]
                message = str(parsed.get("message") or parsed.get("Message") or "")
            except (ValueError, AttributeError):
                code, message = "", ""
        if not code:
            code = f"HTTP{status}"
        message = self._redact(message, creds)
        extra = extra or {}
        plain = plain_error(code, message, action=action, region=self.region,
                            instance_type=str(extra.get("InstanceType") or ""))
        raise AwsError(code, self._redact(plain, creds), action=action, status=status,
                       detail=message[:300])

    # -- public calls --------------------------------------------------------

    def query(self, service: str, action: str, params: dict | None = None, *,
              version: str | None = None) -> dict:
        """One Query API call (EC2 or STS). Returns the parsed response body (the content of
        the ``<ActionResponse>`` element). ``DryRunOperation`` comes back as
        ``{"DryRunOperation": True}`` instead of an error."""
        version = version or (EC2_VERSION if service == "ec2" else STS_VERSION)
        flat = {"Action": action, "Version": version, **flatten_params(params or {})}
        body = urllib.parse.urlencode(sorted(flat.items()), quote_via=urllib.parse.quote).encode()
        host = f"{service}.{self.region}.amazonaws.com"
        headers = {"Host": host,
                   "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
        # A launch is retried only with a ClientToken (AWS then answers a retry with the same
        # instance); without one a 5xx or a throttle could have launched it already.
        launches = action == "RunInstances" and not (params or {}).get("DryRun")
        retry = not launches or bool((params or {}).get("ClientToken"))
        status, data, creds = self._send(service, "POST", f"https://{host}/", headers, body,
                                         action, retry=retry)
        if status != 200:
            code, _msg = _xml_error(data)
            if code == "DryRunOperation":
                return {"DryRunOperation": True}
            self._raise(status, data, creds, action, params)
        try:
            parsed = parse_xml(data)
        except (ET.ParseError, ValueError):
            raise AwsError("bad_response", f"AWS sent an unreadable answer to {action}.",
                           action=action) from None
        content = next(iter(parsed.values()))
        if isinstance(content, dict) and f"{action}Result" in content:
            content = content[f"{action}Result"]
        return content if isinstance(content, dict) else {"value": content}

    def ec2(self, action: str, params: dict | None = None) -> dict:
        return self.query("ec2", action, params)

    def sts(self, action: str, params: dict | None = None) -> dict:
        return self.query("sts", action, params)

    def ssm_parameter(self, name: str) -> str:
        """The value of a public SSM parameter (``ssm:GetParameter``, JSON protocol)."""
        host = f"ssm.{self.region}.amazonaws.com"
        body = json.dumps({"Name": name}).encode("utf-8")
        headers = {"Host": host, "Content-Type": "application/x-amz-json-1.1",
                   "X-Amz-Target": "AmazonSSM.GetParameter"}
        status, data, creds = self._send("ssm", "POST", f"https://{host}/", headers, body,
                                         "GetParameter")
        if status != 200:
            self._raise(status, data, creds, "GetParameter")
        try:
            value = json.loads(data.decode("utf-8"))["Parameter"]["Value"]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise AwsError("bad_response", "AWS sent an unreadable answer to GetParameter.",
                           action="GetParameter") from None
        return str(value)


def _xml_or_json_code(data: bytes) -> str:
    code, _ = _xml_error(data or b"")
    if code:
        return code
    try:
        return str(json.loads((data or b"{}").decode("utf-8")).get("__type", "")).rsplit("#", 1)[-1]
    except (ValueError, AttributeError):
        return ""
