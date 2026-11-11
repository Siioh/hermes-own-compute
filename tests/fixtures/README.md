# Test fixtures

Captured command outputs used by the unit tests, one folder per area:

```
tests/fixtures/
  hosts/      probe output (uname, podman info, nvidia-smi, df, ...)
  sandbox/    podman/docker ps, stats, inspect, port
  slurm/      sinfo, squeue, sacct, sbatch
  desktop/    UI data samples
```

Rules:

- Use real captured output from the test endpoints, trimmed to what the test needs.
  Identifiers are anonymised before a capture is committed: user names become
  `dev`, FQDNs use `example.net`, and IP addresses come from the RFC 5737
  documentation ranges (192.0.2.x, 198.51.100.x). The shape of the output is kept.
- No secrets, tokens, keys or personal data; no hostnames beyond the test
  endpoints (`x1carbon`, `slurmctld`, `localhost`) and `example.net` names.
- Plain text or JSON, UTF-8, LF line endings. Name files after the command,
  e.g. `slurm/sacct-completed.txt`.

Loading a fixture from a test:

```python
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

def fixture_text(area: str, name: str) -> str:
    return (FIXTURES / area / name).read_text(encoding="utf-8")
```
