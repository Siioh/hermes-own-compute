# Slurm fixtures

Captured from the containerized test cluster (Slurm 26.05.5, partitions `cpu`
(default, nodes c[1-2]) and `gpu` (no nodes)) by running the commands inside
the `slurmctld` container as root.

| File | Command | Notes |
|---|---|---|
| `squeue_running.txt` | `squeue -h --me -o '%i\|%T\|%P\|%N\|%S'` | job 7 running, array 8 with task 1 running and `8_[2-3%1]` pending |
| `sacct_running.txt` | `sacct -X -P -n -j 5,6,7,8 -o JobID,State,ExitCode,Start,End,NodeList` | same moment; `Unknown` end times, `None assigned` nodes |
| `sacct_finished.txt` | same as above, later | 5 completed (`hostname`), 6 failed (`exit 3`), 7 `CANCELLED by 0`, array 8 tasks 1-3 completed |
| `sacct_with_steps.txt` | same without `-X` | includes `.batch` steps that the parser must skip |
| `squeue_epoch.txt` | `env SLURM_TIME_FORMAT=%s squeue -h --me -o '%i\|%T\|%P\|%N\|%S'` | job 18 running; times are epoch seconds |
| `sacct_epoch.txt` | `env SLURM_TIME_FORMAT=%s sacct -X -P -n -j 12,13,14,15,18 -o JobID,State,ExitCode,Start,End,NodeList` | epoch seconds; `Unknown` end for running job 18 |
| `sinfo_partitions.txt` | `sinfo -h -o '%P\|%a\|%l\|%D\|%C\|%m\|%G'` | |
| `sinfo_nodes.txt` | `sinfo -h -N -o '%N\|%P\|%C\|%m\|%e\|%T'` | |
| `scontrol_nodes.txt` | `scontrol show node -o` | idle cluster (CPUAlloc=0, AllocMem=0). Written by hand in the documented one-line-per-node format with the real node values from `sinfo_nodes.txt` (not captured live). |
| `scontrol_nodes_busy.txt` | `scontrol show node -o` | c1: CPUAlloc=2 AllocMem=1024, c2: CPUAlloc=1 AllocMem=512 (same provenance). |
