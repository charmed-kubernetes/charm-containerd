import pytest
import subprocess

from charmhelpers.core.unitdata import kv
from charmhelpers.core.hookenv import (
    goal_state as goal,
    relation_ids as mock_rids,
    remote_service_name as mock_remote,
)

from charms.layer import containerd
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


@mock.patch.object(containerd, "arch", return_value="amd64")
@mock.patch.object(containerd, "check_call")
@mock.patch.object(containerd, "resource_get")
def test_unpack_containerd_resource_clears_stale_archives(mock_resource_get, mock_check_call, _arch, tmp_path):
    """Archives left by a previous resource must not be selected."""
    archive = tmp_path / "containerd.tgz"
    with archive.open("wb") as f:
        f.truncate(10000001)
    mock_resource_get.return_value = str(archive)
    unpack_path = tmp_path / "charm" / "resources" / "containerd"
    unpack_path.mkdir(parents=True)
    (unpack_path / "containerd-1.6.38-linux-amd64.tar.gz").touch()

    def fake_extract(cmd):
        if cmd[2] == archive:
            (cmd[4] / "containerd-1.7.36-linux-amd64.tar.gz").write_bytes(b"\0" * 10000001)
        else:
            (cmd[4] / "bin").mkdir(parents=True)
            (cmd[4] / "bin" / "containerd-shim").touch()

    mock_check_call.side_effect = fake_extract
    with mock.patch.dict(containerd.os.environ, {"CHARM_DIR": str(tmp_path / "charm")}):
        assert containerd.unpack_containerd_resource() == unpack_path / "amd64" / "bin"

    assert not (unpack_path / "containerd-1.6.38-linux-amd64.tar.gz").exists()
    assert mock_check_call.call_args_list[1].args[0][2].name == "containerd-1.7.36-linux-amd64.tar.gz"
