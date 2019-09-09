import pytest
from unittest import mock

from charms.layer import containerd


def patch_fixture(patch_target):
    @pytest.fixture()
    def _fixture():
        with mock.patch(patch_target) as m:
            yield m
    return _fixture


arch = patch_fixture('charmhelpers.core.host.arch')
goal = patch_fixture('charmhelpers.core.hookenv.goal_state')
kv = patch_fixture('charmhelpers.core.unitdata.kv')


def test_get_sandbox_image(arch, goal, kv):
    '''Verify we return a sandbox image from the appropriate registry.'''
    arch.return_value = 'foo'
    image_name = 'pause-{}:3.1'.format(arch.return_value)

    canonical_registry = 'rocks.canonical.com:443/cdk'
    related_registry = 'my.registry.com:5000'
    upstream_registry = 'k8s.gcr.io'

    # No related docker-registry and no k8s relations should return the upstream image
    kv.return_value = {}
    goal.return_value = {}
    assert containerd.get_sandbox_image() == '{}/{}'.format(upstream_registry, image_name)

    # No related docker-registry and no goal-state should return the upstream image
    kv.return_value = {}
    goal.side_effect = NotImplementedError()
    assert containerd.get_sandbox_image() == '{}/{}'.format(upstream_registry, image_name)

    # No related docker-registry with a k8s relation should return the canonical image
    kv.return_value = {}
    goal.return_value = {'relations': {'containerd': {'kubernetes-master'}}}
    goal.side_effect = None
    assert containerd.get_sandbox_image() == '{}/{}'.format(canonical_registry, image_name)

    # Related docker-registry should return the kv registry[url]
    kv.return_value = {'registry': {'url': related_registry}}
    assert containerd.get_sandbox_image() == '{}/{}'.format(related_registry, image_name)
