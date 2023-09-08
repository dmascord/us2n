from us2n import server
import rp2
import time

print('Press BOOTSEL to abort...')

aborted = False
for i in range(50):
    if rp2.bootsel_button() == 1:
        aborted = True
        break
    time.sleep(0.1)

if not aborted:
    print('Starting server...')
    server().serve_forever()
else:
    print('Aborted')
    raise KeyboardInterrupt
