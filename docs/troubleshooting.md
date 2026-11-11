# Troubleshooting

Start with the host's status light on the Hosts tab. Messages below are the ones the plugin shows.

## The page and the tools

**No Compute entry in the sidebar, or the page says the backend is missing.** The plugin has two switches. Open **Capabilities > Plugins**, turn on the **Installed** filter, open Own Compute and turn on **Agent** and then **Desktop**. **Compute** appears in the sidebar once the Desktop half is on. The Desktop switch is stored per browser or app, so a new browser needs it turned on again. If the Agent switch was off when the gateway started, run `hermes plugins enable cloud-compute` and restart the gateway.

**The tools are not in my chat.** Start a new chat. Tools of a newly enabled plugin appear in the next one. Hermes may also offer plugin tools through its deferred tool lookup, so a model can need one extra step.

**No approval card appears in chat.** Hermes approvals may be off (`approvals.mode: off`). The Compute page shows a red notice when they are. Turn approvals on in Hermes settings.

## Hosts

**Needs login.** The machine answers but SSH needs a prompt. Run `ssh <alias>` in a terminal, finish the login (password, passphrase, second factor), then press Retry. Loading the key into the SSH agent avoids the passphrase prompt. "Host key verification failed: connect once with ssh in a terminal to accept the host key" means the key of the host changed or could not be accepted: connect once by hand.

**Degraded.** Connected, but the container runtime or scheduler is missing or failing. Open the host to see which check failed. Install or start Podman or Docker, or check that `sbatch` and `squeue` work in a login shell.

**Docker or Podman on this computer.** "Docker is installed but not running. Start Docker Desktop, then test again." Start it and test again. "No Podman or Docker found on this computer. Install Docker Desktop or Podman, then test again." Check that `docker version` or `podman version` answers in a terminal.

**Offline.** Nothing answers on the SSH port. Check that the machine is on and that the network or VPN is up.

**Unknown.** Not checked yet. Press **Refresh status**.

## Environments

**An environment will not start.** Open **Logs**. The first start pulls the image on the host, which is slow, or fails when the host has no route to its registry. An environment that was idle or past its time limit was stopped on purpose and its files are kept; press **Start**.

**"Show a screen needs the Desktop (noVNC) image."** Start from the Desktop (noVNC) template, or turn the screen switch off in the template.

**The Screen button is disabled or says Build display image.** Slurm hosts cannot show a screen, and a template needs the screen switch on. If the button says **Build display image**, press it once; the image is built on the host.

**The screen is blank.** Press **Open in browser**. If the page is open on another computer, forward the port first with the command the page shows: `ssh -N -L <port>:127.0.0.1:<port> <that computer>`.

**A terminal did not open.** "Couldn't open a terminal. The command is on your clipboard." Paste it into any terminal. On Linux a gateway started over SSH has no desktop session; the command is shown instead. The setting `default_terminal` can force one terminal or `copy`.

**A port forward fails.** The local port is probably in use. Pick another or leave it empty. A port that the template does not publish is refused: add it under Published ports and start a new environment.

## EC2

**The host shows an error, or stays in "Starting".** Press **Check AWS** on the host card. A machine that is still running bills until it is stopped or terminated: use **Stop machine** or **Terminate machine**. Terminate asks what to do with the environments on the machine and saves their files first unless told not to.

**"The Hermes secrets ... are not both set" or "The Hermes secret ... is not set".** Store the AWS key under the secret names the host uses (`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` by default), then test again.

**The test or a start is refused by AWS.** The IAM policy may not allow the region or the instance type. Compare them with [ec2-iam-policy.json](ec2-iam-policy.json).

**"The AWS keys now belong to a different account than the one this machine was started in."** The machine may still run and bill in the first account. Put the original keys back, or stop the machine in the AWS console.

**The machine is stopped and an environment on it cannot be removed.** A stopped machine cannot save files. On the host card press **Terminate machine** and choose **Terminate without saving** (the files are lost), or **Start machine** (AWS bills while it runs), save, then terminate. To delete one environment, delete it without saving; its container is removed when the machine next runs, or goes with the disk when the machine is terminated.

**"The machine is about to stop at its time limit and no longer accepts connections."** The machine is within its last moments before the stop. Start it again after it has stopped to reach its files; the disk is kept.

**A launch waits a long time.** The AWS status checks take about 2.5 minutes before SSH and Docker are tried.

**"The machine of ... is already being started by another Hermes process."** Check again in a minute. A claim of 3 minutes keeps two processes from launching one host twice.

## Slurm

**"Work directory check failed ... job output written there cannot be read back."** The work directory is not visible from the compute nodes. Edit the host and set **Work directory** to a folder on storage that the login node and compute nodes share (often a project, scratch or `/data` path), then press **Test connection**.

**A job stays pending.** The cluster has no free resources for it yet. Check the partition's idle CPUs on the host card.

**A job ends as "Ended (state unconfirmed)".** The cluster has no `sacct`, so the exit code is unknown.

## Cloud agents

**"No model is set up for cloud agents."** Add one in **Agents, New agent run, Model**, or set `agent_base_url` and `agent_model` in Settings.

**"The model endpoint ... answered 404: model ... was not found there (or the endpoint address is wrong)."** Check the model id and the endpoint address, then **Run again...**.

**A provider is listed as not available.** It signs in with an account, has a key written into the Hermes configuration, or has no key secret name. Cloud agents need a key stored under a secret name or a model server the user runs.

**"variable CC_AGENT_API_KEY contains a line break."** Store the key again without a trailing line break.

**The first call of a self-hosted model fails.** The server may have unloaded the model. Load it, then run again.

**A failed run still holds a sandbox.** Press **Stop** (it saves `/work` first), then **Remove** if the record is not needed.
