"""Counting and duplicate detection on a stable key.

Two of the most common wrong numbers in reporting come from the same mistake:
counting *rows* and calling the result the number of *entities*.

**Counting.** ``COUNT(*)`` over a table where one customer appears on four rows
is a row count, not a customer count. :func:`count_distinct_rows` counts on a
caller-supplied stable key and reports the gap between the two numbers, so the
over-count is a stated figure rather than an assumption.

**Duplicates.** Two records can describe the same real entity while carrying
different keys — a re-registration, a rename, a typo in a reference field.
:func:`find_duplicate_groups` finds those suspicions and *returns them for a
person to decide*. It never merges, never deletes, and never picks a winner.

The rule holds in both directions: a blank key is held back and reported, never
counted as an entity and never quietly merged into another row.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from difflib import SequenceMatcher
from typing import Any

from .findings import (
    ERROR,
    INFO,
    VERDICT_CLEAN,
    VERDICT_REVIEW_REQUIRED,
    WARNING,
    Finding,
    Locator,
    json_safe,
)

# A duplicate group is a suspicion, never a decision. Recording that in the
# payload means a downstream reader cannot mistake the output for a merge.
RECOMMENDED_ACTION = "human_review"


def normalise_key(value: Any, *, case_sensitive: bool = False, strip_whitespace: bool = True) -> str | None:
    """Render a key as a comparable string, or ``None`` if it is blank.

    Deliberately conservative. It removes the differences that are certainly
    noise (surrounding whitespace, letter case, and ``1.0`` versus ``1``) and
    nothing else. It does not strip punctuation, remove accents, or apply any
    fuzzy rule, because those change which entities are considered the same and
    that is a decision for a person.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value.is_integer():
            text = str(int(value))
        else:
            text = repr(value)
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)

    if strip_whitespace:
        text = text.strip()
    if not text:
        return None
    if not case_sensitive:
        text = text.casefold()
    return text


@dataclass
class KeyedRow:
    """A row plus the normalised key it was counted under."""

    index: int
    key: str
    row: dict[str, Any]
    raw_key: Any

    def locator(self, column: str | None = None) -> Locator:
        return Locator(row_index=self.index, row_key=self.key, column=column)

    def to_dict(self) -> dict[str, Any]:
        return {
            "locator": self.locator().to_dict(),
            "key": self.key,
            "row": json_safe(self.row),
        }


@dataclass
class BlankKeyRow:
    """A row with no usable key. Held back and reported, never counted."""

    index: int
    row: dict[str, Any]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "locator": Locator(row_index=self.index).to_dict(),
            "reason": self.reason,
            "row": json_safe(self.row),
        }


@dataclass
class DistinctOutcome:
    """Row count versus distinct entity count, with the gap explained."""

    key: str
    rows_in: int = 0
    keyed: list[KeyedRow] = dataclass_field(default_factory=list)
    blank_key_rows: list[BlankKeyRow] = dataclass_field(default_factory=list)
    groups: dict[str, list[KeyedRow]] = dataclass_field(default_factory=dict)
    findings: list[Finding] = dataclass_field(default_factory=list)

    @property
    def distinct_count(self) -> int:
        return len(self.groups)

    @property
    def keyed_row_count(self) -> int:
        return len(self.keyed)

    @property
    def row_count(self) -> int:
        """Every row handed in, including the ones with no key."""
        return self.rows_in

    @property
    def difference(self) -> int:
        """Rows minus distinct entities: how much a naive row count over-reports."""
        return self.row_count - self.distinct_count

    @property
    def difference_from_repeated_keys(self) -> int:
        """The part of the gap caused by one key appearing on several rows."""
        return sum(len(rows) - 1 for rows in self.groups.values() if len(rows) > 1)

    @property
    def difference_from_blank_keys(self) -> int:
        """The part of the gap caused by rows that carry no usable key."""
        return len(self.blank_key_rows)

    @property
    def top_repeated(self) -> list[tuple[str, int]]:
        counts = [(key, len(rows)) for key, rows in self.groups.items() if len(rows) > 1]
        return sorted(counts, key=lambda item: (-item[1], item[0]))

    @property
    def verdict(self) -> str:
        return VERDICT_REVIEW_REQUIRED if self.difference else VERDICT_CLEAN

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_key": self.key,
            "row_count": self.row_count,
            "distinct_count": self.distinct_count,
            "difference": self.difference,
            "difference_breakdown": {
                "from_repeated_keys": self.difference_from_repeated_keys,
                "from_rows_with_blank_key": self.difference_from_blank_keys,
            },
            "rows_with_a_key": self.keyed_row_count,
            "rows_with_blank_key": len(self.blank_key_rows),
            "blank_key_rows": [item.to_dict() for item in self.blank_key_rows],
            "repeated_keys": [
                {
                    "key": key,
                    "count": count,
                    "row_indexes": [item.index for item in self.groups[key]],
                }
                for key, count in self.top_repeated
            ],
        }


def _read_rows(rows: Any, key: str, *, case_sensitive: bool, strip_whitespace: bool):
    """Walk a rows payload, splitting it into keyed rows, blank-key rows and findings.

    Never raises on row content. A row that is not an object is held back for the
    same reason a row with a blank key is: it cannot be attributed to an entity.
    """
    keyed: list[KeyedRow] = []
    blank: list[BlankKeyRow] = []
    findings: list[Finding] = []
    total = 0

    for index, row in enumerate(rows):
        total += 1
        locator = Locator(row_index=index)

        if not isinstance(row, Mapping):
            blank.append(
                BlankKeyRow(
                    index=index,
                    row={"_raw": json_safe(row)},
                    reason=f"row is a {type(row).__name__}, not an object with named columns",
                )
            )
            findings.append(
                Finding(
                    code="not_an_object",
                    message=f"row is a {type(row).__name__}, not an object with named columns",
                    severity=ERROR,
                    locator=locator,
                    value=json_safe(row),
                )
            )
            continue

        raw_key = row.get(key)
        normalised = normalise_key(
            raw_key, case_sensitive=case_sensitive, strip_whitespace=strip_whitespace
        )
        if normalised is None:
            blank.append(
                BlankKeyRow(
                    index=index,
                    row=dict(row),
                    reason=f"'{key}' is missing or blank, so this row belongs to no identifiable entity",
                )
            )
            findings.append(
                Finding(
                    code="blank_key",
                    message=(
                        f"'{key}' is missing or blank, so this row cannot be attributed to an "
                        "entity and is excluded from the count"
                    ),
                    severity=WARNING,
                    locator=Locator(row_index=index, column=key),
                    value=json_safe(raw_key),
                    guidance="A blank key is excluded from counts but never dropped — it is listed here.",
                )
            )
            continue

        keyed.append(KeyedRow(index=index, key=normalised, row=dict(row), raw_key=raw_key))

    return total, keyed, blank, findings


def count_distinct_rows(
    rows: Any,
    key: str,
    *,
    case_sensitive: bool = False,
    strip_whitespace: bool = True,
) -> DistinctOutcome:
    """Count distinct entities on ``key``, and report the gap to a row count."""
    total, keyed, blank, findings = _read_rows(
        rows, key, case_sensitive=case_sensitive, strip_whitespace=strip_whitespace
    )

    groups: dict[str, list[KeyedRow]] = defaultdict(list)
    for item in keyed:
        groups[item.key].append(item)

    outcome = DistinctOutcome(
        key=key,
        rows_in=total,
        keyed=keyed,
        blank_key_rows=blank,
        groups={key_name: list(items) for key_name, items in groups.items()},
    )

    for key_name, items in outcome.groups.items():
        if len(items) > 1:
            findings.append(
                Finding(
                    code="repeated_key",
                    message=(
                        f"key {key_name!r} appears on {len(items)} rows "
                        f"(row numbers {', '.join(str(i.index + 1) for i in items)})"
                    ),
                    severity=INFO,
                    locator=Locator(row_index=items[0].index, row_key=key_name, column=key),
                    value=len(items),
                    expected=1,
                    guidance=(
                        "Expected for a transaction table, a problem for an entity table — the "
                        "tool cannot know which this is, so it reports the count rather than "
                        "silently collapsing the rows."
                    ),
                )
            )

    outcome.findings = findings
    return outcome


@dataclass
class DuplicateGroup:
    """A set of rows suspected of describing the same entity."""

    group_id: str
    kind: str
    reason: str
    members: list[KeyedRow]
    keys: list[str]
    review_question: str
    similarity: float | None = None
    truncated_members: int = 0

    @property
    def size(self) -> int:
        """How many rows are in the group, including any not listed individually."""
        return len(self.members) + self.truncated_members

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "group_id": self.group_id,
            "kind": self.kind,
            "reason": self.reason,
            "size": self.size,
            "keys": self.keys,
            "recommended_action": RECOMMENDED_ACTION,
            "auto_merged": False,
            "decision_required": True,
            "review_question": self.review_question,
            "members": [member.to_dict() for member in self.members],
        }
        if self.similarity is not None:
            out["similarity"] = round(self.similarity, 4)
        if self.truncated_members:
            out["members_omitted"] = self.truncated_members
        return out


@dataclass
class DuplicateOutcome:
    """Every suspicion found, with nothing resolved."""

    key: str
    rows_in: int = 0
    distinct_keys: int = 0
    groups: list[DuplicateGroup] = dataclass_field(default_factory=list)
    findings: list[Finding] = dataclass_field(default_factory=list)
    blank_key_rows: list[BlankKeyRow] = dataclass_field(default_factory=list)
    groups_omitted: int = 0

    @property
    def row_count(self) -> int:
        """Every row handed in, including the ones with no usable key."""
        return self.rows_in

    @property
    def group_count(self) -> int:
        return len(self.groups) + self.groups_omitted

    @property
    def rows_in_groups(self) -> int:
        """Rows named by the groups actually returned (omitted groups are not counted)."""
        return sum(group.size for group in self.groups)

    @property
    def verdict(self) -> str:
        return VERDICT_REVIEW_REQUIRED if self.group_count else VERDICT_CLEAN

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_key": self.key,
            "row_count": self.rows_in,
            "distinct_keys": self.distinct_keys,
            "duplicate_group_count": self.group_count,
            "rows_in_duplicate_groups": self.rows_in_groups,
            "auto_merged": False,
            "nothing_was_merged_or_deleted": True,
            "blank_key_rows": [item.to_dict() for item in self.blank_key_rows],
            "groups": [group.to_dict() for group in self.groups],
            "groups_omitted": self.groups_omitted,
        }


def _similarity(left: str, right: str) -> float:
    """Deterministic string similarity in 0..1 (stdlib ``difflib``)."""
    return SequenceMatcher(None, left, right).ratio()


def find_duplicate_groups(
    rows: Any,
    key: str,
    *,
    match_fields: Sequence[str] | None = None,
    similarity_threshold: float | None = None,
    case_sensitive: bool = False,
    strip_whitespace: bool = True,
    max_groups: int = 50,
    max_members_per_group: int = 20,
) -> DuplicateOutcome:
    """Find suspected duplicate groups and return them for human review.

    Three independent kinds of suspicion, each labelled with its ``kind``:

    ``same_key``
        The same normalised key appears on more than one row. Either genuine
        duplicates or legitimate repeated transactions — the tool cannot tell,
        so it reports rather than decides.
    ``same_signature``
        Different keys, but an identical normalised signature over
        ``match_fields`` — the re-registration case.
    ``near_match``
        Different keys, and signatures that are close rather than identical.
        Only produced when the caller supplies ``similarity_threshold``, because
        "close enough" is a policy decision, not a fact.

    Clustering is done in input order against each cluster's first row, so the
    result is deterministic for a given input order. Nothing is merged, nothing
    is deleted, and no row is chosen as the survivor.
    """
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key must be a non-empty column name")
    key = key.strip()

    if isinstance(rows, Mapping):
        raise TypeError("rows must be an array of row objects, not an object")
    total, keyed, blank, findings = _read_rows(
        rows, key, case_sensitive=case_sensitive, strip_whitespace=strip_whitespace
    )

    groups: list[DuplicateGroup] = []
    reported_key_sets: list[set] = []
    counter = 0

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"DUP-{counter:03d}"

    def make(
        kind: str,
        reason: str,
        members: list[KeyedRow],
        review_question: str,
        similarity: float | None = None,
    ) -> DuplicateGroup:
        listed = members[:max_members_per_group]
        keys = sorted({member.key for member in members})
        reported_key_sets.append(set(keys))
        return DuplicateGroup(
            group_id=next_id(),
            kind=kind,
            reason=reason,
            members=listed,
            keys=keys,
            review_question=review_question,
            similarity=similarity,
            truncated_members=len(members) - len(listed),
        )

    # 1. Same key on more than one row.
    by_key: dict[str, list[KeyedRow]] = defaultdict(list)
    for item in keyed:
        by_key[item.key].append(item)

    for key_name in sorted(by_key):
        items = by_key[key_name]
        if len(items) > 1:
            indexes = ", ".join(str(item.index + 1) for item in items[:max_members_per_group])
            groups.append(
                make(
                    "same_key",
                    f"key {key_name!r} appears on {len(items)} rows (row numbers {indexes})",
                    items,
                    (
                        f"Are these {len(items)} rows the same entity recorded more than once, "
                        f"or {len(items)} separate records that legitimately share the key "
                        f"{key_name!r}?"
                    ),
                )
            )
            findings.append(
                Finding(
                    code="duplicate_key",
                    message=f"key {key_name!r} appears on {len(items)} rows",
                    severity=WARNING,
                    locator=Locator(row_index=items[0].index, row_key=key_name, column=key),
                    value=len(items),
                    guidance="Listed for review. Nothing was merged or removed.",
                )
            )

    # 2 and 3. Different keys sharing a signature, exactly or approximately.
    if match_fields:
        fields = [str(name) for name in match_fields]
        one_per_key: list[KeyedRow] = []
        for key_name in sorted(by_key):
            one_per_key.append(by_key[key_name][0])

        def already_reported(members: Sequence[KeyedRow]) -> bool:
            """True when these keys are already named by an earlier group.

            Prevents the same set of rows being reported twice under two kinds.
            A group that names a key no earlier group named is still reported,
            because it is new information.
            """
            target = {member.key for member in members}
            return any(target <= existing for existing in reported_key_sets)

        def signature_of(item: KeyedRow) -> tuple[str, ...] | None:
            parts = [
                normalise_key(
                    item.row.get(name),
                    case_sensitive=case_sensitive,
                    strip_whitespace=strip_whitespace,
                )
                for name in fields
            ]
            if any(part is None for part in parts):
                return None
            return tuple(part for part in parts if part is not None)

        exact: dict[tuple[str, ...], list[KeyedRow]] = defaultdict(list)
        for item in one_per_key:
            token = signature_of(item)
            if token is not None:
                exact[token].append(item)

        for token in sorted(exact):
            items = exact[token]
            if len(items) > 1 and not already_reported(items):
                rendered = " / ".join(f"{name}={value}" for name, value in zip(fields, token, strict=True))
                groups.append(
                    make(
                        "same_signature",
                        f"{len(items)} different keys share the signature {rendered}",
                        items,
                        (
                            f"Do these {len(items)} keys ({', '.join(sorted(i.key for i in items))}) "
                            f"refer to one entity that was registered more than once?"
                        ),
                    )
                )
                findings.append(
                    Finding(
                        code="same_signature",
                        message=f"{len(items)} different keys share the signature {rendered}",
                        severity=WARNING,
                        locator=Locator(row_index=items[0].index, row_key=items[0].key, column=key),
                        value=sorted(item.key for item in items),
                        guidance="Listed for review. Keys were not rewritten and rows were not merged.",
                    )
                )

        if similarity_threshold is not None:
            try:
                threshold = float(similarity_threshold)
            except (TypeError, ValueError):
                raise ValueError(
                    f"similarity_threshold must be a number between 0 and 1, got {similarity_threshold!r}"
                ) from None
            if not 0.0 < threshold <= 1.0:
                raise ValueError(
                    f"similarity_threshold must be greater than 0 and at most 1, got {threshold:g}"
                )

            candidates = [item for item in one_per_key if signature_of(item) is not None]

            clusters: list[dict[str, Any]] = []
            for item in candidates:
                token = signature_of(item)
                assert token is not None
                placed = False
                for cluster in clusters:
                    score = min(
                        _similarity(left, right)
                        for left, right in zip(cluster["token"], token, strict=True)
                    )
                    if score >= threshold:
                        cluster["members"].append(item)
                        cluster["score"] = min(cluster["score"], score)
                        placed = True
                        break
                if not placed:
                    clusters.append({"token": token, "members": [item], "score": 1.0})

            for cluster in clusters:
                items = cluster["members"]
                if len(items) < 2 or already_reported(items):
                    continue
                rendered = " / ".join(
                    f"{name}={value}" for name, value in zip(fields, cluster["token"], strict=True)
                )
                groups.append(
                    make(
                        "near_match",
                        (
                            f"{len(items)} different keys have similar values (score "
                            f"{cluster['score']:.2f} or better, threshold {threshold:g}) on {rendered}"
                        ),
                        items,
                        (
                            f"Are these {len(items)} keys the same entity with a spelling "
                            "difference, or genuinely different entities?"
                        ),
                        similarity=cluster["score"],
                    )
                )
                findings.append(
                    Finding(
                        code="near_match",
                        message=(
                            f"{len(items)} different keys are similar ({cluster['score']:.2f}) on "
                            f"{rendered}"
                        ),
                        severity=WARNING,
                        locator=Locator(row_index=items[0].index, row_key=items[0].key, column=key),
                        value=sorted(item.key for item in items),
                        guidance=(
                            "A suspicion produced by string similarity, not a decision. Nothing "
                            "was merged. Similarity is symmetric and computed with "
                            "difflib.SequenceMatcher."
                        ),
                    )
                )

    omitted = max(0, len(groups) - max(0, int(max_groups)))
    listed = groups[: max(0, int(max_groups))]

    outcome = DuplicateOutcome(
        key=key,
        rows_in=total,
        distinct_keys=len(by_key),
        groups=listed,
        findings=findings,
        blank_key_rows=blank,
        groups_omitted=omitted,
    )
    return outcome


__all__ = [
    "BlankKeyRow",
    "DistinctOutcome",
    "DuplicateGroup",
    "DuplicateOutcome",
    "KeyedRow",
    "RECOMMENDED_ACTION",
    "count_distinct_rows",
    "find_duplicate_groups",
    "normalise_key",
]
