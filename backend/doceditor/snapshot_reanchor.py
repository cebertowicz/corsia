"""Resolve a group of anchors from one exact stored document version."""
from __future__ import annotations

from collections import Counter
import json
import logging
from typing import cast

from doceditor import comment_anchoring, comment_snapshots, live_diff, live_text, snapshot_mapping_cache, snapshot_showing


log = logging.getLogger(__name__)

_ANCHOR_FIELDS = ('from_off', 'to_off', 'anchor_text', 'anchor_prefix', 'anchor_suffix',
                  'decision', 'replacement', 'parent_id', 'review')


class AnchorConflict(Exception):
    """The anchor changed after its source was read; discard the whole calculation."""


def persist_updates(db, artifact_id: str, text: str, updates: list[dict], *, legacy_updates=(), invalid=()) -> int:
    """Commit remapped ranges and their source together, only against the rows read."""
    if not updates and not legacy_updates and not invalid:
        return 0
    from infra.database import write_transaction

    with write_transaction(db, scope='advance_comment_snapshots') as connection:
        checked = [*updates, *invalid]
        rows = {}
        if checked:
            rows = {row[0]: tuple(row[1:]) for row in connection.execute(
                f"SELECT id, snapshot_id, {', '.join(_ANCHOR_FIELDS)} FROM doc_comments "
                "WHERE artifact_id=? AND id IN (SELECT value FROM json_each(?))",
                (artifact_id, json.dumps([update['id'] for update in checked])),
            )}
        for update in checked:
            if rows.get(update['id']) != (update['expected_snapshot_id'], *update['expected_anchor']):
                raise AnchorConflict('comment anchor changed during rematching')
        count = comment_anchoring.store.update_positions(artifact_id, [*updates, *legacy_updates], db=connection)
        if updates:
            snapshot_id = comment_snapshots.intern(connection, text)
            connection.executemany('UPDATE doc_comments SET snapshot_id=? WHERE id=? AND artifact_id=?',
                                   [(snapshot_id, update['id'], artifact_id) for update in updates])
        if invalid:
            connection.executemany('UPDATE doc_comments SET snapshot_id=NULL WHERE id=? AND artifact_id=?',
                                   [(u['id'], artifact_id) for u in invalid])
        comment_snapshots.collect_candidates(connection, (u['expected_snapshot_id'] for u in [*updates, *invalid]))
        return count


def persist_client_positions(db, artifact_id: str, text: str | None, requested: list[dict]) -> int:
    """Preserve client updates; pair only positions justified by the stored source."""
    from infra.database import write_transaction
    from doceditor.quote_anchor import anchor_context

    with write_transaction(db, scope='client_comment_positions') as connection:
        legacy, versioned, invalid = [], [], []
        for update in requested:
            prepared = dict(update)
            comment = comment_anchoring.store.get_comment(str(update.get('id', '')), db=connection)
            quote = update.get('anchor_text', comment.get('anchor_text') if comment else None)
            start, end = update.get('from_off'), update.get('to_off')
            context = anchor_context(text, start, end, quote) if text is not None and quote else None
            if context is not None:
                prepared['anchor_prefix'], prepared['anchor_suffix'] = context
            if not comment or comment['artifact_id'] != artifact_id or comment.get('snapshot_id') is None:
                legacy.append(prepared)
                continue
            expected = {'id': comment['id'], 'expected_snapshot_id': comment['snapshot_id'],
                        'expected_anchor': tuple(comment.get(field) for field in _ANCHOR_FIELDS)}
            reason = comment_snapshots.invalid_reason(text, start, end, quote) if text is not None else 'text_unavailable'
            if reason is None:
                candidate = dict(comment)
                _, _, invalid_source = resolve_versioned([candidate], cast(str, text), connection)
                if (invalid_source or candidate.get('orphaned') or
                        (candidate.get('from_off'), candidate.get('to_off'), candidate.get('anchor_text')) !=
                        (start, end, quote)):
                    reason = 'ambiguous'
            if reason:
                log.info('comment snapshot mismatch id=%s reason=%s', comment['id'], reason)
                legacy.append(prepared)
                invalid.append(expected)
            else:
                versioned.append({**prepared, **expected})
        return persist_updates(connection, artifact_id, cast(str, text), versioned, legacy_updates=legacy, invalid=invalid)


def resolve_versioned(threads: list[dict], text: str, db=None, *, versions=None,
                      detached_point=False) -> tuple[list[dict], list[dict], list[dict]]:
    """Resolve each persisted source once; NULL source keeps the legacy rule."""
    groups: dict[int, list[dict]] = {}
    legacy = []
    invalid = []
    for comment in threads:
        snapshot_id = comment.get('snapshot_id')
        if snapshot_id is None:
            legacy.append(comment)
        else:
            groups.setdefault(snapshot_id, []).append(comment)
    versioned_updates = []
    if versions is None:
        versions = comment_snapshots.read_many(db, groups)
    new_index = snapshot_showing.TextIndex(text)
    digest = snapshot_mapping_cache.destination_digest(text) if any(
        versions[snapshot_id] != text for snapshot_id in groups) else None
    line_context: list[tuple[list[str], dict[str, int]]] = []
    for snapshot_id, comments in groups.items():
        valid = []
        for comment in comments:
            if comment['parent_id'] or comment['review'] or not comment['anchor_text']:
                continue
            reason = comment_snapshots.invalid_reason(
                versions[snapshot_id], comment.get('from_off'), comment.get('to_off'), comment['anchor_text'])
            if reason:
                log.info('comment snapshot mismatch id=%s reason=%s', comment['id'], reason)
                invalid.append({'id': comment['id'], 'expected_snapshot_id': snapshot_id,
                                'expected_anchor': tuple(comment.get(field) for field in _ANCHOR_FIELDS)})
                legacy.append(comment)
            else:
                valid.append(comment)
        expected = {c['id']: tuple(c.get(field) for field in _ANCHOR_FIELDS) for c in valid}
        for update in resolve(valid, versions[snapshot_id], text, new_index=new_index,
                              snapshot_id=snapshot_id, destination_digest=digest, line_context=line_context,
                              detached_point=detached_point):
            update['expected_snapshot_id'] = snapshot_id
            update['expected_anchor'] = expected[update['id']]
            versioned_updates.append(update)
    legacy_updates = comment_anchoring.resolve_threads(legacy, text)
    return legacy_updates, versioned_updates, invalid


def _map_spans(old: str, new: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Follow the same UTF-8 edit delta as a single CRDT text transaction."""
    delta = []
    position = 0
    for start, end, inserted in live_diff.text_changes(old, new):
        if start > position:
            delta.append({'retain': start - position})
        if end > start:
            delta.append({'delete': end - start})
        if inserted:
            delta.append({'insert': inserted})
        position = end
    return live_text.follow_spans(spans, old, delta)[0] if delta else spans


def _accept(eligible, candidates, spans, ranges, old, new, old_index, new_index,
            line_pairs, line_counts, detached_point) -> set[str]:
    """Apply only the mapped anchors whose quote still names the same text.

    Returns the ids accepted. A rejected row keeps its stored offsets; only the
    live-document frames are given a nearby point to draw the detached card at.
    """
    accepted = set()
    for original, candidate, old_span in zip(eligible, candidates, spans):
        quote = comment_anchoring._anchored_quote(original)
        allowed, _, _ = snapshot_showing.evaluate(
            old, new, old_span, (candidate['from_off'], candidate['to_off']), quote,
            line_pairs=line_pairs, line_counts=line_counts, old_index=old_index, new_index=new_index)
        edit_rejected = False
        if allowed and not candidate.get('orphaned'):
            start = new_index.cp_at_u16(candidate['from_off'])
            end = new_index.cp_at_u16(candidate['to_off'])
            if start is not None and end is not None and new[start:end] != quote:
                legacy = dict(original)
                comment_anchoring.resolve_threads([legacy], new)
                allowed = not legacy.get('orphaned') and (
                    legacy['from_off'], legacy['to_off']) == (candidate['from_off'], candidate['to_off'])
                edit_rejected = not allowed
        if allowed and not candidate.get('orphaned'):
            original.update(candidate)
            accepted.add(original['id'])
        else:
            original['orphaned'] = not bool(original.get('decision'))
            if detached_point and original['orphaned'] and not edit_rejected:
                # The nearby point is absent from the persistence updates and from a list response.
                point = ranges[original['id']][0]
                original['from_off'] = original['to_off'] = point
    return accepted


def resolve(threads: list[dict], old: str, new: str, *, new_index=None,
            snapshot_id=None, destination_digest=None, line_context=None,
            detached_point=False) -> list[dict]:
    """Map and judge anchors; rejected anchors keep their stored offsets in the rows.

    The caller groups rows by snapshot and owns persistence of accepted updates.
    A rejected row is never persisted; ``detached_point`` is for the live-document
    frames only, which need a place to draw the detached card. A list response leaves
    the stored offsets where they are, because that is what an agent reads back.
    """
    old_index = snapshot_showing.TextIndex(old)
    new_index = new_index or snapshot_showing.TextIndex(new)
    if old == new and not any(char in old for char in '\r\v\f\x1c\x1d\x1e\x85\u2028\u2029'):
        ranges = {}
        for comment in threads:
            if comment['parent_id'] or comment['review'] or not comment['anchor_text']:
                continue
            start, end = comment.get('from_off'), comment.get('to_off')
            start_cp, end_cp = old_index.cp_at_u16(start), old_index.cp_at_u16(end)
            if (start_cp is None or end_cp is None or end_cp <= start_cp
                    or '\n' in old[start_cp:end_cp] or old[start_cp:end_cp] != comment['anchor_text']):
                break
            ranges[comment['id']] = (start, end)
        else:
            return comment_anchoring.resolve_threads(threads, new, ranges=ranges)
    eligible = []
    for comment in threads:
        if comment['parent_id'] or comment['review'] or not comment['anchor_text']:
            continue
        start, end = comment.get('from_off'), comment.get('to_off')
        start_cp, end_cp = old_index.cp_at_u16(start), old_index.cp_at_u16(end)
        if start_cp is None or end_cp is None or end_cp <= start_cp:
            comment['orphaned'] = not bool(comment.get('decision'))
            continue
        eligible.append(comment)
    if not eligible:
        return []
    spans = [(c['from_off'], c['to_off']) for c in eligible]
    if old == new:
        # Keep the established fallback for unchanged text outside the shortcut.
        doc = live_text.create(old)
        live_text.apply_text(doc, new)
        mapped = spans
    else:
        digest = destination_digest
        if snapshot_id is not None and digest is None:
            digest = snapshot_mapping_cache.destination_digest(new)
        mapped = snapshot_mapping_cache.map_spans(snapshot_id, old, new, spans, digest, _map_spans)
    ranges = dict(zip((c['id'] for c in eligible), mapped))
    candidates = [dict(c) for c in eligible]
    if line_context is None:
        line_pairs = snapshot_showing._line_pairs(old, new)
    else:
        if not line_context:
            lines = new.splitlines(keepends=True)
            line_context.append((lines, live_diff._line_positions(lines)))
        lines, positions = line_context[0]
        line_pairs = snapshot_showing._line_pairs(old, new, new_lines=lines, new_positions=positions)
    line_counts = (Counter(old.splitlines()), Counter(new.splitlines()))
    to_resolve = []
    for candidate, old_span, mapped_span in zip(candidates, spans, mapped):
        old_start = old_index.cp_at_u16(old_span[0])
        kind = line_pairs.get(old_index.line_at(old_start), ('unclear', None))[0]
        if mapped_span[0] == mapped_span[1] and kind == 'delete':
            # A deleted source line cannot pass the showing rule at any place.
            candidate.update(from_off=mapped_span[0], to_off=mapped_span[1],
                             orphaned=not bool(candidate.get('decision')))
        else:
            to_resolve.append(candidate)
    updates = comment_anchoring.resolve_threads(to_resolve, new, ranges=ranges)
    accepted = _accept(eligible, candidates, spans, ranges, old, new, old_index, new_index,
                       line_pairs, line_counts, detached_point)
    result = [update for update in updates if update['id'] in accepted]
    changed_ids = {update['id'] for update in result}
    if old != new:
        # Even unchanged coordinates need their matching source version advanced.
        result.extend({'id': c['id'], 'from_off': c['from_off'], 'to_off': c['to_off']}
                      for c in eligible if c['id'] in accepted and c['id'] not in changed_ids)
    return result
