import logging

import pytest

log = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test):
    """Build and deploy Containerd in bundle."""
    containerd_charm = await ops_test.build_charm(".")
    bundle = ops_test.render_bundle(
        "tests/data/bundle.yaml", master_charm=containerd_charm, series="focal",
    )
    await ops_test.model.deploy(bundle)
    await ops_test.model.wait_for_idle(timeout=60 * 60)
    # note (rgildein): We don't care if kubernetes master will be ready,
    #                  due testing on LXD.
    #                  https://bugs.launchpad.net/charm-kubernetes-worker/+bug/1903566
    await ops_test.model.wait_for_idle(
        apps=["containerd", "flannel", "easyrsa", "etcd", "kubernetes-worker"],
        wait_for_active=True
    )


async def test_status_messages(ops_test):
    """Validate that the status messages are correct."""
    for unit in ops_test.model.applications["containerd"].units:
        assert unit.workload_status == "active"
        assert unit.workload_status_message == "Container runtime available"


async def test_upgrade_containerd_action(ops_test):
    """Test running upgrade-containerd action."""
    unit = ops_test.model.applications["containerd"].units[0]
    action = await unit.run_action("upgrade-containerd")
    output = await action.wait()  # wait for result
    assert output.data.get("status") == "completed"
    assert output.data.get("results", {}).get("runtime") == "containerd"
