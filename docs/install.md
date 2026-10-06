# Install

## One command

```
curl -fsSLO https://raw.githubusercontent.com/Flintstqne/provisionkit/main/scripts/install.sh
less install.sh
bash install.sh
```

Run it as your normal user on the controller (Debian or Ubuntu, Python 3.11 or newer, systemd). It uses `sudo` for system
packages and the service. Each step is safe to repeat.

| Step | What happens |
|---|---|
| Prerequisites | Checks git, Python 3.11+, venv, ssh-keygen, curl. Offers to install what is missing with apt. |
| Checkout | Uses the checkout it runs from, an existing one in `--dir`, or clones the repository. |
| Environment | Builds `.venv` and installs the panel requirements and `ansible-core<2.20`. |
| Deploy key | Creates `~/.ssh/provisionkit_ed25519` (ed25519, no passphrase, mode 600) if it does not exist. |
| Inventory | Creates `inventories/local` for this machine: the controller, no managed nodes, the controller as the only management source, the deploy key and your admin key filled in. Validates it. |
| Admin and service | Creates the first admin with a generated password (shown once), installs the hardened systemd units, the `provisionkit` command and the update timer, and starts the panel on `127.0.0.1:8080`. |

Options: `--dir`, `--admin NAME`, `--admin-key KEY`, `--address IP`, `--port N`, `--no-service`, `--yes`, `--check`, `--repo URL`.
`--check` only reports what is missing. `--no-service` skips systemd and needs no sudo, then you start it with
`.venv/bin/python -m panel run`.

The admin key is the public key you log in to your servers with. By default the installer takes the first key in
`~/.ssh/authorized_keys`. If there is none, it stops and asks for `--admin-key`.

The deploy key has no passphrase because Ansible runs from the panel and from timers with nobody there to type one. It opens
every node you add, so keep the controller account locked down. This is risk 1 in [threat-model.md](threat-model.md).

## After installing

1. Reach the panel: `ssh -L 8080:127.0.0.1:8080 YOU@CONTROLLER`, then open http://127.0.0.1:8080. For access from anywhere,
   follow [cloudflare-tunnel.md](cloudflare-tunnel.md).
2. Devices, Add device. The device page shows a checklist, including the one command that bootstraps the server.
3. Preview and apply the baseline from the device page, or use `provisionkit baseline NODE`
   (see [operations.md](operations.md) and [baseline.md](baseline.md)). The firewall role runs only from the command line.

## Try it without installing

```
bash scripts/demo.sh
```

Synthetic data and simulated jobs. No sudo and no servers.

## By hand

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r panel/requirements.txt "ansible-core<2.20"
ssh-keygen -t ed25519 -N '' -C provisionkit-deploy -f ~/.ssh/provisionkit_ed25519
python -m panel init-inventory --auto --deploy-key ~/.ssh/provisionkit_ed25519
python -m panel create-user alice --role admin
sudo scripts/install_panel.sh --user "$USER" --skip-admin
```

`scripts/install_panel.sh` alone is the service installer. It accepts `--admin NAME --admin-password-file FILE` for scripted use.
