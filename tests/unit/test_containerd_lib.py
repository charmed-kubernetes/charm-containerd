import pytest
import subprocess

from charmhelpers.core.unitdata import kv
from charmhelpers.core.hookenv import (
    goal_state as goal,
    relation_ids as mock_rids,
    remote_service_name as mock_remote,
)

from charms.layer import containerd
from pathlib import Path
from unittest import mock


def test_get_sandbox_image():
    """Verify we return a sandbox image from the appropriate registry."""
    image_name = "pause:3.6"

    canonical_registry = "ghcr.io/canonical/cdk"
    related_registry = "my.registry.com:5000"
    upstream_registry = "k8s.gcr.io"

    # No registry and no k8s in our goal-state: return the upstream image
    goal.return_value = {}
    assert containerd.get_sandbox_image() == "{}/{}".format(upstream_registry, image_name)

    # No registry and no goal-state: return upstream or canonical depending on remote units
    goal.side_effect = NotImplementedError()
    mock_rids.return_value = ["foo"]
    mock_remote.return_value = "not-kubernetes"
    assert containerd.get_sandbox_image() == "{}/{}".format(upstream_registry, image_name)

    mock_rids.return_value = ["foo"]
    mock_remote.return_value = "kubernetes-control-plane"
    assert containerd.get_sandbox_image() == "{}/{}".format(canonical_registry, image_name)

    # No registry with k8s in our goal-state: return the canonical image
    goal.return_value = {"relations": {"containerd": {"kubernetes-control-plane"}}}
    goal.side_effect = None
    assert containerd.get_sandbox_image() == "{}/{}".format(canonical_registry, image_name)

    # A related registry should return registry[url]/image
    kv().set("registry", {"url": related_registry})
    assert containerd.get_sandbox_image() == "{}/{}".format(related_registry, image_name)
    kv().pop("registry")


@mock.patch.object(containerd, "check_output")
def test_can_mount_cgroup2(mock_check_output):
    """Verifies the library method `can_mount_cgroup2`."""
    found = "cgroup2 on /sys/fs/cgroup type cgroup2 (rw,nosuid,nodev,noexec,relatime)"
    subprocess_failure = subprocess.CalledProcessError(
        returncode=1, cmd=["mount", "-t", "cgroup2"], stderr="mock error response"
    )

    mock_check_output.return_value = ""
    assert not containerd.can_mount_cgroup2()

    mock_check_output.return_value = found
    assert containerd.can_mount_cgroup2()

    mock_check_output.side_effect = subprocess_failure
    assert not containerd.can_mount_cgroup2()

    with pytest.raises(FileNotFoundError):
        mock_check_output.side_effect = FileNotFoundError("mount")
        containerd.can_mount_cgroup2()


RESOURCE_SIZE = 10000001


def _make_archive(path):
    with path.open("wb") as f:
        f.truncate(RESOURCE_SIZE)
    return path


def _fake_tar(outer, inner_names):
    """Emulate `tar xfz` for a multi-arch resource containing `inner_names`."""

    def check_call(cmd):
        if cmd[0] != "tar":
            return  # binary version check
        src, dest = Path(cmd[2]), Path(cmd[4])
        if src == outer:
            for name in inner_names:
                _make_archive(dest / name)
        else:
            (dest / "bin").mkdir(parents=True, exist_ok=True)
            (dest / "bin" / "containerd").touch()

    return check_call


@mock.patch.object(containerd, "arch", return_value="amd64")
@mock.patch.object(containerd, "check_call")
@mock.patch.object(containerd, "resource_get")
def test_unpack_containerd_resource_clears_stale_files(
    mock_resource_get, mock_check_call, _arch, tmp_path, monkeypatch
):
    """Archives and binaries left by a previous resource must not be installed."""
    archive = _make_archive(tmp_path / "containerd.tgz")
    mock_resource_get.return_value = str(archive)
    monkeypatch.setenv("CHARM_DIR", str(tmp_path / "charm"))
    unpack_path = tmp_path / "charm" / "resources" / "containerd"
    stale_archive = unpack_path / "containerd-1.6.38-linux-amd64.tar.gz"
    stale_bin = unpack_path / "amd64" / "bin" / "containerd-stale"
    stale_bin.parent.mkdir(parents=True)
    stale_archive.touch()
    stale_bin.touch()
    mock_check_call.side_effect = _fake_tar(
        archive, ["containerd-1.7.36-linux-amd64.tar.gz", "containerd-1.7.36-linux-arm64.tar.gz"]
    )

    assert containerd.unpack_containerd_resource() == unpack_path / "amd64" / "bin"

    assert not stale_archive.exists()
    assert not stale_bin.exists()
    assert mock_check_call.call_args_list[1].args[0][2].name == "containerd-1.7.36-linux-amd64.tar.gz"


@mock.patch.object(containerd, "arch", return_value="amd64")
@mock.patch.object(containerd, "check_call")
@mock.patch.object(containerd, "resource_get")
def test_unpack_containerd_resource_rejects_ambiguous_archives(
    mock_resource_get, mock_check_call, _arch, tmp_path, monkeypatch
):
    """A resource with several archives for one arch must not pick one arbitrarily."""
    archive = _make_archive(tmp_path / "containerd.tgz")
    mock_resource_get.return_value = str(archive)
    monkeypatch.setenv("CHARM_DIR", str(tmp_path / "charm"))
    mock_check_call.side_effect = _fake_tar(
        archive, ["containerd-1.6.38-linux-amd64.tar.gz", "containerd-1.7.36-linux-amd64.tar.gz"]
    )

    with pytest.raises(containerd.ResourceFailure, match="multiple amd64 archives"):
        containerd.unpack_containerd_resource()


@mock.patch.object(containerd.shutil, "rmtree", side_effect=PermissionError("denied"))
@mock.patch.object(containerd, "check_call")
@mock.patch.object(containerd, "resource_get")
def test_unpack_containerd_resource_fails_when_cleanup_fails(
    mock_resource_get, mock_check_call, _rmtree, tmp_path, monkeypatch
):
    """Stale files that cannot be removed must block rather than be reused."""
    mock_resource_get.return_value = str(_make_archive(tmp_path / "containerd.tgz"))
    monkeypatch.setenv("CHARM_DIR", str(tmp_path / "charm"))

    with pytest.raises(containerd.ResourceFailure, match="Failed to clear stale"):
        containerd.unpack_containerd_resource()
    mock_check_call.assert_not_called()
