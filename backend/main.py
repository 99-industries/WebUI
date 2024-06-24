from fastapi import FastAPI, WebSocket, Request, Form, WebSocketDisconnect, HTTPException, Depends, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import psutil
import asyncio
import libvirt
from fastapi.middleware.cors import CORSMiddleware
from xml.etree import ElementTree as ET
import re
import os
from string import ascii_lowercase
import subprocess
import distro
import requests
import pam
from jose import JWTError, jwt
from datetime import datetime, timedelta
import humanize
import json
import pwd
import grp
import shutil
import storage_manager
from notifications import NotificationManager, NotificationType
import vm_backups
from docker_manager import Templates, Containers, Networks, Images, General, DockerManagerException
from settings import SettingsManager, Setting, OvmfPath, SettingsException
from host_manager import libvirt_connection, SystemInfo, HostManagerException
import vm_manager


origins = ["*"]

app = FastAPI()
# app.mount("/static", StaticFiles(directory="static"), name="static")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SECRET_KEY = "secret!"
ALGORITHM = "HS256"


system_status = 'running'
libvirt_conn = libvirt_connection.connection
system_info = SystemInfo()
notification_manager = NotificationManager()
vm_backup_manager = vm_backups.BackupJobManager()
dockerTemplates = Templates()
dockerContainers = Containers()
dockerNetworks = Networks()
dockerImages = Images()
dockerGeneral = General()
settings_manager = SettingsManager()

# check if the user is authenticated
def check_auth(request: Request):
    try:
        token = request.headers['Authorization'].split(" ")[1]
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("username")
        if username is None:
            raise HTTPException(status_code=401, detail="Invalid authentication token")
        # check expiration
        expires = payload.get("exp")
        if expires is None:
            raise HTTPException(status_code=401, detail="Invalid authentication token")
        expires_datetime = datetime.utcfromtimestamp(expires)
        if datetime.utcnow() > expires_datetime:
            raise HTTPException(status_code=401, detail="Authentication token expired")
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid authentication token")

# check auth by token
def check_auth_token(token):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("username")
        if username is None:
            return False
        expires = payload.get("exp")
        if expires is None:
            return False
        expires_datetime = datetime.utcfromtimestamp(expires)
        if datetime.utcnow() > expires_datetime:
            return False
        return True
    except JWTError:
        return False

def getvmstate(uuid):
    domain = libvirt_conn.lookupByUUIDString(uuid)
    state, reason = domain.state()
    if state == libvirt.VIR_DOMAIN_NOSTATE:
        dom_state = 'NOSTATE'
    elif state == libvirt.VIR_DOMAIN_RUNNING:
        dom_state = 'Running'
    elif state == libvirt.VIR_DOMAIN_BLOCKED:
        dom_state = 'Blocked'
    elif state == libvirt.VIR_DOMAIN_PAUSED:
        dom_state = 'Paused'
    elif state == libvirt.VIR_DOMAIN_SHUTDOWN:
        dom_state = 'Shutdown'
    elif state == libvirt.VIR_DOMAIN_SHUTOFF:
        dom_state = 'Shutoff'
    elif state == libvirt.VIR_DOMAIN_CRASHED:
        dom_state = 'Crashed'
    elif state == libvirt.VIR_DOMAIN_PMSUSPENDED:
        dom_state = 'Pmsuspended'
    else:
        dom_state = 'unknown'
    return dom_state

def getvmresults():
    domains = libvirt_conn.listAllDomains(0)
    if len(domains) != 0:
        results = []
        for domain in domains:
            dom_name = domain.name()
            dom_uuid = domain.UUIDString()
            dom_state = getvmstate(dom_uuid)
            vmXml = domain.XMLDesc(0)
            root = ET.fromstring(vmXml)
            vcpus = root.find('vcpu').text

            vnc_state = False
            if domain.isActive() == True:
                graphics = root.find('./devices/graphics')
                if graphics != None:
                    port = graphics.get('port')
                    if port != None:
                        vnc_state = True

            dom_memory_min = storage_manager.convertSizeUnit(size=vmmemory(dom_uuid).current()[0], from_unit="KB", mode="tuple")
            dom_memory_max = storage_manager.convertSizeUnit(size=vmmemory(dom_uuid).current()[1], from_unit="KB", mode="tuple")

            dom_autostart = False
            if domain.autostart() == 1:
                dom_autostart = True
            result = {
                "uuid": dom_uuid,
                "name": dom_name,
                "memory_min": dom_memory_min[0],
                "memory_min_unit": dom_memory_min[1],
                "memory_max": dom_memory_max[0],
                "memory_max_unit": dom_memory_max[1],
                "vcpus": vcpus,
                "state": dom_state,
                "VNC": vnc_state,
                "autostart": dom_autostart,
            }
            results.append(result)
    else:
        results = None
    return results


class vmmemory():
    def __init__(self, uuid):
        self.domain = libvirt_conn.lookupByUUIDString(uuid)

    def current(self):
        maxmem = self.domain.info()[1]
        minmem = self.domain.info()[2]
        return [minmem, maxmem]

    def edit(self, minmem, minmemunit, maxmem, maxmemunit):
        maxmem = storage_manager.convertSizeUnit(size=maxmem, from_unit=maxmemunit, to_unit="KB", mode="int")
        minmem = storage_manager.convertSizeUnit(size=minmem, from_unit=minmemunit, to_unit="KB", mode="int")

        if minmem > maxmem:
            return ("Error: minmemory can't be bigger than maxmemory")

        else:
            vmXml = self.domain.XMLDesc(0)
            try:
                currentminmem = (
                    re.search("<currentMemory unit='KiB'>[0-9]+</currentMemory>", vmXml).group())
                currentmaxmem = (
                    re.search("<memory unit='KiB'>[0-9]+</memory>", vmXml).group())
                try:
                    output = vmXml
                    output = output.replace(
                        currentmaxmem, "<memory unit='KiB'>" + str(maxmem) + "</memory>")
                    output = output.replace(
                        currentminmem, "<currentMemory unit='KiB'>" + str(minmem) + "</currentMemory>")
                    try:
                        libvirt_conn.defineXML(output)
                        return ('Succeed')
                    except libvirt.libvirtError as e:
                        return (f'Error:{e}')
                except:
                    return ("failed to replace minmemory and/or maxmemory!")
            except:
                return ("failed to find minmemory and maxmemory in xml!")


class storage():
    def __init__(self, domain_uuid):
        self.domain_uuid = domain_uuid
        self.domain = libvirt_conn.lookupByUUIDString(domain_uuid)
        self.vmXml = self.domain.XMLDesc(0)

    def get(self):
        tree = ET.fromstring(self.vmXml)
        disks = tree.findall('./devices/disk')
        disklist = []
        for index, i in enumerate(disks):
            disktype = i.get('type')
            devicetype = i.get('device')
            drivertype = i.find('./driver').get('type')
            bootorderelem = i.find('boot')
            if bootorderelem != None:
                bootorder = bootorderelem.get('order')
            else:
                bootorder = None

            source = i.find('./source')
            sourcefile = None
            sourcedev = None
            if source != None:
                sourcefile = source.get('file')
                sourcedev = source.get('dev')

            target = i.find('./target')
            busformat = target.get('bus')
            targetdev = target.get('dev')
            

            readonlyelem = i.find('./readonly')
            if readonlyelem != None:
                readonly = True
            else:
                readonly = False
            disknumber = index
            xml = ET.tostring(i).decode()
            disk = {
                "number": disknumber,
                "type": disktype,
                "devicetype": devicetype,
                "drivertype": drivertype,
                "busformat": busformat,
                "sourcefile": sourcefile,
                "sourcedev": sourcedev,
                "targetdev": targetdev,
                "readonly": readonly,
                "bootorder": bootorder,
                "xml": xml
            }
            disklist.append(disk)
        return disklist

    def getxml(self, disknumber):
        return self.get()[int(disknumber)]["xml"]

    def add_xml(self, disktype, targetbus, devicetype, drivertype, sourcefile=None, sourcedev=None, bootorder=None):
        tree = ET.fromstring(self.vmXml)
        disks = tree.findall('./devices/disk')

        # get last used target bus
        for i in disks:
            target = i.find('./target')
            if targetbus == "sata" or targetbus == "scsi" or targetbus == "usb":
                if target.get('dev').startswith("sd"):
                    busformat = target.get('dev')
                    LastUsedTargetDev = busformat.replace("sd", "")
            elif targetbus == "virtio":
                if target.get('dev').startswith("vd"):
                    busformat = target.get('dev')
                    LastUsedTargetDev = busformat.replace("vd", "")

        # check which bus is free
        try:
            index = ascii_lowercase.index(LastUsedTargetDev)+1
        except NameError:
            index = 0
        if targetbus == "sata" or targetbus == "scsi" or targetbus == "usb":
            FreeTargetDev = "sd" + ascii_lowercase[index]
        elif targetbus == "virtio":
            FreeTargetDev = "vd" + ascii_lowercase[index]

        # create boot order string
        bootorderstring = ""
        if bootorder != None:
            bootorderstring = f"<boot order='{str(bootorder)}'/>"

        source_file_string = ""
        source_dev_string = ""
        if disktype == "file":
            source_file_string = f"<source file='{sourcefile}'/>"
        elif disktype == "block":
            source_dev_string = f"<source dev='{sourcedev}'/>"
        else:
            return
        # add the disk to xml
        self.diskxml = f"""<disk type='{disktype}' device='{devicetype}'>
        <driver name='qemu' type='{drivertype}'/>
        {source_file_string if disktype == "file" else ''}
        {source_dev_string if disktype == "block" else ''}
        <target dev='{FreeTargetDev}' bus='{targetbus}'/>
        {bootorderstring}
        </disk>"""

        self.add_xml_to_vm()
        return self.diskxml

    def add_xml_to_vm(self):
        self.domain.attachDeviceFlags(self.diskxml, libvirt.VIR_DOMAIN_AFFECT_CONFIG)

    def createnew(self, directory, disksize, disksizeunit, disktype, diskbus):
        disksize = storage_manager.convertSizeUnit(size=int(disksize), from_unit=disksizeunit, to_unit="B", mode="int")
        available_disk_number = len(self.get())
        disk_path = os.path.join(directory, f"{self.domain.name()}-{available_disk_number}.{disktype}")
        try:
            subprocess.check_output(["qemu-img", "create", "-f", disktype, disk_path, f"{disksize}B"])
        except subprocess.CalledProcessError as e:
            raise Exception(f"Error: Creating disk failed with error: {e}")
        self.add_xml(
            disktype="file",
            devicetype="disk",
            targetbus=diskbus,
            drivertype=disktype,
            sourcefile=disk_path,
        )


class create_vm():
    def __init__(self, name, machine_type, bios_type, mem_min, mem_min_unit, mem_max, mem_max_unit, disk=False, disk_size=None, disk_size_unit=None, disk_type=None, disk_bus=None, disk_location=None, iso=False, iso_location=None, network=False, network_source=None, network_model=None, ovmf_name=None):
        self.name = name
        self.machine_type = machine_type
        self.bios_type = bios_type
        self.min_mem_unit = mem_min_unit
        self.max_mem_unit = mem_max_unit
        self.mem_min = storage_manager.convertSizeUnit(size=int(mem_min), from_unit=mem_min_unit, to_unit="KB", mode='int')
        self.mem_max =storage_manager.convertSizeUnit(size=int(mem_max), from_unit=mem_max_unit, to_unit="KB", mode='int')
        self.disk = disk
        self.disk_size = disk_size
        self.disk_size_unit = disk_size_unit
        self.disk_type = disk_type
        self.disk_bus = disk_bus
        self.disk_location = disk_location
        self.iso = iso
        self.iso_location = iso_location
        self.network = network
        self.network_source = network_source
        self.network_model = network_model
        if ovmf_name:
            self.ovmf_path = settings_manager.get_ovmf_path(ovmf_name).path
            self.ovmf_string = f"<loader readonly='yes' type='pflash'>{self.ovmf_path}</loader>"
        self.qemu_path = settings_manager.get_setting("qemu_path").value
        self.networkstring = ""
        if self.network:
            self.networkstring = f"<interface type='network'><source network='{libvirt_conn.networkLookupByUUIDString(self.network_source).name()}'/><model type='{self.network_model}'/></interface>"
        
        self.createisoxml = ""
        if self.iso:
            self.createisoxml = f"""<disk type='file' device='cdrom'>
                            <driver name='qemu' type='raw'/>
                            <source file='{iso_location}'/>
                            <target dev='sda' bus='sata'/>
                            <boot order='2'/>
                            "<readonly/>
                            </disk>"""
        self.creatediskxml = ""
        if self.disk:
            disk_size = storage_manager.convertsize.convertSizeUnit(size=int(disk_size), from_unit=self.disk_size_unit, to_unit="B", mode='int')
            disk_volume_name = f"{self.name}-0.{self.disk_type}"
            disk_location = os.path.join(self.disk_location, disk_volume_name)
            try:
                subprocess.check_output(["qemu-img", "create", "-f", self.disk_type, disk_location, f"{disk_size}B"])
            except subprocess.CalledProcessError as e:
                raise Exception(f"Error: Creating disk failed with error: {e}")

            self.creatediskxml = f"""<disk type='file' device='disk'>
                            <driver name='qemu' type='{self.disk_type}'/>
                            <source file='{disk_location}'/>
                            <target dev='{"vda" if self.disk_bus == "virtio" else "sdb"}' bus='{self.disk_bus}'/>
                            <boot order='1'/>
                            </disk>"""

    def windows(self, version):
        self.tpmxml = f"""<tpm model='tpm-tis'>
        <backend type='emulator' version='2.0'/>
        </tpm>"""
        self.xml = f"""<domain type='kvm'>
        <name>{self.name}</name>
        <metadata>
            <libosinfo:libosinfo xmlns:libosinfo="http://libosinfo.org/xmlns/libvirt/domain/1.0">
            <libosinfo:os id="http://microsoft.com/win/{version}"/>
            </libosinfo:libosinfo>
        </metadata>
        <memory unit='KiB'>{self.mem_max}</memory>
        <currentMemory unit='KiB'>{self.mem_min}</currentMemory>
        <vcpu>2</vcpu>
        <os>
            <type arch='x86_64' machine='{self.machine_type}'>hvm</type>
            {self.ovmf_string if self.bios_type == "ovmf" else ""}
        </os>
        <features>
            <acpi/>
            <apic/>
            <hyperv mode='custom'>
            <relaxed state='on'/>
            <vapic state='on'/>
            <spinlocks state='on' retries='8191'/>
            </hyperv>
            <vmport state='off'/>
        </features>
        <cpu mode='host-model' check='partial'/>
        <devices>
            <emulator>{self.qemu_path}</emulator>
            {self.networkstring}
            {self.createisoxml}
            {self.creatediskxml}
            <graphics type='vnc' port='-1'/>
            <video>
            <model type='virtio'/>
            </video>
            <input type='tablet' bus='usb'/>
            # if version == "11", then add xml device
            {self.tpmxml if version == "11" else ""}
        </devices>
        </domain>"""
        return self.xml

    def macos(self, version):
        self.xml = f"""<domain type='kvm' xmlns:qemu='http://libvirt.org/schemas/domain/qemu/1.0'>
        <name>{self.name}</name>
        <memory unit='KiB'>{self.mem_max}</memory>
        <currentMemory unit='KiB'>{self.mem_min}</currentMemory>
        <vcpu>2</vcpu>
        <os>
            <type arch='x86_64' machine='{self.machine_type}'>hvm</type>
            {self.ovmf_string if self.bios_type == "ovmf" else ""}
        </os>
        <features>
            <acpi/>
            <apic/>
        </features>
        <cpu mode='host-passthrough' check='none' migratable='on'>
            <topology sockets='1' dies='1' cores='2' threads='1'/>
            <cache mode='passthrough'/>
        </cpu>
        <clock offset='localtime'>
            <timer name='rtc' tickpolicy='catchup'/>
            <timer name='pit' tickpolicy='delay'/>
            <timer name='hpet' present='no'/>
            <timer name='tsc' present='yes' mode='native'/>
        </clock>
        <devices>
            <emulator>{self.qemu_path}</emulator>
            {self.networkstring}
            {self.createisoxml}
            {self.creatediskxml}
            <serial type='pty'>
                <target type='isa-serial' port='0'>
                    <model name='isa-serial'/>
                </target>
            </serial>
            <console type='pty'>
                <target type='serial' port='0'/>
            </console>
            <channel type='unix'>
                <target type='virtio' name='org.qemu.guest_agent.0'/>
            </channel>
            <graphics type='vnc' port='-1'/>
            <video>
                <model type='vga' vram='65536' heads='1' primary='yes'/>
            </video>
            <input type='tablet' bus='usb'/>
            <memballoon model='none'/>
        </devices>
        <qemu:commandline>
        <qemu:arg value='-global'/>
        <qemu:arg value='ICH9-LPC.acpi-pci-hotplug-with-bridge-support=off'/>
        <qemu:arg value='-device'/>
        <qemu:arg value='isa-applesmc,osk=ourhardworkbythesewordsguardedpleasedontsteal(c)AppleComputerInc'/>
        <qemu:arg value='-cpu'/>
        {"<qemu:arg value='Cascadelake-Server,vendor=GenuineIntel'/>" if float(version) >= 13 else "<qemu:arg value='host,vendor=GenuineIntel'/>"}
    </qemu:commandline>
        </domain>"""
        return self.xml

    def linux(self):
        self.xml = f"""<domain type='kvm'>
        <name>{self.name}</name>
        <metadata>
            <libosinfo:libosinfo xmlns:libosinfo="http://libosinfo.org/xmlns/libvirt/domain/1.0">
            </libosinfo:libosinfo>
        </metadata>
        <memory unit='KiB'>{self.mem_max}</memory>
        <currentMemory unit='KiB'>{self.mem_min}</currentMemory>
        <vcpu>2</vcpu>
        <os>
            <type arch='x86_64' machine='{self.machine_type}'>hvm</type>
            {self.ovmf_string if self.bios_type == "ovmf" else ""}
        </os>
        <features>
            <acpi/>
            <apic/>
            <hyperv mode='custom'>
            <relaxed state='on'/>
            <vapic state='on'/>
            <spinlocks state='on' retries='8191'/>
            </hyperv>
            <vmport state='off'/>
        </features>
        <cpu mode='host-model' check='partial'/>
        <devices>
            <emulator>{self.qemu_path}</emulator>
            {self.networkstring}
            {self.createisoxml}
            {self.creatediskxml}
            <graphics type='vnc' port='-1'/>
            <video>
            <model type='virtio'/>
            </video>
            <input type='tablet' bus='usb'/>
            <channel type='unix'>
                <target type='virtio' name='org.qemu.guest_agent.0'/>
            </channel>
            <rng model='virtio'>
                <backend model='random'>/dev/urandom</backend>
            </rng>
        </devices>
        </domain>"""
        return self.xml
    def create(self):
        libvirt_conn.defineXML(self.xml)


def getGuestMachineTypes():
    capabilities = libvirt_conn.getCapabilities()
    root = ET.fromstring(capabilities)
    machine_types = []
    for arch in root.findall('.//arch[@name="x86_64"]'):
        for machine in arch.findall('machine'):
            machine_types.append(machine.text)
    # filter to only pc-i440fx and pc-q35
    machine_types = [x for x in machine_types if x.startswith('pc-i440fx') or x.startswith('pc-q35')]
    machine_types.sort()
    return machine_types

@app.get("/")
def index():
    return FileResponse("templates/index.html")

### Websockets ###
@app.websocket("/notifications")
async def websocket_endpoint(websocket: WebSocket, token: str):
    await websocket.accept()
    notifications_list = None
    try:
        if check_auth_token(token):
            notifications_list = [x.json for x in notification_manager.get_notifications()]
            await websocket.send_json({"type": "notifications_init", "data": notifications_list})
        while True:
            if check_auth_token(token):
                new_notifications_list = notification_manager.get_notifications()
                if notifications_list != new_notifications_list:
                    notifications_list = new_notifications_list
                    await websocket.send_json({"type": "notifications", "data": [x.json for x in notifications_list]})
                await asyncio.sleep(1)
            else:
                await websocket.send_json({"type": "auth_error"})
                await websocket.close()
                break
    except WebSocketDisconnect:
        pass

@app.websocket("/dashboard")
async def websocket_endpoint(websocket: WebSocket, token: str):
    await websocket.accept()
    try:
        if check_auth_token(token):
            cpu_name = system_info.cpu_model
            mem_total = storage_manager.convertSizeUnit(psutil.virtual_memory().total, from_unit="B", to_unit="GB", round_state=True, round_to=2)
            os_name = distro.name(pretty=True)
            uptime = humanize.precisedelta(datetime.now() - datetime.fromtimestamp(psutil.boot_time()), minimum_unit="minutes", format="%0.0f")
            await websocket.send_json({"type": "dashboard_init", "data": {"cpu_name": cpu_name, "mem_total": mem_total, "os_name": os_name, "uptime": uptime}})
        while True:
            if check_auth_token(token):
                cpu_percent = int(psutil.cpu_percent())
                cpu_thread_data = psutil.cpu_percent(interval=1, percpu=True)
                mem_used = storage_manager.convertSizeUnit(psutil.virtual_memory().used, from_unit="B", to_unit="GB", round_state=True, round_to=2)
                message = {"cpu_percent": cpu_percent, "cpu_thread_data": cpu_thread_data, "mem_used": mem_used}
                await websocket.send_json({"type": "dashboard", "data": message})
                await asyncio.sleep(1)
            else:
                await websocket.send_json({"type": "auth_error"})
                await websocket.close()
                break
    except WebSocketDisconnect:
        pass

@app.websocket("/vmdata")
async def websocket_endpoint(websocket: WebSocket, token: str):
    await websocket.accept()
    vm_list = None
    try:
        while True:
            if check_auth_token(token):
                # only send new data if the vm list has changed
                new_vm_list = getvmresults()
                if vm_list == None or vm_list != new_vm_list:
                    vm_list = new_vm_list
                    await websocket.send_json({"type": "vmdata", "data": vm_list})
                await asyncio.sleep(1)
            else:
                await websocket.send_json({"type": "auth_error"})
                await websocket.close()
                break
    except WebSocketDisconnect:
        pass

@app.websocket("/downloadiso")
async def websocket_endpoint(websocket: WebSocket, token: str):
    await websocket.accept()
    if check_auth_token(token):
        data = await websocket.receive_json()
        url = data["url"]
        filename = data["fileName"]
        directory = data["directory"]
        filepath = os.path.join(directory, filename)

        # use websocket events to send progress and errors
        # downloadISOError: on error
        # downloadISOProgress: on progress
        # downloadISOComplete: on complete

        if (os.path.isfile(filepath)):
            await websocket.send_json({"event": "downloadISOError", "message": f"{filename} already exists in {directory}"})
            return
        
        try:
            response = requests.get(url, stream=True)
            if response.status_code != 200:
                await websocket.send_json({"event": "downloadISOError", "message": f"Response code: {response.status_code}"})
                return
            try:
                total_size = int(response.headers.get('content-length'))
            except TypeError as e:
                websocket.send_json({"event": "downloadISOError", "message": f"Content-Length not found in response headers. Error: {e}"})
                return
            chunk_size = 1000

            with open(filepath, 'wb') as f:
                percentage = 0
                for index, data in enumerate(response.iter_content(chunk_size)):
                    prev_percentage = percentage
                    percentage = round(index * chunk_size / total_size * 100)
                    if prev_percentage != percentage:
                        await websocket.send_json({"event": "downloadISOProgress", "percentage": percentage})
                        if percentage == 100:
                            print("download complete")
                            await websocket.send_json({"event": "downloadISOComplete", "message": ["ISO Download Complete", f"ISO File: {filename}", f"Directory: {directory}"]})
                    f.write(data)
                    await asyncio.sleep(0) # allow the websocket to send the message before continuing
        except Exception as e:
            await websocket.send_json({"event": "downloadISOError", "message": f"Error: {e}"})
    else:
        await websocket.send_json({"event": "auth_error"})
        await websocket.close()


@app.get('/api/no-auth/hostname')
async def get_hostname(request: Request):
    return JSONResponse(content={"hostname": system_info.hostname})

@app.get('/api/no-auth/system-status')
async def get_system_status(request: Request):
    global system_status
    return system_status

@app.post('/api/login')
async def login(request: Request):
    data = await request.json()
    username = data['username']
    password = data['password']

    if not username:
        return HTTPException(status_code=400, detail="Username is required")
    if not password:
        return HTTPException(status_code=400, detail="Password is required")
    
    if pam.authenticate(username, password):
        expire_time_seconds = int(settings_manager.get_setting("login_token_expire").value)
        expires_delta = timedelta(seconds=expire_time_seconds)
        expire = datetime.utcnow() + expires_delta
        token = jwt.encode({"username": username, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM, )
        print("auth success")
        return JSONResponse(content={"access_token": token})
    else:
        return HTTPException(status_code=401, detail="Invalid username or password")


### API/VM-MANAGER ###
@app.get('/api/vm-manager/{action}')
async def get_vm_manager(request: Request, action: str, username: str = Depends(check_auth)):
    if action == "running":
        domainList = []
        for domain in libvirt_conn.listAllDomains():
            if domain.isActive():
                domainList.append({
                    "name": domain.name(),
                    "uuid": domain.UUIDString(),
                })
        return domainList
    elif action == "all":
        return getvmresults()
    else:
        return JSONResponse(content={"error": "Invalid action"})

@app.post('/api/vm-manager/{action}')
async def post_vm_manager(request: Request, action: str, username: str = Depends(check_auth)):
    if action == "create":
        form_data = await request.form()
        name = form_data.get('name')
        os = form_data.get('os')
        machine_type = form_data.get('machine_type')
        bios_type = form_data.get('bios_type')
        ovmf_name = None
        if bios_type == "ovmf":
            ovmf_name = form_data.get('ovmf_name')
            print("ovmf_name: " + ovmf_name)
        min_mem = form_data.get('memory_min')
        mim_mem_unit = form_data.get('memory_min_unit')
        max_mem = form_data.get('memory_max')
        max_mem_unit = form_data.get('memory_max_unit')
        disk = True
        disk_size = form_data.get('disk_size')
        disk_size_unit = form_data.get('disk_size_unit')
        disk_type = form_data.get('disk_type')
        disk_bus = form_data.get('disk_bus')
        disk_location = form_data.get('disk_location')
        iso = True
        cdrom_location = form_data.get('cdrom_location')
        network = True
        network_source = form_data.get('network_source')
        network_model = form_data.get('network_model')

        print("name: " + name)
        print("os: " + os)
        print("machine_type: " + machine_type)
        print("bios_type: " + bios_type)
        print("min_mem: " + min_mem)
        print("mim_mem_unit: " + mim_mem_unit)
        print("max_mem: " + max_mem)
        print("max_mem_unit: " + max_mem_unit)
        print("disk: " + str(disk))
        print("disk_size: " + disk_size)
        print("disk_size_unit: " + disk_size_unit)
        print("disk_type: " + disk_type)
        print("disk_bus: " + disk_bus)
        print("disk_location: " + disk_location)
        print("iso: " + str(iso))
        print("cdrom_location: " + cdrom_location)
        print("network: " + str(network))
        print("network_source: " + network_source)
        print("network_model: " + network_model)

        try:
            vm = create_vm(name=name, machine_type=machine_type, bios_type=bios_type, mem_min=min_mem, mem_min_unit=mim_mem_unit, mem_max=max_mem, mem_max_unit=max_mem_unit, disk=disk,
                        disk_size=disk_size, disk_size_unit=disk_size_unit, disk_type=disk_type, disk_bus=disk_bus, disk_location=disk_location, iso=iso, iso_location=cdrom_location, network=network, network_source=network_source, network_model=network_model, ovmf_name=ovmf_name)
            if os == "Microsoft Windows 11":
                vm.windows(version="11")
            elif os == "Microsoft Windows 10":
                vm.windows(version="10")
            elif os == "Microsoft Windows 8.1":
                vm.windows(version="8.1")
            elif os == "Microsoft Windows 8":
                vm.windows(version="8")
            elif os == "Microsoft Windows 7":
                vm.windows(version="7")
            elif os == "macOS 10.15 Catalina":
                vm.macos(version="10.15")
            elif os == "macOS 11 Big Sur":
                print("macOS 11 Big Sur")
                print(vm.macos(version="11"))
            elif os == "macOS 12 Monterey":
                vm.macos(version="12")
            elif os == "macOS 13 Ventura":
                vm.macos(version="13")
            elif os == "Linux":
                print("Creating new linux vm")
                vm.linux()
            else:
                raise HTTPException(status_code=404, detail="OS not supported")
            vm.create()
            return
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

#### API/VM-MANAGER-ACTIONS ####
@app.get('/api/vm-manager/{vmuuid}/{action}')
async def get_vm_manager_actions(request: Request, vmuuid: str, action: str, username: str = Depends(check_auth)):
    if action == "xml":
        try:
            return { "xml": vm_manager.VirtualMachine(vm_uuid=vmuuid).vm_xml}
        except vm_manager.VmManagerException as e:
            raise HTTPException(status_code=500, detail=str(e))
    elif action == "disk-data":
        try:
            return vm_manager.VirtualMachine(vm_uuid=vmuuid).vm_disk_devices
        except vm_manager.VmManagerException as e:
            raise HTTPException(status_code=500, detail=str(e))
    elif action == "log":
        try:
            return vm_manager.VirtualMachine(vm_uuid=vmuuid).get_vm_log()
        except vm_manager.VmManagerException as e:
            raise HTTPException(status_code=500, detail=str(e))

    elif action == "data":
        try:
            return vm_manager.VirtualMachine(vm_uuid=vmuuid).json
        except vm_manager.VmManagerException as e:
            raise HTTPException(status_code=500, detail=str(e))

    else:
        raise HTTPException(status_code=404, detail="Action not found")

@app.post('/api/vm-manager/{vmuuid}/{action}')
async def post_vm_manager_actions(request: Request, vmuuid: str, action: str, username: str = Depends(check_auth)):
    if action == "start":
        try:
            vm_manager.VirtualMachine(vm_uuid=vmuuid).start_vm()
        except vm_manager.VmManagerException as e:
            raise HTTPException(status_code=500, detail=str(e))

    elif action == "stop":
        try:
            vm_manager.VirtualMachine(vm_uuid=vmuuid).stop_vm()
        except vm_manager.VmManagerException as e:
            raise HTTPException(status_code=500, detail=str(e))

    elif action == "forcestop":
        try:
            vm_manager.VirtualMachine(vm_uuid=vmuuid).stop_vm(force=True)
        except vm_manager.VmManagerException as e:
            raise HTTPException(status_code=500, detail=str(e))

    elif action == "remove":
        try:
            vm_manager.VirtualMachine(vm_uuid=vmuuid).remove_vm()
        except vm_manager.VmManagerException as e:
            raise HTTPException(status_code=500, detail=str(e))

    elif action.startswith("edit"):
        data = await request.json()
        action = action.replace("edit-", "")
        # edit-xml
        if action == "xml":
            xml = data['xml']
            try:
                vm_manager.VirtualMachine(vm_uuid=vmuuid).set_vm_xml(xml)
            except vm_manager.VmManagerException as e:
                raise HTTPException(status_code=500, detail=str(e))

        # edit-name
        elif action == "name":
            name = data['name']
            try:
                vm_manager.VirtualMachine(vm_uuid=vmuuid).set_vm_name(name)
            except vm_manager.VmManagerException as e:
                raise HTTPException(status_code=500, detail=str(e))

        # edit-autostart
        elif action == "autostart":
            autostart = data['autostart']
            try:
                vm_manager.VirtualMachine(vm_uuid=vmuuid).set_vm_autostart(autostart)
            except vm_manager.VmManagerException as e:
                raise HTTPException(status_code=500, detail=str(e))

        # edit-cpu
        elif action == "cpu":
            # model = data['cpu_model']
            # vcpu = str(data['vcpu'])
            # current_vcpu = str(data['current_vcpu'])
            # custom_topology = data['custom_topology']
            # sockets = str(data['topology_sockets'])
            # dies = str(data['topology_dies'])
            # cores = str(data['topology_cores'])
            # threads = str(data['topology_threads'])
            # vm_xml = ET.fromstring(domain.XMLDesc(0))
            # # set cpu model
            # cpu_elem  = vm_xml.find('cpu')
            # cpu_elem.set('mode', model)
            # # remove migratable from cpu element
            # if cpu_elem.attrib.get('migratable') != None:
            #     cpu_elem.attrib.pop('migratable')
            

            # if custom_topology:
            #     # new dict for topology
            #     topologyelem = vm_xml.find('cpu/topology')
            #     if topologyelem != None:
            #         topologyelem.set('sockets', sockets)
            #         topologyelem.set('dies', dies)
            #         topologyelem.set('cores', cores)
            #         topologyelem.set('threads', threads)
            #     else:
            #         topologyelem = ET.Element('topology')
            #         topologyelem.set('sockets', sockets)
            #         topologyelem.set('dies', dies)
            #         topologyelem.set('cores', cores)
            #         topologyelem.set('threads', threads)
            #         vm_xml.find('cpu').append(topologyelem)
            
            # vm_xml.find('vcpu').text = vcpu
            # if current_vcpu != vcpu:
            #     vm_xml.find('vcpu').attrib['current'] = current_vcpu 
            # vm_xml = ET.tostring(vm_xml).decode()
            # try:
            #     domain.undefineFlags(4)
            #     domain = libvirt_conn.defineXML(vm_xml)
            #     return
            # except libvirt.libvirtError as e:
            #     raise HTTPException(status_code=500, detail=str(e))
            #TODO: do this in vm_manager module
            return

        # edit-memory
        elif action == "memory":
            memory_min = int(data['memory_min'])
            memory_min_unit = data['memory_min_unit']
            memory_max = int(data['memory_max'])
            memory_max_unit = data['memory_max_unit']
            try:
                vm_manager.VirtualMachine(vm_uuid=vmuuid).set_vm_memory(min_memory=memory_min, min_memory_unit=memory_min_unit, max_memory=memory_max, max_memory_unit=memory_max_unit, memory_backing=True)
            except vm_manager.VmManagerException as e:
                raise HTTPException(status_code=500, detail=str(e))

        # edit-network-action
        elif action.startswith("network"):
            action = action.replace("network-", "")
            if action == "add":
                source_network = data['sourceNetwork']
                model = data['networkModel']
                try:
                    vm_manager.VirtualMachine(vm_uuid=vmuuid).add_vm_network_device(source_network, model)
                except vm_manager.VmManagerException as e:
                    raise HTTPException(status_code=500, detail=str(e))

            elif action == "delete":
                number = data['number']
                try:
                    vm_manager.VirtualMachine(vm_uuid=vmuuid).remove_vm_network_device(number)
                except vm_manager.VmManagerException as e:
                    raise HTTPException(status_code=500, detail=str(e))
            else:
                raise HTTPException(status_code=404, detail="Action not found")

        # edit-disk-action
        elif action.startswith("disk"):
            action = action.replace("disk-", "")
            if action == "add":
                formDeviceType = data['deviceType']
                if formDeviceType == "cdrom" or formDeviceType == "existingvdisk":
                    formDeviceType = "disk" if formDeviceType == "existingvdisk" else "cdrom"
                    disk_path = data['volumePath']
                    disk_bus = data['diskBus']
                    try:
                        vm_manager.VirtualMachine(vm_uuid=vmuuid).add_vm_storage_device_from_file(source_file=disk_path, disk_bus=disk_bus, device_type=formDeviceType)
                    except vm_manager.VmManagerException as e:
                        raise HTTPException(status_code=500, detail=str(e))
                
                elif formDeviceType == "createvdisk":
                    directory = data['vdiskDirectory']
                    disksize = data['diskSize']
                    disksizeunit = data['diskSizeUnit']
                    diskType = data['diskDriverType']
                    diskBus = data['diskBus']
                    try:
                        storage(domain_uuid=vmuuid).createnew(
                            directory=directory,
                            disksize=disksize,
                            disksizeunit=disksizeunit,
                            disktype=diskType,
                            diskbus=diskBus
                        )
                        return
                    except libvirt.libvirtError as e:
                        raise HTTPException(status_code=500, detail=str(e))
                    
                elif formDeviceType == "block":
                    source_device = data['sourceDevice']
                    disk_bus = data['diskBus']
                    try:
                        vm_manager.VirtualMachine(vm_uuid=vmuuid).add_vm_storage_device_from_block_device(source_device=source_device, disk_bus=disk_bus)
                    except vm_manager.VmManagerException as e:
                        raise HTTPException(status_code=500, detail=str(e))

                else:
                    raise HTTPException(status_code=400, detail="Invalid device type")

            elif action == "delete":
                index = data['index']
                try:
                    vm_manager.VirtualMachine(vm_uuid=vmuuid).remove_vm_storage_device(index=index)
                except vm_manager.VmManagerException as e:
                    raise HTTPException(status_code=500, detail=str(e))
                
            else:
                raise HTTPException(status_code=404, detail="Action not found")

        # edit-usbhotplug-action
        elif action.startswith("usbhotplug"):
            action = action.replace("usbhotplug-", "")
            if action == "add":
                product_id = data['productid']
                vendor_id = data['vendorid']
                #TODO
                print("add usb hotplug", product_id, vendor_id)
                return
            elif action == "delete":
                product_id = data['productid']
                vendor_id = data['vendorid']
                print("delete usb hotplug", product_id, vendor_id)
                #TODO
                return
            else:
                raise HTTPException(status_code=404, detail="Action not found")

        # edit-usb-action
        elif action.startswith("usb"):
            action = action.replace("usb-", "")
            if action == "add":
                product_id = data['product_id']
                vendor_id = data['vendor_id']
                try:
                    vm_manager.VirtualMachine(vm_uuid=vmuuid).add_vm_usb_device(vendor_id=vendor_id, product_id=product_id)
                except vm_manager.VmManagerException as e:
                    raise HTTPException(status_code=500, detail=str(e))
            elif action == "delete":
                product_id = data['product_id']
                vendor_id = data['vendor_id']
                try:
                    vm_manager.VirtualMachine(vm_uuid=vmuuid).remove_vm_usb_device(vendor_id=vendor_id, product_id=product_id)
                except vm_manager.VmManagerException as e:
                    raise HTTPException(status_code=500, detail=str(e))
            else:
                raise HTTPException(status_code=404, detail="Action not found")

        # edit-pcie-action
        elif action.startswith("pcie"):
            action = action.replace("pcie-", "")
            if action == "add":
                domain = data['domain']
                bus = data['bus']
                slot = data['slot']
                function = data['function']
                custom_rom_file = data['customRomFile']
                rom_file = data['romFile']
                try:
                    vm_manager.VirtualMachine(vm_uuid=vmuuid).add_vm_pcie_device(domain=domain, bus=bus, slot=slot, function=function, rom_file=rom_file, custom_rom=custom_rom_file)
                except vm_manager.VmManagerException as e:
                    raise HTTPException(status_code=500, detail=str(e))
            
            elif action == "delete":
                index = data['index']       
                try:
                    vm_manager.VirtualMachine(vm_uuid=vmuuid).remove_vm_pcie_device(index=index)
                except vm_manager.VmManagerException as e:
                    raise HTTPException(status_code=500, detail=str(e))                
            else:
                raise HTTPException(status_code=404, detail="Action not found")

        # edit-graphics-action
        elif action.startswith("graphics"):
            action = action.replace("graphics-", "")
            if action == "add":
                graphics_type = data['type']
                try:
                    vm_manager.VirtualMachine(vm_uuid=vmuuid).add_vm_graphics_device(graphics_type=graphics_type)
                except vm_manager.VmManagerException as e:
                    raise HTTPException(status_code=500, detail=str(e))

            elif action == "delete":
                index = data['index']
                try:
                    vm_manager.VirtualMachine(vm_uuid=vmuuid).remove_vm_graphics_device(index=index)
                except vm_manager.VmManagerException as e:
                    raise HTTPException(status_code=500, detail=str(e))
            else:
                raise HTTPException(status_code=404, detail="Action not found")

        # edit-video-action
        elif action.startswith("video"):
            action = action.replace("video-", "")
            if action == "add":
                model_type = data['type'].lower()
                try:
                    vm_manager.VirtualMachine(vm_uuid=vmuuid).add_vm_video_device(model_type=model_type)
                    return
                except vm_manager.VmManagerException as e:
                    raise HTTPException(status_code=500, detail=str(e))

            elif action == "delete":
                index = data['index']
                try:
                    vm_manager.VirtualMachine(vm_uuid=vmuuid).remove_vm_video_device(index=index)
                    return
                except vm_manager.VmManagerException as e:
                    raise HTTPException(status_code=500, detail=str(e))
            else:
                raise HTTPException(status_code=404, detail="Action not found")

        # edit-sound-action
        elif action.startswith("sound"):
            action = action.replace("sound-", "")
            if action == "add":
                model = data['model']
                try:
                    vm_manager.VirtualMachine(vm_uuid=vmuuid).add_vm_sound_device(model=model)
                    return
                except vm_manager.VmManagerException as e:
                    raise HTTPException(status_code=500, detail=str(e))

            elif action == "delete":
                index = data['index']
                try:
                    vm_manager.VirtualMachine(vm_uuid=vmuuid).remove_vm_sound_device(index=index)
                    return
                except vm_manager.VmManagerException as e:
                    raise HTTPException(status_code=500, detail=str(e))
            else:
                raise HTTPException(status_code=404, detail="Action not found")
        else:
            raise HTTPException(status_code=404, detail="Action not found")
    else:
        raise HTTPException(status_code=404, detail="Action not found")

### API-NETWORKS ###
@app.get("/api/vm-networks")
async def api_networks_get():
    # get all networks from libvirt
    networks = libvirt_conn.listAllNetworks()
    # create empty list for networks
    networks_list = []
    # loop through networks
    for network in networks:
        # get network xml
        network_xml = ET.fromstring(network.XMLDesc(0))            
        # get network autostart
        network_autostart = network.autostart()
        # get network active
        network_active = network.isActive()
        if network_active == 1:
            network_active = True
        else:
            network_active = False
        # get network persistent
        network_persistent = network.isPersistent()
        # create network dict
        _network = {
            "uuid": network.UUIDString(),
            "name": network.name(),
            "active": network_active,
            "persistent": network_persistent,
            "autostart": network_autostart,
            
        }
        # append network dict to networks list
        networks_list.append(_network)
    return networks_list

@app.get("/api/docker-manager/templates/{id}")
async def api_docker_manager_template_get(id: int, username: str = Depends(check_auth)):
    return dockerTemplates.getTemplate(id=id)

@app.get("/api/docker-manager/templates")
async def api_docker_manager_templates_get(username: str = Depends(check_auth)):
    return dockerTemplates.getTemplates()

@app.get("/api/docker-manager/template-locations")
async def api_docker_manager_template_locations_get(username: str = Depends(check_auth)):
    template_locations = dockerTemplates.getLocations()
    return template_locations

@app.post("/api/docker-manager/template-locations/update")
async def api_docker_manager_template_locations_update_post(request: Request, username: str = Depends(check_auth)):
    data = await request.json()
    id = data['id']
    dockerTemplates.updateLocation(id=id)
    return

@app.put("/api/docker-manager/template-locations")
async def api_docker_manager_template_locations_put(request: Request, username: str = Depends(check_auth)):
    data = await request.json()
    id = data['id']
    name = data['name']
    url = data['url']
    branch = data['branch']
    dockerTemplates.editLocation(id=id, name=name, url=url, branch=branch)
    return

@app.delete("/api/docker-manager/template-locations")
async def api_docker_manager_template_locations_delete(request: Request, username: str = Depends(check_auth)):
    data = await request.json()
    id = data['id']
    dockerTemplates.deleteLocation(id=id)
    return

@app.post("/api/docker-manager/template-locations")
async def api_docker_manager_template_locations_post(request: Request, username: str = Depends(check_auth)):
    data = await request.json()
    name = data['name']
    url = data['url']
    branch = data['branch']
    dockerTemplates.addLocation(name=name, url=url, branch=branch)
    return

@app.get("/api/docker-manager/info")
async def api_docker_manager_info_get(username: str = Depends(check_auth)):
    return dockerGeneral.version()

@app.get("/api/docker-manager/images")
async def api_docker_manager_images_get(username: str = Depends(check_auth)):
    return dockerImages.getAll()

@app.post("/api/docker-manager/images/{action}")
async def api_docker_manager_images_post(request: Request, action: str, background_tasks: BackgroundTasks, username: str = Depends(check_auth)):
    data = await request.json()
    if action == "delete":
        for image in data['images']:
            image_name = image['name']
            image_tag = image['tag']
            dockerImages.remove(image_name + ":" + image_tag)
        return
    elif action == "pull":
        background_tasks.add_task(dockerImages.pull, data['image'])
        return
    else:
        raise HTTPException(status_code=404, detail="Action not found")
    
@app.get("/api/docker-manager/containers")
async def api_docker_manager_containers_get(username: str = Depends(check_auth)):
    return dockerContainers.getAll()

@app.get("/api/docker-manager/container/{container_id}")
async def api_docker_manager_container_get(container_id: str, username: str = Depends(check_auth)):
    return dockerContainers.get(id=container_id)

@app.post("/api/docker-manager/container/{id}/{action}")
async def api_docker_manager_containers_post(request: Request, id: str, action: str ,username: str = Depends(check_auth)):
    print(f"action: {action}, id: {id}")
    if action == "start":
        dockerContainers.start(id=id)
        return
    elif action == "stop":
        dockerContainers.stop(id=id)
        return
    elif action == "restart":
        dockerContainers.restart(id=id)
        return
    elif action == "delete":
        container_data = dockerContainers.get(id=id)
        if container_data['container_type'] == "unmanaged":
            dockerContainers.delete(id=id, api_only=True)
        else:
            dockerContainers.delete(id=id)
        return
    else:
        raise HTTPException(status_code=404, detail="action not found")
    
@app.post("/api/docker-manager/containers")
async def api_docker_manager_containers_create(request: Request, username: str = Depends(check_auth)):
    data = await request.json()
    print("Request to create container: ", data)
    action = data['action']
    print("Action: ", action)

    if action == "update":
        # Remove existing container: docker api and database
        id = data['id']
        dockerContainers.delete(id=id)

    # # Create a new container
    container_name = data['name']
    container_type = data['container_type']
    container_webui = data['webui'] if 'webui' in data else {"enable": False}
    container_config = data['config']
    try:
        dockerContainers.create(
            name=container_name,
            type=container_type,
            webui=container_webui,
            config=container_config
        )
    except DockerManagerException as e:
        raise HTTPException(status_code=500, detail=str(e))

    return

@app.get("/api/docker-manager/networks")
async def api_docker_manager_networks_get(username: str = Depends(check_auth)):
    return dockerNetworks.getAll()

@app.delete("/api/docker-manager/network/{id}")
async def api_docker_manager_networks_delete(id: str, username: str = Depends(check_auth)):
    dockerNetworks.delete(id=id)
    return

### api-host-power###
@app.post("/api/host/power/{powermsg}")
async def api_host_power_post(powermsg: str, username: str = Depends(check_auth)):
    if powermsg == "shutdown":
        shutdown_result = subprocess.run(
            ["shutdown", "-h", "now"], capture_output=True, text=True)
        if shutdown_result.returncode == 0:
            return
        else:
            raise HTTPException(status_code=500, detail=shutdown_result.stdout)
    elif powermsg == "reboot":
        global system_status
        system_status = "rebooting"
        reboot_result = subprocess.run(
            ["reboot"], capture_output=True, text=True)
        if reboot_result.returncode == 0:
            return
        else:
            system_status = "running"
            raise HTTPException(status_code=500, detail=reboot_result.stdout)
    else:
        raise HTTPException(status_code=404, detail="power action not found")

### API-STORAGE ###
@app.get("/api/storage/raid-manager")
async def api_host_storage_raid_get(username: str = Depends(check_auth)):
    try:
        return storage_manager.raid_manager.get()
    except storage_manager.StorageManagerException as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/storage/raid-manager/{action}")
async def api_host_storage_raid_post(request: Request, action: str, username: str = Depends(check_auth)):
    data = await request.json()
    if action == "create":
        try:
            storage_manager.raid_manager.create(personality=data['level'], devices=data['devices'], filesystem=data['filesystem'])
            return
        except storage_manager.StorageManagerException as e:
            raise HTTPException(status_code=500, detail=str(e))
    elif action == "delete":
        try:
            storage_manager.raid_manager.delete(path=data['path'])
            return
        except storage_manager.StorageManagerException as e:
            raise HTTPException(status_code=500, detail=str(e))
    else:
        raise HTTPException(status_code=404, detail="action not found")

@app.get("/api/storage/disks")
async def api_host_storage_disks_get(username: str = Depends(check_auth)):
    try:
        return storage_manager.disk_manager.get()
    except storage_manager.StorageManagerException as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/storage/disks/disk/{action}")
async def api_host_storage_disks_post(request: Request, action: str, username: str = Depends(check_auth)):
    data = await request.json()
    if action == "wipe":
        try:
            storage_manager.disk_manager.wipeDisk(path=data['diskpath'])
            return
        except storage_manager.StorageManagerException as e:
            raise HTTPException(status_code=500, detail=str(e))
    else:
        raise HTTPException(status_code=404, detail="action not found")

@app.post("/api/storage/disks/partition/{action}")
async def api_host_storage_disks_partition_post(request: Request, action: str, username: str = Depends(check_auth)):
    data = await request.json()
    if action == "delete":
        try:
            storage_manager.disk_manager.deletePartition(disk=data['disk'], partition=data['partition'])
            return
        except storage_manager.StorageManagerException as e:
            raise HTTPException(status_code=500, detail=str(e))
    elif action == "create":
        try:
            storage_manager.disk_manager.createPartition(diskpath=data['diskpath'], fstype=data['fstype'])
            return
        except storage_manager.StorageManagerException as e:
            raise HTTPException(status_code=500, detail=str(e))
    elif action == "mount":
        try:
            storage_manager.disk_manager.mountPartition(uuid=data['partition'], mountpoint=data['mountpoint'])
            return
        except storage_manager.StorageManagerException as e:
            raise HTTPException(status_code=500, detail=str(e))
    elif action == "unmount":
        try:
            storage_manager.disk_manager.unmountPartition(uuid=data['partition'])
            return
        except storage_manager.StorageManagerException as e:
            raise HTTPException(status_code=500, detail=str(e))
    elif action == "format":
        try:
            storage_manager.disk_manager.formatPartition(path=data['partition'], fstype=data['fstype'])
            return
        except storage_manager.StorageManagerException as e:
            raise HTTPException(status_code=500, detail=str(e))
    else:
        raise HTTPException(status_code=404, detail="action not found")
    
@app.get("/api/storage/sharedfolders")
async def api_host_storage_sharedfolders_get(username: str = Depends(check_auth)):
    try:
        return storage_manager.shared_folders.get()
    except storage_manager.StorageManagerException as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.get("/api/storage/sharedfolders/availabledevices")
async def api_host_storage_sharedfolders_availabledevices_get(username: str = Depends(check_auth)):
    try:
        return storage_manager.shared_folders.getAvailableDevices()
    except storage_manager.StorageManagerException as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/api/storage/sharedfolders/{action}")
async def api_host_storage_sharedfolders_post(request: Request, action: str, username: str = Depends(check_auth)):
    data = await request.json()
    if action == "create":
        try:
            storage_manager.shared_folders.create(name=data['name'], target=data['target'])
            return
        except storage_manager.StorageManagerException as e:
            raise HTTPException(status_code=500, detail=str(e))
    elif action == "delete":
        try:
            storage_manager.shared_folders.remove(name=data['name'])
            return
        except storage_manager.StorageManagerException as e:
            raise HTTPException(status_code=500, detail=str(e))
    elif action == "smb-edit":
        name = data['name']
        smb_status = data['status']
        if smb_status == False:
            if storage_manager.shared_folders.getSmbShare(name=name) is not None:
                storage_manager.shared_folders.removeSMBShare(name=name)
            return
        else:
            if storage_manager.shared_folders.getSmbShare(name=name) is not None:
                storage_manager.shared_folders.removeSMBShare(name=name)
            smb_mode = data['mode']
            smb_path = data['path']
            if smb_mode == "PUBLIC":
                storage_manager.shared_folders.createSMBShare(name=name, path=smb_path, mode="PUBLIC")
            elif smb_mode == "PRIVATE":
                smb_users = data['users']
                users_list = []
                users_write_list = []
                users_read_list = []
                for user in smb_users:
                    users_list.append(user['name'])
                    if user['mode'] == "rw":
                        users_write_list.append(user['name'])
                        users_read_list.append(user['name'])
                    elif user['mode'] == "ro":
                        users_read_list.append(user['name'])
                storage_manager.shared_folders.createSMBShare(
                    name=name, 
                    path=smb_path, 
                    mode="PRIVATE",
                    users_list=users_list,
                    users_write_list=users_write_list,
                    users_read_list=users_read_list
                )
            elif smb_mode == "SECURE":
                smb_users = data['users']
                users_write_list = []
                for user in smb_users:
                    if user['mode'] == "rw":
                        users_write_list.append(user['name'])
                storage_manager.shared_folders.createSMBShare(
                    name=name, 
                    path=smb_path, 
                    mode="SECURE",
                    users_write_list=users_write_list
                )
                return
    else:
        raise HTTPException(status_code=404, detail="action not found")

@app.get("/api/host/system-info/{action}")
async def api_system_info_get(action: str, username: str = Depends(check_auth)):
    if action == "all":
        return {
            "motherboard": system_info.motherboard,
            "processor": system_info.cpu_model,
            "memory": system_info.memory_size,
            "os": system_info.os,
            "hostname": system_info.hostname,
            "linuxVersion": system_info.linux_kernel_version,
            "uptime": system_info.uptime,
        }
    elif action == "hostname":
        return {
            "hostname": system_info.hostname,
        }
    elif action == "guest-machine-types":
        return getGuestMachineTypes()
    else:
        raise HTTPException(status_code=404, detail="action not found")

@app.post("/api/host/system-info/hostname")
async def api_system_info_hostname_post(request: Request, username: str = Depends(check_auth)):
    data = await request.json()
    hostname = data['hostname']
    try:
        system_info.setHostname(hostname)
        return
    except HostManagerException as e:
        raise HTTPException(status_code=500, detail=str(e))
   
    
# API-SYSTEM-USERS
@app.get("/api/system/users")
async def api_system_users_get(username: str = Depends(check_auth)):
    users = []
    for user in pwd.getpwall():
        # list users with UID >= 1000 and <= 60000 or UID == 0
        if user.pw_uid >= 1000 and user.pw_uid <= 60000 or user.pw_uid == 0:
            smb_user = storage_manager.smbusers.lookup(name=user.pw_name)
            users.append({
                "name": user.pw_name,
                "smb_user": smb_user,
                "uid": user.pw_uid,
                "gid": user.pw_gid,
                "home": user.pw_dir,
                "shell": user.pw_shell,
                "groups": [group.gr_name for group in grp.getgrall() if user.pw_name in group.gr_mem],
            })
    return users

@app.post("/api/system/users/change-password")
async def api_system_users_change_password(request: Request, username: str = Depends(check_auth)):
    data = await request.json()
    username = data['username']
    password = data['password']
    try:
        subprocess.check_output(["passwd", username, "--stdin"], input=password.encode())
        return
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=e)
    
@app.post("/api/system/users/remove-user")
async def api_system_users_remove_user(request: Request, username: str = Depends(check_auth)):
    data = await request.json()
    username = data['username']
    try:
        subprocess.check_output(["userdel", username])
        return
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=e)

@app.post("/api/system/users/change-smb-password")
async def api_system_users_change_smb_password(request: Request, username: str = Depends(check_auth)):
    data = await request.json()
    username = data['username']
    password = data['password']
    try:
        storage_manager.smbusers.reset_password(username, password)
        return
    except storage_manager.StorageManagerException as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/system/users/remove-smb-user")
async def api_system_users_remove_smb_user(request: Request, username: str = Depends(check_auth)):
    data = await request.json()
    username = data['username']
    try:
        if storage_manager.smbusers.lookup(name=username) is not None:
            storage_manager.smbusers.delete(name=username)
        return
    except storage_manager.StorageManagerException as e:
        raise HTTPException(status_code=500, detail=str(e))

### API-SYSTEM-FILE-MANAGER ###
@app.post("/api/system/file-manager")
async def api_system_file_manager_get(request: Request, username: str = Depends(check_auth)):
    data = await request.json()
    path = data['path']
    if os.path.isdir(path):
        parent_dir = os.path.abspath(os.path.join(path, os.pardir))
        files = []
        if path != "/":
            files.append({
                "name": "..",
                "parentdir": parent_dir,
                "path": parent_dir,
                "type": "dirparent",
                "size": "",
                "permissions": "",
                "modified": "",
            })
        for file in os.listdir(path):
            file_path = os.path.join(path, file)
            file_type = "file"
            file_size = ""
            if os.path.isdir(file_path):
                file_type = "dir"
            else:
                # calculate size of file if path is not a directory. ConvertSizeUnit returns a tuple with the size and the unit
                file_size = storage_manager.convertSizeUnit(size=os.path.getsize(file_path), from_unit="B", mode="str")

            file_modified = datetime.fromtimestamp(os.path.getmtime(file_path)).strftime("%Y-%m-%d %H:%M:%S")
            file_permissions = oct(os.stat(file_path).st_mode)[-3:]


            files.append({
                "name": file,
                "path": file_path,
                "type": file_type,
                "size": file_size,
                "permissions": file_permissions,
                "modified": file_modified,
            })
        return { "list": files, "path": path }
    else:
        raise HTTPException(status_code=404, detail="Path not found")
    
@app.post("/api/system/file-manager/{action}")
async def api_system_file_manager_action(action: str, request: Request, username: str = Depends(check_auth)):
    data = await request.json()
    if action == "remove":
        path = data['path']
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path=path)
    elif action == "create-folder":
        name = data['name']
        path = data['path']
        new_path = os.path.join(path, name)
        os.makedirs(new_path)
    elif action == "rename":
        name = data['name']
        path = data['path']
        new_path = os.path.join(os.path.dirname(path), name)
        os.rename(path, new_path)
    elif action == "validate-path":
        path = data['path']
        # if directory, return dir, if file return file, if not found return not found
        if os.path.isdir(path):
            return JSONResponse(content={"type": "dir"})
        elif os.path.isfile(path):
            parent = os.path.dirname(path)
            return JSONResponse(content={"type": "file", "parent": parent})
        else:
            raise HTTPException(status_code=500, detail="not found")
    else:
        raise HTTPException(status_code=404, detail="Action not found")

### API-HOST-SYSTEM-DEVICES ###
@app.get("/api/host/system-devices/{devicetype}")
async def api_host_system_devices_get(devicetype: str, username: str = Depends(check_auth)):
    if devicetype == "pcie":
        return system_info.pcie_devices_json
    elif devicetype == "usb":
        return system_info.usb_devices_json
    else:
        raise HTTPException(status_code=404, detail="Device type not found")

### API-SETTINGS-ACTIONS ###
@app.get("/api/settings")
async def api_host_settings_get(username: str = Depends(check_auth)):
    try:
        return settings_manager.get_settings()
    except SettingsException as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/setting/{setting}")
async def api_host_settings_get(setting: str, username: str = Depends(check_auth)):
    try:
        if setting == "vnc":
            vnc_settings = { 
                "ip": settings_manager.get_setting("novnc_ip").value,
                "port": settings_manager.get_setting("novnc_port").value,
                "protocol": settings_manager.get_setting("novnc_protocol").value,
                "path": settings_manager.get_setting("novnc_path").value,
            }
            return vnc_settings
        else:
            return settings_manager.get_setting(setting)
    except SettingsException as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/setting/{setting}")
async def api_host_settings_post(request: Request, setting: str, username: str = Depends(check_auth)):
    data = await request.json()
    value = data['value']
    try:
        _setting = settings_manager.get_setting(setting)
        _setting.value = value
        settings_manager.update_setting(_setting)
        return
    except SettingsException as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/settings/ovmf-paths")
async def api_settings_ovmf_paths_get(username: str = Depends(check_auth)):
    try:
        return settings_manager.get_ovmf_paths()
    except SettingsException as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/settings/ovmf-paths/{name}")
async def api_settings_ovmf_paths_post(name: str, request: Request, username: str = Depends(check_auth)):
    data = await request.json()
    path = data['path']
    try:
        ovmfpath = OvmfPath(name=name, path=path)
        settings_manager.create_ovmf_path(ovmfpath)
        return
    except SettingsException as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.put("/api/settings/ovmf-paths/{name}")
async def api_settings_ovmf_paths_put(request: Request, name: str, username: str = Depends(check_auth)):
    data = await request.json()
    path = data['path']
    try:
        ovmfpath = OvmfPath(name=name, path=path)
        settings_manager.update_ovmf_path(ovmfpath)
        return
    except SettingsException as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.delete("/api/settings/ovmf-paths/{name}")
async def api_settings_ovmf_paths_delete(name: str, username: str = Depends(check_auth)):
    try:
        settings_manager.delete_ovmf_path(name=name)
        return
    except SettingsException as e:
        raise HTTPException(status_code=500, detail=str(e))

### API-NOTIFICATIONS ###
@app.get("/api/notifications")
async def api_notifications_get(username: str = Depends(check_auth)):
    return [notification.json for notification in notification_manager.get_notifications()]

@app.delete("/api/notifications/{id}")
async def api_notifications_delete(id: int, username: str = Depends(check_auth)):
    if id == -1:
        notification_manager.delete_all_notifications()
    else:
        notification = notification_manager.get_notification(id)
        if notification is not None:
            notification_manager.delete_notification(notification)
    return
