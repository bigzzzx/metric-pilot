"""Order-independent multiset comparison, preserving duplicates and NULLs."""
import math


def rows_equal(actual: list[dict], expected: list[list]) -> bool:
    remaining = [list(row.values()) for row in actual]
    if len(remaining) != len(expected):
        return False
    def equal(a, b):
        if isinstance(a, (float, int)) and isinstance(b, (float, int)):
            return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6)
        return a == b
    for row in expected:
        for index, candidate in enumerate(remaining):
            if len(row) == len(candidate) and all(equal(a, b) for a, b in zip(row, candidate)):
                remaining.pop(index)
                break
        else:
            return False
    return not remaining
