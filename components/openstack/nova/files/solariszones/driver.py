# Copyright 2011 Justin Santa Barbara
# All Rights Reserved.
#
# Copyright (c) 2013, 2015, Oracle and/or its affiliates. All rights reserved.
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

"""
Driver for Solaris Zones (nee Containers):
"""

import base64
import glob
import os
import platform
import shutil
import tempfile
import uuid

import rad.bindings.com.oracle.solaris.rad.kstat_1 as kstat
import rad.bindings.com.oracle.solaris.rad.zonemgr_1 as zonemgr
import rad.client
import rad.connect
from solaris_install.archive.checkpoints import InstantiateUnifiedArchive
from solaris_install.archive import LOGFILE as ARCHIVE_LOGFILE
from solaris_install.archive import UnifiedArchive
from solaris_install.engine import InstallEngine
from solaris_install.target.size import Size

from cinderclient import exceptions as cinder_exception
from eventlet import greenthread
from lxml import etree
from oslo_config import cfg
from passlib.hash import sha256_crypt

from nova.api.metadata import password
from nova.compute import power_state
from nova.compute import task_states
from nova.compute import vm_states
from nova.console import type as ctype
from nova import conductor
from nova import context as nova_context
from nova import crypto
from nova import exception
from nova.i18n import _
from nova.image import glance
from nova.network import neutronv2
from nova import objects
from nova.objects import flavor as flavor_obj
from nova.openstack.common import fileutils
from nova.openstack.common import jsonutils
from nova.openstack.common import log as logging
from nova.openstack.common import loopingcall
from nova.openstack.common import processutils
from nova.openstack.common import strutils
from nova import utils
from nova.virt import driver
from nova.virt import event as virtevent
from nova.virt import images
from nova.virt.solariszones import sysconfig
from nova.volume.cinder import API
from nova.volume.cinder import cinderclient
from nova.volume.cinder import get_cinder_client_version
from nova.volume.cinder import translate_volume_exception
from nova.volume.cinder import _untranslate_volume_summary_view

solariszones_opts = [
    cfg.StrOpt('glancecache_dirname',
               default='$state_path/images',
               help='Default path to Glance cache for Solaris Zones.'),
    cfg.StrOpt('solariszones_snapshots_directory',
               default='$instances_path/snapshots',
               help='Location where solariszones driver will store snapshots '
                    'before uploading them to the Glance image service'),
    cfg.StrOpt('zones_suspend_path',
               default='/var/share/suspend',
               help='Default path for suspend images for Solaris Zones.'),
]

CONF = cfg.CONF
CONF.register_opts(solariszones_opts)
CONF.import_opt('vncserver_proxyclient_address', 'nova.vnc')
LOG = logging.getLogger(__name__)

# These should match the strings returned by the zone_state_str()
# function in the (private) libzonecfg library. These values are in turn
# returned in the 'state' string of the Solaris Zones' RAD interface by
# the zonemgr(3RAD) provider.
ZONE_STATE_CONFIGURED = 'configured'
ZONE_STATE_INCOMPLETE = 'incomplete'
ZONE_STATE_UNAVAILABLE = 'unavailable'
ZONE_STATE_INSTALLED = 'installed'
ZONE_STATE_READY = 'ready'
ZONE_STATE_RUNNING = 'running'
ZONE_STATE_SHUTTING_DOWN = 'shutting_down'
ZONE_STATE_DOWN = 'down'
ZONE_STATE_MOUNTED = 'mounted'

# Mapping between zone state and Nova power_state.
SOLARISZONES_POWER_STATE = {
    ZONE_STATE_CONFIGURED:      power_state.NOSTATE,
    ZONE_STATE_INCOMPLETE:      power_state.NOSTATE,
    ZONE_STATE_UNAVAILABLE:     power_state.NOSTATE,
    ZONE_STATE_INSTALLED:       power_state.SHUTDOWN,
    ZONE_STATE_READY:           power_state.RUNNING,
    ZONE_STATE_RUNNING:         power_state.RUNNING,
    ZONE_STATE_SHUTTING_DOWN:   power_state.RUNNING,
    ZONE_STATE_DOWN:            power_state.RUNNING,
    ZONE_STATE_MOUNTED:         power_state.NOSTATE
}

# Solaris Zones brands as defined in brands(5).
ZONE_BRAND_LABELED = 'labeled'
ZONE_BRAND_SOLARIS = 'solaris'
ZONE_BRAND_SOLARIS_KZ = 'solaris-kz'
ZONE_BRAND_SOLARIS10 = 'solaris10'

# Mapping between supported zone brands and the name of the corresponding
# brand template.
ZONE_BRAND_TEMPLATE = {
    ZONE_BRAND_SOLARIS:         'SYSdefault',
    ZONE_BRAND_SOLARIS_KZ:      'SYSsolaris-kz',
}

MAX_CONSOLE_BYTES = 102400
VNC_CONSOLE_BASE_FMRI = 'svc:/application/openstack/nova/zone-vnc-console'
# Required in order to create a zone VNC console SMF service instance
VNC_SERVER_PATH = '/usr/bin/vncserver'
XTERM_PATH = '/usr/bin/xterm'

ROOTZPOOL_RESOURCE = 'rootzpool'


def lookup_resource(zone, resource):
    """Lookup specified resource from specified Solaris Zone."""
    try:
        val = zone.getResources(zonemgr.Resource(resource))
    except rad.client.ObjectError:
        return None
    except Exception:
        raise
    return val[0] if val else None


def lookup_resource_property(zone, resource, prop, filter=None):
    """Lookup specified property from specified Solaris Zone resource."""
    try:
        val = zone.getResourceProperties(zonemgr.Resource(resource, filter),
                                         [prop])
    except rad.client.ObjectError:
        return None
    except Exception:
        raise
    return val[0].value if val else None


def lookup_resource_property_value(zone, resource, prop, value):
    """Lookup specified property with value from specified Solaris Zone
    resource. Returns property if matching value is found, else None
    """
    try:
        resources = zone.getResources(zonemgr.Resource(resource))
        for resource in resources:
            for propertee in resource.properties:
                if propertee.name == prop and propertee.value == value:
                    return propertee
        else:
            return None
    except rad.client.ObjectError:
        return None
    except Exception:
        raise


class SolarisVolumeAPI(API):
    """ Extending the volume api to support additional cinder sub-commands
    """
    @translate_volume_exception
    def create(self, context, size, name, description, snapshot=None,
               image_id=None, volume_type=None, metadata=None,
               availability_zone=None, source_volume=None):
        """Clone the source volume by calling the cinderclient version of
        create with a source_volid argument

        :param context: the context for the clone
        :param size: size of the new volume, must be the same as the source
            volume
        :param name: display_name of the new volume
        :param description: display_description of the new volume
        :param snapshot: Snapshot object
        :param image_id: image_id to create the volume from
        :param volume_type: type of volume
        :param metadata: Additional metadata for the volume
        :param availability_zone: zone:host where the volume is to be created
        :param source_volume: Volume object

        Returns a volume object
        """
        if snapshot is not None:
            snapshot_id = snapshot['id']
        else:
            snapshot_id = None

        if source_volume is not None:
            source_volid = source_volume['id']
        else:
            source_volid = None

        kwargs = dict(snapshot_id=snapshot_id,
                      volume_type=volume_type,
                      user_id=context.user_id,
                      project_id=context.project_id,
                      availability_zone=availability_zone,
                      metadata=metadata,
                      imageRef=image_id,
                      source_volid=source_volid)

        version = get_cinder_client_version(context)
        if version == '1':
            kwargs['display_name'] = name
            kwargs['display_description'] = description
        elif version == '2':
            kwargs['name'] = name
            kwargs['description'] = description

        try:
            item = cinderclient(context).volumes.create(size, **kwargs)
            return _untranslate_volume_summary_view(context, item)
        except cinder_exception.OverLimit:
            raise exception.OverQuota(overs='volumes')
        except cinder_exception.BadRequest as err:
            raise exception.InvalidInput(reason=unicode(err))

    @translate_volume_exception
    def update(self, context, volume_id, fields):
        """Update the fields of a volume for example used to rename a volume
        via a call to cinderclient

        :param context: the context for the update
        :param volume_id: the id of the volume to update
        :param fields: a dictionary of of the name/value pairs to update
        """
        cinderclient(context).volumes.update(volume_id, **fields)

    @translate_volume_exception
    def extend(self, context, volume, newsize):
        """Extend the size of a cinder volume by calling the cinderclient

        :param context: the context for the extend
        :param volume: the volume object to extend
        :param newsize: the new size of the volume in GB
        """
        cinderclient(context).volumes.extend(volume, newsize)


class ZoneConfig(object):
    """ZoneConfig - context manager for access zone configurations.
    Automatically opens the configuration for a zone and commits any changes
    before exiting
    """
    def __init__(self, zone):
        """zone is a zonemgr object representing either a kernel zone or
        non-global zone.
        """
        self.zone = zone
        self.editing = False

    def __enter__(self):
        """enables the editing of the zone."""
        try:
            self.zone.editConfig()
            self.editing = True
            return self
        except rad.client.ObjectError as err:
            LOG.error(_("Unable to initialize editing of instance '%s' via "
                        "zonemgr(3RAD): %s") % (self.zone.name, err))
            raise

    def __exit__(self, exc_type, exc_val, exc_tb):
        """looks for any kind of exception before exiting.  If one is found,
        cancel any configuration changes and reraise the exception.  If not,
        commit the new configuration.
        """
        if exc_type is not None and self.editing:
            # We received some kind of exception.  Cancel the config and raise.
            self.zone.cancelConfig()
            raise
        else:
            # commit the config
            try:
                self.zone.commitConfig()
            except rad.client.ObjectError as err:
                LOG.error(_("Unable to commit the new configuration for "
                            "instance '%s' via zonemgr(3RAD): %s")
                          % (self.zone.name, err))

                # Last ditch effort to cleanup.
                self.zone.cancelConfig()
                raise

    def setprop(self, resource, prop, value):
        """sets a property for an existing resource OR creates a new resource
        with the given property(s).
        """
        current = lookup_resource_property(self.zone, resource, prop)
        if current is not None and current == value:
            # the value is already set
            return

        try:
            if current is None:
                self.zone.addResource(zonemgr.Resource(
                    resource, [zonemgr.Property(prop, value)]))
            else:
                self.zone.setResourceProperties(
                    zonemgr.Resource(resource),
                    [zonemgr.Property(prop, value)])
        except rad.client.ObjectError as err:
            LOG.error(_("Unable to set '%s' property on '%s' resource for "
                        "instance '%s' via zonemgr(3RAD): %s")
                      % (prop, resource, self.zone.name, err))
            raise

    def addresource(self, resource, props=None, ignore_exists=False):
        """creates a new resource with an optional property list, or set the
        property if the resource exists and ignore_exists is true.

        :param ignore_exists: If the resource exists, set the property for the
            resource.
        """
        if props is None:
            props = []

        try:
            self.zone.addResource(zonemgr.Resource(resource, props))
        except rad.client.ObjectError as err:
            result = err.get_payload()
            if not ignore_exists:
                LOG.error(_("Unable to create new resource '%s' for instance "
                            "'%s' via zonemgr(3RAD): %s")
                          % (resource, self.zone.name, err))
                raise

            if result.code == zonemgr.ErrorCode.RESOURCE_ALREADY_EXISTS:
                self.zone.setResourceProperties(zonemgr.Resource(
                    resource, None), props)
            else:
                raise

    def removeresources(self, resource, props=None):
        """removes resources whose properties include the optional property
        list specified in props.
        """
        if props is None:
            props = []

        try:
            self.zone.removeResources(zonemgr.Resource(resource, props))
        except rad.client.ObjectError as err:
            LOG.error(_("Unable to remove resource '%s' for instance '%s' via "
                        "zonemgr(3RAD): %s") % (resource, self.zone.name, err))
            raise


class SolarisZonesDriver(driver.ComputeDriver):
    """Solaris Zones Driver using the zonemgr(3RAD) and kstat(3RAD) providers.

    The interface to this class talks in terms of 'instances' (Amazon EC2 and
    internal Nova terminology), by which we mean 'running virtual machine'
    (XenAPI terminology) or domain (Xen or libvirt terminology).

    An instance has an ID, which is the identifier chosen by Nova to represent
    the instance further up the stack.  This is unfortunately also called a
    'name' elsewhere.  As far as this layer is concerned, 'instance ID' and
    'instance name' are synonyms.

    Note that the instance ID or name is not human-readable or
    customer-controlled -- it's an internal ID chosen by Nova.  At the
    nova.virt layer, instances do not have human-readable names at all -- such
    things are only known higher up the stack.

    Most virtualization platforms will also have their own identity schemes,
    to uniquely identify a VM or domain.  These IDs must stay internal to the
    platform-specific layer, and never escape the connection interface.  The
    platform-specific layer is responsible for keeping track of which instance
    ID maps to which platform-specific ID, and vice versa.

    Some methods here take an instance of nova.compute.service.Instance.  This
    is the data structure used by nova.compute to store details regarding an
    instance, and pass them into this layer.  This layer is responsible for
    translating that generic data structure into terms that are specific to the
    virtualization platform.

    """

    capabilities = {
        "has_imagecache": False,
        "supports_recreate": False,
        }

    def __init__(self, virtapi):
        self.virtapi = virtapi
        self._compute_event_callback = None
        self._conductor_api = conductor.API()
        self._fc_hbas = None
        self._fc_wwnns = None
        self._fc_wwpns = None
        self._host_stats = {}
        self._initiator = None
        self._install_engine = None
        self._pagesize = os.sysconf('SC_PAGESIZE')
        self._rad_connection = None
        self._uname = os.uname()
        self._validated_archives = list()
        self._volume_api = SolarisVolumeAPI()
        self._rootzpool_suffix = ROOTZPOOL_RESOURCE

    @property
    def rad_connection(self):
        if self._rad_connection is None:
            self._rad_connection = rad.connect.connect_unix()
        else:
            # taken from rad.connect.RadConnection.__repr__ to look for a
            # closed connection
            if self._rad_connection._closed is not None:
                # the RAD connection has been lost.  Reconnect to RAD
                self._rad_connection = rad.connect.connect_unix()

        return self._rad_connection

    def _init_rad(self):
        """Connect to RAD providers for kernel statistics and Solaris
        Zones. By connecting to the local rad(1M) service through a
        UNIX domain socket, kernel statistics can be read via
        kstat(3RAD) and Solaris Zones can be configured and controlled
        via zonemgr(3RAD).
        """

        try:
            self._kstat_control = self.rad_connection.get_object(
                kstat.Control())
        except Exception as reason:
            msg = (_('Unable to connect to svc:/system/rad:local: %s')
                   % reason)
            raise exception.NovaException(msg)

    def init_host(self, host):
        """Initialize anything that is necessary for the driver to function,
        including catching up with currently running VM's on the given host.
        """
        # TODO(Vek): Need to pass context in for access to auth_token
        self._init_rad()

    def cleanup_host(self, host):
        """Clean up anything that is necessary for the driver gracefully stop,
        including ending remote sessions. This is optional.
        """
        pass

    def _get_fc_hbas(self):
        """Get Fibre Channel HBA information."""
        if self._fc_hbas:
            return self._fc_hbas

        out = None
        try:
            out, err = utils.execute('/usr/sbin/fcinfo', 'hba-port')
        except processutils.ProcessExecutionError as err:
            return []

        if out is None:
            raise RuntimeError(_("Cannot find any Fibre Channel HBAs"))

        hbas = []
        hba = {}
        for line in out.splitlines():
            line = line.strip()
            # Collect the following hba-port data:
            # 1: Port WWN
            # 2: State (online|offline)
            # 3: Node WWN
            if line.startswith("HBA Port WWN:"):
                # New HBA port entry
                hba = {}
                wwpn = line.split()[-1]
                hba['port_name'] = wwpn
                continue
            elif line.startswith("Port Mode:"):
                mode = line.split()[-1]
                # Skip Target mode ports
                if mode != 'Initiator':
                    break
            elif line.startswith("State:"):
                state = line.split()[-1]
                hba['port_state'] = state
                continue
            elif line.startswith("Node WWN:"):
                wwnn = line.split()[-1]
                hba['node_name'] = wwnn
                continue
            if len(hba) == 3:
                hbas.append(hba)
                hba = {}
        self._fc_hbas = hbas
        return self._fc_hbas

    def _get_fc_wwnns(self):
        """Get Fibre Channel WWNNs from the system, if any."""
        hbas = self._get_fc_hbas()

        wwnns = []
        for hba in hbas:
            if hba['port_state'] == 'online':
                wwnn = hba['node_name']
                wwnns.append(wwnn)
        return wwnns

    def _get_fc_wwpns(self):
        """Get Fibre Channel WWPNs from the system, if any."""
        hbas = self._get_fc_hbas()

        wwpns = []
        for hba in hbas:
            if hba['port_state'] == 'online':
                wwpn = hba['port_name']
                wwpns.append(wwpn)
        return wwpns

    def _get_iscsi_initiator(self):
        """ Return the iSCSI initiator node name IQN for this host """
        out, err = utils.execute('/usr/sbin/iscsiadm', 'list',
                                 'initiator-node')
        # Sample first line of command output:
        # Initiator node name: iqn.1986-03.com.sun:01:e00000000000.4f757217
        initiator_name_line = out.splitlines()[0]
        initiator_iqn = initiator_name_line.rsplit(' ', 1)[1]
        return initiator_iqn

    def _get_zone_by_name(self, name):
        """Return a Solaris Zones object via RAD by name."""
        try:
            zone = self.rad_connection.get_object(
                zonemgr.Zone(), rad.client.ADRGlobPattern({'name': name}))
        except rad.client.NotFoundError:
            return None
        except Exception:
            raise
        return zone

    def _get_state(self, zone):
        """Return the running state, one of the power_state codes."""
        return SOLARISZONES_POWER_STATE[zone.state]

    def _pages_to_kb(self, pages):
        """Convert a number of pages of memory into a total size in KBytes."""
        return (pages * self._pagesize) / 1024

    def _get_max_mem(self, zone):
        """Return the maximum memory in KBytes allowed."""
        if zone.brand == ZONE_BRAND_SOLARIS:
            mem_resource = 'swap'
        else:
            mem_resource = 'physical'

        max_mem = lookup_resource_property(zone, 'capped-memory', mem_resource)
        if max_mem is not None:
            return strutils.string_to_bytes("%sB" % max_mem) / 1024

        # If physical property in capped-memory doesn't exist, this may
        # represent a non-global zone so just return the system's total
        # memory.
        return self._pages_to_kb(os.sysconf('SC_PHYS_PAGES'))

    def _get_mem(self, zone):
        """Return the memory in KBytes used by the domain."""

        # There isn't any way of determining this from the hypervisor
        # perspective in Solaris, so just return the _get_max_mem() value
        # for now.
        return self._get_max_mem(zone)

    def _get_num_cpu(self, zone):
        """Return the number of virtual CPUs for the domain.

        In the case of kernel zones, the number of virtual CPUs a zone
        ends up with depends on whether or not there were 'virtual-cpu'
        or 'dedicated-cpu' resources in the configuration or whether
        there was an assigned pool in the configuration. This algorithm
        attempts to emulate what the virtual platform code does to
        determine a number of virtual CPUs to use.
        """

        # If a 'virtual-cpu' resource exists, use the minimum number of
        # CPUs defined there.
        ncpus = lookup_resource_property(zone, 'virtual-cpu', 'ncpus')
        if ncpus is not None:
            min = ncpus.split('-', 1)[0]
            if min.isdigit():
                return int(min)

        # Otherwise if a 'dedicated-cpu' resource exists, use the maximum
        # number of CPUs defined there.
        ncpus = lookup_resource_property(zone, 'dedicated-cpu', 'ncpus')
        if ncpus is not None:
            max = ncpus.split('-', 1)[-1]
            if max.isdigit():
                return int(max)

        # Finally if neither resource exists but the zone was assigned a
        # pool in the configuration, the number of CPUs would be the size
        # of the processor set. Currently there's no way of easily
        # determining this so use the system's notion of the total number
        # of online CPUs.
        return os.sysconf('SC_NPROCESSORS_ONLN')

    def _get_kstat_by_name(self, kstat_class, module, instance, name):
        """Return Kstat snapshot data via RAD as a dictionary."""
        pattern = {
            'class':    kstat_class,
            'module':   module,
            'instance': instance,
            'name':     name
        }
        try:
            self._kstat_control.update()
            kstat_object = self.rad_connection.get_object(
                kstat.Kstat(), rad.client.ADRGlobPattern(pattern))
        except Exception as reason:
            LOG.info(_("Unable to retrieve kstat object '%s:%s:%s' of class "
                       "'%s' via kstat(3RAD): %s")
                     % (module, instance, name, kstat_class, reason))
            return None

        kstat_data = {}
        for named in kstat_object.fresh_snapshot().data.NAMED:
            kstat_data[named.name] = getattr(named.value,
                                             str(named.value.discriminant))
        return kstat_data

    def _get_cpu_time(self, zone):
        """Return the CPU time used in nanoseconds."""
        if zone.id == -1:
            return 0

        kstat_data = self._get_kstat_by_name('zones', 'cpu', str(zone.id),
                                             'sys_zone_aggr')
        if kstat_data is None:
            return 0

        return kstat_data['cpu_nsec_kernel'] + kstat_data['cpu_nsec_user']

    def get_info(self, instance):
        """Get the current status of an instance, by name (not ID!)

        :param instance: nova.objects.instance.Instance object

        Returns a dict containing:

        :state:           the running state, one of the power_state codes
        :max_mem:         (int) the maximum memory in KBytes allowed
        :mem:             (int) the memory in KBytes used by the domain
        :num_cpu:         (int) the number of virtual CPUs for the domain
        :cpu_time:        (int) the CPU time used in nanoseconds
        """
        # TODO(Vek): Need to pass context in for access to auth_token
        name = instance['name']
        zone = self._get_zone_by_name(name)
        if zone is None:
            LOG.error(_("Unable to find instance '%s' via zonemgr(3RAD)")
                      % name)
            raise exception.InstanceNotFound(instance_id=name)
        return {
            'state':    self._get_state(zone),
            'max_mem':  self._get_max_mem(zone),
            'mem':      self._get_mem(zone),
            'num_cpu':  self._get_num_cpu(zone),
            'cpu_time': self._get_cpu_time(zone)
        }

    def get_num_instances(self):
        """Return the total number of virtual machines.

        Return the number of virtual machines that the hypervisor knows
        about.

        .. note::

            This implementation works for all drivers, but it is
            not particularly efficient. Maintainers of the virt drivers are
            encouraged to override this method with something more
            efficient.
        """
        return len(self.list_instances())

    def instance_exists(self, instance):
        """Checks existence of an instance on the host.

        :param instance: The instance to lookup

        Returns True if an instance with the supplied ID exists on
        the host, False otherwise.

        .. note::

            This implementation works for all drivers, but it is
            not particularly efficient. Maintainers of the virt drivers are
            encouraged to override this method with something more
            efficient.
        """
        try:
            return instance.uuid in self.list_instance_uuids()
        except NotImplementedError:
            return instance.name in self.list_instances()

    def estimate_instance_overhead(self, instance_info):
        """Estimate the virtualization overhead required to build an instance
        of the given flavor.

        Defaults to zero, drivers should override if per-instance overhead
        calculations are desired.

        :param instance_info: Instance/flavor to calculate overhead for.
        :returns: Dict of estimated overhead values.
        """
        return {'memory_mb': 0}

    def _get_list_zone_object(self):
        """Return a list of all Solaris Zones objects via RAD."""
        return self.rad_connection.list_objects(zonemgr.Zone())

    def list_instances(self):
        """Return the names of all the instances known to the virtualization
        layer, as a list.
        """
        # TODO(Vek): Need to pass context in for access to auth_token
        instances_list = []
        for zone in self._get_list_zone_object():
            instances_list.append(self.rad_connection.get_object(zone).name)
        return instances_list

    def list_instance_uuids(self):
        """Return the UUIDS of all the instances known to the virtualization
        layer, as a list.
        """
        raise NotImplementedError()

    def rebuild(self, context, instance, image_meta, injected_files,
                admin_password, bdms, detach_block_devices,
                attach_block_devices, network_info=None,
                recreate=False, block_device_info=None,
                preserve_ephemeral=False):
        """Destroy and re-make this instance.

        A 'rebuild' effectively purges all existing data from the system and
        remakes the VM with given 'metadata' and 'personalities'.

        This base class method shuts down the VM, detaches all block devices,
        then spins up the new VM afterwards. It may be overridden by
        hypervisors that need to - e.g. for optimisations, or when the 'VM'
        is actually proxied and needs to be held across the shutdown + spin
        up steps.

        :param context: security context
        :param instance: nova.objects.instance.Instance
                         This function should use the data there to guide
                         the creation of the new instance.
        :param image_meta: image object returned by nova.image.glance that
                           defines the image from which to boot this instance
        :param injected_files: User files to inject into instance.
        :param admin_password: Administrator password to set in instance.
        :param bdms: block-device-mappings to use for rebuild
        :param detach_block_devices: function to detach block devices. See
            nova.compute.manager.ComputeManager:_rebuild_default_impl for
            usage.
        :param attach_block_devices: function to attach block devices. See
            nova.compute.manager.ComputeManager:_rebuild_default_impl for
            usage.
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param recreate: True if the instance is being recreated on a new
            hypervisor - all the cleanup of old state is skipped.
        :param block_device_info: Information about block devices to be
                                  attached to the instance.
        :param preserve_ephemeral: True if the default ephemeral storage
                                   partition must be preserved on rebuild
        """
        raise NotImplementedError()

    def _get_extra_specs(self, instance):
        """Retrieve extra_specs of an instance."""
        flavor = flavor_obj.Flavor.get_by_id(
            nova_context.get_admin_context(read_deleted='yes'),
            instance['instance_type_id'])
        return flavor['extra_specs'].copy()

    def _fetch_image(self, context, instance):
        """Fetch an image using Glance given the instance's image_ref."""
        glancecache_dirname = CONF.glancecache_dirname
        fileutils.ensure_tree(glancecache_dirname)
        image = ''.join([glancecache_dirname, '/', instance['image_ref']])
        if os.path.exists(image):
            LOG.debug(_("Using existing, cached Glance image: id %s")
                      % instance['image_ref'])
            return image

        LOG.debug(_("Fetching new Glance image: id %s")
                  % instance['image_ref'])
        try:
            images.fetch(context, instance['image_ref'], image,
                         instance['user_id'], instance['project_id'])
        except Exception as reason:
            LOG.error(_("Unable to fetch Glance image: id %s: %s")
                      % (instance['image_ref'], reason))
            raise
        return image

    def _validate_image(self, image, instance):
        """Validate a glance image for compatibility with the instance"""
        # Skip if the image was already checked and confirmed as valid
        if instance['image_ref'] in self._validated_archives:
            return

        if self._install_engine is None:
            self._install_engine = InstallEngine(ARCHIVE_LOGFILE)

        try:
            init_ua_cp = InstantiateUnifiedArchive(instance['image_ref'],
                                                   image)
            init_ua_cp.execute()
        except Exception:
            reason = (_("Image query failed. Possibly invalid or corrupt. "
                        "Log file location: %s:%s")
                      % (self._uname[1], ARCHIVE_LOGFILE))
            LOG.error(reason)
            raise exception.ImageUnacceptable(image_id=instance['image_ref'],
                                              reason=reason)

        try:
            ua = self._install_engine.doc.volatile.get_first_child(
                class_type=UnifiedArchive)
            # Validate the image at this point to ensure:
            # - contains one deployable system
            deployables = ua.archive_objects
            if len(deployables) != 1:
                reason = (_('Image must contain only 1 deployable system'))
                raise exception.ImageUnacceptable(
                    image_id=instance['image_ref'],
                    reason=reason)
            # - matching architecture
            deployable_arch = deployables[0].system.arch
            compute_arch = platform.processor()
            if deployable_arch != compute_arch:
                reason = (_('Image architecture "%s" is incompatible with this'
                          'compute host architecture: "%s"')
                          % (deployable_arch, compute_arch))
                raise exception.ImageUnacceptable(
                    image_id=instance['image_ref'],
                    reason=reason)
            # - single root pool only
            streams = deployables[0].zfs_streams
            stream_pools = set(stream.zpool for stream in streams)
            if len(stream_pools) > 1:
                reason = (_('Image contains more than one zpool: "%s"')
                          % (stream_pools))
                raise exception.ImageUnacceptable(
                    image_id=instance['image_ref'],
                    reason=reason)
            # - looks like it's OK
            self._validated_archives.append(instance['image_ref'])
        finally:
            # Clear the reference to the UnifiedArchive object in the engine
            # data cache to avoid collision with the next checkpoint execution.
            self._install_engine.doc.volatile.delete_children(
                class_type=UnifiedArchive)

    def _suri_from_volume_info(self, connection_info):
        """Returns a suri(5) formatted string based on connection_info
        Currently supports local ZFS volume and iSCSI driver types.
        """
        driver_type = connection_info['driver_volume_type']
        if driver_type not in ['iscsi', 'fibre_channel', 'local']:
            raise exception.VolumeDriverNotFound(driver_type=driver_type)
        if driver_type == 'local':
            suri = 'dev:/dev/zvol/dsk/%s' % connection_info['volume_path']
        elif driver_type == 'iscsi':
            data = connection_info['data']
            # suri(5) format:
            #       iscsi://<host>[:<port>]/target.<IQN>,lun.<LUN>
            # Sample iSCSI connection data values:
            # target_portal: 192.168.1.244:3260
            # target_iqn: iqn.2010-10.org.openstack:volume-a89c.....
            # target_lun: 1
            suri = 'iscsi://%s/target.%s,lun.%d' % (data['target_portal'],
                                                    data['target_iqn'],
                                                    data['target_lun'])
            # TODO(npower): need to handle CHAP authentication also
        elif driver_type == 'fibre_channel':
            data = connection_info['data']
            target_wwn = data['target_wwn']
            # Check for multiple target_wwn values in a list
            if isinstance(target_wwn, list):
                target_wwn = target_wwn[0]
            # Ensure there's a fibre channel HBA.
            hbas = self._get_fc_hbas()
            if not hbas:
                LOG.error(_("Cannot attach Fibre Channel volume '%s' because "
                          "no Fibre Channel HBA initiators were found")
                          % (target_wwn))
                raise exception.InvalidVolume(
                    reason="No host Fibre Channel initiator found")

            target_lun = data['target_lun']
            # If the volume was exported just a few seconds previously then
            # it will probably not be visible to the local adapter yet.
            # Invoke 'fcinfo remote-port' on all local HBA ports to trigger
            # a refresh.
            for wwpn in self._get_fc_wwpns():
                utils.execute('/usr/sbin/fcinfo', 'remote-port',
                              '-p', wwpn)

            # Use suriadm(1M) to generate a Fibre Channel storage URI.
            try:
                out, err = utils.execute('/usr/sbin/suriadm', 'lookup-uri',
                                         '-p', 'target=naa.%s' % target_wwn,
                                         '-p', 'lun=%s' % target_lun)
            except processutils.ProcessExecutionError as err:
                LOG.error(_("Lookup failure of Fibre Channel volume '%s', lun "
                          "%s: %s") % (target_wwn, target_lun, err.stderr))
                raise

            lines = out.split('\n')
            # Use the long form SURI on the second output line.
            suri = lines[1].strip()
        return suri

    def _set_global_properties(self, name, extra_specs, brand):
        """Set Solaris Zone's global properties if supplied via flavor."""
        zone = self._get_zone_by_name(name)
        if zone is None:
            raise exception.InstanceNotFound(instance_id=name)

        # TODO(dcomay): Should figure this out via the brands themselves.
        zonecfg_items = [
            'bootargs',
            'brand',
            'hostid'
        ]
        if brand == ZONE_BRAND_SOLARIS:
            zonecfg_items.extend(
                ['file-mac-profile', 'fs-allowed', 'limitpriv'])

        with ZoneConfig(zone) as zc:
            for key, value in extra_specs.iteritems():
                # Ignore not-zonecfg-scoped brand properties.
                if not key.startswith('zonecfg:'):
                    continue
                _scope, prop = key.split(':', 1)
                # Ignore the 'brand' property if present.
                if prop == 'brand':
                    continue
                # Ignore but warn about unsupported zonecfg-scoped properties.
                if prop not in zonecfg_items:
                    LOG.warning(_("Ignoring unsupported zone property '%s' "
                                  "set on flavor for instance '%s'")
                                % (prop, name))
                    continue
                zc.setprop('global', prop, value)

    def _create_boot_volume(self, context, instance):
        """Create a (Cinder) volume service backed boot volume"""
        try:
            vol = self._volume_api.create(
                context,
                instance['root_gb'],
                instance['hostname'] + "-" + self._rootzpool_suffix,
                "Boot volume for instance '%s' (%s)"
                % (instance['name'], instance['uuid']))
            # TODO(npower): Polling is what nova/compute/manager also does when
            # creating a new volume, so we do likewise here.
            while True:
                volume = self._volume_api.get(context, vol['id'])
                if volume['status'] != 'creating':
                    return volume
                greenthread.sleep(1)

        except Exception as reason:
            LOG.error(_("Unable to create root zpool volume for instance '%s'"
                        ": %s") % (instance['name'], reason))
            raise

    def _connect_boot_volume(self, volume, mountpoint, context, instance):
        """Connect a (Cinder) volume service backed boot volume"""
        instance_uuid = instance['uuid']
        volume_id = volume['id']

        connector = self.get_volume_connector(instance)
        connection_info = self._volume_api.initialize_connection(
            context, volume_id, connector)
        connection_info['serial'] = volume_id

        # Check connection_info to determine if the provided volume is
        # local to this compute node. If it is, then don't use it for
        # Solaris branded zones in order to avoid a known ZFS deadlock issue
        # when using a zpool within another zpool on the same system.
        extra_specs = self._get_extra_specs(instance)
        brand = extra_specs.get('zonecfg:brand', ZONE_BRAND_SOLARIS)
        if brand == ZONE_BRAND_SOLARIS:
            driver_type = connection_info['driver_volume_type']
            if driver_type == 'local':
                msg = _("Detected 'local' zvol driver volume type "
                        "from volume service, which should not be "
                        "used as a boot device for 'solaris' "
                        "branded zones.")
                raise exception.InvalidVolume(reason=msg)
            elif driver_type == 'iscsi':
                # Check for a potential loopback iSCSI situation
                data = connection_info['data']
                target_portal = data['target_portal']
                # Strip off the port number (eg. 127.0.0.1:3260)
                host = target_portal.rsplit(':', 1)
                # Strip any enclosing '[' and ']' brackets for
                # IPv6 addresses.
                target_host = host[0].strip('[]')

                # Check if target_host is an IP or hostname matching the
                # connector host or IP, which would mean the provisioned
                # iSCSI LUN is on the same host as the instance.
                if target_host in [connector['ip'], connector['host']]:
                    msg = _("iSCSI connection info from volume "
                            "service indicates that the target is a "
                            "local volume, which should not be used "
                            "as a boot device for 'solaris' branded "
                            "zones.")
                    raise exception.InvalidVolume(reason=msg)
            # Assuming that fibre_channel is non-local
            elif driver_type != 'fibre_channel':
                # Some other connection type that we don't understand
                # Let zone use some local fallback instead.
                msg = _("Unsupported volume driver type '%s' can not be used "
                        "as a boot device for zones." % driver_type)
                raise exception.InvalidVolume(reason=msg)

        # Volume looks OK to use. Notify Cinder of the attachment.
        self._volume_api.attach(context, volume_id, instance_uuid,
                                mountpoint)
        return connection_info

    def _set_boot_device(self, name, connection_info, brand):
        """Set the boot device specified by connection_info"""
        zone = self._get_zone_by_name(name)
        if zone is None:
            raise exception.InstanceNotFound(instance_id=name)

        suri = self._suri_from_volume_info(connection_info)

        with ZoneConfig(zone) as zc:
            # ZOSS device configuration is different for the solaris-kz brand
            if brand == ZONE_BRAND_SOLARIS_KZ:
                zc.zone.setResourceProperties(
                    zonemgr.Resource(
                        "device",
                        [zonemgr.Property("bootpri", "0")]),
                    [zonemgr.Property("storage", suri)])
            else:
                zc.addresource(
                    ROOTZPOOL_RESOURCE,
                    [zonemgr.Property("storage", listvalue=[suri])],
                    ignore_exists=True)

    def _set_num_cpu(self, name, vcpus, brand):
        """Set number of VCPUs in a Solaris Zone configuration."""
        zone = self._get_zone_by_name(name)
        if zone is None:
            raise exception.InstanceNotFound(instance_id=name)

        # The Solaris Zone brand type is used to specify the type of
        # 'cpu' resource set in the Solaris Zone configuration.
        if brand == ZONE_BRAND_SOLARIS:
            vcpu_resource = 'capped-cpu'
        else:
            vcpu_resource = 'virtual-cpu'

        # TODO(dcomay): Until 17881862 is resolved, this should be turned into
        # an appropriate 'rctl' resource for the 'capped-cpu' case.
        with ZoneConfig(zone) as zc:
            zc.setprop(vcpu_resource, 'ncpus', str(vcpus))

    def _set_memory_cap(self, name, memory_mb, brand):
        """Set memory cap in a Solaris Zone configuration."""
        zone = self._get_zone_by_name(name)
        if zone is None:
            raise exception.InstanceNotFound(instance_id=name)

        # The Solaris Zone brand type is used to specify the type of
        # 'memory' cap set in the Solaris Zone configuration.
        if brand == ZONE_BRAND_SOLARIS:
            mem_resource = 'swap'
        else:
            mem_resource = 'physical'

        with ZoneConfig(zone) as zc:
            zc.setprop('capped-memory', mem_resource, '%dM' % memory_mb)

    def _set_network(self, context, name, instance, network_info, brand,
                     sc_dir):
        """add networking information to the zone."""
        zone = self._get_zone_by_name(name)
        if zone is None:
            raise exception.InstanceNotFound(instance_id=name)

        if not network_info:
            with ZoneConfig(zone) as zc:
                if brand == ZONE_BRAND_SOLARIS:
                    zc.removeresources("anet",
                                       [zonemgr.Property("linkname", "net0")])
                else:
                    zc.removeresources("anet", [zonemgr.Property("id", "0")])
                return

        tenant_id = None
        network_plugin = neutronv2.get_client(context)
        for netid, network in enumerate(network_info):
            if tenant_id is None:
                tenant_id = network['network']['meta']['tenant_id']
            port_uuid = network['id']
            port = network_plugin.show_port(port_uuid)['port']
            evs_uuid = port['network_id']
            vport_uuid = port['id']
            ip = network['network']['subnets'][0]['ips'][0]['address']
            ip_plen = network['network']['subnets'][0]['cidr'].split('/')[1]
            ip = '/'.join([ip, ip_plen])
            ip_version = network['network']['subnets'][0]['version']
            route = network['network']['subnets'][0]['gateway']['address']
            dns_list = network['network']['subnets'][0]['dns']
            nameservers = []
            for dns in dns_list:
                if dns['type'] == 'dns':
                    nameservers.append(dns['address'])

            with ZoneConfig(zone) as zc:
                if netid == 0:
                    zc.setprop('anet', 'configure-allowed-address', 'false')
                    zc.setprop('anet', 'evs', evs_uuid)
                    zc.setprop('anet', 'vport', vport_uuid)
                else:
                    zc.addresource(
                        'anet',
                        [zonemgr.Property('configure-allowed-address',
                                          'false'),
                         zonemgr.Property('evs', evs_uuid),
                         zonemgr.Property('vport', vport_uuid)])

                filter = [zonemgr.Property('vport', vport_uuid)]
                if brand == ZONE_BRAND_SOLARIS:
                    linkname = lookup_resource_property(zc.zone, 'anet',
                                                        'linkname', filter)
                else:
                    id = lookup_resource_property(zc.zone, 'anet', 'id',
                                                  filter)
                    linkname = 'net%s' % id

            # create the required sysconfig file (or skip if this is part of a
            # resize process)
            tstate = instance['task_state']
            if tstate not in [task_states.RESIZE_FINISH,
                              task_states.RESIZE_REVERTING,
                              task_states.RESIZE_MIGRATING]:
                subnet_uuid = port['fixed_ips'][0]['subnet_id']
                subnet = network_plugin.show_subnet(subnet_uuid)['subnet']

                if subnet['enable_dhcp']:
                    tree = sysconfig.create_ncp_defaultfixed('dhcp', linkname,
                                                             netid, ip_version)
                else:
                    tree = sysconfig.create_ncp_defaultfixed('static',
                                                             linkname, netid,
                                                             ip_version, ip,
                                                             route,
                                                             nameservers)

                fp = os.path.join(sc_dir, 'evs-network-%d.xml' % netid)
                sysconfig.create_sc_profile(fp, tree)

        if tenant_id is not None:
            # set the tenant id
            with ZoneConfig(zone) as zc:
                zc.setprop('global', 'tenant', tenant_id)

    def _set_suspend(self, instance):
        """Use the instance name to specify the pathname for the suspend image.
        """
        name = instance['name']
        zone = self._get_zone_by_name(name)
        if zone is None:
            raise exception.InstanceNotFound(instance_id=name)

        path = os.path.join(CONF.zones_suspend_path, '%{zonename}')
        with ZoneConfig(zone) as zc:
            zc.addresource('suspend', [zonemgr.Property('path', path)])

    def _verify_sysconfig(self, sc_dir, instance, admin_password=None):
        """verify the SC profile(s) passed in contain an entry for
        system/config-user to configure the root account.  If an SSH key is
        specified, configure root's profile to use it.
        """
        usercheck = lambda e: e.attrib.get('name') == 'system/config-user'
        hostcheck = lambda e: e.attrib.get('name') == 'system/identity'

        root_account_needed = True
        hostname_needed = True
        sshkey = instance.get('key_data')
        name = instance.get('hostname')
        encrypted_password = None

        # encrypt admin password, using SHA-256 as default
        if admin_password is not None:
            encrypted_password = sha256_crypt.encrypt(admin_password)

        # find all XML files in sc_dir
        for root, dirs, files in os.walk(sc_dir):
            for fname in [f for f in files if f.endswith(".xml")]:
                fileroot = etree.parse(os.path.join(root, fname))

                # look for config-user properties
                if filter(usercheck, fileroot.findall('service')):
                    # a service element was found for config-user.  Verify
                    # root's password is set, the admin account name is set and
                    # the admin's password is set
                    pgs = fileroot.iter('property_group')
                    for pg in pgs:
                        if pg.attrib.get('name') == 'root_account':
                            root_account_needed = False

                # look for identity properties
                if filter(hostcheck, fileroot.findall('service')):
                    for props in fileroot.iter('propval'):
                        if props.attrib.get('name') == 'nodename':
                            hostname_needed = False

        # Verify all of the requirements were met.  Create the required SMF
        # profile(s) if needed.
        if root_account_needed:
            fp = os.path.join(sc_dir, 'config-root.xml')

            if admin_password is not None and sshkey is not None:
                # store password for horizon retrieval
                ctxt = nova_context.get_admin_context()
                enc = crypto.ssh_encrypt_text(sshkey, admin_password)
                instance.system_metadata.update(
                    password.convert_password(ctxt, base64.b64encode(enc)))
                instance.save()

            if encrypted_password is not None or sshkey is not None:
                # set up the root account as 'normal' with no expiration,
                # an ssh key, and a root password
                tree = sysconfig.create_default_root_account(
                    sshkey=sshkey, password=encrypted_password)
            else:
                # sets up root account with expiration if sshkey is None
                # and password is none
                tree = sysconfig.create_default_root_account(expire='0')

            sysconfig.create_sc_profile(fp, tree)

        elif sshkey is not None:
            fp = os.path.join(sc_dir, 'config-root-ssh-keys.xml')
            tree = sysconfig.create_root_ssh_keys(sshkey)
            sysconfig.create_sc_profile(fp, tree)

        if hostname_needed and name is not None:
            fp = os.path.join(sc_dir, 'hostname.xml')
            sysconfig.create_sc_profile(fp, sysconfig.create_hostname(name))

    def _create_config(self, context, instance, network_info, connection_info,
                       sc_dir, admin_password=None):
        """Create a new Solaris Zone configuration."""
        name = instance['name']
        if self._get_zone_by_name(name) is not None:
            raise exception.InstanceExists(name=name)

        extra_specs = self._get_extra_specs(instance)

        # If unspecified, default zone brand is ZONE_BRAND_SOLARIS
        brand = extra_specs.get('zonecfg:brand', ZONE_BRAND_SOLARIS)
        template = ZONE_BRAND_TEMPLATE.get(brand)
        # TODO(dcomay): Detect capability via libv12n(3LIB) or virtinfo(1M).
        if template is None:
            msg = (_("Invalid brand '%s' specified for instance '%s'"
                   % (brand, name)))
            raise exception.NovaException(msg)

        tstate = instance['task_state']
        if tstate not in [task_states.RESIZE_FINISH,
                          task_states.RESIZE_REVERTING,
                          task_states.RESIZE_MIGRATING]:
            sc_profile = extra_specs.get('install:sc_profile')
            if sc_profile is not None:
                if os.path.isfile(sc_profile):
                    shutil.copy(sc_profile, sc_dir)
                elif os.path.isdir(sc_profile):
                    shutil.copytree(sc_profile, os.path.join(sc_dir,
                                    'sysconfig'))

            self._verify_sysconfig(sc_dir, instance, admin_password)

        zonemanager = self.rad_connection.get_object(zonemgr.ZoneManager())
        try:
            zonemanager.create(name, None, template)
            self._set_global_properties(name, extra_specs, brand)
            if connection_info:
                self._set_boot_device(name, connection_info, brand)
            self._set_num_cpu(name, instance['vcpus'], brand)
            self._set_memory_cap(name, instance['memory_mb'], brand)
            self._set_network(context, name, instance, network_info, brand,
                              sc_dir)
        except Exception as reason:
            LOG.error(_("Unable to create configuration for instance '%s' via "
                        "zonemgr(3RAD): %s") % (name, reason))
            raise

    def _create_vnc_console_service(self, instance):
        """Create a VNC console SMF service for a Solaris Zone"""
        # Basic environment checks first: vncserver and xterm
        if not os.path.exists(VNC_SERVER_PATH):
            LOG.warning(_("Zone VNC console SMF service not available on this "
                          "compute node. %s is missing. Run 'pkg install "
                          "x11/server/xvnc'") % VNC_SERVER_PATH)
            raise exception.ConsoleTypeUnavailable(console_type='vnc')

        if not os.path.exists(XTERM_PATH):
            LOG.warning(_("Zone VNC console SMF service not available on this "
                          "compute node. %s is missing. Run 'pkg install "
                          "terminal/xterm'") % XTERM_PATH)
            raise exception.ConsoleTypeUnavailable(console_type='vnc')

        name = instance['name']
        # TODO(npower): investigate using RAD instead of CLI invocation
        try:
            out, err = utils.execute('/usr/sbin/svccfg', '-s',
                                     VNC_CONSOLE_BASE_FMRI, 'add', name)
        except processutils.ProcessExecutionError as err:
            if self._has_vnc_console_service(instance):
                LOG.debug(_("Ignoring attempt to create existing zone VNC "
                            "console SMF service for instance '%s'") % name)
                return
            else:
                LOG.error(_("Unable to create zone VNC console SMF service "
                            "'{0}': {1}").format(
                                VNC_CONSOLE_BASE_FMRI + ':' + name, err))
                raise

    def _delete_vnc_console_service(self, instance):
        """Delete a VNC console SMF service for a Solaris Zone"""
        name = instance['name']
        self._disable_vnc_console_service(instance)
        # TODO(npower): investigate using RAD instead of CLI invocation
        try:
            out, err = utils.execute('/usr/sbin/svccfg', '-s',
                                     VNC_CONSOLE_BASE_FMRI, 'delete', name)
        except processutils.ProcessExecutionError as err:
            if not self._has_vnc_console_service(instance):
                LOG.debug(_("Ignoring attempt to delete a non-existent zone "
                            "VNC console SMF service for instance '%s'")
                          % name)
                return
            else:
                LOG.error(_("Unable to delete zone VNC console SMF service "
                            "'%s': %s")
                          % (VNC_CONSOLE_BASE_FMRI + ':' + name, err))
                raise

    def _enable_vnc_console_service(self, instance):
        """Enable a zone VNC console SMF service"""
        name = instance['name']

        console_fmri = VNC_CONSOLE_BASE_FMRI + ':' + name
        # TODO(npower): investigate using RAD instead of CLI invocation
        try:
            # The console SMF service exits with SMF_TEMP_DISABLE to prevent
            # unnecessarily coming online at boot. Tell it to really bring
            # it online.
            out, err = utils.execute('/usr/sbin/svccfg', '-s', console_fmri,
                                     'setprop', 'vnc/nova-enabled=true')
            out, err = utils.execute('/usr/sbin/svccfg', '-s', console_fmri,
                                     'refresh')
            out, err = utils.execute('/usr/sbin/svcadm', 'enable',
                                     console_fmri)
        except processutils.ProcessExecutionError as err:
            if not self._has_vnc_console_service(instance):
                LOG.debug(_("Ignoring attempt to enable a non-existent zone "
                            "VNC console SMF service for instance '%s'")
                          % name)
            LOG.error(_("Unable to start zone VNC console SMF service "
                        "'%s': %s") % (console_fmri, err))
            raise

        # Allow some time for the console service to come online.
        greenthread.sleep(2)
        while True:
            try:
                out, err = utils.execute('/usr/bin/svcs', '-H', '-o', 'state',
                                         console_fmri)
                state = out.strip()
                if state == 'online':
                    break
                elif state in ['maintenance', 'offline']:
                    LOG.error(_("Zone VNC console SMF service '%s' is in the "
                                "'%s' state. Run 'svcs -x %s' for details.")
                              % (console_fmri, state, console_fmri))
                    raise exception.ConsoleNotFoundForInstance(
                        instance_uuid=instance['uuid'])
                # Wait for service state to transition to (hopefully) online
                # state or offline/maintenance states.
                greenthread.sleep(2)
            except processutils.ProcessExecutionError as err:
                LOG.error(_("Error querying state of zone VNC console SMF "
                            "service '%s': %s") % (console_fmri, err))
                raise
        # TODO(npower): investigate using RAD instead of CLI invocation
        try:
            # The console SMF service exits with SMF_TEMP_DISABLE to prevent
            # unnecessarily coming online at boot. Make that happen.
            out, err = utils.execute('/usr/sbin/svccfg', '-s', console_fmri,
                                     'setprop', 'vnc/nova-enabled=false')
            out, err = utils.execute('/usr/sbin/svccfg', '-s', console_fmri,
                                     'refresh')
        except processutils.ProcessExecutionError as err:
            LOG.error(_("Unable to update 'vnc/nova-enabled' property for "
                        "zone VNC console SMF service "
                        "'%s': %s") % (console_fmri, err))
            raise

    def _disable_vnc_console_service(self, instance):
        """Disable a zone VNC console SMF service"""
        name = instance['name']
        if not self._has_vnc_console_service(instance):
            LOG.debug(_("Ignoring attempt to disable a non-existent zone VNC "
                        "console SMF service for instance '%s'") % name)
            return
        console_fmri = VNC_CONSOLE_BASE_FMRI + ':' + name
        # TODO(npower): investigate using RAD instead of CLI invocation
        try:
            out, err = utils.execute('/usr/sbin/svcadm', 'disable', '-s',
                                     console_fmri)
        except processutils.ProcessExecutionError as err:
            LOG.error(_("Unable to disable zone VNC console SMF service "
                        "'%s': %s") % (console_fmri, err))
        # The console service sets a SMF instance property for the port
        # on which the VNC service is listening. The service needs to be
        # refreshed to reset the property value
        try:
            out, err = utils.execute('/usr/sbin/svccfg', '-s', console_fmri,
                                     'refresh')
        except processutils.ProcessExecutionError as err:
            LOG.error(_("Unable to refresh zone VNC console SMF service "
                        "'%s': %s") % (console_fmri, err))

    def _get_vnc_console_service_state(self, instance):
        """Returns state of the instance zone VNC console SMF service"""
        name = instance['name']
        if not self._has_vnc_console_service(instance):
            LOG.warning(_("Console state requested for a non-existent zone "
                          "VNC console SMF service for instance '%s'")
                        % name)
            return None
        console_fmri = VNC_CONSOLE_BASE_FMRI + ':' + name
        # TODO(npower): investigate using RAD instead of CLI invocation
        try:
            state, err = utils.execute('/usr/sbin/svcs', '-H', '-o', 'state',
                                       console_fmri)
            return state.strip()
        except processutils.ProcessExecutionError as err:
            LOG.error(_("Console state request failed for zone VNC console "
                        "SMF service for instance '%s': %s") % (name, err))
            raise

    def _has_vnc_console_service(self, instance):
        """Returns True if the instance has a zone VNC console SMF service"""
        name = instance['name']
        console_fmri = VNC_CONSOLE_BASE_FMRI + ':' + name
        # TODO(npower): investigate using RAD instead of CLI invocation
        try:
            utils.execute('/usr/bin/svcs', '-H', '-o', 'state',
                          console_fmri)
            return True
        except processutils.ProcessExecutionError as err:
            return False

    def _install(self, instance, image, sc_dir):
        """Install a new Solaris Zone root file system."""
        name = instance['name']
        zone = self._get_zone_by_name(name)
        if zone is None:
            raise exception.InstanceNotFound(instance_id=name)

        # log the zone's configuration
        with ZoneConfig(zone) as zc:
            LOG.debug("-" * 80)
            LOG.debug(zc.zone.exportConfig(True))
            LOG.debug("-" * 80)

        options = ['-a', image]

        if os.listdir(sc_dir):
            # the directory isn't empty so pass it along to install
            options.extend(['-c', sc_dir])

        try:
            LOG.debug(_("installing instance '%s' (%s)") %
                      (name, instance['display_name']))
            zone.install(options=options)
        except Exception as reason:
            LOG.error(_("Unable to install root file system for instance '%s' "
                        "via zonemgr(3RAD): %s") % (name, reason))
            raise

        LOG.debug(_("installation of instance '%s' (%s) complete") %
                  (name, instance['display_name']))

        if os.listdir(sc_dir):
            # remove the sc_profile temp directory
            shutil.rmtree(sc_dir)

    def _power_on(self, instance):
        """Power on a Solaris Zone."""
        name = instance['name']
        zone = self._get_zone_by_name(name)
        if zone is None:
            raise exception.InstanceNotFound(instance_id=name)

        try:
            zone.boot()
        except Exception as reason:
            LOG.error(_("Unable to power on instance '%s' via zonemgr(3RAD): "
                        "%s") % (name, reason))
            raise exception.InstancePowerOnFailure(reason=reason)

    def _uninstall(self, instance):
        """Uninstall an existing Solaris Zone root file system."""
        name = instance['name']
        zone = self._get_zone_by_name(name)
        if zone is None:
            raise exception.InstanceNotFound(instance_id=name)

        if zone.state == ZONE_STATE_CONFIGURED:
            LOG.debug(_("Uninstall not required for zone '%s' in state '%s'")
                      % (name, zone.state))
            return
        try:
            zone.uninstall(['-F'])
        except Exception as reason:
            LOG.error(_("Unable to uninstall root file system for instance "
                        "'%s' via zonemgr(3RAD): %s") % (name, reason))
            raise

    def _delete_config(self, instance):
        """Delete an existing Solaris Zone configuration."""
        name = instance['name']
        if self._get_zone_by_name(name) is None:
            raise exception.InstanceNotFound(instance_id=name)

        zonemanager = self.rad_connection.get_object(zonemgr.ZoneManager())
        try:
            zonemanager.delete(name)
        except Exception as reason:
            LOG.error(_("Unable to delete configuration for instance '%s' via "
                        "zonemgr(3RAD): %s") % (name, reason))
            raise

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info=None, block_device_info=None):
        """Create a new instance/VM/domain on the virtualization platform.

        Once this successfully completes, the instance should be
        running (power_state.RUNNING).

        If this fails, any partial instance should be completely
        cleaned up, and the virtualization platform should be in the state
        that it was before this call began.

        :param context: security context
        :param instance: nova.objects.instance.Instance
                         This function should use the data there to guide
                         the creation of the new instance.
        :param image_meta: image object returned by nova.image.glance that
                           defines the image from which to boot this instance
        :param injected_files: User files to inject into instance.
        :param admin_password: Administrator password to set in instance.
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param block_device_info: Information about block devices to be
                                  attached to the instance.
        """
        image = self._fetch_image(context, instance)
        self._validate_image(image, instance)

        # create a new directory for SC profiles
        sc_dir = tempfile.mkdtemp(prefix="nova-sysconfig-",
                                  dir=CONF.state_path)
        os.chmod(sc_dir, 0755)

        # Attempt to provision a (Cinder) volume service backed boot volume
        volume = self._create_boot_volume(context, instance)
        volume_id = volume['id']
        # c1d0 is the standard dev for for default boot device.
        # Irrelevant value for ZFS, but Cinder gets stroppy without it.
        mountpoint = "c1d0"
        try:
            connection_info = self._connect_boot_volume(volume, mountpoint,
                                                        context, instance)
        except exception.InvalidVolume as badvol:
            # This Cinder volume is not usable for ZOSS so discard it.
            # zonecfg will apply default zonepath dataset configuration
            # instead. Carry on
            LOG.warning(_("Volume '%s' is being discarded: %s")
                        % (volume_id, badvol))
            self._volume_api.delete(context, volume_id)
            connection_info = None
        except Exception as reason:
            # Something really bad happened. Don't pass Go.
            LOG.error(_("Unable to attach root zpool volume '%s' to instance "
                        "%s: %s") % (volume['id'], instance['name'], reason))
            self._volume_api.delete(context, volume_id)
            raise

        name = instance['name']

        LOG.debug(_("creating zone configuration for '%s' (%s)") %
                  (name, instance['display_name']))

        self._create_config(context, instance, network_info, connection_info,
                            sc_dir, admin_password)
        try:
            self._install(instance, image, sc_dir)
            self._power_on(instance)
        except Exception as reason:
            LOG.error(_("Unable to spawn instance '%s' via zonemgr(3RAD): %s")
                      % (name, reason))
            self._uninstall(instance)
            if connection_info:
                self._volume_api.detach(context, volume_id)
                self._volume_api.delete(context, volume_id)
            self._delete_config(instance)
            raise

        if connection_info:
            bdm = objects.BlockDeviceMapping(
                    source_type='volume',
                    destination_type='volume',
                    instance_uuid=instance.uuid,
                    volume_id=volume_id,
                    connection_info=jsonutils.dumps(connection_info),
                    device_name=mountpoint,
                    delete_on_termination=True,
                    volume_size=instance['root_gb'])
            bdm.create(context)
            bdm.save()

    def _power_off(self, instance, halt_type):
        """Power off a Solaris Zone."""
        name = instance['name']
        zone = self._get_zone_by_name(name)
        if zone is None:
            raise exception.InstanceNotFound(instance_id=name)

        try:
            if halt_type == 'SOFT':
                zone.shutdown()
            else:
                # 'HARD'
                zone.halt()
        except rad.client.ObjectError as reason:
            result = reason.get_payload()
            if result.code == zonemgr.ErrorCode.COMMAND_ERROR:
                LOG.warning(_("Ignoring command error returned while trying "
                              "to power off instance '%s' via zonemgr(3RAD): "
                              "%s" % (name, reason)))
                return
        except Exception as reason:
            LOG.error(_("Unable to power off instance '%s' via zonemgr(3RAD): "
                        "%s") % (name, reason))
            raise exception.InstancePowerOffFailure(reason=reason)

    def _samehost_revert_resize(self, context, instance, network_info,
                                block_device_info):
        """Reverts the zones configuration to pre-resize config
        """
        self.power_off(instance)

        inst_type = flavor_obj.Flavor.get_by_id(
            nova_context.get_admin_context(read_deleted='yes'),
            instance['instance_type_id'])
        extra_specs = inst_type['extra_specs'].copy()
        brand = extra_specs.get('zonecfg:brand', ZONE_BRAND_SOLARIS)

        name = instance['name']

        cpu = int(instance.system_metadata['old_instance_type_vcpus'])
        mem = int(instance.system_metadata['old_instance_type_memory_mb'])

        self._set_num_cpu(name, cpu, brand)
        self._set_memory_cap(name, mem, brand)

        rgb = int(instance.system_metadata['new_instance_type_root_gb'])
        old_rvid = instance.system_metadata.get('old_instance_volid')
        if old_rvid:
            new_rvid = instance.system_metadata.get('new_instance_volid')
            newvname = instance['display_name'] + "-" + self._rootzpool_suffix
            mount_dev = instance['root_device_name']
            del instance.system_metadata['new_instance_volid']
            del instance.system_metadata['old_instance_volid']

            self._resize_disk_migration(context, instance, new_rvid, old_rvid,
                                        rgb, mount_dev)

            self._volume_api.delete(context, new_rvid)

    def destroy(self, context, instance, network_info, block_device_info=None,
                destroy_disks=True, migrate_data=None):
        """Destroy the specified instance from the Hypervisor.

        If the instance is not found (for example if networking failed), this
        function should still succeed.  It's probably a good idea to log a
        warning in that case.

        :param context: security context
        :param instance: Instance object as returned by DB layer.
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param block_device_info: Information about block devices that should
                                  be detached from the instance.
        :param destroy_disks: Indicates if disks should be destroyed
        :param migrate_data: implementation specific params
        """
        if (instance['task_state'] == task_states.RESIZE_REVERTING and
           instance.system_metadata['old_vm_state'] == vm_states.RESIZED):
            self._samehost_revert_resize(context, instance, network_info,
                                         block_device_info)
            return

        try:
            # These methods log if problems occur so no need to double log
            # here. Just catch any stray exceptions and allow destroy to
            # proceed.
            if self._has_vnc_console_service(instance):
                self._disable_vnc_console_service(instance)
                self._delete_vnc_console_service(instance)
        except Exception:
            pass

        name = instance['name']
        zone = self._get_zone_by_name(name)
        # If instance cannot be found, just return.
        if zone is None:
            LOG.warning(_("Unable to find instance '%s' via zonemgr(3RAD)")
                        % name)
            return

        try:
            if self._get_state(zone) == power_state.RUNNING:
                self._power_off(instance, 'HARD')
            if self._get_state(zone) == power_state.SHUTDOWN:
                self._uninstall(instance)
            if self._get_state(zone) == power_state.NOSTATE:
                self._delete_config(instance)
        except Exception as reason:
            LOG.warning(_("Unable to destroy instance '%s' via zonemgr(3RAD): "
                          "%s") % (name, reason))

        # One last point of house keeping. If we are deleting the instance
        # during a resize operation we want to make sure the cinder volumes are
        # property cleaned up. We need to do this here, because the periodic
        # task that comes along and cleans these things up isn't nice enough to
        # pass a context in so that we could simply do the work there.  But
        # because we have access to a context, we can handle the work here and
        # let the periodic task simply clean up the left over zone
        # configuration that might be left around.  Note that the left over
        # zone will only show up in zoneadm list, not nova list.
        #
        # If the task state is RESIZE_REVERTING do not process these because
        # the cinder volume cleanup is taken care of in
        # finish_revert_migration.
        if instance['task_state'] == task_states.RESIZE_REVERTING:
            return

        tags = ['old_instance_volid', 'new_instance_volid']
        for tag in tags:
            volid = instance.system_metadata.get(tag)
            if volid:
                try:
                    LOG.debug(_("Deleting volume %s"), volid)
                    self._volume_api.delete(context, volid)
                    del instance.system_metadata[tag]
                except Exception:
                    pass

    def cleanup(self, context, instance, network_info, block_device_info=None,
                destroy_disks=True, migrate_data=None, destroy_vifs=True):
        """Cleanup the instance resources .

        Instance should have been destroyed from the Hypervisor before calling
        this method.

        :param context: security context
        :param instance: Instance object as returned by DB layer.
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param block_device_info: Information about block devices that should
                                  be detached from the instance.
        :param destroy_disks: Indicates if disks should be destroyed
        :param migrate_data: implementation specific params
        """
        raise NotImplementedError()

    def reboot(self, context, instance, network_info, reboot_type,
               block_device_info=None, bad_volumes_callback=None):
        """Reboot the specified instance.

        After this is called successfully, the instance's state
        goes back to power_state.RUNNING. The virtualization
        platform should ensure that the reboot action has completed
        successfully even in cases in which the underlying domain/vm
        is paused or halted/stopped.

        :param instance: nova.objects.instance.Instance
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param reboot_type: Either a HARD or SOFT reboot
        :param block_device_info: Info pertaining to attached volumes
        :param bad_volumes_callback: Function to handle any bad volumes
            encountered
        """
        name = instance['name']
        zone = self._get_zone_by_name(name)
        if zone is None:
            raise exception.InstanceNotFound(instance_id=name)

        if self._get_state(zone) == power_state.SHUTDOWN:
            self._power_on(instance)
            return

        try:
            if reboot_type == 'SOFT':
                zone.shutdown(['-r'])
            else:
                zone.reboot()
        except Exception as reason:
            LOG.error(_("Unable to reboot instance '%s' via zonemgr(3RAD): %s")
                      % (name, reason))
            raise exception.InstanceRebootFailure(reason=reason)

    def get_console_pool_info(self, console_type):
        # TODO(Vek): Need to pass context in for access to auth_token
        raise NotImplementedError()

    def _get_console_output(self, instance):
        """Builds a string containing the console output (capped at
        MAX_CONSOLE_BYTES characters) by reassembling the log files
        that Solaris Zones framework maintains for each zone.
        """
        console_str = ""
        avail = MAX_CONSOLE_BYTES

        # Examine the log files in most-recently modified order, keeping
        # track of the size of each file and of how many characters have
        # been seen. If there are still characters left to incorporate,
        # then the contents of the log file in question are prepended to
        # the console string built so far. When the number of characters
        # available has run out, the last fragment under consideration
        # will likely begin within the middle of a line. As such, the
        # start of the fragment up to the next newline is thrown away.
        # The remainder constitutes the start of the resulting console
        # output which is then prepended to the console string built so
        # far and the result returned.
        logfile_pattern = '/var/log/zones/%s.console*' % instance['name']
        logfiles = sorted(glob.glob(logfile_pattern), key=os.path.getmtime,
                          reverse=True)
        for file in logfiles:
            size = os.path.getsize(file)
            if size == 0:
                continue
            avail -= size
            with open(file, 'r') as log:
                if avail < 0:
                    (fragment, _) = utils.last_bytes(log, avail + size)
                    remainder = fragment.find('\n') + 1
                    console_str = fragment[remainder:] + console_str
                    break
                fragment = ''
                for line in log.readlines():
                    fragment += line
                console_str = fragment + console_str
        return console_str

    def get_console_output(self, context, instance):
        """Get console output for an instance

        :param context: security context
        :param instance: nova.objects.instance.Instance
        """
        return self._get_console_output(instance)

    def get_vnc_console(self, context, instance):
        """Get connection info for a vnc console.

        :param context: security context
        :param instance: nova.objects.instance.Instance

        :returns an instance of console.type.ConsoleVNC
        """
        # Do not provide console access prematurely. Zone console access is
        # exclusive and zones that are still installing require their console.
        # Grabbing the zone console will break installation.
        name = instance['name']
        if instance['vm_state'] == vm_states.BUILDING:
            LOG.info(_("VNC console not available until zone '%s' has "
                     "completed installation. Try again later.") % name)
            raise exception.InstanceNotReady(instance_id=instance['uuid'])

        if not self._has_vnc_console_service(instance):
            LOG.debug(_("Creating zone VNC console SMF service for "
                      "instance '%s'") % name)
            self._create_vnc_console_service(instance)

        self._enable_vnc_console_service(instance)
        console_fmri = VNC_CONSOLE_BASE_FMRI + ':' + name

        # The console service sets an SMF instance property for the port
        # on which the VNC service is listening. The service needs to be
        # refreshed to reflect the current property value
        # TODO(npower): investigate using RAD instead of CLI invocation
        try:
            out, err = utils.execute('/usr/sbin/svccfg', '-s', console_fmri,
                                     'refresh')
        except processutils.ProcessExecutionError as err:
            LOG.error(_("Unable to refresh zone VNC console SMF service "
                        "'%s': %s" % (console_fmri, err)))
            raise

        host = CONF.vncserver_proxyclient_address
        try:
            out, err = utils.execute('/usr/bin/svcprop', '-p', 'vnc/port',
                                     console_fmri)
            port = int(out.strip())
            return ctype.ConsoleVNC(host=host,
                                    port=port,
                                    internal_access_path=None)
        except processutils.ProcessExecutionError as err:
            LOG.error(_("Unable to read VNC console port from zone VNC "
                        "console SMF service '%s': %s"
                      % (console_fmri, err)))

    def get_spice_console(self, context, instance):
        """Get connection info for a spice console.

        :param context: security context
        :param instance: nova.objects.instance.Instance

        :returns an instance of console.type.ConsoleSpice
        """
        raise NotImplementedError()

    def get_rdp_console(self, context, instance):
        """Get connection info for a rdp console.

        :param context: security context
        :param instance: nova.objects.instance.Instance

        :returns an instance of console.type.ConsoleRDP
        """
        raise NotImplementedError()

    def get_serial_console(self, context, instance):
        """Get connection info for a serial console.

        :param context: security context
        :param instance: nova.objects.instance.Instance

        :returns an instance of console.type.ConsoleSerial
        """
        raise NotImplementedError()

    def _get_zone_diagnostics(self, zone):
        """Return data about Solaris Zone diagnostics."""
        if zone.id == -1:
            return None

        diagnostics = {}
        id = str(zone.id)

        kstat_data = self._get_kstat_by_name('zone_caps', 'caps', id,
                                             ''.join(('lockedmem_zone_', id)))
        if kstat_data is not None:
            diagnostics['lockedmem'] = kstat_data['usage']

        kstat_data = self._get_kstat_by_name('zone_caps', 'caps', id,
                                             ''.join(('nprocs_zone_', id)))
        if kstat_data is not None:
            diagnostics['nprocs'] = kstat_data['usage']

        kstat_data = self._get_kstat_by_name('zone_caps', 'caps', id,
                                             ''.join(('swapresv_zone_', id)))
        if kstat_data is not None:
            diagnostics['swapresv'] = kstat_data['usage']

        kstat_data = self._get_kstat_by_name('zones', 'cpu', id,
                                             'sys_zone_aggr')
        if kstat_data is not None:
            for key in kstat_data.keys():
                if key not in ('class', 'crtime', 'snaptime'):
                    diagnostics[key] = kstat_data[key]
        return diagnostics

    def get_diagnostics(self, instance):
        """Return data about VM diagnostics.

        :param instance: nova.objects.instance.Instance
        """
        # TODO(Vek): Need to pass context in for access to auth_token
        name = instance['name']
        zone = self._get_zone_by_name(name)
        if zone is None:
            LOG.error(_("Unable to find instance '%s' via zonemgr(3RAD)")
                      % name)
            raise exception.InstanceNotFound(instance_id=name)
        return self._get_zone_diagnostics(zone)

    def get_instance_diagnostics(self, instance):
        """Return data about VM diagnostics.

        :param instance: nova.objects.instance.Instance
        """
        raise NotImplementedError()

    def get_all_bw_counters(self, instances):
        """Return bandwidth usage counters for each interface on each
           running VM.

        :param instances: nova.objects.instance.InstanceList
        """
        raise NotImplementedError()

    def get_all_volume_usage(self, context, compute_host_bdms):
        """Return usage info for volumes attached to vms on
           a given host.-
        """
        raise NotImplementedError()

    def get_host_ip_addr(self):
        """Retrieves the IP address of the dom0
        """
        # TODO(Vek): Need to pass context in for access to auth_token
        return CONF.my_ip

    def attach_volume(self, context, connection_info, instance, mountpoint,
                      disk_bus=None, device_type=None, encryption=None):
        """Attach the disk to the instance at mountpoint using info."""
        # TODO(npower): Apply mountpoint in a meaningful way to the zone
        # For security reasons this is not permitted in a Solaris branded zone.
        name = instance['name']
        zone = self._get_zone_by_name(name)
        if zone is None:
            raise exception.InstanceNotFound(instance_id=name)

        extra_specs = self._get_extra_specs(instance)
        brand = extra_specs.get('zonecfg:brand', ZONE_BRAND_SOLARIS)
        if brand != ZONE_BRAND_SOLARIS_KZ:
            # Only Solaris kernel zones are currently supported.
            reason = (_("'%s' branded zones are not currently supported")
                      % brand)
            raise NotImplementedError(reason)

        suri = self._suri_from_volume_info(connection_info)

        with ZoneConfig(zone) as zc:
            zc.addresource("device", [zonemgr.Property("storage", suri)])

        # apply the configuration to the running zone
        if zone.state == ZONE_STATE_RUNNING:
            zone.apply()

    def detach_volume(self, connection_info, instance, mountpoint,
                      encryption=None):
        """Detach the disk attached to the instance."""
        name = instance['name']
        zone = self._get_zone_by_name(name)
        if zone is None:
            raise exception.InstanceNotFound(instance_id=name)

        extra_specs = self._get_extra_specs(instance)
        brand = extra_specs.get('zonecfg:brand', ZONE_BRAND_SOLARIS)
        if brand != ZONE_BRAND_SOLARIS_KZ:
            # Only Solaris kernel zones are currently supported.
            reason = (_("'%s' branded zones are not currently supported")
                      % brand)
            raise NotImplementedError(reason)

        suri = self._suri_from_volume_info(connection_info)

        # Check if the specific property value exists before attempting removal
        prop = lookup_resource_property_value(zone, "device", "storage", suri)
        if not prop:
            LOG.warning(_("Storage resource '%s' is not attached to instance "
                        "'%s'") % (suri, name))
            return

        with ZoneConfig(zone) as zc:
            zc.removeresources("device", [zonemgr.Property("storage", suri)])

        # apply the configuration to the running zone
        if zone.state == ZONE_STATE_RUNNING:
            zone.apply()

    def swap_volume(self, old_connection_info, new_connection_info,
                    instance, mountpoint, resize_to):
        """Replace the disk attached to the instance.

        :param instance: nova.objects.instance.Instance
        :param resize_to: This parameter is used to indicate the new volume
                          size when the new volume lager than old volume.
                          And the units is Gigabyte.
        """
        raise NotImplementedError()

    def attach_interface(self, instance, image_meta, vif):
        """Attach an interface to the instance.

        :param instance: nova.objects.instance.Instance
        """
        raise NotImplementedError()

    def detach_interface(self, instance, vif):
        """Detach an interface from the instance.

        :param instance: nova.objects.instance.Instance
        """
        raise NotImplementedError()

    def _cleanup_migrate_disk(self, context, instance, volume):
        """Make a best effort at cleaning up the volume that was created to
        hold the new root disk

        :param context: the context for the migration/resize
        :param instance: nova.objects.instance.Instance being migrated/resized
        :param volume: new volume created by the call to cinder create
        """
        try:
            self._volume_api.delete(context, volume['id'])
        except Exception as err:
            LOG.error(_("Unable to cleanup the resized volume: %s" % err))

    def migrate_disk_and_power_off(self, context, instance, dest,
                                   flavor, network_info,
                                   block_device_info=None,
                                   timeout=0, retry_interval=0):
        """Transfers the disk of a running instance in multiple phases, turning
        off the instance before the end.

        :param instance: nova.objects.instance.Instance
        :param timeout: time to wait for GuestOS to shutdown
        :param retry_interval: How often to signal guest while
                               waiting for it to shutdown
        """
        LOG.debug("Starting migrate_disk_and_power_off", instance=instance)

        samehost = (dest == self.get_host_ip_addr())
        inst_type = flavor_obj.Flavor.get_by_id(
            nova_context.get_admin_context(read_deleted='yes'),
            instance['instance_type_id'])
        extra_specs = inst_type['extra_specs'].copy()
        brand = extra_specs.get('zonecfg:brand', ZONE_BRAND_SOLARIS)
        if brand != ZONE_BRAND_SOLARIS_KZ and not samehost:
            msg = (_("'%s' branded zones do not currently support "
                     "resize to a different host.") % brand)
            raise exception.MigrationPreCheckError(reason=msg)

        if brand != flavor['extra_specs'].get('zonecfg:brand'):
            msg = (_("Unable to change brand of zone during resize."))
            raise exception.MigrationPreCheckError(reason=msg)

        orgb = instance['root_gb']
        nrgb = int(instance.system_metadata['new_instance_type_root_gb'])
        if orgb > nrgb:
            msg = (_("Unable to resize to a smaller boot volume."))
            raise exception.ResizeError(reason=msg)

        self.power_off(instance, timeout, retry_interval)

        disk_info = None
        if nrgb > orgb or not samehost:
            bmap = block_device_info.get('block_device_mapping')
            rootmp = instance.root_device_name
            for entry in bmap:
                mountdev = entry['mount_device'].rpartition('/')[2]
                if mountdev == rootmp:
                    root_ci = entry['connection_info']
                    break
            else:
                # If this is a non-global zone that is on the same host and is
                # simply using a dataset, the disk size is purely an OpenStack
                # quota.  We can continue without doing any disk work.
                if samehost and brand == ZONE_BRAND_SOLARIS:
                    return disk_info
                else:
                    msg = (_("Cannot find an attached root device."))
                    raise exception.ResizeError(reason=msg)

            if root_ci['driver_volume_type'] == 'iscsi':
                volume_id = root_ci['data']['volume_id']
            else:
                volume_id = root_ci['serial']

            if volume_id is None:
                msg = (_("Cannot find an attached root device."))
                raise exception.ResizeError(reason=msg)

            vinfo = self._volume_api.get(context, volume_id)
            newvolume = self._volume_api.create(context, orgb,
                                                vinfo['display_name'] +
                                                '-resized',
                                                vinfo['display_description'],
                                                source_volume=vinfo)

            instance.system_metadata['old_instance_volid'] = volume_id
            instance.system_metadata['new_instance_volid'] = newvolume['id']

            # TODO(npower): Polling is what nova/compute/manager also does when
            # creating a new volume, so we do likewise here.
            while True:
                volume = self._volume_api.get(context, newvolume['id'])
                if volume['status'] != 'creating':
                    break
                greenthread.sleep(1)

            if nrgb > orgb:
                try:
                    self._volume_api.extend(context, newvolume['id'], nrgb)
                except Exception:
                    LOG.error(_("Failed to extend the new volume"))
                    self._cleanup_migrate_disk(context, instance, newvolume)
                    raise

            disk_info = newvolume

        return disk_info

    def snapshot(self, context, instance, image_id, update_task_state):
        """Snapshots the specified instance.

        :param context: security context
        :param instance: nova.objects.instance.Instance
        :param image_id: Reference to a pre-created image that will
                         hold the snapshot.
        """
        # Get original base image info
        (base_service, base_id) = glance.get_remote_image_service(
            context, instance['image_ref'])
        try:
            base = base_service.show(context, base_id)
        except exception.ImageNotFound:
            base = {}

        snapshot_service, snapshot_id = glance.get_remote_image_service(
            context, image_id)

        # Build updated snapshot image metadata
        snapshot = snapshot_service.show(context, snapshot_id)
        metadata = {
            'is_public': False,
            'status': 'active',
            'name': snapshot['name'],
            'properties': {
                'image_location': 'snapshot',
                'image_state': 'available',
                'owner_id': instance['project_id'],
                'instance_uuid': instance['uuid'],
            }
        }
        # Match architecture, hypervisor_type and vm_mode properties to base
        # image.
        for prop in ['architecture', 'hypervisor_type', 'vm_mode']:
            if prop in base.get('properties', {}):
                base_prop = base['properties'][prop]
                metadata['properties'][prop] = base_prop

        # Set generic container and disk formats initially in case the glance
        # service rejects unified archives (uar) and zfs in metadata
        metadata['container_format'] = 'ovf'
        metadata['disk_format'] = 'raw'

        update_task_state(task_state=task_states.IMAGE_PENDING_UPLOAD)
        snapshot_directory = CONF.solariszones_snapshots_directory
        fileutils.ensure_tree(snapshot_directory)
        snapshot_name = uuid.uuid4().hex

        with utils.tempdir(dir=snapshot_directory) as tmpdir:
            out_path = os.path.join(tmpdir, snapshot_name)
            zone_name = instance['name']
            utils.execute('/usr/sbin/archiveadm', 'create', '--root-only',
                          '-z', zone_name, out_path)

            LOG.info(_("Snapshot extracted, beginning image upload"),
                     instance=instance)
            try:
                # Upload the archive image to the image service
                update_task_state(
                    task_state=task_states.IMAGE_UPLOADING,
                    expected_state=task_states.IMAGE_PENDING_UPLOAD)
                with open(out_path, 'r') as image_file:
                    snapshot_service.update(context,
                                            image_id,
                                            metadata,
                                            image_file)
                    LOG.info(_("Snapshot image upload complete"),
                             instance=instance)
                try:
                    # Try to update the image metadata container and disk
                    # formats more suitably for a unified archive if the
                    # glance server recognises them.
                    metadata['container_format'] = 'uar'
                    metadata['disk_format'] = 'zfs'
                    snapshot_service.update(context,
                                            image_id,
                                            metadata,
                                            None)
                except exception.Invalid as invalid:
                    LOG.warning(_("Image service rejected image metadata "
                                  "container and disk formats 'uar' and "
                                  "'zfs'. Using generic values 'ovf' and "
                                  "'raw' as fallbacks."))
            finally:
                # Delete the snapshot image file source
                os.unlink(out_path)

    def post_interrupted_snapshot_cleanup(self, context, instance):
        """Cleans up any resources left after an interrupted snapshot.

        :param context: security context
        :param instance: nova.objects.instance.Instance
        """
        pass

    def _cleanup_finish_migration(self, context, instance, disk_info,
                                  network_info, samehost):
        """Best effort attempt at cleaning up any additional resources that are
        not directly managed by Nova or Cinder so as not to leak these
        resources.
        """
        if disk_info:
            self._volume_api.detach(context, disk_info['id'])
            self._volume_api.delete(context, disk_info['id'])

            old_rvid = instance.system_metadata.get('old_instance_volid')
            if old_rvid:
                connector = self.get_volume_connector(instance)
                connection_info = self._volume_api.initialize_connection(
                                    context, old_rvid, connector)

                new_rvid = instance.system_metadata['new_instance_volid']

                rootmp = instance.root_device_name
                self._volume_api.attach(context, old_rvid, instance['uuid'],
                                        rootmp)

                bdmobj = objects.BlockDeviceMapping()
                bdm = bdmobj.get_by_volume_id(context, new_rvid)
                bdm['connection_info'] = jsonutils.dumps(connection_info)
                bdm['volume_id'] = old_rvid
                bdm.save()

                del instance.system_metadata['new_instance_volid']
                del instance.system_metadata['old_instance_volid']

        if not samehost:
            self.destroy(context, instance, network_info)
            instance['host'] = instance['launched_on']
            instance['node'] = instance['launched_on']

    def finish_migration(self, context, migration, instance, disk_info,
                         network_info, image_meta, resize_instance,
                         block_device_info=None, power_on=True):
        """Completes a resize.

        :param context: the context for the migration/resize
        :param migration: the migrate/resize information
        :param instance: nova.objects.instance.Instance being migrated/resized
        :param disk_info: the newly transferred disk information
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param image_meta: image object returned by nova.image.glance that
                           defines the image from which this instance
                           was created
        :param resize_instance: True if the instance is being resized,
                                False otherwise
        :param block_device_info: instance volume block device info
        :param power_on: True if the instance should be powered on, False
                         otherwise
        """
        if not resize_instance:
            raise NotImplementedError()

        samehost = (migration['dest_node'] == migration['source_node'])
        if samehost:
            instance.system_metadata['old_vm_state'] = vm_states.RESIZED

        inst_type = flavor_obj.Flavor.get_by_id(
            nova_context.get_admin_context(read_deleted='yes'),
            instance['instance_type_id'])
        extra_specs = inst_type['extra_specs'].copy()
        brand = extra_specs.get('zonecfg:brand', ZONE_BRAND_SOLARIS)
        name = instance['name']

        if disk_info:
            bmap = block_device_info.get('block_device_mapping')
            rootmp = instance['root_device_name']
            for entry in bmap:
                if entry['mount_device'] == rootmp:
                    mount_dev = entry['mount_device']
                    root_ci = entry['connection_info']
                    break

        try:
            if samehost:
                metadstr = 'new_instance_type_vcpus'
                cpu = int(instance.system_metadata[metadstr])
                metadstr = 'new_instance_type_memory_mb'
                mem = int(instance.system_metadata[metadstr])
                self._set_num_cpu(name, cpu, brand)
                self._set_memory_cap(name, mem, brand)

                # Add the new disk to the volume if the size of the disk
                # changed
                if disk_info:
                    metadstr = 'new_instance_type_root_gb'
                    rgb = int(instance.system_metadata[metadstr])
                    self._resize_disk_migration(context, instance,
                                                root_ci['serial'],
                                                disk_info['id'],
                                                rgb, mount_dev)

            else:
                # No need to check disk_info here, because when not on the
                # same host a disk_info is always passed in.
                mount_dev = 'c1d0'
                root_serial = root_ci['serial']
                connection_info = self._resize_disk_migration(context,
                                                              instance,
                                                              root_serial,
                                                              disk_info['id'],
                                                              0, mount_dev,
                                                              samehost)

                self._create_config(context, instance, network_info,
                                    connection_info, extra_specs, None)

                zone = self._get_zone_by_name(name)
                if zone is None:
                    raise exception.InstanceNotFound(instance_id=name)

                zone.attach(['-x', 'initialize-hostdata'])

                bmap = block_device_info.get('block_device_mapping')
                for entry in bmap:
                    if entry['mount_device'] != rootmp:
                        self.attach_volume(context,
                                           entry['connection_info'], instance,
                                           entry['mount_device'])

            if power_on:
                self._power_on(instance)

                if brand == ZONE_BRAND_SOLARIS:
                    return

                # Toggle the autoexpand to extend the size of the rpool.
                # We need to sleep for a few seconds to make sure the zone
                # is in a state to accept the toggle.  Once bugs are fixed
                # around the autoexpand and the toggle is no longer needed
                # or zone.boot() returns only after the zone is ready we
                # can remove this hack.
                greenthread.sleep(15)
                out, err = utils.execute('/usr/sbin/zlogin', '-S', name,
                                         '/usr/sbin/zpool', 'set',
                                         'autoexpand=off', 'rpool')
                out, err = utils.execute('/usr/sbin/zlogin', '-S', name,
                                         '/usr/sbin/zpool', 'set',
                                         'autoexpand=on', 'rpool')
        except Exception:
            # Attempt to cleanup the new zone and new volume to at least
            # give the user a chance to recover without too many hoops
            self._cleanup_finish_migration(context, instance, disk_info,
                                           network_info, samehost)
            raise

    def confirm_migration(self, context, migration, instance, network_info):
        """Confirms a resize, destroying the source VM.

        :param instance: nova.objects.instance.Instance
        """
        samehost = (migration['dest_host'] == self.get_host_ip_addr())
        old_rvid = instance.system_metadata.get('old_instance_volid')
        new_rvid = instance.system_metadata.get('new_instance_volid')
        if new_rvid and old_rvid:
            new_vname = instance['display_name'] + "-" + self._rootzpool_suffix
            del instance.system_metadata['old_instance_volid']
            del instance.system_metadata['new_instance_volid']

            self._volume_api.delete(context, old_rvid)
            self._volume_api.update(context, new_rvid,
                                    {'display_name': new_vname})

        if not samehost:
            self.destroy(context, instance, network_info)

    def _resize_disk_migration(self, context, instance, configured,
                               replacement, newvolumesz, mountdev,
                               samehost=True):
        """Handles the zone root volume switch-over or simply
        initializing the connection for the new zone if not resizing to the
        same host

        :param context: the context for the _resize_disk_migration
        :param instance: nova.objects.instance.Instance being resized
        :param configured: id of the current configured volume
        :param replacement: id of the new volume
        :param newvolumesz: size of the new volume
        :param mountdev: the mount point of the device
        :param samehost: is the resize happening on the same host
        """
        connector = self.get_volume_connector(instance)
        connection_info = self._volume_api.initialize_connection(context,
                                                                 replacement,
                                                                 connector)
        connection_info['serial'] = replacement
        rootmp = instance.root_device_name

        if samehost:
            name = instance['name']
            zone = self._get_zone_by_name(name)
            if zone is None:
                raise exception.InstanceNotFound(instance_id=name)

            # Need to detach the zone and re-attach the zone if this is a
            # non-global zone so that the update of the rootzpool resource does
            # not fail.
            if zone.brand == ZONE_BRAND_SOLARIS:
                zone.detach()

            try:
                self._set_boot_device(name, connection_info, zone.brand)
            finally:
                if zone.brand == ZONE_BRAND_SOLARIS:
                    zone.attach()

        try:
            self._volume_api.detach(context, configured)
        except Exception:
            LOG.error(_("Failed to detach the volume"))
            raise

        try:
            self._volume_api.attach(context, replacement, instance['uuid'],
                                    rootmp)
        except Exception:
            LOG.error(_("Failed to attach the volume"))
            raise

        bdmobj = objects.BlockDeviceMapping()
        bdm = bdmobj.get_by_volume_id(context, configured)
        bdm['connection_info'] = jsonutils.dumps(connection_info)
        bdm['volume_id'] = replacement
        bdm.save()

        if not samehost:
            return connection_info

    def finish_revert_migration(self, context, instance, network_info,
                                block_device_info=None, power_on=True):
        """Finish reverting a resize.

        :param context: the context for the finish_revert_migration
        :param instance: nova.objects.instance.Instance being migrated/resized
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param block_device_info: instance volume block device info
        :param power_on: True if the instance should be powered on, False
                         otherwise
        """
        # If this is not a samehost migration then we need to re-attach the
        # original volume to the instance.  If this was processed in the
        # initial revert handling this work has already been done.
        old_rvid = instance.system_metadata.get('old_instance_volid')
        if old_rvid:
            connector = self.get_volume_connector(instance)
            connection_info = self._volume_api.initialize_connection(context,
                                                                     old_rvid,
                                                                     connector)

            new_rvid = instance.system_metadata['new_instance_volid']
            self._volume_api.detach(context, new_rvid)
            self._volume_api.delete(context, new_rvid)

            rootmp = instance.root_device_name
            self._volume_api.attach(context, old_rvid, instance['uuid'],
                                    rootmp)

            bdmobj = objects.BlockDeviceMapping()
            bdm = bdmobj.get_by_volume_id(context, new_rvid)
            bdm['connection_info'] = jsonutils.dumps(connection_info)
            bdm['volume_id'] = old_rvid
            bdm.save()

            del instance.system_metadata['new_instance_volid']
            del instance.system_metadata['old_instance_volid']

            rootmp = instance.root_device_name
            bmap = block_device_info.get('block_device_mapping')
            for entry in bmap:
                if entry['mount_device'] != rootmp:
                    self.attach_volume(context,
                                       entry['connection_info'], instance,
                                       entry['mount_device'])

        self._power_on(instance)

    def pause(self, instance):
        """Pause the specified instance.

        :param instance: nova.objects.instance.Instance
        """
        # TODO(Vek): Need to pass context in for access to auth_token
        raise NotImplementedError()

    def unpause(self, instance):
        """Unpause paused VM instance.

        :param instance: nova.objects.instance.Instance
        """
        # TODO(Vek): Need to pass context in for access to auth_token
        raise NotImplementedError()

    def suspend(self, instance):
        """suspend the specified instance.

        :param instance: nova.objects.instance.Instance
        """
        # TODO(Vek): Need to pass context in for access to auth_token
        name = instance['name']
        zone = self._get_zone_by_name(name)
        if zone is None:
            raise exception.InstanceNotFound(instance_id=name)

        if zone.brand != ZONE_BRAND_SOLARIS_KZ:
            # Only Solaris kernel zones are currently supported.
            reason = (_("'%s' branded zones do not currently support "
                        "suspend. Use 'nova reset-state --active %s' "
                        "to reset instance state back to 'active'.")
                      % (zone.brand, instance['display_name']))
            raise exception.InstanceSuspendFailure(reason=reason)

        if self._get_state(zone) != power_state.RUNNING:
            reason = (_("Instance '%s' is not running.") % name)
            raise exception.InstanceSuspendFailure(reason=reason)

        try:
            new_path = os.path.join(CONF.zones_suspend_path, '%{zonename}')
            if not lookup_resource(zone, 'suspend'):
                # add suspend if not configured
                self._set_suspend(instance)
            elif lookup_resource_property(zone, 'suspend', 'path') != new_path:
                # replace the old suspend resource with the new one
                with ZoneConfig(zone) as zc:
                    zc.removeresources('suspend')
                self._set_suspend(instance)

            zone.suspend()
        except Exception as reason:
            LOG.error(_("Unable to suspend instance '%s' via "
                        "zonemgr(3RAD): %s") % (name, reason))
            raise exception.InstanceSuspendFailure(reason=reason)

    def resume(self, context, instance, network_info, block_device_info=None):
        """resume the specified instance.

        :param context: the context for the resume
        :param instance: nova.objects.instance.Instance being resumed
        :param network_info:
           :py:meth:`~nova.network.manager.NetworkManager.get_instance_nw_info`
        :param block_device_info: instance volume block device info
        """
        name = instance['name']
        zone = self._get_zone_by_name(name)
        if zone is None:
            raise exception.InstanceNotFound(instance_id=name)

        if zone.brand != ZONE_BRAND_SOLARIS_KZ:
            # Only Solaris kernel zones are currently supported.
            reason = (_("'%s' branded zones do not currently support "
                      "resume.") % zone.brand)
            raise exception.InstanceResumeFailure(reason=reason)

        # check that the instance is suspended
        if self._get_state(zone) != power_state.SHUTDOWN:
            reason = (_("Instance '%s' is not suspended.") % name)
            raise exception.InstanceResumeFailure(reason=reason)

        try:
            zone.boot()
        except Exception as reason:
            LOG.error(_("Unable to resume instance '%s' via zonemgr(3RAD): %s")
                      % (name, reason))
            raise exception.InstanceResumeFailure(reason=reason)

    def resume_state_on_host_boot(self, context, instance, network_info,
                                  block_device_info=None):
        """resume guest state when a host is booted.

        :param instance: nova.objects.instance.Instance
        """
        name = instance['name']
        zone = self._get_zone_by_name(name)
        if zone is None:
            raise exception.InstanceNotFound(instance_id=name)

        # TODO(dcomay): Should reconcile with value of zone's autoboot
        # property.
        if self._get_state(zone) not in (power_state.CRASHED,
                                         power_state.SHUTDOWN):
            return

        self._power_on(instance)

    def rescue(self, context, instance, network_info, image_meta,
               rescue_password):
        """Rescue the specified instance.

        :param instance: nova.objects.instance.Instance
        """
        raise NotImplementedError()

    def set_bootable(self, instance, is_bootable):
        """Set the ability to power on/off an instance.

        :param instance: nova.objects.instance.Instance
        """
        raise NotImplementedError()

    def unrescue(self, instance, network_info):
        """Unrescue the specified instance.

        :param instance: nova.objects.instance.Instance
        """
        # TODO(Vek): Need to pass context in for access to auth_token
        raise NotImplementedError()

    def power_off(self, instance, timeout=0, retry_interval=0):
        """Power off the specified instance.

        :param instance: nova.objects.instance.Instance
        :param timeout: time to wait for GuestOS to shutdown
        :param retry_interval: How often to signal guest while
                               waiting for it to shutdown
        """
        self._power_off(instance, 'SOFT')

    def power_on(self, context, instance, network_info,
                 block_device_info=None):
        """Power on the specified instance.

        :param instance: nova.objects.instance.Instance
        """
        self._power_on(instance)

    def soft_delete(self, instance):
        """Soft delete the specified instance.

        :param instance: nova.objects.instance.Instance
        """
        raise NotImplementedError()

    def restore(self, instance):
        """Restore the specified instance.

        :param instance: nova.objects.instance.Instance
        """
        raise NotImplementedError()

    def _get_zpool_property(self, prop, zpool):
        """Get the value of property from the zpool."""
        try:
            value = None
            (out, _err) = utils.execute('/usr/sbin/zpool', 'get', prop, zpool)
        except processutils.ProcessExecutionError as err:
            LOG.error(_("Failed to get property '%s' from zpool '%s': %s")
                      % (prop, zpool, err.stderr))
            return value

        zpool_prop = out.splitlines()[1].split()
        if zpool_prop[1] == prop:
            value = zpool_prop[2]
        return value

    def _update_host_stats(self):
        """Update currently known host stats."""
        host_stats = {}
        host_stats['vcpus'] = os.sysconf('SC_NPROCESSORS_ONLN')
        pages = os.sysconf('SC_PHYS_PAGES')
        host_stats['memory_mb'] = self._pages_to_kb(pages) / 1024

        out, err = utils.execute('/usr/sbin/zfs', 'list', '-Ho', 'name', '/')
        root_zpool = out.split('/')[0]
        size = self._get_zpool_property('size', root_zpool)
        if size is not None:
            host_stats['local_gb'] = Size(size).get(Size.gb_units)
        else:
            host_stats['local_gb'] = 0

        # Account for any existing processor sets by looking at the the
        # number of CPUs not assigned to any processor sets.
        kstat_data = self._get_kstat_by_name('misc', 'unix', '0', 'pset')
        if kstat_data is not None:
            host_stats['vcpus_used'] = \
                host_stats['vcpus'] - kstat_data['ncpus']
        else:
            host_stats['vcpus_used'] = 0

        # Subtract the number of free pages from the total to get the
        # used.
        kstat_data = self._get_kstat_by_name('pages', 'unix', '0',
                                             'system_pages')
        if kstat_data is not None:
            free_ram_mb = self._pages_to_kb(kstat_data['freemem']) / 1024
            host_stats['memory_mb_used'] = \
                host_stats['memory_mb'] - free_ram_mb
        else:
            host_stats['memory_mb_used'] = 0

        free = self._get_zpool_property('free', root_zpool)
        if free is not None:
            free_disk_gb = Size(free).get(Size.gb_units)
        else:
            free_disk_gb = 0
        host_stats['local_gb_used'] = host_stats['local_gb'] - free_disk_gb

        host_stats['hypervisor_type'] = 'solariszones'
        host_stats['hypervisor_version'] = int(self._uname[2].replace('.', ''))
        host_stats['hypervisor_hostname'] = self._uname[1]

        if self._uname[4] == 'i86pc':
            architecture = 'x86_64'
        else:
            architecture = 'sparc64'
        cpu_info = {
            'arch': architecture
        }
        host_stats['cpu_info'] = jsonutils.dumps(cpu_info)

        host_stats['disk_available_least'] = 0

        supported_instances = [
            (architecture, 'solariszones', 'solariszones')
        ]
        host_stats['supported_instances'] = \
            jsonutils.dumps(supported_instances)

        self._host_stats = host_stats

    def get_available_resource(self, nodename):
        """Retrieve resource information.

        This method is called when nova-compute launches, and
        as part of a periodic task that records the results in the DB.

        :param nodename:
            node which the caller want to get resources from
            a driver that manages only one node can safely ignore this
        :returns: Dictionary describing resources
        """
        self._update_host_stats()
        host_stats = self._host_stats

        resources = {}
        resources['vcpus'] = host_stats['vcpus']
        resources['memory_mb'] = host_stats['memory_mb']
        resources['local_gb'] = host_stats['local_gb']
        resources['vcpus_used'] = host_stats['vcpus_used']
        resources['memory_mb_used'] = host_stats['memory_mb_used']
        resources['local_gb_used'] = host_stats['local_gb_used']
        resources['hypervisor_type'] = host_stats['hypervisor_type']
        resources['hypervisor_version'] = host_stats['hypervisor_version']
        resources['hypervisor_hostname'] = host_stats['hypervisor_hostname']
        resources['cpu_info'] = host_stats['cpu_info']
        resources['disk_available_least'] = host_stats['disk_available_least']
        resources['supported_instances'] = host_stats['supported_instances']
        return resources

    def pre_live_migration(self, context, instance, block_device_info,
                           network_info, disk_info, migrate_data=None):
        """Prepare an instance for live migration

        :param context: security context
        :param instance: nova.objects.instance.Instance object
        :param block_device_info: instance block device information
        :param network_info: instance network information
        :param disk_info: instance disk information
        :param migrate_data: implementation specific data dict.
        """
        raise NotImplementedError()

    def live_migration(self, context, instance, dest,
                       post_method, recover_method, block_migration=False,
                       migrate_data=None):
        """Live migration of an instance to another host.

        :param context: security context
        :param instance:
            nova.db.sqlalchemy.models.Instance object
            instance object that is migrated.
        :param dest: destination host
        :param post_method:
            post operation method.
            expected nova.compute.manager._post_live_migration.
        :param recover_method:
            recovery method when any exception occurs.
            expected nova.compute.manager._rollback_live_migration.
        :param block_migration: if true, migrate VM disk.
        :param migrate_data: implementation specific params.

        """
        raise NotImplementedError()

    def rollback_live_migration_at_destination(self, context, instance,
                                               network_info,
                                               block_device_info,
                                               destroy_disks=True,
                                               migrate_data=None):
        """Clean up destination node after a failed live migration.

        :param context: security context
        :param instance: instance object that was being migrated
        :param network_info: instance network information
        :param block_device_info: instance block device information
        :param destroy_disks:
            if true, destroy disks at destination during cleanup
        :param migrate_data: implementation specific params

        """
        raise NotImplementedError()

    def post_live_migration(self, context, instance, block_device_info,
                            migrate_data=None):
        """Post operation of live migration at source host.

        :param context: security context
        :instance: instance object that was migrated
        :block_device_info: instance block device information
        :param migrate_data: if not None, it is a dict which has data
        """
        pass

    def post_live_migration_at_source(self, context, instance, network_info):
        """Unplug VIFs from networks at source.

        :param context: security context
        :param instance: instance object reference
        :param network_info: instance network information
        """
        raise NotImplementedError(_("Hypervisor driver does not support "
                                    "post_live_migration_at_source method"))

    def post_live_migration_at_destination(self, context, instance,
                                           network_info,
                                           block_migration=False,
                                           block_device_info=None):
        """Post operation of live migration at destination host.

        :param context: security context
        :param instance: instance object that is migrated
        :param network_info: instance network information
        :param block_migration: if true, post operation of block_migration.
        """
        raise NotImplementedError()

    def check_instance_shared_storage_local(self, context, instance):
        """Check if instance files located on shared storage.

        This runs check on the destination host, and then calls
        back to the source host to check the results.

        :param context: security context
        :param instance: nova.db.sqlalchemy.models.Instance
        """
        raise NotImplementedError()

    def check_instance_shared_storage_remote(self, context, data):
        """Check if instance files located on shared storage.

        :param context: security context
        :param data: result of check_instance_shared_storage_local
        """
        raise NotImplementedError()

    def check_instance_shared_storage_cleanup(self, context, data):
        """Do cleanup on host after check_instance_shared_storage calls

        :param context: security context
        :param data: result of check_instance_shared_storage_local
        """
        pass

    def check_can_live_migrate_destination(self, context, instance,
                                           src_compute_info, dst_compute_info,
                                           block_migration=False,
                                           disk_over_commit=False):
        """Check if it is possible to execute live migration.

        This runs checks on the destination host, and then calls
        back to the source host to check the results.

        :param context: security context
        :param instance: nova.db.sqlalchemy.models.Instance
        :param src_compute_info: Info about the sending machine
        :param dst_compute_info: Info about the receiving machine
        :param block_migration: if true, prepare for block migration
        :param disk_over_commit: if true, allow disk over commit
        :returns: a dict containing migration info (hypervisor-dependent)
        """
        raise NotImplementedError()

    def check_can_live_migrate_destination_cleanup(self, context,
                                                   dest_check_data):
        """Do required cleanup on dest host after check_can_live_migrate calls

        :param context: security context
        :param dest_check_data: result of check_can_live_migrate_destination
        """
        raise NotImplementedError()

    def check_can_live_migrate_source(self, context, instance,
                                      dest_check_data):
        """Check if it is possible to execute live migration.

        This checks if the live migration can succeed, based on the
        results from check_can_live_migrate_destination.

        :param context: security context
        :param instance: nova.db.sqlalchemy.models.Instance
        :param dest_check_data: result of check_can_live_migrate_destination
        :returns: a dict containing migration info (hypervisor-dependent)
        """
        raise NotImplementedError()

    def get_instance_disk_info(self, instance_name,
                               block_device_info=None):
        """Retrieve information about actual disk sizes of an instance.

        :param instance_name:
            name of a nova instance as returned by list_instances()
        :param block_device_info:
            Optional; Can be used to filter out devices which are
            actually volumes.
        :return:
            json strings with below format::

                "[{'path':'disk',
                   'type':'raw',
                   'virt_disk_size':'10737418240',
                   'backing_file':'backing_file',
                   'disk_size':'83886080'
                   'over_committed_disk_size':'10737418240'},
                   ...]"
        """
        raise NotImplementedError()

    def refresh_security_group_rules(self, security_group_id):
        """This method is called after a change to security groups.

        All security groups and their associated rules live in the datastore,
        and calling this method should apply the updated rules to instances
        running the specified security group.

        An error should be raised if the operation cannot complete.

        """
        # TODO(Vek): Need to pass context in for access to auth_token
        raise NotImplementedError()

    def refresh_security_group_members(self, security_group_id):
        """This method is called when a security group is added to an instance.

        This message is sent to the virtualization drivers on hosts that are
        running an instance that belongs to a security group that has a rule
        that references the security group identified by `security_group_id`.
        It is the responsibility of this method to make sure any rules
        that authorize traffic flow with members of the security group are
        updated and any new members can communicate, and any removed members
        cannot.

        Scenario:
            * we are running on host 'H0' and we have an instance 'i-0'.
            * instance 'i-0' is a member of security group 'speaks-b'
            * group 'speaks-b' has an ingress rule that authorizes group 'b'
            * another host 'H1' runs an instance 'i-1'
            * instance 'i-1' is a member of security group 'b'

            When 'i-1' launches or terminates we will receive the message
            to update members of group 'b', at which time we will make
            any changes needed to the rules for instance 'i-0' to allow
            or deny traffic coming from 'i-1', depending on if it is being
            added or removed from the group.

        In this scenario, 'i-1' could just as easily have been running on our
        host 'H0' and this method would still have been called.  The point was
        that this method isn't called on the host where instances of that
        group are running (as is the case with
        :py:meth:`refresh_security_group_rules`) but is called where references
        are made to authorizing those instances.

        An error should be raised if the operation cannot complete.

        """
        # TODO(Vek): Need to pass context in for access to auth_token
        raise NotImplementedError()

    def refresh_provider_fw_rules(self):
        """This triggers a firewall update based on database changes.

        When this is called, rules have either been added or removed from the
        datastore.  You can retrieve rules with
        :py:meth:`nova.db.provider_fw_rule_get_all`.

        Provider rules take precedence over security group rules.  If an IP
        would be allowed by a security group ingress rule, but blocked by
        a provider rule, then packets from the IP are dropped.  This includes
        intra-project traffic in the case of the allow_project_net_traffic
        flag for the libvirt-derived classes.

        """
        # TODO(Vek): Need to pass context in for access to auth_token
        raise NotImplementedError()

    def refresh_instance_security_rules(self, instance):
        """Refresh security group rules

        Gets called when an instance gets added to or removed from
        the security group the instance is a member of or if the
        group gains or loses a rule.
        """
        raise NotImplementedError()

    def reset_network(self, instance):
        """reset networking for specified instance."""
        # TODO(Vek): Need to pass context in for access to auth_token
        pass

    def ensure_filtering_rules_for_instance(self, instance, network_info):
        """Setting up filtering rules and waiting for its completion.

        To migrate an instance, filtering rules to hypervisors
        and firewalls are inevitable on destination host.
        ( Waiting only for filtering rules to hypervisor,
        since filtering rules to firewall rules can be set faster).

        Concretely, the below method must be called.
        - setup_basic_filtering (for nova-basic, etc.)
        - prepare_instance_filter(for nova-instance-instance-xxx, etc.)

        to_xml may have to be called since it defines PROJNET, PROJMASK.
        but libvirt migrates those value through migrateToURI(),
        so , no need to be called.

        Don't use thread for this method since migration should
        not be started when setting-up filtering rules operations
        are not completed.

        :param instance: nova.objects.instance.Instance object

        """
        # TODO(Vek): Need to pass context in for access to auth_token
        raise NotImplementedError()

    def filter_defer_apply_on(self):
        """Defer application of IPTables rules."""
        pass

    def filter_defer_apply_off(self):
        """Turn off deferral of IPTables rules and apply the rules now."""
        pass

    def unfilter_instance(self, instance, network_info):
        """Stop filtering instance."""
        # TODO(Vek): Need to pass context in for access to auth_token
        raise NotImplementedError()

    def set_admin_password(self, instance, new_pass):
        """Set the root password on the specified instance.

        :param instance: nova.objects.instance.Instance
        :param new_password: the new password
        """
        raise NotImplementedError()

    def inject_file(self, instance, b64_path, b64_contents):
        """Writes a file on the specified instance.

        The first parameter is an instance of nova.compute.service.Instance,
        and so the instance is being specified as instance.name. The second
        parameter is the base64-encoded path to which the file is to be
        written on the instance; the third is the contents of the file, also
        base64-encoded.

        NOTE(russellb) This method is deprecated and will be removed once it
        can be removed from nova.compute.manager.
        """
        # TODO(Vek): Need to pass context in for access to auth_token
        raise NotImplementedError()

    def change_instance_metadata(self, context, instance, diff):
        """Applies a diff to the instance metadata.

        This is an optional driver method which is used to publish
        changes to the instance's metadata to the hypervisor.  If the
        hypervisor has no means of publishing the instance metadata to
        the instance, then this method should not be implemented.

        :param context: security context
        :param instance: nova.objects.instance.Instance
        """
        pass

    def inject_network_info(self, instance, nw_info):
        """inject network info for specified instance."""
        # TODO(Vek): Need to pass context in for access to auth_token
        pass

    def poll_rebooting_instances(self, timeout, instances):
        """Poll for rebooting instances

        :param timeout: the currently configured timeout for considering
                        rebooting instances to be stuck
        :param instances: instances that have been in rebooting state
                          longer than the configured timeout
        """
        # TODO(Vek): Need to pass context in for access to auth_token
        raise NotImplementedError()

    def host_power_action(self, host, action):
        """Reboots, shuts down or powers up the host."""
        raise NotImplementedError()

    def host_maintenance_mode(self, host, mode):
        """Start/Stop host maintenance window. On start, it triggers
        guest VMs evacuation.
        """
        raise NotImplementedError()

    def set_host_enabled(self, host, enabled):
        """Sets the specified host's ability to accept new instances."""
        # TODO(Vek): Need to pass context in for access to auth_token
        raise NotImplementedError()

    def get_host_uptime(self, host):
        """Returns the result of calling "uptime" on the target host."""
        # TODO(Vek): Need to pass context in for access to auth_token
        return utils.execute('/usr/bin/uptime')[0]

    def plug_vifs(self, instance, network_info):
        """Plug VIFs into networks.

        :param instance: nova.objects.instance.Instance
        """
        # TODO(Vek): Need to pass context in for access to auth_token
        pass

    def unplug_vifs(self, instance, network_info):
        """Unplug VIFs from networks.

        :param instance: nova.objects.instance.Instance
        """
        raise NotImplementedError()

    def get_host_stats(self, refresh=False):
        """Return currently known host stats.

        If the hypervisor supports pci passthrough, the returned
        dictionary includes a key-value pair for it.
        The key of pci passthrough device is "pci_passthrough_devices"
        and the value is a json string for the list of assignable
        pci devices. Each device is a dictionary, with mandatory
        keys of 'address', 'vendor_id', 'product_id', 'dev_type',
        'dev_id', 'label' and other optional device specific information.

        Refer to the objects/pci_device.py for more idea of these keys.
        """
        if refresh or not self._host_stats:
            self._update_host_stats()
        return self._host_stats

    def get_host_cpu_stats(self):
        """Get the currently known host CPU stats.

        :returns: a dict containing the CPU stat info, eg:

            | {'kernel': kern,
            |  'idle': idle,
            |  'user': user,
            |  'iowait': wait,
            |   'frequency': freq},

                  where kern and user indicate the cumulative CPU time
                  (nanoseconds) spent by kernel and user processes
                  respectively, idle indicates the cumulative idle CPU time
                  (nanoseconds), wait indicates the cumulative I/O wait CPU
                  time (nanoseconds), since the host is booting up; freq
                  indicates the current CPU frequency (MHz). All values are
                  long integers.

        """
        raise NotImplementedError()

    def block_stats(self, instance_name, disk_id):
        """Return performance counters associated with the given disk_id on the
        given instance_name.  These are returned as [rd_req, rd_bytes, wr_req,
        wr_bytes, errs], where rd indicates read, wr indicates write, req is
        the total number of I/O requests made, bytes is the total number of
        bytes transferred, and errs is the number of requests held up due to a
        full pipeline.

        All counters are long integers.

        This method is optional.  On some platforms (e.g. XenAPI) performance
        statistics can be retrieved directly in aggregate form, without Nova
        having to do the aggregation.  On those platforms, this method is
        unused.

        Note that this function takes an instance ID.
        """
        raise NotImplementedError()

    def interface_stats(self, instance_name, iface_id):
        """Return performance counters associated with the given iface_id
        on the given instance_id.  These are returned as [rx_bytes, rx_packets,
        rx_errs, rx_drop, tx_bytes, tx_packets, tx_errs, tx_drop], where rx
        indicates receive, tx indicates transmit, bytes and packets indicate
        the total number of bytes or packets transferred, and errs and dropped
        is the total number of packets failed / dropped.

        All counters are long integers.

        This method is optional.  On some platforms (e.g. XenAPI) performance
        statistics can be retrieved directly in aggregate form, without Nova
        having to do the aggregation.  On those platforms, this method is
        unused.

        Note that this function takes an instance ID.
        """
        raise NotImplementedError()

    def deallocate_networks_on_reschedule(self, instance):
        """Does the driver want networks deallocated on reschedule?"""
        return False

    def macs_for_instance(self, instance):
        """What MAC addresses must this instance have?

        Some hypervisors (such as bare metal) cannot do freeform virtualisation
        of MAC addresses. This method allows drivers to return a set of MAC
        addresses that the instance is to have. allocate_for_instance will take
        this into consideration when provisioning networking for the instance.

        Mapping of MAC addresses to actual networks (or permitting them to be
        freeform) is up to the network implementation layer. For instance,
        with openflow switches, fixed MAC addresses can still be virtualised
        onto any L2 domain, with arbitrary VLANs etc, but regular switches
        require pre-configured MAC->network mappings that will match the
        actual configuration.

        Most hypervisors can use the default implementation which returns None.
        Hypervisors with MAC limits should return a set of MAC addresses, which
        will be supplied to the allocate_for_instance call by the compute
        manager, and it is up to that call to ensure that all assigned network
        details are compatible with the set of MAC addresses.

        This is called during spawn_instance by the compute manager.

        :return: None, or a set of MAC ids (e.g. set(['12:34:56:78:90:ab'])).
            None means 'no constraints', a set means 'these and only these
            MAC addresses'.
        """
        return None

    def dhcp_options_for_instance(self, instance):
        """Get DHCP options for this instance.

        Some hypervisors (such as bare metal) require that instances boot from
        the network, and manage their own TFTP service. This requires passing
        the appropriate options out to the DHCP service. Most hypervisors can
        use the default implementation which returns None.

        This is called during spawn_instance by the compute manager.

        Note that the format of the return value is specific to Quantum
        client API.

        :return: None, or a set of DHCP options, eg:

             |    [{'opt_name': 'bootfile-name',
             |      'opt_value': '/tftpboot/path/to/config'},
             |     {'opt_name': 'server-ip-address',
             |      'opt_value': '1.2.3.4'},
             |     {'opt_name': 'tftp-server',
             |      'opt_value': '1.2.3.4'}
             |    ]

        """
        pass

    def manage_image_cache(self, context, all_instances):
        """Manage the driver's local image cache.

        Some drivers chose to cache images for instances on disk. This method
        is an opportunity to do management of that cache which isn't directly
        related to other calls into the driver. The prime example is to clean
        the cache and remove images which are no longer of interest.

        :param instances: nova.objects.instance.InstanceList
        """
        pass

    def add_to_aggregate(self, context, aggregate, host, **kwargs):
        """Add a compute host to an aggregate."""
        # NOTE(jogo) Currently only used for XenAPI-Pool
        raise NotImplementedError()

    def remove_from_aggregate(self, context, aggregate, host, **kwargs):
        """Remove a compute host from an aggregate."""
        raise NotImplementedError()

    def undo_aggregate_operation(self, context, op, aggregate,
                                 host, set_error=True):
        """Undo for Resource Pools."""
        raise NotImplementedError()

    def get_volume_connector(self, instance):
        """Get connector information for the instance for attaching to volumes.

        Connector information is a dictionary representing the ip of the
        machine that will be making the connection, the name of the iscsi
        initiator, the WWPN and WWNN values of the Fibre Channel initiator,
        and the hostname of the machine as follows::

            {
                'ip': ip,
                'initiator': initiator,
                'wwnns': wwnns,
                'wwpns': wwpns,
                'host': hostname
            }

        """
        connector = {'ip': self.get_host_ip_addr(),
                     'host': CONF.host}
        if not self._initiator:
            self._initiator = self._get_iscsi_initiator()

        if self._initiator:
            connector['initiator'] = self._initiator
        else:
            LOG.warning(_("Could not determine iSCSI initiator name"),
                        instance=instance)

        if not self._fc_wwnns:
            self._fc_wwnns = self._get_fc_wwnns()
            if not self._fc_wwnns or len(self._fc_wwnns) == 0:
                LOG.debug(_('Could not determine Fibre Channel '
                          'World Wide Node Names'),
                          instance=instance)

        if not self._fc_wwpns:
            self._fc_wwpns = self._get_fc_wwpns()
            if not self._fc_wwpns or len(self._fc_wwpns) == 0:
                LOG.debug(_('Could not determine Fibre channel '
                          'World Wide Port Names'),
                          instance=instance)

        if self._fc_wwnns and self._fc_wwpns:
            connector["wwnns"] = self._fc_wwnns
            connector["wwpns"] = self._fc_wwpns
        return connector

    def get_available_nodes(self, refresh=False):
        """Returns nodenames of all nodes managed by the compute service.

        This method is for multi compute-nodes support. If a driver supports
        multi compute-nodes, this method returns a list of nodenames managed
        by the service. Otherwise, this method should return
        [hypervisor_hostname].
        """
        stats = self.get_host_stats(refresh=refresh)
        if not isinstance(stats, list):
            stats = [stats]
        return [s['hypervisor_hostname'] for s in stats]

    def node_is_available(self, nodename):
        """Return whether this compute service manages a particular node."""
        if nodename in self.get_available_nodes():
            return True
        # Refresh and check again.
        return nodename in self.get_available_nodes(refresh=True)

    def get_per_instance_usage(self):
        """Get information about instance resource usage.

        :returns: dict of  nova uuid => dict of usage info
        """
        return {}

    def instance_on_disk(self, instance):
        """Checks access of instance files on the host.

        :param instance: nova.objects.instance.Instance to lookup

        Returns True if files of an instance with the supplied ID accessible on
        the host, False otherwise.

        .. note::
            Used in rebuild for HA implementation and required for validation
            of access to instance shared disk files
        """
        return False

    def register_event_listener(self, callback):
        """Register a callback to receive events.

        Register a callback to receive asynchronous event
        notifications from hypervisors. The callback will
        be invoked with a single parameter, which will be
        an instance of the nova.virt.event.Event class.
        """

        self._compute_event_callback = callback

    def emit_event(self, event):
        """Dispatches an event to the compute manager.

        Invokes the event callback registered by the
        compute manager to dispatch the event. This
        must only be invoked from a green thread.
        """

        if not self._compute_event_callback:
            LOG.debug("Discarding event %s", str(event))
            return

        if not isinstance(event, virtevent.Event):
            raise ValueError(
                _("Event must be an instance of nova.virt.event.Event"))

        try:
            LOG.debug("Emitting event %s", str(event))
            self._compute_event_callback(event)
        except Exception as ex:
            LOG.error(_("Exception dispatching event %(event)s: %(ex)s"),
                      {'event': event, 'ex': ex})

    def delete_instance_files(self, instance):
        """Delete any lingering instance files for an instance.

        :param instance: nova.objects.instance.Instance
        :returns: True if the instance was deleted from disk, False otherwise.
        """
        LOG.debug(_("Cleaning up for instance %s"), instance['name'])
        # Delete the zone configuration for the instance using destroy, because
        # it will simply take care of the work, and we don't need to duplicate
        # the code here.
        try:
            self.destroy(None, instance, None)
        except Exception:
            return False
        return True

    @property
    def need_legacy_block_device_info(self):
        """Tell the caller if the driver requires legacy block device info.

        Tell the caller whether we expect the legacy format of block
        device info to be passed in to methods that expect it.
        """
        return True

    def volume_snapshot_create(self, context, instance, volume_id,
                               create_info):
        """Snapshots volumes attached to a specified instance.

        :param context: request context
        :param instance: nova.objects.instance.Instance that has the volume
               attached
        :param volume_id: Volume to be snapshotted
        :param create_info: The data needed for nova to be able to attach
               to the volume.  This is the same data format returned by
               Cinder's initialize_connection() API call.  In the case of
               doing a snapshot, it is the image file Cinder expects to be
               used as the active disk after the snapshot operation has
               completed.  There may be other data included as well that is
               needed for creating the snapshot.
        """
        raise NotImplementedError()

    def volume_snapshot_delete(self, context, instance, volume_id,
                               snapshot_id, delete_info):
        """Snapshots volumes attached to a specified instance.

        :param context: request context
        :param instance: nova.objects.instance.Instance that has the volume
               attached
        :param volume_id: Attached volume associated with the snapshot
        :param snapshot_id: The snapshot to delete.
        :param delete_info: Volume backend technology specific data needed to
               be able to complete the snapshot.  For example, in the case of
               qcow2 backed snapshots, this would include the file being
               merged, and the file being merged into (if appropriate).
        """
        raise NotImplementedError()

    def default_root_device_name(self, instance, image_meta, root_bdm):
        """Provide a default root device name for the driver."""
        raise NotImplementedError()

    def default_device_names_for_instance(self, instance, root_device_name,
                                          *block_device_lists):
        """Default the missing device names in the block device mapping."""
        raise NotImplementedError()

    def is_supported_fs_format(self, fs_type):
        """Check whether the file format is supported by this driver

        :param fs_type: the file system type to be checked,
                        the validate values are defined at disk API module.
        """
        # NOTE(jichenjc): Return False here so that every hypervisor
        #                 need to define their supported file system
        #                 type and implement this function at their
        #                 virt layer.
        return False
