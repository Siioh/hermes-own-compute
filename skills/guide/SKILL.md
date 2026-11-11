---
name: guide
description: Use when running work on the user's machines: sandboxes, Slurm jobs, cloud agents.
---

# Own Compute guide

Own Compute runs work on machines the user owns: SSH hosts, Slurm clusters, this computer. Work runs in a
sandbox container, a Slurm batch job, or a cloud agent (a Hermes agent in a sandbox on the user's
machine, not a hosted service). This guide lists every tool, every action and every
parameter with the exact name the tool accepts. Use only these names.

Tools: `compute_status`, `compute_host`, `env_spawn`, `env_exec`, `env_control`, `env_files`, `env_open`, `slurm_submit`, `slurm_jobs`, `cloud_agent_spawn`, `cloud_agent_status`, `cloud_agent_message`, `cloud_agent_rerun`, `cloud_agent_collect`, `cloud_agent_stop`.

## Rules

- Workloads run only inside sandboxes, Slurm jobs or cloud agent sandboxes. No tool runs a command on a host shell.
- Use the exact parameter names in the tables. A name that is not there is rejected with `unknown_parameter`.
- Ids come from tool results. Never guess a host, environment, job, run, model or saved-copy id.
- When Hermes approvals are on, approval cards are the user's decision. If the user declines, ask what they want instead; never retry with other arguments to get past the card. If `compute_status` returns `approvals_warning`, Hermes approvals are off: no card is shown, so tell the user before you start, change or delete anything.
- Never put a secret in a command, a message, a file or a reply. Variables reach sandboxes by name; confirm one is set, never print it.
- Put results in the environment's work folder (`/work`); only that folder is saved when it is deleted.
- Say what a tool returned, in plain words: sizes, paths, counts, the `message`, the `reply`. Paste a `card` line on its own line, with an empty line before and after (never inside a sentence, or it shows as plain text); a result with a `card` also carries `card_hint` with the same instruction.
- Do not poll. Use `wait` (`slurm_jobs`) or `wait_s` (`cloud_agent_status`) once, then act.

## Which tool

| The user wants | Call |
|---|---|
| to see what exists, or to find an id | `compute_status` |
| a machine added, changed or removed | `compute_host` |
| a sandbox to run commands in | `env_spawn`, then `env_exec` |
| to stop, extend, read logs of or delete a sandbox | `env_control` |
| files copied to or from a sandbox, or saved copies | `env_files` |
| a terminal, a screen or a port on their own computer | `env_open` |
| a batch job on a cluster | `slurm_submit`, then `slurm_jobs` |
| a coding task done on a git branch | `cloud_agent_spawn`, then `cloud_agent_status`, `cloud_agent_collect` |

## The usual order of calls

### Set up a machine (the user wants you to do it)

```
compute_status()                      # configured false? then follow next_steps
compute_host(action="types")          # which fields each host kind needs
compute_host(action="test", kind="ssh", address="gpu-box")   # read every row; fix, then test again
compute_host(action="add", kind="ssh", name="GPU box", address="gpu-box")   # the user approves
compute_status()                      # the new host and the template ids
```

### Work in a sandbox

```
compute_status()                                           # host ids, template ids
env_spawn(template="python-dev", host_id="gpu-box")       # approval; returns instance.id
env_exec(instance_id="i-1a2b3c4d", command="python -V")    # as often as needed
env_files(action="put", instance_id="i-1a2b3c4d", local_path="C:/data/in.csv", dest="/work")
env_files(action="size", instance_id="i-1a2b3c4d", path="/work")
env_files(action="fetch", instance_id="i-1a2b3c4d", path="/work")   # results to the user's computer
env_control(instance_id="i-1a2b3c4d", action="destroy")    # saves /work first, then deletes
```

### Run a Slurm job

```
compute_status()                                                  # a host with kind slurm
slurm_submit(host_id="cluster", name="hello", command="echo hi")  # approval; returns job.id
slurm_jobs(action="wait", job_id="j-1a2b3c4d")                    # blocks, then gives the output
slurm_jobs(action="output", job_id="j-1a2b3c4d", stream="err")    # if something failed
slurm_jobs(action="delete", job_id="j-1a2b3c4d")                  # tidy the list when done
```

### Run a cloud agent

```
compute_status()                                    # a host with allow_agents true
cloud_agent_status(models=true)                     # saved models (id m-...)
cloud_agent_spawn(task="...", host_id="gpu-box", repo_path="C:/Users/me/code/project")   # approval
cloud_agent_status(run_id="a-1a2b3c4d", wait_s=300) # blocks until the turn ends; read reply
cloud_agent_collect(run_id="a-1a2b3c4d")            # the branch comes back to the user's repository
cloud_agent_message(run_id="a-1a2b3c4d", message="...", wait_s=300)   # a follow-up, then collect again
cloud_agent_rerun(run_id="a-1a2b3c4d", model_id="m-6d85700b")   # after a failure: a new run with another model
```

## Tools

### compute_status

Always the first call. Lists hosts, environments, running Slurm jobs and the template ids.

Approval: No.

| Parameter | Type | Required | Meaning |
|---|---|---|---|
| `host_id` | string | no | Only this host (id from this tool's output); omit for all. |
| `refresh` | boolean | no | Probe the host(s) now before answering. Default: `false`. |
| `details` | boolean | no | Also list each template's variables (name, secret, set). Default: `false`. |

Returns: `configured` (false = no host yet; then `next_steps` says what to do), `hosts` (each with `id`, `name`, `kind`, `status`, `allow_agents`, `capabilities`, `resources`, `card`), `instances` (environments, each with its `id`), `jobs` (running or pending), `templates` (each with `id`, `kind` environment or agent, `host_kinds`, `screen`) and `warning` when something could not be read.

```
compute_status()
compute_status(host_id="gpu-box")
compute_status(details=true)    # adds each template's variable names and whether they are set
```

- Host ids (`gpu-box`), environment ids (`i-1a2b3c4d`), job ids (`j-1a2b3c4d`) and template ids (`python-dev`) all come from this tool. Never invent one.
- Status values: `online` ready; `degraded` reachable but part is broken (read `capabilities`); `auth_required` the user must log in once; `offline` unreachable.

### compute_host

Set up or change the machines. Use it when `compute_status` says `configured: false`, or when the user asks to add, change or remove a host.

Approval: `add`, `update` and `remove` show the user an approval card, and so does a `test` of an address that is not saved (any of `address`, `user`, `port`, `jump_host`, `identity_file` given). Every card lists each value that will be used. `types`, `list` and a `test` of a saved host (`host_id` alone) do not ask. `start`, `stop`, `terminate` and `extend` of an EC2 machine always ask. This applies when Hermes approvals are on.

Page-only settings: the command prefixes (`exec_prefix`, `interactive_prefix`) and, for an EC2 host, the image (also a custom AMI), subnet, security group and SSH user can only be changed on the Compute page. This tool refuses them with "This setting can only be changed on the Compute page." A host the user already set up that way can still be tested and used.

EC2 hosts and money: an EC2 host is an AWS account, a region and launch settings. `add` with `kind="ec2"` starts nothing and costs nothing yet; `start` launches a machine and AWS bills the user's account while it runs (and for its disk while it exists). Every EC2 card names the region, instance type, disk, max lifetime, idle rule, who may use SSH and whether billing starts now, and says that Own Compute cannot see AWS prices: read the card's cost lines to the user and never say anything is free. Results carry `cost_state`: say it. A machine that is stopped or not launched is started with `start` (the user approves a card) or the Start machine button on the Hosts tab of the Compute page, never in the AWS console or with the AWS command line. Never ask for or accept an AWS key in chat: the key is stored in Hermes secrets (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`); `access_key_secret` and `secret_key_secret` are only the NAMES of those secrets. A request that cannot work (a missing secret, a bad region, a launch-only setting while a machine exists) is refused with a plain sentence before any card.

| Parameter | Type | Required | Meaning |
|---|---|---|---|
| `action` | one of `types`, `list`, `test`, `add`, `update`, `remove`, `start`, `stop`, `terminate`, `extend` | yes | What to do. |
| `host_id` | string | by action | Host id (update, remove; test of a saved host, which needs no approval). |
| `kind` | string | by action | ssh, slurm, local or ec2 (see types). |
| `name` | string | by action | Display name; defaults to the address. |
| `address` | string | by action | SSH alias, host name or IP (not for local). |
| `user` | string | by action | SSH user if the alias does not set it. |
| `port` | integer | by action | SSH port, default 22. |
| `identity_file` | string | by action | Path of a key file on this computer. |
| `jump_host` | string | by action | Host to connect through. |
| `workdir` | string | by action | Slurm: working folder on the shared file system. |
| `partition` | string | by action | Slurm: default partition. |
| `account` | string | by action | Slurm account. |
| `qos` | string | by action | Slurm QOS. |
| `allow_agents` | boolean | by action | Allow cloud agents on this host. |
| `max_instances` | integer | by action | Most environments at once. |
| `region` | string | by action | EC2: AWS region (default `us-east-1`). |
| `instance_type` | string | by action | EC2: instance type (default t3.micro). |
| `disk_gb` | integer | by action | EC2: root disk GB, 8-500 (default 10). |
| `max_lifetime_minutes` | integer | by action | EC2: machine stops itself after this long, 15-1440 (default 120). |
| `idle_stop_minutes` | integer | by action | EC2: stop after this many idle minutes, 0 = never (default 30). |
| `auto_start` | boolean | by action | EC2: start the machine when an environment needs it (default false). |
| `ssh_cidr` | string | by action | EC2: address or range allowed to use SSH (default: this computer). |
| `access_key_secret` | string | by action | EC2: NAME of the Hermes secret with the access key id. Never the key. |
| `secret_key_secret` | string | by action | EC2: NAME of the Hermes secret with the secret key. Never the key. |
| `minutes` | integer | by action | extend: minutes to add to an EC2 machine's time limit. |
| `delete_environments` | boolean | by action | terminate: save and delete every environment first. |
| `save` | boolean | by action | terminate: save files before deleting (default true). |

| Action (`action`) | Needs | Approval | What it does |
|---|---|---|---|
| `types` | - | no | Host kinds (`ssh`, `slurm`, `ec2`, `local`), their fields and examples. Call it once to learn what to ask the user. |
| `list` | - | no | Every host with status and counts. |
| `test` | `kind`, `address` (or `host_id` of a saved host) | only for an address that is not saved | A checklist. Read every row; a failing row says how to fix it. Saves nothing. A saved host tested by `host_id` alone runs at once; a new address shows the user a card with every value first. For an EC2 host it makes read-only calls and a DryRun launch and starts nothing. |
| `add` | `kind`, `address`; for a Slurm host also `workdir` on storage the compute nodes share; for an EC2 host `kind="ec2"` and optionally `name`, `region`, `instance_type`, `disk_gb`, `max_lifetime_minutes`, `idle_stop_minutes`, `auto_start`, `ssh_cidr` | yes | Tests, then saves. The host appears in `compute_status`. An EC2 host is saved with no machine; `cost_state` says so. |
| `update` | `host_id` plus the fields to change | yes | The changed host. For EC2 the card shows each change as old -> new and a line when the change makes the machine more costly or longer-lived. |
| `remove` | `host_id` | yes | Removes the host from Own Compute (refused while environments run on it, or while an EC2 machine exists: terminate it first). |
| `start` | `host_id` of an EC2 host | yes | Launches (or starts again) the host's AWS machine; ready after a few minutes, then use `env_spawn`. AWS bills the account while it runs, and for its disk while it exists. |
| `stop` | `host_id` of an EC2 host | yes | Stops the machine; containers and files stay on its disk (the disk is still billed until Terminate). |
| `terminate` | `host_id` of an EC2 host; `delete_environments`, `save` | yes | Deletes the machine and its disk. Refused while environments exist unless `delete_environments=true`, which saves each one's files first. |
| `extend` | `host_id` of an EC2 host, `minutes` | yes | Moves the machine's own stop time later, at most to twice the host's max lifetime from the last start. The card shows the old and the new stop time. |

Returns: `types`: the host kinds with the fields each needs. `list`: hosts with status and counts. `test`: one row per check (DNS, port, login, runtime or scheduler, work directory), each failing row with a fix; nothing is saved. `add`: tests first and saves only if the test passes; the result has the host and a `card`.

```
compute_host(action="types")
compute_host(action="test", kind="ssh", address="gpu-box")
compute_host(action="add", kind="ssh", name="GPU box", address="gpu-box")
compute_host(action="add", kind="slurm", name="Cluster", address="login.example.edu", workdir="/shared/cloud-compute")
compute_host(action="add", kind="local", name="This computer")
compute_host(action="add", kind="ec2", name="AWS small", region="us-east-1", instance_type="t3.micro", disk_gb=10, max_lifetime_minutes=120)
compute_host(action="update", host_id="gpu-box", allow_agents=true)
compute_host(action="start", host_id="aws-box")
compute_host(action="terminate", host_id="aws-box", delete_environments=true)
```

- An EC2 host needs the AWS key in the Hermes secrets first. If `add` says a secret is not set, tell the user which name to add in Hermes secrets; never take the key in chat. After `add`, nothing runs: ask the user before `start`, and tell them what the card says it costs. The image, subnet, security group and SSH user are set on the Compute page.

- Ask the user for the SSH alias or address and the user name. Never ask for or send a password or key text in chat; `identity_file` is only the path of a key file.
- If a `test` row fails, tell the user the fix it gives instead of retrying. If the user would rather set it up by hand, point them to Compute in the Hermes sidebar, Add host.

### env_spawn

Start a sandbox container for interactive work.

Approval: Yes (card names the template, host, limits and time limit). This applies when Hermes approvals are on.

| Parameter | Type | Required | Meaning |
|---|---|---|---|
| `template` | string | yes | Template id, e.g. python-dev (not an agent template). |
| `host_id` | string | yes | Host id from compute_status (an online ssh or local host). |
| `name` | string | no | Optional display name. |
| `ttl_minutes` | integer | no | Time limit: stop the environment after this many minutes. Its files are kept. |
| `idle_minutes` | integer | no | Stop the environment after this many idle minutes. |

Returns: `instance` (its `id` is the `instance_id` for every other environment tool), `preview` (image, limits, variable names, never values), `warnings` (read them to the user) and `card`.

```
env_spawn(template="python-dev", host_id="gpu-box")
env_spawn(template="python-dev", host_id="gpu-box", name="scrape-test", ttl_minutes=120, idle_minutes=30)
```

- `template` must be an environment template id from `compute_status` (`kind: environment`). Agent templates belong to `cloud_agent_spawn`. A wrong id is refused at once with `not_found` and the valid ids.
- Set `ttl_minutes` and `idle_minutes` on purpose; the defaults are generous and hold the host's resources. When a limit is reached the environment is stopped, not deleted, and its files are kept.

### env_exec

Run a shell command inside a running environment.

Approval: No. Refused on the sandbox of a cloud agent run: "This environment belongs to a cloud agent run and holds its model key. Use cloud_agent_message to give it work, or open a terminal from the Agents tab."

| Parameter | Type | Required | Meaning |
|---|---|---|---|
| `instance_id` | string | yes | Instance id from env_spawn. |
| `command` | string | yes | Shell command, run with sh -lc. |
| `timeout_s` | integer | no | Give up after this many seconds. Default: `120`. |
| `workdir` | string | no | Working directory inside the environment (absolute path). |

Returns: `rc` (exit code), `stdout`, `stderr` (the last 20 KB of each, secrets removed), `timed_out`, `card`.

```
env_exec(instance_id="i-1a2b3c4d", command="pytest -q")
env_exec(instance_id="i-1a2b3c4d", command="python train.py | tail -50", workdir="/work", timeout_s=300)
```

- Each call is a fresh shell: variables you set do not carry over, files do. Put everything one step needs in one command (`cd x && make`).
- Slim images may have no `curl` or `ps`; use Python or `/proc`. Anything over ten minutes belongs in a Slurm job.

### env_control

Stop, start, extend, read the logs of, or delete one environment.

Approval: `destroy` and `extend` ask the user. A `start` asks only when the time limit had been reached. `stop` and `logs` do not. This applies when Hermes approvals are on.

| Parameter | Type | Required | Meaning |
|---|---|---|---|
| `instance_id` | string | yes | Instance id from env_spawn. |
| `action` | one of `stop`, `start`, `destroy`, `extend`, `logs` | yes |  |
| `tail` | integer | by action | Log lines to return (action logs only). Default: `200`. |
| `minutes` | integer | by action | Minutes to add to the time limit (action extend only). |
| `save` | boolean | by action | Save the work folder to this computer before deleting (action destroy only). false deletes ALL files without saving. Default: `true`. |

| Action (`action`) | Needs | Approval | What it does |
|---|---|---|---|
| `stop` | `instance_id` | no | Stops the container; its files are kept. |
| `start` | `instance_id` | restart after the time limit only | Starts it again. |
| `destroy` | `instance_id`; optional `save` | yes | Saves the work folder the environment was created with to the user's computer first, then deletes. If saving fails, or the folder cannot be told (not recorded and the template is gone), nothing is deleted and the answer says how to save first. |
| `extend` | `instance_id`, `minutes` | yes | Adds minutes (1-10080) to the time limit. |
| `logs` | `instance_id`; optional `tail` | no | The container's own output. |

Returns: The environment record. `logs`: `text`, and a `note` when the container printed nothing (normal for `sleep infinity`). `destroy`: `saved` (`export_id`, `path`, `bytes`, `files`, or null when there was nothing to save), `message` (say it to the user as it is) and `copy_card`.

```
env_control(instance_id="i-1a2b3c4d", action="logs", tail=100)
env_control(instance_id="i-1a2b3c4d", action="extend", minutes=60)
env_control(instance_id="i-1a2b3c4d", action="destroy")
```

- `save=false` deletes everything with no copy. Use it only after the user said so in this conversation; the default saves first.
- Only the work folder (usually `/work`) is saved. Tell the user to keep results there.

### env_files

Move files between an environment and the user's computer, and manage saved copies.

Approval: `put`, `restore` and `delete_export` ask the user. `fetch` asks when `dest_dir` is given, when `extract` is true, or when the folder is over 200 MB. `list`, `size` and `exports` do not. A "session" or "always" answer covers only that file, copy and destination. On a cloud agent's sandbox, `list` and `fetch` are limited to its work folder. This applies when Hermes approvals are on.

| Parameter | Type | Required | Meaning |
|---|---|---|---|
| `action` | one of `list`, `size`, `fetch`, `put`, `exports`, `restore`, `delete_export` | yes |  |
| `instance_id` | string | by action |  |
| `path` | string | by action | Folder in the environment; default workdir. |
| `local_path` | string | by action | Absolute path on this computer (put). |
| `dest` | string | by action | Folder in the environment (put, restore). |
| `dest_dir` | string | by action | Existing folder on this computer for the copy (fetch). |
| `extract` | boolean | by action | put: unpack a .tar/.tar.gz. fetch: also unpack the copy into a new folder. |
| `export_id` | string | by action |  |

| Action (`action`) | Needs | Approval | What it does |
|---|---|---|---|
| `list` | `instance_id`; optional `path` | no | Entries of one folder (the environment must be running). |
| `size` | `instance_id`; optional `path` | no | Size and file count. Call it before `fetch` on anything that may be big. |
| `fetch` | `instance_id`; optional `path`, `dest_dir`, `extract` | sometimes | Saves a `.tar.gz` copy on the user's computer; works on stopped environments too. |
| `put` | `instance_id`, `local_path`; optional `dest`, `extract` | yes | Uploads a file or folder from the user's computer. Private folders are refused: the SSH folder and the whole Hermes folder (every profile, its `.env` and `auth.json`); so are network and device paths (those starting with two backslashes). |
| `exports` | optional `instance_id` | no | Saved copies, newest first. |
| `restore` | `export_id`, `instance_id`; optional `dest` | yes | Unpacks a saved copy into an environment. Same-named files are replaced. |
| `delete_export` | `export_id` | yes | Deletes the saved archive. An unpacked folder is never removed. |

Returns: `list`: `entries`. `size`: `path`, `known`, `bytes`, `files`, `reason` and a `message` (`known` is false when it could not be measured; say so). `fetch`: `export` (the saved copy: `id`, `local_path`, `bytes`, `files`, `extracted_to`), a `message` to relay and a `card`. `exports`: the saved copies and the exports `folder`. `restore`: where it went and a `card`. `put`: where it went.

```
env_files(action="size", instance_id="i-1a2b3c4d", path="/work")
env_files(action="fetch", instance_id="i-1a2b3c4d", path="/work/results")
env_files(action="fetch", instance_id="i-1a2b3c4d", dest_dir="C:/Users/me/project/saved", extract=true)
env_files(action="put", instance_id="i-1a2b3c4d", local_path="C:/data/in.csv", dest="/work")
env_files(action="exports", instance_id="i-1a2b3c4d")
env_files(action="restore", export_id="x-1a2b3c4d", instance_id="i-1a2b3c4d", dest="/work")
```

- A folder with no files gives `nothing_to_save`: that is information, not a failure. Tell the user there was nothing to save.
- A saved copy holds one folder (`path`, default the work folder). Copies of agent sandboxes leave out the agent's own working folders; the result says so.

### env_open

Open something on the user's computer: a terminal window, the sandbox screen in the browser, or a forwarded port.

Approval: `port` with an ssh `host_id` asks the user (the card names the host and the port). Everything else does not. A terminal is refused on a cloud agent's sandbox. This applies when Hermes approvals are on.

| Parameter | Type | Required | Meaning |
|---|---|---|---|
| `instance_id` | string | by action | Environment id (from compute_status). |
| `host_id` | string | by action | Host id (from compute_status): a Slurm host for a terminal, an ssh host for a port forward. |
| `job_id` | string | by action | Running Slurm job id (from compute_status). |
| `kind` | one of `terminal`, `display`, `port` | yes | What to open. |
| `remote_port` | integer | by action | Port INSIDE the environment to forward (kind=port). Must be published by the template. |

| Action (`kind`) | Needs | Approval | What it does |
|---|---|---|---|
| `terminal` | `instance_id` (or `host_id` of a Slurm host, or `job_id` of a running job) | no | Opens a terminal window on the user's computer. |
| `display` | `instance_id` of an environment with a screen | no | Opens the screen in the browser. A template without a screen server answers `needs_setup`: use the Desktop (noVNC) template. |
| `port` | `instance_id` (or ssh `host_id`), `remote_port` | only with `host_id` | With `instance_id`: forwards a published port to the user's computer. `remote_port` is the port INSIDE the environment (for example 8000), and the template must publish it. With `host_id`: forwards any loopback port of that ssh host (for example a database or a Jupyter server on the host itself); the user is asked first. |

Returns: A window or `url` result and a `card`. You cannot type into or see what opened; use `env_exec` for output.

```
env_open(kind="terminal", instance_id="i-1a2b3c4d")
env_open(kind="display", instance_id="i-1a2b3c4d")
env_open(kind="port", instance_id="i-1a2b3c4d", remote_port=8000)
```

### slurm_submit

Run a batch job on a Slurm cluster. Heavy work never goes on the login node.

Approval: Yes. The card shows the command or script (the first 1500 characters) and the resources; the answer covers only that command and those resources. This applies when Hermes approvals are on.

| Parameter | Type | Required | Meaning |
|---|---|---|---|
| `host_id` | string | yes | Slurm host id from compute_status. |
| `name` | string | yes | Short job name. |
| `command` | string | no | Shell command to run (or give script). |
| `script` | string | no | Batch script body (or give command). |
| `image` | string | no | Apptainer image: docker://... or a .sif path. Omit for a bare job. |
| `partition` | string | no | Partition; omit for the host default. |
| `cpus` | integer | no | CPUs per task. Default: `1`. |
| `mem_gb` | number | no | Memory in GB. Default: `1`. |
| `gpus` | integer | no | GPUs. Default: `0`. |
| `time_limit` | string | no | Wall time limit, HH:MM:SS or D-HH:MM:SS. Default: `"00:30:00"`. |
| `array` | string | no | Array spec such as 1-10%2. |

Returns: `job` (state PENDING, `id` like `j-1a2b3c4d`, `job_dir` its folder inside the host's work directory) and a `card` line to paste into the reply.

```
slurm_submit(host_id="cluster", name="hello", command="echo hello from $(hostname)")
slurm_submit(host_id="cluster", name="sweep", command="python run.py --seed $SLURM_ARRAY_TASK_ID", image="docker://python:3.12-slim", partition="cpu", cpus=4, mem_gb=8, time_limit="01:00:00", array="1-10%2")
```

- Give exactly one of `command` or `script`. `host_id` must be a host with `kind: slurm`.
- Set `time_limit`, `cpus` and `mem_gb` for anything real: the defaults are tiny (30 minutes, 1 CPU, 1 GB).
- Output goes to files in `job_dir`, which must be on storage the compute nodes share (the host's Work directory). Read it with `slurm_jobs`.

### slurm_jobs

List, inspect, wait for, read the output of, cancel or remove Slurm jobs.

Approval: `cancel` asks the user. Everything else does not. This applies when Hermes approvals are on.

| Parameter | Type | Required | Meaning |
|---|---|---|---|
| `action` | one of `list`, `status`, `output`, `wait`, `cancel`, `delete` | by action | list, status, output, wait, cancel or delete. Default: `"list"`. |
| `host_id` | string | by action | Slurm host id from compute_status (filters list). |
| `job_id` | string | by action | Job id such as j-1a2b3c4d (every action except list). |
| `tail` | integer | by action | Output lines to return (output, wait). Default: `200`. |
| `stream` | one of `out`, `err` | by action | out = stdout, err = stderr (output, wait). Default: `"out"`. |
| `task` | integer | by action | Array task number: read only that task's file (output, wait). |
| `wait_s` | integer | by action | Longest time to block for action=wait, in seconds. Default: `120`. |

| Action (`action`) | Needs | Approval | What it does |
|---|---|---|---|
| `list` | optional `host_id` | no | The default action. |
| `status` | `job_id` | no | One job. |
| `output` | `job_id`; optional `stream`, `tail`, `task` | no | The last lines of the job's output file. Check `status` and read `note` when `text` is empty. |
| `wait` | `job_id`; optional `wait_s`, `stream`, `tail`, `task` | no | Blocks until the job ends or time runs out, then returns the output. Use it instead of polling `status`. |
| `cancel` | `job_id` | yes | Stops the job. |
| `delete` | `job_id` | no | Removes a FINISHED job from the list on this computer. Its folder on the cluster stays. |

Returns: `list`: `jobs`, newest first. `status`: `job` (state PENDING, RUNNING, COMPLETED, FAILED, CANCELLED, TIMEOUT, OUT_OF_MEMORY, NODE_FAIL or UNKNOWN). `output`: `text`, `status` and `note`. `wait`: `job`, `finished`, `waited_s`, `output`. Every result about one job carries a `card`.

```
slurm_jobs()
slurm_jobs(action="wait", job_id="j-1a2b3c4d", wait_s=120)
slurm_jobs(action="output", job_id="j-1a2b3c4d", stream="err")
slurm_jobs(action="cancel", job_id="j-1a2b3c4d")
slurm_jobs(action="delete", job_id="j-1a2b3c4d")
```

- `output` `status` is `ok`, `empty` (the job printed nothing), `not_started` (it has not run yet) or `missing` (no output file where the login node looks). For `missing` the `note` names the folder and the fix: the host's Work directory must be on shared storage; change it with `compute_host` `update`.
- Do not poll in a loop. One `wait`, then act on the answer.

### cloud_agent_spawn

Hand a coding task to a Hermes agent that works on a git branch inside a sandbox and commits there.

Approval: Yes. The card names the template, host, model, endpoint host, turn limit, key secret name, the full repository path, and the task and criteria (the first 1500 characters each). The answer covers only that template, model, turn limit, task and repository. This applies when Hermes approvals are on.

| Parameter | Type | Required | Meaning |
|---|---|---|---|
| `task` | string | yes | What the agent should do (preferred name). |
| `goal` | string | no | Older name for task. |
| `criteria` | string | no | Optional: when the work counts as done (added as 'Done when: ...'). |
| `host_id` | string | yes | Host id from compute_status; the host must allow agents. |
| `repo_path` | string | yes | Path of a git repository on this computer. |
| `model_id` | string | no | Saved model id (m-...); omit for the default model. |
| `template` | string | no | hermes-agent (a model works the task) or agent-script (handoff test: commits a marker file, no model). Default: `"hermes-agent"`. |
| `branch` | string | no | Branch the agent works on; default: named after the task. |
| `new_branch` | boolean | no | Create the branch from base; false continues an existing branch. Default: `true`. |
| `base` | string | no | Revision a new branch starts from. Default: `"HEAD"`. |
| `max_turns` | integer | no | Turn limit for the agent (1-200). Default: `30`. |
| `wait_s` | integer | no | Seconds to block until the run finishes (capped by the wait_max_seconds setting, at most 360). 0 or absent: return at once. |

Returns: The `run` (its `id` is the `run_id` of the other agent tools), a `card`, and with `wait_s` the finished state, `reply` and `next`.

```
cloud_agent_spawn(task="Describe this repository in NOTES.md and commit it", host_id="gpu-box", repo_path="C:/Users/me/code/project", wait_s=300)
cloud_agent_spawn(task="Fix the failing test", criteria="pytest passes", host_id="gpu-box", repo_path="C:/Users/me/code/project", model_id="m-6d85700b")
```

- `task`, `host_id` and `repo_path` are required. `host_id` must allow cloud agents (`allow_agents` in `compute_status`; turn it on with `compute_host` `update`). Not for Slurm hosts.
- `template` is `hermes-agent` (a model works the task; the default) or `agent-script` (a handoff test that commits a marker file and uses no model).
- The model is the default saved choice unless `model_id` is given. List them with `cloud_agent_status(models=true)`. If none is set up, tell the user to add one in Compute, Agents, New agent run, Model.

### cloud_agent_status

See a run, list runs, wait for a run to finish, or list the saved models.

Approval: No.

| Parameter | Type | Required | Meaning |
|---|---|---|---|
| `run_id` | string | no | A run id such as a-1234abcd. |
| `wait_s` | integer | no | Seconds to block until the run finishes (capped by the wait_max_seconds setting, at most 360). 0 or absent: return at once. |
| `models` | boolean | no | Also list saved models (id, name, model, endpoint host, key set, default). Default: `false`. |

Returns: Without `run_id`: `runs`. With `run_id`: `run`, `status` (starting, running, done, failed, collected, stopped), `reply` (the agent's answer), `commits_ahead`, `card` and `next` (what to do now). With `wait_s` also `finished` and `waited_s`. A failed run has `error_text`.

```
cloud_agent_status()
cloud_agent_status(run_id="a-1a2b3c4d", wait_s=300)
cloud_agent_status(models=true)
```

- `wait_s` needs a `run_id`. It blocks until the run ends or `wait_s` passes (at most the wait_max_seconds setting, 360 s); if it timed out, call it again.
- Read `reply` and relay it to the user: it is the agent's answer. `done` with `commits_ahead` above 0 means there is a branch to collect.

### cloud_agent_message

Send a follow-up to a run whose turn has ended (status done, failed or collected) and whose sandbox still exists.

Approval: Yes. The card shows the message (the first 1500 characters). This applies when Hermes approvals are on.

| Parameter | Type | Required | Meaning |
|---|---|---|---|
| `run_id` | string | yes | A run id from cloud_agent_status. |
| `message` | string | yes | The follow-up instruction. |
| `wait_s` | integer | no | Seconds to block until the run finishes (capped by the wait_max_seconds setting, at most 360). 0 or absent: return at once. |

Returns: The run, now running again; with `wait_s` the new `reply`. Collect again afterwards. The turn limit stays the one the run started with; to change it, use `cloud_agent_rerun` with `max_turns`.

```
cloud_agent_message(run_id="a-1a2b3c4d", message="Also add a test for the empty input", wait_s=300)
```

- A run that is still working answers `conflict`: wait for it first. A stopped run has no sandbox; start a new run.

### cloud_agent_rerun

Start a NEW run from a finished one (failed, done, stopped or collected) with the same task, repository and branch. Use it after a failure, with a fixed model or another host.

Approval: Yes. The card is the one of a new run: template, host, model, endpoint host, turn limit, repository path, task and criteria. This applies when Hermes approvals are on.

| Parameter | Type | Required | Meaning |
|---|---|---|---|
| `run_id` | string | yes | The finished run to start again. |
| `task` | string | no | A changed task; omit to repeat the old one. |
| `model_id` | string | no | Saved model id (m-...) to use instead of the old model, or `default` for the current default model (not the old run's model). |
| `model` | string | no | Another model id at the same endpoint (or at the saved endpoint named by base_url). |
| `base_url` | string | no | Endpoint of a saved model; keys are only sent to saved endpoints. |
| `host_id` | string | no | Another host, for example when the old host was full. |
| `max_turns` | integer | no | Turn limit (1-200). |
| `wait_s` | integer | no | Seconds to block until the run finishes (capped by the wait_max_seconds setting, at most 360). 0 or absent: return at once. |

Returns: The new run (new `id`), `rerun_of` (the old run id), a `card`, and with `wait_s` the finished state and `reply`. The old run is kept. A `note` says when the branch name had to change.

```
cloud_agent_rerun(run_id="a-1a2b3c4d", model_id="m-6d85700b", wait_s=300)
cloud_agent_rerun(run_id="a-1a2b3c4d", host_id="gpu-box")
```

- After a model failure (wrong model id, bad key, endpoint down) a `cloud_agent_message` to the same run would fail the same way. Rerun with another `model_id` (list them with `cloud_agent_status(models=true)`) or another `model`.

### cloud_agent_collect

Bring the agent's branch back to the user's repository.

Approval: No.

| Parameter | Type | Required | Meaning |
|---|---|---|---|
| `run_id` | string | yes | A run id from cloud_agent_status. |
| `into` | one of `worktree`, `branch` | no | worktree: also check the branch out in a new worktree; branch: ref only. Default: `"worktree"`. |

Returns: `ref` (branch `cloud/<run>/<branch>`), `worktree_path`, `commits_ahead`, `diverged`, and a `next` line. Nothing is merged and the user's branches do not move. It can be called again after a follow-up.

```
cloud_agent_collect(run_id="a-1a2b3c4d")
cloud_agent_collect(run_id="a-1a2b3c4d", into="branch")
```

### cloud_agent_stop

End a run early or clean up a finished one. Commits the agent made that were not fetched yet are fetched to the user's computer first (like `cloud_agent_collect`); then the sandbox is deleted after its work folder is saved to the user's computer. No follow-up is possible afterwards. If fetching fails, nothing is deleted.

Approval: Yes. This applies when Hermes approvals are on.

| Parameter | Type | Required | Meaning |
|---|---|---|---|
| `run_id` | string | yes | A run id from cloud_agent_status. |
| `keep_branch` | boolean | no | Keep the agent's work so it can still be collected. Default: `true`. |
| `delete_anyway` | boolean | no | Delete the sandbox even when the agent's newest commits could not be fetched (they are then only in the saved copy of its files). Only after the user agreed. Default: `false`. |

Returns: The stopped run, a `card`, and a `note` that says what was fetched ("Fetched 1 new commit to branch ... before stopping.") or, after `delete_anyway`, where the unfetched commits are (the saved copy path). If the commits could not be fetched the answer is the error `not_fetched`; nothing was deleted.

```
cloud_agent_stop(run_id="a-1a2b3c4d")
```

- `keep_branch=false` also deletes the branches already collected, and fetches nothing. Use it only when the user asks.
- After the error `not_fetched`, tell the user why. Try `cloud_agent_collect` again, or call `cloud_agent_stop` again with `delete_anyway=true` only if the user agrees; the approval card names `delete_anyway`.

## Errors and what to do

Errors come back as `{"error": {"code": ..., "message": ...}}`. Read the message first: it names the fix.

| Code or situation | What to do |
|---|---|
| `unknown_parameter` | A name you sent is not in the table. The message lists the valid parameters. Fix the name and call again; do not search the plugin source for names. |
| `not_found` | An id is wrong or the thing was deleted. The message lists the valid ids, or says to call `compute_status`. Use one of those. |
| `invalid` | A value is missing or the wrong type. The message says which. |
| `conflict` | The state does not allow it, for example `cloud_agent_message` on a run that is still working, or `slurm_jobs` `delete` on a job that has not finished. Wait, stop it, or cancel it first. |
| `not_running` | The environment is stopped. `env_control` `start` it first. |
| `nothing_to_save` | The folder holds no files (hidden files count). Nothing was saved and nothing is wrong. Tell the user. |
| `not_saved` | `destroy` could not save the files, so nothing was deleted. Report the message. Never retry with `save=false` unless the user says to. |
| `too_large` | The copy is over the size limit. Save a smaller folder. |
| `needs_setup` | Something must be set up first, for example `env_open` `display` on a template without a screen server (use the Desktop (noVNC) template) or a cloud agent with no model (the user adds one in Compute, Agents). |
| `unavailable` | A part is not installed, not enabled, or still starting (a screen that is still booting). The message says which; try again later only if it says so. |
| `host_offline` / `auth_required` | The machine cannot be reached or needs the user to log in once (`ssh <alias>` in a terminal). Tell the user; retrying does not help. |
| `limit_reached` | The host is full (maximum environments) or does not allow agents. Stop something or change the host with `compute_host` `update`. |
| `slurm_jobs` `output` empty with `status` `missing` | No output file where the login node looks: the Work directory is not shared with the compute nodes. Use the `note`; fix with `compute_host` `update` (`workdir`). |
| A cloud agent `failed` | Read `error_text` and `reply`. A model error (wrong model id, bad key) is fixed by choosing another saved model (`model_id`) and starting a new run, not by messaging the same run. |

## Slurm etiquette

- The login node is for submitting and reading logs. Never run compilation, training or large data work there, and do not work through a login-node terminal.
- Give every job a real time limit, CPU count and memory. Cancel what you no longer need.
- The job's folder lives in the host's Work directory, which must be on storage the compute nodes share. `compute_host` `test` checks it.

## Hygiene

- Set a time limit when you spawn; delete sandboxes when the work is done. `fetch` what matters first, or let `destroy` save the work folder.
- Do not leave agents or jobs running once you have what you need.
- Keep replies short: truncate, grep or summarise large output before returning it.

