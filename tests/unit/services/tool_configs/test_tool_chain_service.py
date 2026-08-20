"""
Tests for app/services/tool_configs/tool_chain_service.py — the rules about what
may be embedded in what.

Nesting turns one tool's result into another tool's filter, and every rule here
exists because breaking it produces a *plausible wrong answer* rather than an
error: a cycle hangs a conversation, a cross-datasource child matches ids that
merely look alike, and a child deleted out from under its parent leaves the parent
running with one fewer restriction and no sign of it. So the refusals are tested as
carefully as the successes, and each one is asserted to *say* what is wrong —
those sentences are what the operator sees in the form.

No LangGraph here: this module is deliberately free of it so these rules can be
run anywhere. Executing a chain is tested in ``test_tool_chain_graph.py``.
"""

from __future__ import annotations

import pytest
from litestar.exceptions import HTTPException

from app.models.data_agents import DataAgent
from app.models.datasource import DataSource
from app.models.tool_configs import ToolConfig
from app.services.tool_configs import tool_chain_service as svc


@pytest.fixture
def make_agent(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str, **kwargs):  # noqa: ANN001
        row = DataAgent(user_id=owner.id, name=name, **kwargs)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_datasource(db):  # noqa: ANN001, ANN201
    async def _make(owner, name: str, **kwargs):  # noqa: ANN001
        row = DataSource(
            user_id=owner.id,
            datasource_name=name,
            db_type=kwargs.pop("db_type", "postgres"),
            password_encrypted="enc",
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
def make_tool(db):  # noqa: ANN001, ANN201
    async def _make(agent, datasource, tool_name: str, **kwargs):  # noqa: ANN001
        row = ToolConfig(
            data_agent_id=agent.id,
            datasource_id=datasource.id,
            tool_name=tool_name,
            table_name=kwargs.pop("table_name", "orders"),
            config=kwargs.pop("config", {"columns": [{"column": "id", "alias": ""}]}),
            **kwargs,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    return _make


@pytest.fixture
async def workspace(db, user, make_agent, make_datasource, make_tool):  # noqa: ANN001, ANN201
    """One agent, one datasource, and three tools on it."""
    agent = await make_agent(user, "reporter")
    datasource = await make_datasource(user, "warehouse")

    return {
        "agent": agent,
        "datasource": datasource,
        "parent": await make_tool(agent, datasource, "projects_by_client"),
        "child": await make_tool(agent, datasource, "active_clients"),
        # Returns `client_id`, so it can feed `active_clients.id` — a three-level
        # chain needs each level to return what the one above filters on.
        "grandchild": await make_tool(
            agent,
            datasource,
            "paid_invoices",
            config={"columns": [{"column": "client_id", "alias": ""}]},
        ),
    }


def entry(child, column: str = "id", target: str = "client_id") -> dict:
    return {
        "child_id": str(child.uuid),
        "child_column": column,
        "parent_reference": target,
    }


async def link(db, user, parent, child, **kwargs) -> None:  # noqa: ANN001
    """Save one link the way the service does, validation included."""
    links = await svc.validated_children(db, user.id, parent, [entry(child, **kwargs)])
    await svc.replace_child_links(db, parent.id, links)
    await db.commit()


class TestWhatMayBeEmbedded:
    async def test_a_tool_on_the_same_datasource_is_accepted(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        await link(db, user, workspace["parent"], workspace["child"])

        chain = await svc.chain_for_tool(
            db, workspace["parent"], workspace["datasource"],
        )

        assert [node.tool.tool_name for node in chain.children] == ["active_clients"]
        assert chain.children[0].child_column == "id"
        assert chain.children[0].parent_reference == "client_id"

    async def test_a_tool_from_another_agent_is_accepted(
        self, db, user, make_agent, make_tool, workspace  # noqa: ANN001
    ) -> None:
        """Agents are how tools are *given out*, not how they are owned. Sharing a
        child across agents is the case the runtime expansion exists for."""
        other_agent = await make_agent(user, "finance")
        theirs = await make_tool(other_agent, workspace["datasource"], "paid_totals")

        await link(db, user, workspace["parent"], theirs)

        chain = await svc.chain_for_tool(
            db, workspace["parent"], workspace["datasource"],
        )

        assert chain.children[0].tool.tool_name == "paid_totals"

    async def test_a_tool_on_another_datasource_is_refused(
        self, db, user, make_datasource, make_tool, workspace  # noqa: ANN001
    ) -> None:
        """Only values cross the boundary, so it would *run* — and match ids from
        one system against ids in another, which is a coincidence, not a join."""
        elsewhere = await make_datasource(user, "crm")
        stranger = await make_tool(workspace["agent"], elsewhere, "crm_accounts")

        with pytest.raises(HTTPException, match="different datasource"):
            await svc.validated_children(
                db, user.id, workspace["parent"], [entry(stranger)],
            )

    async def test_another_user_s_tool_is_not_found(
        self, db, user, make_user, make_agent, make_datasource, make_tool, workspace  # noqa: ANN001
    ) -> None:
        stranger = await make_user("stranger@example.com")
        their_agent = await make_agent(stranger, "theirs")
        their_datasource = await make_datasource(stranger, "theirs")
        their_tool = await make_tool(their_agent, their_datasource, "secrets")

        with pytest.raises(HTTPException) as excinfo:
            await svc.validated_children(
                db, user.id, workspace["parent"], [entry(their_tool)],
            )

        assert excinfo.value.status_code == 404

    async def test_a_disabled_tool_is_refused(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        """`is_enabled` is the operator's "stop using this". A parent running it
        anyway would make the switch a lie."""
        workspace["child"].is_enabled = False
        await db.commit()

        with pytest.raises(HTTPException, match="is disabled"):
            await svc.validated_children(
                db, user.id, workspace["parent"], [entry(workspace["child"])],
            )

    async def test_a_tool_cannot_embed_itself(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        with pytest.raises(HTTPException, match="cannot embed itself"):
            await svc.validated_children(
                db, user.id, workspace["parent"], [entry(workspace["parent"])],
            )

    async def test_a_cycle_through_another_tool_is_refused(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        """The one that would not be an error but a hang: the chain runner walks
        depth-first with no visited set."""
        await link(db, user, workspace["parent"], workspace["child"])

        with pytest.raises(HTTPException, match="cannot loop back"):
            await svc.validated_children(
                db, user.id, workspace["child"], [entry(workspace["parent"])],
            )

    async def test_a_new_tool_can_embed_anything_it_owns(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        """A tool that does not exist yet cannot be in a cycle, so the checks that
        need an id are skipped rather than guessed at."""
        prospective = ToolConfig(
            id=None,
            tool_name="new_tool",
            datasource_id=workspace["datasource"].id,
            table_name="projects",
            query_mode="builder",
            config={},
        )

        links = await svc.validated_children(
            db, user.id, prospective, [entry(workspace["child"])],
        )

        assert links[0]["child_id"] == workspace["child"].id


class TestTheCaps:
    async def test_more_children_than_allowed_is_refused(
        self, db, user, make_tool, workspace  # noqa: ANN001
    ) -> None:
        children = [
            entry(await make_tool(
                workspace["agent"], workspace["datasource"], f"child_{index}",
            ))
            for index in range(svc.MAX_CHILDREN_PER_TOOL + 1)
        ]

        with pytest.raises(HTTPException, match="at most"):
            await svc.validated_children(db, user.id, workspace["parent"], children)

    async def test_the_same_child_on_the_same_target_twice_is_refused(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        with pytest.raises(HTTPException, match="embedded twice"):
            await svc.validated_children(
                db,
                user.id,
                workspace["parent"],
                [entry(workspace["child"]), entry(workspace["child"])],
            )

    async def test_the_same_child_on_two_targets_is_allowed(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        """One tool returning client ids can restrict both `owner_id` and
        `billed_to_id` — two questions, not a duplicate."""
        links = await svc.validated_children(
            db,
            user.id,
            workspace["parent"],
            [
                entry(workspace["child"], target="owner_id"),
                entry(workspace["child"], target="billed_to_id"),
            ],
        )

        assert len(links) == 2

    async def test_a_chain_deeper_than_the_limit_is_refused(
        self, db, user, make_tool, workspace  # noqa: ANN001
    ) -> None:
        """Measured over the whole chain, not just downwards: every level is a
        round trip inside a turn a visitor is waiting on."""
        previous = workspace["parent"]

        for index in range(svc.MAX_CHAIN_DEPTH - 1):
            current = await make_tool(
                workspace["agent"], workspace["datasource"], f"level_{index}",
            )
            await link(db, user, previous, current)
            previous = current

        one_too_deep = await make_tool(
            workspace["agent"], workspace["datasource"], "too_deep",
        )

        with pytest.raises(HTTPException, match="deep"):
            await svc.validated_children(db, user.id, previous, [entry(one_too_deep)])


class TestTheColumnAndTheTarget:
    async def test_a_column_the_child_does_not_return_is_refused(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        with pytest.raises(HTTPException, match="does not return a column"):
            await svc.validated_children(
                db,
                user.id,
                workspace["parent"],
                [entry(workspace["child"], column="nope")],
            )

    async def test_an_alias_is_the_name_the_column_comes_back_under(
        self, db, user, make_tool, workspace  # noqa: ANN001
    ) -> None:
        aliased = await make_tool(
            workspace["agent"],
            workspace["datasource"],
            "aliased",
            config={"columns": [{"column": "id", "alias": "client_ref"}]},
        )

        assert svc.child_output_columns(aliased) == ["client_ref"]

    async def test_an_aggregation_is_named_the_way_the_executor_labels_it(
        self, db, user, make_tool, workspace  # noqa: ANN001
    ) -> None:
        counted = await make_tool(
            workspace["agent"],
            workspace["datasource"],
            "counted",
            config={"aggregations": [{"type": "count", "column": "id", "alias": ""}]},
        )

        assert svc.child_output_columns(counted) == ["count_id"]

    async def test_a_tool_selecting_everything_promises_no_column_list(
        self, db, user, make_tool, workspace  # noqa: ANN001
    ) -> None:
        """It expands to every *active* column when it runs, a set Data Sources can
        change — so the name is checked against the real result instead."""
        everything = await make_tool(
            workspace["agent"], workspace["datasource"], "everything", config={},
        )

        assert svc.child_output_columns(everything) == []

        links = await svc.validated_children(
            db, user.id, workspace["parent"], [entry(everything, column="anything")],
        )
        assert links[0]["child_column"] == "anything"

    async def test_a_sql_child_promises_no_column_list_either(
        self, db, user, make_tool, workspace  # noqa: ANN001
    ) -> None:
        statement = await make_tool(
            workspace["agent"],
            workspace["datasource"],
            "written",
            query_mode="sql",
            config={},
            sql_query="SELECT DISTINCT client_id FROM invoices",
        )

        assert svc.child_output_columns(statement) == []


class TestASqlParent:
    @pytest.fixture
    async def sql_parent(self, make_tool, workspace):  # noqa: ANN001, ANN201
        return await make_tool(
            workspace["agent"],
            workspace["datasource"],
            "written_parent",
            query_mode="sql",
            config={},
            sql_query="SELECT name FROM projects WHERE client_id IN :active_clients",
        )

    async def test_a_placeholder_the_statement_uses_is_accepted(
        self, db, user, workspace, sql_parent  # noqa: ANN001
    ) -> None:
        links = await svc.validated_children(
            db,
            user.id,
            sql_parent,
            [entry(workspace["child"], target="active_clients")],
        )

        assert links[0]["parent_reference"] == "active_clients"

    async def test_a_placeholder_the_statement_does_not_use_is_refused(
        self, db, user, workspace, sql_parent  # noqa: ANN001
    ) -> None:
        """It would bind nothing and crash the moment the tool ran."""
        with pytest.raises(HTTPException, match="does not use ':nope'"):
            await svc.validated_children(
                db, user.id, sql_parent, [entry(workspace["child"], target="nope")],
            )

    async def test_a_placeholder_with_no_child_to_fill_it_is_refused(
        self, db, user, make_tool, workspace  # noqa: ANN001
    ) -> None:
        """The same fault from the other side, and just as unrunnable."""
        two_holes = await make_tool(
            workspace["agent"],
            workspace["datasource"],
            "two_holes",
            query_mode="sql",
            config={},
            sql_query=(
                "SELECT name FROM projects WHERE client_id IN :active_clients "
                "AND owner_id IN :owners"
            ),
        )

        with pytest.raises(HTTPException, match="':owners'"):
            await svc.validated_children(
                db,
                user.id,
                two_holes,
                [entry(workspace["child"], target="active_clients")],
            )

    async def test_a_placeholder_with_no_children_at_all_is_refused(
        self, db, user, sql_parent  # noqa: ANN001
    ) -> None:
        """The empty case is the same unrunnable query, and it is the one that gets
        missed: an early return on "no children" let it be saved."""
        with pytest.raises(HTTPException, match="':active_clients'"):
            await svc.validated_children(db, user.id, sql_parent, [])

    async def test_a_statement_with_no_placeholders_needs_no_children(
        self, db, user, make_tool, workspace  # noqa: ANN001
    ) -> None:
        plain = await make_tool(
            workspace["agent"],
            workspace["datasource"],
            "plain_sql",
            query_mode="sql",
            config={},
            sql_query="SELECT DISTINCT name FROM projects",
        )

        assert await svc.validated_children(db, user.id, plain, []) == []

    async def test_a_name_that_is_not_a_placeholder_is_refused(
        self, db, user, workspace, sql_parent  # noqa: ANN001
    ) -> None:
        with pytest.raises(HTTPException, match="must start with a letter"):
            await svc.validated_children(
                db, user.id, sql_parent, [entry(workspace["child"], target="1bad!")],
            )


class TestTheGuards:
    async def test_an_embedded_tool_cannot_be_deleted(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        await link(db, user, workspace["parent"], workspace["child"])

        with pytest.raises(HTTPException) as excinfo:
            await svc.require_not_embedded(
                db, workspace["child"], "cannot be deleted",
            )

        assert "projects_by_client" in excinfo.value.detail
        assert "more rows than they should" in excinfo.value.detail

    async def test_a_tool_nothing_embeds_is_free(
        self, db, workspace  # noqa: ANN001
    ) -> None:
        await svc.require_not_embedded(db, workspace["child"], "cannot be deleted")

    async def test_the_parents_of_a_tool_are_named(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        await link(db, user, workspace["parent"], workspace["child"])

        names = await svc.parent_names(db, [workspace["child"].id])

        assert names == {workspace["child"].id: ["projects_by_client"]}


class TestTheResolvedTree:
    @pytest.fixture(autouse=True)
    async def three_levels(self, db, user, workspace):  # noqa: ANN001, ANN201
        await link(
            db, user, workspace["child"], workspace["grandchild"],
            column="client_id", target="id",
        )
        await link(db, user, workspace["parent"], workspace["child"])

    async def test_the_tree_is_resolved_to_its_full_depth(
        self, db, workspace  # noqa: ANN001
    ) -> None:
        chain = await svc.chain_for_tool(
            db, workspace["parent"], workspace["datasource"],
        )

        assert chain.children[0].tool.tool_name == "active_clients"
        assert chain.children[0].children[0].tool.tool_name == "paid_invoices"

    async def test_walk_returns_the_deepest_node_first(
        self, db, workspace  # noqa: ANN001
    ) -> None:
        """The order the chain has to run in — a node cannot be executed before the
        tools that feed it."""
        chain = await svc.chain_for_tool(
            db, workspace["parent"], workspace["datasource"],
        )

        assert [node.tool.tool_name for node in chain.walk()] == [
            "paid_invoices", "active_clients", "projects_by_client",
        ]

    async def test_the_view_flattens_the_tree_with_its_indents(
        self, db, workspace  # noqa: ANN001
    ) -> None:
        chain = await svc.chain_for_tool(
            db, workspace["parent"], workspace["datasource"],
        )

        assert svc.chain_view(chain) == [
            {
                "depth": 1,
                "tool_uuid": str(workspace["child"].uuid),
                "tool_name": "active_clients",
                "child_column": "id",
                "parent_reference": "client_id",
                "parent_name": "projects_by_client",
                "binding_mode": "in_list",
                "iterates": False,
                "value_alias": "",
                "is_enabled": True,
            },
            {
                "depth": 2,
                "tool_uuid": str(workspace["grandchild"].uuid),
                "tool_name": "paid_invoices",
                "child_column": "client_id",
                "parent_reference": "id",
                "parent_name": "active_clients",
                "binding_mode": "in_list",
                "iterates": False,
                "value_alias": "",
                "is_enabled": True,
            },
        ]

    async def test_a_tool_that_embeds_nothing_has_an_empty_view(
        self, db, workspace  # noqa: ANN001
    ) -> None:
        chain = await svc.chain_for_tool(
            db, workspace["grandchild"], workspace["datasource"],
        )

        assert svc.chain_view(chain) == []

    async def test_descendants_are_what_an_agent_inherits(
        self, db, workspace  # noqa: ANN001
    ) -> None:
        """Giving an agent the parent gives it the whole chain — that is what makes
        a child callable on its own from that agent."""
        rows = await svc.descendant_rows(db, [workspace["parent"].id])

        assert sorted(tool.tool_name for tool, _ds in rows) == [
            "active_clients", "paid_invoices",
        ]

    async def test_descendants_exclude_the_tools_asked_about(
        self, db, workspace  # noqa: ANN001
    ) -> None:
        rows = await svc.descendant_rows(
            db, [workspace["parent"].id, workspace["child"].id],
        )

        assert [tool.tool_name for tool, _ds in rows] == ["paid_invoices"]


class TestThePicker:
    async def test_it_offers_the_other_tools_on_the_datasource(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        offered = await svc.embeddable_tools(
            db,
            user.id,
            workspace["datasource"].uuid,
            exclude_uuid=workspace["parent"].uuid,
        )

        assert sorted(tool["tool_name"] for tool in offered) == [
            "active_clients", "paid_invoices",
        ]

    async def test_it_never_offers_a_tool_that_already_embeds_this_one(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        """The cycle rule applied before the operator can build one."""
        await link(db, user, workspace["parent"], workspace["child"])

        offered = await svc.embeddable_tools(
            db,
            user.id,
            workspace["datasource"].uuid,
            exclude_uuid=workspace["child"].uuid,
        )

        assert [tool["tool_name"] for tool in offered] == ["paid_invoices"]

    async def test_it_never_offers_a_disabled_tool(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        workspace["child"].is_enabled = False
        await db.commit()

        offered = await svc.embeddable_tools(
            db,
            user.id,
            workspace["datasource"].uuid,
            exclude_uuid=workspace["parent"].uuid,
        )

        assert [tool["tool_name"] for tool in offered] == ["paid_invoices"]

    async def test_it_carries_the_columns_each_tool_returns(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        offered = await svc.embeddable_tools(
            db,
            user.id,
            workspace["datasource"].uuid,
            exclude_uuid=workspace["parent"].uuid,
        )
        columns = {tool["tool_name"]: tool["columns"] for tool in offered}

        assert columns == {"active_clients": ["id"], "paid_invoices": ["client_id"]}

    async def test_another_user_s_datasource_offers_nothing(
        self, db, make_user, workspace  # noqa: ANN001
    ) -> None:
        stranger = await make_user("nobody@example.com")

        assert await svc.embeddable_tools(
            db, stranger.id, workspace["datasource"].uuid,
        ) == []


class TestHowTheValuesAreUsed:
    """
    ``binding_mode`` — whether the child's values are matched all at once or the
    parent is run once per value, and the rules that only apply to the second.
    """

    async def test_the_default_is_the_behaviour_every_link_already_had(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        """A form posted by an older cached page saves the tool it describes rather
        than an error, which is what makes the column's default safe."""
        links = await svc.validated_children(
            db, user.id, workspace["parent"], [entry(workspace["child"])],
        )

        assert links[0]["binding_mode"] == "in_list"
        assert links[0]["value_alias"] is None

    async def test_an_iterating_link_is_accepted_and_recorded(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        links = await svc.validated_children(
            db,
            user.id,
            workspace["parent"],
            [{**entry(workspace["child"]), "binding_mode": "each",
              "value_alias": "client"}],
        )

        assert links[0]["binding_mode"] == "each"
        assert links[0]["value_alias"] == "client"

    async def test_an_unknown_mode_is_refused(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        with pytest.raises(HTTPException, match="how a nested tool's values"):
            await svc.validated_children(
                db,
                user.id,
                workspace["parent"],
                [{**entry(workspace["child"]), "binding_mode": "sideways"}],
            )

    async def test_only_one_child_may_iterate(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        """
        Two would run the parent once per *combination*, and the row cap makes that
        the first few combinations rather than a bigger answer — with nothing saying
        the rest were never run.
        """
        with pytest.raises(HTTPException, match="Only one nested tool"):
            await svc.validated_children(
                db,
                user.id,
                workspace["parent"],
                [
                    {**entry(workspace["child"]), "binding_mode": "each"},
                    {**entry(workspace["grandchild"], column="client_id",
                             target="id"),
                     "binding_mode": "each"},
                ],
            )

    async def test_a_value_alias_on_a_list_binding_is_refused(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        """A list produces one result set, so there is no single value to record —
        and a field that silently does nothing is one the operator will swear they
        set."""
        with pytest.raises(HTTPException, match="no single value to record"):
            await svc.validated_children(
                db,
                user.id,
                workspace["parent"],
                [{**entry(workspace["child"]), "value_alias": "client"}],
            )

    async def test_a_value_alias_has_to_be_a_name(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        """It becomes a key in the result rows and is grouped by like any other
        output column."""
        with pytest.raises(HTTPException, match="must start with a letter"):
            await svc.validated_children(
                db,
                user.id,
                workspace["parent"],
                [{**entry(workspace["child"]), "binding_mode": "each",
                  "value_alias": "2 clients!"}],
            )

    async def test_a_blank_alias_is_not_a_refusal(
        self, db, user, workspace  # noqa: ANN001
    ) -> None:
        """A query that already returns the value needs no second copy of it."""
        links = await svc.validated_children(
            db,
            user.id,
            workspace["parent"],
            [{**entry(workspace["child"]), "binding_mode": "each",
              "value_alias": "   "}],
        )

        assert links[0]["value_alias"] is None


class TestPlaceholderArity:
    """
    A placeholder used in a shape its binding cannot take.

    Both are syntax errors the *database* reports, mid-conversation, months after
    the tool was saved. A text check next to the placeholder catches the two
    mistakes an operator actually makes.
    """

    @pytest.fixture
    async def in_parent(self, make_tool, workspace):  # noqa: ANN001, ANN201
        return await make_tool(
            workspace["agent"],
            workspace["datasource"],
            "in_parent",
            query_mode="sql",
            config={},
            sql_query="SELECT name FROM projects WHERE client_id IN :clients",
        )

    @pytest.fixture
    async def equals_parent(self, make_tool, workspace):  # noqa: ANN001, ANN201
        return await make_tool(
            workspace["agent"],
            workspace["datasource"],
            "equals_parent",
            query_mode="sql",
            config={},
            sql_query="SELECT name FROM projects WHERE client_id = :clients",
        )

    async def test_an_iterating_link_into_an_in_clause_is_refused(
        self, db, user, workspace, in_parent  # noqa: ANN001
    ) -> None:
        """``id IN ?`` with a scalar bound is not a query."""
        with pytest.raises(HTTPException, match="which expects a list"):
            await svc.validated_children(
                db,
                user.id,
                in_parent,
                [{**entry(workspace["child"], target="clients"),
                  "binding_mode": "each"}],
            )

    async def test_a_list_link_into_a_comparison_is_refused(
        self, db, user, workspace, equals_parent  # noqa: ANN001
    ) -> None:
        """``id = (?, ?, ?)`` is not one either."""
        with pytest.raises(HTTPException, match="as a single value"):
            await svc.validated_children(
                db,
                user.id,
                equals_parent,
                [entry(workspace["child"], target="clients")],
            )

    async def test_the_matching_pairs_are_accepted(
        self, db, user, workspace, in_parent, equals_parent  # noqa: ANN001
    ) -> None:
        assert await svc.validated_children(
            db, user.id, in_parent, [entry(workspace["child"], target="clients")],
        )
        assert await svc.validated_children(
            db,
            user.id,
            equals_parent,
            [{**entry(workspace["child"], target="clients"),
              "binding_mode": "each"}],
        )

    async def test_a_placeholder_inside_a_string_is_not_a_shape(
        self, db, user, make_tool, workspace  # noqa: ANN001
    ) -> None:
        """Literals are blanked before the check, so a colon in a LIKE pattern is
        not read as a comparison against the parameter."""
        parent = await make_tool(
            workspace["agent"],
            workspace["datasource"],
            "pattern_parent",
            query_mode="sql",
            config={},
            sql_query=(
                "SELECT name FROM projects WHERE tags LIKE '%a=:clients%' "
                "AND client_id IN :clients"
            ),
        )

        assert await svc.validated_children(
            db, user.id, parent, [entry(workspace["child"], target="clients")],
        )


class TestAToolThatNeedsArguments:
    """
    A child declaring required values the assistant supplies cannot be embedded.

    An inner tool is never called by the model — the model calls the parent — so
    nothing would ever fill them, and the chain would fail on its first run with a
    message about a parameter the operator did not know was involved.
    """

    @pytest.fixture
    async def needy(self, make_tool, workspace):  # noqa: ANN001, ANN201
        return await make_tool(
            workspace["agent"],
            workspace["datasource"],
            "needs_a_date",
            query_mode="sql",
            config={},
            sql_query="SELECT id FROM clients WHERE created_at > :since",
            sql_params=[{"param": "since", "type": "text", "required": True}],
        )

    async def test_it_is_refused_with_the_name_it_needs(
        self, db, user, workspace, needy  # noqa: ANN001
    ) -> None:
        with pytest.raises(HTTPException, match="'since'"):
            await svc.validated_children(
                db, user.id, workspace["parent"], [entry(needy)],
            )

    async def test_an_optional_one_is_fine(
        self, db, user, make_tool, workspace  # noqa: ANN001
    ) -> None:
        """It binds NULL, which a statement written for it reads as no filter."""
        relaxed = await make_tool(
            workspace["agent"],
            workspace["datasource"],
            "optional_date",
            query_mode="sql",
            config={},
            sql_query=(
                "SELECT id FROM clients WHERE (:since IS NULL OR created_at > :since)"
            ),
            sql_params=[{"param": "since", "type": "text", "required": False}],
        )

        assert await svc.validated_children(
            db, user.id, workspace["parent"], [entry(relaxed)],
        )


class TestDeclaredValuesFillPlaceholdersToo:
    async def test_a_placeholder_a_declared_value_fills_needs_no_child(
        self, db, user, make_tool, workspace  # noqa: ANN001
    ) -> None:
        """
        "Is this name accounted for" is one question however it is answered.
        Splitting it would let a tool be saved with a name each check thought the
        other one covered.
        """
        parent = await make_tool(
            workspace["agent"],
            workspace["datasource"],
            "asks_the_agent",
            query_mode="sql",
            config={},
            sql_query="SELECT name FROM projects WHERE client_id = :client",
            sql_params=[{"param": "client", "type": "number", "required": True}],
        )

        assert await svc.validated_children(db, user.id, parent, []) == []

    async def test_a_placeholder_nothing_fills_is_still_refused(
        self, db, user, make_tool, workspace  # noqa: ANN001
    ) -> None:
        parent = await make_tool(
            workspace["agent"],
            workspace["datasource"],
            "unfilled",
            query_mode="sql",
            config={},
            sql_query="SELECT name FROM projects WHERE client_id = :client",
        )

        with pytest.raises(HTTPException, match="which nothing fills"):
            await svc.validated_children(db, user.id, parent, [])
