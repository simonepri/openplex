<!-- Explains how to choose a BuildBuddy Bazel profile and how to read BuildBuddy CI results from a terminal: invocation logs, failed tests, timing profiles, and artifacts. -->

# Reading BuildBuddy Results

Use the `bb` CLI from [BuildBuddy](https://github.com/buildbuddy-io/buildbuddy) to read CI logs and failed tests, and the BuildBuddy API to read timing profiles. Both work from inside the repository, where mise provides `bb` and `mise run init` has stored your login.

**Prerequisites:** `bb login --check` exits with 0; `mise run init` performs the login.

## Choosing a BuildBuddy profile

[`src/bazel/profiles/profiles.bazelrc`](../profiles/profiles.bazelrc) defines every `--config` profile. BuildBuddy access comes from two independent choices. Pick at most one flavor, which sets the cache and the build event service:

- `bb-cloud` uses the cache, build event service, and remote downloader at `remote.buildbuddy.io`.
- `bb-cloud-proxy` extends `bb-cloud` and reads the cache through the in-cluster enterprise cache proxy; adding it on top of `bb-cloud` still counts as one flavor.
- `bb-community` uses the in-cluster community BuildBuddy cache; each deployment sets its own build event service.

Then pick at most one execution profile, which sends actions to remote executors. Neither combines with `bb-community`:

- `bb-rbe-cloud` runs actions on BuildBuddy cloud executors.
- `bb-rbe-sh` runs actions on self-hosted executors through the `//src/bazel/runners:sh_cpu` and `:sh_gpu` platforms and turns on `--//src/bazel/runners:gpu_runners`.

`ci` only tunes Bazel for CI machines and contacts no BuildBuddy service on its own. Configs whose names start with `_` are building blocks for the profiles above; never select them directly.

The root `.bazelrc` selects no BuildBuddy profile by default; openplex leaves this choice to downstream repositories and developer overrides. Each site adds profiles on top of any root default, and `//src/bazel/profiles` rejects any site whose combined selection is invalid:

| Site | Adds | Effective selection |
|---|---|---|
| Laptop | `--config=bb-cloud` (optional) | `bb-cloud` |
| `buildbuddy.yaml`, all actions | `--config=ci --config=bb-cloud` | `bb-cloud`, `ci` |
| Coder, cell label `buildbuddy.io/mode: cloud` | `--config=bb-cloud`, or `--config=bb-cloud-proxy` with `buildbuddy.io/enterprise-proxy: enabled`; plus `--config=bb-rbe-sh` unless `buildbuddy.io/executors` is `none` | `bb-cloud`, optionally `bb-cloud-proxy` and `bb-rbe-sh` |
| Coder, cell label `buildbuddy.io/mode: community` or unset | `--config=bb-community` | `bb-community` |
| GitHub Actions CI | `--config=ci` | `ci` |

A test that needs a GPU opts in through the platform constraint, so it runs only where GPU runners exist and is skipped elsewhere:

```starlark
exec_compatible_with = ["//src/bazel/runners:gpu"],
target_compatible_with = select({
    "//src/bazel/runners:gpu_runners_enabled": [],
    "//conditions:default": ["@platforms//:incompatible"],
}),
```

## How CI invocations nest

Each CI action in `buildbuddy.yaml` (`Format`, `Lint`, `Test`, `Review`) is a workflow invocation. Its log is the runner console, and GitHub commit statuses link to it. Every Bazel command inside the action streams to its own child invocation, announced in the workflow log by a `Streaming build results to: <url>` line. Build errors, test results, and timing profiles live in the child invocations; the workflow invocation has none of them.

## Find a commit's CI runs

```sh
gh api repos/{owner}/{repo}/commits/{sha}/statuses --jq '.[] | "\(.context)\t\(.state)\t\(.target_url)"'
```

Each status links to a workflow invocation.

## Read a log

```sh
bb view <invocation-id-or-url>
bb view <workflow-id> | grep -o 'invocation/[0-9a-f-]\{36\}' | sort -u
```

The first command prints a full log. The second lists the child invocations of a workflow invocation.

## Diagnose a failure

```sh
bb view <child-id> --errors
bb view <child-id> //path/to:failed_test
```

`--errors` prints the first build error of a child invocation. A test label prints that test's log. Both return nothing on a workflow invocation.

## Find slow actions

Fetch the critical path summary and the timing profile of a child invocation through the API. Keep the key in a shell variable so it is never printed:

```sh
key=$(git config buildbuddy.api-key)
curl -sS -H "x-buildbuddy-api-key: ${key}" -H 'Content-Type: application/json' \
  -d '{"selector":{"invocation_id":"<child-id>"},"include_build_tool_logs":true}' \
  https://app.buildbuddy.io/api/v1/GetInvocation
```

`invocation[0].buildToolLogs` holds a base64 `critical path` entry, which lists the actions that bounded wall time, and a `command.profile.gz` entry with a `uri`. Download the profile, the same data as the Timing tab, by posting that URI:

```sh
curl -sS -H "x-buildbuddy-api-key: ${key}" -H 'Content-Type: application/json' \
  -d '{"uri":"<command.profile.gz uri>"}' \
  https://app.buildbuddy.io/api/v1/GetFile -o profile.gz
```

The profile is a gzipped Chrome trace. Sort its `traceEvents` entries with `"ph":"X"` and `"cat":"action processing"` by `dur`, in microseconds, to rank actions; total `dur` per `cat` to separate action time from repository fetching and analysis.

## Download artifacts

`bb download artifacts <invocation-id-or-url> --output_directory=<dir>` downloads every file attached to an invocation. Use it to retrieve outputs that only exist in CI, such as a built binary or a patch produced by a remote run. It fetches files one by one, so a full CI run takes many minutes; read logs with `bb view` instead.

## Keep the API key private

`bb login` stores your personal API key in `.git/config` under `buildbuddy.api-key`. Plain `bazel` reads it from there through the credential helper in [`src/bazel/tools/buildbuddy`](../tools/buildbuddy), and the root `.bazelrc` selects no key by default, so the key never lands in a bazelrc file. Never print `.git/config` or the `buildbuddy` git config section; check the login with `bb login --check`.
