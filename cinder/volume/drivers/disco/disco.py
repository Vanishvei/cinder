#    copyright (c) 2016 Industrial Technology Research Institute.
#    All Rights Reserved.
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

"""DISCO Block device Driver."""

import os
import time

from oslo_config import cfg
from oslo_log import log as logging
from oslo_service import loopingcall
from oslo_utils import units
import six
from suds import client

from cinder import context
from cinder.db.sqlalchemy import api
from cinder import exception
from cinder.i18n import _
from cinder.image import image_utils
from cinder import interface
from cinder.volume import driver
from cinder.volume.drivers.disco import disco_api
from cinder.volume.drivers.disco import disco_attach_detach


LOG = logging.getLogger(__name__)

disco_opts = [
    cfg.IPOpt('disco_client',
              default='127.0.0.1',
              help='The IP of DMS client socket server'),
    cfg.PortOpt('disco_client_port',
                default='9898',
                help='The port to connect DMS client socket server'),
    cfg.StrOpt('disco_wsdl_path',
               default='/etc/cinder/DISCOService.wsdl',
               deprecated_for_removal=True,
               help='Path to the wsdl file '
                    'to communicate with DISCO request manager'),
    cfg.IPOpt('rest_ip',
              help='The IP address of the REST server'),
    cfg.StrOpt('choice_client',
               help='Use soap client or rest client for communicating '
                    'with DISCO. Possible values are "soap" or '
                    '"rest".'),
    cfg.PortOpt('disco_src_api_port',
                default='8080',
                help='The port of DISCO source API'),
    cfg.StrOpt('volume_name_prefix',
               default='openstack-',
               help='Prefix before volume name to differentiate '
                    'DISCO volume created through openstack '
                    'and the other ones'),
    cfg.IntOpt('snapshot_check_timeout',
               default=3600,
               help='How long we check whether a snapshot '
                    'is finished before we give up'),
    cfg.IntOpt('restore_check_timeout',
               default=3600,
               help='How long we check whether a restore '
                    'is finished before we give up'),
    cfg.IntOpt('clone_check_timeout',
               default=3600,
               help='How long we check whether a clone '
                    'is finished before we give up'),
    cfg.IntOpt('retry_interval',
               default=1,
               help='How long we wait before retrying to '
                    'get an item detail')
]

DISCO_CODE_MAPPING = {
    'request.success': 1,
    'request.ongoing': 2,
    'request.failure': 3,
}

CONF = cfg.CONF
CONF.register_opts(disco_opts)


# Driver to communicate with DISCO storage solution
@interface.volumedriver
class DiscoDriver(driver.VolumeDriver):
    """Execute commands related to DISCO Volumes.

    Version history:
        1.0 - disco volume driver using SOAP
        1.1 - disco volume driver using REST and only compatible
              with version greater than disco-1.6.4

    """

    VERSION = "1.1"
    CI_WIKI_NAME = "ITRI_DISCO_CI"

    def __init__(self, *args, **kwargs):
        """Init Disco driver : get configuration, create client."""
        super(DiscoDriver, self).__init__(*args, **kwargs)
        self.configuration.append_config_values(disco_opts)
        self.ctxt = context.get_admin_context()
        self.attach_detach_volume = (
            disco_attach_detach.AttachDetachDiscoVolume())

    def do_setup(self, context):
        """Create client for DISCO request manager."""
        LOG.debug("Enter in DiscoDriver do_setup.")
        if CONF.choice_client.lower() == "rest":
            self.client = disco_api.DiscoApi(
                CONF.rest_ip, CONF.disco_src_api_port)
        else:
            path = ''.join(['file:', self.configuration.disco_wsdl_path])
            init_client = client.Client(path, cache=None)
            self.client = init_client.service

    def check_for_setup_error(self):
        """Make sure we have the pre-requisites."""
        if not CONF.rest_ip and CONF.choice_client.lower() == "rest":
            msg = _("Could not find the IP address of the REST server.")
            raise exception.VolumeBackendAPIException(data=msg)
        else:
            path = self.configuration.disco_wsdl_path
            if not os.path.exists(path):
                msg = _("Could not find DISCO wsdl file.")
                raise exception.VolumeBackendAPIException(data=msg)

    def create_volume(self, volume):
        """Create a disco volume."""
        name = self.configuration.volume_name_prefix, volume["id"]
        vol_name = ''.join(name)
        vol_size = volume['size'] * units.Ki
        LOG.debug("Create volume : [name] %(vname)s - [size] %(vsize)s.",
                  {'vname': vol_name, 'vsize': six.text_type(vol_size)})
        reply = self.client.volumeCreate(vol_name, vol_size)
        status = reply['status']
        result = reply['result']
        LOG.debug("Create volume : [status] %(stat)s - [result] %(res)s.",
                  {'stat': six.text_type(status), 'res': result})

        if status:
            msg = (_("Error while creating volume "
                     "[status] %(stat)s - [result] %(res)s.") %
                   {'stat': six.text_type(status), 'res': result})
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        LOG.debug("Volume %s created.", volume["name"])
        return {'provider_location': result}

    def delete_volume(self, volume):
        """Delete a logical volume."""
        disco_vol_id = volume['provider_location']
        LOG.debug("Delete disco volume : %s.", disco_vol_id)
        reply = self.client.volumeDelete(disco_vol_id)
        status = reply['status']
        result = reply['result']

        LOG.debug("Delete volume [status] %(stat)s - [result] %(res)s.",
                  {'stat': six.text_type(status), 'res': result})

        if status:
            msg = (_("Error while deleting volume "
                     "[status] %(stat)s - [result] %(res)s.") %
                   {'stat': six.text_type(status), 'res': result})
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

        LOG.debug("Volume %s deleted.", volume['name'])

    def create_snapshot(self, snapshot):
        """Create a disco snapshot."""
        volume = api.volume_get(self.ctxt, snapshot['volume_id'])
        description = snapshot['display_description']
        vol_id = volume['provider_location']
        LOG.debug("Create snapshot of volume : %(id)s, "
                  "description : %(desc)s.",
                  {'id': vol_id, 'desc': description})

        # Trigger an asynchronous local snapshot
        reply = self.client.snapshotCreate(vol_id,
                                           -1, -1,
                                           description)
        status = reply['status']
        result = reply['result']
        LOG.debug("Create snapshot : [status] %(stat)s - [result] %(res)s.",
                  {'stat': six.text_type(status), 'res': result})

        if status:
            msg = (_("Error while creating snapshot "
                     "[status] %(stat)s - [result] %(res)s.") %
                   {'stat': six.text_type(status), 'res': result})
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

        # Monitor the status until it becomes either success or fail
        params = {'snapshot_id': int(result)}
        start_time = int(time.time())
        snapshot_request = DISCOCheck(self.client,
                                      params,
                                      start_time,
                                      "snapshot_detail")
        timeout = self.configuration.snapshot_check_timeout
        snapshot_request._monitor_request(timeout)

        snapshot['provider_location'] = result
        LOG.debug("snapshot taken successfully on volume : %(volume)s.",
                  {'volume': volume['name']})
        return {'provider_location': result}

    def delete_snapshot(self, snapshot):
        """Delete a disco snapshot."""
        LOG.debug("Enter in delete a disco snapshot.")

        snap_id = snapshot['provider_location']
        LOG.debug("[start] Delete snapshot : %s.", snap_id)
        reply = self.client.snapshotDelete(snap_id)
        status = reply['status']
        result = reply['result']
        LOG.debug("[End] Delete snapshot : "
                  "[status] %(stat)s - [result] %(res)s.",
                  {'stat': six.text_type(status), 'res': result})

        if status:
            msg = (_("Error while deleting snapshot "
                     "[status] %(stat)s - [result] %(res)s") %
                   {'stat': six.text_type(status), 'res': result})
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def create_volume_from_snapshot(self, volume, snapshot):
        """Create a volume from a snapshot."""
        name = self.configuration.volume_name_prefix, volume['id']
        snap_id = snapshot['provider_location']
        vol_name = ''.join(name)
        # Trigger an asynchronous restore operation
        LOG.debug("[start] Create volume from snapshot : "
                  "%(snap_id)s - name : %(vol_name)s.",
                  {'snap_id': snap_id, 'vol_name': vol_name})
        reply = self.client.restoreFromSnapshot(snap_id, vol_name, -1, None,
                                                -1)
        status = reply['status']
        result = reply['result']
        LOG.debug("Restore  volume from snapshot "
                  "[status] %(stat)s - [result] %(res)s.",
                  {'stat': six.text_type(status), 'res': result})

        if status:
            msg = (_("Error[%(stat)s - %(res)s] while restoring snapshot "
                     "[%(snap_id)s] into volume [%(vol)s].") %
                   {'stat': six.text_type(status), 'res': result,
                    'snap_id': snap_id, 'vol': vol_name})
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

        # Monitor the status until it becomes
        # either success, fail or timeout
        params = {'restore_id': int(result)}
        start_time = int(time.time())
        restore_request = DISCOCheck(self.client,
                                     params,
                                     start_time,
                                     "restore_detail")
        timeout = self.configuration.restore_check_timeout
        restore_request._monitor_request(timeout)
        reply = self.client.volumeDetailByName(vol_name)
        status = reply['status']
        new_vol_id = reply['volumeInfoResult']['volumeId']

        if status:
            msg = (_("Error[status] %(stat)s - [result] %(res)s] "
                     "while getting volume id.") %
                   {'stat': six.text_type(status), 'res': result})
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        LOG.debug("Restore done [status] %(stat)s - "
                  "[volume id] %(vol_id)s.",
                  {'stat': status, 'vol_id': six.text_type(new_vol_id)})
        return {'provider_location': new_vol_id}

    def create_cloned_volume(self, volume, src_vref):
        """Create a clone of the specified volume."""
        LOG.debug("Creating clone of volume: %s.", src_vref['id'])
        name = self.configuration.volume_name_prefix, volume['id']
        vol_name = ''.join(name)
        vol_size = volume['size'] * units.Ki
        src_vol_id = src_vref['provider_location']
        LOG.debug("Clone volume : "
                  "[name] %(name)s - [source] %(source)s - [size] %(size)s.",
                  {'name': vol_name,
                   'source': src_vol_id,
                   'size': six.text_type(vol_size)})
        reply = self.client.volumeClone(src_vol_id, vol_name)
        status = reply['status']
        result = reply['result']
        LOG.debug("Clone volume : [status] %(stat)s - [result] %(res)s.",
                  {'stat': six.text_type(status), 'res': result})

        if status:
            msg = (_("Error while creating volume "
                     "[status] %(stat)s - [result] %(res)s.") %
                   {'stat': six.text_type(status), 'res': result})
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

        # Monitor the status until it becomes
        # either success, fail or timeout
        params = {'clone_id': int(result),
                  'vol_name': vol_name}
        start_time = int(time.time())
        clone_request = DISCOCheck(self.client,
                                   params,
                                   start_time,
                                   "clone_detail")
        clone_request._monitor_request(self.configuration.clone_check_timeout)
        reply = self.client.volumeDetailByName(vol_name)
        status = reply['status']
        new_vol_id = reply['volumeInfoResult']['volumeId']

        if status:
            msg = (_("Error[%(stat)s - %(res)s] "
                     "while getting volume id."),
                   {'stat': six.text_type(status), 'res': result})
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

        LOG.debug("clone done : "
                  "[status] %(stat)s - [volume id] %(vol_id)s.",
                  {'stat': status, 'vol_id': six.text_type(new_vol_id)})
        return {'provider_location': new_vol_id}

    def copy_image_to_volume(self, context, volume, image_service, image_id):
        """Fetch the image from image_service and write it to the volume."""
        LOG.debug("Enter in copy image to volume for disco.")

        try:
            attach_detach_volume = (
                disco_attach_detach.AttachDetachDiscoVolume())
            device_info = attach_detach_volume._attach_volume(volume)
            image_utils.fetch_to_raw(context,
                                     image_service,
                                     image_id,
                                     device_info['path'],
                                     self.configuration.volume_dd_blocksize,
                                     size=volume['size'])
        finally:
            attach_detach_volume._detach_volume(volume)

    def copy_volume_to_image(self, context, volume, image_service, image_meta):
        """Copy a  volume to a new image."""
        LOG.debug("Enter in copy image to volume for disco.")
        try:
            attach_detach_volume = (
                disco_attach_detach.AttachDetachDiscoVolume())
            device_info = attach_detach_volume._attach_volume(volume)
            image_utils.upload_volume(context,
                                      image_service,
                                      image_meta,
                                      device_info['path'])
        finally:
            attach_detach_volume._detach_volume(volume)

    def extend_volume(self, volume, new_size):
        """Extend an existing volume's size."""
        vol_id = volume['provider_location']
        LOG.debug("Extends volume : %(id)s, new size : %(size)s.",
                  {'id': vol_id, 'size': new_size})
        new_size_mb = new_size * units.Ki
        reply = self.client.volumeExtend(vol_id, new_size_mb)
        status = reply['status']
        result = reply['result']
        if status:
            msg = (_("Error while extending volume "
                     "[status] %(stat)s - [result] %(res)s."),
                   {'stat': six.text_type(status), 'res': result})
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        LOG.debug("Volume extended : [id] %(vid)s - "
                  "[status] %(stat)s - [result] %(res)s.",
                  {'vid': vol_id,
                   'stat': six.text_type(status),
                   'res': result})

    def initialize_connection(self, volume, connector):
        """Function called before attaching a volume."""
        LOG.debug("Enter in initialize connection with disco, "
                  "connector is %s.", connector)
        cp = self.attach_detach_volume._get_connection_properties(volume)
        data = {
            'driver_volume_type': 'disco',
            'data': cp
        }
        LOG.debug("Initialize connection [data]: %s.", data)
        return data

    def terminate_connection(self, volume, connector, **kwargs):
        """Function called after attaching a volume."""
        LOG.debug("Enter in terminate connection with disco.")

    def _update_volume_stats(self):
        LOG.debug("Enter in update volume stats.")
        stats = {}
        backend_name = self.configuration.safe_get('volume_backend_name')
        stats['volume_backend_name'] = backend_name or 'disco'
        stats['storage_protocol'] = 'disco'
        stats['driver_version'] = self.VERSION
        stats['reserved_percentage'] = 0
        stats['vendor_name'] = 'ITRI'
        stats['QoS_support'] = False

        try:
            reply = self.client.systemInformationList()
            status = reply['status']

            if status:
                msg = (_("Error while getting "
                         "disco information [%s].") %
                       six.text_type(status))
                LOG.error(msg)
                raise exception.VolumeBackendAPIException(data=msg)

            info_list = reply['propertyListResult']['PropertyInfoList']
            for info in info_list:
                if info['name'] == 'freeCapacityGB':
                    stats['free_capacity_gb'] = float(info['value'])
                elif info['name'] == 'totalCapacityGB':
                    stats['total_capacity_gb'] = float(info['value'])
        except Exception:
            stats['total_capacity_gb'] = 'unknown'
            stats['free_capacity_gb'] = 'unknown'

        self._stats = stats

    def get_volume_stats(self, refresh=False):
        """Get backend information."""
        if refresh:
            self._update_volume_stats()
        return self._stats

    def local_path(self, volume):
        """Return the path to the DISCO volume."""
        return "/dev/dms%s" % volume['name']

    def ensure_export(self, context, volume):
        """Ensure an export."""
        pass

    def create_export(self, context, volume, connector):
        """Export the volume."""
        pass

    def remove_export(self, context, volume):
        """Remove an export for a logical volume."""
        pass


class DISCOCheck(object):
    """Used to monitor DISCO operations."""

    def __init__(self, client, param, start_time, function):
        """Init some variables for checking some requests done in DISCO."""
        self.start_time = start_time
        self.function = function
        self.client = client
        self.param = param

    def is_timeout(self, start_time, timeout):
        """Check whether we reach the timeout."""
        current_time = int(time.time())
        return current_time - start_time > timeout

    def _retry_get_detail(self, start_time, timeout, operation, params):
        """Keep trying to query an item detail unless we reach the timeout."""
        reply = self._call_api(operation, params)
        status = reply['status']
        msg = (_("Error while getting %(op)s details, "
                 "returned code: %(status)s.") %
               {'op': operation, 'status': six.text_type(status)})
        if status:
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

        item_status = self._get_item_status(operation, reply)
        if item_status == DISCO_CODE_MAPPING['request.failure']:
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

        elif item_status == DISCO_CODE_MAPPING['request.success']:
            raise loopingcall.LoopingCallDone(retvalue=reply)
        elif self.is_timeout(start_time, timeout):
            msg = (_("Timeout while calling %s ") % operation)
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def _call_api(self, operation, params):
        """Make the call to the SOAP api."""
        if operation == 'snapshot_detail':
            return self.client.snapshotDetail(params['snapshot_id'])
        if operation == 'restore_detail':
            return self.client.restoreDetail(params['restore_id'])
        if operation == 'clone_detail':
            return self.client.cloneDetail(params['clone_id'],
                                           params['vol_name'])
        else:
            msg = (_("Unknown operation %s."), operation)
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def _get_item_status(self, operation, reply):
        """Make the call to the SOAP api."""
        if reply is None:
            msg = (_("Call returned a None object"))
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        elif operation == 'snapshot_detail':
            return reply['snapshotInfoResult']['status']
        elif operation == 'restore_detail':
            return reply['restoreInfoResult']['status']
        elif operation == 'clone_detail':
            return int(reply['result'])
        else:
            msg = (_("Unknown operation "
                     "%s."), operation)
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)

    def _monitor_request(self, timeout):
        """Monitor the request."""
        timer = loopingcall.FixedIntervalLoopingCall(
            self._retry_get_detail,
            self.start_time,
            timeout,
            self.function,
            self.param)
        timer.start(interval=CONF.retry_interval).wait()
