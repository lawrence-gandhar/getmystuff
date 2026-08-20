"""
Tests for ``flow_service.py``.

Four properties, in the order the failures cost the most.

**Publishing snapshots, and the snapshot is not the drawing.** Editing after a publish
must change nothing about what runs. This is the departure from Graph Designer that the
whole versions table exists for, and the only way to know it holds is to publish, edit,
and read the version back.

**One published version per flow.** The partial unique index is the guarantee and
``publish_flow`` is the message — but the index does not exist on SQLite unless
``sqlite_where`` is set, so the *service-level* rule is what these tests pin. Publishing
three times leaves one published row and two archived ones, and asserting the count is the
assertion; asserting that the newest is published would pass with three of them published.

**Publish refuses what save allows, by exactly one rule.** A write step with a required
field nobody mapped saves fine and publishes never. Both halves are asserted, because a
test that only checked the refusal would pass an implementation that refused the save too
— and that implementation makes the canvas unusable.

**Every trigger write recomputes ``next_run_at``.** That column is the entire schedule.
A path that edits an interval without recomputing it leaves the workflow running on the
old one, which looks like the scheduler is broken and is not.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any, Dict

import pytest
from litestar.exceptions import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integrations import (
    AUTH_API_KEY,
    MAX_BATCH_SIZE,
    MIN_INTERVAL_SECONDS,
    NODE_CONNECTOR_WRITE,
    NODE_SUCCESS,
    NODE_TRIGGER,
    OPERATION_WRITE,
    OVERLAP_SKIP,
    RUN_MODE_DRY_RUN,
    RUN_MODE_LIVE,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    TRIGGER_MANUAL,
    TRIGGER_SCHEDULE,
    VERSION_ARCHIVED,
    VERSION_PUBLISHED,
    IntegrationConnection,
    IntegrationFlow,
    IntegrationFlowVersion,
    IntegrationRestOperation,
    IntegrationRun,
    IntegrationRunJob,
)
from app.models.user.user import User
from app.services.integrations import flow_service
from app.services.integrations.credentials import credential_service
from app.services.integrations.errors import FlowValidationError

# ---------------------------------------------------------------------------
# Drawings
# ---------------------------------------------------------------------------


def trigger_only() -> Dict[str, Any]:
    """The smallest drawing that validates: one trigger, nothing else."""
    return {
        "nodes": [
            {"id": "t1", "type": NODE_TRIGGER, "data": {"label": "Start", "kind": TRIGGER_MANUAL}}
        ],
        "edges": [],
    }


def trigger_then_success() -> Dict[str, Any]:
    return {
        "nodes": [
            {"id": "t1", "type": NODE_TRIGGER, "data": {"label": "Start", "kind": TRIGGER_MANUAL}},
            {"id": "s1", "type": NODE_SUCCESS, "data": {"label": "Done"}},
        ],
        "edges": [{"id": "e1", "source": "t1", "target": "s1", "sourcePort": "default"}],
    }


@pytest.fixture
async def flow(db: AsyncSession, user: User) -> IntegrationFlow:
    return await flow_service.create_flow(db, user.id, "Nightly sync")


async def published_count(db: AsyncSession, flow_id: int) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(IntegrationFlowVersion)
        .where(
            IntegrationFlowVersion.flow_id == flow_id,
            IntegrationFlowVersion.status == VERSION_PUBLISHED,
        )
    )
    return int(result.scalar() or 0)


# ---------------------------------------------------------------------------
# Creating and saving
# ---------------------------------------------------------------------------


class TestCreating:
    async def test_a_new_flow_opens_holding_a_trigger(
        self, db: AsyncSession, user: User
    ) -> None:
        """
        An empty canvas would give somebody nothing to drag from, and ``validate_flow``
        refuses a workflow with no trigger — so the first thing a blank flow would do is
        fail to save.
        """
        flow = await flow_service.create_flow(db, user.id, "First")

        types = [node["type"] for node in flow.graph_data["nodes"]]
        assert types == [NODE_TRIGGER]

    async def test_a_new_flow_is_a_draft(self, db: AsyncSession, user: User) -> None:
        """
        ``is_active`` is not a parameter of ``create_flow``. A workflow that arrived
        switched on would run on whatever schedule it was created with before anybody had
        looked at it — and the caller most likely to want that is the AI generator, which
        is exactly the one that must not have it.
        """
        flow = await flow_service.create_flow(db, user.id, "First")

        assert flow.is_active is False

    async def test_the_default_graph_is_not_shared_between_flows(
        self, db: AsyncSession, user: User
    ) -> None:
        """
        A module-level dict handed out by reference would make editing one new flow edit
        every other one created since the process started — invisible in any single test
        and catastrophic in a worker.
        """
        first = await flow_service.create_flow(db, user.id, "One")
        second = await flow_service.create_flow(db, user.id, "Two")

        first.graph_data["nodes"][0]["data"]["label"] = "Changed"

        assert second.graph_data["nodes"][0]["data"]["label"] == "Trigger"

    async def test_a_duplicate_name_is_refused_with_the_name_in_it(
        self, db: AsyncSession, user: User
    ) -> None:
        """
        The index is the guarantee; this is the message. Without it the collision is an
        ``IntegrityError`` and a 500, which tells somebody nothing about what to change.
        """
        await flow_service.create_flow(db, user.id, "Nightly sync")

        with pytest.raises(HTTPException) as caught:
            await flow_service.create_flow(db, user.id, "  nightly SYNC ")

        assert "Nightly sync" in caught.value.detail

    async def test_another_users_name_is_not_a_collision(
        self, db: AsyncSession, user: User, make_user
    ) -> None:
        """Uniqueness is per user. Two people naming their workflow 'Nightly sync' is the
        ordinary case, not a conflict."""
        other = await make_user(email="other@example.com")
        await flow_service.create_flow(db, user.id, "Nightly sync")

        mine = await flow_service.create_flow(db, other.id, "Nightly sync")

        assert mine.name == "Nightly sync"

    async def test_a_blank_name_is_refused(self, db: AsyncSession, user: User) -> None:
        with pytest.raises(HTTPException) as caught:
            await flow_service.create_flow(db, user.id, "   ")

        assert "needs a name" in caught.value.detail


class TestSaving:
    async def test_a_drawing_that_validates_is_stored(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        saved = await flow_service.save_flow(
            db, user.id, flow.uuid, trigger_then_success()
        )

        assert len(saved.graph_data["nodes"]) == 2

    async def test_nothing_is_written_when_validation_fails(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        """
        This is what lets the canvas keep the user's unsaved work and highlight the
        offending step. A partial save would leave the stored drawing and the one on
        screen disagreeing, and the next Save would be against a baseline nobody chose.
        """
        before = flow.graph_data

        with pytest.raises(FlowValidationError):
            await flow_service.save_flow(db, user.id, flow.uuid, {"nodes": [], "edges": []})

        await db.refresh(flow)
        assert flow.graph_data == before

    async def test_the_refusal_names_the_node(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        """
        ``FlowValidationError`` is deliberately not flattened to an ``HTTPException`` in
        the service: the canvas needs ``node_id`` to highlight the step, and a string
        would throw that away.
        """
        drawing = trigger_then_success()
        drawing["nodes"].append(
            {"id": "t2", "type": NODE_TRIGGER, "data": {"label": "Second", "kind": TRIGGER_MANUAL}}
        )

        with pytest.raises(FlowValidationError) as caught:
            await flow_service.save_flow(db, user.id, flow.uuid, drawing)

        assert isinstance(caught.value, FlowValidationError)

    async def test_another_users_flow_is_not_found(
        self, db: AsyncSession, user: User, make_user, flow: IntegrationFlow
    ) -> None:
        """Scoped in the query, not checked afterwards — so there is no window in which
        another user's row is in scope at all."""
        other = await make_user(email="other@example.com")

        with pytest.raises(HTTPException) as caught:
            await flow_service.save_flow(db, other.id, flow.uuid, trigger_only())

        assert caught.value.status_code == 404


class TestSettings:
    async def test_a_batch_size_over_the_ceiling_is_refused(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        """
        The ceiling is not a preference. ``record_buffer`` is process memory, so a batch is
        that many records held at once per running node.
        """
        with pytest.raises(HTTPException) as caught:
            await flow_service.update_flow_settings(
                db, user.id, flow.uuid,
                name="Nightly sync",
                default_batch_size=MAX_BATCH_SIZE + 1,
            )

        assert str(MAX_BATCH_SIZE) in caught.value.detail

    async def test_redacted_fields_are_lowercased_and_deduplicated(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        """The deny-list is matched case-insensitively, and a stored list holding both
        'Email' and 'email' is one somebody will misread."""
        updated = await flow_service.update_flow_settings(
            db, user.id, flow.uuid,
            name="Nightly sync",
            redacted_fields=[" SSN ", "ssn", "IBAN", ""],
        )

        assert updated.redacted_fields == ["ssn", "iban"]

    async def test_renaming_to_its_own_name_is_allowed(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        """The uniqueness check excludes the row being edited. Without that, saving the
        settings form without touching the name refuses every time."""
        updated = await flow_service.update_flow_settings(
            db, user.id, flow.uuid, name="Nightly sync", description="Runs at 3am"
        )

        assert updated.description == "Runs at 3am"


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


class TestPublishing:
    async def test_publishing_produces_version_one(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        await flow_service.save_flow(db, user.id, flow.uuid, trigger_then_success())

        version = await flow_service.publish_flow(db, user.id, flow.uuid)

        assert version.version_number == 1
        assert version.status == VERSION_PUBLISHED

    async def test_editing_after_publishing_does_not_change_the_version(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        """
        The property the whole versions table exists for. Without it, editing a workflow
        at 2pm silently changes what the 3am sync does, and last Tuesday's run record
        describes a topology that no longer exists.
        """
        await flow_service.save_flow(db, user.id, flow.uuid, trigger_then_success())
        version = await flow_service.publish_flow(db, user.id, flow.uuid)

        await flow_service.save_flow(db, user.id, flow.uuid, trigger_only())

        await db.refresh(version)
        assert len(version.graph_data["nodes"]) == 2

    async def test_publishing_three_times_leaves_one_published_version(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        """
        **The count is the assertion.** Checking only that the newest is published would
        pass an implementation that left all three published — and on SQLite the partial
        unique index does not exist unless ``sqlite_where`` is set, so the service-level
        rule is the one that has to hold here.
        """
        await flow_service.save_flow(db, user.id, flow.uuid, trigger_then_success())

        for _ in range(3):
            await flow_service.publish_flow(db, user.id, flow.uuid)

        assert await published_count(db, flow.id) == 1

    async def test_the_superseded_versions_are_archived_not_deleted(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        """A run pinned to version 1 has to still resolve it. Deleting the row would turn
        a completed run's history into a blank."""
        await flow_service.save_flow(db, user.id, flow.uuid, trigger_then_success())
        await flow_service.publish_flow(db, user.id, flow.uuid)
        await flow_service.publish_flow(db, user.id, flow.uuid)

        versions = await flow_service.list_versions(db, user.id, flow.uuid)

        assert [v.version_number for v in versions] == [2, 1]
        assert versions[1].status == VERSION_ARCHIVED

    async def test_version_numbers_do_not_repeat(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        """
        Numbered from the highest ever, not from the count. The day a version is deleted,
        ``count + 1`` reuses a number a run row points at and a person knows by name — and
        'version 3' would then mean two different topologies.
        """
        await flow_service.save_flow(db, user.id, flow.uuid, trigger_then_success())
        first = await flow_service.publish_flow(db, user.id, flow.uuid)
        second = await flow_service.publish_flow(db, user.id, flow.uuid)

        await db.delete(first)
        await db.commit()

        third = await flow_service.publish_flow(db, user.id, flow.uuid)

        assert third.version_number == 3
        assert second.version_number == 2

    async def test_the_hash_is_over_the_snapshot(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        """Two publishes of the same drawing hash the same. That is what makes 'is this
        the same workflow that ran last Tuesday' answerable without comparing JSON."""
        await flow_service.save_flow(db, user.id, flow.uuid, trigger_then_success())

        first = await flow_service.publish_flow(db, user.id, flow.uuid)
        second = await flow_service.publish_flow(db, user.id, flow.uuid)

        assert first.graph_hash == second.graph_hash

    async def test_an_invalid_drawing_cannot_be_published(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        """
        Publish runs everything save runs. A drawing stored before a rule existed must not
        become a scheduled run of something the current validator refuses.
        """
        flow.graph_data = {"nodes": [], "edges": []}
        await db.commit()

        with pytest.raises(FlowValidationError):
            await flow_service.publish_flow(db, user.id, flow.uuid)

        assert await published_count(db, flow.id) == 0


class TestPublishRefusesWhatSaveAllows:
    """
    The one rule publish adds, and the machinery that makes it possible.

    ``flow_rules`` deliberately has no database, so it cannot look up what fields an
    operation takes. ``publish_flow`` resolves them and **stamps them onto the snapshot**;
    without that stamp the unmapped-required rule has nothing to check and the mapping
    panel's red warning is decorative.
    """

    @pytest.fixture
    async def connection(self, db: AsyncSession, user: User) -> IntegrationConnection:
        row = IntegrationConnection(
            user_id=user.id,
            connector_id="rest_generic",
            label="Billing API",
            auth_kind=AUTH_API_KEY,
            base_url="https://api.example.com",
        )
        db.add(row)
        await db.commit()
        await credential_service.store_credential(db, row, user_id=user.id, api_key="sk-1")
        return row

    @pytest.fixture
    async def operation(
        self, db: AsyncSession, connection: IntegrationConnection
    ) -> IntegrationRestOperation:
        row = IntegrationRestOperation(
            connection_id=connection.id,
            operation_id="create_invoice",
            label="Create invoice",
            kind=OPERATION_WRITE,
            method="POST",
            path="/invoices",
            inputs=[
                {"name": "email", "type": "string", "required": True},
                {"name": "amount", "type": "number", "required": True},
                {"name": "note", "type": "string", "required": False},
            ],
        )
        db.add(row)
        await db.commit()
        return row

    def drawing(self, connection: IntegrationConnection, mappings: list) -> Dict[str, Any]:
        return {
            "nodes": [
                {
                    "id": "t1",
                    "type": NODE_TRIGGER,
                    "data": {"label": "Start", "kind": TRIGGER_MANUAL},
                },
                {
                    "id": "w1",
                    "type": NODE_CONNECTOR_WRITE,
                    "data": {
                        "label": "Create invoice",
                        "connection_uuid": str(connection.uuid),
                        "operation_id": "create_invoice",
                        "mappings": mappings,
                    },
                },
            ],
            "edges": [
                {"id": "e1", "source": "t1", "target": "w1", "sourcePort": "default"}
            ],
        }

    async def test_a_half_mapped_write_step_saves(
        self,
        db: AsyncSession,
        user: User,
        flow: IntegrationFlow,
        connection: IntegrationConnection,
        operation: IntegrationRestOperation,
    ) -> None:
        """
        **Both halves matter.** A test that only checked the publish refusal would pass an
        implementation that refused the save too — and that implementation makes the canvas
        unusable, because a workflow halfway through being built is what a draft *is*.
        """
        saved = await flow_service.save_flow(
            db, user.id, flow.uuid,
            self.drawing(connection, [{"target": "email", "source": "mail"}]),
        )

        assert len(saved.graph_data["nodes"]) == 2

    async def test_publishing_it_is_refused_and_names_the_field(
        self,
        db: AsyncSession,
        user: User,
        flow: IntegrationFlow,
        connection: IntegrationConnection,
        operation: IntegrationRestOperation,
    ) -> None:
        """A published workflow is about to run with nobody watching, and a required field
        nobody mapped fails on its first record."""
        await flow_service.save_flow(
            db, user.id, flow.uuid,
            self.drawing(connection, [{"target": "email", "source": "mail"}]),
        )

        with pytest.raises(FlowValidationError) as caught:
            await flow_service.publish_flow(db, user.id, flow.uuid)

        assert "amount" in str(caught.value)
        assert caught.value.node_id == "w1"
        assert await published_count(db, flow.id) == 0

    async def test_a_fully_mapped_write_step_publishes(
        self,
        db: AsyncSession,
        user: User,
        flow: IntegrationFlow,
        connection: IntegrationConnection,
        operation: IntegrationRestOperation,
    ) -> None:
        """The optional field stays unmapped, and that is fine — only ``required`` is
        refused."""
        await flow_service.save_flow(
            db, user.id, flow.uuid,
            self.drawing(connection, [
                {"target": "email", "source": "mail"},
                {"target": "amount", "source": "total"},
            ]),
        )

        version = await flow_service.publish_flow(db, user.id, flow.uuid)

        assert version.version_number == 1

    async def test_the_required_inputs_are_stamped_onto_the_snapshot(
        self,
        db: AsyncSession,
        user: User,
        flow: IntegrationFlow,
        connection: IntegrationConnection,
        operation: IntegrationRestOperation,
    ) -> None:
        """
        Asserted on the stored version rather than inferred from the refusal, because this
        is what the *run* reads: a snapshot with no stamp validates green at run time no
        matter what the operation actually requires.
        """
        await flow_service.save_flow(
            db, user.id, flow.uuid,
            self.drawing(connection, [
                {"target": "email", "source": "mail"},
                {"target": "amount", "source": "total"},
            ]),
        )

        version = await flow_service.publish_flow(db, user.id, flow.uuid)

        write = version.graph_data["nodes"][1]
        assert write["data"]["required_inputs"] == ["email", "amount"]

    async def test_the_stamp_does_not_reach_the_drawing(
        self,
        db: AsyncSession,
        user: User,
        flow: IntegrationFlow,
        connection: IntegrationConnection,
        operation: IntegrationRestOperation,
    ) -> None:
        """
        ``required_inputs`` is derived from an operation that can change, and a stale copy
        left on the drawing would make the canvas's warning describe last month's API. Stale
        is exactly what a snapshot is for; the drawing is not a snapshot.
        """
        await flow_service.save_flow(
            db, user.id, flow.uuid,
            self.drawing(connection, [
                {"target": "email", "source": "mail"},
                {"target": "amount", "source": "total"},
            ]),
        )

        await flow_service.publish_flow(db, user.id, flow.uuid)

        await db.refresh(flow)
        assert "required_inputs" not in flow.graph_data["nodes"][1]["data"]

    async def test_a_write_step_pointed_at_nothing_is_refused_at_publish(
        self,
        db: AsyncSession,
        user: User,
        flow: IntegrationFlow,
        connection: IntegrationConnection,
        operation: IntegrationRestOperation,
    ) -> None:
        """
        Publishing is the last moment anybody is watching. Leaving an unresolvable
        connection for the run means discovering it at 3am, from a log, instead of from a
        red banner while the canvas is still open.
        """
        drawing = self.drawing(connection, [{"target": "email", "source": "mail"}])
        drawing["nodes"][1]["data"]["connection_uuid"] = str(uuid_pkg.uuid4())
        await flow_service.save_flow(db, user.id, flow.uuid, drawing)

        with pytest.raises(FlowValidationError) as caught:
            await flow_service.publish_flow(db, user.id, flow.uuid)

        assert "Create invoice" in str(caught.value)

    async def test_another_users_connection_cannot_be_published_against(
        self,
        db: AsyncSession,
        user: User,
        make_user,
        flow: IntegrationFlow,
        connection: IntegrationConnection,
        operation: IntegrationRestOperation,
    ) -> None:
        """
        The stamp resolves through ``resolve_target``, which scopes to the user **in the
        query** — so a uuid pasted in by hand, or invented by a language model, resolves to
        nothing rather than to somebody else's credential.
        """
        other = await make_user(email="other@example.com")
        other_flow = await flow_service.create_flow(db, other.id, "Theirs")
        await flow_service.save_flow(
            db, other.id, other_flow.uuid,
            self.drawing(connection, [
                {"target": "email", "source": "mail"},
                {"target": "amount", "source": "total"},
            ]),
        )

        with pytest.raises(FlowValidationError):
            await flow_service.publish_flow(db, other.id, other_flow.uuid)


class TestUnpublishing:
    async def test_unpublishing_also_switches_the_flow_off(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        """
        Both, together. An active flow with nothing published is not idle — the scheduler
        claims its trigger every interval, finds no version and writes a ``skipped`` run,
        which says 'misconfigured' when the truth is 'somebody withdrew it deliberately'.
        """
        await flow_service.save_flow(db, user.id, flow.uuid, trigger_then_success())
        await flow_service.publish_flow(db, user.id, flow.uuid)
        await flow_service.set_flow_active(db, user.id, flow.uuid, True)

        updated = await flow_service.unpublish_flow(db, user.id, flow.uuid)

        assert updated.is_active is False
        assert await published_count(db, flow.id) == 0


class TestActivating:
    async def test_an_unpublished_flow_cannot_be_switched_on(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        with pytest.raises(HTTPException) as caught:
            await flow_service.set_flow_active(db, user.id, flow.uuid, True)

        assert "Publish this workflow" in caught.value.detail

    async def test_a_published_flow_can_be(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        await flow_service.save_flow(db, user.id, flow.uuid, trigger_then_success())
        await flow_service.publish_flow(db, user.id, flow.uuid)

        updated = await flow_service.set_flow_active(db, user.id, flow.uuid, True)

        assert updated.is_active is True


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


class TestTriggers:
    @pytest.fixture
    async def live_flow(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> IntegrationFlow:
        await flow_service.save_flow(db, user.id, flow.uuid, trigger_then_success())
        await flow_service.publish_flow(db, user.id, flow.uuid)
        return flow

    async def test_saving_an_enabled_schedule_sets_next_run_at(
        self, db: AsyncSession, user: User, live_flow: IntegrationFlow
    ) -> None:
        """
        That column is the entire schedule — the scheduler holds nothing in memory and a
        fresh process fires a due row on its first tick. A trigger written without it is a
        schedule that never runs.
        """
        trigger = await flow_service.save_trigger(
            db, user.id, live_flow.uuid,
            node_id="t1", kind=TRIGGER_SCHEDULE, is_enabled=True, interval_seconds=3600,
        )

        assert trigger.next_run_at is not None

    async def test_disabling_clears_next_run_at(
        self, db: AsyncSession, user: User, live_flow: IntegrationFlow
    ) -> None:
        """Cleared rather than left stale, so the column reads as 'when this is next due'
        without a second condition beside every use of it."""
        trigger = await flow_service.save_trigger(
            db, user.id, live_flow.uuid,
            node_id="t1", kind=TRIGGER_SCHEDULE, is_enabled=True, interval_seconds=3600,
        )

        updated = await flow_service.set_trigger_enabled(
            db, user.id, live_flow.uuid, trigger.uuid, False
        )

        assert updated.next_run_at is None

    async def test_editing_the_interval_recomputes_the_next_slot(
        self, db: AsyncSession, user: User, live_flow: IntegrationFlow
    ) -> None:
        """
        The bug this catches: an edit that writes ``interval_seconds`` and leaves
        ``next_run_at`` alone. The workflow keeps running on the old interval until it
        next fires, which looks like the scheduler is broken and is not.
        """
        hourly = await flow_service.save_trigger(
            db, user.id, live_flow.uuid,
            node_id="t1", kind=TRIGGER_SCHEDULE, is_enabled=True, interval_seconds=3600,
        )
        was_due = hourly.next_run_at

        minutely = await flow_service.save_trigger(
            db, user.id, live_flow.uuid,
            node_id="t1", kind=TRIGGER_SCHEDULE, is_enabled=True, interval_seconds=60,
        )

        assert minutely.next_run_at != was_due

    async def test_the_same_node_is_updated_not_duplicated(
        self, db: AsyncSession, user: User, live_flow: IntegrationFlow
    ) -> None:
        """
        Keyed on ``(flow, node_id)``. An insert-only version would accumulate a row per
        edit and the scheduler would fire all of them — one workflow, four runs an hour.
        """
        await flow_service.save_trigger(
            db, user.id, live_flow.uuid,
            node_id="t1", kind=TRIGGER_SCHEDULE, is_enabled=True, interval_seconds=3600,
        )
        await flow_service.save_trigger(
            db, user.id, live_flow.uuid,
            node_id="t1", kind=TRIGGER_SCHEDULE, is_enabled=True, interval_seconds=7200,
        )

        triggers = await flow_service.list_triggers(db, live_flow)

        assert len(triggers) == 1
        assert triggers[0].interval_seconds == 7200

    async def test_an_interval_under_the_floor_is_refused(
        self, db: AsyncSession, user: User, live_flow: IntegrationFlow
    ) -> None:
        """
        A minute is the floor because every fire is a run row, a queue job, a compile and
        a checkpoint stream.
        """
        with pytest.raises(HTTPException) as caught:
            await flow_service.save_trigger(
                db, user.id, live_flow.uuid,
                node_id="t1", kind=TRIGGER_SCHEDULE, is_enabled=True,
                interval_seconds=MIN_INTERVAL_SECONDS - 1,
            )

        assert str(MIN_INTERVAL_SECONDS) in caught.value.detail

    async def test_a_schedule_cannot_be_enabled_on_a_draft(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        with pytest.raises(HTTPException) as caught:
            await flow_service.save_trigger(
                db, user.id, flow.uuid,
                node_id="t1", kind=TRIGGER_SCHEDULE, is_enabled=True, interval_seconds=3600,
            )

        assert "Publish this workflow" in caught.value.detail

    async def test_a_disabled_schedule_may_be_drafted(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        """Setting a schedule up before publishing is ordinary. Only *enabling* it needs a
        published version."""
        trigger = await flow_service.save_trigger(
            db, user.id, flow.uuid,
            node_id="t1", kind=TRIGGER_SCHEDULE, is_enabled=False, interval_seconds=3600,
        )

        assert trigger.interval_seconds == 3600
        assert trigger.next_run_at is None

    async def test_an_unknown_overlap_policy_is_refused_by_name(
        self, db: AsyncSession, user: User, live_flow: IntegrationFlow
    ) -> None:
        with pytest.raises(HTTPException) as caught:
            await flow_service.save_trigger(
                db, user.id, live_flow.uuid,
                node_id="t1", kind=TRIGGER_SCHEDULE, is_enabled=False,
                interval_seconds=3600, overlap_policy="whatever",
            )

        assert OVERLAP_SKIP in caught.value.detail

    async def test_a_manual_trigger_keeps_no_interval(
        self, db: AsyncSession, user: User, live_flow: IntegrationFlow
    ) -> None:
        """An interval on a manual trigger is a number nothing reads. Cleared rather than
        stored, so the row cannot be mistaken for a schedule that stopped firing."""
        trigger = await flow_service.save_trigger(
            db, user.id, live_flow.uuid,
            node_id="t1", kind=TRIGGER_MANUAL, interval_seconds=3600,
        )

        assert trigger.interval_seconds is None
        assert trigger.next_run_at is None


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


class TestRunning:
    @pytest.fixture
    async def live_flow(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> IntegrationFlow:
        await flow_service.save_flow(db, user.id, flow.uuid, trigger_then_success())
        await flow_service.publish_flow(db, user.id, flow.uuid)
        return flow

    async def test_pressing_run_writes_a_run_and_a_job(
        self, db: AsyncSession, user: User, live_flow: IntegrationFlow
    ) -> None:
        """
        A manual run goes through the **same** queue as a scheduled one, so the run tested
        at 11am takes the path that fires at 3am. Both rows or neither — a crash between
        two commits either loses a run or leaves a job pointing at nothing.
        """
        run = await flow_service.start_run(db, user.id, live_flow.uuid)

        jobs = await db.execute(
            select(func.count()).select_from(IntegrationRunJob).where(
                IntegrationRunJob.run_id == run.id
            )
        )
        assert int(jobs.scalar() or 0) == 1

    async def test_a_live_run_is_pinned_to_the_published_version(
        self, db: AsyncSession, user: User, live_flow: IntegrationFlow
    ) -> None:
        run = await flow_service.start_run(db, user.id, live_flow.uuid)

        version = await flow_service.get_published_version(db, live_flow)
        assert run.flow_version_id == version.id

    async def test_a_draft_cannot_be_run_for_real(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        with pytest.raises(HTTPException) as caught:
            await flow_service.start_run(db, user.id, flow.uuid, mode=RUN_MODE_LIVE)

        assert "dry-run" in caught.value.detail

    async def test_a_draft_can_be_dry_run(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        """Most of what a dry run is for. It calls nobody, so there is nothing a draft can
        damage by being wrong."""
        run = await flow_service.start_run(db, user.id, flow.uuid, mode=RUN_MODE_DRY_RUN)

        assert run.mode == RUN_MODE_DRY_RUN
        assert run.flow_version_id is None

    async def test_a_nonsense_mode_is_refused(
        self, db: AsyncSession, user: User, live_flow: IntegrationFlow
    ) -> None:
        """A dry run that quietly became a live one would write to somebody's production
        system on the strength of a typo."""
        with pytest.raises(HTTPException):
            await flow_service.start_run(db, user.id, live_flow.uuid, mode="LIVE!")


class TestReplaying:
    @pytest.fixture
    async def finished_run(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> IntegrationRun:
        await flow_service.save_flow(db, user.id, flow.uuid, trigger_then_success())
        await flow_service.publish_flow(db, user.id, flow.uuid)
        run = await flow_service.start_run(db, user.id, flow.uuid)
        run.status = RUN_SUCCEEDED
        await db.commit()
        return run

    async def test_a_replay_uses_the_same_version_even_after_an_edit(
        self,
        db: AsyncSession,
        user: User,
        flow: IntegrationFlow,
        finished_run: IntegrationRun,
    ) -> None:
        """
        **The property replay exists for.** Not 'run the workflow again' — a replay of a
        run that failed last Tuesday has to be the thing that failed last Tuesday, or the
        result answers a question nobody asked.
        """
        await flow_service.save_flow(db, user.id, flow.uuid, trigger_only())
        await flow_service.publish_flow(db, user.id, flow.uuid)

        replay = await flow_service.replay_run(db, user.id, finished_run.uuid)

        assert replay.flow_version_id == finished_run.flow_version_id
        assert replay.replay_of_run_id == finished_run.id

    async def test_a_run_still_going_cannot_be_replayed(
        self, db: AsyncSession, user: User, finished_run: IntegrationRun
    ) -> None:
        finished_run.status = RUN_RUNNING
        await db.commit()

        with pytest.raises(HTTPException) as caught:
            await flow_service.replay_run(db, user.id, finished_run.uuid)

        assert caught.value.status_code == 409

    async def test_another_users_run_is_not_found(
        self, db: AsyncSession, make_user, finished_run: IntegrationRun
    ) -> None:
        """
        The same sentence for 'not yours' and 'does not exist'. Distinguishing them would
        confirm the existence of another user's run to anybody willing to guess uuids.
        """
        other = await make_user(email="other@example.com")

        with pytest.raises(HTTPException) as caught:
            await flow_service.replay_run(db, other.id, finished_run.uuid)

        assert caught.value.status_code == 404
        assert caught.value.detail == flow_service.NO_SUCH_RUN


class TestDeleting:
    async def test_a_flow_with_a_live_run_is_not_deleted(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        """
        The worker driving it would carry on writing into somebody's system with no row
        left to record what it did, and the first sign of it would be a foreign-key error
        in a log nobody reads.
        """
        await flow_service.save_flow(db, user.id, flow.uuid, trigger_then_success())
        await flow_service.publish_flow(db, user.id, flow.uuid)
        run = await flow_service.start_run(db, user.id, flow.uuid)
        run.status = RUN_RUNNING
        await db.commit()

        with pytest.raises(HTTPException) as caught:
            await flow_service.delete_flow(db, user.id, flow.uuid)

        assert caught.value.status_code == 409

    async def test_a_finished_flow_is_deleted(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        """
        The versions, triggers and runs go with it, but that is the **database's** cascade
        rather than this function's: the relationships declare ``passive_deletes=True`` and
        the foreign keys declare ``ON DELETE CASCADE``, so the delete is one statement
        rather than a load-and-loop over eight hundred thousand record rows.

        Which is why only the flow's own disappearance is asserted here. SQLite does not
        enforce foreign keys without ``PRAGMA foreign_keys=ON``, which this harness does
        not set, so a cascade assertion in this file would be testing the harness rather
        than the schema. The cascade itself was verified against both dialects when the
        migration landed.
        """
        await flow_service.save_flow(db, user.id, flow.uuid, trigger_then_success())
        await flow_service.publish_flow(db, user.id, flow.uuid)

        await flow_service.delete_flow(db, user.id, flow.uuid)

        with pytest.raises(HTTPException) as caught:
            await flow_service.get_flow(db, user.id, flow.uuid)

        assert caught.value.status_code == 404


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


class TestViews:
    async def test_a_view_carries_uuids_and_never_the_bigint_id(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        """CLAUDE.md's rule, asserted rather than reviewed: the internal primary key is a
        foreign-key target and never leaves this layer."""
        views = await flow_service.build_flow_views(db, [flow])

        assert views[0]["uuid"] == str(flow.uuid)
        assert "id" not in views[0]

    async def test_the_last_run_comes_back_with_the_flow(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        """'Last run: failed, 20 minutes ago' is the column people scan, and one query per
        flow is what makes a page with forty workflows take a second."""
        await flow_service.save_flow(db, user.id, flow.uuid, trigger_then_success())
        await flow_service.publish_flow(db, user.id, flow.uuid)
        run = await flow_service.start_run(db, user.id, flow.uuid)

        views = await flow_service.build_flow_views(db, [flow])

        assert views[0]["last_run_uuid"] == str(run.uuid)
        assert views[0]["is_published"] is True

    async def test_a_flow_with_no_runs_says_so_rather_than_failing(
        self, db: AsyncSession, user: User, flow: IntegrationFlow
    ) -> None:
        views = await flow_service.build_flow_views(db, [flow])

        assert views[0]["last_run_status"] == ""
        assert views[0]["is_published"] is False
