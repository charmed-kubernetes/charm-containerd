#!/usr/local/sbin/charm-env python3

import os
import sys

from charmhelpers.core.hookenv import (
    action_set,
    action_get,
    action_fail,
    config,
)

from charmhelpers.fetch import (
    apt_hold,
    apt_install,
    apt_update,
    apt_unhold,
)

from charmhelpers.fetch.ubuntu_apt_pkg import PkgVersion

from charmhelpers.core.host import service_restart

from charms.reactive import is_state, remove_state

from reactive.containerd import CONTAINERD_PACKAGE, apt_packages, install_nvidia_drivers


class ActionError(Exception):
    pass


def _gpu_packages():
    """Returns list of packages required for specific gpu support"""
    if is_state("containerd.nvidia.ready"):
        return config("nvidia_apt_packages").split()
    return []


def _package_list(containerd, gpu):
    package_list = []
    if not containerd and not gpu:
        raise ActionError("Must select at-least one of container and gpu")

    if containerd:
        package_list += [CONTAINERD_PACKAGE]

    if gpu and _gpu_packages():
        package_list += _gpu_packages()

    return set(package_list)


def _dry_run(containerd, gpu):
    """Determine if a new package is available."""
    apt_update(fatal=True)
    package_list = _package_list(containerd, gpu)
    search = apt_packages(package_list)
    for name in package_list:
        if name not in search:
            raise ActionError(f"Package '{name}' not found in apt.")

    result = {}
    for name, pkg in search.items():
        current_ver = pkg.current_ver.ver_str if pkg.current_ver else "0.not-installed.0"
        # there could be a package not install with a current version, assume it needs updating?
        available, installed = map(PkgVersion, (pkg.version, current_ver))
        result[f"{name}.available"] = available.version
        result[f"{name}.installed"] = installed.version
        result[f"{name}.upgrade-available"] = available > installed

    return result


def _upgrade(containerd, gpu):
    """Do actual upgrade."""

    if not containerd and not gpu:
        raise ActionError("Must select at-least one of container and gpu")

    upgrade_list = _dry_run(containerd, gpu)
    try:
        pkg = CONTAINERD_PACKAGE
        if upgrade_list.get(f"{pkg}.upgrade-available"):
            apt_update(fatal=True)
            apt_unhold(pkg)
            apt_install(pkg, fatal=True)
            apt_hold(pkg)
            upgrade_list[f"{pkg}.upgrade-complete"] = True

        if any(upgrade_list.get(f"{pkg}.upgrade-available") for pkg in _gpu_packages()):
            install_nvidia_drivers(reconfigure=False)
            for pkg in _gpu_packages():
                upgrade_list[f"{pkg}.upgrade-complete"] = True

        if any(upgrade_list.get(f"{pkg}.upgrade-complete") for pkg in upgrade_list):
            service_restart(CONTAINERD_PACKAGE)
            remove_state("containerd.version-published")

        return upgrade_list

    except Exception as e:
        raise ActionError("Failed to complete upgrades") from e


def upgrade_main(containerd, gpu):
    """Upgrade containerd to the latest in apt."""
    dry_run = action_get().get("dry-run")

    try:
        if dry_run:
            result = _dry_run(containerd, gpu)
        else:
            result = _upgrade(containerd, gpu)
        action_set(result)
    except ActionError as ae:
        action_fail(str(ae))


def main(args):
    action_name = os.path.basename(args[0])
    if action_name == "upgrade-containerd":
        upgrade_main(True, False)
    elif action_name == "upgrade-packages":
        containerd = action_get().get("containerd")
        gpu = action_get().get("gpu")
        upgrade_main(containerd, gpu)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
