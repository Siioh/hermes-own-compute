"""Help topics and UI copy for the Own Compute Desktop page.

Pure data plus two tiny helpers; standard library only.  ``docs_api`` serves the
topics at ``GET /docs`` and ``GET /docs/{topic}``.  ``COPY`` is the UI copy
(type cards, wizard hints, tooltips, status lights); ``desktop/plugin.js`` embeds
the identical object and a test keeps the two in sync.
"""

import copy

from . import hosttypes as _hosttypes

# Stable, public documentation links used by the topics below.
_HERMES_DOCS = {"label": "Hermes Agent documentation", "url": "https://hermes-agent.nousresearch.com/docs"}
_SBATCH = {"label": "Slurm: sbatch", "url": "https://slurm.schedmd.com/sbatch.html"}
_JOB_ARRAYS = {"label": "Slurm: job arrays", "url": "https://slurm.schedmd.com/job_array.html"}
_APPTAINER = {"label": "Apptainer documentation", "url": "https://apptainer.org/docs/"}
_PODMAN = {"label": "Podman documentation", "url": "https://docs.podman.io/"}
_PODMAN_INSTALL = {"label": "Podman install guide", "url": "https://podman.io/docs/installation"}
_DOCKER_DESKTOP = {"label": "Docker Desktop documentation", "url": "https://docs.docker.com/desktop/"}
_NOVNC = {"label": "noVNC", "url": "https://github.com/novnc/noVNC"}
_OPENSSH = {"label": "OpenSSH manual", "url": "https://www.openssh.com/manual.html"}
_SSH_CONFIG = {"label": "ssh_config manual", "url": "https://man.openbsd.org/ssh_config"}
_GIT_BUNDLE = {"label": "git bundle", "url": "https://git-scm.com/docs/git-bundle"}
_GIT_WORKTREE = {"label": "git worktree", "url": "https://git-scm.com/docs/git-worktree"}


_GETTING_STARTED = """\
Own Compute lists the machines you can run work on, shows whether each one is online, and starts sandboxed environments or Slurm jobs on them. The same tools work from chat.

### Step 1: Install and turn on

Own Compute has two switches in **Capabilities → Plugins**: **Agent** (the chat tools) and **Desktop** (the Compute page).

1. Install the plugin, then run **Rescan** in Desktop.
2. Open **Capabilities → Plugins** and turn on the **Installed** filter. Own Compute is listed there only; the unfiltered catalog search does not show it.
3. Turn on the **Agent** switch. This turns on the **Desktop** switch too, unless that one was switched off on purpose. The Desktop switch is stored per browser or app, so a new browser needs it turned on again.
4. Using the command line? `hermes plugins enable cloud-compute` turns on the Agent switch only. Turn on **Desktop** in Capabilities → Plugins afterwards.
5. Turn on Agent **before the gateway starts**, or restart the gateway afterwards; otherwise the chat tools do not appear.
6. Start a **new chat**. Tools of a newly enabled plugin appear in the next chat, not in one that is already open.

If this page says the Python half is missing, run `hermes plugins enable cloud-compute` and restart the gateway and Desktop.

### Step 2: Add your first host

1. Open **Compute** in the sidebar, then **+ Add host**.
2. Pick a type: SSH host, Slurm login node or Local Docker/Podman.
3. Enter the endpoint and how to log in, then press **Test connection**. It checks DNS, port 22, login, the container runtime or scheduler, display support and GPUs, and tells you what to fix.
4. Choose the sandbox defaults and save. The host appears with a status light.

### Step 3: Start something

On a container host, open **Templates** and choose **Spawn on...** (or ask in chat). Check the "What will be sent" preview first; it lists names, never values. On a Slurm login node, use the **Jobs** tab to submit a batch job.

Everything opens locally, so the Hermes gateway has to run on the same computer that shows Desktop.
"""

_SSH_HOST = """\
An SSH host is a Linux machine you can `ssh` into: a spare laptop, workstation, homelab box or a cloud VM you own. Work runs in containers on it, not in its shell.

### What you need

- **Endpoint:** an alias from your `~/.ssh/config` (example: `gpu-box`), or host + user + port. A jump host is optional.
- **Login:** a key loaded in your SSH agent, or the path of a key file. Passwords and MFA prompts are not supported for automatic use. If a prompt would appear, the host shows **Needs login**.
- **On the remote side:** Podman or Docker. Rootless Podman is fine and no root access is needed. The runtime is detected automatically.

### Limits

Limits apply to environments the plugin starts on this host: the maximum number of environments, CPU and memory caps, and whether cloud agents are allowed. The idle timeout and time limit come from the request, else the template, else the defaults in **Settings** (this includes cloud agents), and a reaper stops idle environments and environments past their time limit. Stopping keeps the files; see *Keeping your data*.

### Resources

The host card shows CPU, RAM, disk and GPUs, used versus available, and CPU and RAM per environment. A GPU appears when the host has one and its container runtime can use it.

### Ports stay on loopback

Ports an environment publishes are bound to `127.0.0.1` on the host. **Forward port** and **Screen** reach them through an SSH tunnel that listens only on `127.0.0.1` on your computer. Nothing is opened to your network.

**Cloud agents** are off by default; allow them per host in the sandbox defaults.

If a check fails during **Test connection**, the hint beside it says what to change.
"""

_SLURM = """\
A Slurm login node is the front door of an HPC or lab cluster. Work runs as Slurm batch jobs, optionally inside an Apptainer image, never as a long-running sandbox.

### What you need

- **Endpoint and login:** the same as an SSH host. Optional: a jump host, partition, account, QOS and a work directory on the shared filesystem.
- **On the cluster:** `sbatch`, `squeue`, `sacct` and `scancel` on the PATH of a login shell. Apptainer is optional.
- **MFA:** if your site asks for a second factor, log in once in a terminal. The host shows **Needs login** until a connection works without a prompt; MFA is never automated.

### Login-node etiquette

Login nodes are shared, so probes are light and batched (health checks no faster than every 30 seconds) and each job is labelled `--comment=cloud-compute:<id>`. Heavy work belongs in a job. **Open shell** gives you a login-node shell; the model cannot open one. Cloud agents are off for Slurm hosts: an agent needs a long-running container, and most clusters do not allow long-running processes on login nodes.

### Job states and meters

- **Refresh:** open jobs are refreshed about every 10 seconds and when you open the job list; a slow scheduler leaves the last known state with a muted *not refreshed* hint.
- **Meters:** CPU and RAM *allocated* against the node total (*RAM n/a* if unknown).
- **No accounting:** without `sacct`, a job that left the queue is closed as **Ended (state unconfirmed)**, with no exit code.

### Submitting jobs

In the **Jobs** tab, give a command or script, an optional image, partition, CPUs, memory and time limit. An image is `docker://...` or a `.sif` path. Set **array** (for example `1-10%2`) for a job array: ten tasks, two at a time. Read output, cancel, or open a terminal into a running job; a notification arrives at the end. When the output is empty, the dialog says why: the job has not started, it printed nothing, or there is no output file where the login node looks. A finished job has **Remove from list** and **Clear finished** removes them all. This only removes the records on this computer; the job folders on the cluster stay.

### Work directory and limits

Each job gets its own folder, `<work directory>/<job id>` (default `~/cloud-compute`), holding `job.sh` and the output. Jobs start there. A job runs inside Apptainer only when an image is given; otherwise it runs directly as your user on the compute nodes. The work directory must be on storage the compute nodes also see (shared storage); home folders are not shared on every cluster. **Test connection** checks this, and the host card shows the failed check with its fix. There is no screen or port forward, because compute nodes sit behind the login node.

**Advanced:** *Command prefix* reaches a scheduler inside a container on the login host, for example `podman exec -i slurmctld`. Leave it empty for a real cluster.
"""

_LOCAL = """\
Local Docker/Podman runs containers on this computer. There is no network hop and nothing to log in to, which makes it good for quick tests when you have no other machine.

### What you need

Docker Desktop or Podman installed on this computer and reachable from the command line, meaning `docker` or `podman` works when you type it in a terminal (it is on your PATH). The endpoint and login fields stay empty. The runtime is detected automatically.

### What you get

- Long-running sandboxes from your templates, with the same host limits as an SSH host, and the time limit and idle timeout from the template or, if it sets none, from **Settings**.
- **Terminal** into a sandbox.
- **Screen** for templates that have a display. No tunnel is needed, because the published loopback port is used directly.
- **Forward port** to a port inside the sandbox. Published ports are bound to `127.0.0.1` only.
- A GPU only if Docker GPU access already works on this computer.
- No cost.

### Setting up

1. Install Docker Desktop or Podman and start it.
2. In a terminal, confirm that `docker version` or `podman version` answers.
3. In Hermes, **+ Add host**, choose **Local Docker/Podman**, then **Test connection**.

If the host shows **Degraded**, the computer is reachable but the runtime is missing or failing. Open the host to see which check failed. Check that Docker Desktop (or Podman) is running and that its command is on your PATH.
"""

_EC2 = """\nAn EC2 host is an AWS account, a region and launch settings. **Start machine** launches one AWS instance (Amazon Linux 2023 with Docker); once ready, environments run on it as containers. **AWS bills your account while the machine runs; a stopped machine still bills a small amount for its disk until it is terminated.**

### What to provide

- An **IAM user made only for this plugin** (never the root user), with the policy in `docs/ec2-iam-policy.json` (put your own region and instance types in it). It limits what the key can launch; it is not a spending cap, so set a budget alert in AWS billing.
- Its key, stored as the Hermes secrets **AWS_ACCESS_KEY_ID** and **AWS_SECRET_ACCESS_KEY** (optional **AWS_SESSION_TOKEN**). Never paste a key into a chat. Chat accepts only secret names that start with `AWS_`; other names are set on this page.

### In chat

Chat can add, change and remove an EC2 host and start, stop, extend and terminate its machine, each after a card with every value and the cost. A Start or Extend card differs every time, so one "always" never covers the next. Image, subnet, security group and SSH user: this page only.

### What Own Compute does in AWS

- **Test** reads the account and checks a launch with **DryRun**; nothing starts. The wizard lists the types AWS reports as free-tier eligible.
- **Start machine** creates a key pair (the private half stays on this computer) and a security group that allows SSH **only from this computer's public address** (looked up once at `https://checkip.amazonaws.com`) or the address you typed. The host key is read from the console output and pinned; if it is missing, the first SSH login trusts the key it sees.
- Everything created carries the tag `cloud-compute:install`; nothing without it is ever changed.

### Spending guards

- **Max lifetime:** at every boot the machine's own stop timer counts the max lifetime last written to the machine: the launch value, replaced by the current setting once this computer reaches the machine over SSH. **While Hermes is closed only that timer stops the machine, and until then it can be longer than the setting; the card says how long.** While Hermes runs it also stops the machine at the limit. **Extend** adds time, at most to twice the max lifetime from the last start.
- **Idle stop** stops the machine after the set minutes with no running environment.
- **Auto-start** (off by default) starts a stopped machine when an environment is spawned.
- Tagged instances that no host tracks are listed on the host card when it shows, never touched automatically. The account is recorded at launch; another account's keys leave the machine on the card with a warning. Two Hermes processes starting one host at once are held apart by a claim that lapses after 3 minutes.

**Terminate machine** deletes the instance and its disk; it is refused while the host has environments unless you save and delete them first. On a stopped machine, saving needs it running: start it (billed) or choose **Terminate without saving**, which loses those files.
"""

_TEMPLATES = """\
An environment template describes what to start: the image, its limits and its variables. Spawning a template on a host creates an environment, also called a sandbox.

**Built-in templates:** `ubuntu-dev`, `python-dev`, `desktop-novnc` and `slurm-python`. Editing a built-in saves your own version; **Reset to default** brings the original back, and a built-in cannot be deleted. Press **+ New template** to make your own (a name and an image are enough), or **Duplicate** one. The **Name** field in the editor renames a template. Cloud agent templates have their own section, because the Agents tab uses them.

### Fields and toggles

- **Image, command and working directory.**
- **CPUs, memory and GPUs** requested for the environment. A host's own caps still apply.
- **Network:** default, or none for an environment that cannot reach the network.
- **Display:** turn it on if you want a remote screen; see the Remote screen topic.
- **Lifetime:** a time limit after which the environment is stopped (files are kept), an idle timeout after which it is stopped, and a persistent switch that keeps it past the time limit. Leave a value blank and the **Default ttl minutes** / **Default idle minutes** in **Settings** apply when the environment starts; a value in the template wins. Only a persistent template has no expiry.
- **Host kinds** the template may be used on.
- **Cloud agent toggles** for templates used by cloud agents.

### Variables and scopes

Each variable has a name, a description, a source and a scope. The source is either a literal value or `secret:NAME`, which is looked up in Hermes secrets only when the sandbox starts. The scope is `runtime`, `redacted` (the default) or `agent-visible`. Secret values and literal values with the scope `redacted` are hidden in command output and logs; `runtime` and `agent-visible` literal values are shown. A variable can be limited to certain hosts.

### What will be sent

Before anything starts, the preview shows exactly what starts on the host: image, command, limits, labels, published ports and the names and scopes of variables, with a reason for any that will not be sent, such as a secret that is not set. Secret values are never shown and never written to logs.

To spawn: **Templates** tab, **Spawn on...**, pick a host. The dialog lists each variable that will be sent, with its description. If a secret will be sent, **Spawn** stays off until **Send these secrets into the environment** is ticked; a secret that is not set shows as *not set, will not be sent*.
"""

_KEEPING_DATA = """\
Environments hold your work, so Own Compute stops them instead of deleting them, and saves a copy before any delete. A saved copy holds the working folder (`/work`) only; files elsewhere in the container, such as installed packages or your home folder, are not saved. Keep important work in `/work`.

### Time limit and idle

When an environment reaches its time limit it is **stopped**, not deleted, and its files are kept. A warning arrives shortly before, set by **Expiry warning minutes** in **Settings**, with an **Extend** button; **Extend...** on the environment card adds time. An idle environment is stopped too. Starting a stopped environment gives it a fresh time limit.

### Automatic deleting

Environments are not deleted automatically unless **Auto delete days** in **Settings** is above 0. Even then, the files are saved to this computer first, and if saving fails nothing is deleted. The only other automatic removal is the clean-up of a container left by a failed start; it also saves first and keeps the container if that fails.

### Deleting an environment

**Delete** has **Save the files in the work folder to this computer first** ticked by default. The folder saved is the one the environment was created with, even if its template changed or was deleted later. If that folder cannot be told, the delete is refused and nothing is removed. If saving fails, nothing is deleted. Only then does **Delete without saving** appear, behind a second confirmation. After a saved delete the page shows the message from Own Compute, the folder, the number of files and the size, with **Show in folder** where the system allows it. If the folder had no files, it says so.

### Environments without a record

A container started by this install that has no record, for example after a data reset, is never removed. It is adopted and listed as a stopped environment marked *Found on its host without a record*, with its files untouched.

### Files and saved copies

**Files...** on an environment browses its folders, downloads a file or folder, uploads from this computer after a confirmation, and lists its saved copies. **Save all of /work** also works on a stopped environment. It first measures the folder and shows the size and the number of files; you confirm before anything is transferred, and a folder without files is not saved. **Save into folder** saves into another folder on this computer, for example a project folder, and **Unpack into a new subfolder** also unpacks the files there. Deleting a saved copy never removes an unpacked folder. Copies are stored in the exports folder inside the plugin's data folder; the dialog shows the full path with a Copy button. **Restore into...** unpacks a copy into any environment after you confirm that files with the same names are replaced, and **Delete** removes one. The size limit is **Export max mb** in **Settings**.
"""

_DISPLAY = """\
**Screen** opens the desktop of a sandbox in a tab, so you can use a graphical program inside it. It is only for templates with a display, such as `desktop-novnc`.

### How it works

The sandbox runs a small desktop and a VNC server, shown in your browser by noVNC. Everything inside the container listens on its own loopback, and the port is published only on `127.0.0.1` of the host. For an SSH host, Own Compute opens an `ssh -L` tunnel from a free port on your computer to it. For local Docker/Podman no tunnel is needed. The screen is protected by a random per-instance password that is part of the link and is not shown in chat. Tunnels expire after a time limit.

### States of the Screen button

- **Available / Running:** open it, or stop it.
- **Build display image:** the host has a runtime but not the display image yet. One click builds it on the host.
- **Not available:** the host can't show a screen, or the template has no screen. The tooltip says which.

### Why some hosts can't

Slurm hosts never show a screen: the compute nodes sit behind the login node, so there is nothing to reach. A host without a container runtime can't either, and a template needs the display switch on.

### If there is no screen

A template without a screen server answers that it needs setup: use the **Desktop (noVNC)** template, or turn off **Show a screen** in the template.

### From another computer

The address belongs to the computer that runs Hermes. If the page is open on another computer, for example a Webapp opened remotely or through an ssh tunnel, forward the port first: `ssh -N -L <port>:127.0.0.1:<port> <that computer>`. The page shows the command with the port filled in.

### Browser fallback

By default the screen opens inside Desktop. If it stays blank or does not take keystrokes, **Open in browser** opens the same address in your default browser. If this Desktop version cannot host the in-app tab, the browser is used automatically.
"""

_TERMINAL = """\
**Terminal** opens a terminal window on this computer, inside this environment: Windows Terminal or Command Prompt on Windows, Terminal on macOS, your usual terminal on Linux. On macOS it is always the Terminal app, also when iTerm is installed. It runs `ssh -t <host> podman exec -it <container> bash -l` (falling back to `sh` if the container has no bash). For Docker the same command uses `docker`. On a local host it goes straight into the container. The window can appear behind Desktop; a notice confirms that it opened. If the command fails, the window stays open with "Press Enter to close" so the message can be read.

### If the terminal can't open

With the default setting (**auto**) Own Compute picks the usual terminal for your operating system. If none opens, you see "Couldn't open a terminal. The command is on your clipboard." Paste it into any terminal to get the same shell. The **Default terminal** setting in the plugin settings can pick a specific terminal, or copy only.

### Other terminals

- A **Slurm login node** has **Open shell**, a shell on the login node for you to use by hand.
- A running **Slurm job** can be entered through `srun --jobid ... --pty`.

Opening a terminal counts as activity, so it resets the idle timer.

### Port forwards

**Forward port** makes a port inside the environment available at `127.0.0.1` on this computer. Give the remote port; the local port is optional, and a free one is chosen when you leave it empty. The tunnel binds to `127.0.0.1` only and is closed automatically after a time limit. Open and close tunnels from the list. Slurm jobs cannot be forwarded.

A template can list **Published ports**; the environment card then shows them with **Forward** and **Open in browser**. If a forward fails, the local port is probably taken. Pick another, or leave it empty.

### From another computer

The terminal window opens on the computer that runs Hermes. From another computer, copy the command and run it there. Forwarded ports belong to that computer too: forward the port first with `ssh -N -L <port>:127.0.0.1:<port> <that computer>`.
"""

_CHAT = """\
Hermes chat sessions can use the same machines you see on this page.

### What chat sees

On the first turn, and again whenever something changes, a short line is added to the conversation: which hosts are online (with CPU and RAM use and their environments), which are offline, running jobs, and jobs finished since the last turn. It is short, built from stored state and never contacts a host. Turn it off with the `chat_context` setting. `/compute` prints a summary (`/compute status`, `hosts` or `jobs`).

### Tools

- `compute_status`: hosts, resources, environments, active jobs and template ids. Host ids come from here.
- `compute_host`: add, test, change or remove a host, so the model can set up a machine for you.
- `env_spawn`, `env_exec`, `env_control`, `env_files`, `env_open`: start an environment, run a command inside it, stop/start/destroy it or read logs, open a terminal, screen or port.
- `env_files`: list, size, save and upload files, and manage saved copies.
- `slurm_submit`, `slurm_jobs`: submit, list, inspect, wait for, cancel, remove and read output.
- `cloud_agent_spawn`, `cloud_agent_status`, `cloud_agent_message`, `cloud_agent_collect`, `cloud_agent_stop`: hand a goal to a headless Hermes agent that works on a git branch inside a sandbox. The branch is shipped in as a git bundle, and the result comes back to your computer as a branch (or a worktree). The sandbox never gets push credentials.

### Approvals

When Hermes approvals are on, these show an approval card in chat, and nothing happens until you approve: starting, extending or deleting an environment; submitting or cancelling a Slurm job (the card shows the command or script); starting, stopping, re-running or messaging a cloud agent (the card shows the model, turn limit, repository and task); adding, changing or removing a host, or testing an address that is not saved yet (the card lists every value); uploading, restoring or deleting saved files; saving into a folder you choose; forwarding a host's port. An answer of "session" or "always" covers only the same action with the same values. The `approval` setting can skip the card for local hosts, except for deleting. Buttons on this page need no card. If approvals are switched off in Hermes settings (`approvals.mode: off`), these chat requests run without a card; the Compute page then says so at the top. Turn approvals on in Settings to see a card before each one.

### Cards

When you ask for details, the model can show a live card in the conversation: an environment, a Slurm job, a cloud agent run, a saved copy of files or a host.

The chat tools give the model no shell on your hosts: environments run in containers and work runs as Slurm jobs. A host's command prefix can only be set on this page. The model cannot run commands in, or open a terminal for, a cloud agent's sandbox, because it holds the model key. New tools appear in the next chat after you enable the plugin.
"""

_AGENTS = """\
A cloud agent is a headless Hermes agent that works on a git branch of a repository on this computer, inside a sandbox on one of your hosts. You give it a task and get the result back as a branch.

### Use the Agents tab

1. Open **Agents** and press **New agent run**. Describe the task, pick a host and a template (the model-backed *Hermes cloud agent* or the model-free *Handoff test*), give the repository path and a branch, then **Start**.
2. The run card shows *starting*, *running*, then *done* or *failed*.
3. Press **Get results** to bring the commits back. **Run again** refills the dialog from a run. **Stop** first fetches commits you do not have yet, then removes its sandbox.

The run card shows the agent's reply, the model and the turns, and updates by itself. **Send a message** continues a run once its turn ended. A failed run shows the cause and what to do; **Run again** can change the model or host first. **Remove** deletes the record of a run that is not active. The model key is hidden in replies and logs; a reply is withheld if it cannot be.

You can also ask for a run in chat (`cloud_agent_spawn`).

### Before you start one

1. Allow cloud agents on the host (sandbox defaults, **Allow cloud agents**). Slurm hosts cannot.
2. Choose the **Model** in the New agent run dialog: a saved choice, one this Hermes profile offers, or another endpoint you type and keep with **Save this choice**. Or set it once in **Settings** → Own Compute, set **Cloud agent model endpoint** (an OpenAI-compatible base URL such as `https://llm.example.net/v1`; plain http only on a trusted private network) and **Cloud agent model**; a saved value applies to the next run, with no restart. The template's Agent section (`agent.base_url`, `agent.model`) wins over Settings. The handoff test needs no model.
   Lifetime follows **Default ttl minutes** and **Default idle minutes** in Settings unless the template sets its own; a working agent is not idle-stopped.
3. Add the model key as the secret `CC_AGENT_API_KEY` in your profile's `.env` file. A model this Hermes profile already offers uses its own key secret instead. Use a spend-capped key, never your own login. It is the only secret a cloud-agent sandbox gets.
4. If the model runs on your own server, warm it first.

### The approval

The card shows the template, host, model, endpoint, turn limit, repository path and task, and says the model key `CC_AGENT_API_KEY` goes into the sandbox. Handoff tests send no secrets. When Hermes approvals are on, nothing starts until you approve. Inside the sandbox the agent runs with Hermes approvals on: commands Hermes flags as dangerous are refused, and the run continues.

### Get results

**Get results** brings the agent's commits back as the local branch `refs/heads/cloud/<run>/<branch>` and checks it out in a sibling worktree. The card then says *Results fetched*; **Get new results** appears only after new commits. Nothing is merged and none of your branches move.

**No push credentials enter the sandbox.** The agent cannot push; work leaves the sandbox only through Get results.
"""

_SECURITY = """\
**What runs where.** The model gets no shell on your hosts. Environments run in Podman or Docker containers, and Slurm work runs as batch jobs. The tools manage containers the plugin labelled itself, submit and read jobs, and run commands inside those containers; the plugin itself runs container, Slurm and file commands on a host over SSH. A command prefix can only be set on the Compute page, never by the model. Some terminal and tunnel work runs on your computer: `ssh` tunnels, a terminal window, and `git` for cloud agent handoff.

**What is stored.** In a local database on your computer: hosts, templates, environments, jobs, tunnels and events, including the **path** of an identity file and the random screen password of each environment. **Never stored:** private keys, passphrases or secret values. Secret names are kept; values are read from Hermes secrets only when a sandbox starts, delivered over standard input to a private file on the host that is deleted right after the container starts, and never placed in a command line or a log. Output sent to the chat is redacted by value: a secret is hidden when printed as it is, not when a command changes it first. When the secret values cannot be read back from the environment (for example because its host cannot be reached), the output is withheld instead of shown. Give an environment only secrets you would trust the chat model with. The model cannot run commands in a cloud agent's sandbox, so the agent's key does not reach the chat.

**What is sent, and where.** SSH commands go to the hosts you added. Container images are pulled by the remote runtime, and Apptainer may pull `docker://` images. Cloud agents call the model endpoint you configure, with a dedicated spend-capped key. There is no telemetry.

**Network exposure.** Published ports and tunnels bind to `127.0.0.1`. Screens use a per-environment password and tunnels expire. A template can set the network to none.

**Control.** Spawning, submitting, destroying, cancelling, changing hosts, uploading, restoring, saving into a chosen folder, forwarding a host's port and testing an address that is not saved need your approval in chat when Hermes approvals are on. Hosts have limits, environments have a time limit and an idle timeout that stop them (files are kept), and **Stop all** stops everything. Output is redacted by value. The "What will be sent" preview shows names, never values. Uploads and save folders are never inside the SSH folder or the Hermes folder (all profiles); paths starting with two backslashes are refused.

**Cloud agents.** They are opt-in per host, off for Slurm hosts, and work on a copy of the branch. No push credentials enter the sandbox.
"""

_TROUBLESHOOTING = """\
Start with the host's status light.

**Needs login (amber).** The machine answers, but SSH needs a prompt such as a password, a passphrase or MFA. Run `ssh <alias>` once in a terminal, finish the login, then press Retry. Load your key into the OpenSSH agent so no passphrase is asked.

**Degraded (amber).** Connected, but the container runtime or scheduler is missing or failing. Open the host to see which check failed, then fix it on the machine (install or start Podman or Docker; for Slurm check that `sbatch` and `squeue` work in a login shell).

**Offline (red).** Nothing answers on port 22. Check the machine is on and on your network or VPN.

**Unknown (grey).** Not checked yet. Use **Refresh status**.

**The page says the backend is missing.** Turn on the **Agent** switch in Capabilities → Plugins, or run `hermes plugins enable cloud-compute`, then restart Desktop.

**New tools are not in my chat.** Start a new chat. Tools of a newly enabled plugin appear in the next one.

**No Screen button, or it is disabled.** Slurm hosts can't show a screen, and a template needs the display switch on. If the button says **Build display image**, press it once.

**A terminal did not open.** The command is on your clipboard. Paste it into any terminal.

**A port forward fails.** The local port is probably in use. Pick another or leave it empty.

**An environment will not start.** Check **Logs**. The first start pulls the image on the host, which can be slow or fail if the host has no route to its registry. An environment that was idle or past its time limit was stopped on purpose; its files are kept, so press **Start**.

**A job is stuck pending.** The cluster has no free resources for it yet; check the partition's idle CPUs on the host card.
"""


TOPICS = [
    {
        "topic": "getting-started",
        "title": "Getting started",
        "body_md": _GETTING_STARTED,
        "links": [dict(_HERMES_DOCS)],
    },
    {
        "topic": "ssh-host",
        "title": "SSH hosts",
        "body_md": _SSH_HOST,
        "links": [dict(_OPENSSH), dict(_SSH_CONFIG), dict(_PODMAN)],
    },
    {
        "topic": "slurm",
        "title": "Slurm login nodes",
        "body_md": _SLURM,
        "links": [dict(_SBATCH), dict(_JOB_ARRAYS), dict(_APPTAINER)],
    },
    {
        "topic": "local",
        "title": "Local Docker/Podman",
        "body_md": _LOCAL,
        "links": [dict(_DOCKER_DESKTOP), dict(_PODMAN_INSTALL)],
    },
    {
        "topic": "ec2",
        "title": "EC2",
        "body_md": _EC2,
        "links": [],
    },
    {
        "topic": "templates",
        "title": "Environment templates",
        "body_md": _TEMPLATES,
        "links": [dict(_PODMAN)],
    },
    {
        "topic": "keeping-data",
        "title": "Keeping your data",
        "body_md": _KEEPING_DATA,
        "links": [],
    },
    {
        "topic": "display",
        "title": "Remote screen",
        "body_md": _DISPLAY,
        "links": [dict(_NOVNC)],
    },
    {
        "topic": "terminal",
        "title": "Terminals and ports",
        "body_md": _TERMINAL,
        "links": [dict(_OPENSSH), dict(_SSH_CONFIG)],
    },
    {
        "topic": "chat",
        "title": "Using Own Compute from chat",
        "body_md": _CHAT,
        "links": [dict(_HERMES_DOCS), dict(_GIT_BUNDLE), dict(_GIT_WORKTREE)],
    },
    {
        "topic": "agents",
        "title": "Cloud agents",
        "body_md": _AGENTS,
        "links": [dict(_GIT_BUNDLE), dict(_GIT_WORKTREE), dict(_HERMES_DOCS)],
    },
    {
        "topic": "security",
        "title": "Security and privacy",
        "body_md": _SECURITY,
        "links": [dict(_HERMES_DOCS)],
    },
    {
        "topic": "troubleshooting",
        "title": "Troubleshooting",
        "body_md": _TROUBLESHOOTING,
        "links": [dict(_HERMES_DOCS)],
    },
]


_CAPABILITY_KEYS = (
    "containers", "batch_jobs", "interactive_terminal", "display", "port_forward", "gpu", "agents", "costs_money",
)


def _capabilities(*values):
    return dict(zip(_CAPABILITY_KEYS, values))


COPY = {
    "type_cards": {
        "ssh": {
            "kind": "ssh",
            "title": "SSH host",
            "one_liner": "Run sandboxed containers on a Linux machine you can SSH into.",
            "when_to_use": "Your own laptop, workstation, homelab or VM.",
            "learn_more_topic": "ssh-host",
            "mvp": True,
            "capabilities": _capabilities(
                "yes", "no", "yes", "partial", "yes", "partial", "partial", "no",
            ),
            "capability_notes": {
                "display": "Needs the display image on the host; the Screen button offers to build it.",
                "gpu": "If the host has a GPU that Docker or Podman can use.",
                "agents": "Off until allowed in the host's sandbox defaults.",
            },
        },
        "slurm": {
            "kind": "slurm",
            "title": "Slurm login node",
            "one_liner": "Submit batch jobs to an HPC cluster through its login node.",
            "when_to_use": "HPC or lab clusters, sweeps, scheduled work.",
            "learn_more_topic": "slurm",
            "mvp": True,
            "capabilities": _capabilities(
                "no", "yes", "partial", "no", "no", "partial", "no", "partial",
            ),
            "capability_notes": {
                "interactive_terminal": "A shell on the login node, or inside a running job.",
                "gpu": "If your cluster has GPUs.",
                "costs_money": "Only if your site bills for cluster time.",
            },
        },
        "local": {
            "kind": "local",
            "title": "Local Docker/Podman",
            "one_liner": "Run containers on this computer.",
            "when_to_use": "Quick tests without another machine.",
            "learn_more_topic": "local",
            "mvp": True,
            "capabilities": _capabilities(
                "yes", "no", "yes", "partial", "yes", "partial", "partial", "no",
            ),
            "capability_notes": {
                "display": "Needs the display image on this computer; the Screen button offers to build it.",
                "port_forward": "Forwards the screen and any ports a template publishes.",
                "gpu": "If Docker or Podman GPU access works on this computer.",
                "agents": "Off until allowed in the host's sandbox defaults.",
            },
        },
        "ec2": {k: v for k, v in _hosttypes.get_host_type("ec2").items()
                if k in ("kind", "title", "one_liner", "when_to_use", "learn_more_topic", "mvp",
                         "capabilities", "capability_notes")},
    },
    "wizard_hints": {
        "endpoint": "Use an alias from your SSH config if you have one. Example: gpu-box.",
        "login": "Uses your SSH agent or the key file you choose. The key itself is never stored.",
        "test_connection": (
            "Checks DNS, port 22, login, the container runtime or scheduler, display support and GPUs."
        ),
        "sandbox_defaults": "Limits apply to environments the plugin starts on this host.",
        "jump_host": "Optional. A gateway server to connect through (ssh -J), as [user@]host[:port]. Leave empty for a direct connection.",
        "partition": "Optional. The Slurm partition for jobs that do not name one (sbatch -p). Leave empty to use the cluster default.",
        "account": "Optional. The Slurm account added to every job (sbatch -A). Leave empty if the cluster does not ask for one.",
        "qos": "Optional. The quality-of-service level added to every job (sbatch --qos). Leave empty unless the site asks for one.",
        "workdir": (
            "Where each job gets its own folder, holding its script and output. Jobs start there, "
            "but it is not a sandbox: they run as you and can reach anything your account can. "
            "The folder must be on storage the compute nodes also see (shared storage). Home folders are not shared on every cluster, and job output cannot be read from a folder that is not. Test connection checks this."
        ),
        "exec_prefix": (
            "Advanced. Added before every command run on this host. "
            "Only for a scheduler inside a container, for example podman exec -i slurmctld. "
            "It can only be set here: the chat tool refuses it."
        ),
        "interactive_prefix": (
            "Advanced. Used instead of the command prefix for terminal sessions that need a TTY, "
            "for example podman exec -it -w /data slurmctld. "
            "It can only be set here: the chat tool refuses it."
        ),
        "slurm_agents": (
            "Cloud agents are off for Slurm hosts: an agent needs a long-running container, "
            "and most clusters do not allow long-running processes on login nodes."
        ),
    },
    "tooltips": {
        "screen": "Opens the sandbox desktop in a tab. Only for templates with a display.",
        "screen_disabled": "This host type can't show a screen.",
        "terminal": (
            "Opens a terminal window on this computer, inside this environment. "
            "Windows Terminal or Command Prompt on Windows, Terminal on macOS, your usual terminal on Linux."
        ),
        "terminal_fallback": "Couldn't open a terminal. The command is on your clipboard.",
        "forward_port": "Makes a port inside the environment available at 127.0.0.1 on this computer.",
        "send_preview": (
            "Exactly what starts on the host: image, limits and the names of variables. "
            "Secret values are never shown."
        ),
        "idle_timeout": "Stopped automatically after this many minutes without use.",
        "command_prefix": "Advanced. Only for schedulers inside a container.",
        "refresh_status": (
            "Re-reads this host's status, resources and capabilities now. "
            "Changes nothing on the host."
        ),
        "test_connection": (
            "Runs the step-by-step connection checklist: DNS, network port, login, "
            "container runtime or scheduler, screen support and GPUs. Nothing is saved."
        ),
        "screen_not_enabled": "Screen: not enabled in this template",
    },
    "status_lights": {
        "online": {
            "tone": "green",
            "label": "Online",
            "meaning": "Connected; runtime or scheduler working",
            "action": "",
        },
        "degraded": {
            "tone": "amber",
            "label": "Degraded",
            "meaning": "Connected, but the runtime/scheduler is missing or failing",
            "action": "open the host to see which check failed",
        },
        "auth_required": {
            "tone": "amber",
            "label": "Needs login",
            "meaning": "The machine answers but SSH login needs a prompt",
            "action": "run ssh <alias> once in a terminal, then Retry",
        },
        "offline": {
            "tone": "red",
            "label": "Offline",
            "meaning": "No answer on port 22",
            "action": "check the machine is on and on your network/VPN",
        },
        "unknown": {
            "tone": "grey",
            "label": "Unknown",
            "meaning": "Not checked yet",
            "action": "Refresh status",
        },
    },
}


def list_topics():
    """Return ``[{topic, title}]`` in display order."""
    return [{"topic": t["topic"], "title": t["title"]} for t in TOPICS]


def get_topic(topic):
    """Return a fresh copy of one topic dict, or ``None`` if it does not exist."""
    for entry in TOPICS:
        if entry["topic"] == topic:
            return copy.deepcopy(entry)
    return None
