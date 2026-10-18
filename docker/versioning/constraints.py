"""Numeric version parser, constraint parser, and consistency validator.

No Docker, network, or subprocess — pure Python with re and dataclasses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .errors import ConstraintSyntaxError, VersionSyntaxError


# ---------------------------------------------------------------------------
# Regex
# ---------------------------------------------------------------------------

VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
CLAUSE_RE = re.compile(
    r"^(==|>=|<=|>|<)"
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)$"
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class NumericVersion:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class ConstraintClause:
    operator: str
    operand: NumericVersion


@dataclass(frozen=True)
class Constraint:
    clauses: tuple[ConstraintClause, ...]

    def matches(self, version: NumericVersion) -> bool:
        for clause in self.clauses:
            if not _eval_clause(clause, version):
                return False
        return True

    def __str__(self) -> str:
        return ",".join(f"{c.operator}{c.operand}" for c in self.clauses)


def _eval_clause(clause: ConstraintClause, version: NumericVersion) -> bool:
    op = clause.operator
    op_v = clause.operand
    if op == "==":
        return version == op_v
    elif op == ">":
        return version > op_v
    elif op == ">=":
        return version >= op_v
    elif op == "<":
        return version < op_v
    elif op == "<=":
        return version <= op_v
    raise ConstraintSyntaxError(f"Unknown operator: {op}")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_numeric_version(raw: str) -> NumericVersion:
    """Parse a strict X.Y.Z string. Rejects prereleases, v-prefix, etc."""
    if not isinstance(raw, str):
        raise VersionSyntaxError(f"Version must be a string, got {type(raw).__name__}")
    m = VERSION_RE.match(raw)
    if not m:
        raise VersionSyntaxError(
            f"Invalid numeric version: {raw!r}. Expected X.Y.Z with no prefix/suffix."
        )
    return NumericVersion(
        major=int(m.group(1)),
        minor=int(m.group(2)),
        patch=int(m.group(3)),
    )


def parse_constraint(raw: str) -> Constraint:
    """Parse a comma-separated constraint expression.

    Each clause is operator + X.Y.Z.  Empty clauses, unsupported operators,
    wildcards, OR, and prereleases are rejected.
    """
    if not isinstance(raw, str) or raw.strip() == "":
        raise ConstraintSyntaxError("Constraint must be a non-empty string")

    clauses: list[ConstraintClause] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise ConstraintSyntaxError(f"Empty clause in constraint {raw!r}")
        m = CLAUSE_RE.match(part)
        if not m:
            raise ConstraintSyntaxError(
                f"Invalid constraint clause {part!r} in {raw!r}"
            )
        clauses.append(
            ConstraintClause(
                operator=m.group(1),
                operand=NumericVersion(
                    major=int(m.group(2)),
                    minor=int(m.group(3)),
                    patch=int(m.group(4)),
                ),
            )
        )
    return Constraint(clauses=tuple(clauses))


# ---------------------------------------------------------------------------
# Consistency validation
# ---------------------------------------------------------------------------


def validate_constraint_consistency(constraint: Constraint) -> None:
    """Raise ConstraintSyntaxError if clauses contradict each other.

    Detects:
      - Two different == clauses
      - ==x and >x or <x (strict contradiction with equality)
      - Lower bound > upper bound
      - Lower bound >= upper bound when equal with mixed strict/loose
      - Equal strict/inclusive bounds where strictness makes the set empty
        (e.g. ``>3.0.0,>=3.0.0,<=3.0.0``)
    """
    eq: Optional[NumericVersion] = None
    strict_lower: Optional[NumericVersion] = None  # >
    loose_lower: Optional[NumericVersion] = None  # >=
    strict_upper: Optional[NumericVersion] = None  # <
    loose_upper: Optional[NumericVersion] = None  # <=

    for c in constraint.clauses:
        op, v = c.operator, c.operand
        if op == "==":
            if eq is not None and eq != v:
                raise ConstraintSyntaxError(f"Contradictory equality: =={eq} and =={v}")
            eq = v
        elif op == ">":
            if strict_lower is None or v > strict_lower:
                strict_lower = v
        elif op == ">=":
            if loose_lower is None or v > loose_lower:
                loose_lower = v
        elif op == "<":
            if strict_upper is None or v < strict_upper:
                strict_upper = v
        elif op == "<=":
            if loose_upper is None or v < loose_upper:
                loose_upper = v

    # Combine strict and loose lower bounds.
    # When operands are equal the strict variant wins — a >x bound is
    # tighter than >=x and losing that information hides contradictions
    # like >3.0.0,<=3.0.0.
    lower: Optional[NumericVersion] = None
    lower_inclusive: bool = True
    if strict_lower is not None and loose_lower is not None:
        if strict_lower > loose_lower:
            lower = strict_lower
            lower_inclusive = False
        elif strict_lower < loose_lower:
            lower = loose_lower
            lower_inclusive = True
        else:  # equal — preserve strictness
            lower = strict_lower
            lower_inclusive = False
    elif strict_lower is not None:
        lower = strict_lower
        lower_inclusive = False
    elif loose_lower is not None:
        lower = loose_lower
        lower_inclusive = True

    # Combine strict and loose upper bounds — same equal-operand logic.
    upper: Optional[NumericVersion] = None
    upper_inclusive: bool = True
    if strict_upper is not None and loose_upper is not None:
        if strict_upper < loose_upper:
            upper = strict_upper
            upper_inclusive = False
        elif strict_upper > loose_upper:
            upper = loose_upper
            upper_inclusive = True
        else:  # equal — preserve strictness
            upper = strict_upper
            upper_inclusive = False
    elif strict_upper is not None:
        upper = strict_upper
        upper_inclusive = False
    elif loose_upper is not None:
        upper = loose_upper
        upper_inclusive = True

    # Check eq against bounds
    if eq is not None:
        if lower is not None:
            if lower_inclusive:
                if eq < lower:
                    raise ConstraintSyntaxError(f"Contradictory: =={eq} but >= {lower}")
            else:
                if eq <= lower:
                    raise ConstraintSyntaxError(f"Contradictory: =={eq} but > {lower}")
        if upper is not None:
            if upper_inclusive:
                if eq > upper:
                    raise ConstraintSyntaxError(f"Contradictory: =={eq} but <= {upper}")
            else:
                if eq >= upper:
                    raise ConstraintSyntaxError(f"Contradictory: =={eq} but < {upper}")
        # Also check: ==x with >x (strict equality contradiction)
        for c in constraint.clauses:
            if c.operator == ">" and c.operand == eq:
                raise ConstraintSyntaxError(f"Contradictory: =={eq} and >{eq}")
            if c.operator == "<" and c.operand == eq:
                raise ConstraintSyntaxError(f"Contradictory: =={eq} and <{eq}")

    # Check lower > upper
    if lower is not None and upper is not None:
        if lower > upper:
            raise ConstraintSyntaxError(
                f"Contradictory bounds: lower {lower} > upper {upper}"
            )
        if lower == upper and (not lower_inclusive or not upper_inclusive):
            raise ConstraintSyntaxError(
                f"Contradictory bounds: {lower} and {upper} are mutually exclusive"
            )
