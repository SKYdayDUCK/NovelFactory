"""Collect real consumer-group lag and pending counters around the populated fixture."""
from functools import wraps
import json
import os
from pathlib import Path
from novel_factory.infrastructure import event_streams
samples=[]

def pytest_configure(config):
    original=event_streams.Streams.read_batch
    @wraps(original)
    def measured(self,key,group,worker,count,idle):
        rows=original(self,key,group,worker,count,idle)
        stream=event_streams.STREAMS[key]
        summaries=self.client.xinfo_groups(stream)
        for found in summaries:
            name=found.get('name')
            if isinstance(name,bytes):
                name=name.decode()
            if name==group:
                samples.append({"key":key,"consumer_group":group,"read_count":len(rows[0]),"reclaimed_count":rows[1],"pending_after_read_before_ack":self.client.xpending(stream,group)["pending"],"lag_after_read":found.get("lag"),"stream_length":self.client.xlen(stream)})
        return rows
    event_streams.Streams.read_batch=measured

def pytest_sessionfinish(session,exitstatus):
    Path(os.environ["P59_REDIS_LAG"]).write_text(json.dumps({"exit":int(exitstatus),"observation":"after read, before this worker ack. Other consumers may advance concurrently.","samples":samples},indent=2)+"\n")
