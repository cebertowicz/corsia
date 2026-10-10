#!/usr/bin/env python3
"""Median of 5 for the sidebar list calls: sidebar_timing.py BASE TOKEN_FILE"""
import sys
import time
import urllib.request

base, token_file = sys.argv[1], sys.argv[2]
token = open(token_file).read().strip()
for path in ("/api/threads", "/api/plans/operator/thread-states"):
    ms = []
    for _ in range(5):
        start = time.perf_counter()
        body = urllib.request.urlopen(f"{base}{path}?token={token}").read()
        ms.append(round((time.perf_counter() - start) * 1000, 1))
    print(path, "median_ms", sorted(ms)[2], "bytes", len(body))
