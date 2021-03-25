#!/usr/bin/env python
#
# VM Backup extension
#
# Copyright 2015 Microsoft Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Requires Python 2.7+
#

import inspect
import os
import sys
import io
import re

from inspect import ismethod
from time import sleep
from OSEncryptionState import OSEncryptionState
from CommandExecutor import ProcessCommunicator
from Common import CommonVariables, CryptItem


class PatchBootSystemState(OSEncryptionState):
    def __init__(self, context):
        super(PatchBootSystemState, self).__init__('PatchBootSystemState', context)
        self.root_partuuid = self._get_root_partuuid()
        self.context.logger.log("root_partuuid: " + str(self.root_partuuid))

    def should_enter(self):
        self.context.logger.log("Verifying if machine should enter patch_boot_system state")

        if not super(PatchBootSystemState, self).should_enter():
            return False

        self.context.logger.log("Performing enter checks for patch_boot_system state")

        if not os.path.exists('/dev/mapper/osencrypt'):
            return False

        return True

    def enter(self):
        if not self.should_enter():
            return

        self.context.logger.log("Entering patch_boot_system state")

        self.command_executor.Execute('systemctl restart lvm2-lvmetad', False)
        self.command_executor.Execute('pvscan', True)
        self.command_executor.Execute('vgcfgrestore -f /volumes.lvm rootvg', True)
        self.command_executor.Execute('cryptsetup luksClose osencrypt', True)

        self._find_bek_and_execute_action('_luks_open')

        self.unmount_lvm_volumes()

        # RHEL 7.8 does not seem to activate VG on close and open.
        # Hence, explicitly activating it.
        self.command_executor.Execute('vgchange -a y rootvg')
        self.command_executor.Execute('mount /dev/rootvg/rootlv /oldroot', True)
        self.command_executor.Execute('mount /dev/rootvg/varlv /oldroot/var', True)
        self.command_executor.Execute('mount /dev/rootvg/usrlv /oldroot/usr', True)
        self.command_executor.Execute('mount /dev/rootvg/tmplv /oldroot/tmp', True)
        self.command_executor.Execute('mount /dev/rootvg/homelv /oldroot/home', True)
        self.command_executor.Execute('mount /dev/rootvg/optlv /oldroot/opt', True)

        self.command_executor.Execute('mount /boot', False)
        # Try mounting /boot/efi for UEFI image support
        self.command_executor.Execute('mount /boot/efi', False)
        self.command_executor.Execute('mount --make-rprivate /', True)
        self.command_executor.Execute('mkdir /oldroot/memroot', True)
        self.command_executor.Execute('pivot_root /oldroot /oldroot/memroot', True)

        self.command_executor.ExecuteInBash('for i in dev proc sys boot; do mount --move /memroot/$i /$i; done', True)
        self.command_executor.ExecuteInBash('[ -e "/boot/luks" ]', True)

        try:
            self._modify_pivoted_oldroot()
        except Exception:
            self.command_executor.Execute('mount --make-rprivate /')
            self.command_executor.Execute('pivot_root /memroot /memroot/oldroot')
            self.command_executor.Execute('rmdir /oldroot/memroot')
            self.command_executor.ExecuteInBash('for i in dev proc sys boot; do mount --move /oldroot/$i /$i; done')

            raise
        else:
            self.command_executor.Execute('mount --make-rprivate /')
            self.command_executor.Execute('pivot_root /memroot /memroot/oldroot')
            self.command_executor.Execute('rmdir /oldroot/memroot')
            self.command_executor.ExecuteInBash('for i in dev proc sys boot; do mount --move /oldroot/$i /$i; done')

            extension_full_name = 'Microsoft.Azure.Security.' + CommonVariables.extension_name
            extension_versioned_name = 'Microsoft.Azure.Security.' + \
                CommonVariables.extension_name + '-' + CommonVariables.extension_version
            test_extension_full_name = CommonVariables.test_extension_publisher + \
                CommonVariables.test_extension_name
            test_extension_versioned_name = CommonVariables.test_extension_publisher + \
                CommonVariables.test_extension_name + '-' + CommonVariables.extension_version
            self.command_executor.Execute('/bin/cp -ax' +
                                          ' /var/log/azure/{0}'.format(extension_full_name) +
                                          ' /oldroot/var/log/azure/{0}.Stripdown'.format(extension_full_name))
            self.command_executor.ExecuteInBash('/bin/cp -ax' +
                                                ' /var/lib/waagent/{0}/config/*.settings.rejected'.format(extension_versioned_name) +
                                                ' /oldroot/var/lib/waagent/{0}/config'.format(extension_versioned_name))
            self.command_executor.ExecuteInBash('/bin/cp -ax' +
                                                ' /var/lib/waagent/{0}/status/*.status.rejected'.format(extension_versioned_name) +
                                                ' /oldroot/var/lib/waagent/{0}/status'.format(extension_versioned_name))
            self.command_executor.Execute('/bin/cp -ax' +
                                          ' /var/log/azure/{0}'.format(test_extension_full_name) +
                                          ' /oldroot/var/log/azure/{0}.Stripdown'.format(test_extension_full_name), suppress_logging=True)
            self.command_executor.ExecuteInBash('/bin/cp -ax' +
                                                ' /var/lib/waagent/{0}/config/*.settings.rejected'.format(test_extension_versioned_name) +
                                                ' /oldroot/var/lib/waagent/{0}/config'.format(test_extension_versioned_name), suppress_logging=True)
            self.command_executor.ExecuteInBash('/bin/cp -ax' +
                                                ' /var/lib/waagent/{0}/status/*.status.rejected'.format(test_extension_versioned_name) +
                                                ' /oldroot/var/lib/waagent/{0}/status'.format(test_extension_versioned_name), suppress_logging=True)
            # Preserve waagent log from pivot root env
            self.command_executor.Execute(
                '/bin/cp -ax /var/log/waagent.log /oldroot/var/log/waagent.log.pivotroot')
            self.command_executor.ExecuteInBash('/bin/cp -ax' +
                                                ' /var/lib/azure_disk_encryption_config/os_encryption_markers/*' +
                                                ' /oldroot/var/lib/azure_disk_encryption_config/os_encryption_markers/',
                                                True)
            self.command_executor.Execute(
                'touch /oldroot/var/lib/azure_disk_encryption_config/os_encryption_markers/PatchBootSystemState', True)
            self.command_executor.Execute('umount /boot')
            self.command_executor.Execute('umount /oldroot')
            self.command_executor.Execute('systemctl restart waagent')

            self.context.logger.log("Pivoted back into memroot successfully")

            self.unmount_lvm_volumes()

    def should_exit(self):
        self.context.logger.log("Verifying if machine should exit patch_boot_system state")

        return super(PatchBootSystemState, self).should_exit()

    def unmount_lvm_volumes(self):
        self.command_executor.Execute('swapoff -a', True)
        self.command_executor.Execute('umount -a')

        for mountpoint in ['/var', '/opt', '/tmp', '/home', '/usr']:
            if self.command_executor.Execute('mountpoint /oldroot' + mountpoint) == 0:
                self.unmount('/oldroot' + mountpoint)
            if self.command_executor.Execute('mountpoint ' + mountpoint) == 0:
                self.unmount(mountpoint)

        self.unmount_var()

    def unmount_var(self):
        unmounted = False

        while not unmounted:
            self.command_executor.Execute('systemctl stop NetworkManager')
            self.command_executor.Execute('systemctl stop rsyslog')
            self.command_executor.Execute('systemctl stop systemd-udevd')
            self.command_executor.Execute('systemctl stop systemd-journald')
            self.command_executor.Execute('systemctl stop systemd-hostnamed')
            self.command_executor.Execute('systemctl stop atd')
            self.command_executor.Execute('systemctl stop postfix')
            self.unmount('/var')

            sleep(3)

            if self.command_executor.Execute('mountpoint /var'):
                unmounted = True

    def unmount(self, mountpoint):
        if mountpoint != '/var':
            self.unmount_var()

        if self.command_executor.Execute("mountpoint " + mountpoint):
            return

        proc_comm = ProcessCommunicator()

        self.command_executor.Execute(command_to_execute="fuser -vm " + mountpoint,
                                      raise_exception_on_failure=True,
                                      communicator=proc_comm)

        self.context.logger.log("Processes using {0}:\n{1}".format(mountpoint, proc_comm.stdout))

        procs_to_kill = [p for p in proc_comm.stdout.split() if p.isdigit()]
        procs_to_kill = reversed(sorted(procs_to_kill))

        for victim in procs_to_kill:
            if int(victim) == os.getpid():
                self.context.logger.log("Restarting WALA before committing suicide")
                self.context.logger.log("Current executable path: " + sys.executable)
                self.context.logger.log("Current executable arguments: " + " ".join(sys.argv))

                # Kill any other daemons that are blocked and would be executed after this process commits
                # suicide
                self.command_executor.Execute('systemctl restart atd')

                os.chdir('/')
                with open("/delete-lock.sh", "w") as f:
                    f.write("rm -f /var/lib/azure_disk_encryption_config/daemon_lock_file.lck\n")

                self.command_executor.Execute('at -f /delete-lock.sh now + 1 minutes', True)
                self.command_executor.Execute('at -f /restart-wala.sh now + 2 minutes', True)
                self.command_executor.ExecuteInBash('pkill -f .*ForLinux.*handle.py.*daemon.*', True)

            if int(victim) == 1:
                self.context.logger.log("Skipping init")
                continue

            self.command_executor.Execute('kill -9 {0}'.format(victim))

        self.command_executor.Execute('telinit u', True)

        sleep(3)

        self.command_executor.Execute('umount ' + mountpoint, True)

    def _append_contents_to_file(self, contents, path):
        # Python 3.x strings are Unicode by default and do not use decode
        if sys.version_info[0] < 3:
            if isinstance(contents, str):
                contents = contents.decode('utf-8')

        with io.open(path, 'a') as f:
            f.write(contents)

    def _modify_pivoted_oldroot(self):
        self.context.logger.log("Pivoted into oldroot successfully")
        if not self.root_partuuid:
            self._modify_pivoted_oldroot_no_partuuid()
        else:
            boot_uuid = self._get_boot_uuid()
            self._modify_pivoted_oldroot_with_partuuid(self.root_partuuid, boot_uuid)

    def _modify_pivoted_oldroot_with_partuuid(self, root_partuuid, boot_uuid):
        # Copy the 91adeOnline directory to dracut/modules.d
        scriptdir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
        ademoduledir = os.path.join(scriptdir, '../../91adeOnline')
        self.command_executor.Execute('cp -r {0} /lib/dracut/modules.d/'.format(ademoduledir), True)

        # Change config so that dracut will force add the dm_crypt kernel module
        self._append_contents_to_file('\nadd_drivers+=" dm_crypt "\n',
                                      '/etc/dracut.conf.d/ade.conf')

        # Change config so that dracut will add the fstab line to the initrd
        self._append_contents_to_file('\nadd_fstab+=" /lib/dracut/modules.d/91adeOnline/ade_fstab_line "\n',
                                      '/etc/dracut.conf.d/ade.conf')

        # Add the new kernel param
        additional_params = ["rd.luks.ade.partuuid={0}".format(root_partuuid),
                             "rd.luks.ade.bootuuid={0}".format(boot_uuid),
                             "rd.debug"]
        self._add_kernelopts(additional_params)

        # For clarity after reboot, we should also add the correct info to crypttab
        crypt_item = CryptItem()
        crypt_item.dev_path = os.path.join("/dev/disk/by-partuuid/", root_partuuid)
        crypt_item.mapper_name = CommonVariables.osmapper_name
        crypt_item.luks_header_path = "/boot/luks/osluksheader"
        self.crypt_mount_config_util.add_crypt_item(crypt_item)

        self._append_contents_to_file('\nadd_dracutmodules+=" crypt lvm"\n',
                                      '/etc/dracut.conf.d/ade.conf')

        self.command_executor.ExecuteInBash("/usr/sbin/dracut -f -v --kver `grubby --default-kernel | sed 's|/boot/vmlinuz-||g'`", True)

    def _modify_pivoted_oldroot_no_partuuid(self):
        scriptdir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
        ademoduledir = os.path.join(scriptdir, '../../91ade')
        dracutmodulesdir = '/lib/dracut/modules.d'
        udevaderulepath = os.path.join(dracutmodulesdir, '91ade/50-udev-ade.rules')

        proc_comm = ProcessCommunicator()

        self.command_executor.Execute('cp -r {0} /lib/dracut/modules.d/'.format(ademoduledir), True)

        udevadm_cmd = "udevadm info --attribute-walk --name={0}".format(self.rootfs_block_device)
        self.command_executor.Execute(command_to_execute=udevadm_cmd, raise_exception_on_failure=True, communicator=proc_comm)

        matches = re.findall(r'ATTR{partition}=="(.*)"', proc_comm.stdout)
        if not matches:
            raise Exception("Could not parse ATTR{partition} from udevadm info")
        partition = matches[0]
        sed_cmd = 'sed -i.bak s/ENCRYPTED_DISK_PARTITION/{0}/ "{1}"'.format(partition, udevaderulepath)
        self.command_executor.Execute(command_to_execute=sed_cmd, raise_exception_on_failure=True)

        self._append_contents_to_file('\nadd_drivers+=" fuse vfat nls_cp437 nls_iso8859-1"\n',
                                      '/etc/dracut.conf')
        self._append_contents_to_file('\nadd_dracutmodules+=" crypt"\n',
                                      '/etc/dracut.conf')

        self.command_executor.ExecuteInBash("/usr/sbin/dracut -f -v --kver `grubby --default-kernel | sed 's|/boot/vmlinuz-||g'`", True)
        self._add_kernelopts(["rd.debug"])

    def _luks_open(self, bek_path):
        self.command_executor.Execute('mount /boot')
        self.command_executor.Execute('cryptsetup luksOpen --header /boot/luks/osluksheader {0} osencrypt -d {1}'.format(self.rootfs_block_device,
                                                                                                                         bek_path),
                                      raise_exception_on_failure=True)

    def _find_bek_and_execute_action(self, callback_method_name):
        callback_method = getattr(self, callback_method_name)
        if not ismethod(callback_method):
            raise Exception("{0} is not a method".format(callback_method_name))

        bek_path = self.bek_util.get_bek_passphrase_file(self.encryption_config)
        callback_method(bek_path)
