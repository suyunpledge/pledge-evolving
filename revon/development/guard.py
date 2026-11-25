"""Compatibility import for the development editor; strict reads remain default."""
from revon.file_revisions import FileRevisionGuard, READS, WRITES

__all__ = ["FileRevisionGuard", "READS", "WRITES"]
