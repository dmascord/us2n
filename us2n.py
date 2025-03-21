# us2n.py

import json
import time
import select
import socket
import machine
import network
import sys

print_ = print
VERBOSE = 1
def print(*args, **kwargs):
    if VERBOSE:
        print_(*args, **kwargs)


def read_config(filename='us2n.json', obj=None, default=None):
    with open(filename, 'r') as f:
        config = json.load(f)
        if obj is None:
            return config
        return config.get(obj, default)


def parse_bind_address(addr, default=None):
    if addr is None:
        return default
    args = addr
    if not isinstance(args, (list, tuple)):
        args = addr.rsplit(':', 1)
    host = '' if len(args) == 1 or args[0] == '0' else args[0]
    port = int(args[1])
    return host, port

class RINGBUFFER:
    def __init__(self, size):
        self.data = bytearray(size)
        self.size = size
        self.index_put = 0
        self.index_get = 0
        self.index_rewind = 0
        self.wrapped = False

    def put(self, data):
        cur_idx = 0
        while cur_idx < len(data):
            min_idx = min(self.index_put+len(data)-cur_idx, self.size)
            self.data[self.index_put:min_idx] = data[cur_idx:min_idx-self.index_put+cur_idx]
            cur_idx += min_idx-self.index_put
            if self.index_get > self.index_put:
                self.index_get = max(min_idx+1, self.index_get)
                if self.index_get >= self.size:
                    self.index_get -= self.size
            self.index_put = min_idx
            if self.index_put == self.size:
                self.index_put = 0
                self.wrapped = True
                if self.index_get == 0:
                    self.index_get = 1

    def putc(self, value):
        next_index = (self.index_put + 1) % self.size
        self.data[self.index_put] = value
        self.index_put = next_index
        # check for overflow
        if self.index_get == self.index_put:
            self.index_get = (self.index_get + 1) % self.size
        return value

    def get(self, numbytes):
        data = bytearray()
        while len(data) < numbytes:
            start = self.index_get
            min_idx = min(self.index_get+numbytes-len(data), self.size)
            if self.index_put >= self.index_get:
                min_idx = min(min_idx, self.index_put)
            data.extend(self.data[start:min_idx])
            self.index_get = min_idx
            if self.index_get == self.size:
                self.index_get = 0
            if self.index_get == self.index_put:
                break
        return data

    def getc(self):
        if not self.has_data():
            return None  ## buffer empty
        else:
            value = self.data[self.index_get]
            self.index_get = (self.index_get + 1) % self.size
            return value

    def has_data(self):
        return self.index_get != self.index_put

    def rewind(self):
        if self.wrapped:
            self.index_get = (self.index_put+1) % self.size
        else:
            self.index_get = 0

def UART(config):
    config = dict(config)
    uart_type = config.pop('type') if 'type' in config.keys() else 'hw'
    port = config.pop('port')
    if uart_type == 'SoftUART':
        print('Using SoftUART...')
        uart = machine.SoftUART(machine.Pin(config.pop('tx')),machine.Pin(config.pop('rx')),timeout=config.pop('timeout'),timeout_char=config.pop('timeout_char'),baudrate=config.pop('baudrate'))
    else:
        print('Using HW UART...')
        uart = machine.UART(port)
        uart.init(**config)
    return uart


class Bridge:

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.uart = None
        self.uart_port = config['uart']['port']
        self.tcp = None
        self.address = parse_bind_address(config['tcp']['bind'])
        self.bind_port = self.address[1]
        self.client = None
        self.client_address = None
        self.ring_buffer = RINGBUFFER(16 * 1024)
        self.cur_line = bytearray()
        self.state = 'listening'
        self.menu_state = 'main'
        self.uart = UART(self.config['uart'])
        print('UART opened ', self.uart)
        print(self.config)

    def bind(self):
        tcp = socket.socket()
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    #    tcp.setblocking(False)
        tcp.bind(self.address)
        tcp.listen(5)
        print('Bridge listening at TCP({0}) for UART({1})'
              .format(self.bind_port, self.uart_port))
        self.tcp = tcp
        if 'ssl' in self.config:
            import ntptime
            ntptime.host = "pool.ntp.org"
            while True:
                try:
                    ntptime.settime()
                except OSError as e:
                    print(f"NTP synchronization failed, {e}")
                    time.sleep(15)
                    continue
                print(f"NTP synchronization succeeded, {time.time()}")
                print(time.gmtime())
                break

        return tcp

    def fill(self, fds):
        if self.uart is not None:
            fds.append(self.uart)
        if self.tcp is not None:
            fds.append(self.tcp)
        if self.client is not None:
            fds.append(self.client)
        return fds

    def recv(self, sock, n):
        if hasattr(sock, 'recv'):
            return sock.recv(n)
        else:
            # SSL-wrapped sockets don't have recv(), use read() instead
            # TODO: Read more than 1 byte? Probably needs non-blocking sockets
            return sock.read(1)

    def sendall(self, sock, bytes):
        if hasattr(sock, 'sendall'):
            return sock.sendall(bytes)
        else:
            # SSL-wrapped sockets don't have sendall(), use write() instead
            return sock.write(bytes)

    def handle(self, fd):
        if fd == self.tcp:
            self.close_client()
            self.open_client()
        elif fd == self.client:
            data = self.recv(self.client, 4096)
            if data:
                if self.state == 'enterpassword':
                    while len(data):
                        c = data[0:1]
                        data = data[1:]
                        if c == b'\n' or c == b'\r':
                            print("Received password {0}".format(self.password))
                            if self.password.decode('utf-8') == self.config['auth']['password']:
                                self.sendall(self.client, "\r\nAuthentication succeeded\r\n")
                                self.state = 'authenticated'
                                self.ring_buffer.rewind()
                                fd = self.uart # Send all uart data
                                break
                            else:
                                self.password = b""
                                self.sendall(self.client, "\r\nAuthentication failed\r\npassword: ")
                        else:
                                self.password += c
                if self.state == 'authenticated':
                    
                    if data == b"\xff\xf3": #break
                        self.uart.sendbreak()
                        print('sending Break signal')
                    elif data == b"\xff\xf6": #ayt
                        self.sendall(self.client,"\r\nI'm here\r\n")
                    elif data == b"\xff\xf4": #IP: interrupt process comes to a menu, maybe changing in future.
                        self.state = "inMenu"
                        self.menu_state = 'main'
                        data=''
                    else:
                        print('TCP({0})->UART({1}) {2}'.format(self.bind_port,
                                                           self.uart_port, data))
                        self.uart.write(data)

                if self.state == 'inMenu':
                    #menu for changing uart parameters :)
                    main_options = {b'a':'databits', b'b':'baudrate', b'c':'parity', b'd':'stop', b'e':'close'}
                    databit_options = { b'a':7, b'b':8, b'c':'main'}
                    baud_options={b'a':4800, b'b':9600, b'c':19200, b'd':38400, b'e':57600, b'f':115200, b'z':'main'}
                    parity_options={b'a':'None', b'b':"Even", b'c':"Odd", b'd':'main'}
                    stop_options = { b'a':1, b'b':2, b'c':'main'}
                    
                    def menutrace():
                        print('self state: {0}, self.menustate: {1}, current config: {2} {3} {4}, curr vel: {5}, termdata: {6}'.format(self.state,self.menu_state,str(self.config['uart']['bits']),str(self.config['uart']['parity']),str(self.config['uart']['stop']),str(self.config['uart']['baudrate']),data))
                    
                    def mainMenu():
                        menutrace()
                        self.sendall(self.client,b'\033[2J'+
                            "UART parameters menu:\r\n"+
                            "a) Data bits: "+ str(self.config['uart']['bits'])+"\r\n"+
                            "b) Baudrate: " + str(self.config['uart']['baudrate'])+"\r\n"+
                            "c) Parity: " + str(self.config['uart']['parity'])+"\r\n"+
                            "d) stop bits:" + str(self.config['uart']['stop'])+"\r\n"+
                            "e) exit\r\n"+
                            "please select an option: ")
                        
                    def dataBitMenu():
                        menutrace()
                        self.sendall(self.client,b'\033[2J'+
                            "databits parameters menu:\r\n"+
                            "actual -> "+str(self.config['uart']['bits'])+"\r\n"+
                            "a) 7 \r\n"+
                            "b) 8 \r\n"+
                            "c) exit\r\n"+
                            "please select an option: ")
                        
                    def baudMenu():
                        menutrace()
                        self.sendall(self.client,b'\033[2J'+
                            "baudrate parameters menu:\r\n"+
                            "actual -> "+str(self.config['uart']['baudrate'])+"\r\n"+
                            "a) 4800 \r\n"+
                            "b) 9600 \r\n"+
                            "c) 19200 \r\n"+
                            "d) 38400\r\n"+
                            "e) 57600\r\n"+
                            "z) exit"+
                            "please select an option: ")

                    def parityMenu():
                        menutrace()
                        self.sendall(self.client,b'\033[2J'+
                            "parity parameters menu:\r\n"+
                            "actual -> "+str(self.config['uart']['parity'])+"\r\n"+
                            "a) None \r\n"+
                            "b) Even \r\n"+
                            "c) Odd \r\n"+
                            "d) exit\r\n"+
                            "please select an option: ")

                    def stopMenu():
                        menutrace()
                        self.sendall(self.client,b'\033[2J'+
                            "stop bit parameters menu:\r\n"+
                            "actual -> "+str(self.config['uart']['stop'])+"\r\n"+
                            "a) 1 \r\n"+
                            "b) 2 \r\n"+
                            "c) exit\r\n"+
                            "please select an option: ")    
                        
                    if self.menu_state=='main':
                        if data==b'':
                            mainMenu()
                        else:
                            try:
                                self.menu_state = main_options[data]
                                data=b''
                            except:
                                mainMenu() 

                    if self.menu_state=='databits':
                        if data==b'':
                            dataBitMenu()
                        else:
                            try:
                                if databit_options[data] != 'main':
                                    self.config['uart']['bits']=databit_options[data]
                                    dataBitMenu()
                                else:
                                    self.menu_state=databit_options[data]
                                    mainMenu()
                            except:
                                dataBitMenu()

                    if self.menu_state=='baudrate':
                        if data==b'':
                            baudMenu()
                        else:
                            try:
                                if baud_options[data] != 'main':
                                    self.config['uart']['baudrate']=baud_options[data]
                                    baudMenu()
                                else:
                                    self.menu_state=baud_options[data]
                                    mainMenu()
                            except:
                                baudMenu()

                    if self.menu_state=='parity':
                        if data==b'':
                            parityMenu()
                        else:
                            try:
                                if parity_options[data] != 'main':
                                    self.config['uart']['parity']=parity_options[data]
                                    parityMenu()
                                else:
                                    self.menu_state=parity_options[data]
                                    mainMenu()
                            except:
                                parityMenu()
                    
                    if self.menu_state=='stop':
                        if data==b'':
                            stopMenu()
                        else:
                            try:
                                if stop_options[data] != 'main':
                                    self.config['uart']['stop']=stop_options[data]
                                    stopMenu()
                                else:
                                    self.menu_state=databit_options[data]
                                    mainMenu()
                            except:
                                stopMenu()        
                    
                    if self.menu_state=='close':
                        menutrace()
                        self.sendall(self.client,b'\033[2J')
                        #if new changes, save new data to config file
                        with open('us2n.json','r') as f:
                            excnf=json.loads(f.read())
                        for item in excnf['bridges']:
                            if item not in self.config:
                                print("found a new configuration {0}, resetting...".format(item))
                                with open('us2n.json','w') as f:
                                    excnf['bridges']=self.config
                                    json.dump(excnf,f)
                                #reset uart
                                #here is a problem i cant debug, making a soft reset
                                sys.exit()
                        #if not changes where made we go back to terminal.                        
                        self.menu_state = 'main'
                        self.state = 'authenticated'
                        data=b''
            else:
                print('Client ', self.client_address, ' disconnected')
                self.close_client()
        if fd == self.uart:
            data = self.uart.read(64)
            if data is not None:
                self.ring_buffer.put(data)
            if self.state == 'authenticated' and self.ring_buffer.has_data():
                data = self.ring_buffer.get(4096)
                print('UART({0})->TCP({1}) {2}'.format(self.uart_port,
                                                       self.bind_port, data))
                self.sendall(self.client, data)

    def close_client(self):
        if self.client is not None:
            print('Closing client ', self.client_address)
            self.client.close()
            self.client = None
            self.client_address = None
        self.state = 'listening'

    def open_client(self):
        self.client, self.client_address = self.tcp.accept()
        print('Accepted connection from ', self.client_address)
        if 'ssl' in self.config:
            import ussl
            import ubinascii
            print(time.gmtime())
            sslconf = self.config['ssl'].copy()
            for key in ['cadata', 'key', 'cert']:
                if key in sslconf:
                    with open(sslconf[key], "rb") as file:
                        sslconf[key] = file.read()
            # TODO: Setting CERT_REQUIRED produces MBEDTLS_ERR_X509_CERT_VERIFY_FAILED
            sslconf['cert_reqs'] = ussl.CERT_OPTIONAL
            self.client = ussl.wrap_socket(self.client, server_side=True, **sslconf)
        self.state = 'enterpassword' if 'auth' in self.config else 'authenticated'
        self.password = b""
        if self.state == 'enterpassword':
            self.sendall(self.client, "password: ")
            print("Prompting for password")

    def close(self):
        self.close_client()
        if self.tcp is not None:
            print('Closing TCP server {0}...'.format(self.address))
            self.tcp.close()
            self.tcp = None


class S2NServer:

    def __init__(self, config):
        self.config = config

    def report_exception(self, e):
        if 'syslog' in self.config:
            try:
                import usyslog
                import io
                import sys
                stringio = io.StringIO()
                sys.print_exception(e, stringio)
                stringio.seek(0)
                e_string = stringio.read()
                s = usyslog.UDPClient(**self.config['syslog'])
                s.error(e_string)
                s.close()
            except BaseException as e2:
                sys.print_exception(e2)

    def serve_forever(self):
        while True:
            config_network(self.config.get('wlan'), self.config.get('name'))
            try:
                self._serve_forever()
            except KeyboardInterrupt:
                print('Ctrl-C pressed. Bailing out')
                break
            except BaseException as e:
                import sys
                sys.print_exception(e)
                self.report_exception(e)
                time.sleep(1)
                print("Restarting")

    def bind(self):
        bridges = []
        for config in self.config['bridges']:
            bridge = Bridge(config)
            bridge.bind()
            bridges.append(bridge)
        return bridges

    def _serve_forever(self):
        bridges = self.bind()

        try:
            while True:
                fds = []
                for bridge in bridges:
                    bridge.fill(fds)
                rlist, _, xlist = select.select(fds, (), fds)
                if xlist:
                    print('Errors. bailing out')
                    break
                for fd in rlist:
                    for bridge in bridges:
                        bridge.handle(fd)
        finally:
            for bridge in bridges:
                bridge.close()


def config_lan(config, name):
    # For a board which has LAN
    pass


def config_wlan(config, name):
    if config is None:
        return None, None
    return (WLANStation(config.get('sta'), name),
            WLANAccessPoint(config.get('ap'), name))


def WLANStation(config, name):
    if config is None:
        return
    config.setdefault('connection_attempts', -1)
    essid = config['essid']
    password = config['password']
    attempts_left = config['connection_attempts']
    sta = network.WLAN(network.STA_IF)

    if not sta.isconnected():
        while not sta.isconnected() and attempts_left != 0:
            attempts_left -= 1
            sta.disconnect()
            sta.active(False)
            sta.active(True)
            sta.connect(essid, password)
            print('Connecting to WiFi...')
            n, ms = 20, 250
            t = n*ms
            while not sta.isconnected() and n > 0:
                time.sleep_ms(ms)
                n -= 1
        if not sta.isconnected():
            print('Failed to connect wifi station after {0}ms. I give up'
                  .format(t))
            return sta
    print('Wifi station connected as {0}'.format(sta.ifconfig()))
    return sta


def WLANAccessPoint(config, name):
    if config is None:
        return
    config.setdefault('essid', name)
    config.setdefault('channel', 11)
    config.setdefault('authmode', getattr(network,'AUTH_OPEN'))
    config.setdefault('hidden', False)
    ap = network.WLAN(network.AP_IF)
    
    if not ap.isconnected():
        ap.config(**config)
        ap.active(True)
        n, ms = 20, 250
        t = n * ms
        while not ap.active() and n > 0:
            time.sleep_ms(ms)
            n -= 1
        if not ap.active():
            print('Failed to activate wifi access point after {0}ms. ' \
                  'I give up'.format(t))
            return ap

    print('Wifi {0!r} connected as {1}'.format(ap.config('essid'),
                                               ap.ifconfig()))
    return ap


def config_network(config, name):
    config_lan(config, name)
    config_wlan(config, name)


def config_verbosity(config):
    global VERBOSE
    VERBOSE = config.setdefault('verbose', 1)
    for bridge in config.get('bridges'):
        if bridge.get('uart',{}).get('port',None)==0:
            VERBOSE = 0


def server(config_filename='us2n.json'):
    config = read_config(config_filename)
    VERBOSE = config.setdefault('verbose', 1)
    name = config.setdefault('name', 'Tiago-ESP32')
    config_verbosity(config)
    print(50*'=')
    print('Welcome to ESP8266/32 serial <-> tcp bridge\n')
    return S2NServer(config)
