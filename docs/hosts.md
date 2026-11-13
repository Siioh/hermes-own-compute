# Hosts and templates

A host is a machine that work can run on. The Hosts tab lists them with a status light, the last time each was seen, and CPU, RAM, GPU and disk used against available. Four types exist: SSH host, Slurm login node, Local Docker/Podman and EC2 (see [ec2.md](ec2.md)).

## Status lights

| Light | Meaning | What to do |
|---|---|---|
| Online | Connected; the runtime or scheduler works | Nothing |
| Degraded | Connected, but the container runtime or scheduler is missing or failing | Open the host and read the failed row of the checklist |
| Needs login | The machine answers but SSH needs a prompt (password, passphrase, second factor, a new host key) | Run `ssh <alias>` once in a terminal, then press Retry |
| Offline | Nothing answers on the SSH port | Check that the machine is on and on the network or VPN |
| Unknown | Not checked yet | Press **Refresh status** |

**Test connection** runs the checklist step by step: name lookup, network port, login, container runtime or scheduler, work directory (Slurm), screen support and GPUs. Nothing is saved by a test. **Refresh status** re-reads a saved host and changes nothing on it.

## SSH host

An SSH host is a Linux machine reachable with `ssh`: a spare laptop, a workstation, a homelab box or a cloud machine the user owns. Work runs in containers on it.

- **Endpoint.** An alias from the SSH configuration, or host, user and port. A jump host (`ssh -J`) is optional.
- **Login.** A key loaded in the SSH agent, or the path of a key file. When a key file is set, the ssh program offers only that key. With the setting `use_asyncssh` on, Windows may also offer the keys of the SSH agent. Passwords and second-factor prompts are not used automatically; the host shows **Needs login** until `ssh` works without a prompt. The plugin's own SSH commands use `BatchMode`, so a prompt fails fast instead of hanging; terminal windows are interactive.
- **Host keys.** The first connection to a new host adds its key to the SSH known-hosts file. A key that later changes is refused, and the host shows **Needs login** with the text "connect once with ssh in a terminal to accept the host key".
- **Remote side.** Podman (rootless works, no root access needed) or Docker. The runtime is detected on its own.
- **Limits.** Per host: maximum environments, CPU and memory caps, and whether cloud agents are allowed. They apply to environments the plugin starts.
- **Ports.** Ports an environment publishes bind `127.0.0.1` on the host. **Forward port** and **Screen** reach them through an `ssh -L` tunnel that listens only on `127.0.0.1` on this computer.

## Slurm login node

A Slurm login node is the front door of a cluster. Work runs as batch jobs, optionally inside an Apptainer image. There are no long-running sandboxes.

- **Needs.** The same endpoint and login as an SSH host. `sbatch`, `squeue`, `sacct` and `scancel` on the PATH of a login shell. Partition, account and QOS are optional and are added to every job.
- **Work directory.** Each job gets a folder `<work directory>/<job id>` with `job.sh` and the output; jobs start there. The default is `~/cloud-compute`. The work directory must be on storage that the compute nodes also see, because job output is read from the login node. Home folders are not shared on every cluster. **Test connection** writes a small file on the login node and reads it from a compute node with a one-CPU, one-minute `srun`, and the host card shows the failed check with its fix.
- **Etiquette.** A login node is shared. Probes are light and batched; Slurm is polled every 10 seconds while a job is open and otherwise at the host's interval (default 60 seconds, 30 at least); each job is tagged `--comment=cloud-compute:<id>`. Heavy work belongs in a job.
- **Jobs tab.** Give a command or a script, an optional image, partition, CPUs, memory and a time limit. An image is `docker://...` or a `.sif` path and runs through Apptainer; without an image the job runs as the user on the compute nodes. An array such as `1-10%2` runs ten tasks, two at a time. Read output, cancel, open a terminal into a running job, or wait for the notification.
- **States.** Meters show CPU and RAM allocated against the node total. Without `sacct`, a job that left the queue ends as "Ended (state unconfirmed)" with no exit code. When output is empty, the dialog says why: the job has not started, it printed nothing, or no output file exists where the login node looks.
- **Removing records.** **Remove from list** and **Clear finished** remove records on this computer only; job folders on the cluster stay. A host cannot be deleted while it has open jobs or environments.
- **Not available.** Screens, port forwards, sandboxes and cloud agents: compute nodes sit behind the login node, and most clusters do not allow long-running processes there. A second factor is not automated.
- **Advanced.** A **Command prefix**, for example `podman exec -i slurmctld`, reaches a scheduler inside a container on the login host. Leave it empty for a real cluster. It can be set on the page only.

## Local Docker or Podman

The local host runs containers on this computer. There is no network hop and nothing to log in to.

- **Needs.** Docker Desktop or Podman, installed and running, with `docker` or `podman` answering in a terminal. The plugin also looks in the usual install folders when the command is not on the PATH.
- **Gets.** Long-running environments from templates with the same host limits, terminals, screens without a tunnel (the published loopback port is used directly), port forwards, and GPUs only where the runtime's GPU access already works.
- **Degraded.** Docker or Podman is installed but not running ("Docker is installed but not running. Start Docker Desktop, then test again."), or neither is found ("No Podman or Docker found on this computer. Install Docker Desktop or Podman, then test again.").
- **Files.** Secret files for a local host are written to the plugin data folder on Windows and to `$HOME/.cache/cloud-compute` on Linux and macOS.

## Templates

A template says what to start: image, command, working folder, CPUs, memory, GPUs, network (default or none), screen on or off, time limit, idle timeout, persistent switch, the host kinds it may run on, and variables.

- **Built in:** `ubuntu-dev`, `python-dev`, `desktop-novnc`, `slurm-python`, and two cloud agent templates, **Hermes cloud agent** and **Handoff test (no model)**. Editing a built-in template saves a version of your own; **Reset to default** restores the original. A built-in template cannot be deleted. **+ New template** needs only a name and an image; **Duplicate** copies one.
- **Lifetime.** A blank time limit or idle timeout takes `default_ttl_minutes` and `default_idle_minutes` from the settings; a value in the template wins. Only a persistent template has no expiry.
- **Variables.** Each has a name, a description, a source and a scope. The source is a literal value or `secret:NAME`, looked up in the Hermes secrets only when the environment starts. The scope is `runtime`, `redacted` (the default) or `agent-visible`; secret values and `redacted` literals are hidden in output and logs. A variable can be limited to certain hosts.
- **Published ports.** A template lists container ports to publish on `127.0.0.1` of the host. The environment card then shows **Forward** and **Open in browser** for each. A port the template does not publish is refused.
- **Screen.** The display switch needs the `desktop-novnc` image. A template with the switch on and another image is refused with "Show a screen needs the Desktop (noVNC) image". The image is built on the host the first time a screen is used (about 1 GB, built from a Debian base with `apt-get`).
- **What will be sent.** Before an environment starts, the preview shows image, command, limits, labels, published ports and the names and scopes of variables, with a reason for any that will not be sent. If a secret will be sent, **Spawn** stays off until **Send these secrets into the environment** is ticked.
