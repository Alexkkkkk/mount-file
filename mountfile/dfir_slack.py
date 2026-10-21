# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License.
"""Read-only extraction of the final-cluster slack for a resolved file extent.

This module intentionally does not resolve a filesystem path, walk a directory,
or follow a FAT chain.  The caller must provide the absolute image offset of the
file's final cluster.  The helper only reads the bytes after the logical file
size in that cluster and reports the absolute image offset of those bytes.

A no-slack result is represented explicitly as ``SlackResult(b"", None)``.  In
particular, an empty file and a file whose size is an exact multiple of the
cluster size do not cause a read and do not pretend that a slack offset exists.
"""

from __future__ import annotations

import operator
import os
from typing import Any, NamedTuple, Optional, Union

ImageSource = Union[str, os.PathLike, bytes, bytearray, memoryview, Any]


class InvalidSlackBounds(ValueError):
    """Raised when a supplied extent or requested slack is outside the image."""


# A tuple keeps the public result limited to precisely the two requested
# values, while named fields make the result self-documenting.  ``offset`` is
# ``None`` when there is no final-cluster slack.
class SlackResult(NamedTuple):
    """Bytes and absolute offset for one final-cluster slack region."""

    bytes: bytes
    offset: Optional[int]

    @property
    def data(self) -> bytes:
        """Compatibility alias for callers that call the bytes ``data``."""

        return self.bytes

    @property
    def absolute_offset(self) -> Optional[int]:
        """Compatibility alias emphasizing that ``offset`` is image-absolute."""

        return self.offset

    @property
    def has_slack(self) -> bool:
        """Whether the result contains a non-empty slack region."""

        return bool(self.bytes)


# A descriptive alias is useful to callers that prefer the word ``region``.
SlackRegion = SlackResult
SlackBoundsError = InvalidSlackBounds


def _integer(value: Any, name: str, *, positive: bool = False) -> int:
    """Coerce an integer-like value while rejecting bools and invalid signs."""

    if isinstance(value, bool):
        raise TypeError("%s must be an integer, not bool" % name)
    try:
        result = operator.index(value)
    except TypeError as exc:
        raise TypeError("%s must be an integer" % name) from exc
    if result < 0 or (positive and result == 0):
        qualifier = "greater than zero" if positive else "non-negative"
        raise ValueError("%s must be %s" % (name, qualifier))
    return result


def _is_integer_like(value: Any) -> bool:
    """Return whether ``value`` is a non-bool integer accepted by ``index``."""

    if isinstance(value, bool):
        return False
    try:
        operator.index(value)
    except TypeError:
        return False
    return True


def _memoryview(source: Any) -> Optional[memoryview]:
    """Return a byte view for an in-memory image, or ``None`` otherwise."""

    if isinstance(source, (bytes, bytearray, memoryview)):
        try:
            return memoryview(source).cast("B")
        except (TypeError, ValueError) as exc:
            raise TypeError("image_file must expose a contiguous byte buffer") from exc
    return None


def _stream_size(stream: Any) -> int:
    """Find a seekable stream's size without leaving its position changed."""

    try:
        original = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
    except (AttributeError, OSError, TypeError) as exc:
        raise TypeError(
            "image_file must be a path, bytes-like value, or seekable binary file"
        ) from exc
    finally:
        try:
            stream.seek(original)
        except (UnboundLocalError, AttributeError, OSError, TypeError):
            pass
    return _integer(size, "image size")


def _read_stream_at(stream: Any, offset: int, length: int) -> bytes:
    """Read one exact range and restore the stream position."""

    try:
        original = stream.tell()
        stream.seek(offset, os.SEEK_SET)
        value = stream.read(length)
    except (AttributeError, OSError, TypeError) as exc:
        raise TypeError("image_file must be a seekable binary file") from exc
    finally:
        try:
            stream.seek(original)
        except (UnboundLocalError, AttributeError, OSError, TypeError):
            pass
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError("image_file must be opened in binary mode")
    data = bytes(value)
    if len(data) != length:
        raise InvalidSlackBounds("image ended before the requested slack region")
    return data


def _image_size(source: ImageSource) -> int:
    """Return the source size without opening any logical file path."""

    view = _memoryview(source)
    if view is not None:
        return len(view)
    if isinstance(source, (str, os.PathLike)):
        try:
            with open(os.fspath(source), "rb") as stream:
                return _stream_size(stream)
        except (OSError, TypeError) as exc:
            raise OSError("unable to open image_file for read-only access") from exc
    return _stream_size(source)


def _read_at(source: ImageSource, offset: int, length: int) -> bytes:
    """Read exactly ``length`` bytes from the supplied image source."""

    view = _memoryview(source)
    if view is not None:
        return view[offset : offset + length].tobytes()
    if isinstance(source, (str, os.PathLike)):
        try:
            with open(os.fspath(source), "rb") as stream:
                return _read_stream_at(stream, offset, length)
        except (OSError, TypeError) as exc:
            raise OSError("unable to open image_file for read-only access") from exc
    return _read_stream_at(source, offset, length)


def read_file_slack(
    image_file: ImageSource,
    last_cluster_offset: int,
    file_size: int,
    cluster_size: int,
) -> SlackResult:
    """Read the final-cluster slack for a caller-resolved file extent.

    Args:
        image_file: Raw image path, bytes-like image, or seekable binary stream.
        last_cluster_offset: Absolute image offset where the final file cluster
            begins.  This is supplied by the caller; it is never inferred here.
        file_size: Logical file size in bytes.
        cluster_size: Size in bytes of one cluster.

    Returns:
        ``SlackResult(slack_bytes, absolute_slack_offset)``.  For an empty or
        cluster-aligned file, returns ``SlackResult(b"", None)``.

    Raises:
        InvalidSlackBounds: If the final cluster is not wholly inside the image
            or the image ends before the requested slack bytes.
        TypeError, ValueError: If an argument has an invalid type or sign.
    """

    # Accept the same positional convenience as ``extract_slack`` while still
    # keeping the documented, image-first call signature.  This only swaps
    # unambiguous offset-first calls; it never attempts to resolve an extent.
    if _is_integer_like(image_file) and not _is_integer_like(cluster_size):
        image_file, last_cluster_offset, file_size, cluster_size = (
            cluster_size,
            image_file,
            last_cluster_offset,
            file_size,
        )

    last_cluster_offset = _integer(last_cluster_offset, "last_cluster_offset")
    file_size = _integer(file_size, "file_size")
    cluster_size = _integer(cluster_size, "cluster_size", positive=True)
    image_size = _image_size(image_file)

    # Validate the complete caller-supplied final cluster before deciding
    # whether its slack is empty.  This prevents malformed extents from being
    # silently accepted merely because the file happens to be aligned.
    if (
        last_cluster_offset > image_size
        or cluster_size > image_size - last_cluster_offset
    ):
        raise InvalidSlackBounds("last cluster is outside the image")

    remainder = file_size % cluster_size
    if file_size == 0 or remainder == 0:
        return SlackResult(b"", None)

    slack_offset = last_cluster_offset + remainder
    slack_length = cluster_size - remainder
    if slack_offset < last_cluster_offset or slack_length > image_size - slack_offset:
        raise InvalidSlackBounds("slack region is outside the image")

    data = _read_at(image_file, slack_offset, slack_length)
    if len(data) != slack_length:
        # The in-memory path is exact by construction; this also protects
        # callers from a source whose size changed between validation and read.
        raise InvalidSlackBounds("image ended before the requested slack region")
    return SlackResult(data, slack_offset)


def extract_slack(
    image_file: ImageSource,
    last_cluster_offset: int,
    file_size: int,
    cluster_size: int,
) -> SlackResult:
    """Alias for :func:`read_file_slack` using the common extraction spelling.

    The canonical argument order is ``(image_file, last_cluster_offset,
    file_size, cluster_size)``.  For compatibility with callers that mirror
    the requirement's value order, the positional form
    ``(last_cluster_offset, file_size, cluster_size, image_file)`` is also
    accepted and normalized without inferring any extent.
    """

    return read_file_slack(image_file, last_cluster_offset, file_size, cluster_size)


# Keep descriptive spellings available without adding alternate behavior.
extract_file_slack = extract_slack
read_slack = extract_slack
get_file_slack = extract_slack


__all__ = [
    "InvalidSlackBounds",
    "SlackBoundsError",
    "SlackRegion",
    "SlackResult",
    "extract_file_slack",
    "extract_slack",
    "get_file_slack",
    "read_file_slack",
    "read_slack",
]
