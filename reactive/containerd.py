import os
import json
import requests

from subprocess import check_call, check_output, CalledProcessError

from charms.apt import purge

from charms.reactive import endpoint_from_flag
from charms.reactive import when, when_not, set_state, is_state

from charmhelpers.core import host
from charmhelpers.core.templating import render
from charmhelpers.core.hookenv import status_set, config

from charmhelpers.fetch import apt_install, apt_update, import_key


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


@when_not('containerd.nvidia.available')
@when_not('containerd.nvidia.ready')
def check_for_gpu():
    """
    Check if an Nvidia GPU
    exists.
    """
    cfg = config()
    out = check_output(['lspci', '-nnk']).rstrip().decode('utf-8').lower()

    if out.count('nvidia') > 0 and not cfg.get('disable_gpu'):
        set_state('containerd.nvidia.available')


@when('containerd.nvidia.available')
@when_not('containerd.nvidia.ready')
def configure_nvidia():
    status_set('maintenance', 'Installing Nvidia drivers.')

    dist = host.lsb_release()
    release = '{}{}'.format(
        dist['DISTRIB_ID'].lower(),
        dist['DISTRIB_RELEASE']
    )

    ncr_gpg_key = requests.get(
        'https://nvidia.github.io/nvidia-container-runtime/gpgkey').text
    import_key(ncr_gpg_key)
    with open(
        '/etc/apt/sources.list.d/nvidia-container-runtime.list', 'w'
    ) as f:
        f.write(
            'deb '
            'https://nvidia.github.io/libnvidia-container/{}/$(ARCH) /\n'
            .format(release)
        )
        f.write(
            'deb '
            'https://nvidia.github.io/nvidia-container-runtime/{}/$(ARCH) /\n'
            .format(release)
        )

    cuda_gpg_key = requests.get(
        'https://developer.download.nvidia.com/'
        'compute/cuda/repos/{}/x86_64/7fa2af80.pub'
        .format(release.replace('.', ''))
    ).text
    import_key(cuda_gpg_key)
    with open('/etc/apt/sources.list.d/cuda.list', 'w') as f:
        f.write(
            'deb '
            'http://developer.download.nvidia.com/'
            'compute/cuda/repos/{}/x86_64 /\n'
            .format(release.replace('.', ''))
        )

    apt_update()

    apt_install([
        'cuda-drivers',
        'nvidia-container-runtime',
    ], fatal=True)

    set_state('containerd.nvidia.ready')


@when('endpoint.containerd.departed')
def purge_containerd():
    """
    Purge Containerd from the
    cluster.

    :returns: None
    """
    purge('containerd')


@when('config.changed')
def config_changed():
    """
    Render the config template
    and restart the service.

    :returns: None
    """
    context = config()
    config_file = 'config.toml'
    config_directory = '/etc/containerd'

    # Mutate the input string into a dictionary.
    context['custom_registries'] = \
        json.loads(context['custom_registries'])

    if is_state('containerd.nvidia.available'):
        context['runtime'] = 'nvidia-container-runtime'
    else:
        context['runtime'] = 'runc'

    if not os.path.isdir(config_directory):
        os.mkdir(config_directory)

    render(
        config_file,
        os.path.join(config_directory, config_file),
        context
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
        nvidia_enabled=is_state('containerd.nvidia.ready')
    )
