# -*- mode: python; python-indent: 4 -*-
import ncs
import ncs.maapi
from ncs.application import Service, PlanComponent
from ncs.dp import Action
import _ncs.dp
import requests 
import traceback
from time import sleep
import collections
import netaddr
import _ncs

#TODO Handle VNF recovery scenario
#TODO Investigate reactive-redeploy on error condition from NFVO
#TODO API check script needs to be split or adding/deleting to 
# to the actions of the rule needs to be investigated so
# that there is not an immeadiate recovering when the API 
# check fails immeadiately but recovery is supported in future

day0_authgroup = "ftd_day0"
default_timeout = 600
ftd_api_port = 443

class ScalableService(Service):

    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info('')
        self.log.info('**** Service create(service=', service._path, ') ****')
        # This data should be valid based on the model
        site = service._parent._parent
        vnf_catalog = root.vnf_manager.vnf_catalog[service.catalog_vnf]
        vnf_deployment_name = service.tenant+'-'+service.deployment_name
        vnf_authgroup = vnf_catalog.authgroup
        try:
            vnf_day0_authgroup = root.devices.authgroups.group[day0_authgroup]
            vnf_day0_username = vnf_day0_authgroup.default_map.remote_name
            vnf_day0_password = _ncs.decrypt(vnf_day0_authgroup.default_map.remote_password)
            vnf_day1_authgroup = root.devices.authgroups.group[vnf_catalog.authgroup]
            vnf_day1_username = vnf_day1_authgroup.default_map.remote_name
            vnf_day1_password = _ncs.decrypt(vnf_day1_authgroup.default_map.remote_password)
        except Exception as e:
            self.log.error('VNF user/password initialization failed: {}'.format(e))
            self.log.error(traceback.format_exc())
            self.addPlanFailure(planinfo, 'service', 'init')
            service.status_message = 'VNF user/password initialization failed: {}'.format(e)
            return

        # This is internal service data that is persistant between reactive-re-deploy's
        proplistdict = dict(proplist)
        self.log.info('Service Properties: ', proplistdict)
        # These are for presenting the status and timings of the service deployment
        #  Even if there is a failure or exit early, this data will be written to
        #  the service's operational model
        planinfo = {}
        planinfo['devices'] = {}
        planinfo['failure'] = {}
        planinfo_devices = planinfo['devices']

        # Initialize variables for this service deployment run
        nfvo_deployment_status = None

        # Every time the service is re-run it starts with a network model just
        # as it was the very first time, this means that any changes that where made
        # in a previous run that need to be preserved must be run again.
        # NSO will detect that we are updating something to the same thing and
        # ignore when when it commits at the end of the service run, but if something
        # is not repeated, it will be considered deleted and NSO will attempt
        # to delete from the model, with all that that implies
        try:
            self.log.info('Site Name: ', site.name)
            self.log.info('Tenant Name: ', service.tenant)
            self.log.info('Deployment Name: ', service.deployment_name)
            # Do initial validation checks here
            if root.devices.authgroups.group[vnf_authgroup] is None or \
              root.devices.authgroups.group[vnf_authgroup].default_map.remote_name is None:
                self.addPlanFailure(planinfo, 'service', 'init')
                raise Exception('Remote Name in Default Map or authgroup {} not configure'.format(vnf_authgroup))

            # First step is to make sure that the sites ip address pools are instantiated
            planinfo['ip-addressing'] = 'NOT COMPLETED'
            for network in service.scaling.networks.network:
                site_network = site.networks.network[network.name]
                site_network.resource_pool.name = "{}_{}".format(site.name, network.name)
                output = site_network.initialize_ip_address_pool()
                if 'Error' in output.result:
                    raise Exception(output.result)
            # Calculate the subnet size
            max_vnf_count = root.nfvo.vnfd[vnf_catalog.descriptor_name] \
                            .deployment_flavor[vnf_catalog.descriptor_flavor] \
                            .vdu_profile[vnf_catalog.descriptor_vdu] \
                            .max_number_of_instances
            # Allocate IP Addresses
            for network in service.scaling.networks.network:
                site_network = site.networks.network[network.name]
                network.resource_pool_allocation.name = "{}_{}_{}".format(service.tenant, service.deployment_name,
                                                                          network.name)
                inputs = network.resource_pool_allocation.allocate_ip_addresses.get_input()
                inputs.network_keypath = site_network._path
                inputs.allocating_service = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}']"
                                             "[deployment-name='{}']").format(site.name, service.tenant, service.deployment_name)
                inputs.address_count = max_vnf_count
                output = network.resource_pool_allocation.allocate_ip_addresses(inputs)
                if 'Error' in output.result:
                    raise Exception(output.result)
                self.log.info("Network Initialized: ", network.name)
            # Check and see if the ip pools have been instantiated
            ip_addresses_initialized = True
            failure = False
            with ncs.maapi.single_read_trans(tctx.uinfo.username, 'itd',
                                      db=ncs.OPERATIONAL) as trans:
                op_root = ncs.maagic.get_root(trans)
                for network in service.scaling.networks.network:
                    try:
                        site_network = site.networks.network[network.name]
                        inputs = network.resource_pool_allocation.check_ready.get_input()
                        inputs.network_keypath = site_network._path
                        output = network.resource_pool_allocation.check_ready(inputs)
                        if 'Not Allocated' in output.result:
                            ip_addresses_initialized = False
                    except Exception as e:
                        failure = True
            if failure:
                self.log.error('Network ip pools initialization failed: {}'.format(e))
                self.log.error(traceback.format_exc())
                planinfo['ip-addressing'] = 'FAILURE'
                service.status_message = 'IP addressing failed, please check that ip resource pools are not exhausted'
            elif not ip_addresses_initialized:
                # There are pools that need to be configured
                self.log.info('Network ip pools are being configured, wait for resource manager to call back')
            else:
                planinfo['ip-addressing'] = 'COMPLETED'

            # VNF Deployment with Scale Monitors not configured
            planinfo['vnfs-deployed'] = 'NOT COMPLETED'
            try:
                if self.service_status_good(planinfo) and planinfo['ip-addressing'] == 'COMPLETED':
                    vars = ncs.template.Variables()
                    vars.add('SITE-NAME', service._parent._parent.name);
                    vars.add('DEPLOYMENT-TENANT', service.tenant);
                    vars.add('DEPLOYMENT-NAME', service.deployment_name);
                    vars.add('DEPLOY-PASSWORD', vnf_day0_password); # admin password to set when deploy
                    vars.add('MONITORS-ENABLED', 'true');
                    vars.add('MONITOR-USERNAME', vnf_day1_username);
                    vars.add('MONITOR-PASSWORD', vnf_day1_password);
                    vars.add('IMAGE-NAME', root.nfvo.vnfd[vnf_catalog.descriptor_name]
                                            .vdu[vnf_catalog.descriptor_vdu]
                                            .software_image_descriptor.image);
                    # Set the context of the template to /vnf-manager
                    template = ncs.template.Template(service._parent._parent._parent._parent)
                    template.apply('vnf-deployment', vars)
            except Exception as e:
                self.log.error(e)
                self.log.error(traceback.format_exc())
                self.addPlanFailure(planinfo, 'service', 'vnfs-deployed')

            # Initialize plug-ins
            # Load balancer
            if self.service_status_good(planinfo):
                planinfo['load-balancing-configured'] = 'DISABLED'
                for loadbalancer in service.scaling.load_balance:
                    if str(loadbalancer) != 'ftdv-ngfw:load-balancer':
                        try:
                            service.scaling.load_balance.__getitem__(loadbalancer).initialize()
                            planinfo['load-balancing-configured'] = 'INITIALIZED'
                            service.scaling.load_balance.status = 'Initialized'
                            break
                        except Exception as e:
                            self.log.error(e)
                            self.log.error(traceback.format_exc())
                            self.addPlanFailure(planinfo, 'service', 'load-balancing-configured')
                            service.scaling.load_balance.status == 'Failed'
                            service.status.message = "{} Plug-in failed to initialize".format(loadbalancer)
                if planinfo['load-balancing-configured'] == 'DISABLED':
                    service.scaling.load_balance.status = 'Disabled'
   
            # Gather current state of service here
            with ncs.maapi.single_write_trans(tctx.uinfo.username, 'itd',
                                      db=ncs.OPERATIONAL) as trans:
                try:
                    op_root = ncs.maagic.get_root(trans)
                    nfvo_deployment_status = op_root.nfvo.vnf_info.nfvo_rel2_esc__esc \
                                             .vnf_deployment_result[service.tenant, \
                                                                    service.deployment_name, \
                                                                    site.elastic_services_controller] \
                                             .status.cstatus
                    if not proplistdict:
                        try: 
                            vnf_info = root.nfvo.vnf_info.esc \
                                       .vnf_deployment[service.tenant, service.deployment_name, 
                                                       site.elastic-services-controller]
                        except Exception:
                            # There should not be any data for the deployment result, clean up
                            self.log.info('Reseting the NFVO Deployment Result')
                            del op_root.nfvo.vnf_info.nfvo_rel2_esc__esc \
                                .vnf_deployment_result[service.tenant, service.deployment_name, 
                                                       site.elastic_services_controller]
                            if [service.tenant, service.deployment_name, site.elastic_services_controller] \
                             in op_root.nfvo.vnf_info.nfvo_rel2_esc__esc.vnf_deployment_result:
                                self.log.error("NFVO vnf deployment result {} {} {} failed"
                                               .format(service.tenant, service.deployment_name, 
                                                       site.elastic_services_controller))
                            nfvo_deployment_status = None
                    self.log.info("NFVO Deployment Status: ", nfvo_deployment_status)
                except KeyError:
                    # nfvo_deployment_status will not exist the first pass through the service logic
                    pass
            if nfvo_deployment_status is None:
                 # Service has just been called, have not committed NFVO information yet
                self.log.info('Initial Service Call - wait for NFVO to report back')

            vm_count = None
            new_vm_count = None
            # VNF deployment exists in NFVO, collect additional information
            if [service.tenant, service.deployment_name, site.elastic_services_controller] in \
              root.nfvo.vnf_info.esc.vnf_deployment_result:
                vm_devices = None
                try:
                    vm_devices = root.nfvo.vnf_info.esc.vnf_deployment_result[service.tenant, \
                                 service.deployment_name, site.elastic_services_controller] \
                                .vdu[service.deployment_name, vnf_catalog.descriptor_vdu] \
                                .vm_device
                except KeyError as e:
                    pass
                # This is the number of devices that the service has provisioned possibly from a 
                #  previous re-deploy, initialize if neccessary if this is the first time the service
                #  has been called
                vm_count = int(proplistdict.get('ProvisionedVMCount', 0)) 
                new_vm_count = 0
                if vm_devices is not None: # This will happen during the ip-addressing stage
                    new_vm_count = len(vm_devices) # This is the number of devices that NFVO reports it is aware of
                self.log.info('Current VM Count: '+str(vm_count), ' New VM Count: '+str(new_vm_count))
                # Reset the device tracking
                # Device goes through Not Provisioned -> Not Registered -> Provisioned -> Not Registered -> Provisioned...
                # 'Not Provisioned' devices still have to be initially provisioned, all others will still need
                # to be registered
                if vm_devices is not None:
                    for nfvo_device in vm_devices:
                        # Initialize the plan status information for the device
                        planinfo_devices[nfvo_device.device_name] = {}
                        # Keep track of Device's and the IP addresses in the service operational model
                        self.log.info('Creating Device: ', nfvo_device.device_name)
                        service_device = service.device.create(nfvo_device.device_name)
                        service_device.vm_name = nfvo_device.vmname
                        service_device.vmid = nfvo_device.vmid
                        esc_device = root.devices.device[site.elastic_services_controller].live_status \
                                     .esc__esc_datamodel.opdata.tenants.tenant[service.tenant] \
                                     .deployments[service.deployment_name] \
                                     .vm_group[service.deployment_name+'-'+vnf_catalog \
                                     .descriptor_vdu].vm_instance[nfvo_device.vmid]
                        # If the NFVO deployment is 'ready' the device's IP address assigned by ESC
                        #  from the pool will be available
                        service_device.management_ip_address = esc_device.interfaces.interface['1'].ip_address
                        service_device.inside_ip_address = esc_device.interfaces.interface['3'].ip_address
                        service_device.outside_ip_address = esc_device.interfaces.interface['4'].ip_address
                        service_device.status = 'Starting'
                    # Reset all persistant device service data so that we are sure to register all
                    #  provisioned and and not yet provisioned devices every re-deploy run
                    # Remove devices that are no longer in NFVO as the have been removed
                    for dev_name in [ k[8:] for k in proplistdict.keys() if k.startswith('DEVICE: ') and k[8:] not in [ d.device_name for d in vm_devices]]:
                        del proplistdict[str('DEVICE: '+dev_name)]
                        if service.device.exists(dev_name):
                            service.device.delete(dev_name)
                    # When a device is removed, it first goes back through the deployed phase
                    # Reset out status for those devices so that we do not try to sync-from them
                    for dev in vm_devices:
                        if dev.status.cstatus == 'deployed':
                            planinfo_devices[dev.device_name]['deployed'] = 'COMPLETED'
                        if dev.status.cstatus == 'ready':
                            planinfo_devices[dev.device_name]['deployed'] = 'COMPLETED'
                            planinfo_devices[dev.device_name]['api-available'] = 'COMPLETED'
                    # Add any new devices NFVO has added
                    for dev_name in [ d.device_name for d in vm_devices if d.device_name not in [ k[8:] for k in proplistdict.keys() if k.startswith('DEVICE: ')]]:
                        self.log.info("Adding Device ({}) to Service Properties List".format(dev_name))
                        proplistdict[str('DEVICE: '+dev_name)] = 'Starting'
            self.log.info('==== Service Reactive-Redeploy Properties ====')
            od = collections.OrderedDict(sorted(proplistdict.items()))
            for k, v in od.iteritems(): self.log.info(k, ' ', v)
            for device in service.device:
                self.log.info(device.name, ': ', device.management_ip_address)
            self.log.info('==============================================')

            if nfvo_deployment_status == 'deployed':
                # Service VNFs are deployed or cloned or copied but have not completed booting and are 
                #  not ready
                self.log.info('VNFs\' APIs are NOT not available - wait for NFVO to report back')
                planinfo['vnfs-deployed'] = 'COMPLETED'
            elif nfvo_deployment_status == 'ready':
                # The API metric collector on ESC has reported to NFVO that the API's are reachable
                self.log.info('VNFs\' APIs are available')
                planinfo['vnfs-deployed'] = 'COMPLETED'
                planinfo['vnfs-api-available'] = 'COMPLETED'
            elif nfvo_deployment_status == 'failed':
                self.log.info('!! Service failure condition encountered !!')
                self.log.info('Error: ' + nfvo_deployment_status.error)
                raise Exception('Error: ' + nfvo_deployment_status.error)
                return
            elif nfvo_deployment_status == 'recovering':
                raise Exception('VNF Recovering - This is not supported')
            elif nfvo_deployment_status == 'error':
                if nfvo_deployment_status == 'error':
                    self.addPlanFailure(planinfo, 'service', 'vnfs-deployed')
                    with ncs.maapi.single_read_trans(tctx.uinfo.username, 'itd') as t:
                        service.status_message = str(t.get_elem("/nfvo/vnf-info/nfvo-rel2-esc:esc/" +
                                                    "vnf-deployment-result{{{} {} {}}}/status/error".format(
                                                    service.tenant, service.deployment_name, 
                                                    site.elastic_services_controller)))
                    raise Exception('VNF Error Condition from NFVO reported: ', service.status_message)

            # Register devices with NSO
            failure = False
            all_vnfs_registered = True
            for device in service.device:
                try:
                    # Device is registered by kicker call action
                    if device.name in root.devices.device:
                        self.log.info('Device Registered: '+device.name)
                        device.status = 'Registered'
                        planinfo_devices[device.name]['registered-with-nso'] = 'COMPLETED'
                        if proplistdict[str('DEVICE: '+device.name)] not in ('Provisioned', 'Configurable'):
                            proplistdict[str('DEVICE: '+device.name)] = 'Registered'
                    else:
                        planinfo_devices[device.name]['registered-with-nso'] = 'NOT COMPLETED'
                        all_vnfs_registered = False
                        self.log.info('Device NOT Registered: '+device.name)
                except Exception as e:
                    self.log.error(e)
                    failure = True
                    self.addPlanFailure(planinfo, device.name, 'registered-with-nso')
                    self.addPlanFailure(planinfo, 'service', 'vnfs-registered-with-nso')
            if new_vm_count is not None and new_vm_count !=0 and not failure and all_vnfs_registered:
                planinfo['vnfs-registered-with-nso'] = 'COMPLETED'

            # Do initial provisioning of each device
            failure = False
            proplistdict['ProvisionedVMCount'] = "0"
            all_vnfs_provisioned = True
            for device in service.device:
                try:
                    if proplistdict[str('DEVICE: '+device.name)] in ('Provisioned', 'Configurable'):
                        planinfo_devices[device.name]['initialized'] = 'COMPLETED'
                        proplistdict[str('DEVICE: '+device.name)] = 'Provisioned'
                        device.status = 'Provisioned'
                        dev = root.devices.device[device.name]
                        dev.authgroup = vnf_day1_authgroup.name
                        self.log.info('Device Provisioned: '+device.name)
                    elif proplistdict[str('DEVICE: '+device.name)] == 'Registered':
                        self.log.info('Provisioning Device: '+device.name)
                        device.status = 'Provisioning'
                        dev = root.devices.device[device.name]
                        input = dev.config.cisco_ftd__ftd.actions.provision.get_input()
                        input.acceptEULA = True
                        input.currentPassword = vnf_day0_password
                        input.newPassword = vnf_day1_password
                        output = dev.config.cisco_ftd__ftd.actions.provision(input)
                        dev.authgroup = vnf_day1_authgroup.name
                        planinfo_devices[device.name]['initialized'] = 'COMPLETED'
                        proplistdict[str('DEVICE: '+device.name)] = 'Provisioned'
                        device.status = 'Provisioned'
                        self.log.info('Device Provisioned: '+device.name)
                    else:
                        all_vnfs_provisioned = False
                        self.log.info('Device NOT Provisioned (Device not registered): '+device.name)
                except Exception as e:
                    failure = True
                    self.log.error(e)
                    self.log.error(traceback.format_exc())
                    self.addPlanFailure(planinfo, device.name, 'initialized')
                    self.addPlanFailure(planinfo, 'service', 'vnfs-initialized')
            if new_vm_count is not None and new_vm_count !=0 and not failure and all_vnfs_provisioned:
                planinfo['vnfs-initialized'] = 'COMPLETED'

            failure = False
            all_vnfs_synced = True
            planinfo['vnfs-synchronized-with-nso'] = 'NOT COMPLETED'
            for device in service.device:
                try:
                    with ncs.maapi.single_read_trans(tctx.uinfo.username, 'itd',
                                                     db=ncs.RUNNING) as trans:
                        run_root = ncs.maagic.get_root(trans)
                        try:
                            # NED does not support check-syc
                            # if run_root.devices.device[device.name].check_sync() == 'in-sync':
                            planinfo_devices[device.name]['synchronized-with-nso'] = 'NOT COMPLETED'
                            planinfo_devices[device.name]['configurable'] = 'NOT COMPLETED'
                            if run_root.devices.device[device.name].config.ftd.license.smartagentconnections.connectionType is not None:
                                self.log.info('Device Synced: ', device.name)
                                planinfo_devices[device.name]['synchronized-with-nso'] = 'COMPLETED'
                                planinfo_devices[device.name]['configurable'] = 'COMPLETED'
                                proplistdict[str('DEVICE: '+device.name)] = 'Configurable'
                                device.status = 'Configurable'
                                proplistdict['ProvisionedVMCount'] = str(int(proplistdict['ProvisionedVMCount']) + 1)
                            else:
                                self.log.info('Device NOT synced: ', device.name)
                                all_vnfs_synced = False
                        except KeyError as error:
                            self.log.info('Device NOT synced (Device not registered): ', device.name)
                            all_vnfs_synced = False
                except Exception as e:
                    self.log.error(e)
                    self.log.error(traceback.format_exc())
                    failure = True
                    self.addPlanFailure(planinfo, device.name, 'synchronized-with-nso')
                    self.addPlanFailure(planinfo, 'service', 'vnfs-synchronized-with-nso')
            if new_vm_count is not None and new_vm_count !=0 and not failure and all_vnfs_synced:
                planinfo['vnfs-synchronized-with-nso'] = 'COMPLETED'

            if planinfo['load-balancing-configured'] == 'INITIALIZED' and \
               planinfo['vnfs-synchronized-with-nso'] == 'COMPLETED':
                for loadbalancer in service.scaling.load_balance:
                    if str(loadbalancer) != 'ftdv-ngfw:load-balancer':
                        try:
                            service.scaling.load_balance.__getitem__(loadbalancer).deploy()
                            service.scaling.load_balance.status == 'Enabled'
                            planinfo['load-balancing-configured'] = 'COMPLETED'
                            self.log.info("Load Balancing Configured")
                            break
                        except Exception as e:
                            self.log.error(e)
                            self.log.error(traceback.format_exc())
                            self.addPlanFailure(planinfo, 'service', 'load-balancing-configured')
                            service.scaling.load_balance.status == 'Failed'
                            service.status.message = "{} Plug-in failed to deploy".format(loadbalancer)
            elif planinfo['load-balancing-configured'] == 'DISABLED':
                self.log.info("Load Balancing Not Used")
                service.scaling.load_balance.status == 'Disabled'
            else:
                self.log.info("Load Balancing Not Configured")

            # Add scaling monitoring when VNFs are provisioned or anytime after Monitoring
            # is initially turned on
            if proplistdict.get('Monitored', 'False') == 'True' or int(proplistdict.get('ProvisionedVMCount', 0)) > 0:
                # Turn monitoring back on
                vars = ncs.template.Variables()
                vars.add('SITE-NAME', service._parent._parent.name);
                vars.add('DEPLOYMENT-TENANT', service.tenant);
                vars.add('DEPLOYMENT-NAME', service.deployment_name);
                vars.add('DEPLOY-PASSWORD', vnf_day0_password); # admin password to set when deploy
                vars.add('MONITORS-ENABLED', 'true');
                vars.add('MONITOR-USERNAME', vnf_day1_username);
                vars.add('MONITOR-PASSWORD', vnf_day1_password);
                vars.add('IMAGE-NAME', root.nfvo.vnfd[vnf_catalog.descriptor_name].vdu[
                                        vnf_catalog.descriptor_vdu].software_image_descriptor.image);
                # Set the context of the template to /vnf-manager
                template = ncs.template.Template(service._parent._parent._parent._parent)
                template.apply('vnf-deployment-monitoring', vars)
                proplistdict['Monitored'] = 'True'
                planinfo['scaling-monitoring-enabled'] = 'COMPLETED'
                self.log.info('VNF load monitoring Enabled')
            for device in service.device:
                #device.status = "Provisioning"
                if planinfo_devices[device.name]['registered-with-nso'] != 'COMPLETED':
                    # Apply kicker to do device registration, this should be applied after status of nfvo changes to deploying
                    self.applyRegisterDeviceKicker(root, self.log, service.deployment_name, site.name, service.tenant, service.deployment_name,
                                                   site.elastic_services_controller, vnf_catalog.descriptor_vdu, device.name)
                if planinfo_devices[device.name]['synchronized-with-nso'] != 'COMPLETED':
                    self.applySyncDeviceKicker(root, self.log, service.deployment_name, site.name, service.tenant, service.deployment_name,
                                               site.elastic_services_controller,  device.name)
                if planinfo_devices[device.name]['registered-with-nso'] == 'COMPLETED' and \
                  planinfo_devices[device.name]['synchronized-with-nso'] != 'COMPLETED':
                    # Apply kicker to rerun service once a devices configuration shows up after synchronization
                    self.applyDeviceSyncedKicker(root, self.log, service.deployment_name, site.name, service.tenant, service.deployment_name,
                                                 site.elastic_services_controller, device.name)
                if device.status == 'Configurable':
                    # This is temporary
                    device.get_device_data()
            # Apply kicker to monitor for nfvo scaling and recovery events
            self.applyServiceKicker(root, self.log, service.deployment_name, site.name, service.tenant,
                                    service.deployment_name, site.elastic_services_controller)
        except Exception as e:
            self.log.error("Exception Here:")
            self.log.info(e)
            self.log.info(traceback.format_exc())
            service.status = 'Failure'
        finally:
            proplist = [(k,v) for k,v in proplistdict.iteritems()]
            self.log.info('Service Properties: ', str(proplist))
            self.write_plan_data(service, planinfo)
            self.log.info('Service status will be set to: ', service.status)
            self.log.info('Service message will be set to: ', service.status_message)
            return proplist

    def service_status_good(self, planinfo):
        self.log.info('Checking service status with: '+str(planinfo))
        if len(planinfo['failure']) == 0:
            self.log.info('Service Status GOOD: ', len(planinfo['failure']))
            return True
        else:
            self.log.info('Service Status BAD: ', len(planinfo['failure']))
            return False

    def addPlanFailure(self, planinfo, component, step):
        fail = planinfo['failure'].get(component, list())
        fail.append(step)
        planinfo['failure'][component] = fail

    def write_plan_data(self, service, planinfo):
        self.log.info('Plan Data: ', planinfo)
        self_plan = PlanComponent(service, 'vnf-deployment_'+service.deployment_name, 'ncs:self')
        self_plan.append_state('ncs:init')
        self_plan.append_state('ftdv-ngfw:ip-addressing')
        self_plan.append_state('ftdv-ngfw:vnfs-deployed')
        self_plan.append_state('ftdv-ngfw:vnfs-api-available')
        self_plan.append_state('ftdv-ngfw:vnfs-registered-with-nso')
        self_plan.append_state('ftdv-ngfw:vnfs-initialized')
        self_plan.append_state('ftdv-ngfw:vnfs-synchronized-with-nso')
        self_plan.append_state('ftdv-ngfw:scaling-monitoring-enabled')
        if planinfo.get('load-balancing-configured', '') != 'DISABLED':
            self_plan.append_state('ftdv-ngfw:load-balancing-configured')
        self_plan.append_state('ncs:ready')
        self_plan.set_reached('ncs:init')

        if planinfo['failure'].get('service', None) is not None:
            if 'init' in planinfo['failure']['service']:
                self_plan.set_failed('ncs:init')
                service.status = 'Failure'
                return

        service.status = 'Initializing'
        if planinfo.get('ip-addressing', '') == 'COMPLETED':
            self_plan.set_reached('ftdv-ngfw:ip-addressing')
            service.status = 'Deploying'
        if planinfo.get('vnfs-deployed', '') == 'COMPLETED':
            self_plan.set_reached('ftdv-ngfw:vnfs-deployed')
            service.status = 'Starting VNFs'
        if planinfo.get('load-balancing-configured', '') == 'COMPLETED':
            self_plan.set_reached('ftdv-ngfw:load-balancing-configured')
        if planinfo.get('vnfs-api-available', '') == 'COMPLETED':
            self_plan.set_reached('ftdv-ngfw:vnfs-api-available')
            service.status = 'Provisioning'
        if planinfo.get('vnfs-initialized', '') == 'COMPLETED':
            self_plan.set_reached('ftdv-ngfw:vnfs-initialized')
            service.status = 'Provisioned'
        if planinfo.get('vnfs-registered-with-nso', '') == 'COMPLETED':
            self_plan.set_reached('ftdv-ngfw:vnfs-registered-with-nso')
            service.status = 'Synchronizing'
        if planinfo.get('vnfs-synchronized-with-nso', '') == 'COMPLETED':
            self_plan.set_reached('ftdv-ngfw:vnfs-synchronized-with-nso')
            service.status = 'Configurable'
        if planinfo.get('scaling-monitoring-enabled', '') == 'COMPLETED':
            self_plan.set_reached('ftdv-ngfw:scaling-monitoring-enabled')
            if planinfo['failure'].get('service', None) is None:
                self_plan.set_reached('ncs:ready')
        if planinfo['failure'].get('service', None) is not None:
            for failure in planinfo['failure']['service']:
                self.log.info('setting service failure ', 'ftdv-ngfw:'+failure)
                self_plan.set_failed('ftdv-ngfw:'+failure)
                service.status = 'Failure'

        for device in planinfo['devices']:
            self.log.info(("Creating plan for device: {}").format(device))
            device_states = planinfo['devices'][device]
            device_plan = PlanComponent(service, device, 'ftdv-ngfw:vnf')
            device_plan.append_state('ncs:init')
            device_plan.append_state('ftdv-ngfw:deployed')
            device_plan.append_state('ftdv-ngfw:registered-with-nso')
            device_plan.append_state('ftdv-ngfw:api-available')
            device_plan.append_state('ftdv-ngfw:initialized')
            device_plan.append_state('ftdv-ngfw:synchronized-with-nso')
            device_plan.append_state('ftdv-ngfw:configurable')
            device_plan.append_state('ncs:ready')
            device_plan.set_reached('ncs:init')

            service.device[device].status = 'Deploying'
            if device_states.get('deployed', '') == 'COMPLETED':
                device_plan.set_reached('ftdv-ngfw:deployed')
                service.device[device].status= 'Starting'
            if device_states.get('registered-with-nso', '') == 'COMPLETED':
                device_plan.set_reached('ftdv-ngfw:registered-with-nso')
            if device_states.get('api-available', '') == 'COMPLETED':
                device_plan.set_reached('ftdv-ngfw:api-available')
                service.device[device].status = 'Provisioning'
            if device_states.get('initialized', '') == 'COMPLETED':
                device_plan.set_reached('ftdv-ngfw:initialized')
                service.device[device].status = 'Provisioned'
            if device_states.get('synchronized-with-nso', '') == 'COMPLETED':
                device_plan.set_reached('ftdv-ngfw:synchronized-with-nso')
                service.device[device].status = 'Synchronized'
            if device_states.get('configurable', '') == 'COMPLETED':
                device_plan.set_reached('ftdv-ngfw:configurable')
                service.device[device].status = 'Configurable'
                if planinfo['failure'].get(device, None) is None:
                    device_plan.set_reached('ncs:ready')

            if planinfo['failure'].get(device, None) is not None:
                for failure in planinfo['failure'][device]:
                    self.log.info('setting ',device,' failure ', 'ftdv-ngfw:'+failure)
                    device_plan.set_failed('ftdv-ngfw:'+failure)
                    service.device[device].status = 'Failure'

    def applyRegisterDeviceKicker(self, root, log, vnf_deployment_name, site_name, tenant, service_deployment_name,
                                  esc_device_name, vdu, device_name):
        kick_monitor_node = ("/nfvo/vnf-info/nfvo-rel2-esc:esc" 
                             "/vnf-deployment[tenant='{}'][deployment-name='{}'][esc='{}']" 
                             "/plan/component[name='{}-{}']/state[name='ncs:ready']").format(
                             tenant, service_deployment_name, esc_device_name, vnf_deployment_name, vdu)
        kick_node = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']/device[name='{}']").format(
                     site_name, tenant, service_deployment_name, device_name)
        trigger_expr = "status='reached'"
        self.applyKicker(root, log, vnf_deployment_name, site_name, tenant, service_deployment_name,
                         esc_device_name, 'register-vnf-with-nso', int(device_name[-1])+10, kick_monitor_node, kick_node, 'nfvoDeviceReady', None, device_name)

    def applySyncDeviceKicker(self, root, log, vnf_deployment_name, site_name, tenant, service_deployment_name,
                              esc_device_name, device_name):
        kick_monitor_node = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']/device[name='{}']").format(
                              site_name, tenant, service_deployment_name, device_name)
        trigger_expr = "status='Provisioned'"
        kick_node = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']/device[name='{}']").format(
                     site_name, tenant, service_deployment_name, device_name)
        self.applyKicker(root, log, vnf_deployment_name, site_name, tenant, service_deployment_name,
                         esc_device_name, 'sync-vnf-with-nso', int(device_name[-1])+20, kick_monitor_node, kick_node, 'vnfdeviceProvisioned', trigger_expr, device_name)

    def applyDeviceSyncedKicker(self, root, log, vnf_deployment_name, site_name, tenant, service_deployment_name,
                    esc_device_name, device_name):
        kick_monitor_node = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']/device[name='{}']").format(
                              site_name, tenant, service_deployment_name, device_name)
        trigger_expr = "status='Synchronized'"
        kick_node = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']").format(
                      site_name, tenant, service_deployment_name)
        self.applyKicker(root, log, vnf_deployment_name, site_name, tenant, service_deployment_name,
                         esc_device_name, 'reactive-re-deploy', int(device_name[-1])+30, kick_monitor_node, kick_node, 'vnfdeviceSynced', trigger_expr)

    def applyServiceKicker(self, root, log, vnf_deployment_name, site_name, tenant, service_deployment_name,
                           esc_device_name):
        kick_node = ("/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']").format(
                      site_name, tenant, service_deployment_name)
        kick_monitor_node = ("/nfvo/vnf-info/nfvo-rel2-esc:esc" 
                             "/vnf-deployment[tenant='{}'][deployment-name='{}'][esc='{}']" 
                             "/plan/component[name='self']/state[name='ncs:ready']").format(
                              tenant, service_deployment_name, esc_device_name)
        trigger_expr = "status='reached'"
        self.applyKicker(root, log, vnf_deployment_name, site_name, tenant, service_deployment_name,
                         esc_device_name, 'reactive-re-deploy', 1, kick_monitor_node, kick_node, 'nfvoReady')

    def applyKicker(self, root, log, vnf_deployment_name, site_name, tenant, service_deployment_name, 
                    esc_device_name, action_name, priority, kick_monitor_node, kick_node, monitor, trigger_expr=None, device_name=''):
        log.info('Creating Kicker Monitor on: ', action_name, ' ', kick_monitor_node, ' for ', kick_node, ' when ', trigger_expr)
        kicker = root.kickers.data_kicker.create('ftdv_ngfw-{}-{}-{}-{}-{}'.format(monitor, tenant, vnf_deployment_name, device_name, action_name))
        kicker.monitor = kick_monitor_node
        if trigger_expr is not None:
            kicker.trigger_expr = trigger_expr
        kicker.kick_node = kick_node
        kicker.action_name = action_name
        kicker.priority = priority
        kicker.trigger_type = 'enter'

    def provisionFTD(self, ip_address, username, current_password, new_password):
        self.log.info(" Device Provisining Started")
        URL = '/devices/default/action/provision'
        payload = { "acceptEULA": True,
                    "eulaText": "End User License Agreement\n\nEffective: May 22, 2017\n\nThis is an agreement between You and Cisco Systems, Inc. or its affiliates\n(\"Cisco\") and governs your Use of Cisco Software. \"You\" and \"Your\" means the\nindividual or legal entity licensing the Software under this EULA. \"Use\" or\n\"Using\" means to download, install, activate, access or otherwise use the\nSoftware. \"Software\" means the Cisco computer programs and any Upgrades made\navailable to You by an Approved Source and licensed to You by Cisco.\n\"Documentation\" is the Cisco user or technical manuals, training materials,\nspecifications or other documentation applicable to the Software and made\navailable to You by an Approved Source. \"Approved Source\" means (i) Cisco or\n(ii) the Cisco authorized reseller, distributor or systems integrator from whom\nyou acquired the Software. \"Entitlement\" means the license detail; including\nlicense metric, duration, and quantity provided in a product ID (PID) published\non Cisco's price list, claim certificate or right to use notification.\n\"Upgrades\" means all updates, upgrades, bug fixes, error corrections,\nenhancements and other modifications to the Software and backup copies thereof.\n\nThis agreement, any supplemental license terms and any specific product terms\nat www.cisco.com/go/softwareterms (collectively, the \"EULA\") govern Your Use of\nthe Software.\n\n1. Acceptance of Terms. By Using the Software, You agree to be bound by the\nterms of the EULA. If you are entering into this EULA on behalf of an entity,\nyou represent that you have authority to bind that entity. If you do not have\nsuch authority or you do not agree to the terms of the EULA, neither you nor\nthe entity may Use the Software and it may be returned to the Approved Source\nfor a refund within thirty (30) days of the date you acquired the Software or\nCisco product. Your right to return and refund applies only if you are the\noriginal end user licensee of the Software.\n\n2. License. Subject to payment of the applicable fees and compliance with this\nEULA, Cisco grants You a limited, non-exclusive and non-transferable license to\nUse object code versions of the Software and the Documentation solely for Your\ninternal operations and in accordance with the Entitlement and the\nDocumentation. Cisco licenses You the right to Use only the Software You\nacquire from an Approved Source. Unless contrary to applicable law, You are not\nlicensed to Use the Software on secondhand or refurbished Cisco equipment not\nauthorized by Cisco, or on Cisco equipment not purchased through an Approved\nSource. In the event that Cisco requires You to register as an end user, Your\nlicense is valid only if the registration is complete and accurate. The\nSoftware may contain open source software, subject to separate license terms\nmade available with the Cisco Software or Documentation.\n\nIf the Software is licensed for a specified term, Your license is valid solely\nfor the applicable term in the Entitlement. Your right to Use the Software\nbegins on the date the Software is made available for download or installation\nand continues until the end of the specified term, unless otherwise terminated\nin accordance with this Agreement.\n\n3. Evaluation License. If You license the Software or receive Cisco product(s)\nfor evaluation purposes or other limited, temporary use as authorized by Cisco\n(\"Evaluation Product\"), Your Use of the Evaluation Product is only permitted\nfor the period limited by the license key or otherwise stated by Cisco in\nwriting. If no evaluation period is identified by the license key or in\nwriting, then the evaluation license is valid for thirty (30) days from the\ndate the Software or Cisco product is made available to You. You will be\ninvoiced for the list price of the Evaluation Product if You fail to return or\nstop Using it by the end of the evaluation period. The Evaluation Product is\nlicensed \"AS-IS\" without support or warranty of any kind, expressed or implied.\nCisco does not assume any liability arising from any use of the Evaluation\nProduct. You may not publish any results of benchmark tests run on the\nEvaluation Product without first obtaining written approval from Cisco. You\nauthorize Cisco to use any feedback or ideas You provide Cisco in connection\nwith Your Use of the Evaluation Product.\n\n4. Ownership. Cisco or its licensors retain ownership of all intellectual\nproperty rights in and to the Software, including copies, improvements,\nenhancements, derivative works and modifications thereof. Your rights to Use\nthe Software are limited to those expressly granted by this EULA. No other\nrights with respect to the Software or any related intellectual property rights\nare granted or implied.\n\n5. Limitations and Restrictions. You will not and will not allow a third party\nto:\n\na. transfer, sublicense, or assign Your rights under this license to any other\nperson or entity (except as expressly provided in Section 12 below), unless\nexpressly authorized by Cisco in writing;\n\nb. modify, adapt or create derivative works of the Software or Documentation;\n\nc. reverse engineer, decompile, decrypt, disassemble or otherwise attempt to\nderive the source code for the Software, except as provided in Section 16\nbelow;\n\nd. make the functionality of the Software available to third parties, whether\nas an application service provider, or on a rental, service bureau, cloud\nservice, hosted service, or other similar basis unless expressly authorized by\nCisco in writing;\n\ne. Use Software that is licensed for a specific device, whether physical or\nvirtual, on another device, unless expressly authorized by Cisco in writing; or\n\nf. remove, modify, or conceal any product identification, copyright,\nproprietary, intellectual property notices or other marks on or within the\nSoftware.\n\n6. Third Party Use of Software. You may permit a third party to Use the\nSoftware licensed to You under this EULA if such Use is solely (i) on Your\nbehalf, (ii) for Your internal operations, and (iii) in compliance with this\nEULA. You agree that you are liable for any breach of this EULA by that third\nparty.\n\n7. Limited Warranty and Disclaimer.\n\na. Limited Warranty. Cisco warrants that the Software will substantially\nconform to the applicable Documentation for the longer of (i) ninety (90) days\nfollowing the date the Software is made available to You for your Use or (ii)\nas otherwise set forth at www.cisco.com/go/warranty. This warranty does not\napply if the Software, Cisco product or any other equipment upon which the\nSoftware is authorized to be used: (i) has been altered, except by Cisco or its\nauthorized representative, (ii) has not been installed, operated, repaired, or\nmaintained in accordance with instructions supplied by Cisco, (iii) has been\nsubjected to abnormal physical or electrical stress, abnormal environmental\nconditions, misuse, negligence, or accident; (iv) is licensed for beta,\nevaluation, testing or demonstration purposes or other circumstances for which\nthe Approved Source does not receive a payment of a purchase price or license\nfee; or (v) has not been provided by an Approved Source. Cisco will use\ncommercially reasonable efforts to deliver to You Software free from any\nviruses, programs, or programming devices designed to modify, delete, damage or\ndisable the Software or Your data.\n\nb. Exclusive Remedy. At Cisco's option and expense, Cisco shall repair,\nreplace, or cause the refund of the license fees paid for the non-conforming\nSoftware. This remedy is conditioned on You reporting the non-conformance in\nwriting to Your Approved Source within the warranty period. The Approved Source\nmay ask You to return the Software, the Cisco product, and/or Documentation as\na condition of this remedy. This Section is Your exclusive remedy under the\nwarranty.\n\nc. Disclaimer.\n\nExcept as expressly set forth above, Cisco and its licensors provide Software\n\"as is\" and expressly disclaim all warranties, conditions or other terms,\nwhether express, implied or statutory, including without limitation,\nwarranties, conditions or other terms regarding merchantability, fitness for a\nparticular purpose, design, condition, capacity, performance, title, and\nnon-infringement. Cisco does not warrant that the Software will operate\nuninterrupted or error-free or that all errors will be corrected. In addition,\nCisco does not warrant that the Software or any equipment, system or network on\nwhich the Software is used will be free of vulnerability to intrusion or\nattack.\n\n8. Limitations and Exclusions of Liability. In no event will Cisco or its\nlicensors be liable for the following, regardless of the theory of liability or\nwhether arising out of the use or inability to use the Software or otherwise,\neven if a party been advised of the possibility of such damages: (a) indirect,\nincidental, exemplary, special or consequential damages; (b) loss or corruption\nof data or interrupted or loss of business; or (c) loss of revenue, profits,\ngoodwill or anticipated sales or savings. All liability of Cisco, its\naffiliates, officers, directors, employees, agents, suppliers and licensors\ncollectively, to You, whether based in warranty, contract, tort (including\nnegligence), or otherwise, shall not exceed the license fees paid by You to any\nApproved Source for the Software that gave rise to the claim. This limitation\nof liability for Software is cumulative and not per incident. Nothing in this\nAgreement limits or excludes any liability that cannot be limited or excluded\nunder applicable law.\n\n9. Upgrades and Additional Copies of Software. Notwithstanding any other\nprovision of this EULA, You are not permitted to Use Upgrades unless You, at\nthe time of acquiring such Upgrade:\n\na. already hold a valid license to the original version of the Software, are in\ncompliance with such license, and have paid the applicable fee for the Upgrade;\nand\n\nb. limit Your Use of Upgrades or copies to Use on devices You own or lease; and\n\nc. unless otherwise provided in the Documentation, make and Use additional\ncopies solely for backup purposes, where backup is limited to archiving for\nrestoration purposes.\n\n10. Audit. During the license term for the Software and for a period of three\n(3) years after its expiration or termination, You will take reasonable steps\nto maintain complete and accurate records of Your use of the Software\nsufficient to verify compliance with this EULA. No more than once per twelve\n(12) month period, You will allow Cisco and its auditors the right to examine\nsuch records and any applicable books, systems (including Cisco product(s) or\nother equipment), and accounts, upon reasonable advanced notice, during Your\nnormal business hours. If the audit discloses underpayment of license fees, You\nwill pay such license fees plus the reasonable cost of the audit within thirty\n(30) days of receipt of written notice.\n\n11. Term and Termination. This EULA shall remain effective until terminated or\nuntil the expiration of the applicable license or subscription term. You may\nterminate the EULA at any time by ceasing use of or destroying all copies of\nSoftware. This EULA will immediately terminate if You breach its terms, or if\nYou fail to pay any portion of the applicable license fees and You fail to cure\nthat payment breach within thirty (30) days of notice. Upon termination of this\nEULA, You shall destroy all copies of Software in Your possession or control.\n\n12. Transferability. You may only transfer or assign these license rights to\nanother person or entity in compliance with the current Cisco\nRelicensing/Transfer Policy (www.cisco.com/c/en/us/products/\ncisco_software_transfer_relicensing_policy.html). Any attempted transfer or,\nassignment not in compliance with the foregoing shall be void and of no effect.\n\n13. US Government End Users. The Software and Documentation are \"commercial\nitems,\" as defined at Federal Acquisition Regulation (\"FAR\") (48 C.F.R.) 2.101,\nconsisting of \"commercial computer software\" and \"commercial computer software\ndocumentation\" as such terms are used in FAR 12.212. Consistent with FAR 12.211\n(Technical Data) and FAR 12.212 (Computer Software) and Defense Federal\nAcquisition Regulation Supplement (\"DFAR\") 227.7202-1 through 227.7202-4, and\nnotwithstanding any other FAR or other contractual clause to the contrary in\nany agreement into which this EULA may be incorporated, Government end users\nwill acquire the Software and Documentation with only those rights set forth in\nthis EULA. Any license provisions that are inconsistent with federal\nprocurement regulations are not enforceable against the U.S. Government.\n\n14. Export. Cisco Software, products, technology and services are subject to\nlocal and extraterritorial export control laws and regulations. You and Cisco\neach will comply with such laws and regulations governing use, export,\nre-export, and transfer of Software, products and technology and will obtain\nall required local and extraterritorial authorizations, permits or licenses.\nSpecific export information may be found at: tools.cisco.com/legal/export/pepd/\nSearch.do\n\n15. Survival. Sections 4, 5, the warranty limitation in 7(a), 7(b) 7(c), 8, 10,\n11, 13, 14, 15, 17 and 18 shall survive termination or expiration of this EULA.\n\n16. Interoperability. To the extent required by applicable law, Cisco shall\nprovide You with the interface information needed to achieve interoperability\nbetween the Software and another independently created program. Cisco will\nprovide this interface information at Your written request after you pay\nCisco's licensing fees (if any). You will keep this information in strict\nconfidence and strictly follow any applicable terms and conditions upon which\nCisco makes such information available.\n\n17. Governing Law, Jurisdiction and Venue.\n\nIf You acquired the Software in a country or territory listed below, as\ndetermined by reference to the address on the purchase order the Approved\nSource accepted or, in the case of an Evaluation Product, the address where\nProduct is shipped, this table identifies the law that governs the EULA\n(notwithstanding any conflict of laws provision) and the specific courts that\nhave exclusive jurisdiction over any claim arising under this EULA.\n\n\nCountry or Territory     | Governing Law           | Jurisdiction and Venue\n=========================|=========================|===========================\nUnited States, Latin     | State of California,    | Federal District Court,\nAmerica or the           | United States of        | Northern District of\nCaribbean                | America                 | California or Superior\n                         |                         | Court of Santa Clara\n                         |                         | County, California\n-------------------------|-------------------------|---------------------------\nCanada                   | Province of Ontario,    | Courts of the Province of\n                         | Canada                  | Ontario, Canada\n-------------------------|-------------------------|---------------------------\nEurope (excluding        | Laws of England         | English Courts\nItaly), Middle East,     |                         |\nAfrica, Asia or Oceania  |                         |\n(excluding Australia)    |                         |\n-------------------------|-------------------------|---------------------------\nJapan                    | Laws of Japan           | Tokyo District Court of\n                         |                         | Japan\n-------------------------|-------------------------|---------------------------\nAustralia                | Laws of the State of    | State and Federal Courts\n                         | New South Wales         | of New South Wales\n-------------------------|-------------------------|---------------------------\nItaly                    | Laws of Italy           | Court of Milan\n-------------------------|-------------------------|---------------------------\nChina                    | Laws of the People's    | Hong Kong International\n                         | Republic of China       | Arbitration Center\n-------------------------|-------------------------|---------------------------\nAll other countries or   | State of California     | State and Federal Courts\nterritories              |                         | of California\n-------------------------------------------------------------------------------\n\n\nThe parties specifically disclaim the application of the UN Convention on\nContracts for the International Sale of Goods. In addition, no person who is\nnot a party to the EULA shall be entitled to enforce or take the benefit of any\nof its terms under the Contracts (Rights of Third Parties) Act 1999. Regardless\nof the above governing law, either party may seek interim injunctive relief in\nany court of appropriate jurisdiction with respect to any alleged breach of\nsuch party's intellectual property or proprietary rights.\n\n18. Integration. If any portion of this EULA is found to be void or\nunenforceable, the remaining provisions of the EULA shall remain in full force\nand effect. Except as expressly stated or as expressly amended in a signed\nagreement, the EULA constitutes the entire agreement between the parties with\nrespect to the license of the Software and supersedes any conflicting or\nadditional terms contained in any purchase order or elsewhere, all of which\nterms are excluded. The parties agree that the English version of the EULA will\ngovern in the event of a conflict between it and any version translated into\nanother language.\n\n\nCisco and the Cisco logo are trademarks or registered trademarks of Cisco\nand/or its affiliates in the U.S. and other countries. To view a list of Cisco\ntrademarks, go to this URL: www.cisco.com/go/trademarks. Third-party trademarks\nmentioned are the property of their respective owners. The use of the word\npartner does not imply a partnership relationship between Cisco and any other\ncompany. (1110R)\n",
                    "currentPassword": "",
                    "newPassword": "",
                    "type": "initialprovision"
                  }
        payload["currentPassword"] = current_password
        payload["newPassword"] = new_password
        sendRequest(self.log, ip_address, URL, 'POST', payload, username, current_password)
        self.log.info(" Device Provisining Complete")

# def deployChanges():

def sendRequest(log, ip_address, url_suffix, operation='GET', json_payload=None, username='admin', password=''):
    access_token = getAccessToken(log, ip_address, username, password)
    URL = 'https://{}/api/fdm/latest{}'.format(ip_address, url_suffix)
    headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json', 
            'Authorization': 'Bearer ' + access_token}
    if operation == 'GET':
        log.info('Sending GET: ', URL)
        response = requests.get(url=URL, headers=headers, verify=False)
    elif operation == 'POST':
        log.info('Sending POST: ', URL)
        response = requests.post(url=URL, headers=headers, verify=False, json=json_payload )
    elif operation == 'DELETE':
        log.info('Sending DELETE: ', URL)
        response = requests.delete(url=URL, headers=headers, verify=False)
    else:
        raise Exception('Unknown Operation: {}'.format(operation))

    log.info('Response Status: ', response.status_code)
    if response.status_code == requests.codes.ok \
        or (response.status_code == 204 and response.text == ''):
        return response
    else:
        log.error('Error Response: ', response.text)
        log.error('Request Payload: ', json_payload)
        raise Exception('Bad status code: {}'.format(response.status_code))

def getAccessToken(log, ip_address, username, password):
    URL = 'https://{}/api/fdm/latest/fdm/token'.format(ip_address)
    payload = {'grant_type': 'password','username': username,'password': password}
    headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json'}
    login_wait_increment = 10
    login_wait_time = 15
    progressive_multiplier = 1
    timeout = 60
    while (True):
        response = requests.post(url=URL, headers=headers, verify=False, json=payload )
        if response.status_code == requests.codes.ok:
            data = response.json()
            access_token = data['access_token']
            log.debug('AccessToken: ', access_token)
            return access_token
        else:
            response_json = response.json()
            if response_json['message'].startswith('Too many failed attempts') and login_wait_time < timeout:
                log.info('Login failed, wait for it to reset {} seconds'.format(login_wait_time))
                login_wait_time = login_wait_time + (progressive_multiplier * login_wait_increment)
                progressive_multiplier = progressive_multiplier + 1 
                sleep(login_wait_time)
            else:
                log.error('Error Response:', response.text)
                raise Exception('Bad status code: {}'.format(response.status_code))

def commitDeviceChanges(log, ip_address, timeout=default_timeout):
    URL = '/operational/deploy'
    response = sendRequest(log, ip_address, URL, 'POST')
    log.debug(response.text)
    data = response.json()
    commit_id = data['id']
    URL = '/operational/deploy/{}'.format(commit_id)
    wait_time = 5
    wait_increment = 5
    progressive_multiplier = 1
    elapsed_time = 0
    while (True):
        response = sendRequest(log, ip_address, URL)
        data = response.json()
        state = data['state']
        log.info('commit change state: {}'.format(state))
        if state == 'DEPLOYED':
            log.info('Deploy time: ', elapsed_time)
            break
        elif elapsed_time < timeout:
            log.info('Elapsed wait time: {}, wait {} seconds to check status of device commit'.format(timeout, wait_time))
            wait_time = wait_time + (progressive_multiplier * wait_increment)
            progressive_multiplier = progressive_multiplier + 1 
            sleep(wait_time)
            elapsed_time = elapsed_time + wait_time
        else:
            log.error('Commit device change wait time ({}) exceeded'.format(timeout))
            raise Exception('Commit device change wait time ({}) exceeded'.format(timeout))

def addDeviceUser(log, transaction, device, username, password):
    URL = '/object/users'
    payload = { "name": "",
                "identitySourceId": "e3e74c32-3c03-11e8-983b-95c21a1b6da9",
                "password": "",
                "type": "user",
                "userRole": "string",
                "userServiceTypes": [
                    "RA_VPN"
                ]
              }
    payload['name'] = username
    payload['password'] = password
    root = ncs.maagic.get_root(transaction)
    service = device._parent._parent
    catalog_vnf = root.vnf_manager.vnf_catalog[service.catalog_vnf]
    vnf_day1_authgroup = root.devices.authgroups.group[catalog_vnf.authgroup]
    vnf_day1_username = vnf_day1_authgroup.default_map.remote_name
    vnf_day1_password = _ncs.decrypt(vnf_day1_authgroup.default_map.remote_password)
    response = sendRequest(log, device.management_ip_address, URL, 'POST', payload,
                           username=vnf_day1_username, password=vnf_day1_password)
    # commitDeviceChanges(log, device.management_ip_address)
    getDeviceData(log, device, trans)
    return response

def deleteDeviceUser(log, transaction, device, userid):
    URL = '/object/users/{}'.format(userid)
    root = ncs.maagic.get_root(transaction)
    service = device._parent._parent
    catalog_vnf = root.vnf_manager.vnf_catalog[service.catalog_vnf]
    vnf_day1_authgroup = root.devices.authgroups.group[catalog_vnf.authgroup]
    vnf_day1_username = vnf_day1_authgroup.default_map.remote_name
    vnf_day1_password = _ncs.decrypt(vnf_day1_authgroup.default_map.remote_password)
    response = sendRequest(log, device.management_ip_address, URL, 'DELETE',
                           username=vnf_day1_username, password=vnf_day1_password)
    # commitDeviceChanges(log, device.management_ip_address)
    getDeviceData(log, device, transaction)
    log.info('User delete complete')
    return response

def getDeviceData(log, device, trans):
    if device.state.port is not None:
        device.state.port.delete()
    if device.state.zone is not None:
        device.state.zone.delete()
    if device.state.user is not None:
        device.state.user.delete()

    catalog_vnf_name = device._parent._parent.catalog_vnf
    root = ncs.maagic.get_root(trans)
    authgroup_name = root.vnf_manager.vnf_catalog[catalog_vnf_name].authgroup
    vnf_authgroup = root.devices.authgroups.group[authgroup_name]
    vnf_username = vnf_authgroup.default_map.remote_name
    vnf_password = _ncs.decrypt(vnf_authgroup.default_map.remote_password)
    URL = '/object/tcpports?limit=0'
    response = sendRequest(log, device.management_ip_address, URL, username=vnf_username, password=vnf_password)
    data = response.json()
    log.debug(data)
    for item in data['items']:
        log.debug(item['name'], ' ', item['id'])
        port = device.state.port.create(str(item['name']))
        port.id = item['id']
    URL = '/object/securityzones?limit=0'
    response = sendRequest(log, device.management_ip_address, URL, username=vnf_username, password=vnf_password)
    data = response.json()
    log.debug(data)
    for item in data['items']:
        log.debug(item['name'], ' ', item['id'])
        zone = device.state.zone.create(str(item['name']))
        zone.id = item['id']
    URL = '/object/users'
    response = sendRequest(log, device.management_ip_address, URL, username=vnf_username, password=vnf_password)
    data = response.json()
    log.debug(data)
    for item in data['items']:
        log.debug(item['name'], ' ', item['id'])
        user = device.state.user.create(str(item['name']))
        user.id = item['id']

class SyncVNFWithNSO(Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        self.log.info('*************************************** action name: ', name)
        result = "Device Synchronization Successful"
        try:
            with ncs.maapi.single_write_trans(uinfo.username, 'test',
                                             db=ncs.RUNNING) as trans:
                service_device = ncs.maagic.get_node(trans, kp)
                self.log.info('Syncing Device: '+service_device.name)
                op_root = ncs.maagic.get_root(trans)
                device = op_root.devices.device[service_device.name]
                result = device.sync_from()
                #trans.apply()
        except Exception as error:
            self.log.info(traceback.format_exc())
            result = 'Error Syncing Device: ' + str(error)
            return
        finally:
            output.result = result
        try:
            with ncs.maapi.single_write_trans(uinfo.username, 'test',
                                              db=ncs.OPERATIONAL) as trans:
                service_device = ncs.maagic.get_node(trans, kp)
                self.log.info('Reporting Device Synced: '+service_device.name)
                service_device.status = 'Synchronized'
                trans.apply()
        except Exception as error:
            self.log.info(traceback.format_exc())
            result = 'Error Syncing Device: ' + str(error)
        finally:
            output.result = result


class RegisterVNFWithNSO(Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        self.log.info('*************************************** action name: ', name)
        try:
            with ncs.maapi.single_write_trans(uinfo.username, uinfo.context,
                                              db=ncs.RUNNING) as trans:
                service_device = ncs.maagic.get_node(trans, kp)
                self.log.info('Registering Device: '+service_device.name)
                service_device.status = 'Registering'
                vars = ncs.template.Variables()
                vars.add('DEVICE-NAME', service_device.name);
                vars.add('IP-ADDRESS', service_device.management_ip_address);
                vars.add('PORT', ftd_api_port);
                vars.add('AUTHGROUP', day0_authgroup);
                template = ncs.template.Template(service_device)
                template.apply('nso-device', vars)
                trans.apply()
                result = "Device Registration Successful"
        except Exception as error:
            self.log.info(traceback.format_exc())
            result = 'Error Registering Device: ' + str(error)
        finally:
            output.result = result

class DeleteDeviceUser(Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        self.log.info('action name: ', name)
        try:
            with ncs.maapi.single_write_trans(uinfo.username, uinfo.context,
                                              db=ncs.RUNNING) as trans:
                device = ncs.maagic.get_node(trans, kp)
                if device.state.user[input.username] is None:
                    raise Exception('User {} not valid'.format(input.username))
                userid = device.state.user[input.username].id
                deleteDeviceUser(self.log, trans, device, userid)
                result = "User Deleted"
                trans.apply()
        except Exception as error:
            self.log.info(traceback.format_exc())
            result = 'Error Deleting User: ' + str(error)
        finally:
            output.result = result

class AddDeviceUser(Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        self.log.info('action name: ', name)

        try:
            with ncs.maapi.single_write_trans(uinfo.username, uinfo.context,
                                              db=ncs.RUNNING) as trans:
                device = ncs.maagic.get_node(trans, kp)
                addDeviceUser(self.log, trans, device, input.username, input.password)
                result = "User Added"
                trans.apply()
        except Exception as error:
            self.log.info(traceback.format_exc())
            result = 'Error Adding User: ' + str(error)
        finally:
            output.result = result

class GetDeviceData(Action):
    @Action.action
    def cb_action(self, uinfo, name, kp, input, output):
        self.log.info('action name: ', name)

        maapi = ncs.maapi.Maapi()
        maapi.attach2(0, 0, uinfo.actx_thandle)
        trans = ncs.maapi.Transaction(maapi, uinfo.actx_thandle)
        device = ncs.maagic.get_node(trans, kp)
        getDeviceData(self.log, device, trans)
        output.result = "Ok"

class NGFWAdvancedService(Service):
    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info('Service create(service=', service._path, ')')
        proplistdict = dict(proplist)
        planinfo = {}
        try:
            # Deploy the VNF(s) using vnf-manager
            vars = ncs.template.Variables()
            template = ncs.template.Template(service)
            template.apply('vnf-manager-vnf-deployment', vars)
            # Check VNF-Manger service deployment status
            status = 'Unknown'
            with ncs.maapi.single_read_trans(tctx.uinfo.username, 'itd',
                                      db=ncs.OPERATIONAL) as trans:
                try:
                    op_root = ncs.maagic.get_root(trans)
                    deployment = op_root.vnf_manager.site[service.site].vnf_deployment[service.tenant, service.deployment_name]
                    status = deployment.status
                except KeyError:
                     # Service has just been called, have not committed NFVO information yet
                    self.log.info('Initial Service Call - wait for vnf-manager to report back')
                    pass
                self.log.info('VNF-Manager deployment status: ', status)
                if status == 'Failure':
                    planinfo['failure'] = 'vnfs-deployed'
                    return
                if status != 'Configurable':
                    return proplist
                planinfo['vnfs-deployed'] = 'COMPLETED'
                # Apply policies
                # TODO: This will be replaced with a template against the FTD NED when available
                for device in op_root.vnf_manager.site[service.site].vnf_deployment[service.tenant, service.deployment_name] \
                                .device:
                    self.log.info('Configuring device: ', device.name)
                    # Now apply the rules specified in the service by the user
                    for rule in service.access_rule:
                        zoneid = device.state.zone[rule.source_zone].id
                        portid = device.state.port[rule.source_port].id
                        url_suffix = '/policy/accesspolicies/c78e66bc-cb57-43fe-bcbf-96b79b3475b3/accessrules'
                        payload = {"name": rule.name,
                                   "sourceZones": [ {"id": zoneid,
                                                     "type": "securityzone"} ],
                                   "sourcePorts": [ {"id": portid,
                                                     "type": "tcpportobject"} ],
                                   "ruleAction": str(rule.action),
                                   "eventLogAction": "LOG_NONE",
                                   "type": "accessrule" }
                        try:
                            response = sendRequest(self.log, device.management_ip_address, url_suffix, 'POST', payload)
                        except Exception as e:
                            if str(e) == 'Bad status code: 422':
                                self.log.info('Ignoring: ', e, ' for now as it is probably an error on applying the same rule twice')
                            else:
                                planinfo['failure'] = 'vnfs-deployed'
                                raise
                planinfo['vnfs-configured'] = 'COMPLETED'
        except Exception as e:
            self.log.error("Exception Here:")
            self.log.info(e)
            self.log.info(traceback.format_exc())
            raise
        finally:
            # Create a kicker to be alerted when the VNFs are deployed/undeployed
            kick_monitor_node = "/vnf-manager/site[name='{}']/vnf-deployment[tenant='{}'][deployment-name='{}']/status".format(
                                service.site, service.tenant, service.deployment_name)
            kick_node = "/firewall/ftdv-ngfw-advanced[site='{}'][tenant='{}'][deployment-name='{}']".format(
                                service.site, service.tenant, service.deployment_name)
            kick_expr = ". = 'Configurable' or . = 'Failure' or . = 'Starting VNFs'"

            self.log.info('Creating Kicker Monitor on: ', kick_monitor_node)
            self.log.info(' kicking node: ', kick_node)
            kicker = root.kickers.data_kicker.create('firewall-service-{}-{}-{}'.format(service.site, service.tenant, service.deployment_name))
            kicker.monitor = kick_monitor_node
            kicker.kick_node = kick_node
            # kicker.trigger_expr = kick_expr
            # kicker.trigger_type = 'enter'
            kicker.action_name = 'reactive-re-deploy'
            self.log.info(str(proplistdict))
            proplist = [(k,v) for k,v in proplistdict.iteritems()]
            self.log.info(str(proplist))
            self.write_plan_data(service, planinfo)
            return proplist

    def write_plan_data(self, service, planinfo):
        self_plan = PlanComponent(service, 'vnf-deployment', 'ncs:self')
        self_plan.append_state('ncs:init')
        self_plan.append_state('ftdv-ngfw:vnfs-deployed')
        self_plan.append_state('ftdv-ngfw:vnfs-configured')
        self_plan.append_state('ncs:ready')
        self_plan.set_reached('ncs:init')

        if planinfo.get('vnfs-deployed', '') == 'COMPLETED':
            self_plan.set_reached('ftdv-ngfw:vnfs-deployed')
        if planinfo.get('vnfs-configured', '') == 'COMPLETED':
            self_plan.set_reached('ftdv-ngfw:vnfs-configured')
            if planinfo.get('failure', None) is None:
                self_plan.set_reached('ncs:ready')

        if planinfo.get('failure', None) is not None:
            self.log.info('setting failure, ftdv-ngfw:'+planinfo['failure'])
            self_plan.set_failed('ftdv-ngfw:'+planinfo['failure'])


class NGFWBasicService(Service):

    @Service.create
    def cb_create(self, tctx, root, service, proplist):
        self.log.info('Service create(service=', service._path, ')')
        vnf_catalog = root.vnf_manager.vnf_catalog
        site = root.vnf_manager.site[service.site]
        management_network = site.management_network

        vars = ncs.template.Variables()
        vars.add('DEPLOYMENT-NAME', service.deployment_name);
        vars.add('DATACENTER-NAME', site.datacenter_name);
        vars.add('DATASTORE-NAME', site.datastore_name);
        vars.add('CLUSTER-NAME', site.cluster_name);
        vars.add('MANAGEMENT-NETWORK-NAME', management_network.name);
        vars.add('MANAGEMENT-NETWORK-IP-ADDRESS', service.ip_address);
        vars.add('MANAGEMENT-NETWORK-NETMASK', management_network.netmask);
        vars.add('MANAGEMENT-NETWORK-GATEWAY-IP-ADDRESS', management_network.gateway_ip_address);
        vars.add('DNS-IP-ADDRESS', site.dns_ip_address);
        vars.add('DEPLOY-PASSWORD', day0_admin_password); # admin password to set when deploy
        vars.add('IMAGE-NAME', root.nfvo.vnfd[vnf_catalog[service.catalog_vnf].descriptor_name]
                                .vdu[vnf_catalog[service.catalog_vnf].descriptor_vdu]
                                .software_image_descriptor.image);
        template = ncs.template.Template(service)
        template.apply('esc-ftd-deployment', vars)

        try:
            with ncs.maapi.single_read_trans(tctx.uinfo.username, 'system',
                                              db=ncs.RUNNING) as trans:
                servicetest = ncs.maagic.get_node(trans, service._path)
                self.log.info('Deployment Exists - RUNNING')
        except Exception as e:
            self.log.info('Deployment does not exist!')
            # self.log.info(traceback.format_exc())
            return
        # service = ncs.maagic.get_node(root, kp)
        access_token = getAccessToken(self.log, service)
        headers = {'Content-Type' : 'application/json', 'Accept' : 'application/json', 
                    'Authorization': 'Bearer ' + access_token}
        for rule in service.access_rule:
            URL = 'https://{}/api/fdm/latest/policy/accesspolicies/c78e66bc-cb57-43fe-bcbf-96b79b3475b3/accessrules'.format(service.ip_address)
            response = requests.get(url=URL, headers=headers, verify=False)
            data = response.json()
            found = False
            for item in data['items']:
                if item['name'] == rule.name:
                    found = True
                    self.log.info('Found')
            self.log.info('Got here')
            self.log.info('Deployment Exists')
            zoneid = service.state.zone[rule.source_zone].id
            portid = service.state.port[rule.source_port].id
            self.log.info('Deployment Exists ', rule.source_zone, ' ', rule.source_port)
            self.log.info('Deployment Exists ', zoneid, ' ', portid)
            URL = 'https://{}/api/fdm/latest/policy/accesspolicies/c78e66bc-cb57-43fe-bcbf-96b79b3475b3/accessrules'.format(service.ip_address)
            payload = {"name": rule.name,
                       "sourceZones": [ {"id": zoneid,
                                         "type": "securityzone"} ],
                       "sourcePorts": [ {"id": portid,
                                         "type": "tcpportobject"} ],
                       "ruleAction": str(rule.action),
                       "eventLogAction": "LOG_NONE",
                       "type": "accessrule" }
            self.log.info(str(payload))
            if not found:
                response = requests.post(url=URL, headers=headers, verify=False, json=payload )
            self.log.info('Got here 2')
            self.log.info(response.content)
    # The pre_modification() and post_modification() callbacks are optional,
    # and are invoked outside FASTMAP. pre_modification() is invoked before
    # create, update, or delete of the service, as indicated by the enum
    # ncs_service_operation op parameter. Conversely
    # post_modification() is invoked after create, update, or delete
    # of the service. These functions can be useful e.g. for
    # allocations that should be stored and existing also when the
    # service instance is removed.

    # @Service.pre_lock_create
    # def cb_pre_lock_create(self, tctx, root, service, proplist):
    #     self.log.info('Service plcreate(service=', service._path, ')')

    # @Service.pre_modification
    # def cb_pre_modification(self, tctx, op, kp, root, proplist):
    #     self.log.info('Service premod(service=', kp, ')')

    # @Service.post_modification
    # def cb_post_modification(self, tctx, op, kp, root, proplist):
    #     self.log.info('Service postmod(service=', kp, ' ', op, ')')
    #     try:
    #         with ncs.maapi.single_write_trans(uinfo.username, uinfo.context) as trans:
    #             service = ncs.maagic.get_node(trans, kp)
    #             device_name = service.device_name
    #             device = ncs.maagic.get_root().devices.device[device_name]
    #             inputs = service.check_bgp
    #             inputs.service_name = service.name
    #             result = service.check_bgp()
    #             addDeviceUser(self.log, x`, input.username, input.password)
    #             result = "User Added"
    #             service.status = "GOOD"
    #             trans.apply()
    #     except Exception as error:
    #         self.log.info(traceback.format_exc())
    #         result = 'Error Adding User: ' + str(error)
    #     finally:
    #         output.result = result


# ---------------------------------------------
# COMPONENT THREAD THAT WILL BE STARTED BY NCS.
# ---------------------------------------------
class Main(ncs.application.Application):
    def setup(self):
        # The application class sets up logging for us. It is accessible
        # through 'self.log' and is a ncs.log.Log instance.
        self.log.info('Main RUNNING')
        with ncs.maapi.Maapi() as m:
            m.install_crypto_keys()
        # Service callbacks require a registration for a 'service point',
        # as specified in the corresponding data model.
        #
        self.register_service('ftdv-ngfw-servicepoint', NGFWBasicService)
        self.register_service('ftdv-ngfw-advanced-servicepoint', NGFWAdvancedService)
        self.register_service('ftdv-ngfw-scalable-servicepoint', ScalableService)
        self.register_action('ftdv-ngfw-registerVnfWithNso-action', RegisterVNFWithNSO)
        self.register_action('ftdv-ngfw-syncVnfWithNso-action', SyncVNFWithNSO)
        self.register_action('ftdv-ngfw-getDeviceData-action', GetDeviceData)
        self.register_action('ftdv-ngfw-addUser-action', AddDeviceUser)
        self.register_action('ftdv-ngfw-deleteUser-action', DeleteDeviceUser)
#        self.register_service('ftdv-ngfw-access-rule-servicepoint', AccessRuleService)

        # If we registered any callback(s) above, the Application class
        # took care of creating a daemon (related to the service/action point).

        # When this setup method is finished, all registrations are
        # considered done and the application is 'started'.

    def teardown(self):
        # When the application is finished (which would happen if NCS went
        # down, packages were reloaded or some error occurred) this teardown
        # method will be called.

        self.log.info('Main FINISHED')


