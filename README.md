# ComfyCast

A **"Run on Remote ▶" button inside ComfyUI**. Build workflows in your local
ComfyUI editor (a laptop with no GPU and zero model files), click the button,
and the workflow runs on your remote GPU machine — live progress in a panel,
a small compressed preview when it finishes, and a *Fetch full-res* button
for when you actually want the big files. Built for bad hotel wifi: a run
costs a few KB up and ~100 KB down.

No terminal required: a **⚙ settings panel** in the UI asks for your remote's
address (Tailscale IP or hostname) and shows a **green/red connection light**
on the button. Settings live in `~/.comfycast.json` if you would rather edit
them by hand.

## Before you start (prerequisites)

You need two things. Nothing gets installed on the remote machine.

**1. A machine running ComfyUI with a GPU** — your desktop, a studio
workstation, a rented box. Start its ComfyUI so it accepts connections from
other machines, by adding `--listen` to how you launch it:

```
python main.py --listen
```

(Without `--listen`, ComfyUI only answers its own machine and nothing can
reach it — this is the #1 setup mistake.)

**2. A network path from your laptop to that machine.** Pick whichever
matches your situation:

- **Same network (home/studio LAN):** nothing extra to install. Use the
  machine's local IP (e.g. `192.168.1.42` — `ip addr` on Linux shows it) as
  the address in ComfyCast's ⚙ panel.
- **Different networks (working from anywhere):** install
  [Tailscale](https://tailscale.com) (free for personal use) on both
  machines and log into the same account. Each machine gets a stable
  `100.x.y.z` address that works from any wifi, hotel, or hotspot — use the
  remote's Tailscale IP as the address. This is the recommended setup and
  what ComfyCast was built around.
- **You only have SSH access** (e.g. a rented server): leave the address
  empty and fill in the SSH fallback (`user@host`) instead — ComfyCast
  opens an SSH tunnel automatically. Requires key-based login (`ssh host`
  works without a password prompt).

Do **not** expose ComfyUI's port directly to the public internet
(no router port-forwarding) — it has no authentication. Tailscale or SSH
give you the same convenience, privately.

## Install

On your **local (editor)** ComfyUI — not the GPU machine:

```
cd ComfyUI/custom_nodes
git clone https://github.com/AGBKdev/ComfyCast
```

Restart ComfyUI. No dependencies beyond what ComfyUI already ships.

## First run

1. A "Run on Remote ▶" button appears top-right, with a status dot.
2. Click **⚙** → enter your remote's address (e.g. a Tailscale IP like
   `100.x.y.z`, or any hostname that resolves) and ComfyUI's port on the
   remote (default 8188). Optional: an SSH fallback (`user@host`) used
   automatically if the direct address doesn't answer — needs key-based
   auth (Tailscale SSH or a normal `~/.ssh` key).
3. **Save & test** → the light goes green and shows the remote's ComfyUI
   version, GPU, and free VRAM in the button tooltip.

## Daily use

Build or load a workflow as usual, then click the run button — it's labeled
with the machine you're connected to (e.g. **Run on 100.x.y.z ▶**), so if
you work with more than one remote you always know where a run will land
(switch machines in ⚙):

- **input files travel automatically**: images/videos referenced by
  LoadImage-style nodes are uploaded to the remote before the run — only
  ones the remote doesn't already have (a progress bar shows the transfer;
  unchanged files are never re-sent)
- live progress panel: node-by-node execution, sampler progress bar, queue
  position if the remote is busy, elapsed time — and honest quiet-time
  feedback ("no events for 40s — model loads are silent, the run is still
  going"). If your connection is too weak for the live feed, it says so and
  falls back to polling; the run always survives a dropped connection
- a **■ Stop** button — interrupts the run on the remote (or pulls it from
  the queue if it hasn't started)
- progress lives **in the ComfyCast panel**, not on the canvas. ComfyCast does
  re-dispatch the remote's execution events into the local frontend, and on
  older frontends that lit up the running node with the usual green outline —
  it does not on current ones, so treat the panel as the source of truth. The
  run itself is unaffected either way
- **all errors pass through**: a rejected workflow shows the exact per-node
  reason (bad model name, missing node), and a crash mid-run shows the node,
  the exception, and the remote's traceback tail in the panel
- on success: a small preview appears (size/quality set in ⚙ — `webp;60`
  is ~50–100 KB for a 1 MP image). Full resolution stays on the remote until
  you click **Fetch full-res**, which saves into `~/ComfyCast-Outputs/<run>/`.

Workflows whose nodes write files directly (EXR savers and similar) finish
with a note instead of a preview — those files are in the remote's output
folder.

## The models toggle (REMOTE / LOCAL)

Next to the run button there's a **models** switch:

- **REMOTE** — your model dropdowns list everything on the remote, via
  zero-byte stub files. Author for the big machine without downloading a
  single model. The remote's model list is cached, so this works offline too.
- **LOCAL** — stubs are removed (only ever 0-byte files; real models are
  never touched) and dropdowns show just what's really on this machine, for
  when you want to run locally.

Switching refreshes the dropdowns in place.

## Requirements on the remote

Just ComfyUI, reachable from your machine — either listening on a tailnet /
LAN address (`--listen`), or reachable over SSH for the automatic tunnel.
Nothing to install on the remote.

## Keeping editor and remote in sync

The editor can only author with node packs and model names it knows about.

**Model names are handled** — that is what the models toggle above does. Switch
to REMOTE and your dropdowns list everything on the remote, as zero-byte stubs,
cached so it works offline.

**Node packs are not.** If a workflow uses a node the remote does not have, you
find out when the run is rejected — the panel shows the exact per-node reason,
so it is a clear failure rather than a silent one, but nothing here diffs the
two installs for you in advance. Install the same packs on both sides.

## How it works

The pack registers routes on your *local* ComfyUI server
(`/comfycast/submit|status|preview|fetch|config|ping`) that proxy to the remote —
so the browser never talks to the remote directly (no CORS, no mixed-content
issues), and the remote needs zero changes. Progress is relayed from the remote's
websocket; binary live-preview frames are deliberately dropped to save
bandwidth.

## Security model

ComfyCast opens a network path between your editor and a GPU box, so it is
worth being explicit about what is trusted.

Everything below is about the **editor** machine — the laptop ComfyCast is
installed on. (The *remote* is the one you were told to start with `--listen`;
that instruction stands, and the warning above about never port-forwarding it
to the public internet is the important half.)

**ComfyCast's routes are unauthenticated**, like ComfyUI's own API. Anything
that can reach your editor's ComfyUI port can submit runs through ComfyCast,
read its status, and change which remote it points at. Two things make that
acceptable in normal use: ComfyUI binds `127.0.0.1` by default, and it
installs an origin-only middleware that rejects cross-site browser requests —
so a malicious web page cannot drive these routes.

**Those protections vanish if you start your editor's ComfyUI with
`--listen 0.0.0.0`.** Then treat everyone who can reach that laptop as able to
use your remote, spend your GPU time, and read your workflows. Put it behind a
firewall or a VPN/tailnet, not the open internet.

**The remote is trusted with a lot.** It receives your workflow and any input
files it references, and its responses drive what gets written to
`~/ComfyCast-Outputs/`. As of v1.4.1 those paths are confined — a hostile
remote could previously escape them — but "confined" is not the same as
"harmless". Point this at a box you control or rent, not one a stranger hands
you.

**In transit, plain HTTP.** The settings UI builds an `http://` URL, so use
the ssh tunnel option unless the remote is already on a private network you
trust. The tunnel is the recommended setup for exactly this reason.

Found something? Open an issue.
