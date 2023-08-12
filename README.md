# micropython ESP8266/ESP32/Raspberry Pi Pico UART to TCP bridge

A micropython server running on an ESP8266/ESP32/Raspberry Pi Pico which acts as a bridge
between UART and TCP (LAN/WLAN).

## Installation

Follow steps to install *esptool* and *micropython for ESP8266/ESP32*.

For RPi Pico, follow *Getting started with Raspberry Pi Pico*.

Then...

* clone me, oh please, clone me!

```bash
$ git clone git@github.com/tiagocoutinho/us2n
```

### Configuration

* Create a file called `us2n.json` with a json configuration for Hardware UART:

```python

import json

config = {
    "name": "SuperESP32",
    "verbose": False,
    "wlan": {
        "sta": {
            "essid": "<name of your access point>",
            "password": "<password of your access point>",
        },
    },
    "bridges": [
        {
            "tcp": {
                "bind": ["", 8000],
            },
            "uart": {
                "port": 1,
                "baudrate": 9600,
                "bits": 8,
                "parity": None,
                "stop": 1,
            },
        },
    ],
}

with open('us2n.json', 'w') as f:
    json.dump(config, f)

```

**Note: if you are running us2n on an ESP32, specifying rx and tx pins is supported on hardware UART.**

* Or, create a file called `us2n.json` with a json configuration for SoftUART:

```python

import json

config = {
    "name": "SuperESP32",
    "verbose": False,
    "wlan": {
        "sta": {
            "essid": "<name of your access point>",
            "password": "<password of your access point>",
        },
    },
    "bridges": [
        {
            "tcp": {
                "bind": ["", 8000],
            },
            "uart": {
                "type": "SoftUART",
                "tx": 12,
                "rx": 14,
                "timeout": 20,
                "timeout_char": 10,
                "port": 1,
                "baudrate": 9600,
                "bits": 8,
                "parity": None,
                "stop": 1,
            },
        },
    ],
}

with open('us2n.json', 'w') as f:
    json.dump(config, f)

```

You can also enable password authentication on connection by adding this under a bridge:

```

"auth": {
    "password": "<password prompted on connection>",
},

```

### Running

* Include in your `main.py`:

```python
import us2n
server = us2n.server()
server.serve_forever()
```

An example `main.py` for the RPi Pico is included as `picomain.py`, which
waits for 5 seconds and unless BOOTSEL is pressed, the server is started.

* Load the newly created `us2n.json` to your MCU (ESP8266/ESP32/RPi Pico)

* Load `us2n.py` to your MCU

* Load `main.py` to your MCU

* Press reset

The server board should be ready to accept requests in a few seconds.


## Usage

Now, if, for example, your MCU UART is connected to a SCPI device,
you can, from any PC:

```bash
$ nc <MCU Wifi IP> 8000
*IDN?
ACME Instruments, C4, 122393-2, 10-0-1

```
* Using socat to bridge back to a tty
```bash
$ socat pty,link=$HOME/dev/ttyV0,b9600,waitslave tcp:<MCU Wifi IP>:8000
```
* Connect to the virtual tty with miniterm.py
```bash
$ miniterm.py dev/ttyV0 9600
```
That's all folks!
