# Safety

What the plugin does to protect files, secrets and spending, and where those protections end.

## What runs where

The chat tools give the model no shell on any host. Environments run in Podman or Docker containers, and Slurm work runs as batch jobs. The tools manage containers the plugin labelled itself, submit and read jobs, and run commands inside those containers. A person can open a terminal from the page; the model cannot.

The plugin itself runs container, Slurm and file commands on a host over SSH (the kinds are listed in the README under Disclosures). Addresses, user names, jump hosts, container names and job ids are checked against an allow-list before they reach a command line, and remote commands are quoted as one argument list. A command prefix on a host runs before every command there; it can be set on the page only, and the chat tool refuses it.

## Files

- **Time limits stop, they do not delete.** An environment past its time limit or idle timeout is stopped and its files stay. Starting it again gives a fresh time limit.
- **Delete saves first.** The page and the chat tool save `/work` to this computer, read the archive back end to end, and delete nothing if that fails. The folder saved is the one the environment was created with, even if its template changed or was deleted. If that folder cannot be determined, the delete is refused. Only an explicit "Delete without saving", behind a second confirmation (and an approval card in chat), skips the save.
- **Automatic deletion** is off by default (`auto_delete_days` is 0). When it is on, files are saved first and a failed save keeps the environment. The reaper also removes a container left behind by a failed start, with the same save first.
- **Containers without a record** that this install started (for example after the data was reset) are adopted and listed as stopped, with their files untouched.
- **Only `/work` is saved.** Files elsewhere in a container (installed packages, a home folder) are not.
- **Saved copies** are `.tar.gz` files in the exports folder (the plugin data folder, or `exports_dir`), with owner-only permissions on Linux and macOS. The size is measured and shown before a save, and `export_max_mb` caps it. **Save into folder** can also unpack the archive into a subfolder; unpacking refuses absolute paths, `..`, links that leave the folder, device files and names that would overwrite another file after case or Unicode folding, keeps the archive, and removes the new folder on refusal.
- **Uploads and save destinations** exclude the Hermes home folder (all profiles), the SSH folder, Windows device and share paths, and the plugin's private folders. A path in the exports setting that points inside those folders is refused.

## Approvals

When Hermes approvals are on, a card appears before each of these chat actions, and nothing happens until it is approved:

- starting, extending or deleting an environment;
- submitting or cancelling a Slurm job (the card shows the command or script);
- starting, stopping, re-running or messaging a cloud agent (the card shows model, endpoint, turn limit, repository path and task);
- adding, changing or removing a host, or testing an address that is not saved (the card lists every value; an EC2 host's card states the cost);
- starting, stopping, extending or terminating an EC2 machine (the card states the cost and whether billing starts now);
- uploading files, restoring or deleting saved copies, and saving into a folder;
- forwarding a host's port.

Each approval key names the action and its target, so approving one environment, run, file or destination does not approve another. An answer of "session" or "always" covers only the same action with the same values. The check fails closed: an error still produces a card.

The `approval` setting `remote-only` skips the card for hosts on this computer, except for deleting and for deleting without a save. Buttons on the Compute page are the user acting directly and need no card.

**With Hermes approvals switched off** (`approvals.mode: off`), Hermes shows no card and these chat requests run at once. The Compute page shows a red notice when it detects this, and the chat model is told so. Headless single-query runs without a person present refuse these actions instead of running them.

## Secrets

- Secrets are read from the Hermes secret store by name, when needed. Only names are stored.
- A secret reaches a host over SSH standard input, into a file with mode 0600 inside a folder with mode 0700, passed to the container runtime and deleted after the container starts. A reaper sweep removes leftovers older than 15 minutes. It is not placed in a command line, a log, the database, an event or a git bundle.
- Templates ship without secret variables. A secret is sent only when a template names it and the spawn dialog's box is ticked.
- Output sent to chat is checked by value. The plugin reads the values back from the container's environment when its own memory has none, and withholds the output when they cannot be read.
- Child processes on this computer (ssh, git, the container runtime, terminal windows, the address opener that the chat tool uses to open a screen) get only a short list of environment variables, such as `PATH`, `HOME` and `SSH_AUTH_SOCK`. The full lists, one of them for `git`, are in the README under Disclosures. On Windows, terminal windows and the address opener also get `SYSTEMDRIVE` (without it Windows Terminal cannot load its settings) and `LOCALAPPDATA` (without it the browser opens the address in a new, empty profile instead of the browser that is running). Keys, tokens and every other variable stay out. Proxy settings and `DOCKER_HOST` are not passed either: put ssh settings in the ssh config file. With `use_asyncssh` on, asyncssh sends no local variable to the server and starts no proxy command.

**Limits.** The check matches the value as printed; a command that encodes or splits the value first can get past it. A model that runs commands in an environment that holds a secret can see that secret. Give an environment only secrets the chat model may know.

## Cloud agents

- The sandbox gets the model key and no other secret, and no push credentials. Work leaves it as a git bundle through **Get results**.
- The model key is in the sandbox environment. A command the agent runs there can read it. Use a spend-capped, narrowly scoped key.
- The key is hidden in the run's reply, log, error text, notifications and tool results, by the key's value and by its token shape; if the sandbox cannot be read to learn the value, those texts are withheld with a plain sentence.
- The chat model cannot run commands in or open a terminal for an agent sandbox.
- The in-sandbox agent runs with Hermes approvals on and without YOLO mode.

## Network exposure

- Published ports and tunnels bind `127.0.0.1` on both ends. Nothing is opened to the network.
- A screen link carries a random per-environment token and every other path answers 404. Screens and tunnels expire after `tunnel_ttl_minutes`.
- A template can set the network to none for an environment that must not reach the network. There is no allow-list proxy.
- An EC2 host's security group allows SSH from one address only: this computer's, or the one typed. A range wider than /16 is refused, and `0.0.0.0/0` is not accepted.

## Host etiquette

Probes are light and batched. Slurm is polled at least 30 seconds apart per host (10 seconds while a job is open). Jobs are tagged `--comment=cloud-compute:<id>`. Cloud agents are off on Slurm hosts. A host cannot be deleted while it has environments or open jobs.

## What is stored

On this computer, in the plugin data folder: a SQLite database (hosts, templates, environments, jobs, tunnels, events, agent run records, the saved-copy index, and the random screen token of each environment) and the exports folder. Paths of identity files are stored. No passphrase or secret value is stored, and AWS key values are not stored. The one private key the plugin keeps is the SSH key it generates for an EC2 host, in the plugin data folder (`ec2/<host id>/`, mode 0600 on Linux and macOS); it is removed with the host.
