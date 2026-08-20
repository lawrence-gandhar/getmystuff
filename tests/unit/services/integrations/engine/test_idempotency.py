"""
Tests for ``engine/idempotency.py``.

The assertion that matters most is in ``TestWriteMayBeRetried``, and it is the rule that
protects a merchant's store:

    A read timeout on a non-idempotent write is a **permanent** failure. It is
    retried zero times.

Shopify's ``POST /orders.json`` has no idempotency header. The request goes out, the
order is created, the response never arrives. Retrying duplicates a real order in
somebody's real business, and no amount of backoff makes that less true. Every other
test in this file is about the machinery that makes that rule expressible.

Everything here is pure — no clock, no randomness, no database — which is what makes a
replay comparable to the run it repeats.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.integrations.engine.idempotency import (
    canonical_json,
    graph_hash,
    manual_run_key,
    natural_key_hash,
    operation_hash,
    schedule_run_key,
    sha256_of,
    webhook_run_key,
    write_may_be_retried,
)


class TestWriteMayBeRetried:
    """The rule. See the module docstring."""

    def test_a_request_that_never_left_is_always_retryable(self) -> None:
        """Connection refused, DNS failure, connect timeout — nothing was processed."""
        assert write_may_be_retried(
            reached_server=False,
            operation_is_idempotent=False,
            has_idempotency_header=False,
        )

    def test_a_read_timeout_on_a_plain_write_is_not(self) -> None:
        """
        The one that matters. The request went out and the answer did not come back, so
        the destination may already have created the record.
        """
        assert not write_may_be_retried(
            reached_server=True,
            operation_is_idempotent=False,
            has_idempotency_header=False,
        )

    def test_an_idempotent_operation_earns_the_retry(self) -> None:
        assert write_may_be_retried(
            reached_server=True,
            operation_is_idempotent=True,
            has_idempotency_header=False,
        )

    def test_an_idempotency_header_earns_it_too(self) -> None:
        """
        The destination will deduplicate it, which is a stronger guarantee than our
        believing the operation is safe.
        """
        assert write_may_be_retried(
            reached_server=True,
            operation_is_idempotent=False,
            has_idempotency_header=True,
        )

    def test_the_default_is_not_to_retry(self) -> None:
        """
        Stated as a table so the safe answer is visible: of the four combinations where
        the request reached the server, only the ones that opted in are retryable.
        """
        reached = [
            (False, False, False),
            (True, False, True),
            (False, True, True),
            (True, True, True),
        ]
        for is_idempotent, has_header, expected in reached:
            assert (
                write_may_be_retried(
                    reached_server=True,
                    operation_is_idempotent=is_idempotent,
                    has_idempotency_header=has_header,
                )
                is expected
            )


class TestCanonicalJson:
    def test_key_order_does_not_change_the_form(self) -> None:
        """
        A dict built by two code paths is the same operation, and a hash that disagreed
        would report every replay as a different run.
        """
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})

    def test_whitespace_is_not_meaning(self) -> None:
        assert " " not in canonical_json({"a": 1, "b": [1, 2]})

    def test_a_datetime_is_hashed_as_its_text_rather_than_raising(self) -> None:
        """
        Losing a little precision in a hash beats failing the run that produced it.
        """
        value = canonical_json({"at": datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc)})

        assert "2026-08-14" in value

    def test_the_same_value_hashes_the_same_every_time(self) -> None:
        assert sha256_of({"a": [1, 2]}) == sha256_of({"a": [1, 2]})

    def test_a_different_value_hashes_differently(self) -> None:
        assert sha256_of({"a": [1, 2]}) != sha256_of({"a": [2, 1]})


class TestGraphAndOperationHashes:
    def test_a_graph_hash_is_hex_sha256(self) -> None:
        digest = graph_hash({"nodes": [], "edges": []})

        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")

    def test_reordering_a_nodes_keys_does_not_change_the_graph(self) -> None:
        """
        The canvas serialises node data in whatever order the properties panel wrote
        it. A hash that changed for that would make every save look like a new version.
        """
        first = {"nodes": [{"id": "n1", "type": "trigger"}]}
        second = {"nodes": [{"type": "trigger", "id": "n1"}]}

        assert graph_hash(first) == graph_hash(second)

    def test_moving_a_node_does_change_it(self) -> None:
        """
        Positions are part of the drawing, and a version is a snapshot of the drawing.
        Excluding them would make two visibly different canvases claim to be the same
        published version.
        """
        first = {"nodes": [{"id": "n1", "position": {"x": 0, "y": 0}}]}
        second = {"nodes": [{"id": "n1", "position": {"x": 40, "y": 0}}]}

        assert graph_hash(first) != graph_hash(second)

    def test_an_operation_hash_detects_a_changed_operation(self) -> None:
        """
        The half of the determinism claim the version hash does not cover — and the
        reason a REST operation is a row with columns rather than a function in a
        module.
        """
        before = {"method": "POST", "path": "/orders"}
        after = {"method": "POST", "path": "/orders/v2"}

        assert operation_hash(before) != operation_hash(after)


class TestRunKeys:
    def test_a_schedule_key_names_the_slot_not_the_moment(self) -> None:
        """
        A run that waited eleven minutes in the queue is still the 09:00 run. Deriving
        the key from ``now()`` would make every late fire look new, which defeats the
        mechanism exactly when the system is loaded and needs it.
        """
        slot = datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc)

        assert schedule_run_key("trig-1", slot) == "trig-1:2026-08-14T09:00:00+00:00"

    def test_two_slots_of_one_trigger_differ(self) -> None:
        first = schedule_run_key("trig-1", datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc))
        second = schedule_run_key("trig-1", datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc))

        assert first != second

    def test_one_slot_of_two_triggers_differs(self) -> None:
        slot = datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc)

        assert schedule_run_key("trig-1", slot) != schedule_run_key("trig-2", slot)

    def test_a_webhook_key_is_scoped_to_its_endpoint(self) -> None:
        """Vendors mint event ids in their own namespaces; a collision is not something
        to leave to luck."""
        first = webhook_run_key("ep-1", "evt_123")
        second = webhook_run_key("ep-2", "evt_123")

        assert first != second

    @pytest.mark.parametrize("missing", [None, "", "   "])
    def test_no_event_id_means_no_key(self, missing) -> None:
        """
        A fabricated key would deduplicate two genuinely different events, which is
        worse than processing one twice. The body-hash fallback belongs at the delivery
        table, not here.
        """
        assert webhook_run_key("ep-1", missing) is None

    def test_a_manual_run_has_no_key(self) -> None:
        """Somebody pressing Run twice means they want it twice."""
        assert manual_run_key() is None


class TestNaturalKeyHash:
    def test_the_same_record_hashes_the_same(self) -> None:
        record = {"email": "a@b.com", "name": "Jane", "extra": "ignored"}

        first = natural_key_hash(record, ["email", "name"])
        second = natural_key_hash(dict(reversed(list(record.items()))), ["email", "name"])

        assert first == second

    def test_field_order_comes_from_the_mapping_not_the_record(self) -> None:
        """
        Two records with their keys in a different order are the same record. Two
        mappings declaring their key fields in a different order are different keys —
        which is correct, because the declared order is the author's statement of what
        identity means.
        """
        record = {"a": 1, "b": 2}

        assert natural_key_hash(record, ["a", "b"]) != natural_key_hash(record, ["b", "a"])

    def test_a_missing_field_is_not_the_same_as_a_present_one(self) -> None:
        """
        "No email address" and "the email field was absent" must not collapse into the
        identity of a record that has one.
        """
        with_email = natural_key_hash({"id": 1, "email": "a@b.com"}, ["id", "email"])
        without = natural_key_hash({"id": 1}, ["id", "email"])

        assert with_email != without

    def test_a_key_with_no_fields_is_refused(self) -> None:
        """
        Without a field there is nothing to match against, so every record would be
        created again on the next run — a duplicate-generating configuration that would
        otherwise be accepted silently.
        """
        with pytest.raises(ValueError, match="at least one field"):
            natural_key_hash({"a": 1}, [])

    def test_it_does_not_store_the_key_itself(self) -> None:
        """A natural key is usually an email address, which does not need to be kept in
        order to be matched."""
        digest = natural_key_hash({"email": "jane@example.com"}, ["email"])

        assert "jane" not in digest
        assert len(digest) == 64
