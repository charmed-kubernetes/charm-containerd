import logging
import shlex
from pathlib import Path
import pytest
import yaml
import toml

log = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test):
    """Build and deploy Containerd in bundle."""
    log.info("Build Charm...")
    charm = await ops_test.build_charm(".")

    overlays = [
        ops_test.Bundle("kubernetes-core", channel="edge"),
        Path("tests/data/charm.yaml"),
    ]

    log.info("Build Bundle...")
    bundle, *overlays = await ops_test.async_render_bundles(*overlays, charm=charm)

    log.info("Deploy Bundle...")
    model = ops_test.model_full_name
    cmd = f"juju deploy -m {model} {bundle} "
    cmd += " ".join(f"--overlay={f}" for f in overlays)
    rc, stdout, stderr = await ops_test.run(*shlex.split(cmd))
    assert rc == 0, f"Bundle deploy failed: {(stderr or stdout).strip()}"

    apps = [app for fragment in (bundle, *overlays) for app in yaml.safe_load(fragment.open())["applications"]]
    await ops_test.model.wait_for_idle(apps=apps, status="active", timeout=60 * 60)


async def test_status_messages(ops_test):
    """Validate that the status messages are correct."""
    for unit in ops_test.model.applications["containerd"].units:
        assert unit.workload_status == "active"
        assert unit.workload_status_message == "Container runtime available"


async def process_elapsed_time(unit, process):
    """Get elasped time of a running process."""
    result = await unit.run(f"ps -p `pidof {process}` -o etimes=")
    return int(result.data["results"]["Stdout"])


@pytest.mark.parametrize("which_action", ("containerd", "packages"))
async def test_upgrade_action(ops_test, which_action):
    """Test running upgrade action."""
    unit = ops_test.model.applications["containerd"].units[0]
    start = await process_elapsed_time(unit, "containerd")
    action = await unit.run_action(f"upgrade-{which_action}")
    output = await action.wait()  # wait for result
    assert output.data.get("status") == "completed"
    results = output.data.get("results", {})
    log.info(f"Upgrade results = '{results}'")
    assert results["containerd"]["available"] == results["containerd"]["installed"]
    assert results["containerd"]["upgrade-available"] == "False"
    assert not results["containerd"].get("upgrade-completed"), "No upgrade should have been run"
    end = await process_elapsed_time(unit, "containerd")
    assert end >= start, "containerd service shouldn't have been restarted"


@pytest.mark.parametrize("which_action", ("containerd", "packages"))
async def test_upgrade_dry_run_action(ops_test, which_action):
    """Test running upgrade action in dry-run mode."""
    unit = ops_test.model.applications["containerd"].units[0]
    start = await process_elapsed_time(unit, "containerd")
    action = await unit.run_action(f"upgrade-{which_action}", **{"dry-run": True})
    output = await action.wait()  # wait for result
    assert output.data.get("status") == "completed"
    results = output.data.get("results", {})
    log.info(f"Upgrade dry-run results = '{results}'")
    assert results["containerd"]["available"] == results["containerd"]["installed"]
    assert results["containerd"]["upgrade-available"] == "False"
    end = await process_elapsed_time(unit, "containerd")
    assert end >= start, "containerd service shouldn't have been restarted"


async def test_upgrade_action_containerd_force(ops_test):
    """Test running upgrade action without GPU and with force."""
    unit = ops_test.model.applications["containerd"].units[0]
    start = await process_elapsed_time(unit, "containerd")
    action = await unit.run_action(f"upgrade-packages", **{"force": True})
    output = await action.wait()  # wait for result
    assert output.data.get("status") == "completed"
    results = output.data.get("results", {})
    log.info(f"Upgrade results = '{results}'")
    assert results["containerd"]["available"] == results["containerd"]["installed"]
    assert results["containerd"]["upgrade-available"] == "False"
    assert not results["containerd"].get("upgrade-completed"), "No upgrade should have been run"
    end = await process_elapsed_time(unit, "containerd")
    assert end >= start, "containerd service shouldn't have been restarted"


async def test_upgrade_action_without_gpu_gpu_force(ops_test):
    """Test running upgrade action with GPU and force without GPU does nothing."""
    unit = ops_test.model.applications["containerd"].units[0]
    start = await process_elapsed_time(unit, "containerd")
    action = await unit.run_action(
        f"upgrade-packages", **{"containerd": False, "gpu": True, "force": True}
    )
    output = await action.wait()  # wait for result
    assert output.data.get("status") == "completed"
    results = output.data.get("results", {})
    log.info(f"Upgrade results = '{results}'")
    output = await unit.run("dpkg-query --list cuda-drivers")
    stderr = output.data["results"].get("Stderr")
    assert "cuda-drivers" in stderr, "cuda-drivers shouldn't be installed"
    end = await process_elapsed_time(unit, "containerd")
    assert end >= start, "containerd service shouldn't have been restarted"


@pytest.fixture(scope="module")
async def juju_config(ops_test):
    """Apply configuration for a test, then revert after the test is completed."""

    async def setup(application, _timeout=10 * 60, **new_config):
        """Apply config by application name and the config values.

        @param: str application: name of app to configure
        @param: dict new_config: configuration key=values to adjust
        @param: float  _timeout: time in seconds to wait for applications to be stable
        """
        await update_reverts(application, new_config.keys(), _timeout)
        await ops_test.model.applications[application].set_config(new_config)
        await ops_test.model.wait_for_idle(apps=[application], status="active", timeout=_timeout)

    async def update_reverts(application, configs, _timeout):
        """Control what config is reverted per app during the test module teardown.

        Because juju_config is a module scoped fixture, it isn't torn down until all the tests
        in the module are completed. The `setup` method could be called multiple times
        by various tests, but only the first call should gather the original config

        Subsequent calls, should update which keys are reverted, and the greatest timeout
        selected to revert all keys.
        """
        reverts = to_revert.get(application)
        if not reverts:
            reverts = (await ops_test.model.applications[application].get_config(), set(configs), _timeout)
        else:
            reverts = (reverts[0], reverts[1] | set(configs), max(reverts[2], _timeout))
        to_revert[application] = reverts

    to_revert = {}
    yield setup
    for app, (pre_test, settable, timeout) in to_revert.items():
        revert_config = {key: pre_test[key]["value"] for key in settable}
        await ops_test.model.applications[app].set_config(revert_config)
    await ops_test.model.wait_for_idle(apps=list(to_revert.keys()), status="active", timeout=timeout)


@pytest.fixture(scope="module", params=["v1", "v2"])
async def config_version(request, juju_config):
    """Set the containerd config_version based on a parameter."""
    await juju_config("containerd", config_version=request.param)
    return request.param


async def containerd_config(unit):
    """Gather containerd config and load as a dict from its toml representation."""
    output = await unit.run("cat /etc/containerd/config.toml")
    stdout = output.data["results"].get("Stdout")
    assert stdout, "Containerd output was empty"
    return toml.loads(stdout)


async def test_containerd_registry_has_dockerio_mirror(config_version, ops_test):
    """Test gathering the list of registries."""
    plugin = "cri" if config_version == "v1" else "io.containerd.grpc.v1.cri"
    for unit in ops_test.model.applications["containerd"].units:
        config = await containerd_config(unit)
        mirrors = config["plugins"][plugin]["registry"]["mirrors"]
        assert "docker.io" in mirrors, "docker.io missing from containerd config"
        assert mirrors["docker.io"]["endpoint"] == ["https://registry-1.docker.io"]


async def test_containerd_registry_with_private_registry(config_version, ops_test):
    """Test whether private registry config is represented in containerd."""
    registry_unit = ops_test.model.applications.get("docker-registry").units[0]
    plugin = "cri" if config_version == "v1" else "io.containerd.grpc.v1.cri"
    for unit in ops_test.model.applications["containerd"].units:
        config = await containerd_config(unit)
        configs = config["plugins"][plugin]["registry"]["configs"]
        assert len(configs) == 1, "registry config isn't represented in config.toml"
        docker_registry = next(iter(configs))
        assert configs[docker_registry]["tls"], "TLS config isn't represented in the config.toml"
        assert docker_registry in registry_unit.workload_status_message


async def test_containerd_nvidia_gpu_support(ops_test, juju_config):
    """Test that enabling gpu support installed nvidia drivers."""
    await juju_config("containerd", gpu_driver="nvidia", _timeout=15 * 60)
    for unit in ops_test.model.applications["containerd"].units:
        output = await unit.run("cat /etc/apt/sources.list.d/nvidia.list")
        stdout = output.data["results"].get("Stdout")
        assert stdout, "NVIDIA sources list was empty"

        output = await unit.run("dpkg-query --list cuda-drivers")
        stdout = output.data["results"].get("Stdout")
        assert "cuda-drivers" in stdout, "cuda-drivers not installed"


async def test_upgrade_action_gpu_force(ops_test):
    """Test running upgrade action with GPU and force."""
    unit = ops_test.model.applications["containerd"].units[0]
    start = await process_elapsed_time(unit, "containerd")
    action = await unit.run_action(
        f"upgrade-packages", **{"containerd": False, "gpu": True, "force": True}
    )
    output = await action.wait()  # wait for result
    assert output.data.get("status") == "completed"
    results = output.data.get("results", {})
    log.info(f"Upgrade results = '{results}'")
    assert results["cuda-drivers"]["available"] == results["cuda-drivers"]["installed"]
    assert results["cuda-drivers"]["upgrade-available"] == "False"
    assert results["cuda-drivers"]["upgrade-complete"] == "True"
    end = await process_elapsed_time(unit, "containerd")
    assert end >= start, "containerd service shouldn't have been restarted"


async def test_containerd_disable_gpu_support(ops_test, juju_config):
    """Test that disabling gpu support removes nvidia drivers."""
    await juju_config("containerd", gpu_driver="none")
    for unit in ops_test.model.applications["containerd"].units:
        output = await unit.run("cat /etc/apt/sources.list.d/nvidia.list")
        stderr = output.data["results"].get("Stderr")
        assert "No such file " in stderr, "NVIDIA sources list was populated"

        output = await unit.run("dpkg-query --list cuda-drivers")
        stderr = output.data["results"].get("Stderr")
        assert "cuda-drivers" in stderr, "cuda-drivers shouldn't be installed"
