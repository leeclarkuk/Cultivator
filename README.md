# Cultivator

Cultivator deploys modules from declarative manifests, the way a GitOps controller would. You describe modules and their dependencies in YAML, keep them in git, and Cultivator works out what has changed since the last successful apply and runs only that, in dependency order, in parallel where it can.

It is a small Python CLI with one dependency (PyYAML). It does not know what your modules deploy. Each module supplies its own commands (`kubectl`, `terraform`, `make`, a shell script), and Cultivator decides when to run them.

Cultivator has no cloud provider, platform or tool built in. Anything with a command line works: Terraform or OpenTofu, Pulumi, Ansible, Helm, `kubectl`, `az`, `gcloud`, `aws`, Docker, or your own scripts. Mix them freely in one workspace.

## Install

```sh
pip install -e .          # Python 3.11 or later
pip install -e '.[dev]'   # adds pytest
```

## Quick start

```sh
cultivator init .         # scaffolds cultivator.yaml and modules/hello
cultivator plan           # what would change
cultivator apply          # do it
cultivator apply          # second run: nothing to do
```

A larger worked example lives in `examples/webshop`.

## Manifests

The root `cultivator.yaml` defines environments and where modules live.

```yaml
apiVersion: cultivator/v1
kind: Config
spec:
  modules: [modules/*]          # globs of directories holding a module.yaml
  defaultEnvironment: staging
  parallelism: 4
  stateDir: .cultivator/state   # default
  vars:                         # shared by all environments
    site: primary
  environments:
    staging:
      vars: { replicas: 1 }
    production:
      vars: { replicas: 3 }
```

Each module has a `module.yaml`.

```yaml
apiVersion: cultivator/v1
kind: Module
metadata:
  name: api
spec:
  dependsOn:
    - database                  # changes in database trigger api
    - name: cache
      propagate: false          # order only: cache changes do not trigger api
  watch: ["src/**", "../../shared/**"]   # default: everything in the module directory
  ignore: ["**/*.md"]
  inputs:
    replicas: ${vars.replicas}
  env:
    LOG_LEVEL: info
  apply:
    - ./deploy.sh ${inputs.replicas}
    - [kubectl, rollout, status, deploy/api]   # a nested list is run directly, without a shell
  verify: ./smoke-test.sh       # runs after apply; failure fails the module
  check: ./is-healthy.sh        # used by --refresh to detect drift
  destroy: ./teardown.sh        # run when the module is removed from the manifests
  timeout: 300                  # seconds, per command
  retries: 2                    # retries per phase (apply, verify)
  overrides:
    production:
      inputs: { replicas: 5 }
```

Interpolation covers `${vars.x}`, `${inputs.x}`, `${module.name}`, `${module.path}` and `${environment}`. Any other `${...}` is left for the shell. Unknown fields are errors, so typos are caught by `cultivator validate`.

Commands run in the module directory with `CULTIVATOR_ROOT`, `CULTIVATOR_ENVIRONMENT`, `CULTIVATOR_MODULE` and `CULTIVATOR_INPUT_<NAME>` set.

## How change detection works

Each module gets a fingerprint: a SHA-256 over its resolved spec (inputs, env, `apply`, `verify`), the contents of its watched files, and the fingerprints of the dependencies it propagates from. A successful apply records the fingerprint in the state file for that environment. A module is out of date when its current fingerprint differs from the recorded one.

Consequences:

- Edits that do not change meaning (comments in a manifest, files outside `watch`) do nothing.
- Dependents are redeployed because their dependency's fingerprint is part of theirs, so a change fans out through the graph without a separate traversal.
- A failed apply leaves the old fingerprint in place, so the next run retries it and anything downstream.
- `plan` tells you why: `3 files changed (a.py, b.py, c.py)`, `inputs changed: replicas`, `dependency changed: database`.

## How it saves work

- Unchanged modules are skipped entirely.
- Modules in the same stage of the graph run in parallel, up to `parallelism` or `--jobs`.
- File hashes are cached by mtime and size in `.cultivator/cache`, so replanning a large tree is cheap. Files modified in the last two seconds are never cached.
- `propagate: false` keeps ordering without forcing redeploys.
- `--target api` deploys `api` and only the dependencies that need it.
- A failure skips the dependents of the failed module and, by default, stops starting new work. `--keep-going` continues with unrelated branches.

## Commands

| Command | Purpose |
| --- | --- |
| `init [dir]` | Scaffold a workspace. |
| `validate` | Check manifests and the graph for cycles and unknown references. |
| `graph [--format text\|dot\|json]` | Show stages and the longest dependency chain. |
| `plan` | Show what would change and why. `--format json`, `--detailed-exitcode` (exit 2 on changes). |
| `apply` | Plan, confirm, deploy. `-y` skips the prompt, `--dry-run` stops after the plan. |
| `watch` | Reconcile on a loop. `--pull` runs `git pull --ff-only` first, `--interval`, `--once`. |

Common options: `-e ENV`, `-C DIR`, `-t MODULE`, `--since REF`, `--refresh`, `--force MODULE`, `--all`, `--no-prune`, `-j N`, `--keep-going`, `-v`.

Without `--yes`, `apply` asks for confirmation and refuses to run on a non-interactive terminal.

### Stateless runs in CI

`plan --since origin/main` ignores recorded state and treats modules with files changed against that git ref (working tree and untracked files included) as changed, then follows propagating dependencies. A change to `cultivator.yaml` marks everything. Use it where no state file persists between runs.

### Drift

Give a module a `check` command and run `plan --refresh`. A non-zero exit marks the module as drifted and it is redeployed.

### Removal

A module that is in the state file but no longer in the manifests is destroyed using the `destroy` commands recorded when it was applied, dependents first, and only after all deploys in the run have succeeded. `--no-prune` and `--target` disable this.

## State and locking

State is JSON in `.cultivator/state/<environment>.json` (gitignored by `init`), written atomically after each module succeeds. A lock file per environment prevents concurrent runs; a lock left by a dead process on the same host is reclaimed. Point `stateDir` at shared storage if several machines apply to the same environment, and note that the lock is only as reliable as that filesystem.

## Limitations

- Cultivator runs whatever the manifests say, with your privileges. Treat manifests like code and review them as such.
- No rollback. Failed modules stay at their last recorded fingerprint and are retried.
- Secrets are not managed. Pass them through the environment of the process running Cultivator.
- Interrupting a run (Ctrl-C) waits for commands already started to finish, then records their results.
- The hash cache trusts mtime and size, as git does. A same-size edit that preserves the mtime of an old file would be missed; delete `.cultivator/cache` if you suspect that.
- `watch` retries failed modules on every pass. It has no backoff.

## Development

```sh
pytest
```

## Licence

Apache 2.0, see `LICENSE`.
