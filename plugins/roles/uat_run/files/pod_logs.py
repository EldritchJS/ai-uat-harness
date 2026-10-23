#!/usr/bin/env python3
# Assisted by Claude Opus
"""Stream the logs of a run's pods to files while the run goes.

pod_logs.py <run dir> <run id> <ansible-playbook pid> <namespace> [<namespace> ...]

One process per run, one thread per container. It follows every pod in a step manifest under
<run dir>/logs/*/manifest.yaml, and every pod that the objects in those manifests made, found
through owner references (directly or through up to two other objects) or the run's labels. A pod
from a manifest writes <step dir>/<container>.log; a pod an object made writes
<step dir>/<pod>.<container>.log. The bytes are written as they come; a dropped stream is
resumed without repeating lines; a restarted container is appended after a marker line.
When a container's log has ended, meta/streams/<pod>.<container>.done is written. It stops
when meta/streams/stop appears or the ansible-playbook process ends.
"""

import fcntl
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime

POLL = 2
READ_TIMEOUT = int(os.environ.get("UAT_STREAM_READ_TIMEOUT", "60"))
GRACE = 60
SEQ_LABELS = ("uat/tid", "uat/seq", "uat/step")


def ts_key(raw):
    """Sortable text for a kubelet timestamp such as b'2026-10-21T04:35:03.9155Z', or None."""
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    if len(text) < 20 or text[10:11] != "T" or not text.endswith("Z"):
        return None
    whole, _, frac = text[:-1].partition(".")
    if len(whole) != 19 or not (frac.isdigit() or frac == ""):
        return None
    return whole + "." + frac.ljust(9, "0")[:9]


def key_epoch(key):
    return (
        datetime.strptime(key[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC).timestamp()
    )


def load_manifests(logs_dir, seen, index):
    """Add the objects of new <step dir>/manifest.yaml files to index, keyed by (namespace, kind, name)."""
    import yaml

    for d in sorted(os.listdir(logs_dir)):
        path = os.path.join(logs_dir, d, "manifest.yaml")
        if d in seen or not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                docs = yaml.safe_load(f)
        except (OSError, ValueError, yaml.YAMLError):
            continue
        for obj in docs if isinstance(docs, list) else [docs]:
            meta = (obj or {}).get("metadata") or {}
            index[(meta.get("namespace"), obj.get("kind"), meta.get("name"))] = {
                "dir": d,
                "labels": meta.get("labels") or {},
            }
        seen.add(d)


def place(pod, index, run_id, get_object):
    """Where a pod's logs go, as (step dir, file prefix), or None when the run did not make it."""
    meta = pod["metadata"]
    ns, name, labels = meta.get("namespace"), meta["name"], meta.get("labels") or {}
    own = index.get((ns, "Pod", name))
    if own:
        return (own["dir"], "") if labels.get("uat/run") == run_id else None
    for ref in meta.get("ownerReferences") or []:
        found = _root(ns, ref, index, run_id, get_object, 2)
        if found:
            return found, name + "."
    if labels.get("uat/run") == run_id and all(labels.get(k) for k in SEQ_LABELS):
        for (ens, kind, _), entry in index.items():
            if (
                ens == ns
                and kind != "Pod"
                and all(entry["labels"].get(k) == labels[k] for k in SEQ_LABELS)
            ):
                return entry["dir"], name + "."
    return None


def _root(ns, ref, index, run_id, get_object, hops):
    entry = index.get((ns, ref["kind"], ref["name"]))
    if entry and ref["kind"] != "Pod":
        obj = get_object(ref["apiVersion"], ref["kind"], ns, ref["name"])
        labels = ((obj or {}).get("metadata") or {}).get("labels") or {}
        return entry["dir"] if labels.get("uat/run") == run_id else None
    if hops == 0:
        return None
    obj = get_object(ref["apiVersion"], ref["kind"], ns, ref["name"])
    for up in ((obj or {}).get("metadata") or {}).get("ownerReferences") or []:
        found = _root(ns, up, index, run_id, get_object, hops - 1)
        if found:
            return found
    return None


def statuses(pod):
    status = pod.get("status") or {}
    return (status.get("initContainerStatuses") or []) + (
        status.get("containerStatuses") or []
    )


class Lines:
    """Writes a container's log lines without their timestamps. A resumed stream starts again
    a little before the last line written; lines older than that one are skipped, and so are as
    many lines with its timestamp as were written, because lines read together share one."""

    def __init__(self, out):
        self.out = out
        self.key, self.count = None, 0
        self.resume, self.same = None, 0

    def reconnect(self):
        self.resume, self.same = (self.key, self.count) if self.key else None, 0

    def since(self):
        return max(1, int(time.time() - key_epoch(self.key)) + 1) if self.key else None

    def write(self, line):
        stamp, sep, text = line.partition(b" ")
        key = ts_key(stamp) if sep else None
        if key is None:
            self.out.write(line)
            return
        if self.resume:
            if key < self.resume[0]:
                return
            if key == self.resume[0]:
                self.same += 1
                if self.same <= self.resume[1]:
                    return
            else:
                self.resume = None
        self.out.write(text)
        if key == self.key:
            self.count += 1
        else:
            self.key, self.count = key, 1


class Transient(Exception):
    pass


class Streamer:
    def __init__(self, run_dir, run_id, pid, namespaces):
        from kubernetes import client
        from provenance import connect

        self.client = client
        self.api, _ = connect()
        self.core = client.CoreV1Api(self.api)
        self.dyn = None
        self.run_id, self.pid, self.namespaces = run_id, pid, namespaces
        self.logs = os.path.join(run_dir, "logs")
        self.streams = os.path.join(run_dir, "meta", "streams")
        self.index, self.seen, self.objects = {}, set(), {}
        self.placed, self.started, self.instances = {}, set(), {}
        self.threads, self.files, self.errors = [], {}, []
        self.halt = threading.Event()
        self.lock = threading.Lock()
        self.log_file = open(os.path.join(self.logs, "streamer.log"), "a")  # noqa: SIM115

    def log(self, msg):
        with self.lock:
            self.log_file.write(f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} {msg}\n")
            self.log_file.flush()

    def get_object(self, api_version, kind, ns, name):
        key = (api_version, kind, ns, name)
        if key not in self.objects:
            from kubernetes.dynamic import DynamicClient

            try:
                self.dyn = self.dyn or DynamicClient(self.api)
                self.objects[key] = (
                    self.dyn.resources.get(api_version=api_version, kind=kind)
                    .get(name=name, namespace=ns)
                    .to_dict()
                )
            except self.client.exceptions.ApiException as e:
                if e.status not in (403, 404):
                    raise Transient(e) from e
                self.objects[key] = None
            except Exception as e:
                if "ResourceNotFound" not in type(e).__name__:
                    raise Transient(e) from e
                self.objects[key] = None
        return self.objects[key]

    def read_pod(self, ns, name):
        try:
            resp = self.core.read_namespaced_pod(
                name, ns, _preload_content=False, _request_timeout=30
            )
            return json.loads(resp.data)
        except self.client.exceptions.ApiException as e:
            if e.status == 404:
                return None
            raise

    def parent_alive(self):
        if not self.pid:
            return True
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return True

    def heartbeat(self):
        tmp = os.path.join(self.streams, "alive.tmp")
        with open(tmp, "w") as f:
            f.write(f"{time.time():.0f}\n")
        os.replace(tmp, os.path.join(self.streams, "alive"))

    def run(self):
        self.log(
            f"started for run {self.run_id}, namespaces {' '.join(self.namespaces)}, watching pid {self.pid}"
        )
        reason = "stop file"
        while True:
            self.heartbeat()
            if os.path.exists(os.path.join(self.streams, "stop")):
                break
            if not self.parent_alive():
                reason = "ansible-playbook ended"
                break
            try:
                load_manifests(self.logs, self.seen, self.index)
                for ns in self.namespaces:
                    resp = self.core.list_namespaced_pod(
                        ns, _preload_content=False, _request_timeout=30
                    )
                    for pod in json.loads(resp.data).get("items", []):
                        self.consider(pod)
            except Exception as e:  # noqa: BLE001
                self.log(f"poll failed: {e}")
            time.sleep(POLL)
        self.log(f"stopping: {reason}")
        self.halt.set()
        deadline = time.time() + (GRACE if reason == "stop file" else 5)
        for t in self.threads:
            t.join(max(0, deadline - time.time()))
            self.heartbeat()
        left = [t.name for t in self.threads if t.is_alive()]
        summary = {
            "run": self.run_id,
            "stopped": reason,
            "streams": len(self.started),
            "still_open": left,
            "files": {
                k: os.path.getsize(v)
                for k, v in self.files.items()
                if os.path.exists(v)
            },
            "errors": self.errors[-50:],
        }
        with open(os.path.join(self.streams, "summary.json"), "w") as f:
            json.dump(summary, f, indent=2)
        self.log(f"stopped: {len(self.started)} streams, {len(left)} still open")
        os.remove(os.path.join(self.streams, "alive"))

    def consider(self, pod):
        meta = pod["metadata"]
        uid = meta["uid"]
        if uid not in self.placed:
            try:
                self.placed[uid] = place(pod, self.index, self.run_id, self.get_object)
            except Transient as e:
                self.log(f"cannot place {meta['name']} yet: {e}")
                return
        where = self.placed[uid]
        if not where:
            return
        for st in statuses(pod):
            cid = st.get("containerID")
            if not cid or (uid, st["name"], cid) in self.started:
                continue
            self.started.add((uid, st["name"], cid))
            n = self.instances.get((uid, st["name"]), 0)
            self.instances[(uid, st["name"])] = n + 1
            path = os.path.join(self.logs, where[0], f"{where[1]}{st['name']}.log")
            self.files[os.path.relpath(path, self.logs)] = path
            t = threading.Thread(
                target=self.follow,
                name=f"{meta['name']}/{st['name']}",
                daemon=True,
                args=(
                    meta["namespace"],
                    meta["name"],
                    uid,
                    st["name"],
                    cid,
                    path,
                    n,
                    st.get("restartCount", 0),
                ),
            )
            self.threads.append(t)
            t.start()

    def follow(self, ns, pod, uid, container, cid, path, earlier, restarts):
        marker = os.path.join(self.streams, f"{pod}.{container}.done")
        if os.path.exists(marker):
            os.remove(marker)
        self.log(f"following {pod}/{container} -> {os.path.relpath(path, self.logs)}")
        ended = False
        with open(path, "ab" if earlier else "wb") as out:
            if earlier:
                out.write(
                    f"\n--- {container} restarted (restart count {restarts}) ---\n".encode()
                )
            lines = Lines(out)
            while not ended and not self.halt.is_set():
                lines.reconnect()
                try:
                    resp = self.core.read_namespaced_pod_log(
                        pod,
                        ns,
                        container=container,
                        follow=True,
                        timestamps=True,
                        since_seconds=lines.since(),
                        _preload_content=False,
                        _request_timeout=(10, READ_TIMEOUT),
                    )
                    rest = b""
                    for chunk in resp.stream(1 << 16):
                        parts = (rest + chunk).split(b"\n")
                        rest = parts.pop()
                        for part in parts:
                            lines.write(part + b"\n")
                        out.flush()
                    if rest:
                        lines.write(rest)
                        out.flush()
                    resp.release_conn()
                except self.client.exceptions.ApiException as e:
                    if e.status == 404:
                        break
                    if e.status != 400:
                        self.errors.append(f"{pod}/{container}: {e.status} {e.reason}")
                except Exception as e:  # noqa: BLE001
                    if "Timeout" not in type(e).__name__ and "timed out" not in str(e):
                        self.errors.append(f"{pod}/{container}: {e}")
                try:
                    now = self.read_pod(ns, pod)
                except Exception:  # noqa: BLE001
                    time.sleep(1)
                    continue
                st = next(
                    (s for s in statuses(now or {}) if s["name"] == container), None
                )
                if now is None or now["metadata"]["uid"] != uid or st is None:
                    ended = True
                    break
                if st.get("containerID") and st["containerID"] != cid:
                    break
                ended = "running" not in (st.get("state") or {})
                if not ended:
                    time.sleep(1)
        if ended or self.halt.is_set():
            with open(marker, "w") as f:
                f.write(f"{os.path.relpath(path, self.logs)}\n")
        self.log(f"ended {pod}/{container}: {os.path.getsize(path)} bytes")


def main():
    run_dir, run_id, pid = sys.argv[1], sys.argv[2], sys.argv[3]
    namespaces = sys.argv[4:]
    streams = os.path.join(run_dir, "meta", "streams")
    os.makedirs(streams, exist_ok=True)
    lock = open(os.path.join(streams, "lock"), "w")  # noqa: SIM115
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            watched = int(pid) if b"ansible" in f.read() else 0
    except (OSError, ValueError):
        watched = 0
    stop = os.path.join(streams, "stop")
    if os.path.exists(stop):
        os.remove(stop)
    Streamer(run_dir, run_id, watched, namespaces).run()


if __name__ == "__main__":
    main()
