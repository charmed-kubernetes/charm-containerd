import asyncio
import logging
import jinja2
from juju.unit import Unit
from juju.application import Application
from pathlib import Path
import pytest
import pytest_asyncio
import shlex
import toml
from typing import Dict
from pytest_operator.plugin import OpsTest
from tenacity import retry, stop_after_attempt, wait_exponential
from utils import JujuRun
import yaml


log = logging.getLogger(__name__)


def format_kubectl_cmd(cmd: str) -> str:
    """Return a kubectl command with the kubeconfig path."""
    return f"kubectl --kubeconfig /root/.kube/config {cmd}"


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test):
    """Build and deploy Containerd in bundle."""
    log.info("Build Charm...")
    charm = next(Path.cwd().glob("containerd*.charm"), None)
    if not charm:
        log.info("Build Charm...")
        charm = await ops_test.build_charm(".")

    build_script = Path.cwd() / "build-resources.sh"
    resources = await ops_test.build_resources(build_script, with_sudo=False)
    expected_resources = {"containerd-multiarch"}

    if resources and all(rsc.stem in expected_resources for rsc in resources):
        resources = {rsc.stem.replace("-", "_"): rsc for rsc in resources}
    else:
        log.info("Failed to build resources, downloading from latest/edge")
        arch_resources = ops_test.arch_specific_resources(charm)
        resources = await ops_test.download_resources(charm, resources=arch_resources)
        resources = {name.replace("-", "_"): rsc for name, rsc in resources.items()}

    assert resources, "Failed to build or download charm resources."

    context = dict(charm=charm, **resources)
    overlays = [
        ops_test.Bundle("kubernetes-core", channel="edge"),
        Path("tests/data/charm.yaml"),
    ]

    log.info("Build Bundle...")
    bundle, *overlays = await ops_test.async_render_bundles(*overlays, **context)

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
    result = await JujuRun.command(unit, f"ps -p `pidof {process}` -o etimes=")
    return int(result.stdout)


@retry(wait=wait_exponential(multiplier=1, min=4, max=10), stop=stop_after_attempt(5))
async def pods_in_state(unit: Unit, selector: Dict[str, str], state: str = "Running"):
    """Retry checking until the pods all match a specified state."""
    format = ",".join("=".join(pairs) for pairs in selector.items())
    cmd = format_kubectl_cmd(f"get pods -l={format} --no-headers")
    result = await JujuRun.command(unit, cmd)
    pod_set = result.stdout.splitlines()
    assert pod_set and all(state in line for line in pod_set)
    return pod_set


@pytest.mark.parametrize("which_action", ("containerd", "packages"))
async def test_upgrade_action(ops_test, which_action):
    """Test running upgrade action."""
    unit = ops_test.model.applications["containerd"].units[0]
    start = await process_elapsed_time(unit, "containerd")
    output = await JujuRun.action(unit, f"upgrade-{which_action}")
    results = output.results
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
    output = await JujuRun.action(unit, f"upgrade-{which_action}", **{"dry-run": True})
    results = output.results
    log.info(f"Upgrade dry-run results = '{results}'")
    assert results["containerd"]["available"] == results["containerd"]["installed"]
    assert results["containerd"]["upgrade-available"] == "False"
    end = await process_elapsed_time(unit, "containerd")
    assert end >= start, "containerd service shouldn't have been restarted"


async def test_upgrade_action_containerd_force(ops_test):
    """Test running upgrade action without GPU and with force."""
    unit = ops_test.model.applications["containerd"].units[0]
    start = await process_elapsed_time(unit, "containerd")
    action = await JujuRun.action(unit, "upgrade-packages", force=True)
    results = action.results
    log.info(f"Upgrade results = '{results}'")
    assert results["containerd"]["available"] == results["containerd"]["installed"]
    assert results["containerd"]["upgrade-available"] == "False"
    assert not results["containerd"].get("upgrade-completed"), "No upgrade should have been run"
    end = await process_elapsed_time(unit, "containerd")
    assert end >= start, "containerd service shouldn't have been restarted"


async def test_upgrade_action_gpu_uninstalled_but_gpu_forced(ops_test):
    """Test running GPU force upgrade-action with no GPU drivers installed.

    upgrade-action with `GPU` and `force` flags both set but without GPU drivers currently
    installed should not upgrade any GPU drivers.
    """
    unit = ops_test.model.applications["containerd"].units[0]
    start = await process_elapsed_time(unit, "containerd")
    action = await JujuRun.action(unit, "upgrade-packages", containerd=False, gpu=True, force=True)
    results = action.results
    log.info(f"Upgrade results = '{results}'")
    action = await JujuRun.command(unit, "dpkg-query --list cuda-drivers", check=False)
    assert "cuda-drivers" in action.stderr, "cuda-drivers shouldn't be installed"
    end = await process_elapsed_time(unit, "containerd")
    assert end >= start, "containerd service shouldn't have been restarted"


@pytest_asyncio.fixture(scope="module")
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
    await ops_test.model.wait_for_idle(apps=list(to_revert.keys()), status="active")


@pytest_asyncio.fixture(scope="module", params=["v1", "v2"])
async def config_version(request, juju_config):
    """Set the containerd config_version based on a parameter."""
    await juju_config("containerd", config_version=request.param)
    return request.param


async def containerd_config(unit):
    """Gather containerd config and load as a dict from its toml representation."""
    output = await JujuRun.command(unit, "cat /etc/containerd/config.toml")
    assert output.stdout, "Containerd output was empty"
    return toml.loads(output.stdout)


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


async def test_containerd_disable_gpu_support(ops_test, juju_config):
    """Test that disabling gpu support removes nvidia drivers."""
    await juju_config("containerd", gpu_driver="none")
    for unit in ops_test.model.applications["containerd"].units:
        output = await JujuRun.command(unit, "cat /etc/apt/sources.list.d/nvidia.list", check=False)
        assert "No such file " in output.stderr, "NVIDIA sources list was populated"

        output = await JujuRun.command(unit, "dpkg-query --list cuda-drivers", check=False)
        assert "cuda-drivers" in output.stderr, "cuda-drivers shouldn't be installed"


async def test_containerd_nvidia_gpu_support(ops_test, juju_config):
    """Test that enabling gpu support installed nvidia drivers."""
    await juju_config("containerd", gpu_driver="nvidia", _timeout=15 * 60)
    for unit in ops_test.model.applications["containerd"].units:
        output = await JujuRun.command(unit, "cat /etc/apt/sources.list.d/nvidia.list")
        assert output.stdout, "NVIDIA sources list was empty"

        output = await JujuRun.command(unit, "dpkg-query --list cuda-drivers")
        assert "cuda-drivers" in output.stdout, "cuda-drivers not installed"


async def test_upgrade_action_gpu_force(ops_test):
    """Test running upgrade action with GPU and force."""
    unit = ops_test.model.applications["containerd"].units[0]
    start = await process_elapsed_time(unit, "containerd")
    action = await JujuRun.action(unit, "upgrade-packages", containerd=False, gpu=True, force=True)
    results = action.results
    log.info(f"Upgrade results = '{results}'")
    assert results["cuda-drivers"]["available"] == results["cuda-drivers"]["installed"]
    assert results["cuda-drivers"]["upgrade-available"] == "False"
    assert results["cuda-drivers"]["upgrade-complete"] == "True"
    end = await process_elapsed_time(unit, "containerd")
    assert end >= start, "containerd service shouldn't have been restarted"


@pytest_asyncio.fixture()
async def microbots(ops_test: OpsTest, tmp_path: Path):
    """Start microbots workload on each k8s-worker, cleanup at the end of the test."""
    workers: Application = ops_test.model.applications["kubernetes-worker"]
    any_worker: Unit = workers.units[0]
    arch = any_worker.machine.safe_data["hardware-characteristics"]["arch"]

    context = {
        "public_address": any_worker.public_address,
        "replicas": len(workers.units),
        "arch": arch,
    }
    rendered = str(tmp_path / "microbot.yaml")
    microbot = jinja2.Template(Path("tests/data/microbot.yaml.j2").read_text())
    microbot.stream(**context).dump(rendered)
    kubectl = format_kubectl_cmd("{command} -f /tmp/microbot.yaml")
    try:
        cmd = f"scp {rendered} {any_worker.name}:/tmp/microbot.yaml"
        await ops_test.juju(*shlex.split(cmd), check=True)

        await JujuRun.command(any_worker, kubectl.format(command="apply"))
        pods = await pods_in_state(any_worker, {"app": "microbot"}, "Running")
        yield len(pods)
    finally:
        await JujuRun.command(any_worker, kubectl.format(command="delete"))


async def test_restart_containerd(microbots, ops_test: OpsTest):
    """Test microbots continue running while containerd stopped."""
    containerds = ops_test.model.applications["containerd"]
    num_units = len(containerds.units)
    any_containerd = containerds.units[0]
    try:
        await asyncio.gather(*(JujuRun.command(_, "service containerd stop") for _ in containerds.units))
        async with ops_test.fast_forward():
            await ops_test.model.wait_for_idle(apps=["containerd"], status="blocked", timeout=6 * 60)

        nodes = await JujuRun.command(any_containerd, format_kubectl_cmd("get nodes"))
        assert nodes.stdout.count("NotReady") == num_units, "Ensure all nodes aren't ready"

        # test that pods are still running while containerd is offline
        pods = await JujuRun.command(any_containerd, format_kubectl_cmd("get pods -l=app=microbot"))
        assert pods.stdout.count("microbot") == microbots, f"Ensure {microbots} pod(s) are installed"
        assert pods.stdout.count("Running") == microbots, f"Ensure {microbots} pod(s) are running with containerd down"

        cluster_ip = await JujuRun.command(
            any_containerd,
            format_kubectl_cmd("get service -l=app=microbot -ojsonpath='{.items[*].spec.clusterIP}'"),
        )
        endpoint = f"http://{cluster_ip.stdout.strip()}"
        await JujuRun.command(any_containerd, f"curl {endpoint}")
    finally:
        await asyncio.gather(*(JujuRun.command(_, "service containerd start") for _ in containerds.units))
        async with ops_test.fast_forward():
            await ops_test.model.wait_for_idle(apps=["containerd"], status="active", timeout=6 * 60)
