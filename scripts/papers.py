#!/usr/bin/env python3
"""Helper for the paper-researcher agent: search, fetch and verify quotes."""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "papers"
TEXT_DIR = CACHE_DIR / "text"
META_DIR = CACHE_DIR / "meta"

MIN_QUOTE_CHARS = 20


def normalize(text, join_hyphens=True):
    """Lowercase and flatten text so PDF/HTML artefacts don't defeat matching."""
    text = unicodedata.normalize("NFKC", text)  # also expands ligatures
    text = text.replace("­", "")  # soft hyphen
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2" if join_hyphens else r"\1-\2", text)
    for old, new in (("‘", "'"), ("’", "'"), ("“", '"'),
                     ("”", '"'), ("–", "-"), ("—", "-")):
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip().lower()


def segments(text):
    """Split text into (label, body): PDF pages if form feeds exist, else ## sections."""
    if "\f" in text:
        return [("p.%d" % i, body) for i, body in enumerate(text.split("\f"), 1)]
    segs, label, buf = [], "start", []
    for line in text.splitlines():
        if line.startswith("## "):
            segs.append((label, "\n".join(buf)))
            label, buf = line[3:].strip(), []
        else:
            buf.append(line)
    segs.append((label, "\n".join(buf)))
    return segs


def verify_quote(source, quote):
    q = normalize(quote, join_hyphens=False)
    if len(q) < MIN_QUOTE_CHARS:
        return {"found": False, "location": None, "reason": "too short"}
    variants = (True, False)
    if not any(q in normalize(source, j) for j in variants):
        return {"found": False, "location": None, "reason": "not found in text"}
    for label, body in segments(source):
        if any(q in normalize(body, j) for j in variants):
            return {"found": True, "location": label, "reason": None}
    return {"found": True, "location": None, "reason": None}
