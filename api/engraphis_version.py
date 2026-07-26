"""Engraphis build/runtime contract from the component manifest."""

from api.component_manifest import component_manifest

_ENGRAPHIS = component_manifest()["engraphis"]
ENGRAPHIS_VERSION = str(_ENGRAPHIS["version"])
ENGRAPHIS_COMMIT = str(_ENGRAPHIS["commit"])
