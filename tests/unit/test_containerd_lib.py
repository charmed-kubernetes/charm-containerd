from charmhelpers.core.unitdata import kv
from charmhelpers.core.hookenv import (
    goal_state as goal,
    relation_ids as mock_rids,
    remote_service_name as mock_remote,
)

from charms.layer import containerd


def test_get_sandbox_image():
    """Verify we return a sandbox image from the appropriate registry."""
    image_name = 'pause:3.4.1'

    canonical_registry = 'rocks.canonical.com:443/cdk'
    related_registry = 'my.registry.com:5000'
    upstream_registry = 'k8s.gcr.io'

    # No registry and no k8s in our goal-state: return the upstream image
    goal.return_value = {}
    assert containerd.get_sandbox_image() == '{}/{}'.format(upstream_registry, image_name)

    # No registry and no goal-state: return upstream or canonical depending on remote units
    goal.side_effect = NotImplementedError()
    mock_rids.return_value = ['foo']
    mock_remote.return_value = 'not-kubernetes'
    assert containerd.get_sandbox_image() == '{}/{}'.format(upstream_registry, image_name)

    mock_rids.return_value = ['foo']
    mock_remote.return_value = 'kubernetes-control-plane'
    assert containerd.get_sandbox_image() == '{}/{}'.format(canonical_registry, image_name)

    # No registry with k8s in our goal-state: return the canonical image
    goal.return_value = {'relations': {'containerd': {'kubernetes-control-plane'}}}
    goal.side_effect = None
    assert containerd.get_sandbox_image() == '{}/{}'.format(canonical_registry, image_name)

    # A related registry should return registry[url]/image
    kv().set('registry', {'url': related_registry})
    assert containerd.get_sandbox_image() == '{}/{}'.format(related_registry, image_name)
