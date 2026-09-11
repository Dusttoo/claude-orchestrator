"""Shared required delivery fields for decomposition and Jira publication."""
def validate_delivery(item, error):
    owner = item.get("migration_owner")
    tests = item.get("test_plan")
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 32:
        raise error("slice requires migration_owner (owning slice id or 'none')")
    if (not isinstance(tests, list) or not 1 <= len(tests) <= 30
            or any(not isinstance(test, str) or not test.strip() or len(test) > 2000 for test in tests)):
        raise error("slice requires a bounded explicit test_plan")


def validate_owner(item, identifiers, error):
    if item["migration_owner"] != "none" and item["migration_owner"] not in identifiers:
        raise error("migration_owner must name a slice in this decomposition or 'none'")
