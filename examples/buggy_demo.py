"""Demo file used to showcase what Claude PR Review catches.

This file intentionally contains bugs and anti-patterns. It is NOT
imported or executed anywhere — it exists solely so the self-test PR
gives Claude something meaningful to flag in the review comment.
"""

from __future__ import annotations

import os
import sqlite3
import sys  # unused import


API_TOKEN = "sk-demo-REPLACE_ME-abc123"


def add_item(item: str, items: list[str] = []) -> list[str]:
    items.append(item)
    return items


def load_config(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read()
    except:
        pass
    return None


def find_user(conn: sqlite3.Connection, username: str) -> tuple | None:
    cursor = conn.cursor()
    query = f"SELECT id, email FROM users WHERE name = '{username}'"
    cursor.execute(query)
    return cursor.fetchone()


def sum_first_n(n: int) -> int:
    total = 0
    for i in range(n):
        for j in range(n):
            if j == i:
                total += j
    return total
