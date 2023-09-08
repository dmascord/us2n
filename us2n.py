# us2n.py

import json
import time
import select
import socket
import machine
import network

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
                                break
                            else:
                                self.password = b""
                                self.sendall(self.client, "\r\nAuthentication failed\r\npassword: ")
                        else:
                                self.password += c
                if self.state == 'authenticated':
                    print('TCP({0})->UART({1}) {2}'.format(self.bind_port,
                                                           self.uart_port, data))
                    self.uart.write(data)
            else:
                print('Client ', self.client_address, ' disconnected')
                self.close_client()
        elif fd == self.uart:
            data = self.uart.read()
            if data is not None:
                if self.state == 'authenticated':
                    print('UART({0})->TCP({1}) {2}'.format(self.uart_port,
                                                           self.bind_port, data))
                    self.sendall(self.client, data)
                else:
                    print("Ignoring UART data, not authenticated")

    def close_client(self):
        if self.client is not None:
            print('Closing client ', self.client_address)
            self.client.close()
            self.client = None
            self.client_address = None
        if self.uart is not None:
#            self.uart.deinit()
            self.uart = None

    def open_client(self):
        self.client, self.client_address = self.tcp.accept()
        print('Accepted connection from ', self.client_address)
        self.uart = UART(self.config['uart'])
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
        print('UART opened ', self.uart)
        print(self.config)
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
    config.setdefault('authmode',
                      getattr(network,'AUTH_' +
                              config.get('authmode', 'OPEN').upper()))
    config.setdefault('hidden', False)
#    config.setdefault('dhcp_hostname', name)
    ap = network.WLAN(network.AP_IF)
    if not ap.isconnected():
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

#    ap.config(**config)
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
        if bridge.get('uart', {}).get('port', None) == 0:
            VERBOSE = 0


def server(config_filename='us2n.json'):
    config = read_config(config_filename)
    VERBOSE = config.setdefault('verbose', 1)
    name = config.setdefault('name', 'Tiago-ESP32')
    config_verbosity(config)
    print(50*'=')
    print('Welcome to ESP8266/32 serial <-> tcp bridge\n')
    return S2NServer(config)
