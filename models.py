"""Lore graph data model — typed Cognee DataPoints (the backbone).

Reference fields declared as ``SkipValidation[Any]`` become graph EDGES named
after the field when assigned (e.g. ``decision.concerns = code_unit`` ->
a "concerns" edge). Verified against cognee 1.2.2.
"""
from typing import Any, Optional
from pydantic import SkipValidation
from cognee.low_level import DataPoint


class Author(DataPoint):
    name: str
    github_handle: Optional[str] = None
    metadata: dict = {"index_fields": ["name", "github_handle"]}


class CodeUnit(DataPoint):
    """A file, module, or symbol. ``path`` is the canonical join key across history."""

    path: str
    kind: str = "file"  # file | module | function | class
    metadata: dict = {"index_fields": ["path"]}


class Issue(DataPoint):
    number: int
    title: str
    body: Optional[str] = None
    opened_on: Optional[str] = None
    closed_on: Optional[str] = None
    reporter: SkipValidation[Any] = None  # Author
    metadata: dict = {"index_fields": ["title", "body"]}


class PullRequest(DataPoint):
    number: int
    title: str
    body: Optional[str] = None
    merged_on: Optional[str] = None
    author: SkipValidation[Any] = None  # Author
    closes: SkipValidation[Any] = None  # list[Issue]
    touches: SkipValidation[Any] = None  # list[CodeUnit]
    metadata: dict = {"index_fields": ["title", "body"]}


class Commit(DataPoint):
    sha: str
    message: str
    committed_on: str
    author: SkipValidation[Any] = None  # Author
    in_pr: SkipValidation[Any] = None  # PullRequest
    touches: SkipValidation[Any] = None  # list[CodeUnit]
    metadata: dict = {"index_fields": ["message"]}


class Decision(DataPoint):
    """The rationale memory — the star node."""

    text: str
    rationale: Optional[str] = None
    topic: Optional[str] = None
    concerns: SkipValidation[Any] = None  # list[CodeUnit] — what it governs
    made_in: SkipValidation[Any] = None  # PullRequest / Commit
    motivated_by: SkipValidation[Any] = None  # Issue
    decided_on: Optional[str] = None
    supersedes: SkipValidation[Any] = None  # Decision (evolution thread)
    status: str = "active"  # active | superseded | at_risk
    metadata: dict = {"index_fields": ["text", "rationale", "topic"]}


class Change(DataPoint):
    """A proposed edit being evaluated for regression (the target diff / a PR under review)."""

    text: str
    touches: SkipValidation[Any] = None  # list[CodeUnit]
    removes_symbols: SkipValidation[Any] = None  # list[str] — symbols/lines deleted
    proposed_on: Optional[str] = None
    metadata: dict = {"index_fields": ["text"]}
