"""EC2 hosts against an in-process fake AWS (no sockets, no real AWS, no real ssh).

The fake replaces the one ``http`` callable the AWS client uses. It answers the EC2 Query API
in AWS's XML shape, STS GetCallerIdentity, SSM GetParameter and checkip, keeps instances, key
pairs and security groups in memory, and records every call so the tests can check which
actions ran, with which tags, and that no key value ever travels outside the signature.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from xml.sax.saxutils import escape

import pytest

from cloud_compute_core import aws as aws_mod
from cloud_compute_core import ec2 as ec2_mod
from cloud_compute_core.interfaces import ApiError, ApiRequest
from cloud_compute_core.models import Ec2Settings, HostSpec, TemplateSpec

AK = "AKIDFAKEFAKEFAKE0000"            # ws-scan:allow (documented fake test value)
SK = "fake/secret/key/value/for/tests/only000"   # ws-scan:allow (fake)
TOKEN = "fake-session-token-value-xyz"           # ws-scan:allow (fake)
T0 = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
HOST_KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIFakeHostKeyForTestsOnly0000000000000000"


# --------------------------------------------------------------------------- fake AWS


def _xml(tag: str, value) -> str:
    if isinstance(value, dict):
        return f"<{tag}>" + "".join(_xml(k, v) for k, v in value.items()) + f"</{tag}>"
    if isinstance(value, list):
        return f"<{tag}>" + "".join(_xml("item", v) for v in value) + f"</{tag}>"
    if isinstance(value, bool):
        value = "true" if value else "false"
    return f"<{tag}>{escape(str(value))}</{tag}>"


def _err(status: int, code: str, message: str = "") -> tuple[int, bytes]:
    return status, (f"<Response><Errors><Error><Code>{code}</Code><Message>{escape(message)}"
                    "</Message></Error></Errors><RequestID>r</RequestID></Response>").encode()


class FakeAws:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.instances: dict[str, dict] = {}
        self.keys: dict[str, dict] = {}
        self.groups: dict[str, dict] = {}
        self.errors: dict[str, tuple[int, str, str]] = {}
        self.my_ip = "198.51.100.7"
        self.counter = 0
        self.raw: list[tuple[str, dict, bytes]] = []
        self.tokens: dict[str, str] = {}
        self.account = "123456789012"

    # helpers for tests
    def actions(self) -> list[str]:
        return [a for _s, a, _p in self.calls]

    def add_instance(self, tags: dict, state="running") -> str:
        self.counter += 1
        iid = f"i-0{self.counter:016x}"
        self.instances[iid] = {"id": iid, "state": state, "tags": dict(tags), "ip": "203.0.113.50",
                               "type": "t3.micro", "user_data": ""}
        return iid

    def __call__(self, method, url, headers, body, timeout):
        self.raw.append((url, dict(headers), body or b""))
        host = urllib.parse.urlsplit(url).hostname or ""
        if url == aws_mod.CHECKIP_URL:
            return 200, (self.my_ip + "\n").encode()
        assert "Authorization" in headers and headers["Authorization"].startswith("AWS4-HMAC")
        service = host.split(".")[0]
        if service == "ssm":
            name = json.loads(body)["Name"]
            self.calls.append(("ssm", "GetParameter", {"Name": name}))
            arch = "arm64" if "arm64" in name else "x86_64"
            return 200, json.dumps({"Parameter": {"Value": "ami-0abc" + ("1" if arch == "arm64"
                                                                          else "0") * 13}}
                                   ).encode()
        params = dict(urllib.parse.parse_qsl(body.decode()))
        action = params.pop("Action")
        params.pop("Version", None)
        self.calls.append((service, action, params))
        if action in self.errors:
            status, code, msg = self.errors[action]
            return _err(status, code, msg)
        handler = getattr(self, "do_" + action)
        status, content = handler(params)
        if status != 200:
            return status, content
        return 200, _xml(f"{action}Response", content).encode()

    # STS
    def do_GetCallerIdentity(self, p):
        return 200, {"GetCallerIdentityResult": {"Account": self.account,
                                                 "Arn": f"arn:aws:iam::{self.account}:user/cc"}}

    # EC2
    def do_DescribeRegions(self, p):
        return 200, {"regionInfo": [{"regionName": p.get("RegionName.1", "us-east-1")}]}

    def do_DescribeImages(self, p):
        image = p["ImageId.1"]
        return 200, {"imagesSet": [{"imageId": image, "rootDeviceName": "/dev/xvda",
                                    "architecture": "arm64" if image.endswith("1") else "x86_64"}]}

    def do_DescribeInstanceTypes(self, p):
        return 200, {"instanceTypeSet": [
            {"instanceType": "t3.micro", "memoryInfo": {"sizeInMiB": 1024},
             "vCpuInfo": {"defaultVCpus": 2}},
            {"instanceType": "t2.micro", "memoryInfo": {"sizeInMiB": 1024},
             "vCpuInfo": {"defaultVCpus": 1}}]}

    def _tags_from(self, p, n):
        tags, i = {}, 1
        while f"TagSpecification.{n}.Tag.{i}.Key" in p:
            tags[p[f"TagSpecification.{n}.Tag.{i}.Key"]] = p[f"TagSpecification.{n}.Tag.{i}.Value"]
            i += 1
        return tags

    def do_RunInstances(self, p):
        if p.get("DryRun") == "true":
            return _err(412, "DryRunOperation", "Request would have succeeded")
        token = p.get("ClientToken")
        if token and token in self.tokens:           # AWS answers a retry with the same instance
            iid = self.tokens[token]
            return 200, {"instancesSet": [{"instanceId": iid,
                                           "instanceState": {"name": "pending"}}]}
        iid = self.add_instance(self._tags_from(p, 1), state="pending")
        self.instances[iid].update(type=p["InstanceType"], user_data=p.get("UserData", ""),
                                   ip=None, token=token)
        if token:
            self.tokens[token] = iid
        return 200, {"instancesSet": [{"instanceId": iid,
                                       "instanceState": {"name": "pending"}}]}

    def _item(self, inst):
        out = {"instanceId": inst["id"], "instanceState": {"name": inst["state"]},
               "instanceType": inst["type"],
               "tagSet": [{"key": k, "value": v} for k, v in inst["tags"].items()]}
        if inst.get("ip") and inst["state"] == "running":
            out["ipAddress"] = inst["ip"]
        return out

    def do_DescribeInstances(self, p):
        if "InstanceId.1" in p:
            inst = self.instances.get(p["InstanceId.1"])
            if inst is None:
                return _err(400, "InvalidInstanceID.NotFound", "not found")
            found = [inst]
        else:
            filters: dict[str, list[str]] = {}
            n = 1
            while f"Filter.{n}.Name" in p:
                filters[p[f"Filter.{n}.Name"]] = [v for k, v in sorted(p.items())
                                                  if k.startswith(f"Filter.{n}.Value.")]
                n += 1
            found = []
            for i in self.instances.values():
                want = filters.get("tag:cloud-compute:install")
                if want is not None and i["tags"].get("cloud-compute:install") not in want:
                    continue
                if "client-token" in filters and i.get("token") not in filters["client-token"]:
                    continue
                states = filters.get("instance-state-name")
                if (states is not None and i["state"] not in states) or (
                        states is None and i["state"] == "terminated"):
                    continue
                found.append(i)
        for inst in found:                       # time passes between describes
            if inst["state"] == "pending":
                self.counter += 1
                inst.update(state="running", ip=f"203.0.113.{10 + self.counter}")
            elif inst["state"] == "stopping":
                inst["state"] = "stopped"
        return 200, {"reservationSet": [{"instancesSet": [self._item(i)]} for i in found]}

    def do_DescribeInstanceStatus(self, p):
        return 200, {"instanceStatusSet": [{"instanceId": p["InstanceId.1"],
                                            "systemStatus": {"status": "ok"},
                                            "instanceStatus": {"status": "ok"}}]}

    def do_GetConsoleOutput(self, p):
        text = ("boot...\n-----BEGIN SSH HOST KEY KEYS-----\n"
                f"ssh-ed25519 {HOST_KEY} root@ip\n-----END SSH HOST KEY KEYS-----\n")
        return 200, {"instanceId": p["InstanceId"],
                     "output": base64.b64encode(text.encode()).decode()}

    def _state(self, p, new):
        inst = self.instances[p["InstanceId.1"]]
        inst["state"] = new
        if new == "pending":
            inst["ip"] = None
        return 200, {"instancesSet": [{"instanceId": inst["id"]}]}

    def do_StopInstances(self, p):
        return self._state(p, "stopping")

    def do_StartInstances(self, p):
        return self._state(p, "pending")

    def do_TerminateInstances(self, p):
        return self._state(p, "terminated")

    def do_ImportKeyPair(self, p):
        self.keys[p["KeyName"]] = {"tags": self._tags_from(p, 1),
                                   "material": p["PublicKeyMaterial"]}
        return 200, {"keyName": p["KeyName"], "keyPairId": "key-0123"}

    def do_DescribeKeyPairs(self, p):
        key = self.keys.get(p["KeyName.1"])
        return 200, {"keySet": [{"keyName": p["KeyName.1"], "tagSet": [
            {"key": k, "value": v} for k, v in key["tags"].items()]}] if key else []}

    def do_DeleteKeyPair(self, p):
        self.keys.pop(p["KeyName"], None)
        return 200, {"return": True}

    def do_DescribeVpcs(self, p):
        return 200, {"vpcSet": [{"vpcId": "vpc-0123456789abcdef0", "isDefault": True}]}

    def do_CreateSecurityGroup(self, p):
        gid = "sg-0123456789abcdef0"
        self.groups[gid] = {"tags": self._tags_from(p, 1), "cidrs": []}
        return 200, {"groupId": gid}

    def do_DescribeSecurityGroups(self, p):
        g = self.groups.get(p["GroupId.1"])
        if g is None:
            return _err(400, "InvalidGroup.NotFound", "nope")
        return 200, {"securityGroupInfo": [{"groupId": p["GroupId.1"], "tagSet": [
            {"key": k, "value": v} for k, v in g["tags"].items()],
            "ipPermissions": [{"fromPort": 22, "ipRanges": [{"cidrIp": c} for c in g["cidrs"]]}]}]}

    def do_AuthorizeSecurityGroupIngress(self, p):
        self.groups[p["GroupId"]]["cidrs"].append(p["IpPermissions.1.IpRanges.1.CidrIp"])
        return 200, {"return": True}

    def do_RevokeSecurityGroupIngress(self, p):
        self.groups[p["GroupId"]]["cidrs"].remove(p["IpPermissions.1.IpRanges.1.CidrIp"])
        return 200, {"return": True}

    def do_DeleteSecurityGroup(self, p):
        self.groups.pop(p["GroupId"], None)
        return 200, {"return": True}


# --------------------------------------------------------------------------- fixtures


class FakeProber:
    def __init__(self):
        self.tests = 0

    def test_connection(self, spec, _machine=False):
        self.tests += 1
        return SimpleNamespace(to_dict=lambda: {"ok": True, "status": "online", "steps": []})

    def probe(self, host):
        return None


class FakeFiles:
    def __init__(self):
        self.saved: list[str] = []

    def export(self, instance_id, path=None, *, reason="manual"):
        self.saved.append(instance_id)
        return {"id": "x-00000001", "instance_id": instance_id, "reason": reason,
                "local_path": "C:/exports/x.tar.gz", "bytes": 10, "files": 1}


def _keygen(path, comment):
    path.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n")  # ws-scan:allow
    path.with_name(path.name + ".pub").write_text(f"ssh-ed25519 AAAAfakepublic {comment}\n")


@pytest.fixture
def env(services, fake_secrets, fake_events, fake_transports):
    fake_secrets.set("AWS_ACCESS_KEY_ID", AK)
    fake_secrets.set("AWS_SECRET_ACCESS_KEY", SK)
    fake = FakeAws()
    clock = SimpleNamespace(now=T0)
    mgr = ec2_mod.Ec2Manager(services, http=fake, keygen=_keygen, sleep=lambda s: None,
                             clock=lambda: clock.now, background=False, poll_s=0)
    services.set("ec2", mgr)
    services.set("prober", FakeProber())
    services.set("files", FakeFiles())
    host = HostSpec(id="aws", name="AWS box", kind="ec2", address=ec2_mod.NO_MACHINE,
                    ec2=Ec2Settings(region="us-east-1"))
    services.store.upsert_host(host)
    from cloud_compute_core.testing import exec_result
    fake_transports.get("aws").script(re.compile(r"shutdown"), exec_result(rc=0))
    return SimpleNamespace(services=services, mgr=mgr, fake=fake, clock=clock, events=fake_events,
                           secrets=fake_secrets, transports=fake_transports, store=services.store)


def _host(env):
    return env.store.get_host("aws")


def _start(env):
    env.mgr.start_machine(_host(env), wait=True)
    return _host(env)


# --------------------------------------------------------------------------- signing


# Published AWS Signature Version 4 examples (credentials AKIDEXAMPLE / the documented example
# secret, 2015-08-30T12:36:00Z), copied by hand from the AWS SigV4 test suite and the IAM
# ListUsers walk-through in the AWS General Reference.
EXAMPLE = aws_mod.Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")  # ws-scan:allow


@pytest.mark.parametrize("method,url,headers,service,signature", [
    ("GET", "https://example.amazonaws.com/", {"Host": "example.amazonaws.com"}, "service",
     "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"),            # get-vanilla
    ("POST", "https://example.amazonaws.com/", {"Host": "example.amazonaws.com"}, "service",
     "5da7c1a2acd57cee7505fc6676e4e544621c30862966e37dddb68e92efbe5d6b"),            # post-vanilla
    ("GET", "https://example.amazonaws.com/?Param2=value2&Param1=value1",
     {"Host": "example.amazonaws.com"}, "service",
     "b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500"),            # query order
    ("GET", "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
     {"Host": "iam.amazonaws.com",
      "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}, "iam",
     "5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"),            # IAM example
])
def test_sigv4_published_vectors(method, url, headers, service, signature):
    out = aws_mod.sign_request(method, url, headers, b"", credentials=EXAMPLE,
                               region="us-east-1", service=service, amz_date="20150830T123600Z")
    assert out["Authorization"].endswith("Signature=" + signature)
    assert f"Credential=AKIDEXAMPLE/20150830/us-east-1/{service}/aws4_request" in out["Authorization"]


def test_signing_key_published_example():
    key = aws_mod.signing_key("wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "20120215",  # ws-scan:allow
                              "us-east-1", "iam")
    assert key.hex() == "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d"


def test_session_token_is_signed_header():
    creds = aws_mod.Credentials("AKIDEXAMPLE", "x", "tok")
    out = aws_mod.sign_request("POST", "https://ec2.us-east-1.amazonaws.com/",
                               {"Host": "ec2.us-east-1.amazonaws.com"}, b"a=b",
                               credentials=creds, region="us-east-1", service="ec2",
                               amz_date="20261110T120000Z")
    assert out["X-Amz-Security-Token"] == "tok"
    assert "x-amz-security-token" in out["Authorization"]


def test_flatten_and_parse():
    flat = aws_mod.flatten_params({"InstanceId": ["i-1", "i-2"], "Filter": [
        {"Name": "a", "Value": ["b"]}], "DryRun": True})
    assert flat == {"InstanceId.1": "i-1", "InstanceId.2": "i-2", "Filter.1.Name": "a",
                    "Filter.1.Value.1": "b", "DryRun": "true"}
    parsed = aws_mod.parse_xml(b'<X xmlns="http://ec2"><set><item><a>1</a></item></set></X>')
    assert parsed == {"X": {"set": [{"a": "1"}]}}
    with pytest.raises(ValueError):
        aws_mod.parse_xml(b'<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>')


# --------------------------------------------------------------------------- plain errors


@pytest.mark.parametrize("code,needle", [
    ("AuthFailure", "did not accept the access key"),
    ("ExpiredToken", "session token has expired"),
    ("UnauthorizedOperation", "not allowed to call RunInstances"),
    ("InstanceLimitExceeded", "quota"),
    ("VcpuLimitExceeded", "quota"),
    ("InsufficientInstanceCapacity", "no free t3.micro capacity"),
    ("OptInRequired", "not enabled"),
])
def test_api_errors_become_plain_sentences(env, code, needle):
    env.fake.errors["RunInstances"] = (400, code, f"raw text with {SK}")
    with pytest.raises(ApiError) as info:
        _start(env)
    assert needle in info.value.message
    assert SK not in info.value.message and AK not in info.value.message


def test_no_credentials_and_bad_region(env):
    env.secrets.values.pop("AWS_SECRET_ACCESS_KEY", None) if hasattr(env.secrets, "values") \
        else None
    s = Ec2Settings(region="us-east-1", secret_key_secret="NOT_SET_ANYWHERE")
    result = env.mgr.test(HostSpec(id="t", name="t", kind="ec2", address="x", ec2=s))
    assert not result["ok"] and result["steps"][0]["name"] == "credentials"
    assert "AWS_ACCESS_KEY_ID" in result["steps"][0]["detail"]
    with pytest.raises(aws_mod.AwsError):
        aws_mod.AwsClient("not a region", lambda: None)


def test_network_failure_is_plain(env):
    def broken(*a, **k):
        raise OSError("name resolution failed")
    client = aws_mod.AwsClient("us-east-1", lambda: aws_mod.Credentials(AK, SK), http=broken,
                               sleep=lambda s: None)
    with pytest.raises(aws_mod.AwsError) as info:
        client.ec2("DescribeRegions")
    assert "could not be reached" in info.value.plain and SK not in info.value.plain


# --------------------------------------------------------------------------- connection test


def test_connection_test_rows_and_dry_run(env):
    result = env.mgr.test(_host(env))
    names = [s["name"] for s in result["steps"]]
    assert names == ["credentials", "identity", "region", "image", "launch", "address"]
    assert result["ok"] and all(s["ok"] for s in result["steps"])
    runs = [p for _s, a, p in env.fake.calls if a == "RunInstances"]
    assert len(runs) == 1 and runs[0]["DryRun"] == "true"
    assert not env.fake.instances                 # nothing launched
    assert "198.51.100.7" in result["steps"][-1]["detail"]


def test_dry_run_unauthorized_row(env):
    env.fake.errors["RunInstances"] = (403, "UnauthorizedOperation", "no")
    result = env.mgr.test(_host(env))
    row = next(s for s in result["steps"] if s["name"] == "launch")
    assert row["ok"] is False and "not allowed" in row["detail"] and row["hint"]


def test_expired_token_row(env):
    env.fake.errors["GetCallerIdentity"] = (403, "ExpiredToken", "x")
    result = env.mgr.test(_host(env))
    row = next(s for s in result["steps"] if s["name"] == "identity")
    assert row["ok"] is False and "expired" in row["detail"]
    assert result["status"] == "auth_required"


def test_free_tier_types_from_aws(env):
    types = env.mgr.free_tier_types(Ec2Settings())
    assert [t["instance_type"] for t in types] == ["t2.micro", "t3.micro"]
    _s, action, params = env.fake.calls[-1]
    assert action == "DescribeInstanceTypes" and params["Filter.1.Name"] == "free-tier-eligible"


def test_free_tier_types_follow_sparse_pages(env, monkeypatch):
    # Real AWS (us-east-1) applies the filter per page: most pages come back empty with a
    # nextToken, and t3.micro arrived on page 13 of 14.
    pages = [[] for _ in range(14)]
    pages[1] = [{"instanceType": "c7i-flex.large", "memoryInfo": {"sizeInMiB": 4096}}]
    pages[12] = [{"instanceType": "t3.micro", "memoryInfo": {"sizeInMiB": 1024}}]

    def paged(p):
        n = int(p.get("NextToken") or 0)
        content: dict = {"instanceTypeSet": pages[n]}
        if n + 1 < len(pages):
            content["nextToken"] = str(n + 1)
        return 200, content
    monkeypatch.setattr(env.fake, "do_DescribeInstanceTypes", paged)
    types = env.mgr.free_tier_types(Ec2Settings())
    assert [t["instance_type"] for t in types] == ["t3.micro", "c7i-flex.large"]


# --------------------------------------------------------------------------- start / stop / start


def test_launch_wait_ready(env):
    host = _start(env)
    s = host.ec2
    assert s.machine_state == "ready", s.machine_detail
    assert s.instance_id in env.fake.instances
    assert host.address.startswith("203.0.113.")
    run = next(p for _s, a, p in env.fake.calls if a == "RunInstances")
    assert run["InstanceInitiatedShutdownBehavior"] == "stop"
    assert run["InstanceType"] == "t3.micro" and run["MetadataOptions.HttpTokens"] == "required"
    assert run["BlockDeviceMapping.1.Ebs.VolumeSize"] == "10"
    ud = base64.b64decode(run["UserData"]).decode()
    assert "--on-active=120m" in ud and "dnf install -y docker" in ud and "ec2-user" in ud
    tags = env.fake.instances[s.instance_id]["tags"]
    assert tags["cloud-compute:install"] and tags["cloud-compute:host"] == "aws"
    assert run["TagSpecification.2.ResourceType"] == "volume"
    # key pair, group, SSH rule only from this computer
    assert s.key_name in env.fake.keys and env.fake.keys[s.key_name]["tags"]["cloud-compute:install"]
    assert env.fake.groups[s.managed_group_id]["cidrs"] == ["198.51.100.7/32"]
    # host key pinned under the instance id, in the plugin's own file
    known = open(s.known_hosts_file).read()
    assert known.startswith(f"{s.instance_id} ssh-ed25519 {HOST_KEY}")
    assert env.services.prober.tests >= 1          # the normal connection test gated "ready"
    rearm = [a for a in env.transports.get("aws").argvs() if "systemd-run" in " ".join(a)]
    assert rearm and rearm[-1][-1] == "120"


def test_new_instance_not_yet_visible_is_waited_for(env, monkeypatch):
    # Real AWS: DescribeInstances by id answered InvalidInstanceID.NotFound 0.4 s after
    # RunInstances returned the id (eventual consistency). That is not "gone".
    real = env.fake.do_DescribeInstances
    misses = {"n": 0}

    def lagging(p):
        if "InstanceId.1" in p and misses["n"] < 3:
            misses["n"] += 1
            return _err(400, "InvalidInstanceID.NotFound", "The instance ID does not exist")
        return real(p)
    monkeypatch.setattr(env.fake, "do_DescribeInstances", lagging)
    host = _start(env)
    assert misses["n"] == 3
    assert host.ec2.machine_state == "ready", host.ec2.machine_detail


def test_ssh_options_for_ec2(env):
    from cloud_compute_core.transport import ssh_base_args
    host = _start(env)
    args = ssh_base_args(host)
    joined = " ".join(args)
    assert f"UserKnownHostsFile={host.ec2.known_hosts_file}" in joined
    assert f"HostKeyAlias={host.ec2.instance_id}" in joined
    assert "StrictHostKeyChecking=yes" in joined and "IdentitiesOnly=yes" in joined
    assert "accept-new" not in joined
    assert host.ec2.key_file in args


def test_stop_and_start_with_new_address(env):
    host = _start(env)
    first_ip = host.address
    env.mgr.stop_machine(host)
    host = _host(env)
    assert host.ec2.machine_state == "stopped" and host.address == ec2_mod.NO_MACHINE
    assert "StopInstances" in env.fake.actions()
    env.fake.my_ip = "198.51.100.99"               # this computer's address changed meanwhile
    host = env.mgr.start_machine(host, wait=True)
    host = _host(env)
    assert host.ec2.machine_state == "ready" and host.address != first_ip
    assert env.fake.groups[host.ec2.managed_group_id]["cidrs"] == ["198.51.100.99/32"]


def test_start_after_stop_ignores_the_stale_stopped_view(env, monkeypatch):
    # Real AWS: 1 s after StartInstances was accepted, DescribeInstances still showed the
    # instance as stopped with the reason of the earlier stop (Client.UserInitiatedShutdown),
    # then pending (same stale reason), then running with a new address.
    host = _start(env)
    first_ip = host.address
    env.mgr.stop_machine(host)
    real_start, real_item = env.fake.do_StartInstances, env.fake._item
    lag = {"n": 0, "after_start": False}

    def start(p):
        lag["after_start"] = True
        return real_start(p)

    def item(inst):
        out = real_item(inst)
        if lag["after_start"]:
            out["stateReason"] = {"code": "Client.UserInitiatedShutdown",
                                  "message": "Client.UserInitiatedShutdown: User initiated shutdown"}
            if lag["n"] < 2:
                lag["n"] += 1
                out["instanceState"] = {"name": "stopped"}
                out.pop("ipAddress", None)
        return out
    monkeypatch.setattr(env.fake, "do_StartInstances", start)
    monkeypatch.setattr(env.fake, "_item", item)
    env.mgr.start_machine(_host(env), wait=True)
    host = _host(env)
    assert lag["n"] == 2
    assert host.ec2.machine_state == "ready", host.ec2.machine_detail
    assert host.address not in (first_ip, ec2_mod.NO_MACHINE)


def test_start_fails_when_it_stops_again_after_leaving_stopped(env, monkeypatch):
    host = _start(env)
    env.mgr.stop_machine(host)
    real_item, seen = env.fake._item, {"n": 0}

    def item(inst):                      # pending once, then stopped for real
        out = real_item(inst)
        if inst["state"] in ("pending", "running"):
            seen["n"] += 1
            out["instanceState"] = {"name": "pending" if seen["n"] == 1 else "stopped"}
            out.pop("ipAddress", None)
        return out
    monkeypatch.setattr(env.fake, "_item", item)
    env.mgr.start_machine(_host(env), wait=True)
    assert _host(env).ec2.machine_state == "error"
    assert "stopped while starting" in _host(env).ec2.machine_detail


def test_every_mutating_call_reads_tags_first(env):
    host = _start(env)
    env.mgr.stop_machine(host)
    env.mgr.start_machine(_host(env), wait=True)
    env.mgr.terminate_machine(_host(env))
    calls = env.fake.calls
    for i, (_s, action, params) in enumerate(calls):
        if action in ("StopInstances", "StartInstances", "TerminateInstances"):
            iid = params["InstanceId.1"]
            before = [p for _x, a, p in calls[:i] if a == "DescribeInstances"]
            assert before and before[-1].get("InstanceId.1") == iid, action


def test_untagged_instance_never_touched(env):
    foreign = env.fake.add_instance({"Name": "someone else"})
    other = env.fake.add_instance({"cloud-compute:install": "another-install"})
    for iid in (foreign, other):
        h = _host(env)
        h = HostSpec(**{**h.__dict__, "ec2": Ec2Settings(instance_id=iid, machine_state="ready")})
        env.store.upsert_host(h)
        with pytest.raises(ApiError) as info:
            env.mgr.stop_machine(h)
        assert "did not change it" in info.value.message
        with pytest.raises(ApiError):
            env.mgr.terminate_machine(h)
    assert not {"StopInstances", "TerminateInstances", "StartInstances"} & set(env.fake.actions())


# --------------------------------------------------------------------------- data safety


def _environment(env, status="running"):
    from cloud_compute_core.testing import sample_instance
    rec = sample_instance(id="i-ab12cd34", host_id="aws", status=status)
    env.store.upsert_instance(rec)
    return rec


def test_terminate_refused_with_environments_then_saves_each(env, monkeypatch):
    _start(env)
    rec = _environment(env)
    with pytest.raises(ApiError) as info:
        env.mgr.terminate_machine(_host(env))
    assert "Save and delete them first" in info.value.message
    assert "TerminateInstances" not in env.fake.actions()
    destroyed = []

    def destroy(instance_id, *, save, report, notify):
        assert save is True
        destroyed.append(instance_id)
        report["message"] = "saved"
    monkeypatch.setattr(env.services.sandboxes, "destroy", destroy)
    out = env.mgr.terminate_machine(_host(env), delete_environments=True)
    assert destroyed == [rec.id] and out["terminated"]
    assert env.fake.actions().index("TerminateInstances") > 0
    assert _host(env).ec2.machine_state == "none"


def test_terminate_of_stopped_machine_needs_start_or_confirm(env, monkeypatch):
    host = _start(env)
    _environment(env, status="stopped")
    env.mgr.stop_machine(host)
    with pytest.raises(ApiError) as info:
        env.mgr.terminate_machine(_host(env), delete_environments=True)
    assert info.value.code == "not_saved" and "Terminate without saving" in info.value.message
    assert "start the machine" in info.value.message
    assert "TerminateInstances" not in env.fake.actions()


def test_failed_save_blocks_terminate(env, monkeypatch):
    _start(env)
    _environment(env)

    def destroy(instance_id, **kw):
        raise ApiError(409, "not_saved", "could not be saved")
    monkeypatch.setattr(env.services.sandboxes, "destroy", destroy)
    with pytest.raises(ApiError):
        env.mgr.terminate_machine(_host(env), delete_environments=True)
    assert "TerminateInstances" not in env.fake.actions()


def test_stop_keeps_environments_as_stopped(env, monkeypatch):
    host = _start(env)
    rec = _environment(env)
    monkeypatch.setattr(env.services.sandboxes, "stop",
                        lambda iid, reason: (_ for _ in ()).throw(RuntimeError("ssh down")))
    env.mgr.stop_machine(host)
    assert env.store.get_instance(rec.id).status == "stopped"


# --------------------------------------------------------------------------- timers + reconcile


def test_lifetime_and_idle_rules(env):
    host = _start(env)
    env.clock.now = T0 + timedelta(minutes=5)
    env.mgr.tick(_host(env))                       # starts the idle clock
    assert _host(env).ec2.machine_state == "ready"
    env.clock.now = T0 + timedelta(minutes=40)
    env.mgr.tick(_host(env))
    assert _host(env).ec2.machine_state == "stopped"   # idle 30 min
    env.mgr.start_machine(_host(env), wait=True)
    _environment(env)                              # busy: idle rule does not apply
    env.clock.now = T0 + timedelta(minutes=40 + 121)
    env.mgr.tick(_host(env))
    assert _host(env).ec2.machine_state == "stopped"   # max lifetime


def test_reconcile_lists_untracked_and_never_touches(env):
    _start(env)
    install = env.mgr.install_id()
    lost = env.fake.add_instance({"cloud-compute:install": install, "Name": "lost"})
    env.fake.add_instance({"Name": "not ours"})
    before = list(env.fake.actions())
    data = env.mgr.reconcile(_host(env))
    assert [i["instance_id"] for i in data["untracked"]] == [lost]
    new = env.fake.actions()[len(before):]
    assert set(new) == {"DescribeInstances"}
    filt = [p for _s, a, p in env.fake.calls if a == "DescribeInstances" and "Filter.1.Name" in p][-1]
    assert filt["Filter.1.Name"] == "tag:cloud-compute:install" and filt["Filter.1.Value.1"] == install
    with pytest.raises(ApiError):
        env.mgr.act_untracked(_host(env), "i-0ffffffffffffffff", "terminate")
    env.mgr.act_untracked(_host(env), lost, "stop")
    assert env.fake.instances[lost]["state"] in ("stopping", "stopped")


def test_reconcile_sees_self_stop(env):
    host = _start(env)
    rec = _environment(env)
    env.fake.instances[host.ec2.instance_id]["state"] = "stopped"   # its own timer fired
    env.mgr.reconcile(_host(env))
    assert _host(env).ec2.machine_state == "stopped"
    assert env.store.get_instance(rec.id).status == "stopped"


# --------------------------------------------------------------------------- integration


def test_prober_never_dials_a_stopped_machine(env):
    from cloud_compute_core.prober import Prober
    prober = Prober(env.services)
    t = env.transports.get("aws")
    t.reset_calls()
    assert prober.liveness(_host(env)) in ("offline", "unknown")
    state = prober.probe(_host(env))
    assert state.status == "offline" and "Start machine" in state.last_error
    assert t.tcp_checks == 0 and not t.calls


def test_spawn_refused_until_machine_ready(env):
    tpl = TemplateSpec(id="py", name="py", description="", host_kinds=["ssh", "local"])
    env.services.set("templates", SimpleNamespace(get=lambda i: tpl, list=lambda: [tpl]))
    from cloud_compute_core.sandbox import SandboxManager
    mgr = SandboxManager(env.services)
    with pytest.raises(ApiError) as info:
        mgr.validate_spawn("py", "aws")
    assert info.value.code == "not_running" and "Start machine" in info.value.message
    _start(env)
    got_tpl, got_host = mgr.validate_spawn("py", "aws")   # an SSH template fits an EC2 host
    assert got_host.kind == "ec2"


def test_rest_create_ignores_plugin_fields_and_delete_cleans_up(env):
    from cloud_compute_core import hosts_api
    body = {"name": "Cloud", "kind": "ec2", "address": "evil.example", "ec2": {
        "region": "eu-west-1", "instance_id": "i-0123456789abcdef0", "machine_state": "ready",
        "key_file": "C:/x", "ssh_cidr": "203.0.113.0/24"}}
    out = hosts_api.create_host(ApiRequest(path_params={}, query={}, body=body,
                                           services=env.services))
    spec = out["spec"]
    assert spec["address"] == ec2_mod.NO_MACHINE and spec["ec2"]["instance_id"] is None
    assert spec["ec2"]["machine_state"] == "none" and spec["ec2"]["key_file"] is None
    with pytest.raises(ApiError):
        hosts_api.create_host(ApiRequest(path_params={}, query={}, services=env.services, body={
            "name": "Open", "kind": "ec2", "ec2": {"ssh_cidr": "0.0.0.0/0"}}))
    host = _start(env)
    with pytest.raises(ApiError) as info:
        hosts_api.delete_host(ApiRequest(path_params={"host_id": "aws"}, query={}, body=None,
                                         services=env.services))
    assert "Terminate the machine first" in info.value.message
    env.mgr.terminate_machine(_host(env))
    res = hosts_api.delete_host(ApiRequest(path_params={"host_id": "aws"}, query={}, body=None,
                                           services=env.services))
    assert res["deleted"] and not env.fake.keys and not env.fake.groups
    assert {"DeleteKeyPair", "DeleteSecurityGroup"} <= set(env.fake.actions())


def test_chat_machine_actions_ask_and_agree_with_fallback(env):
    from cloud_compute_core import approval, hosts_api
    tool = next(t for t in hosts_api.tools() if t.name == "compute_host")

    def ask(action, **extra):
        args = {"action": action, "host_id": "aws", **extra}
        got = approval.approval_for("compute_host", args, tool, env.services)
        assert got and got["action"] == "approve", (action, got)
        text = got["message"]
        assert "us-east-1" in text and "t3.micro" in text and "10 GB" in text and "2 hours" in text
        assert SK not in text and AK not in text
        return text

    assert "Launches a new EC2 machine" in ask("start")
    assert "AWS bills the account" in ask("start")
    _start(env)                                    # the others need a machine that runs
    assert "AWS bills the account" in ask("extend", minutes=30)
    ask("stop")
    assert "saved to this computer" in ask("terminate", delete_environments=True)
    k1 = approval.rule_key("compute_host", {"action": "start", "host_id": "aws"}, "aws", "a")
    k2 = approval.rule_key("compute_host", {"action": "start", "host_id": "aws"}, "aws", "b")
    assert k1 != k2


def test_fallback_needs_approval_for_machine_actions():
    import importlib.util
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "__init__.py").read_text(encoding="utf-8")
    for action in ("start", "stop", "terminate", "extend"):
        assert f'"{action}"' in src.split("def _fallback_needs_approval", 1)[1].split("def ", 1)[0]


def test_env_spawn_card_names_the_cost(env):
    from cloud_compute_core import instances_api
    tpl = TemplateSpec(id="py", name="py", description="", host_kinds=["ssh"])
    env.services.set("templates", SimpleNamespace(get=lambda i: tpl, list=lambda: [tpl]))
    _start(env)
    text = instances_api.approval_env_spawn({"template": "py", "host_id": "aws"}, env.services)
    text = str(text)
    assert "us-east-1" in text and "t3.micro" in text and "10 GB" in text
    assert "AWS bills the account" in text


def test_secrets_never_leave_the_signature(env):
    env.secrets.set("AWS_SESSION_TOKEN", TOKEN)
    host = _start(env)
    env.mgr.stop_machine(host)
    env.fake.errors["StartInstances"] = (400, "InsufficientInstanceCapacity", f"x {SK} {AK}")
    with pytest.raises(ApiError) as info:
        env.mgr.start_machine(_host(env))
    assert SK not in info.value.message and AK not in info.value.message
    for url, headers, body in env.fake.raw:
        for value in (SK, TOKEN):
            assert value not in url and value.encode() not in body
            assert all(value not in v for k, v in headers.items()
                       if k not in ("X-Amz-Security-Token",))
        assert AK not in url and AK.encode() not in body
    db = open(env.store.path, "rb").read()
    for value in (AK, SK, TOKEN):
        assert value.encode() not in db
        assert value not in json.dumps(env.events.events if hasattr(env.events, "events")
                                       else [], default=str)
    for row in env.store.list_hosts():
        assert SK not in json.dumps(row.to_dict()) and AK not in json.dumps(row.to_dict())
