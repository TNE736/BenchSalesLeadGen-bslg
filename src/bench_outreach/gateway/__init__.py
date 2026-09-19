"""Agent Gateway — the single ingress for HubSpot triggers.

HubSpot fires a webhook when a contact property changes. The gateway verifies
the request really came from HubSpot, keeps only the events whose property and
value are a configured route (today: ``decision_maker`` = Yes), drops
redeliveries it has already handled, mints a stable ``trigger_id`` and hands
``trigger_id`` + the contact's HubSpot ``object_id`` to the next agent.

It holds no lead data and is not a system of record. The agent that receives
the hand-off reads the contact from HubSpot itself.
"""
