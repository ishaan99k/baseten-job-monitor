# Baseten Job Monitor

One macOS menu bar item for [SwiftBar](https://github.com/swiftbar/SwiftBar) that shows three things in a single dropdown:

- **Baseten** — your [Baseten](https://www.baseten.co/) training jobs, polled every minute.
- **AWS Dev Boxes** — your EC2 dev boxes, with CPU, RAM, disk, and GPU usage and a table of what is running on each one, streamed over SSH every 10 seconds by default (set it to 2 seconds for an `htop`-like live view).
- **This Mac** — the same usage bars and process table for the machine you are sitting at.

The plugin is `monitor.py`. Click any section header (a box, the Mac, or the Baseten line) to pin it to the menu bar; click it again to unpin and go back to the combined summary.

`baseten.1m.py` is the original Baseten-only plugin. Install it instead of `monitor.py` if you only want job status and nothing else.

## Features

### Baseten training jobs

- Live job status in your menu bar (refreshes every 1 minute)
- Color-coded status dots: green running, orange starting, red failed, grey stopped or completed
- GPU count summary across active jobs
- Launcher and elapsed runtime for each active job
- Click any job to open it in the Baseten dashboard
- Open the corresponding Weights & Biases run directly from the job menu
- Copy job IDs to clipboard
- Browse all training projects

### Menu bar title

- With nothing pinned the title is a compact summary of all three, e.g. `BT: 12 running (12 GPUs)  ·  EC2: 2/2 up  ·  Mac: CPU 17%`.
- Pin a dev box and the title becomes its instance type, short name, and live CPU, RAM, and VRAM, e.g. `g6.4xlarge  ·  Ishaan-exte…  ·  CPU 21%  ·  RAM 35%  ·  VRAM 6%`.
- Pin the Mac to see `Ishaan’s MacBoo…  ·  CPU 17%  ·  RAM 68%`, or pin Baseten to see just `BT: 12 running (12 GPUs)`.
- The pinned item shows a checkmark in the dropdown. **Pin to menu bar** / **Unpin from menu bar** are also in each item's submenu.

### AWS dev boxes (optional)

- Per-box state dot (green running, orange pending, grey stopped, red impaired), instance type, uptime, public and private IP.
- Usage inside each box's menu, updated every 10 seconds by default (see **Usage refresh** below): CPU % with core count and load average, RAM, swap (when in use), disk on `/`, and on GPU boxes each GPU's utilization, VRAM, and temperature from `nvidia-smi`. A compact `CPU 32% · RAM 41% · GPU 87%` summary sits on the box's main line. Rows turn orange above 70% and red above 90%.
- A process table under the usage bars shows what is actually running on the box, grouped by project and command rather than by process, e.g. `822% cpu 6.5% mem 18m  10× [ocr-bench-scoring] python -m ocr_bench.cli rescore --datasets …`. The project label comes from the virtualenv or repo directory in the command path. Identical workers collapse into one row with a count, long paths are shortened, and processes running as another user are marked. GPU boxes also list which process holds how much VRAM.
- Each box keeps one persistent SSH session open for the metrics stream. If the connection drops (laptop sleep, network change) it reconnects by itself, and stale readings are labelled.
- Clicking a box pins it to the menu bar (clicking the pinned one unpins it). SSH, htop, SSM, and the AWS console are options in the box's submenu.
- Submenu actions: open `htop` in Terminal, open in the AWS console, connect via SSM or SSH, copy the IP or instance ID, start a stopped box, or stop a running one (with a confirmation dialog)
- A "Sign in" action appears in the menu when your AWS session has expired. If your profile is set up for IAM Identity Center it runs `aws sso login`, which opens your organization's own start page; otherwise it runs `aws login`.
- If your organization only allows sign-in through its Identity Center start URL (something like `https://xxx.awsapps.com/start`), use **Set up SSO sign-in (aws configure sso)…** under the sign-in prompt. That runs the AWS CLI's own wizard, which asks for the URL and region and writes them to your `~/.aws/config`. The plugin never stores or hardcodes that URL; it only reads your profile to decide which login command to run.

### This Mac

- Computer name and chip, with CPU (from `top`, a real one-second sample), RAM, and disk on the data volume, in the same bar style as the dev boxes. The RAM bar shows **memory pressure** (from `memory_pressure`), not raw used/total, because macOS deliberately keeps "used" near 100% with compressed and cached pages; the used/total figure is still printed next to it.
- The same grouped process table, so you can see what Cursor, your training scripts, or anything else is doing locally.
- **Open Activity Monitor** in the submenu.

### Usage refresh

The **Usage refresh** item at the bottom of the dropdown picks how often usage is sampled on the boxes and on the Mac, and how often the menu redraws: every 2, 5, 10, 30, or 60 seconds. The default is 10 seconds, which keeps the load on the boxes low. Pick 2 seconds when you want to watch something closely, and use the **Open htop** action when you need real-time detail. The choice is saved as `BTMON_USAGE_INTERVAL` in the config file.

## Prerequisites

- macOS
- Python 3.9+ (the macOS system Python works)
- [SwiftBar](https://github.com/swiftbar/SwiftBar)
- For the Baseten section: [Truss CLI](https://docs.baseten.co/truss/overview) installed and authenticated. Without it the section just shows an error; the rest keeps working.
- For AWS dev boxes: [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) installed and signed in (`aws login` or `aws sso login`)

## Installation

### 1. Install SwiftBar

```bash
brew install --cask swiftbar
```

On first launch, SwiftBar will ask you to choose a **plugin directory** (e.g. `~/swiftbar-plugins`). Remember this path.

### 2. Install and authenticate the Truss CLI

```bash
pip install truss
truss login
```

Make sure `truss` is on your PATH:

```bash
which truss
```

### 3. Add the plugin

Clone this repo and symlink (or copy) `monitor.py` into your SwiftBar plugins directory. (Use `baseten.1m.py` instead if you only want Baseten job status.)

```bash
git clone https://github.com/ameya005/baseten-job-monitor.git
ln -s "$(pwd)/baseten-job-monitor/monitor.py" ~/swiftbar-plugins/monitor.py
chmod +x ~/swiftbar-plugins/*.py
```

### 4. Refresh SwiftBar

Click the SwiftBar icon in the menu bar and select **Refresh All**, or restart SwiftBar. You should see something like **BT: idle  ·  Mac: CPU 12%** in the menu bar (the AWS part appears once you set it up).

### 5. (Optional) Add your AWS dev boxes

Open the dropdown, then under **AWS Dev Boxes** click **Set up AWS dev boxes…**. A Terminal window opens, lists the EC2 instances your AWS sign-in can see, and asks you to pick yours. Or run it by hand:

```bash
python3 ~/swiftbar-plugins/monitor.py --aws-setup
```

That writes `~/.config/baseten-job-monitor/aws.env` (mode `0600`) that looks like this:

```bash
# Baseten Monitor — AWS dev boxes. No credentials belong in this file.
AWS_PROFILE=default
AWS_REGION=us-east-2          # optional; defaults to the profile's region
BTMON_DEV_BOXES=i-0123456789abcdef0,my-box-name   # instance IDs or Name tags
BTMON_SSH_USER=ubuntu         # optional; blank means rely on ~/.ssh/config
BTMON_SSH_KEY=~/.ssh/my-key.pem   # optional; passed to `ssh -i`, must be mode 0600
BTMON_PINNED=i-0123456789abcdef0  # optional; set by "Pin to menu bar". Also accepts "local" (this Mac) or "baseten"
BTMON_USAGE_INTERVAL=10           # optional; 2, 5, 10, 30, or 60 seconds. Set by "Usage refresh" in the dropdown
```

Any of those keys can also be set as environment variables, which take priority over the file.

## How AWS access is kept secure

The plugin is deliberately unable to hold AWS secrets:

- **No credentials are stored or read by the plugin.** Every AWS call shells out to the `aws` CLI, which uses whatever sign-in you already have: an `aws sso login` session with your organization's Identity Center, an `aws login` session, a named profile, or a `credential_process`. With SSO there are no long-lived keys on disk at all. When the session expires the menu shows a **Sign in** action that runs the right login command for your profile. Organization-specific details such as the SSO start URL stay in your own `~/.aws/config`, written by `aws configure sso`.
- **The config file only holds non-secret settings** (profile name, region, which instances are yours, SSH username, and the *path* to your SSH key). It is plain `KEY=VALUE` text that the plugin parses itself; it is never `source`d or executed.
- **The SSH key is never read by the plugin.** Its path is handed to `ssh -i`, and only if the key file is owned by you and not readable by anyone else; otherwise the SSH action falls back to your `~/.ssh/config` and the menu shows a warning.
- **Usage metrics use the same SSH access you already have.** The plugin pipes a short, fixed shell script (read `/proc/stat`, `free`, `df`, and `nvidia-smi` if present, in a loop) to `sh` on the box in batch mode, so it never prompts for a password and never runs anything from the config file. If SSH fails, the menu just says usage is unavailable and retries.
- **The config file is checked before it is read.** If it is not owned by you or is readable by group/others (anything other than mode `0600`), the plugin refuses to load it and offers a one-click fix.
- **Credential keys are rejected.** If `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, or `AWS_SECURITY_TOKEN` appear in the file, the plugin refuses to use it and tells you to sign in with the CLI instead. Unknown keys are ignored with a warning.
- **All input is validated.** Instance IDs, Name tags, profile, region, and SSH user are checked against strict patterns, and every command is run as an argument list with no shell, so nothing in the config can inject commands.
- **Stopping a box asks first.** Start is a single click; Stop shows a native confirmation dialog.

The plugin only needs read access to EC2 (`ec2:DescribeInstances`, `ec2:DescribeInstanceStatus`) for monitoring, plus `ec2:StartInstances` / `ec2:StopInstances` if you use those actions, and `ssm:StartSession` for the SSM connect action.

## Customization

### Refresh interval

`monitor.py` is a SwiftBar "streamable" plugin, so it has no interval in its name. The usage sampling and redraw interval is chosen from the dropdown (**Usage refresh**, default 10 s). The Baseten and EC2 state polls are constants in the file: `TRUSS_POLL_INTERVAL` and `EC2_POLL_INTERVAL` (both 60 s).

For the Baseten-only plugin, the filename controls the refresh interval — `1m` means every 1 minute. Rename the file to change it:

| Filename | Interval |
|---|---|
| `baseten.30s.py` | 30 seconds |
| `baseten.1m.py` | 1 minute (default) |
| `baseten.5m.py` | 5 minutes |

### PATH resolution

The plugin automatically adds common tool directories to PATH (`~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, mise shims). If `truss` or `aws` is installed somewhere else, add its directory to the `_extra_paths` list at the top of the script.

If you manage `truss` with mise and see `No version is set for shim: truss`, pick a global default, for example `mise use -g pipx:truss@0.18.35`.

## License

MIT
