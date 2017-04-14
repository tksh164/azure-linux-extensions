#!/usr/bin/env python
#
# Azure Linux extension
#
# Linux Azure Diagnostic Extension (Current version is specified in manifest.xml)
# Copyright (c) Microsoft Corporation
# All rights reserved.
# MIT License
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
#  documentation files (the ""Software""), to deal in the Software without restriction, including without limitation
#  the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
#  permit persons to whom the Software is furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
#  Software.
# THE SOFTWARE IS PROVIDED *AS IS*, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
#  WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS
#  OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
#  OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import os.path
import traceback
import xml.etree.ElementTree as ET

import Providers.Builtin as BuiltIn
import Utils.LadDiagnosticUtil as LadUtil
import Utils.XmlUtil as XmlUtil
from Utils.lad_logging_config import LadLoggingConfig, copy_source_mdsdevent_elems, LadLoggingConfigException
from Utils.misc_helpers import get_storage_endpoint_with_account, escape_nonalphanumerics

_mdsd_xml_template = """
<MonitoringManagement eventVersion="2" namespace="" timestamp="2017-03-27T19:45:00.000" version="1.0">
  <Accounts>
    <Account account="" isDefault="true" key="" moniker="moniker" tableEndpoint="" />
    <SharedAccessSignature account="" isDefault="true" key="" moniker="moniker" tableEndpoint="" />
  </Accounts>

  <Management defaultRetentionInDays="90" eventVolume="">
    <Identity>
      <IdentityComponent name="DeploymentId" />
      <IdentityComponent name="Host" useComputerName="true" />
    </Identity>
    <AgentResourceUsage diskQuotaInMB="50000" />
  </Management>

  <Schemas>
  </Schemas>

  <Sources>
  </Sources>

  <Events>
    <MdsdEvents>
    </MdsdEvents>

    <OMI>
    </OMI>

    <DerivedEvents>
    </DerivedEvents>
  </Events>

  <EventStreamingAnnotations>
  </EventStreamingAnnotations>

</MonitoringManagement>
"""


class LadConfigAll:
    """
    A class to generate configs for all 3 core components of LAD: mdsd, omsagent (fluentd), and syslog
    (rsyslog or syslog-ng) based on LAD's JSON extension settings.
    The mdsd XML config file generated will be /var/lib/waagent/Microsoft. ...-x.y.zzzz/xmlCfg.xml (hard-coded).
    Other config files whose contents are generated by this class are as follows:
    - /etc/opt/microsoft/omsagent/LAD/conf/omsagent.d/syslog.conf : fluentd's syslog source config
    - /etc/opt/microsoft/omsagent/LAD/conf/omsagent.d/tail.conf : fluentd's tail source config (fileLogs)
    - /etc/opt/microsoft/omsagent/LAD/conf/omsagent.d/z_out_mdsd.conf : fluentd's out_mdsd out plugin config
    - /etc/rsyslog.conf or /etc/rsyslog.d/95-omsagent.conf: rsyslog config for LAD's syslog settings
       The content should be appended to the corresponding file, not overwritten. After that, the file should be
       processed so that the '%SYSLOG_PORT%' pattern is replaced with the assigned TCP port number.
    - /etc/syslog-ng.conf: syslog-ng config for LAD's syslog settings. The content should be appended, not overwritten.
    """
    _default_perf_cfgs = [
        {"query": "SELECT PercentAvailableMemory, AvailableMemory, UsedMemory, PercentUsedSwap "
                  "FROM SCX_MemoryStatisticalInformation",
         "table": "LinuxMemory"},
        {"query": "SELECT PercentProcessorTime, PercentIOWaitTime, PercentIdleTime "
                  "FROM SCX_ProcessorStatisticalInformation WHERE Name='_TOTAL'",
         "table": "LinuxCpu"},
        {"query": "SELECT AverageWriteTime,AverageReadTime,ReadBytesPerSecond,WriteBytesPerSecond "
                  "FROM  SCX_DiskDriveStatisticalInformation WHERE Name='_TOTAL'",
         "table": "LinuxDisk"}
    ]

    def __init__(self, ext_settings, ext_dir, waagent_dir, deployment_id,
                 fetch_uuid, encrypt_string, logger_log, logger_error):
        """
        Constructor.
        :param ext_settings: A LadExtSettings (in Utils/lad_ext_settings.py) obj wrapping the Json extension settings.
        :param ext_dir: Extension directory (e.g., /var/lib/waagent/Microsoft.OSTCExtensions.LinuxDiagnostic-2.3.xxxx)
        :param waagent_dir: WAAgent directory (e.g., /var/lib/waagent)
        :param deployment_id: Deployment ID string (or None) that should be obtained & passed by the caller
                              from waagent's HostingEnvironmentCfg.xml.
        :param fetch_uuid: A function which fetches the UUID for the VM
        :param encrypt_string: A function which encrypts a string, given a cert_path
        :param logger_log: Normal logging function (e.g., hutil.log) that takes only one param for the logged msg.
        :param logger_error: Error logging function (e.g., hutil.error) that takes only one param for the logged msg.
        """
        self._ext_settings = ext_settings
        self._ext_dir = ext_dir
        self._waagent_dir = waagent_dir
        self._deployment_id = deployment_id
        self._fetch_uuid = fetch_uuid
        self._encrypt_secret = encrypt_string
        self._logger_log = logger_log
        self._logger_error = logger_error

        # Generated logging configs place holders
        self._fluentd_syslog_src_config = None
        self._fluentd_tail_src_config = None
        self._fluentd_out_mdsd_config = None
        self._rsyslog_config = None
        self._syslog_ng_config = None

        self._mdsd_config_xml_tree = ET.ElementTree(ET.fromstring(_mdsd_xml_template))
        self._sink_configs = LadUtil.SinkConfiguration()
        self._sink_configs.insert_from_config(self._ext_settings.read_protected_config('sinksConfig'))
        # If we decide to also read sinksConfig from ladCfg, do it first, so that private settings override

        # Get encryption settings
        thumbprint = ext_settings.get_handler_settings()['protectedSettingsCertThumbprint']
        path = '{0}/{1}.{2}'
        self._cert_path = os.path.join(waagent_dir, thumbprint + '.crt')
        self._pkey_path = os.path.join(waagent_dir, thumbprint + '.prv')

    def _ladCfg(self):
        return self._ext_settings.read_public_config('ladCfg')

    def _update_metric_collection_settings(self, ladCfg):
        """
        Update mdsd_config_xml_tree for Azure Portal metric collection. The mdsdCfg performanceCounters element contains
        an array of metric definitions; this method passes each definition to its provider's AddMetric method, which is
        responsible for configuring the provider to deliver the metric to mdsd and for updating the mdsd config as
        required to expect the metric to arrive. This method also builds the necessary aggregation queries (from the
        metrics.metricAggregation array) that grind the ingested data and push it to the WADmetric table.
        :param ladCfg: ladCfg object from extension config
        :return: None
        """
        metrics = LadUtil.getPerformanceCounterCfgFromLadCfg(ladCfg)
        if not metrics:
            return

        counter_to_table = {}
        local_tables = set()

        # Add each metric
        for metric in metrics:
            if metric['type'] == 'builtin':
                local_table_name = BuiltIn.AddMetric(metric)
                if local_table_name:
                    local_tables.add(local_table_name)
                    counter_to_table[metric['counterSpecifier']] = local_table_name

        # Finalize; update the mdsd config to be prepared to receive the metrics
        BuiltIn.UpdateXML(self._mdsd_config_xml_tree)

        # Pump the received data from the local tables to the desired sinks. The "WADmetrics" shoebox table sink is
        # always served; after that, check for other sinks and handle appropriately. The partitionKey is filled in
        # later.
        ladquery = '''
<DerivedEvent duration="{interval}" eventName="WADMetrics{interval}P10DV2S" isFullName="true" source="{localtable}">
<LADQuery columnName="CounterName" columnValue="Value" partitionKey="" />
</DerivedEvent>
'''
        intervals = LadUtil.getAggregationPeriodsFromLadCfg(ladCfg)
        sinks = LadUtil.getFeatureWideSinksFromLadCfg(ladCfg, 'performanceCounters')
        for table_name in local_tables:
            for aggregation_interval in intervals:
                query = ladquery.format(interval=aggregation_interval, localtable=table_name)
                XmlUtil.addElement(self._mdsd_config_xml_tree, 'Events/DerivedEvents', ET.fromstring(query))
            # Other sinks are handled here
            for name in sinks.split(','):
                sink = self._sink_configs.get_sink_by_name(name)
                if sink is None:
                    self._logger_log("Ignoring sink '{0}' for which no definition was found".format(name))
                else:
                    if sink['type'] == 'EventHub':
                        # Generate a <DerivedEvent> to extract data (raw or aggregated) and send it to EH
                        pass

    def _update_perf_counters_settings(self, omi_queries):
        """
        Update the mdsd XML tree with the OMI queries provided.
        :param omi_queries: List of dictionaries specifying OMI queries and destination tables. E.g.:
         [
             {"query":"SELECT PercentAvailableMemory, AvailableMemory, UsedMemory, PercentUsedSwap FROM SCX_MemoryStatisticalInformation","table":"LinuxMemory"},
             {"query":"SELECT PercentProcessorTime, PercentIOWaitTime, PercentIdleTime FROM SCX_ProcessorStatisticalInformation WHERE Name='_TOTAL'","table":"LinuxCpu"},
             {"query":"SELECT AverageWriteTime,AverageReadTime,ReadBytesPerSecond,WriteBytesPerSecond FROM  SCX_DiskDriveStatisticalInformation WHERE Name='_TOTAL'","table":"LinuxDisk"}
         ]
        :return: None. The mdsd XML tree member is updated accordingly.
        """
        if not omi_queries:
            return

        mdsd_omi_query_schema = """
<OMIQuery cqlQuery="" dontUsePerNDayTable="true" eventName="" omiNamespace="" priority="High" sampleRateInSeconds="" />
"""

        for omi_query in omi_queries:
            if 'query' in omi_query and 'table' in omi_query:
                mdsd_omi_query_element = XmlUtil.createElement(mdsd_omi_query_schema)
                mdsd_omi_query_element.set('cqlQuery', omi_query['query'])
                mdsd_omi_query_element.set('eventName', omi_query['table'])
                namespace = omi_query['namespace'] if 'namespace' in omi_query else 'root/scx'
                mdsd_omi_query_element.set('omiNamespace', namespace)
                frequency = omi_query['frequency'] if 'frequency' in omi_query else '300'
                mdsd_omi_query_element.set('sampleRateInSeconds', frequency)
                XmlUtil.addElement(xml=self._mdsd_config_xml_tree, path='Events/OMI',
                                   el=mdsd_omi_query_element, addOnlyOnce=True)
            else:
                self._logger_log("Ignoring perfCfg array element missing required elements: '{0}'".format(omi_query))

    def _apply_perf_cfg(self):
        """
        Extract the 'perfCfg' settings from ext_settings and apply them to mdsd config XML root. These are *not* the
        ladcfg{performanceCounters{...}} settings; the perfCfg block is found at the top level of the public configs.
        :return: None. Changes are applied directly to the mdsd config XML tree member.
        """
        assert self._mdsd_config_xml_tree is not None

        perf_cfg = self._ext_settings.read_public_config('perfCfg')
        # If none, use default (3 OMI queries) DISABLED
        # if not perf_cfgs and not self._ext_settings.has_public_config('perfCfg'):
        #     perf_cfgs = LadConfigAll._default_perf_cfgs

        try:
            self._update_perf_counters_settings(perf_cfg)
        except Exception as e:
            self._logger_error("Failed to create perf config. Error:{0}\n"
                               "Stacktrace: {1}".format(e, traceback.format_exc()))

    def _encrypt_secret_with_cert(self, secret):
        """
        update_account_settings() helper.
        :param secret: Secret to encrypt
        :return: Encrypted secret string. None if openssl command exec fails.
        """
        return self._encrypt_secret(self._cert_path, secret)

    def _update_account_settings(self, account, key, token, endpoint):
        """
        Update the MDSD configuration Account element with Azure table storage properties.
        Exactly one of (key, token) must be provided.
        :param account: Storage account to which LAD should write data
        :param key: Shared key secret for the storage account, if present
        :param token: SAS token to access the storage account, if present
        :param endpoint: Identifies the Azure instance (public or specific sovereign cloud) where the storage account is
        """
        assert key or token, "Either key or token must be given."
        assert self._mdsd_config_xml_tree is not None

        if key:
            key = self._encrypt_secret_with_cert(key)
            assert key, "Could not encrypt key"
            XmlUtil.setXmlValue(self._mdsd_config_xml_tree, 'Accounts/Account',
                                "account", account, ['isDefault', 'true'])
            XmlUtil.setXmlValue(self._mdsd_config_xml_tree, 'Accounts/Account',
                                "key", key, ['isDefault', 'true'])
            XmlUtil.setXmlValue(self._mdsd_config_xml_tree, 'Accounts/Account',
                                "decryptKeyPath", self._pkey_path, ['isDefault', 'true'])
            XmlUtil.setXmlValue(self._mdsd_config_xml_tree, 'Accounts/Account',
                                "tableEndpoint", endpoint, ['isDefault', 'true'])
            XmlUtil.removeElement(self._mdsd_config_xml_tree, 'Accounts', 'SharedAccessSignature')
        else:  # token
            token = self._encrypt_secret_with_cert(token)
            assert token, "Could not encrypt token"
            XmlUtil.setXmlValue(self._mdsd_config_xml_tree, 'Accounts/SharedAccessSignature',
                                "account", account, ['isDefault', 'true'])
            XmlUtil.setXmlValue(self._mdsd_config_xml_tree, 'Accounts/SharedAccessSignature',
                                "key", token, ['isDefault', 'true'])
            XmlUtil.setXmlValue(self._mdsd_config_xml_tree, 'Accounts/SharedAccessSignature',
                                "decryptKeyPath", self._pkey_path, ['isDefault', 'true'])
            XmlUtil.setXmlValue(self._mdsd_config_xml_tree, 'Accounts/SharedAccessSignature',
                                "tableEndpoint", endpoint, ['isDefault', 'true'])
            XmlUtil.removeElement(self._mdsd_config_xml_tree, 'Accounts', 'Account')

    def _set_xml_attr(self, key, value, xml_path, selector=[]):
        """
        Set XML attribute on the element specified with xml_path.
        :param key: The attribute name to set on the XML element.
        :param value: The default value to be set, if there's no public config for that attribute.
        :param xml_path: The path of the XML element(s) to which the attribute is applied.
        :param selector: Selector for finding the actual XML element (see XmlUtil.setXmlValue)
        :return: None. Change is directly applied to mdsd_config_xml_tree XML member object.
        """
        assert self._mdsd_config_xml_tree is not None

        v = self._ext_settings.read_public_config(key)
        if not v:
            v = value
        XmlUtil.setXmlValue(self._mdsd_config_xml_tree, xml_path, key, v, selector)

    def _set_event_volume(self, lad_cfg):
        """
        Set event volumne in mdsd config. Check if desired event volume is specified,
        first in ladCfg then in public config. If in neither then default to Medium.
        :param lad_cfg: 'ladCfg' Json object to look up for the event volume setting.
        :return: None. The mdsd config XML tree's eventVolume attribute is directly updated.
        :rtype: str
        """
        assert self._mdsd_config_xml_tree is not None

        event_volume = LadUtil.getEventVolumeFromLadCfg(lad_cfg)
        if event_volume:
            self._logger_log("Event volume found in ladCfg: " + event_volume)
        else:
            event_volume = self._ext_settings.read_public_config("eventVolume")
            if event_volume:
                self._logger_log("Event volume found in public config: " + event_volume)
            else:
                event_volume = "Medium"
                self._logger_log("Event volume not found in config. Using default value: " + event_volume)
        XmlUtil.setXmlValue(self._mdsd_config_xml_tree, "Management", "eventVolume", event_volume)

    ######################################################################
    # This is the main API that's called by user. All other methods are
    # actually helpers for this, thus made private by convention.
    ######################################################################
    def generate_all_configs(self):
        """
        Generates configs for all components required by LAD.
        Generates XML cfg file for mdsd, from JSON config settings (public & private).
        Also generates rsyslog/syslog-ng configs corresponding to 'syslogEvents' or 'syslogCfg' setting.
        Also generates fluentd's syslog/tail src configs and out_mdsd configs.
        The rsyslog/syslog-ng and fluentd configs are not yet saved to files. They are available through
        the corresponding getter methods of this class (get_fluentd_*_config(), get_*syslog*_config()).

        Returns (True, '') if config was valid and proper xmlCfg.xml was generated.
        Returns (False, '...') if config was invalid and the error message.
        """

        # 1. Add DeploymentId (if available) to identity columns
        if self._deployment_id:
            XmlUtil.setXmlValue(self._mdsd_config_xml_tree, "Management/Identity/IdentityComponent", "",
                                self._deployment_id, ["name", "DeploymentId"])
        # 2. Use ladCfg to generate OMIQuery and LADQuery elements
        lad_cfg = self._ladCfg()
        if lad_cfg:
            try:
                self._update_metric_collection_settings(lad_cfg)
                resource_id = self._ext_settings.get_resource_id()
                if resource_id:
                    XmlUtil.setXmlValue(self._mdsd_config_xml_tree, 'Events/DerivedEvents/DerivedEvent/LADQuery',
                                        'partitionKey', escape_nonalphanumerics(resource_id))
                    instance_id = ""
                    if resource_id.find("providers/Microsoft.Compute/virtualMachineScaleSets") >= 0:
                        instance_id = self._fetch_uuid()
                    self._set_xml_attr("instanceID", instance_id, "Events/DerivedEvents/DerivedEvent/LADQuery")
            except Exception as e:
                self._logger_error("Failed to create portal config  error:{0} {1}".format(e, traceback.format_exc()))
                return False, 'Failed to create portal config from ladCfg (see extension error logs for more details)'

        # 3. Generate config for perfCfg. Need to distinguish between non-AppInsights scenario and AppInsights scenario,
        #    so check if Application Insights key is present and pass it to the actual helper
        #    function (self._apply_perf_cfg()).
        try:
            self._apply_perf_cfg()
        except Exception as e:
            self._logger_error("Failed check for Application Insights key in LAD configuration with exception:{0}\n"
                               "Stacktrace: {1}".format(e, traceback.format_exc()))
            return False, 'Failed to update perf counter config (see extension error logs for more details)'

        # 4. Generate omsagent (fluentd) configs, rsyslog/syslog-ng config, and update corresponding mdsd config XML
        try:
            syslogEvents_setting = self._ext_settings.get_syslogEvents_setting()
            fileLogs_setting = self._ext_settings.get_fileLogs_setting()
            lad_logging_config_helper = LadLoggingConfig(syslogEvents_setting, fileLogs_setting)
            mdsd_syslog_config = lad_logging_config_helper.get_oms_mdsd_syslog_config()
            mdsd_filelog_config = lad_logging_config_helper.get_oms_mdsd_filelog_config()
            copy_source_mdsdevent_elems(self._mdsd_config_xml_tree, mdsd_syslog_config)
            copy_source_mdsdevent_elems(self._mdsd_config_xml_tree, mdsd_filelog_config)
            self._fluentd_syslog_src_config = lad_logging_config_helper.get_oms_fluentd_syslog_src_config()
            self._fluentd_tail_src_config = lad_logging_config_helper.get_oms_fluentd_filelog_src_config()
            self._fluentd_out_mdsd_config = lad_logging_config_helper.get_oms_fluentd_out_mdsd_config()
            self._rsyslog_config = lad_logging_config_helper.get_oms_rsyslog_config()
            self._syslog_ng_config = lad_logging_config_helper.get_oms_syslog_ng_config()
        except Exception as e:
            self._logger_error("Failed to create omsagent (fluentd), rsyslog/syslog-ng configs or to update "
                               "corresponding mdsd config XML. Error: {0}\nStacktrace: {1}"
                               .format(e, traceback.format_exc()))
            return False, 'Failed to generate configs for fluentd, syslog, and mdsd for that (' \
                          'see extension error logs for more details)'

        # 5. Before starting to update the storage account settings, log extension's protected settings'
        #    keys only (except well-known values), for diagnostic purpose. This is mainly to make sure that
        #    the extension's Json settings include a correctly entered 'storageEndpoint'.
        self._ext_settings.log_protected_settings_keys(self._logger_log, self._logger_error)

        # 6. Actually update the storage account settings on mdsd config XML tree (based on extension's
        #    protectedSettings).
        account = self._ext_settings.read_protected_config('storageAccountName')
        if not account:
            return False, "Empty storageAccountName"
        key = self._ext_settings.read_protected_config('storageAccountKey')
        token = self._ext_settings.read_protected_config('storageAccountSasToken')
        if not key and not token:
            return False, "Neither storageAccountKey nor storageAccountSasToken is given"
        if key and token:
            return False, "Either storageAccountKey or storageAccountSasToken (but not both) should be given"
        endpoint = get_storage_endpoint_with_account(account,
                                                     self._ext_settings.read_protected_config('storageAccountEndPoint'))
        self._update_account_settings(account, key, token, endpoint)

        # 7. Update mdsd config XML's eventVolume attribute based on the logic specified in the helper.
        self._set_event_volume(lad_cfg)

        # 8. Finally generate mdsd config XML file out of the constructed XML tree object.
        self._mdsd_config_xml_tree.write(os.path.join(self._ext_dir, 'xmlCfg.xml'))

        return True, ""

    def __throw_if_output_is_none(self, output):
        """
        Helper to check if output is already generated (not None) and throw if it's not (None).
        :return: None
        """
        if output is None:
            raise LadLoggingConfigException('LadConfigAll.get_*_config() should be called after '
                                            'LadConfigAll.generate_mdsd_omsagent_syslog_config() is called')

    def get_fluentd_syslog_src_config(self):
        """
        Returns the obtained Fluentd's syslog src config. This getter (and all that follow) should be called
        after self.generate_mdsd_omsagent_syslog_config() is called.
        The return value should be overwritten to /etc/opt/microsoft/omsagent/LAD/conf/omsagent.d/syslog.conf
        after replacing '%SYSLOG_PORT%' with the assigned TCP port number.
        :rtype: str
        :return: Fluentd syslog src config string
        """
        self.__throw_if_output_is_none(self._fluentd_syslog_src_config)
        return self._fluentd_syslog_src_config

    def get_fluentd_tail_src_config(self):
        """
        Returns the obtained Fluentd's tail src config. This getter (and all that follow) should be called
        after self.generate_mdsd_omsagent_syslog_config() is called.
        The return value should be overwritten to /etc/opt/microsoft/omsagent/LAD/conf/omsagent.d/tail.conf.
        :rtype: str
        :return: Fluentd tail src config string
        """
        self.__throw_if_output_is_none(self._fluentd_tail_src_config)
        return self._fluentd_tail_src_config

    def get_fluentd_out_mdsd_config(self):
        """_fluentd_out_mdsd_config
        Returns the obtained Fluentd's out_mdsd config. This getter (and all that follow) should be called
        after self.generate_mdsd_omsagent_syslog_config() is called.
        The return value should be overwritten to /etc/opt/microsoft/omsagent/LAD/conf/omsagent.d/z_out_mdsd.conf.
        :rtype: str
        :return: Fluentd out_mdsd config string
        """
        self.__throw_if_output_is_none(self._fluentd_out_mdsd_config)
        return self._fluentd_out_mdsd_config

    def get_rsyslog_config(self):
        """
        Returns the obtained rsyslog config. This getter (and all that follow) should be called
        after self.generate_mdsd_omsagent_syslog_config() is called.
        The return value should be appended to /etc/rsyslog.d/95-omsagent.conf if rsyslog ver is new (that is, if
        /etc/rsyslog.d/ exists). It should be appended to /etc/rsyslog.conf if rsyslog ver is old (no /etc/rsyslog.d/).
        The appended file (either /etc/rsyslog.d/95-omsagent.conf or /etc/rsyslog.conf) should be processed so that
        the '%SYSLOG_PORT%' pattern in the file is replaced with the assigned TCP port number.
        :rtype: str
        :return: rsyslog config string
        """
        self.__throw_if_output_is_none(self._rsyslog_config)
        return self._rsyslog_config

    def get_syslog_ng_config(self):
        """
        Returns the obtained syslog-ng config. This getter (and all that follow) should be called
        after self.generate_mdsd_omsagent_syslog_config() is called.
        The return value should be appended to /etc/syslog-ng.conf.
        The appended file (/etc/syslog-ng.conf) should be processed so that
        the '%SYSLOG_PORT%' pattern in the file is replaced with the assigned TCP port number.
        :rtype: str
        :return: syslog-ng config string
        """
        self.__throw_if_output_is_none(self._syslog_ng_config)
        return self._syslog_ng_config
