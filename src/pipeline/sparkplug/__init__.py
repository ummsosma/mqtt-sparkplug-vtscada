"""Compiled Sparkplug B protobuf.

`sparkplug_b_pb2.py` is generated from the official schema (`sparkplug_b.proto`,
taken from eclipse-tahu) -- use the protobuf codec directly rather than a wrapper
library. Wrapper libraries have a habit of pinning paho-mqtt to 1.x, which breaks
the 2.x callback API used everywhere in this repo.

To regenerate:

    python -m grpc_tools.protoc --proto_path=. --python_out=. sparkplug_b.proto

The generated file validates the protobuf runtime version at import, so the
runtime must be >= the version stamped in it (see requirements.txt).
"""

from . import sparkplug_b_pb2 as spb  # noqa: F401

__all__ = ["spb"]
