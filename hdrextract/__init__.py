"""hdrextract - analyse smartphone HDR / depth / gain-map photos as image layers.

This package powers two CLI tools:

* ``scripts/extract_ultrahdr_layers.py`` - Android Ultra HDR JPEG -> layers
* ``scripts/extract_heic_aux_layers.py``  - Apple/iPhone HEIC -> layers

It is intentionally an *analysis* tool: the goal is to make the hidden
contents (gain map, depth, auxiliary items, metadata) visible as separate
images, not to perform colour-accurate HDR editing.
"""

__version__ = "0.1.0"
