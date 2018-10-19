#
# Copyright (c) 2018 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

from sysinv.common import constants
from sysinv.common import exception
from sysinv.common.storage_backend_conf import StorageBackendConfig

from sysinv.openstack.common import log as logging
from . import common
from . import openstack

LOG = logging.getLogger(__name__)


class CinderHelm(openstack.OpenstackBaseHelm):
    """Class to encapsulate helm operations for the cinder chart"""

    CHART = constants.HELM_CHART_CINDER
    SUPPORTED_NAMESPACES = [
        common.HELM_NS_OPENSTACK
    ]

    SERVICE_NAME = 'cinder'
    AUTH_USERS = ['cinder']

    @property
    def docker_repo_source(self):
        return common.DOCKER_SRC_STX

    @property
    def docker_repo_tag(self):
        return common.DOCKER_SRCS[self.docker_repo_source][common.IMG_TAG_KEY]

    def get_namespaces(self):
        return self.SUPPORTED_NAMESPACES

    def get_overrides(self, namespace=None):
        overrides = {
            common.HELM_NS_OPENSTACK: {
                'pod': {
                    'replicas': {
                        'api': self._num_controllers(),
                        'volume': self._num_controllers(),
                        'scheduler': self._num_controllers(),
                        'backup': self._num_controllers()
                    }
                },
                'images': self._get_images_overrides(),
                'conf': {
                    'cinder': self._get_conf_cinder_overrides(),
                    'ceph': self._get_conf_ceph_overrides(),
                    'backends': self._get_conf_backends_overrides(),
                },
                'storage': 'rbd',
                'endpoints': self._get_endpoints_overrides(),
            }
        }

        if namespace in self.SUPPORTED_NAMESPACES:
            return overrides[namespace]
        elif namespace:
            raise exception.InvalidHelmNamespace(chart=self.CHART,
                                                 namespace=namespace)
        else:
            return overrides

    def _get_conf_ceph_overrides(self):
        ceph_backend = self._get_primary_ceph_backend()
        if not ceph_backend:
            return {}

        # Get Ceph monitors.
        ceph_mon_ips = StorageBackendConfig.get_ceph_mon_ip_addresses(
            self.dbapi).values()

        replication, min_replication =\
            StorageBackendConfig.get_ceph_pool_replication(self.dbapi)

        # We don't use the chart to configure the cinder-volumes
        # pool, so these values don't have any impact right now.
        ruleset = 0

        conf_ceph = {
            'monitors': [
                "%s:6789" % ceph_mon_ips[0],
                "%s:6789" % ceph_mon_ips[1],
                "%s:6789" % ceph_mon_ips[2]
            ],
            'admin_keyring': 'null',
            'pools': {
                'backup': {
                    # We use the chart to configure the pool for backups, so
                    # it's safe to use the same replication as for the primary
                    # tier pools.
                    'replication': replication,
                    'crush_rule': ruleset,
                    'chunk_size': 8
                },
                'volume': {
                    # We don't use the chart to configure the cinder-volumes
                    # pool, so these values don't have any impact right now.
                    'replication': replication,
                    'crush_rule': ruleset,
                    'chunk_size': 8
                }
            }
        }

        return conf_ceph

    def _get_conf_cinder_overrides(self):
        # Get all the internal CEPH backends.
        backends = self.dbapi.storage_backend_get_list_by_type(
            backend_type=constants.SB_TYPE_CEPH)
        conf_cinder = {
            'DEFAULT': {
                # Use the CEPH backup driver. The chart will create a pool for
                # keeping the backups. All cinder backup pods will use it.
                'backup_driver': 'cinder.backup.drivers.ceph',
                'enabled_backends': ','.join(
                    b.name.encode('utf8', 'strict') for b in backends)
            },
        }

        # If there are more CEPH backends, we default to the volume type
        # associated with the primary CEPH tier, which currently has the name
        # set to 'ceph-store'.
        if len(backends) > 1:
            default = next(
                (b.name for b in backends
                 if b.name ==
                 constants.SB_DEFAULT_NAMES[constants.SB_TYPE_CEPH]), None)
            if default:
                conf_cinder['DEFAULT']['default_volume_type'] = \
                    default.encode('utf8', 'strict')

        return conf_cinder

    def _get_conf_backends_overrides(self):
        conf_backends = {}

        # We don't use the chart's default backends.
        conf_backends['rbd1'] = ""

        # Get tier info.
        tiers = self.dbapi.storage_tier_get_list()
        primary_tier_name =\
            constants.SB_TIER_DEFAULT_NAMES[constants.SB_TIER_TYPE_CEPH]

        # We support primary and secondary CEPH tiers.
        backends = self.dbapi.storage_backend_get_list_by_type(
            backend_type=constants.SB_TYPE_CEPH)

        # No data if there are no CEPH backends.
        if not backends:
            return {}

        for bk in backends:
            bk_name = bk.name.encode('utf8', 'strict')
            tier = next((t for t in tiers if t.forbackendid == bk.id), None)
            if not tier:
                raise Exception("No tier present for backend %s" % bk_name)

            if tier.name == primary_tier_name:
                rbd_pool = constants.CEPH_POOL_VOLUMES_NAME
            else:
                rbd_pool = "%s-%s" % (constants.CEPH_POOL_VOLUMES_NAME,
                                      tier.name)

            conf_backends[bk_name] = {
                'volume_backend_name': bk_name,
                'volume_driver': 'cinder.volume.drivers.rbd.RBDDriver',
                'rbd_pool': rbd_pool.encode('utf8', 'strict'),
                'rbd_ceph_conf':
                    (constants.CEPH_CONF_PATH +
                     constants.SB_TYPE_CEPH_CONF_FILENAME),
                'rbd_user': 'cinder',
            }

        return conf_backends

    def _get_endpoints_identity_users_overrides(self):
        overrides = {}
        overrides.update(self._get_common_users_overrides(self.SERVICE_NAME))

        for user in self.AUTH_USERS:
            overrides.update({
                user: {
                    'region_name': self._region_name(),
                    'password': self._get_keyring_password(self.SERVICE_NAME,
                                                           user)
                }
            })
        return overrides

    def _get_endpoints_identity_overrides(self):
        return {'auth': self._get_endpoints_identity_users_overrides()}

    def _get_endpoints_overrides(self):
        return {
            'identity': self._get_endpoints_identity_overrides(),
        }

    def _get_images_overrides(self):
        heat_image = self._operator.chart_operators[
            constants.HELM_CHART_HEAT].docker_image
        return {
            'tags': {
                'bootstrap': heat_image,
                'cinder_api': self.docker_image,
                'cinder_backup': self.docker_image,
                'cinder_db_sync': self.docker_image,
                'cinder_scheduler': self.docker_image,
                'cinder_volume': self.docker_image,
                'cinder_volume_usage_audit': self.docker_image,
                'db_drop': heat_image,
                'db_init': heat_image,
                'ks_endpoints': heat_image,
                'ks_service': heat_image,
                'ks_user': heat_image,
            }
        }

    def _get_primary_ceph_backend(self):
        try:
            backend = self.dbapi.storage_backend_get_by_name(
                constants.SB_DEFAULT_NAMES[constants.SB_TYPE_CEPH])
        except Exception:
            backend = None
            pass

        return backend
