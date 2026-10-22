"""CLICK PLUS PLC -> MQTT -> durable queue -> Sparkplug B -> VTScada historian.

Two long-running services and no shared mutable state:

    collector.py  MQTT in  -> TimescaleDB durable queue (rows are 'pending')
    bridge.py     queue out -> Sparkplug B -> VTScada, confirmed by readback

Everything else in the repo is a tool or a test.
"""

__version__ = "1.0.0"
