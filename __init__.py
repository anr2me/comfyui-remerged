"""
Custom nodes for AI video APIs.

Remerged (https://remerged.ai) is built in and works out of the box.  Any
other service that describes itself with an OpenAPI 3.1 document can be added
with the "Remerged API Provider" node, which reads the document and works out
the endpoints, the request field names, the response fields and the status
words for itself.

Install by copying this folder into ``ComfyUI/custom_nodes/`` and restarting
ComfyUI.  See README.md for the API key setup.
"""

from .remerged_nodes.nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

__version__ = "1.1.0"
