#!/usr/bin/env python
# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2013 Red Hat, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# Copyright (c) 2013-2020 Wind River Systems, Inc.
#

from eventlet.green import subprocess
import jsonpatch
import netaddr
import os
import pecan
import re
import socket
import uuid
import wsme

from fm_api import constants as fm_constants
import tsconfig.tsconfig as tsc

from oslo_config import cfg
from oslo_log import log
from sysinv._i18n import _
from sysinv.common import ceph
from sysinv.common import constants
from sysinv.common import exception
from sysinv.common import health
from sysinv.helm import common as helm_common

LOG = log.getLogger(__name__)

CONF = cfg.CONF

LOG = log.getLogger(__name__)

JSONPATCH_EXCEPTIONS = (jsonpatch.JsonPatchException,
                        jsonpatch.JsonPointerException,
                        KeyError)


def ip_version_to_string(ip_version):
    return str(constants.IP_FAMILIES[ip_version])


def validate_limit(limit):
    if limit and limit < 0:
        raise wsme.exc.ClientSideError(_("Limit must be positive"))

    if limit:
        return min(CONF.api_limit_max, limit) or CONF.api_limit_max
    else:
        return CONF.api_limit_max


def validate_sort_dir(sort_dir):
    if sort_dir not in ['asc', 'desc']:
        raise wsme.exc.ClientSideError(_("Invalid sort direction: %s. "
                                         "Acceptable values are "
                                         "'asc' or 'desc'") % sort_dir)
    return sort_dir


def validate_patch(patch):
    """Performs a basic validation on patch."""

    if not isinstance(patch, list):
        patch = [patch]

    for p in patch:
        path_pattern = re.compile("^/[a-zA-Z0-9-_]+(/[a-zA-Z0-9-_]+)*$")

        if not isinstance(p, dict) or \
                any(key for key in ["path", "op"] if key not in p):
            raise wsme.exc.ClientSideError(_("Invalid patch format: %s")
                                             % str(p))

        path = p["path"]
        op = p["op"]

        if op not in ["add", "replace", "remove"]:
            raise wsme.exc.ClientSideError(_("Operation not supported: %s")
                                             % op)

        if not path_pattern.match(path):
            raise wsme.exc.ClientSideError(_("Invalid path: %s") % path)

        if op == "add":
            if path.count('/') == 1:
                raise wsme.exc.ClientSideError(_("Adding an additional "
                                                 "attribute (%s) to the "
                                                 "resource is not allowed")
                                                 % path)


def validate_mtu(mtu):
    """Check if MTU is valid"""
    if mtu < 576 or mtu > 9216:
        raise wsme.exc.ClientSideError(_(
            "MTU must be between 576 and 9216 bytes."))


def validate_address_within_address_pool(ip, pool):
    """Determine whether an IP address is within the specified IP address pool.
       :param ip netaddr.IPAddress object
       :param pool objects.AddressPool object
    """
    ipset = netaddr.IPSet()
    for start, end in pool.ranges:
        ipset.update(netaddr.IPRange(start, end))

    if netaddr.IPAddress(ip) not in ipset:
        raise wsme.exc.ClientSideError(_(
            "IP address %s is not within address pool ranges" % str(ip)))


def validate_address_within_nework(ip, network):
    """Determine whether an IP address is within the specified IP network.
       :param ip netaddr.IPAddress object
       :param network objects.Network object
    """
    pool = pecan.request.dbapi.address_pool_get(network.pool_uuid)
    validate_address_within_address_pool(ip, pool)


class ValidTypes(wsme.types.UserType):
    """User type for validate that value has one of a few types."""

    def __init__(self, *types):
        self.types = types

    def validate(self, value):
        for t in self.types:
            if t is wsme.types.text and isinstance(value, wsme.types.bytes):
                value = value.decode()
            if isinstance(value, t):
                return value

        raise ValueError("Wrong type. Expected '%s', got '%s'" % (
                         self.types, type(value)))


def is_valid_subnet(subnet, ip_version=None):
    """Determine whether an IP subnet is valid IPv4 subnet.
       Raise Client-Side Error on failure.
    """

    if ip_version is not None and subnet.version != ip_version:
        raise wsme.exc.ClientSideError(_(
            "Invalid IP version %s %s. "
            "Please configure valid %s subnet") %
            (subnet.version, subnet, ip_version_to_string(ip_version)))
    elif subnet.size < 8:
        raise wsme.exc.ClientSideError(_(
            "Invalid subnet size %s with %s. "
            "Please configure at least size /24 subnet") %
            (subnet.size, subnet))
    elif subnet.ip != subnet.network:
        raise wsme.exc.ClientSideError(_(
            "Invalid network address %s."
            "Network address of subnet is %s. "
            "Please configure valid %s subnet.") %
            (subnet.ip, subnet.network, ip_version_to_string(ip_version)))


def is_valid_address_within_subnet(ip_address, subnet):
    """Determine whether an IP address is valid and within
       the specified subnet.  Raise on Client-Side Error on failure.
    """

    if ip_address.version != subnet.version:
        raise wsme.exc.ClientSideError(_(
            "Invalid IP version %s %s. "
            "Please configure valid %s address.") %
            (ip_address.version, subnet, ip_version_to_string(subnet.version)))
    elif ip_address == subnet.network:
        raise wsme.exc.ClientSideError(_(
            "Invalid IP address: %s. "
            "Cannot use network address: %s. "
            "Please configure valid %s address.") %
            (ip_address, subnet.network, ip_version_to_string(subnet.version)))
    elif ip_address == subnet.broadcast:
        raise wsme.exc.ClientSideError(_(
            "Cannot use broadcast address: %s. "
            "Please configure valid %s address.") %
            (subnet.broadcast, ip_version_to_string(subnet.version)))
    elif ip_address not in subnet:
        raise wsme.exc.ClientSideError(_(
            "IP Address %s is not in subnet: %s. "
            "Please configure valid %s address.") %
            (ip_address, subnet, ip_version_to_string(subnet.version)))

    return True


def is_valid_hostname(hostname):
    """Determine whether an address is valid as per RFC 1123.
    """

    # Maximum length of 255
    rc = True
    length = len(hostname)
    if length > 255:
        raise wsme.exc.ClientSideError(_(
            "Hostname %s is too long.  Length %s is greater than 255."
            "Please configure valid hostname.") % (hostname, length))

    # Allow a single dot on the right hand side
    if hostname[-1] == ".":
        hostname = hostname[:-1]
    # Create a regex to ensure:
    # - hostname does not begin or end with a dash
    # - each segment is 1 to 63 characters long
    # - valid characters are A-Z (any case) and 0-9
    valid_re = re.compile("(?!-)[A-Z\d-]{1,63}(?<!-)$", re.IGNORECASE)
    rc = all(valid_re.match(x) for x in hostname.split("."))
    if not rc:
        raise wsme.exc.ClientSideError(_(
            "Hostname %s is invalid.  Hostname may not begin or end with"
            " a dash. Each segment is 1 to 63 chars long and valid"
            " characters are A-Z, a-z,  and 0-9."
            " Please configure valid hostname.") % (hostname))

    return rc


def is_host_active_controller(host):
    """Returns True if the supplied host is the active controller."""
    if host['personality'] == constants.CONTROLLER:
        return host['hostname'] == socket.gethostname()
    return False


def is_host_simplex_controller(host):
    return host['personality'] == constants.CONTROLLER and \
           os.path.isfile(tsc.PLATFORM_SIMPLEX_FLAG)


def is_aio_simplex_host_unlocked(host):
    return (get_system_mode() == constants.SYSTEM_MODE_SIMPLEX and
            host['administrative'] != constants.ADMIN_LOCKED and
            host['invprovision'] != constants.PROVISIONING)


def is_host_state_valid_for_fs_resize(host):
    """
    This function verifies the administrative, operational, availability of
    each host.
    """

    if (host.administrative != constants.ADMIN_UNLOCKED or
            host.availability != constants.AVAILABILITY_AVAILABLE or
            host.operational != constants.OPERATIONAL_ENABLED):

        # A node can become degraded due to not free space available in a
        # FS and thus block the resize operation. If the only alarm that
        # degrades a node is a filesystem alarm, we shouldn't block the
        # resize as the resize itself will clear the degrade.
        health_helper = health.Health(pecan.request.dbapi)

        degrade_alarms = health_helper.get_alarms_degrade(
            pecan.request.context,
            alarm_ignore_list=[fm_constants.FM_ALARM_ID_FS_USAGE],
            entity_instance_id_filter=(host.personality + "-"))
        allowed_resize = False
        if (not degrade_alarms and
                host.availability == constants.AVAILABILITY_DEGRADED):
            allowed_resize = True

        if not allowed_resize:
            alarm_explanation = ""
            if degrade_alarms:
                alarm_explanation = "Check alarms with the following IDs: %s" % str(degrade_alarms)
            raise wsme.exc.ClientSideError(
                _("This operation requires controllers to be %s, %s, %s. "
                  "Current status is %s, %s, %s. %s." %
                  (constants.ADMIN_UNLOCKED, constants.OPERATIONAL_ENABLED,
                   constants.AVAILABILITY_AVAILABLE,
                   host.administrative, host.operational,
                   host.availability, alarm_explanation)))

    return True


def get_vswitch_type():
    system = pecan.request.dbapi.isystem_get_one()
    return system.capabilities.get('vswitch_type')


def is_openstack_compute(ihost):
    for obj in pecan.request.dbapi.label_get_by_host(ihost['uuid']):
        try:
            if helm_common.LABEL_COMPUTE_LABEL == obj.label_key:
                return True
        except AttributeError:
            pass
    return False


def get_https_enabled():
    system = pecan.request.dbapi.isystem_get_one()
    return system.capabilities.get('https_enabled', False)


def get_tpm_config():
    tpmconfig = None
    try:
        tpmconfig = pecan.request.dbapi.tpmconfig_get_one()
    except exception.SysinvException:
        pass
    return tpmconfig


def get_sdn_enabled():
    system = pecan.request.dbapi.isystem_get_one()
    return system.capabilities.get('sdn_enabled', False)


def get_region_config():
    system = pecan.request.dbapi.isystem_get_one()
    # TODO(mpeters): this should to be updated to return a boolean value
    # requires integration changes between horizon, cgts-client and users to
    # transition to a proper boolean value
    return system.capabilities.get('region_config', False)


def get_shared_services():
    system = pecan.request.dbapi.isystem_get_one()
    return system.capabilities.get('shared_services', None)


def lookup_static_ip_address(name, networktype):
    """"Find a statically configured address based on name and network
    type."""
    try:
        # address names are refined by network type to ensure they are
        # unique across different address pools
        name = '%s-%s' % (name, networktype)
        address = pecan.request.dbapi.address_get_by_name(name)
        return address.address
    except exception.AddressNotFoundByName:
        return None


def update_address_mode(interface, family, mode, pool):
    interface_id = interface['id']
    pool_id = pecan.request.dbapi.address_pool_get(pool)['id'] if pool else None
    try:
        # retrieve the existing value and compare
        existing = pecan.request.dbapi.address_mode_query(
            interface_id, family)
        if existing.mode == mode:
            if (mode != 'pool' or existing.pool_uuid == pool):
                return
        if existing.mode == 'pool' or (not mode or mode == 'disabled'):
            pecan.request.dbapi.routes_destroy_by_interface(
                interface_id, family)
            pecan.request.dbapi.addresses_destroy_by_interface(
                interface_id, family)
    except exception.AddressModeNotFoundByFamily:
        # continue and update DB with new record
        pass
    updates = {'family': family, 'mode': mode, 'address_pool_id': pool_id}
    pecan.request.dbapi.address_mode_update(interface_id, updates)


def config_is_reboot_required(config_uuid):
    """Check if the supplied config_uuid has the reboot required flag

    :param config_uuid UUID object or UUID string
    :return True if reboot is required, False otherwise
    """
    return int(uuid.UUID(config_uuid)) & constants.CONFIG_REBOOT_REQUIRED


def config_flip_reboot_required(config_uuid):
    """flip the reboot required flag for the supplied UUID

    :param config_uuid UUID object or UUID string
    :return The modified UUID as a string
    :rtype str
    """
    uuid_str = str(config_uuid)
    uuid_int = int(uuid.UUID(uuid_str)) ^ constants.CONFIG_REBOOT_REQUIRED
    return str(uuid.UUID(int=uuid_int))


class SystemHelper(object):
    @staticmethod
    def get_product_build():
        active_controller = HostHelper.get_active_controller()
        if constants.WORKER in active_controller.subfunctions:
            return constants.TIS_AIO_BUILD
        return constants.TIS_STD_BUILD


class HostHelper(object):
    @staticmethod
    def get_active_controller(dbapi=None):
        """Returns host object for active controller."""
        if not dbapi:
            dbapi = pecan.request.dbapi
        hosts = dbapi.ihost_get_by_personality(
            constants.CONTROLLER)
        active_controller = None
        for host in hosts:
            if is_host_active_controller(host):
                active_controller = host
                break

        return active_controller


def is_drbd_fs_resizing(fs_name=None):
    controller_fs_list = pecan.request.dbapi.controller_fs_get_list()
    for fs in controller_fs_list:
        if fs['replicated']:
            if fs['state'] == constants.CONTROLLER_FS_RESIZING_IN_PROGRESS:
                if fs_name:
                    if fs['name'] == fs_name:
                        return True
                else:
                    return True
    return False


def is_drbd_fs_syncing():
    output = subprocess.check_output(  # pylint: disable=not-callable
        "drbd-overview", stderr=subprocess.STDOUT, universal_newlines=True)
    LOG.info("is_drbd_fs_syncing returned '%s'" % output)
    if "sync\'ed" in output:
        return True
    return False


def get_system_mode(dbapi=None):
    if not dbapi:
        dbapi = pecan.request.dbapi
    system = dbapi.isystem_get_one()
    return system.system_mode


def get_distributed_cloud_role(dbapi=None):
    if not dbapi:
        dbapi = pecan.request.dbapi
    system = dbapi.isystem_get_one()
    return system.distributed_cloud_role


def get_worker_count(dbapi=None):
    if not dbapi:
        dbapi = pecan.request.dbapi
    return len(dbapi.ihost_get_by_personality(constants.WORKER))


def get_node_cgtsvg_limit(host):
    """Calculate free space for host filesystem
       returns: cgtsvg_max_free_gib
    """
    cgtsvg_free_mib = 0

    ipvs = pecan.request.dbapi.ipv_get_by_ihost(host.uuid)
    for ipv in ipvs:
        if (ipv.lvm_vg_name == constants.LVG_CGTS_VG and
                ipv.pv_state != constants.PROVISIONED):
            msg = _(
                "There are still unprovisioned physical volumes on '%s'. "
                "Cannot perform operation." % host.hostname)
            raise wsme.exc.ClientSideError(msg)

    ilvgs = pecan.request.dbapi.ilvg_get_by_ihost(host.uuid)
    for ilvg in ilvgs:
        if (ilvg.lvm_vg_name == constants.LVG_CGTS_VG and
                ilvg.lvm_vg_size and ilvg.lvm_vg_total_pe):
            # Integer division in Python 2 behaves like floating point division
            # in Python 3. Replacing / by // rectifies this behavior.
            cgtsvg_free_mib = (int(ilvg.lvm_vg_size) * int(
                ilvg.lvm_vg_free_pe)
                               // int(ilvg.lvm_vg_total_pe)) // (1024 * 1024)
            break

    cgtsvg_max_free_gib = cgtsvg_free_mib // 1024

    LOG.info("get_node_cgtsvg_limit host=%s, cgtsvg_max_free_gib=%s"
        % (host.hostname, cgtsvg_max_free_gib))
    return cgtsvg_max_free_gib


def check_disallow_during_upgrades():
    """
    Check for upgrades in progress, and raise error if found.
    """

    # There must not already be a platform upgrade in progress
    try:
        pecan.request.dbapi.software_upgrade_get_one()
    except exception.NotFound:
        pass
    else:
        raise wsme.exc.ClientSideError(_(
            "Action rejected while a platform upgrade is in progress"))

    # There must not already be a kubernetes upgrade in progress
    try:
        pecan.request.dbapi.kube_upgrade_get_one()
    except exception.NotFound:
        pass
    else:
        raise wsme.exc.ClientSideError(_(
            "Action rejected while a kubernetes upgrade is in progress"))


def check_node_ceph_mon_growth(host, ceph_mon_gib, cgtsvg_max_free_gib):
    """ Check node for ceph_mon growth, include controller/worker/storage node,
        if check failed, raise error with msg.
    """
    if host is not None:
        hostname = host.hostname
    else:
        hostname = "controller"

    ceph_mon = pecan.request.dbapi.ceph_mon_get_list()
    cgtsvg_growth_gib = None
    for mon in ceph_mon:
        if mon.hostname == hostname:
            cgtsvg_growth_gib = ceph_mon_gib - mon.ceph_mon_gib
            break
    # When new mon is creating, growth should minus ceph-mon-lv reserve gib
    if cgtsvg_growth_gib is None:
        cgtsvg_growth_gib = ceph_mon_gib - constants.SB_CEPH_MON_GIB

    LOG.info("check_node_ceph_mon_growth hostname: %s, ceph_mon_gib: %s, "
             "cgtsvg_growth_gib: %s, cgtsvg_max_free_gib: %s"
             % (hostname, ceph_mon_gib, cgtsvg_growth_gib,
                cgtsvg_max_free_gib))

    if cgtsvg_growth_gib and (cgtsvg_growth_gib > cgtsvg_max_free_gib):
        if ceph_mon_gib:
            msg = _(
                "Node: %s Total target growth size %s GiB for database "
                "(doubled for upgrades), glance, "
                "scratch, backup, extension and ceph-mon exceeds "
                "growth limit of %s GiB." %
                (hostname, cgtsvg_growth_gib, cgtsvg_max_free_gib)
            )
        else:
            msg = _(
                "Node: %s Total target growth size %s GiB for database "
                "(doubled for upgrades), glance, scratch, "
                "backup and extension exceeds growth limit of %s GiB." %
                (hostname, cgtsvg_growth_gib, cgtsvg_max_free_gib)
            )
        raise wsme.exc.ClientSideError(msg)


def check_all_ceph_mon_growth(ceph_mon_gib, host=None):
    """ Check all nodes (except controllers) filesystem with growth limitation,
        host is used only when mon is creating and check current node,
        when mon update, host is None and check all nodes of mon list.
    """
    if host is not None:
        cgtsvg_max_free_gib = get_node_cgtsvg_limit(host)
        check_node_ceph_mon_growth(host, ceph_mon_gib, cgtsvg_max_free_gib)

    ceph_mons = pecan.request.dbapi.ceph_mon_get_list()
    for mon in ceph_mons:
        ceph_mon_host = pecan.request.dbapi.ihost_get(mon['forihostid'])
        cgtsvg_max_free_gib = get_node_cgtsvg_limit(ceph_mon_host)
        check_node_ceph_mon_growth(ceph_mon_host, ceph_mon_gib,
            cgtsvg_max_free_gib)


def check_node_lock_ceph_mon(ihost, force=False, ceph_helper=None):
    if not ceph_helper:
        ceph_helper = ceph.CephApiOperator()

    num_monitors, required_monitors, active_monitors = \
        ceph_helper.get_monitors_status(pecan.request.dbapi)

    host_has_last_mon = (num_monitors == 1 and
                         ihost['hostname'] in active_monitors)

    if (num_monitors - 1 < required_monitors and
            not force or host_has_last_mon):
        raise wsme.exc.ClientSideError(_(
                "Only %d storage "
                "monitor available. At least %d unlocked and "
                "enabled hosts with monitors are required. Please"
                " ensure hosts with monitors are unlocked and "
                "enabled.") %
                (num_monitors, required_monitors))


class SBApiHelper(object):
    """ API Helper Class for manipulating Storage Backends.

        Common functionality needed by the storage_backend API and it's derived
        APIs: storage_ceph, storage_lvm, storage_file.
    """
    @staticmethod
    def validate_backend(storage_backend_dict):

        backend = storage_backend_dict.get('backend')
        if not backend:
            raise wsme.exc.ClientSideError("This operation requires a "
                                           "storage backend to be specified.")

        if backend not in constants.SB_SUPPORTED:
            raise wsme.exc.ClientSideError("Supplied storage backend (%s) is "
                                           "not supported." % backend)

        name = storage_backend_dict.get('name')
        if not name:
            # Get the list of backends of this type. If none are present, then
            # this is the system default backend for this type. Therefore use
            # the default name.
            backend_list = pecan.request.dbapi.storage_backend_get_list_by_type(
                backend_type=backend)
            if not backend_list:
                storage_backend_dict['name'] = constants.SB_DEFAULT_NAMES[
                    backend]
            else:
                raise wsme.exc.ClientSideError("This operation requires storage "
                                               "backend name to be specified.")
        return backend

    @staticmethod
    def common_checks(operation, storage_backend_dict):
        backend = SBApiHelper.validate_backend(storage_backend_dict)

        backend_type = storage_backend_dict['backend']
        backend_name = storage_backend_dict['name']

        try:
            existing_backend = pecan.request.dbapi.storage_backend_get_by_name(
                backend_name)
        except exception.StorageBackendNotFoundByName:
            existing_backend = None

        # The "shared_services" of an external backend can't have any internal
        # backend, vice versa. Note: This code needs to be revisited when
        # "non_shared_services" external backend (e.g. emc) is added into
        # storage-backend.
        if operation in [constants.SB_API_OP_CREATE, constants.SB_API_OP_MODIFY]:
            current_bk_svcs = []
            backends = pecan.request.dbapi.storage_backend_get_list()
            for bk in backends:
                if backend_type == constants.SB_TYPE_EXTERNAL:
                    if bk.as_dict()['backend'] != backend_type:
                        current_bk_svcs += SBApiHelper.getListFromServices(bk.as_dict())
                else:
                    if bk.as_dict()['backend'] == constants.SB_TYPE_EXTERNAL:
                        current_bk_svcs += SBApiHelper.getListFromServices(bk.as_dict())

            new_bk_svcs = SBApiHelper.getListFromServices(storage_backend_dict)
            for svc in new_bk_svcs:
                if svc in current_bk_svcs:
                    raise wsme.exc.ClientSideError("Service (%s) already has "
                                                   "a backend." % svc)

        # Deny any change while a backend is configuring
        backends = pecan.request.dbapi.storage_backend_get_list()
        for bk in backends:
            if bk['state'] == constants.SB_STATE_CONFIGURING:
                msg = _("%s backend is configuring, please wait for "
                        "current operation to complete before making "
                        "changes.") % bk['backend'].title()
                raise wsme.exc.ClientSideError(msg)

        if not existing_backend:
            existing_backends_by_type = set(bk['backend'] for bk in backends)

            if (backend_type in existing_backends_by_type and
                    backend_type not in [constants.SB_TYPE_CEPH, constants.SB_TYPE_CEPH_EXTERNAL]):
                msg = _("Only one %s backend is supported." % backend_type)
                raise wsme.exc.ClientSideError(msg)

            elif (backend_type != constants.SB_TYPE_CEPH_EXTERNAL and
                      backend_type not in existing_backends_by_type and
                      backend_name != constants.SB_DEFAULT_NAMES[backend_type]):
                msg = _("The primary %s backend must use the default name: %s."
                        % (backend_type,
                           constants.SB_DEFAULT_NAMES[backend_type]))
                raise wsme.exc.ClientSideError(msg)

        # Deny operations with a single, unlocked, controller.
        # TODO(oponcea): Remove this once sm supports in-service config reload
        ctrls = pecan.request.dbapi.ihost_get_by_personality(constants.CONTROLLER)
        if len(ctrls) == 1:
            if (ctrls[0].administrative == constants.ADMIN_UNLOCKED and
                    get_system_mode() == constants.SYSTEM_MODE_DUPLEX):
                msg = _("Storage backend operations require both controllers "
                        "to be enabled and available.")
                raise wsme.exc.ClientSideError(msg)
        else:
            for ctrl in ctrls:
                if ctrl.availability not in [constants.AVAILABILITY_AVAILABLE,
                                             constants.AVAILABILITY_DEGRADED]:
                    msg = _("Storage backend operations require both controllers "
                            "to be enabled and available/degraded.")
                    raise wsme.exc.ClientSideError(msg)

        if existing_backend and operation == constants.SB_API_OP_CREATE:
            if (existing_backend.state == constants.SB_STATE_CONFIGURED or
                  existing_backend.state == constants.SB_STATE_CONFIG_ERR):
                msg = (_("Initial (%s) backend was previously created. Use the "
                         "modify API for further provisioning or supply a unique "
                         "name to add an additional backend.") %
                       existing_backend.name)
                raise wsme.exc.ClientSideError(msg)
        elif not existing_backend and operation == constants.SB_API_OP_MODIFY:
            raise wsme.exc.ClientSideError("Attempting to modify non-existant (%s) "
                                           "backend." % backend)

    @staticmethod
    def set_backend_data(requested, defaults, checks, supported_svcs, current=None):
        """ Returns a valid backend dictionary based on current inputs

        :param requested: data from the API
        :param defaults: values that should be set if missing or not currently set
        :param checks: a set of valid data to be mapped into the backend capabilities
        :param supported_svcs: services that are allowed to be used with this backend
        :param current: the existing view of this data (typically from the DB)
        """
        if current:
            merged = current.copy()
        else:
            merged = requested.copy()

        # go through the requested values
        for key in requested:
            if key in merged and merged[key] != requested[key]:
                merged[key] = requested[key]

        # Set existing defaults
        for key in merged:
            if merged[key] is None and key in defaults:
                merged[key] = defaults[key]

        # Add the missing defaults
        for key in defaults:
            if key not in merged:
                merged[key] = defaults[key]

        # Pop the current set of data and make sure only supported parameters
        # are populated
        hiera_data = merged.pop('capabilities', {})
        merged['capabilities'] = {}

        merged_hiera_data = defaults.pop('capabilities', {})
        merged_hiera_data.update(hiera_data)

        for key in merged_hiera_data:
            if key in checks['backend']:
                merged['capabilities'][key] = merged_hiera_data[key]
                continue
            for svc in supported_svcs:
                if key in checks[svc]:
                    merged['capabilities'][key] = merged_hiera_data[key]

        return merged

    @staticmethod
    def check_minimal_number_of_controllers(min_number):
        chosts = pecan.request.dbapi.ihost_get_by_personality(
            constants.CONTROLLER)

        if len(chosts) < min_number:
            raise wsme.exc.ClientSideError(
                "This operation requires %s controllers provisioned." % min_number
            )

        for chost in chosts:
            if chost.invprovision != constants.PROVISIONED:
                raise wsme.exc.ClientSideError(
                    "This operation requires %s controllers provisioned." %
                    min_number
                )

    @staticmethod
    def check_swift_enabled():
        try:
            swift_enabled = pecan.request.dbapi.service_parameter_get_one(
                service=constants.SERVICE_TYPE_RADOSGW,
                section=constants.SERVICE_PARAM_SECTION_RADOSGW_CONFIG,
                name=constants.SERVICE_PARAM_NAME_RADOSGW_SERVICE_ENABLED)
            if swift_enabled and swift_enabled.value.lower() == 'true':
                raise wsme.exc.ClientSideError(
                    "Swift is already enabled through service parameter.")
        except exception.SysinvException:
            raise wsme.exc.ClientSideError(
                "Failed to check if Swift is already enabled through service "
                "parameter.")

    @staticmethod
    def getListFromServices(be_dict):
        return [] if be_dict['services'] is None else be_dict['services'].split(',')

    @staticmethod
    def setServicesFromList(be_dict, svc_list):
        be_dict['services'] = ','.join(svc_list)

    @staticmethod
    def is_svc_enabled(sb_list, svc):
        for b in sb_list:
            if b.services:
                if svc in b.services:
                    return True
        return False

    @staticmethod
    def enable_backend(sb, backend_enable_function):
        """ In-service enable storage backend """
        try:
            # Initiate manifest application
            LOG.info(_("Initializing configuration of storage %s backend.") % sb.backend.title())
            backend_enable_function(pecan.request.context)
            LOG.info("Configuration of storage %s backend initialized, "
                     "continuing in background." % sb.backend.title())
        except exception.SysinvException:
            LOG.exception("Manifests failed!")
            # Set lvm backend to error so that it can be recreated
            values = {'state': constants.SB_STATE_CONFIG_ERR, 'task': None}
            pecan.request.dbapi.storage_backend_update(sb.uuid, values)
            msg = _("%s configuration failed, check node status and retry. "
                    "If problem persists contact next level of support.") % sb.backend.title()
            raise wsme.exc.ClientSideError(msg)

    @staticmethod
    def is_primary_ceph_tier(name_string):
        """Check if a tier name string is for the primary ceph tier. """
        if name_string == constants.SB_TIER_DEFAULT_NAMES[
                    constants.SB_TYPE_CEPH]:
            return True
        return False

    @staticmethod
    def is_primary_ceph_backend(name_string):
        """Check if a backend name string is for the primary ceph backend. """
        if name_string == constants.SB_DEFAULT_NAMES[constants.SB_TYPE_CEPH]:
            return True
        return False

    @staticmethod
    def remove_service_from_backend(sb, svc_name):
        services = SBApiHelper.getListFromServices(sb)
        services.remove(svc_name)
        pecan.request.dbapi.storage_backend_update(
            sb.id, {'services': ','.join(services)})
