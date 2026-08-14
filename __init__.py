"""
ComfyCast — run the workflow you're editing on a remote machine, from inside
the ComfyUI interface. Companion to comfycast (the CLI); reads the same
~/.comfycast.json config.

Install this pack on your LOCAL (editor) ComfyUI. It adds:
  - a "Run on Remote ▶" button in the UI
  - server routes on the local ComfyUI that proxy to the remote:
      POST /comfycast/submit     queue the current graph on the remote
      GET  /comfycast/status     live progress for a run
      GET  /comfycast/preview    compressed preview of an output image
      GET  /comfycast/fetch      download full-res outputs to disk
      GET  /comfycast/ping       is the remote reachable + its GPU stats

No nodes are registered — this pack is UI + routes only.
"""

import asyncio
import json
import os
import socket
import subprocess
import time
import uuid

import aiohttp
from aiohttp import web

from server import PromptServer

CONFIG_PATH = os.path.expanduser("~/.comfycast.json")

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]


# --------------------------------------------------------------------------- config / connection

_tunnel_proc = None
_base_cache = {"url": None, "ts": 0.0}


def load_cfg():
    if not os.path.exists(CONFIG_PATH):
        raise RuntimeError(
            f"no config at {CONFIG_PATH} — install comfycast and run `comfycast init`, "
            "or create the file (see comfycast README)")
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg.setdefault("remote", {})
    cfg.setdefault("output", {})
    cfg["remote"].setdefault("comfy_port", 8188)
    cfg["remote"].setdefault("tunnel_local_port", 8189)
    cfg["output"].setdefault("dir", "~/ComfyCast-Outputs")
    cfg["output"].setdefault("preview", "webp;80")
    return cfg


def _port_open(port, host="127.0.0.1", timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def _reachable(session, base):
    try:
        async with session.get(base.rstrip("/") + "/system_stats",
                               timeout=aiohttp.ClientTimeout(total=4)) as r:
            return r.status == 200
    except Exception:
        return False


async def get_base(session):
    """Base URL for the remote: direct_url if reachable, else SSH tunnel."""
    global _tunnel_proc
    now = time.time()
    if _base_cache["url"] and now - _base_cache["ts"] < 20:
        return _base_cache["url"]

    cfg = load_cfg()
    r = cfg["remote"]

    direct = (r.get("direct_url") or "").strip()
    if direct and await _reachable(session, direct):
        _base_cache.update(url=direct, ts=now)
        return direct

    lp = int(r["tunnel_local_port"])
    tunnel_base = f"http://127.0.0.1:{lp}"
    if _port_open(lp) and await _reachable(session, tunnel_base):
        _base_cache.update(url=tunnel_base, ts=now)
        return tunnel_base

    host = r.get("ssh_host")
    if not host:
        raise RuntimeError("remote unreachable and no remote.ssh_host configured")
    rp = int(r["comfy_port"])
    if _tunnel_proc is None or _tunnel_proc.poll() is not None:
        _tunnel_proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
             "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
             "-N", "-L", f"{lp}:127.0.0.1:{rp}", host],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.time() + 15
    while time.time() < deadline:
        if _tunnel_proc.poll() is not None:
            err = _tunnel_proc.stderr.read().decode("utf-8", "replace").strip()
            raise RuntimeError(f"ssh tunnel to {host} failed: {err[:400]}")
        if _port_open(lp) and await _reachable(session, tunnel_base):
            _base_cache.update(url=tunnel_base, ts=time.time())
            return tunnel_base
        await asyncio.sleep(0.3)
    raise RuntimeError(f"ssh tunnel opened but ComfyUI on {host}:{rp} did not respond")


# --------------------------------------------------------------------------- job tracking

JOBS = {}  # prompt_id -> state dict
_session = None


def _get_session():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


def _job_public(j):
    out = {k: j.get(k) for k in
           ("status", "total", "done", "current", "current_label", "value",
            "max", "error", "error_tb", "error_node", "error_node_type",
            "error_type", "error_msg", "outputs", "queue_ahead", "ws")}
    now = time.time()
    out["age"] = round(now - j.get("updated", now))
    out["elapsed"] = round(now - j.get("started", now))
    return out


async def _watch_ws(base, prompt_id, client_id, wf):
    """Consume the remote's websocket for this run; keep JOBS[prompt_id] current."""
    j = JOBS[prompt_id]
    session = _get_session()
    ws_url = base.replace("http://", "ws://").replace("https://", "wss://") \
        + f"/ws?clientId={client_id}"
    try:
        async with session.ws_connect(ws_url, heartbeat=20) as ws:
            j["ws"] = True
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue  # binary live previews — skipped (bandwidth)
                try:
                    m = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                t, d = m.get("type"), m.get("data", {})
                if d.get("prompt_id") not in (None, prompt_id):
                    continue
                j["updated"] = time.time()
                if t == "execution_cached":
                    j["done"] += len(d.get("nodes", []))
                elif t == "executing":
                    node = d.get("node")
                    if node is None:
                        await _finish(base, prompt_id)
                        return
                    if j["current"] is not None:
                        j["done"] += 1
                    j["current"] = node
                    n = wf.get(str(node), {})
                    title = (n.get("_meta") or {}).get("title")
                    j["current_label"] = title or n.get("class_type", f"node {node}")
                    j["value"] = j["max"] = None
                elif t == "progress":
                    j["value"], j["max"] = d.get("value", 0), d.get("max", 1)
                elif t == "execution_error":
                    j["status"] = "error"
                    j["error"] = (f"{d.get('node_type','?')} (node {d.get('node_id')}): "
                                  f"{d.get('exception_type','')}: "
                                  f"{d.get('exception_message','')}")
                    j["error_tb"] = "\n".join(
                        l.rstrip() for l in (d.get("traceback") or [])[-10:])
                    j["error_node"] = d.get("node_id")
                    j["error_node_type"] = d.get("node_type")
                    j["error_type"] = d.get("exception_type")
                    j["error_msg"] = d.get("exception_message")
                    return
                elif t == "execution_interrupted":
                    j["status"] = "error"
                    j["error"] = "stopped — run interrupted on the remote"
                    return
                elif t == "execution_success":
                    await _finish(base, prompt_id)
                    return
                elif t == "status":
                    q = d.get("status", {}).get("exec_info", {}).get("queue_remaining")
                    if isinstance(q, int):
                        j["queue_ahead"] = max(q - 1, 0)
    except Exception:
        pass  # fall through to history polling
    # websocket gone or errored — poll history until the run resolves
    for _ in range(3600):
        st = await _history_state(base, prompt_id)
        if st is not None:
            return
        await asyncio.sleep(2)
    j["status"] = "error"
    j["error"] = "timed out waiting for the remote"


async def _history_state(base, prompt_id):
    """Check history; if the run is finished (ok or error), update job. -> status | None"""
    j = JOBS[prompt_id]
    session = _get_session()
    try:
        async with session.get(f"{base}/history/{prompt_id}",
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            h = await r.json()
    except Exception:
        return None
    entry = h.get(prompt_id)
    if not entry:
        return None
    if (entry.get("status") or {}).get("status_str") == "error":
        msgs = (entry.get("status") or {}).get("messages", [])
        detail = ""
        for m in msgs:
            if m[0] == "execution_error":
                d = m[1]
                detail = (f"{d.get('node_type','?')} (node {d.get('node_id')}): "
                          f"{d.get('exception_message','')}")
                j["error_tb"] = "\n".join(
                    l.rstrip() for l in (d.get("traceback") or [])[-10:])
                j["error_node"] = d.get("node_id")
                j["error_node_type"] = d.get("node_type")
                j["error_type"] = d.get("exception_type")
                j["error_msg"] = d.get("exception_message")
        j["status"], j["error"] = "error", detail or "execution error on the remote"
        return "error"
    await _finish(base, prompt_id, entry)
    return "done"


async def _finish(base, prompt_id, entry=None):
    j = JOBS[prompt_id]
    if entry is None:
        session = _get_session()
        try:
            async with session.get(f"{base}/history/{prompt_id}",
                                   timeout=aiohttp.ClientTimeout(total=15)) as r:
                h = await r.json()
            entry = h.get(prompt_id) or {}
        except Exception:
            entry = {}
    outs = []
    for node_id, out in (entry.get("outputs") or {}).items():
        for key, items in out.items():
            if not isinstance(items, list):
                continue
            for it in items:
                if isinstance(it, dict) and "filename" in it:
                    outs.append({"filename": it["filename"],
                                 "subfolder": it.get("subfolder", ""),
                                 "type": it.get("type", "output"),
                                 "kind": key})
    j["outputs"] = outs
    j["done"] = j["total"]
    j["current"] = j["current_label"] = None
    j["status"] = "done"


# --------------------------------------------------------------------------- routes

routes = PromptServer.instance.routes


@routes.get("/comfycast/config")
async def get_config(request):
    try:
        cfg = load_cfg()
        configured = True
    except Exception:
        cfg = {"remote": {"ssh_host": "", "comfy_port": 8188,
                          "direct_url": "", "tunnel_local_port": 8189},
               "output": {"dir": "~/ComfyCast-Outputs", "preview": "webp;80"}}
        configured = False
    r = cfg["remote"]
    # derive the simple "address" the UI shows from direct_url
    address = ""
    direct = (r.get("direct_url") or "").strip()
    if direct:
        address = direct.split("//", 1)[-1].rsplit(":", 1)[0]
    return web.json_response({
        "configured": configured,
        "address": address,
        "port": int(r.get("comfy_port", 8188)),
        "ssh_host": r.get("ssh_host", ""),
        "preview": cfg["output"].get("preview", "webp;80"),
    })


@routes.post("/comfycast/config")
async def set_config(request):
    global _tunnel_proc
    body = await request.json()
    try:
        cfg = load_cfg()
    except Exception:
        cfg = {"remote": {"tunnel_local_port": 8189},
               "local": {}, "output": {"dir": "~/ComfyCast-Outputs"}}
    r = cfg.setdefault("remote", {})
    address = (body.get("address") or "").strip()
    port = int(body.get("port") or 8188)
    ssh_host = (body.get("ssh_host") or "").strip()
    r["comfy_port"] = port
    r["direct_url"] = f"http://{address}:{port}" if address else ""
    r["ssh_host"] = ssh_host
    r.setdefault("tunnel_local_port", 8189)
    if body.get("preview"):
        cfg.setdefault("output", {})["preview"] = str(body["preview"])
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    # force a fresh connection with the new settings
    _base_cache.update(url=None, ts=0)
    if _tunnel_proc is not None and _tunnel_proc.poll() is None:
        _tunnel_proc.terminate()
        _tunnel_proc = None
    # connecting a machine catalogues its models in the background (per-machine)
    if not SYNC["running"]:
        asyncio.create_task(_background_catalogue())
    return web.json_response({"ok": True, "path": CONFIG_PATH})


@routes.get("/comfycast/ping")
async def ping(request):
    session = _get_session()
    try:
        base = await get_base(session)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=502)
    try:
        async with session.get(f"{base}/system_stats",
                               timeout=aiohttp.ClientTimeout(total=6)) as r:
            stats = await r.json()
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=502)
    sysinfo = stats.get("system", {})
    dev = (stats.get("devices") or [{}])[0]
    return web.json_response({
        "ok": True,
        "version": sysinfo.get("comfyui_version", "?"),
        "os": sysinfo.get("os", "?"),
        "gpu": dev.get("name", "?"),
        "vram_free": dev.get("vram_free"),
        "vram_total": dev.get("vram_total"),
    })


# ---- input-file transfer -------------------------------------------------
# LoadImage/LoadVideo/etc. reference files that live in the LOCAL input
# folder; the remote has never seen them. Before queueing, find every such
# file in the graph and upload the ones the remote doesn't have yet.

_uploaded = {}  # (machine, relpath) -> (size, mtime) already sent this session
SUBMITS = {}    # token -> {"phase","done","total","bytes_done","bytes_total","file"}


def _local_input_files(wf):
    """(relpath, abspath, size) for every string input that resolves to a
    file in the local input directory."""
    import folder_paths
    indir = folder_paths.get_input_directory()
    seen, out = set(), []
    for node in wf.values():
        for v in (node.get("inputs") or {}).values():
            if not isinstance(v, str) or not v.strip():
                continue
            rel = v[:-8] if v.endswith(" [input]") else v
            rel = rel.replace("\\", "/").strip()
            if not rel or rel.startswith("/") or ".." in rel.split("/"):
                continue
            ap = os.path.join(indir, rel)
            if rel not in seen and os.path.isfile(ap):
                seen.add(rel)
                out.append((rel, ap, os.path.getsize(ap)))
    return out


async def _upload_inputs(session, base, machine, wf, prog):
    """Send graph-referenced local input files the remote doesn't have."""
    files = _local_input_files(wf)
    todo = []
    for rel, ap, size in files:
        st = os.stat(ap)
        if _uploaded.get((machine, rel)) == (st.st_size, int(st.st_mtime)):
            continue  # unchanged since we last sent it
        todo.append((rel, ap, size, st))
    prog.update(phase="uploading inputs", done=0, total=len(todo),
                bytes_total=sum(t[2] for t in todo), bytes_done=0)
    for rel, ap, size, st in todo:
        prog["file"] = rel
        sub, name = os.path.split(rel)
        with open(ap, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("image", f, filename=name,
                           content_type="application/octet-stream")
            form.add_field("overwrite", "true")
            form.add_field("type", "input")
            if sub:
                form.add_field("subfolder", sub)
            async with session.post(f"{base}/upload/image", data=form,
                                    timeout=aiohttp.ClientTimeout(total=1800)) as r:
                if r.status != 200:
                    raise RuntimeError(
                        f"uploading input file '{rel}' failed (HTTP {r.status})")
        _uploaded[(machine, rel)] = (st.st_size, int(st.st_mtime))
        prog["done"] += 1
        prog["bytes_done"] += size
    prog["phase"] = "queueing"


@routes.get("/comfycast/submit_progress")
async def submit_progress(request):
    tok = request.query.get("token")
    return web.json_response(SUBMITS.get(tok) or {"phase": None})


@routes.post("/comfycast/submit")
async def submit(request):
    body = await request.json()
    wf = body.get("prompt")
    token = body.get("token") or str(uuid.uuid4())
    if not isinstance(wf, dict) or not wf:
        return web.json_response({"error": "no workflow in request"}, status=400)
    session = _get_session()
    try:
        base = await get_base(session)
    except Exception as e:
        return web.json_response({"error": f"remote unreachable: {e}"}, status=502)

    prog = SUBMITS[token] = {"phase": "checking inputs", "done": 0, "total": 0,
                             "bytes_done": 0, "bytes_total": 0, "file": None}
    try:
        machine = _machine_key(load_cfg())
        await _upload_inputs(session, base, machine, wf, prog)
    except Exception as e:
        prog["phase"] = None
        return web.json_response({"error": f"input transfer failed: {e}"}, status=502)

    client_id = str(uuid.uuid4())
    # NB: the watcher's websocket connects after queueing; execution_start may be
    # missed but _history_state backstops the terminal state, so nothing hangs.
    try:
        async with session.post(f"{base}/prompt",
                                json={"prompt": wf, "client_id": client_id},
                                timeout=aiohttp.ClientTimeout(total=30)) as r:
            res = await r.json()
            if r.status != 200:
                prog["phase"] = None
                return web.json_response(
                    {"error": "the remote rejected the workflow",
                     "detail": res}, status=400)
    except Exception as e:
        prog["phase"] = None
        return web.json_response({"error": f"submit failed: {e}"}, status=502)
    finally:
        SUBMITS.pop(token, None)

    prompt_id = res["prompt_id"]
    JOBS[prompt_id] = {
        "status": "running", "total": len(wf), "done": 0,
        "current": None, "current_label": None, "value": None, "max": None,
        "error": None, "outputs": None, "queue_ahead": 0, "ws": False,
        "started": time.time(), "updated": time.time(),
    }
    asyncio.create_task(_watch_ws(base, prompt_id, client_id, wf))
    return web.json_response({"prompt_id": prompt_id})


@routes.post("/comfycast/stop")
async def stop(request):
    """Stop a run: drop it from the remote queue if pending, interrupt it if
    it's the one executing."""
    pid = request.query.get("pid")
    j = JOBS.get(pid)
    session = _get_session()
    try:
        base = await get_base(session)
    except Exception as e:
        return web.json_response({"error": f"remote unreachable: {e}"}, status=502)
    # remove from pending queue (no-op if it's not pending)
    try:
        async with session.post(f"{base}/queue", json={"delete": [pid]},
                                timeout=aiohttp.ClientTimeout(total=10)):
            pass
    except Exception:
        pass
    # interrupt current execution ONLY if our job is (or may be) the one running —
    # never kill someone else's job that's ahead in the queue
    interrupted = False
    if j is None or j.get("current") is not None or not j.get("queue_ahead"):
        try:
            async with session.post(f"{base}/interrupt",
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                interrupted = r.status == 200
        except Exception:
            pass
    if j is not None and j["status"] == "running":
        j["status"] = "error"
        j["error"] = "stopped by you" + ("" if interrupted else " (removed from queue)")
        j["updated"] = time.time()
    return web.json_response({"ok": True, "interrupted": interrupted})


@routes.get("/comfycast/status")
async def status(request):
    pid = request.query.get("pid")
    j = JOBS.get(pid)
    if j is None:
        return web.json_response({"error": "unknown prompt id"}, status=404)
    if j["status"] == "running" and not j.get("ws"):
        base = await get_base(_get_session())
        await _history_state(base, pid)
    return web.json_response(_job_public(j))


def _view_query(item, extra=""):
    from urllib.parse import urlencode, quote
    q = urlencode({"filename": item["filename"],
                   "subfolder": item.get("subfolder", ""),
                   "type": item.get("type", "output")})
    return q + extra


@routes.get("/comfycast/preview")
async def preview(request):
    pid = request.query.get("pid")
    idx = int(request.query.get("idx", 0))
    j = JOBS.get(pid)
    if not j or not j.get("outputs"):
        return web.json_response({"error": "no outputs for this run"}, status=404)
    images = [o for o in j["outputs"] if o["kind"] == "images"]
    if idx >= len(images):
        return web.json_response({"error": "index out of range"}, status=404)
    cfg = load_cfg()
    spec = cfg["output"]["preview"]
    from urllib.parse import quote
    session = _get_session()
    base = await get_base(session)
    url = f"{base}/view?" + _view_query(images[idx], "&preview=" + quote(spec))
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
        data = await r.read()
        ctype = r.headers.get("Content-Type", "image/webp")
    return web.Response(body=data, content_type=ctype.split(";")[0])


@routes.get("/comfycast/fetch")
async def fetch(request):
    """Download every full-res output of a run to output.dir on this machine."""
    pid = request.query.get("pid")
    j = JOBS.get(pid)
    if not j or not j.get("outputs"):
        return web.json_response({"error": "no outputs for this run"}, status=404)
    cfg = load_cfg()
    outdir = os.path.expanduser(cfg["output"]["dir"])
    run_dir = os.path.join(outdir, pid[:8])
    os.makedirs(run_dir, exist_ok=True)
    session = _get_session()
    base = await get_base(session)
    saved, total = [], 0
    for it in j["outputs"]:
        url = f"{base}/view?" + _view_query(it)
        dest = os.path.join(run_dir, os.path.basename(it["filename"]))
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=1800)) as r:
                if r.status != 200:
                    continue
                with open(dest, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 16):
                        f.write(chunk)
            n = os.path.getsize(dest)
            saved.append({"file": dest, "bytes": n})
            total += n
        except Exception as e:
            saved.append({"file": dest, "error": str(e)})
    return web.json_response({"dir": run_dir, "files": saved, "bytes": total})


# --------------------------------------------------------------------------- models toggle (REMOTE / LOCAL), per-machine cache

STUB_CACHE_PATH = os.path.expanduser("~/.comfycast_stubs.json")

# progress of a running sync/toggle, polled by the UI for the load bar
SYNC = {"running": False, "phase": None, "done": 0, "total": 0,
        "machine": None, "error": None}


def _safe_relpath(name):
    p = name.replace("\\", "/")
    if p.startswith("/") or ".." in p.split("/"):
        return None
    return p


def _machine_key(cfg):
    """Stable id for the connected machine, e.g. '100.78.97.1:8188' — model
    lists are cached PER MACHINE so multi-remote parity never mixes up."""
    r = cfg.get("remote", {})
    direct = (r.get("direct_url") or "").strip()
    if direct:
        return direct.split("//", 1)[-1].split("/")[0]
    host = (r.get("ssh_host") or "remote").split("@")[-1]
    return f"{host}:{r.get('comfy_port', 8188)}"


def _load_cache():
    try:
        with open(STUB_CACHE_PATH) as f:
            c = json.load(f)
    except Exception:
        c = {}
    if "machines" not in c:  # migrate/initialize
        old = c.get("folders")
        c = {"machines": {}, "active": None, "stub_paths": []}
        if old:
            c["machines"]["legacy"] = {"folders": old, "updated": None}
    c.setdefault("stub_paths", [])
    c.setdefault("active", None)
    return c


def _save_cache(c):
    with open(STUB_CACHE_PATH, "w") as f:
        json.dump(c, f)


def _stub_dest(folder, rel):
    """Where a stub for <folder>/<rel> belongs. Asks the RUNNING ComfyUI
    (folder_paths) — no configuration needed, always the tree this server
    actually reads. Unknown folder types land under models/<folder>."""
    import folder_paths
    try:
        paths = folder_paths.get_folder_paths(folder)
    except Exception:
        paths = None
    base = paths[0] if paths else os.path.join(folder_paths.models_dir, folder)
    return os.path.join(base, rel)


async def _fetch_model_list(machine):
    """Catalogue the remote's models with progress. -> {folder: [names]}"""
    session = _get_session()
    base = await get_base(session)
    async with session.get(f"{base}/models",
                           timeout=aiohttp.ClientTimeout(total=30)) as r:
        if r.status != 200:
            raise RuntimeError("remote too old for the /models API — update its ComfyUI")
        folders = await r.json()
    names = [f["name"] if isinstance(f, dict) else f for f in folders]
    SYNC.update(phase="cataloguing models", done=0, total=len(names), machine=machine)
    out = {}
    from urllib.parse import quote
    for name in names:
        try:
            async with session.get(f"{base}/models/{quote(name)}",
                                   timeout=aiohttp.ClientTimeout(total=60)) as r:
                files = await r.json()
            out[name] = sorted(x["name"] if isinstance(x, dict) else x for x in files)
        except Exception:
            out[name] = []
        SYNC["done"] += 1
    return out


async def _sync_machine(machine):
    folders = await _fetch_model_list(machine)
    c = _load_cache()
    c["machines"][machine] = {"folders": folders,
                              "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _save_cache(c)
    return folders


def _apply_stubs(folders):
    """Create zero-byte stand-ins; returns (created, skipped, paths_created)."""
    created = skipped = 0
    paths = []
    total = sum(len(v) for v in folders.values())
    SYNC.update(phase="creating stubs", done=0, total=total)
    for folder, files in folders.items():
        for name in files:
            SYNC["done"] += 1
            rel = _safe_relpath(name)
            if rel is None:
                continue
            dest = _stub_dest(folder, rel)
            if os.path.exists(dest):
                skipped += 1
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            open(dest, "wb").close()
            paths.append(os.path.abspath(dest))
            created += 1
    return created, skipped, paths


def _remove_stubs(c):
    """Delete previously created stubs (zero-byte only; real files never touched)."""
    paths = c.get("stub_paths") or []
    SYNC.update(phase="removing stubs", done=0, total=len(paths))
    removed = 0
    for p in paths:
        SYNC["done"] += 1
        try:
            if os.path.exists(p) and os.path.getsize(p) == 0:
                os.remove(p)
                removed += 1
        except OSError:
            pass
    return removed


async def _do_toggle(want_active):
    try:
        cfg = load_cfg()
        machine = _machine_key(cfg)
        c = _load_cache()
        if want_active:
            try:
                folders = await _sync_machine(machine)
                source = machine
            except Exception:
                m = c["machines"].get(machine)
                if not m:
                    raise RuntimeError(
                        f"{machine} is unreachable and no cached model list exists "
                        "for it — connect once so the catalogue can be saved")
                folders = m["folders"]
                source = f"cache from {m.get('updated', '?')}"
            created, skipped, paths = _apply_stubs(folders)
            c = _load_cache()
            c["stub_paths"] = sorted(set(c.get("stub_paths", [])) | set(paths))
            c["active"] = machine
            _save_cache(c)
            SYNC.update(phase=f"done — {created} stubs created, {skipped} already present ({source})")
        else:
            removed = _remove_stubs(c)
            c["stub_paths"] = []
            c["active"] = None
            _save_cache(c)
            SYNC.update(phase=f"done — {removed} stubs removed")
    except Exception as e:
        SYNC["error"] = str(e)
    finally:
        SYNC["running"] = False


@routes.get("/comfycast/stubs/state")
async def stubs_state(request):
    c = _load_cache()
    try:
        machine = _machine_key(load_cfg())
    except Exception:
        machine = None
    return web.json_response({
        "active": bool(c.get("active")),
        "active_machine": c.get("active"),
        "machine": machine,
        "machines": {k: {"count": sum(len(v) for v in m.get("folders", {}).values()),
                         "updated": m.get("updated")}
                     for k, m in c.get("machines", {}).items()},
        "syncing": dict(SYNC),
    })


@routes.post("/comfycast/stubs/toggle")
async def stubs_toggle(request):
    if SYNC["running"]:
        return web.json_response({"error": "a sync is already running"}, status=409)
    body = await request.json()
    SYNC.update(running=True, phase="starting", done=0, total=0, error=None)
    asyncio.create_task(_do_toggle(bool(body.get("active"))))
    return web.json_response({"started": True})


async def _background_catalogue():
    """After connecting a machine, catalogue its models so REMOTE mode is
    instant (and the user sees a progress bar while it happens)."""
    try:
        cfg = load_cfg()
        machine = _machine_key(cfg)
        SYNC.update(running=True, phase="starting", done=0, total=0,
                    error=None, machine=machine)
        await _sync_machine(machine)
        c = _load_cache()
        n = sum(len(v) for v in c["machines"][machine]["folders"].values())
        SYNC.update(phase=f"done — {n} models catalogued from {machine}")
        # if REMOTE mode is on for this machine, refresh its stubs too
        if c.get("active") == machine:
            created, skipped, paths = _apply_stubs(c["machines"][machine]["folders"])
            c = _load_cache()
            c["stub_paths"] = sorted(set(c.get("stub_paths", [])) | set(paths))
            _save_cache(c)
    except Exception as e:
        SYNC["error"] = str(e)
    finally:
        SYNC["running"] = False


print("[ComfyCast] loaded — Run-on-Remote button + /comfycast/* routes")
