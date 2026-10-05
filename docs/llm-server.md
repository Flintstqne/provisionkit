# Local LLM server (llm_server role)

Runs llama.cpp's `llama-server` on a node in the `llm_servers` group. CPU only, no hosted API, nothing leaves the lab.
Built for `pk-worker` (Core i5-8400H, 15 GB RAM). The controller Pi 4 (4 GB) is too small and holds the deploy key, so
it stays a client.

## Design

- **Pinned and verified.** The role needs a release archive and a GGUF model, each with a SHA-256 you supply. Downloads
  that do not match are rejected and removed. There are no default URLs or hashes on purpose.
- **Loopback only.** The role refuses any bind address other than 127.0.0.1 or ::1 and checks with `ss` after start
  that nothing listens elsewhere. Reach it from the controller through an SSH tunnel:
  `ssh -N -L 8081:127.0.0.1:8081 -i <deploy key> provisionkit@pk-worker`. LAN access waits until the firewall role has
  been applied and tested on the node.
- **API key.** `LLAMA_API_KEY` lives in `/etc/llm-server/env` (mode 0600, root). The key never appears on a command
  line.
- **Sandboxed service.** Dedicated `llm` user with no shell, no capabilities, read-only filesystem apart from its own
  state directory, a system call filter, and `IPAddressDeny=any` with `IPAddressAllow=localhost`, so the process cannot
  open outbound connections. CPU weight, nice level and a memory cap keep SSH and Ansible responsive.
- **Fails early.** Before touching the running service the role checks the inputs, the RAM, that exactly one
  `llama-server` is in the archive, and that the binary's `--help` lists every flag the unit uses.

## Use

```
# 1. On a machine with internet access: hash the files and print the variables.
scripts/pin_llm_artifacts.py --binary-url <llama.cpp linux x64 release archive> --model-url <model .gguf>

# 2. Put the output and a 32+ character llm_server_api_key (Vault) in
#    inventories/local/group_vars/llm_servers.yml. Template: inventories/example/group_vars/llm_servers.yml

# 3. Deploy to the canary node, then measure.
ansible-playbook playbooks/llm.yml --limit pk-worker --ask-vault-pass
ansible-playbook playbooks/llm_bench.yml --limit pk-worker --ask-vault-pass
```

`llm_bench.yml` sends the same prompt several times to the local server and writes median generation speed, CPU,
threads, context size and model name to `panel/instance/bench/`. Use those files for the README table. Tune
`llm_server_threads` against them (physical cores usually win).

## Status

Tested here (sandbox, localhost, real ansible-core 2.19, fake archive and model): input assertions, checksum
verification and rejection, extraction, binary lookup, flag check, file modes and ownership, a second run with no
changes, unit template rendering and `systemd-analyze verify` syntax. 4 pytest cases for the pin script.

**Not tested:** a real `llama-server` binary, the systemd service start, the health and loopback checks, the benchmark
playbook, anything on `pk-worker`. The sandbox could not reach GitHub or Hugging Face. The role assumes the pinned
`llama-server` reads `LLAMA_API_KEY` and supports `--no-webui`. The flag check catches a missing flag, but not the
environment variable. Treat the first run on `pk-worker` as the real test.
