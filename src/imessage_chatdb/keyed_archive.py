"""Minimal reader for ``NSKeyedArchiver`` property lists.

A keyed archive is a plist with a flat ``$objects`` table and a ``$top``
dictionary whose ``root`` entry is a :class:`plistlib.UID` pointer into that
table.  Every reference inside an archived object is again a UID, so reading
anything means following UID chains.  :class:`KeyedArchive` wraps the table and
offers the three accessors the link-preview decoder needs:

* :meth:`KeyedArchive.deref` follows UIDs with a visited set and a depth cap,
  returning ``None`` for out-of-range or cyclic pointers instead of raising;
* :meth:`KeyedArchive.string` returns the first non-empty string among several
  keys, unwrapping archived ``NSURL`` objects (``NS.relative``);
* :meth:`KeyedArchive.nested_url` returns the ``http`` URL stored one level
  down (``obj[key]["URL"]``), as ``LPImageMetadata`` stores it.

Only :meth:`KeyedArchive.load` raises (``ValueError`` for anything that is not
a keyed archive); the accessors never do.
"""

from __future__ import annotations

import plistlib
from collections.abc import Mapping, Sequence

__all__ = ["KeyedArchive"]


class KeyedArchive:
    """A loaded ``NSKeyedArchiver`` object table plus its dereferenced root."""

    __slots__ = ("objects", "root")

    objects: list[object]
    root: object

    def __init__(self, objects: Sequence[object], root_ref: object = None) -> None:
        self.objects = list(objects)
        self.root = self.deref(root_ref)

    @classmethod
    def load(cls, payload: bytes) -> KeyedArchive:
        """Parse ``payload`` with :func:`plistlib.loads` and validate its shape.

        Raises ``ValueError`` when the bytes are not a property list, when the
        plist is not a dictionary, or when ``$objects`` (a list) or ``$top``
        (a dictionary) is missing.  A missing ``$top["root"]`` is tolerated:
        ``root`` is then ``None``.
        """
        try:
            plist = plistlib.loads(payload)
        except Exception as exc:  # plistlib raises several unrelated types
            raise ValueError(f"not a property list: {exc.__class__.__name__}") from exc
        if not isinstance(plist, dict):
            raise ValueError("keyed archive is not a dictionary")
        objects = plist.get("$objects")
        top = plist.get("$top")
        if not isinstance(objects, list):
            raise ValueError("keyed archive has no $objects table")
        if not isinstance(top, dict):
            raise ValueError("keyed archive has no $top dictionary")
        return cls(objects, top.get("root"))

    def deref(self, v: object, *, max_depth: int = 32) -> object:
        """Follow a UID chain and return the object it lands on.

        Returns ``None`` when a UID is out of range, when a UID repeats (a
        cycle), or when more than ``max_depth`` hops are needed.  Non-UID
        values are returned unchanged.
        """
        seen: set[int] = set()
        hops = 0
        while isinstance(v, plistlib.UID):
            i = v.data
            if hops >= max_depth or i in seen or not 0 <= i < len(self.objects):
                return None
            seen.add(i)
            hops += 1
            v = self.objects[i]
        return v

    def nsurl(self, v: object) -> str | None:
        """Dereference ``v`` to a non-empty string, unwrapping an archived ``NSURL``.

        An archived ``NSURL`` is a dictionary whose ``NS.relative`` entry points
        at the URL string.  Returns ``None`` for anything else.
        """
        obj = self.deref(v)
        if isinstance(obj, str) and obj:
            return obj
        if isinstance(obj, dict):
            rel = self.deref(obj.get("NS.relative"))
            if isinstance(rel, str) and rel:
                return rel
        return None

    def string(self, obj: Mapping[str, object], *keys: str) -> str | None:
        """Return the first non-empty string stored under ``keys`` in ``obj``.

        Each value is dereferenced; archived ``NSURL`` objects are unwrapped.
        """
        for key in keys:
            s = self.nsurl(obj.get(key))
            if s is not None:
                return s
        return None

    def nested_url(self, obj: Mapping[str, object], key: str) -> str | None:
        """Return ``obj[key]["URL"]`` as a string when it starts with ``http``.

        ``obj[key]`` must dereference to a dictionary (for example an
        ``LPImageMetadata``); its ``URL`` may be a plain string or an archived
        ``NSURL``.  Anything else yields ``None``.
        """
        node = self.deref(obj.get(key))
        if not isinstance(node, dict):
            return None
        u = self.nsurl(node.get("URL"))
        return u if u is not None and u.startswith("http") else None
