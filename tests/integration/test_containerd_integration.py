import logging

import pytest
import toml

log = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test):
    """Build and deploy Containerd in bundle."""
    bundle = ops_test.render_bundle(
        "tests/data/bundle.yaml",
        master_charm=await ops_test.build_charm("."),
        series="focal",
    )
    # Use CLI to deploy bundle until https://github.com/juju/python-libjuju/pull/497
    # is released.
    # await ops_test.model.deploy(bundle)
    retcode, stdout, stderr = await ops_test._run(
        "juju", "deploy", "-m", ops_test.model_full_name, bundle
    )
    assert retcode == 0, "Bundle deploy failed: {}".format((stderr or stdout).strip())
    await ops_test.model.wait_for_idle(timeout=60 * 60)
    # note (rgildein): We don't care if kubernetes master will be ready,
    #                  due testing on LXD.
    #                  https://bugs.launchpad.net/charm-kubernetes-worker/+bug/1903566
    await ops_test.model.wait_for_idle(
        apps=["containerd", "flannel", "easyrsa", "etcd", "kubernetes-worker"],
        wait_for_active=True,
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


async def test_containerd_registry_has_dockerio_mirror(ops_test):
    """Test gathering the list of registries."""
    for unit in ops_test.model.applications["containerd"].units:
        config = await containerd_config(unit)
        mirrors = config["plugins"]["cri"]["registry"]["mirrors"]
        assert "docker.io" in mirrors, "docker.io missing from containerd config"
        assert mirrors["docker.io"]["endpoint"] == ["https://registry-1.docker.io"]


@pytest.fixture(scope="module")
async def private_registry(ops_test):
    """Create and connect Module Fixture for  a private registry to containerd."""
    registry = ops_test.model.applications.get("docker-registry")
    if not registry:
        await ops_test.model.deploy(
            "cs:~containers/docker-registry", "docker-registry", channel="edge"
        )
        registry = ops_test.model.applications["docker-registry"]
    if not any(rel.matches("containerd:docker-registry") for rel in registry.relations):
        await ops_test.model.add_relation(
            "docker-registry:docker-registry", "containerd:docker-registry"
        )
    if not any(rel.matches("easyrsa:client") for rel in registry.relations):
        await ops_test.model.add_relation(
            "docker-registry:cert-provider", "easyrsa:client"
        )
    await ops_test.model.wait_for_idle(
        apps=["containerd", "docker-registry", "easyrsa"], wait_for_active=True
    )
    yield registry
    await registry.destroy()


async def containerd_config(unit):
    """Gather containerd config and load as a dict from its toml representation."""
    output = await unit.run("cat /etc/containerd/config.toml")
    stdout = output.data["results"].get("Stdout")
    assert stdout, "Containerd output was empty"
    return toml.loads(stdout)


async def test_containerd_registry_with_private_registry(ops_test, private_registry):
    """Test whether private registry config is represented in containerd."""
    registry_unit = private_registry.units[0]
    for unit in ops_test.model.applications["containerd"].units:
        config = await containerd_config(unit)
        configs = config["plugins"]["cri"]["registry"]["configs"]
        assert len(configs) == 1, "registry config isn't represented in config.toml"
        docker_registry = next(iter(configs))
        assert configs[docker_registry][
            "tls"
        ], "TLS config isn't represented in the config.toml"
        assert docker_registry in registry_unit.workload_status_message
