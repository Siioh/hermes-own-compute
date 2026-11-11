# Cloud agents

A cloud agent is a headless Hermes agent that works on a git branch of a repository on this computer, inside a sandbox on one of the hosts. It runs on the user's own machine, not in Hermes Cloud or any hosted service. The task goes in; the result comes back as a branch.

## What it needs

1. **A model that can be reached from the sandbox.** An OpenAI-compatible endpoint from an API-key provider, or a model server the user runs. A provider that signs in with an account (a subscription login) cannot be used: the sign-in lives on this computer and cannot be sent to a remote sandbox. The model list in the run dialog shows such providers as not available, with the reason.
2. **A key stored as a Hermes secret.** By default the secret is `CC_AGENT_API_KEY`, saved from **Settings, Own Compute, Cloud agent model key**. A model that the Hermes profile already offers uses the key secret named in the profile. Use a scoped key with a spending cap. The key is the only secret a cloud agent sandbox gets.
3. **A host that allows agents.** In the host's sandbox defaults, **Allow cloud agents** (off by default). Slurm hosts cannot run agents.
4. **`git` on this computer**, and a git repository to work on.
5. **A model that is loaded.** A model server that unloads when idle can fail the first call. Warm it before a run.

A plain `http` endpoint is meant only for a private network the user trusts; use `https` otherwise. An endpoint that points at this computer cannot be reached from a sandbox on another host.

## Choosing the model

In **Agents, New agent run...**, the **Model** list offers:

- **Default model:** the saved default choice, else the endpoint and model set in Settings (`agent_base_url`, `agent_model`).
- A model the Hermes profile offers, when its key is stored under a secret name.
- A choice saved earlier.
- **Another endpoint...:** an endpoint, a model id and the name of the key secret. **Save this choice** keeps it for later runs.

A template can name its own endpoint and model in its Agent section; that wins over the settings. A chat request (`cloud_agent_spawn`) cannot pick an arbitrary URL: it uses the settings, the template or a saved choice.

## Running one

1. **Agents, New agent run...** Give the task, optionally a "done when" condition, the host, the repository path on this computer, a new or existing branch with a base, the template and the maximum number of turns (default 30).
2. The approval card (when Hermes approvals are on) shows the template, host, model, endpoint, turn limit, repository path and task, and says that the key secret goes into the sandbox. A handoff test sends no secrets.
3. The first run on a host builds the agent image there (several gigabytes; the base is the Hermes agent image pinned by digest). Later runs reuse it.
4. The repository branch is packed as a git bundle and sent to the sandbox. The agent works on a copy.
5. The run card shows *starting*, *running*, then *done* or *failed*, the agent's reply, the model and the number of turns. It updates by itself.

The sandbox runs the Hermes agent with Hermes approvals on and without YOLO mode: commands Hermes flags as dangerous are refused and the run continues. The sandbox gets no push credentials: work leaves it only through **Get results**.

## Getting the work back

- **Get results** imports the agent's commits as the local branch `refs/heads/cloud/<run>/<branch>` and checks it out in a sibling worktree next to the repository (`<repo name>-<run id>`). No existing branch moves and nothing is merged. Commits made in the sandbox are authored by `Own Compute agent <agent@cloud-compute.invalid>`. After more commits, **Get new results** appears.
- **Send a message** continues the same sandbox, branch and Hermes session once the turn ended. The follow-up keeps the run's turn limit.
- **Run again...** fills the dialog from the run; the model or host can be changed first.
- **Stop** fetches the commits you do not have yet, saves `/work` to this computer, then removes the sandbox. If saving fails, the sandbox is kept.
- **Remove** deletes the record of a run that is not active. Branches and worktrees already fetched stay.

Saved copies of an agent sandbox leave out the agent's own state folders (its Hermes home and run state), so they hold the work only.

## When a run fails

The card names the cause in one sentence and says what to do. Examples:

- "The model endpoint ... answered 404: model ... was not found there (or the endpoint address is wrong)." Check the model id and the endpoint, then **Run again...**.
- "No model is set up for cloud agents." Add one in **New agent run, Model** or in Settings.
- "variable CC_AGENT_API_KEY contains a line break." The stored key has a stray line break; store it again.
- "The run did not start": the cause is in the error text; fix it and start again with the same arguments.
- A used-up turn budget is reported with the limit; run again with a higher one.

A failed run keeps its sandbox until it is stopped.

## Chat tools

`cloud_agent_spawn`, `cloud_agent_status` (with `wait_s` to wait for the end of the turn), `cloud_agent_message`, `cloud_agent_rerun`, `cloud_agent_collect` and `cloud_agent_stop`. Each that starts, messages, re-runs or stops needs approval when Hermes approvals are on. The chat model cannot run commands in an agent sandbox and cannot open a terminal into it, because the sandbox holds the model key; `env_exec` on such a sandbox is refused.

## Limits

- The model key is in the environment of the sandbox. A command the agent itself runs there can read it, and so can a process in the sandbox. Use a key with a spending cap and a narrow scope, and treat it as exposed to the model that runs the agent.
- The agent's session is not imported into local chat history, and there is no token or cost accounting; usage accrues on the endpoint.
- Slurm hosts cannot run agents.
- The handoff test template needs no model: it commits a marker file to show the round trip.
