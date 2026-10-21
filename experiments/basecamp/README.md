# Original Basecamp Rust application

Run the unmodified [once-campfire-rust](https://github.com/basecamp/once-campfire-rust/tree/6dae2fd3c1a2dce61ca11beb731bcce2b42b67ad) beside the three workerd modes. The original application keeps its HTTP front end, authentication, SQL, rich-text rendering, and cache implementation.

## Build

Use macOS with Git, Xcode Command Line Tools, Python 3.11+, Rustup, pkg-config, and libvips. The [workerd build](../workerd/README.md#build) also requires Bazelisk. On Homebrew, the native media dependency is available with `brew install pkg-config vips`.

From the repository root:

```sh
python3 experiments/workerd/build_workerd.py
python3 experiments/basecamp/build_basecamp.py
```

The Basecamp builder fetches the revisions in [`basecamp.json`](basecamp.json), initializes the Rails reference submodule, and builds the original application with Rust 1.98.1 and its upstream release profile. It writes `.build/basecamp-host/campfire` and a manifest of source hashes, executable hashes, and native dependency versions. `PKG_CONFIG_PATH` can select a local libvips installation.

The recorded executable links libvips 8.16.1 and GLib 2.88.1. Its libvips build disables optional codecs and modules; the text requests measured here do not initialize libvips or use media handlers. The exact feature list is in the [recorded manifest](../../results/2026-10-21-basecamp/sources/.build/basecamp-host/build.json).

## Run the comparison

Ports **19880**, **19881**, and **19882** must be free. Stop build processes before measuring, including the Bazel server started by the workerd build:

```sh
(cd .build/workerd-source && bazelisk --output_user_root="$PWD/../workerd-bazel" shutdown)
python3 experiments/basecamp/run.py compare
python3 experiments/workerd/report.py .runs/workerd > .runs/workerd/REPORT.md
python3 experiments/basecamp/report.py .runs/basecamp --workerd .runs/workerd > .runs/basecamp/REPORT.md
python3 experiments/workerd/report.py .runs/workerd --check
python3 experiments/basecamp/report.py .runs/basecamp --workerd .runs/workerd --check
```

`compare` prepares 50 rooms with 1,000 messages each, logs in through the original session endpoint, and validates pages, cache invalidation, message writes, and persistence after restart. It runs three 5-second trials with 32 HTTP clients, rotating the order of Basecamp and the workerd modes for matching read/post requests. It also runs workerd's JSON, mixed, and WebSocket cases, the original full room page, and ten fresh processes per executable. `--seconds`, `--rounds`, and `--cold-rounds` change these durations and counts.

Every trial restores its seed. Reads check the complete response against an independently validated preflight page. Every acknowledged post is checked in the original message, rich-text, and search tables after shutdown. CPU and memory cover the complete server process. The runner stops the processes it starts; local state, sessions, logs, and measurements remain under `.runs/basecamp/` and `.runs/workerd/`.

## Run the original application alone

```sh
python3 experiments/basecamp/run.py prepare
python3 experiments/basecamp/run.py validate
python3 experiments/basecamp/run.py bench --seconds 5 --rounds 3
python3 experiments/basecamp/run.py cold --cold-rounds 10
```

The comparison report requires a matching workerd session; use `compare` for that report.

## Verify the recorded results

Python 3.11+ can verify the published data and source snapshots on any operating system:

```sh
python3 experiments/workerd/report.py --check
python3 experiments/basecamp/report.py --check
```

The [original application report](../../results/2026-10-21-basecamp/REPORT.md) explains the differences between the applications. Basecamp returns 40 rich-text messages per page and uses one database; the Workers chat returns 50 plain-text messages and uses one database per room. Their SQL, caching, authentication, response sizes, and write durability also differ. These are whole-application measurements; their ratios do not isolate Workers/DO scheduling overhead.
