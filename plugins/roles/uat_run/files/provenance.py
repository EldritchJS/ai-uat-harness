#!/usr/bin/env python3
# Assisted by Claude Opus
"""Print the provenance of a run as JSON: git state, tool versions and the cluster login."""

import json
import os
import platform
import subprocess
import sys


def git(repo, *args):
    try:
        out = subprocess.run(
            ["git", "-C", repo, *args], capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def collection_version(repo, namespace, name):
    manifest = os.path.join(
        repo, "collections", "ansible_collections", namespace, name, "MANIFEST.json"
    )
    try:
        with open(manifest) as f:
            return json.load(f)["collection_info"]["version"]
    except (OSError, KeyError, ValueError):
        return ""


def connect():
    """An API client made the way kubernetes.core makes one: the in-cluster service account, else the kubeconfig."""
    from kubernetes import client, config

    try:
        config.load_incluster_config()
        return client.ApiClient(), {"source": "in-cluster"}
    except Exception:  # noqa: BLE001
        api = config.new_client_from_config()
        _, current = config.list_kube_config_contexts()
        return api, {"source": "kubeconfig", "context": current.get("name", "")}


def cluster():
    try:
        from kubernetes import client
    except ImportError:
        return {"error": "python kubernetes library not installed"}
    try:
        api, info = connect()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    info["server"] = api.configuration.host
    try:
        review = client.AuthenticationV1Api(api).create_self_subject_review(
            client.V1SelfSubjectReview()
        )
        user = review.status.user_info
        info["user"] = user.username
        info["groups"] = user.groups or []
    except Exception as e:  # noqa: BLE001
        info["user_error"] = str(e)
    return info


def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    try:
        from ansible.release import __version__ as ansible_version
    except ImportError:
        ansible_version = ""
    try:
        import kubernetes

        kubernetes_version = kubernetes.__version__
    except ImportError:
        kubernetes_version = ""
    status = git(repo, "status", "--porcelain")
    source = {
        "commit": git(repo, "rev-parse", "HEAD"),
        "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "remote": git(repo, "config", "--get", "remote.origin.url"),
        "dirty": bool(status),
        "changed_files": status.splitlines(),
    }
    if not source["commit"] and os.environ.get("UAT_SOURCE"):
        source = json.loads(os.environ["UAT_SOURCE"])
        source["copied_from_local_checkout"] = True
    print(
        json.dumps(
            {
                "git": source,
                "versions": {
                    "ansible_core": ansible_version,
                    "kubernetes_core": collection_version(repo, "kubernetes", "core"),
                    "python_kubernetes": kubernetes_version,
                    "python": platform.python_version(),
                },
                "host": platform.node(),
                "cluster": cluster(),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
