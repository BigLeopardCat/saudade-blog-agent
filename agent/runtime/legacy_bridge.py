"""Explicit compatibility boundary for native runtime calls into the old graph.

New runtime modules must use this module when a graph-shaped checkpoint value is
still required. Keeping the import here makes the remaining dependency visible
and gives the migration a single removal point when graph checkpoints retire.
"""


def graph_input(messages):
    """Build the legacy checkpoint state without importing the graph elsewhere."""
    from agent.graph import graph_input as build_graph_input

    return build_graph_input(messages)

