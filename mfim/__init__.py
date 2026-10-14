"""Mount File — a file manager that works *on top of* disk images.

The package is split so the filesystem layer never depends on the GUI:

    mfim.partition   MBR / EBR-chain / GPT parsing + filesystem sniffing (pure Python)
    mfim.rawio       raw image reading and transactional sector patching
    mfim.vfs         Virtual File System facade over pytsk3 (read path)
    mfim.fatwrite    optional write path for FAT via mtools
    mfim.tree_model  Qt-independent lazy tree data layer
    mfim.gui         PyQt6 front-end (tree + hex viewer)
    mfim.cli         headless command line front-end
"""

__version__ = "0.1.0"
__all__ = ["partition", "rawio", "vfs", "fatwrite", "tree_model", "util"]
