#
# Copyright (c) 2017 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

""" System Inventory Puppet common top level code."""

from eventlet.green import subprocess
import os

from oslo_log import log as logging
from sysinv._i18n import _
from sysinv.common import constants
from sysinv.common import exception
from tsconfig import tsconfig


LOG = logging.getLogger(__name__)

PUPPET_HIERADATA_PATH = os.path.join(tsconfig.PUPPET_PATH, 'hieradata')

# runtime applied manifest constants
REPORT_STATUS_CFG = 'report_status'
REPORT_SUCCESS = 'report_success'
REPORT_FAILURE = 'report_failure'

REPORT_INVENTORY_UPDATE = 'inventory_update'

# name of manifest config operations to report back to sysinv conductor
REPORT_ROUTE_CONFIG = 'route_config'
REPORT_UPGRADE_ACTIONS = 'upgrade_actions'
REPORT_AIO_CINDER_CONFIG = 'aio_cinder_config'
REPORT_DISK_PARTITON_CONFIG = 'manage_disk_partitions'
REPORT_LVM_BACKEND_CONFIG = 'lvm_config'
REPORT_EXTERNAL_BACKEND_CONFIG = 'external_config'
REPORT_CEPH_BACKEND_CONFIG = 'ceph_config'
REPORT_CEPH_EXTERNAL_BACKEND_CONFIG = 'ceph_external_config'
REPORT_CEPH_SERVICES_CONFIG = 'ceph_services'
REPORT_CEPH_MONITOR_CONFIG = 'ceph_monitor'
REPORT_PCI_SRIOV_CONFIG = 'pci_sriov_config'
REPORT_CEPH_OSD_CONFIG = 'ceph_osd'
REPORT_CEPH_RADOSGW_CONFIG = 'ceph_radosgw'
REPORT_CEPH_ROOK_CONFIG = 'ceph_rook_config'
# puppet report configs for hosts cert update
REPORT_KUBE_CERT_UPDATE_TRUSTBOTHCAS = constants.KUBE_CERT_UPDATE_TRUSTBOTHCAS
REPORT_KUBE_CERT_UPDATE_UPDATECERTS = constants.KUBE_CERT_UPDATE_UPDATECERTS
REPORT_KUBE_CERT_UPDATE_TRUSTNEWCA = constants.KUBE_CERT_UPDATE_TRUSTNEWCA
# puppet report configs for pods cert update
REPORT_KUBE_CERT_UPDATE_PODS_TRUSTBOTHCAS = \
    'pods_' + constants.KUBE_CERT_UPDATE_TRUSTBOTHCAS
REPORT_KUBE_CERT_UPDATE_PODS_TRUSTNEWCA = \
    'pods_' + constants.KUBE_CERT_UPDATE_TRUSTNEWCA


def puppet_apply_manifest(ip_address, personality,
                          manifest=None, runtime=None, do_reboot=False,
                          hieradata_path=PUPPET_HIERADATA_PATH):
    """ Apply configuration for the specified manifest."""
    if not manifest:
        manifest = personality

    cmd = [
        "/usr/local/bin/puppet-manifest-apply.sh",
        hieradata_path,
        str(ip_address),
        personality,
        manifest
    ]

    if runtime:
        cmd.append(runtime)

    try:
        if do_reboot:
            LOG.warn("Sysinv will be rebooting the node post "
                     "manifest application")

            with open("/dev/console", "w") as fconsole:
                cmdstr = " ".join(cmd) + ' && reboot'
                subprocess.Popen(cmdstr,
                                 stdout=fconsole,
                                 stderr=fconsole,
                                 shell=True)
        else:
            with open(os.devnull, "w") as fnull:
                subprocess.check_call(cmd, stdout=fnull, stderr=fnull)  # pylint: disable=not-callable
    except subprocess.CalledProcessError:
        msg = "Failed to execute %s manifest for host %s" % \
              (manifest, ip_address)
        LOG.exception(msg)
        raise exception.SysinvException(_(msg))
