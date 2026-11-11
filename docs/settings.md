# Settings

Set them under **Settings, Own Compute** in Hermes Desktop or the Webapp, or in the Hermes configuration as `plugins.entries.cloud-compute.settings.<name>`. A saved value applies to the next read; no restart is needed. The same list is in `plugin.yaml` (`config_schema`).

| Setting | Type, default | Range or choices | Meaning |
|---|---|---|---|
| `liveness_seconds` | int, 15 | 5 to 3600 | Seconds between quick reachability checks of each host |
| `poll_seconds` | int, 60 | 30 to 3600 | Seconds between full status polls of instances, jobs and resources |
| `tunnel_ttl_minutes` | int, 60 | 5 to 1440 | Minutes before an idle SSH tunnel is closed |
| `default_terminal` | str, `auto` | `auto`, `wt`, `cmd`, `macos`, `linux`, `copy` | How to open a terminal on this computer. `auto` picks the usual terminal for the operating system. `wt` and `cmd` are for Windows, `macos` for macOS, `linux` for Linux; a value meant for another system behaves like `auto`. `copy` only puts the command on the clipboard |
| `default_idle_minutes` | int, 60 | 5 to 1440 | Default idle timeout for new environments and cloud agents. A template or request can override it |
| `default_ttl_minutes` | int, 240 | 5 to 10080 | Default maximum lifetime for new environments and cloud agents. A template or request can override it |
| `chat_context` | bool, true | | Tell the model which environments are online |
| `chat_context_max_chars` | int, 600 | 100 to 4000 | Size limit of that summary |
| `approval` | str, `always` | `always`, `remote-only` | When model-initiated actions need approval. `remote-only` skips the card for hosts on this computer, except for deleting |
| `notify_job_complete` | bool, true | | Show a notification when a Slurm job finishes |
| `ssh_path` | str, empty | | Path of the `ssh` executable; empty uses the one on the PATH |
| `use_asyncssh` | bool, false | | Use the `asyncssh` library instead of the `ssh` executable, when installed |
| `expiry_warning_minutes` | int, 15 | 0 to 1440 | Warn this long before an environment reaches its time limit; 0 is no warning |
| `auto_delete_days` | int, 0 | 0 to 365 | Delete environments stopped for this many days, after saving their files; 0 is never |
| `export_max_mb` | int, 2048 | 10 to 102400 | Largest file export when saving an environment's files |
| `exports_dir` | str, empty | | Folder on this computer for saved copies; empty uses the plugin's data folder. A folder inside the Hermes home or the SSH folder is refused |
| `agent_base_url` | str, empty | | OpenAI-compatible base URL for cloud agents, for example `https://llm.example.net/v1`. Used when the template sets none. Plain `http` only on a private network you trust |
| `agent_model` | str, empty | | Model id for cloud agents. Used when the template sets none |
| `wait_max_seconds` | int, 300 | 10 to 1800 | Longest time a chat tool waits for a job or an agent run before returning |
| `agent_api_key` | secret | | Key for the default cloud agent model, saved as the Hermes secret `CC_AGENT_API_KEY` in the active profile. Sent only into cloud agent sandboxes, after approval |

Optional secrets the plugin declares: `CC_AGENT_API_KEY` and `HF_TOKEN` (a Hugging Face token passed into environments whose template uses the variable `HF_TOKEN`). On Hermes installs with several profiles, store the secret in the profile that runs Hermes.
