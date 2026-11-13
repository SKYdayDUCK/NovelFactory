#!/usr/bin/env python
"""CLI shim for a fixture-only regression with real Redis processes on changing ports."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
root=Path(os.environ["PORT_SWITCH_STATE"])
def start():
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port=port_socket.getsockname()[1]
    old=json.loads((root/"state").read_text()) if (root/"state").exists() else {}
    if old.get("port")==port:
        return start()
    process=subprocess.Popen(["redis-server","--port",str(port),"--bind","127.0.0.1","--save","","--appendonly","no","--dir",str(root)],
                             stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    (root/"state").write_text(json.dumps({"port":port,"pid":process.pid}))
    (root/"history").open("a").write(str(port)+"\n")
args=sys.argv[1:]
if args[0]=="inspect":
    print("private-test-image")
elif args[0]=="run":
    start()
    print("private-test-container")
elif args[0]=="port":
    print("127.0.0.1:"+str(json.loads((root/"state").read_text())["port"]))
elif args[0]=="kill" or args[0]=="rm":
    old=json.loads((root/"state").read_text())
    os.kill(old["pid"],signal.SIGKILL)
    for _ in range(100):
        with socket.socket() as check:
            if check.connect_ex(("127.0.0.1",old["port"])):
                break
        time.sleep(.01)
elif args[0]=="start":
    start()
else:
    sys.exit("unsupported test command")
