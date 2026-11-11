# EC2 hosts

An EC2 host is an AWS account, a region and launch settings. **Start machine** launches one AWS instance (Amazon Linux 2023 with Docker by default). Once it is ready it behaves like an ordinary SSH host: environments run on it as containers, with terminals, screens, port forwards, files and cloud agents as on any container host.

Each EC2 host has at most one machine. A second Start on the same host does not launch a second instance.

## Costs

**AWS bills the user's account** for:

- the instance while it runs;
- its disk for as long as the instance exists, also while it is stopped;
- a public IPv4 address, possibly.

Own Compute cannot see AWS prices, and it cannot tell whether a free allowance of the account covers a launch. Check the AWS billing page, and create a budget alert in the AWS billing console. A budget alert reports late and does not stop anything.

Every approval card for an EC2 action states the region, instance type, disk, maximum lifetime, idle rule, who may use SSH, and whether the action starts billing now. No card names a price.

## What to set up in AWS

1. Create an IAM user for this plugin. Never use the root user.
2. Attach the policy in [ec2-iam-policy.json](ec2-iam-policy.json) as a **customer managed policy**. The file is longer than the inline limit. Before attaching it, put your region in place of every `us-east-1` and the instance types you are willing to pay for in place of the two examples. The policy lets the key launch only those types, in one region, with a root disk of at most 30 GB. It lets the key start, stop or terminate only instances that carry the tag `cloud-compute:install`. [ec2-iam-policy.md](ec2-iam-policy.md) explains it. The policy limits what the key can launch. It is not a spending cap.
3. Store the user's access key as Hermes secrets named `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` (and `AWS_SESSION_TOKEN` when the key is temporary). A host refers to the secrets by name and can name others. The plugin reads a value from the secret store at the moment of each AWS call and does not keep it. Do not paste a key into chat: chat refuses key values.
4. In AWS, set a budget with an alert, and keep the vCPU quota low.

## Adding the host

**+ Add host**, **EC2**. Fields:

- region (default `us-east-1`);
- instance type (default `t3.micro`);
- image: `auto-al2023` (default), `auto-ubuntu` or an `ami-` id;
- disk in GB, 8 to 500 (default 10);
- SSH user (set from the image);
- maximum lifetime in minutes, 15 to 1440 (default 120);
- idle stop in minutes, 0 to 1440, with no running environment (default 30; 0 never stops on idle);
- start the machine when an environment needs it (default off);
- subnet and security group (optional);
- who may use SSH (default: the public address of this computer, looked up once at `checkip.amazonaws.com`). A range wider than /16 is refused, and `0.0.0.0/0` is not accepted.

**Show free-tier eligible types** asks AWS which instance types it reports as free-tier eligible for the account. Check the billing page for what that covers.

**Test connection** reads the account, checks the keys, the region and the image, and checks a launch with a DryRun that AWS evaluates without starting anything. Nothing is created.

Instance type, disk, image, subnet and security group cannot change while the host has a machine, because they apply when a machine is created. Terminate the machine first. The key secret names and the region are locked the same way, because the machine is looked up with those keys.

### In chat

`compute_host` can add, change and remove an EC2 host, and start, stop, extend and terminate its machine, each after an approval card. The card shows every value and the cost. A Start or Extend card differs each time, so an answer of "always" does not cover the next one.

Set on the page only: the image, subnet, security group and SSH user. Chat refuses AWS key values and accepts only secret names that are `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` or start with `AWS_`.

## What Start machine does

1. Creates an SSH key pair for the host (the private key stays on this computer), imports the public key into AWS, and creates a security group that allows SSH from one address only (this computer's, or the one typed). Everything created is tagged `cloud-compute:install` and `cloud-compute:host`.
2. Launches the instance with a client token. The token is stored on the host before the call and reused on every retry, so a retry, a lost answer or a restart does not launch a second instance. An unfinished launch is found by its token and the install tag before another is made. A claim keeps two Hermes processes from starting the same host at once. It lapses after 3 minutes, so a process that dies in the middle of a launch blocks the other for up to 3 minutes. The first-boot script sets the stop timer and installs Docker.
3. Waits for the AWS status checks (about 2.5 minutes), reads the SSH host key from the console output and pins it per instance in the host's own `known_hosts` file (without a console key, the first login records it), then waits for SSH and the container runtime.

The AWS account is recorded when the machine is launched. If the keys later belong to another account, the machine stays on the host card with a warning that it may still run and bill in the first account, and Start does not launch another one. Restore the keys, or stop the machine in the AWS console.

## Time limits and what they cover

- **Maximum lifetime.** The machine has a stop timer of its own: a systemd timer, `cloud-compute-stop.timer`, that powers the machine off. The first-boot script arms it, and also writes a small systemd unit that arms it again at every boot, with the lifetime last written to the machine. Once this computer reaches the machine over SSH after a start, the plugin replaces the timer with the exact stop time and writes the current lifetime into the unit. `InstanceInitiatedShutdownBehavior=stop` turns the power-off into a stop, so the disk stays. A scheduled `shutdown -h` is not used, because Linux then refuses every SSH login for its last five minutes. With the timer, SSH works until the machine goes down. To read it on the machine: `systemctl list-timers cloud-compute-stop.timer`.
- **While Hermes runs**, the plugin also stops the machine at the limit, resumes a start whose worker died, stops a machine that is still running after a failed or interrupted start, and retries a failed stop with a warning on the card.
- **While Hermes is closed**, only the machine's own timer stops it. Until the plugin reaches the machine, that timer can be longer than the current setting (for example after the setting was lowered); the card says how long.
- **Extend** moves the stop time. The total is capped at twice the maximum lifetime counted from the last start. A longer limit is set on the page, in the host's maximum lifetime.
- **Idle stop** stops the machine after the set minutes with no running environment.
- **Not covered.** A stopped machine still bills for its disk until it is terminated. The plugin stops a machine by itself and terminates one only on request.
- **The last minute.** In the last minute before the stop time the machine may no longer take connections: the card offers no Extend, and a command that is refused because the machine is going down says so. Start the machine again after it has stopped to reach its files; the disk is kept.

## Stop, start and terminate

- **Stop machine** stops the instance and keeps the disk. Environments are marked stopped and their files stay. Start gives the instance a new public address; the plugin handles it and the pinned host key still applies.
- **Terminate machine** deletes the instance and its disk. While environments exist, the page asks first. **Save all and terminate** deletes each environment after the same verified save as **Delete**; the machine has to be running for that. On a stopped machine it says so and offers the two choices: start the machine (AWS bills while it runs) and then save and terminate, or **Terminate without saving**. **Terminate without saving** works in every machine state: it does not contact the machine, terminates the instance (AWS deletes the disk with every container on it) and closes the records of its environments with the note "Deleted with its machine, files not saved". From chat, a terminate is refused while environments exist unless the call sets `delete_environments`; each is then saved first unless the call sets `save` to false, and the card names the environments and states which.
- **Delete one environment** on a stopped, unreachable or terminated machine: with saving it is refused with the same two choices; without saving its record is closed at once and its container is removed the next time the machine answers (when it starts, or the disk goes with a terminate). It is never adopted back as an orphan.
- **Check AWS** lists instances that carry this install's tag but belong to no host, for example after a lost response, with Stop and Terminate buttons. The host card also shows them with a warning. Nothing untracked is touched automatically.
- A host cannot be removed while it has a machine, an unfinished launch or environments. Removing a host deletes its key pair and security group in AWS (when the AWS key is still stored) and its key files on this computer.

## What is sent where

- HTTPS to `ec2.<region>.amazonaws.com`, `sts.<region>.amazonaws.com` and `ssm.<region>.amazonaws.com` for the host's region, signed with the user's key. Redirects are refused and each call times out after 30 s.
- One lookup of this computer's public address at `https://checkip.amazonaws.com` (skipped when the address is typed).
- SSH from this computer to the instance. The instance downloads Docker or Podman from its image's package repositories at first boot.
- Nothing goes to the plugin's author.
- Key values are read from the Hermes secrets at the moment of each call. They are not stored, not sent to the instance and not shown in any error text.
- Written on this computer: the plugin data folder, in `ec2/<host id>/`, with the private key and the `known_hosts` file. Written in AWS: one instance with a gp3 root volume, one key pair and one security group per host, tagged as above.

## Tested

All runs used us-east-1, a `t3.micro` and Amazon Linux 2023, from a Windows 11 computer with Windows OpenSSH.

- 2026-11-10: three short launches through the plugin's code, run from a script (about 11 instance-minutes in total). The first two each found a bug, which was fixed. The third passed every check: connection test with a DryRun launch, launch, status checks, key pinning, SSH, Docker, an environment (spawn, run a command, save the files and verify the copy, delete), stop, start again with a new address, terminate, and removal of the key pair and security group. It also extended the machine, but with the scheduled shutdown that the systemd timer replaced later.
- 2026-11-11, first run from the Compute page of a running Hermes (about 22 instance-minutes): keys by name through Hermes, launch, the machine stopping itself at its limit with Hermes closed, the card settling to Stopped, and a second boot arming the stop again. It found problems: removing the environments of a stopped machine, spawning on an EC2 host from the page, a login block before the scheduled shutdown, no confirmation before Start, and an EC2 host with no machine shown as offline.
- 2026-11-11, second run from the page after those fixes, on the code being published apart from the changes made after it (one launch, about 15 instance-minutes): wizard and connection test, confirmation dialog before Start, launch, spawn from the page, one stop timer with no login block, the machine stopping itself with Hermes closed, saving files in the last minutes, terminate without saving on a stopped machine, and host removal with the key pair and security group removed. Two problems found there were fixed afterwards and are checked by unit and page tests only: Extend offered when it could not work, and the Overview tab calling a host without a machine offline.

Not tested: Extend on a live machine with the systemd timer; the timer re-arming after a Stop and Start with the new timer; adding or starting a host from chat (the approval cards were shown and rejected, not executed); any other region, image or instance type; GPU types; AWS use from Linux or macOS; Ubuntu images.
