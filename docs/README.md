# Own Compute documentation

The [main README](../README.md) covers what the plugin does, how to install it, a quick start for each host type, the disclosures and the platform status. These pages go deeper.

The internal plugin id is `cloud-compute`. It appears in the settings path `plugins.entries.cloud-compute`, the data folder `plugin-data/cloud-compute`, labels on remote machines and image names.

| Page | Read it when |
|---|---|
| [Hosts](hosts.md) | Adding an SSH host, a Slurm login node or the local Docker or Podman, and writing templates |
| [EC2](ec2.md) | Starting an AWS machine on demand, its costs, its time limits and what the plugin can and cannot see |
| [Cloud agents](cloud-agents.md) | Running a Hermes agent in a sandbox and getting its work back as a git branch |
| [Safety](safety.md) | Checking what is saved, what asks for approval, how secrets travel and where the limits are |
| [Settings](settings.md) | Looking up every setting, its default and its range |
| [Troubleshooting](troubleshooting.md) | A host, a terminal, a screen, a job or an agent run does not work |
| [ec2-iam-policy.md](ec2-iam-policy.md) and [ec2-iam-policy.json](ec2-iam-policy.json) | Creating the least-privilege IAM policy for an EC2 host |

The Help tab of the Compute page holds the same topics in short form.
