from charmhelpers.core import hookenv, unitdata
from charmhelpers.core.hookenv import resource_get
from functools import lru_cache
import os
from pathlib import Path
from subprocess import check_call, check_output, CalledProcessError
from typing import Union


@lru_cache()
def arch():
    """Determine current machine's arch."""
    return check_output(["dpkg", "--print-architecture"]).decode().strip()


class ResourceFailure(Exception):
    """Custom exception to raise when resource isn't viable."""

    pass


def can_mount_cgroup2() -> bool:
    """Determine if it's possible to mount cgroup2 type filesystems."""
    try:
        stdout = check_output(["mount", "-t", "cgroup2"], text=True)
    except CalledProcessError:
        return False
    return "type cgroup2" in stdout


def unpack_containerd_resource() -> Union[None, Path]:
    """Unpack containerd resource and provide pathh to parent directory."""
    try:
        archive = resource_get("containerd")
    except Exception:
        raise ResourceFailure("Error fetching the containerd resource.")

    if not archive:
        raise ResourceFailure("Missing containerd resource.")

    charm_dir = os.getenv("CHARM_DIR")
    unpack_path = Path(charm_dir, "resources", "containerd")
    return _unpack_archive(archive, unpack_path)


def _unpack_archive(archive, unpack_path):
    unpack_path.mkdir(exist_ok=True, parents=True)
    archive = Path(archive)
    filesize = archive.stat().st_size
    if filesize == 0:
        return None
    if filesize < 10000000:
        raise ResourceFailure("Incomplete containerd resource")
    check_call(["tar", "xfz", archive, "-C", unpack_path])
    return _collect_resource_bins(unpack_path)


def _collect_resource_bins(unpack_path):
    for arch_based in unpack_path.glob("./*.tar.gz"):
        if arch() in arch_based.name:
            unpack_path = unpack_path / arch()
            return _unpack_archive(arch_based, unpack_path)
    bins = list(unpack_path.glob("bin/*"))
    if not bins:
        raise ResourceFailure("containerd resource didn't contain any binaries")
    for bin in bins:
        if bin.name == "containerd-shim":
            continue  # containerd-shim cannot run with '-v'
        try:
            check_call([bin, "-v"])
        except CalledProcessError:
            msg = f"containerd resource binary {bin.name} failed a version check"
            raise ResourceFailure(msg)
    return unpack_path / "bin"


def get_sandbox_image():
    """
    Return the container image location for the sandbox_image.

    Set an appropriate sandbox image based on known registries. Precedence should be:
    - related docker-registry
    - default charmed k8s registry (if related to kubernetes)
    - upstream

    :return: str container image location
    """
    db = unitdata.kv()
    canonical_registry = "rocks.canonical.com:443/cdk"
    upstream_registry = "k8s.gcr.io"

    docker_registry = db.get("registry", None)
    if docker_registry:
        sandbox_registry = docker_registry["url"]
    else:
        try:
            deployment = hookenv.goal_state()
        except NotImplementedError:
            relations = []
            for rid in hookenv.relation_ids("containerd"):
                relations.append(hookenv.remote_service_name(rid))
        else:
            relations = deployment.get("relations", {}).get("containerd", {})

        if any(
            k in relations
            for k in (
                "kubernetes-control-plane",
                "kubernetes-master",  # wokeignore:rule=master
                "kubernetes-worker",
            )
        ):
            sandbox_registry = canonical_registry
        else:
            sandbox_registry = upstream_registry

    return "{}/pause:3.6".format(sandbox_registry)
