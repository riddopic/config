########################################################################
#
# Copyright (c) 2021 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
########################################################################

from cgtsclient.common import base
from cgtsclient.common import utils
from cgtsclient import exc
from cgtsclient.v1 import options

CREATION_ATTRIBUTES = ['name', 'ptp_instance_uuid']


class PtpInterface(base.Resource):
    def __repr__(self):
        return "<PtpInterface %s>" % self._info


class PtpInterfaceManager(base.Manager):
    resource_class = PtpInterface

    @staticmethod
    def _path(ptp_interface_id=None):
        return 'v1/ptp_interfaces/%s' % ptp_interface_id if ptp_interface_id \
            else 'v1/ptp_interfaces'

    def list(self, q=None):
        return self._list(options.build_url(self._path(), q), "ptp_interfaces")

    def list_by_host(self, host_id):
        path = 'v1/ihosts/%s/ptp_interfaces' % host_id
        return self._list(path, "ptp_interfaces")

    def list_by_interface(self, interface_id):
        path = 'v1/iinterfaces/%s/ptp_interfaces' % interface_id
        return self._list(path, "ptp_interfaces")

    def get(self, ptp_interface_id):
        try:
            return self._list(self._path(ptp_interface_id))[0]
        except IndexError:
            return None

    def create(self, **kwargs):
        body = {}
        for (key, value) in kwargs.items():
            if key in CREATION_ATTRIBUTES:
                body[key] = value
            else:
                raise exc.InvalidAttribute('Invalid attribute: %s' % key)
        return self._create(self._path(), body)

    def update(self, ptp_interface_id, patch):
        return self._update(self._path(ptp_interface_id), patch)

    def delete(self, ptp_interface_id):
        return self._delete(self._path(ptp_interface_id))


def _find_ptp_interface(cc, key):
    if key.isdigit() or utils.is_uuid_like(key):
        try:
            interface = cc.ptp_interface.get(key)
        except exc.HTTPNotFound:
            raise exc.CommandError('PTP interface not found: %s' % key)
        else:
            return interface
    else:
        ptp_interfaces = cc.ptp_interface.list()
        for interface in ptp_interfaces:
            if interface.name == key:
                return interface
        else:
            raise exc.CommandError('PTP interface not found: %s' % key)
