# The provisionkit command

`scripts/provisionkit` keeps the controller's checkout current and healthy. It uses only the Python standard library,
so it runs before the virtualenv exists.

```
provisionkit update     pull the latest main from GitHub, then make sure everything works
provisionkit check      run the health checks without changing anything
provisionkit status     version, whether an update is waiting, panel state
provisionkit version    the installed version
provisionkit install    put the command on your PATH (sudo)
```

## First time

The command arrives with the code, so pull it once by hand, then install it:

```
sudo -u provisionkit -H git -C /home/provisionkit/provisionkit pull origin main
sudo /home/provisionkit/provisionkit/scripts/provisionkit install      # links /usr/local/bin/provisionkit
```

From then on, `provisionkit update` is all you need. `sudo scripts/install_panel.sh` also installs the link.

## What `update` does

1. **Refuses to run over local work.** It stops if tracked files have uncommitted changes, if the checkout is not on
   `main`, or if local history has diverged from GitHub. Untracked and ignored files, such as `inventories/local`,
   never block it. It only fast-forwards, so it never rewrites history.
2. **Pulls `origin/main`** and lists the new commits.
3. **Fixes the environment.** It creates `.venv` if missing and installs the dependencies when
   `panel/requirements.txt` changed or an import fails.
4. **Restarts the panel when needed.** If the panel service is installed, it re-renders the systemd unit from the
   template. It restarts the service when panel code, the unit or the installer changed, or when the service is not
   running, and waits for the health check. It asks for your sudo password for this step.
5. **Rolls back if the panel does not come back healthy.** The code returns to the previous commit and the service
   restarts again. Use `--no-rollback` to keep the new code and debug it.
6. **Runs the health checks** and prints a summary.

## Health checks (`provisionkit check`)

| Check | Fails or warns when |
|---|---|
| git | tracked files have local changes (warn) |
| python environment | `.venv` or a dependency is missing (fail) |
| playbook syntax | `ansible-playbook --syntax-check` fails on any playbook (fail) |
| inventory | `inventories/local` is missing (warn) or fails the inventory validator (fail) |
| deploy key | the key is missing or unreadable (fail), open to other users or protected by a passphrase (warn) |
| panel service | not running, or running but not answering (fail) |

The exit code is 1 if any check fails, so you can use `provisionkit check` in scripts or cron.

## Options

`update --skip-env`, `--skip-service` and `--no-rollback` skip those steps. `status --no-fetch` does not contact GitHub.

## Status

Covered by 30 pytest cases that use real git repositories, with a local bare repo standing in for GitHub: fast-forward,
dirty tree, wrong branch, diverged history, unreachable remote, dependency changes, restart decisions, rollback,
key checks. The whole command was also run once from a fresh checkout (pull, create the virtualenv, install
dependencies, run every check). **Not tested:** the systemd restart path on a real machine (the sandbox has no systemd)
and the sudo re-exec. The service steps are covered by tests with the system calls replaced.
