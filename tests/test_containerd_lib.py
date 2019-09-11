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

    # No registry and no k8s in our goal-state: return the upstream image
    kv().get.return_value = {}
    goal.return_value = {}
    assert containerd.get_sandbox_image() == '{}/{}'.format(upstream_registry, image_name)

    # No registry and no goal-state: return upstream or canonical depending on remote units
    kv().get.return_value = {}
    goal.side_effect = NotImplementedError()
    with mock.patch('charmhelpers.core.hookenv.relation_ids') as mock_rids, \
            mock.patch('charmhelpers.core.hookenv.remote_service_name') as mock_remote:
        mock_rids.return_value = ['foo']
        mock_remote.return_value = 'not-kubernetes'
        assert containerd.get_sandbox_image() == '{}/{}'.format(upstream_registry, image_name)

        mock_rids.return_value = ['foo']
        mock_remote.return_value = 'kubernetes-master'
        assert containerd.get_sandbox_image() == '{}/{}'.format(canonical_registry, image_name)

    # No registry with k8s in our goal-state: return the canonical image
    kv().get.return_value = {}
    goal.return_value = {'relations': {'containerd': {'kubernetes-master'}}}
    goal.side_effect = None
    assert containerd.get_sandbox_image() == '{}/{}'.format(canonical_registry, image_name)

    # A related registry should return registry[url]/image
    kv().get.return_value = {'url': related_registry}
    assert containerd.get_sandbox_image() == '{}/{}'.format(related_registry, image_name)
