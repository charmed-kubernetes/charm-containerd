import os

from subprocess import check_call, CalledProcessError

from charms.reactive import endpoint_from_flag
from charms.reactive import when, when_not, set_state

from charmhelpers.core import host
from charmhelpers.core.templating import render
from charmhelpers.core.hookenv import status_set, config


def _check_containerd():
    """
    Check that containerd is running.

    :returns: Boolean
    """
    try:
        check_call([
            'ctr',
            'c',
            'ls'
        ])
    except CalledProcessError:
        return False

    return True

@when('apt.installed.containerd')
@when_not('containerd.ready')
def install_containerd():
    """
    Actual install is handled
    by `layer-apt`.  We'll just
    configure it.

    :returns: None
    """
    config_changed()


@when('config.changed')
def config_changed():
    """
    Render the config template 
    and restart the service.

    :returns: None
    """
    config_file: str = 'config.toml'
    config_directory: str = '/etc/containerd'

    if not os.path.isdir(config_directory):
        os.mkdir(config_directory)

    render(
        config_file,
        os.path.join(config_directory, config_file),
        config()
    )

    host.service_restart('containerd')

    if _check_containerd():
        status_set('active', 'Container runtime available.')
        set_state('containerd.ready')

    else:
        status_set('blocked', 'Container runtime not available.')


@when('containerd.ready')
@when('endpoint.containerd.joined')
def pubish_config():
    """
    Pass configuration to principal
    charm.

    :returns: None
    """
    endpoint = endpoint_from_flag('endpoint.containerd.joined')
    endpoint.set_config(
        socket='unix:///var/run/containerd/containerd.sock',
        runtime='remote',  # TODO handle in k8s worker.
        nvidia_enabled=False
    )
