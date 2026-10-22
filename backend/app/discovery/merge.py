"""Deterministic de-duplication and metadata merging for Search-1."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..pixiv.adapter import NovelSummary, NovelTag
from . import SOURCE_PRIORITY
from .types import DiscoveryBatch, MergedCandidate, MergedSighting


MetadataKey = tuple[int, str, int]


def _metadata_key(priority: int, query_key: str, rank: int) -> MetadataKey:
    return (priority, query_key, rank)


def _choice_key(key: MetadataKey, value: object) -> tuple[object, ...]:
    """Use a value as the final tie-break, never input order or object hash."""
    return (*key, repr(value))


def _non_negative(value: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


@dataclass
class _Accumulator:
    novel_id: int
    title: str = ""
    caption: str = ""
    author_id: int = 0
    author_name: str = ""
    series_id: int | None = None
    series_name: str | None = None
    text_length: int = 0
    total_bookmarks: int = 0
    total_view: int = 0
    total_comments: int = 0
    is_original: bool = True
    create_date: str | None = None
    x_restrict: int = 0
    visible: bool = True
    novel_ai_type: int | None = None
    likes: int | None = None
    updated_at: str | None = None
    # name -> (translation, deterministic source key)
    tags: dict[str, tuple[str | None, tuple[object, ...] | None]] = field(default_factory=dict)
    sightings: dict[tuple[str, str], MergedSighting] = field(default_factory=dict)
    _field_keys: dict[str, tuple[object, ...]] = field(default_factory=dict)
    _author_entity_key: tuple[object, ...] | None = None
    _series_entity_key: tuple[object, ...] | None = None
    _author_names: dict[int, tuple[str, tuple[object, ...]]] = field(default_factory=dict)
    _series_names: dict[int, tuple[str, tuple[object, ...]]] = field(default_factory=dict)

    def _set_text(self, field_name: str, value: str | None, key: MetadataKey) -> None:
        if not value:
            return
        candidate = _choice_key(key, value)
        current = self._field_keys.get(field_name)
        if current is None or candidate < current:
            setattr(self, field_name, value)
            self._field_keys[field_name] = candidate

    def _set_entity(
        self,
        entity: str,
        entity_id: int | None,
        name: str | None,
        key: MetadataKey,
    ) -> None:
        names = self._author_names if entity == "author" else self._series_names
        valid_id = (entity == "author" and isinstance(entity_id, int) and entity_id != 0) or (
            entity == "series" and entity_id is not None
        )
        if valid_id:
            entity_candidate = _choice_key(key, entity_id)
            current = getattr(self, f"_{entity}_entity_key")
            if current is None or entity_candidate < current:
                setattr(self, f"_{entity}_entity_key", entity_candidate)
                setattr(self, f"{entity}_id", entity_id)
        if name:
            if valid_id:
                name_candidate = _choice_key(key, name)
                existing = names.get(int(entity_id))
                if existing is None or name_candidate < existing[1]:
                    names[int(entity_id)] = (name, name_candidate)
            elif getattr(self, f"{entity}_id") in (0, None):
                # A name-only record is only a fallback; it cannot erase an id.
                field_name = f"{entity}_name"
                name_candidate = _choice_key(key, name)
                current = self._field_keys.get(field_name)
                if current is None or name_candidate < current:
                    setattr(self, field_name, name)
                    self._field_keys[field_name] = name_candidate

    def add_novel(self, novel: NovelSummary, key: MetadataKey) -> None:
        """Merge one result using source priority and stable tie-breaks."""
        self._set_text("title", novel.title, key)
        self._set_text("caption", novel.caption, key)
        self._set_entity("author", novel.author_id, novel.author_name, key)
        self._set_entity("series", novel.series_id, novel.series_name, key)
        self._set_text("create_date", novel.create_date, key)
        self._set_text("updated_at", novel.updated_at, key)

        for field_name in (
            "text_length",
            "total_bookmarks",
            "total_view",
            "total_comments",
            "x_restrict",
        ):
            setattr(
                self,
                field_name,
                max(getattr(self, field_name), _non_negative(getattr(novel, field_name))),
            )
        self.visible = self.visible and bool(novel.visible)

        # bool False and numeric 0 are valid metadata, so test against None.
        for field_name in ("is_original", "novel_ai_type", "likes"):
            value = getattr(novel, field_name)
            if value is None:
                continue
            candidate = _choice_key(key, value)
            current = self._field_keys.get(field_name)
            if current is None or candidate < current:
                setattr(self, field_name, value)
                self._field_keys[field_name] = candidate

        for tag in novel.tags:
            if not tag.name:
                continue
            current = self.tags.get(tag.name)
            if current is None:
                self.tags[tag.name] = (tag.translated_name, None)
            if tag.translated_name:
                candidate = _choice_key(key, tag.translated_name)
                current = self.tags[tag.name]
                if current[1] is None or candidate < current[1]:
                    self.tags[tag.name] = (tag.translated_name, candidate)

    def add_sighting(self, sighting: MergedSighting) -> None:
        sighting_key = (sighting.source_kind, sighting.query_key)
        current = self.sightings.get(sighting_key)
        current_sort = (
            current.source_rank,
            current.query_text or "",
            current.discovered_at,
        ) if current is not None else None
        new_sort = (sighting.source_rank, sighting.query_text or "", sighting.discovered_at)
        if current_sort is None or new_sort < current_sort:
            self.sightings[sighting_key] = sighting

    def build(self) -> tuple[NovelSummary, tuple[MergedSighting, ...]]:
        if self._author_entity_key is not None:
            self.author_name = self._author_names.get(self.author_id, ("", ()))[0]
        if self._series_entity_key is not None:
            self.series_name = self._series_names.get(self.series_id, (None, ()))[0]
        tags = tuple(
            NovelTag(name=name, translated_name=value[0])
            for name, value in sorted(self.tags.items(), key=lambda item: item[0])
        )
        novel = NovelSummary(
            novel_id=self.novel_id,
            title=self.title,
            caption=self.caption,
            author_id=self.author_id,
            author_name=self.author_name,
            series_id=self.series_id,
            series_name=self.series_name,
            text_length=self.text_length,
            total_bookmarks=self.total_bookmarks,
            total_view=self.total_view,
            total_comments=self.total_comments,
            is_original=self.is_original,
            create_date=self.create_date,
            x_restrict=self.x_restrict,
            visible=self.visible,
            novel_ai_type=self.novel_ai_type if self.novel_ai_type is not None else 0,
            likes=self.likes,
            updated_at=self.updated_at,
            tags=tags,
        )
        ordered_sightings = tuple(
            sorted(
                self.sightings.values(),
                key=lambda s: (s.source_kind, s.query_key, s.source_rank, s.query_text or ""),
            )
        )
        return novel, ordered_sightings


def merge_batches(batches: list[DiscoveryBatch], *, discovered_at: str) -> list[MergedCandidate]:
    """Merge all consumed pages; output is initially ordered by novel id."""
    accumulators: dict[int, _Accumulator] = {}
    priority = {kind: idx for idx, kind in enumerate(SOURCE_PRIORITY)}
    for batch in batches:
        source_priority = priority.get(batch.query.source_kind, len(SOURCE_PRIORITY))
        for rank, novel in enumerate(batch.page.items, start=1):
            acc = accumulators.setdefault(novel.novel_id, _Accumulator(novel_id=novel.novel_id))
            key = _metadata_key(source_priority, batch.query.query_key, rank)
            acc.add_novel(novel, key)
            acc.add_sighting(
                MergedSighting(
                    novel_id=novel.novel_id,
                    source_kind=batch.query.source_kind,
                    query_key=batch.query.query_key,
                    query_text=batch.query.text,
                    source_rank=rank,
                    discovered_at=discovered_at,
                )
            )
    result: list[MergedCandidate] = []
    for novel_id in sorted(accumulators):
        novel, sightings = accumulators[novel_id].build()
        result.append(MergedCandidate(novel=novel, sightings=sightings))
    return result


def candidate_budget_key(candidate: MergedCandidate) -> tuple[object, ...]:
    """Best sighting priority used for deterministic 200-candidate selection."""
    priority = {kind: idx for idx, kind in enumerate(SOURCE_PRIORITY)}
    return min(
        (
            priority.get(s.source_kind, len(SOURCE_PRIORITY)),
            s.source_rank,
            s.query_key,
            candidate.novel.novel_id,
        )
        for s in candidate.sightings
    )


def select_candidates(candidates: list[MergedCandidate], limit: int) -> list[MergedCandidate]:
    """Select by best-source budget priority, then restore stable display order."""
    selected = sorted(candidates, key=candidate_budget_key)[:limit]
    return sorted(selected, key=lambda candidate: candidate.novel.novel_id)
